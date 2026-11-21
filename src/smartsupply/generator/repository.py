"""Lecture de l'état de la simulation et écriture d'une journée en base."""
from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection

from smartsupply.generator.clients import ClientProfile, build_client_profile
from smartsupply.generator.demand import ProductProfile, build_profile
from smartsupply.generator.simulator import (
    DEMAND_WINDOW_DAYS,
    DayResult,
    Promotion,
    PurchaseOrder,
    SimulationState,
    timestamp,
)

# clé interne -> (table, colonne identifiant)
ID_COLUMNS = {
    "client": ("raw.clients", "client_id"),
    "order": ("raw.orders", "order_id"),
    "order_item": ("raw.order_items", "order_item_id"),
    "po": ("raw.purchase_orders", "po_id"),
    "po_item": ("raw.purchase_order_items", "po_item_id"),
    "movement": ("raw.stock_movements", "movement_id"),
    "promotion": ("raw.promotions", "promotion_id"),
}


# -----------------------------------------------------------------------------
# Lecture
# -----------------------------------------------------------------------------
def last_simulated_date(conn: Connection) -> date | None:
    return conn.execute(text("SELECT max(simulated_date) FROM raw.simulation_runs")).scalar()


def check_seed(conn: Connection, seed: int) -> None:
    """Refuse de mélanger deux seeds dans le même historique."""
    previous = conn.execute(
        text("SELECT seed FROM raw.simulation_runs ORDER BY run_id LIMIT 1")
    ).scalar()
    if previous is not None and previous != seed:
        raise ValueError(
            f"L'historique a été généré avec seed={previous}, pas {seed}. "
            "Utilise la même seed ou lance database/reset_simulation.sql."
        )


def load_profiles(conn: Connection, day: date, seed: int) -> list[ProductProfile]:
    rows = conn.execute(
        text(
            """
            SELECT p.product_id, p.category, p.supplier_id, p.unit_price, p.unit_cost,
                   s.lead_time_days, s.reliability_score
            FROM raw.products p
            JOIN raw.suppliers s USING (supplier_id)
            WHERE p.active AND p.created_at < :day_end
            ORDER BY p.product_id
            """
        ),
        {"day_end": timestamp(day + timedelta(days=1))},
    )
    return [
        build_profile(
            r.product_id, r.category, r.supplier_id, r.unit_price, r.unit_cost,
            r.lead_time_days, float(r.reliability_score), seed,
        )
        for r in rows
    ]


def load_warehouse_shares(conn: Connection) -> dict[int, float]:
    """Part de la demande de chaque entrepôt, proportionnelle à sa capacité."""
    rows = conn.execute(
        text("SELECT warehouse_id, capacity FROM raw.warehouses ORDER BY warehouse_id")
    ).all()
    total = sum(r.capacity for r in rows)
    return {r.warehouse_id: r.capacity / total for r in rows}


def load_clients(
    conn: Connection, day: date, shares: dict[int, float], seed: int,
    history_start: date | None = None,
) -> list[ClientProfile]:
    """Tous les clients déjà inscrits à la date D, avec leur profil caché
    (rythme d'achat, affinités, montée en puissance, date de départ)."""
    rows = conn.execute(
        text(
            "SELECT client_id, segment, (created_at AT TIME ZONE 'UTC')::date AS created "
            "FROM raw.clients WHERE created_at < :day_end ORDER BY client_id"
        ),
        {"day_end": timestamp(day + timedelta(days=1))},
    )
    return [
        build_client_profile(r.client_id, r.segment, shares, seed,
                             created=r.created, history_start=history_start or r.created)
        for r in rows
    ]


def load_state(conn: Connection, day: date) -> SimulationState:
    balances = {
        (r.product_id, r.warehouse_id): r.on_hand_qty
        for r in conn.execute(
            text("SELECT product_id, warehouse_id, on_hand_qty FROM raw.inventory_balances")
        )
    }

    pos: OrderedDict[int, PurchaseOrder] = OrderedDict()
    rows = conn.execute(
        text(
            """
            SELECT po.po_id, po.supplier_id, po.warehouse_id, po.ordered_at, po.expected_at,
                   s.reliability_score, i.po_item_id, i.product_id, i.ordered_qty
            FROM raw.purchase_orders po
            JOIN raw.suppliers s USING (supplier_id)
            JOIN raw.purchase_order_items i USING (po_id)
            WHERE po.status = 'in_transit'
            ORDER BY po.po_id, i.po_item_id
            """
        )
    )
    for r in rows:
        po = pos.get(r.po_id)
        if po is None:
            po = PurchaseOrder(
                r.po_id, r.supplier_id, r.warehouse_id, r.ordered_at, r.expected_at,
                float(r.reliability_score), [],
            )
            pos[r.po_id] = po
        po.items.append((r.po_item_id, r.product_id, r.ordered_qty))

    promotions = [
        Promotion(r.promotion_id, r.product_id, r.starts_at, r.ends_at, float(r.discount_pct))
        for r in conn.execute(
            text(
                "SELECT promotion_id, product_id, starts_at, ends_at, discount_pct "
                "FROM raw.promotions WHERE ends_at >= :day ORDER BY promotion_id"
            ),
            {"day": day},
        )
    ]

    next_ids = {
        key: conn.execute(text(f"SELECT coalesce(max({col}), 0) + 1 FROM {table}")).scalar_one()
        for key, (table, col) in ID_COLUMNS.items()
    }
    return SimulationState(day, balances, list(pos.values()), promotions, next_ids,
                           load_recent_demand(conn, day, balances))


def load_recent_demand(conn: Connection, day: date, balances: dict) -> dict[tuple[int, int], list[int]]:
    """Demande des DEMAND_WINDOW_DAYS jours simulés avant `day`, par produit x entrepôt.

    Identique à ce que la simulation garde en mémoire : un jour sans demande vaut 0,
    et seuls les jours réellement simulés comptent (liste plus courte au démarrage).
    """
    window_start = day - timedelta(days=DEMAND_WINDOW_DAYS)
    days = [
        r[0] for r in conn.execute(
            text("SELECT simulated_date FROM raw.simulation_runs "
                 "WHERE simulated_date >= :a AND simulated_date < :b ORDER BY simulated_date"),
            {"a": window_start, "b": day},
        )
    ]
    totals: dict[tuple[int, int, date], int] = {}
    for r in conn.execute(
        text(
            """
            SELECT oi.product_id, oi.warehouse_id, (o.ordered_at AT TIME ZONE 'UTC')::date AS d,
                   sum(oi.requested_qty) AS qty
            FROM raw.orders o
            JOIN raw.order_items oi USING (order_id)
            WHERE o.ordered_at >= :a AND o.ordered_at < :b
            GROUP BY 1, 2, 3
            """
        ),
        {"a": timestamp(window_start), "b": timestamp(day)},
    ):
        totals[(r.product_id, r.warehouse_id, r.d)] = int(r.qty)
    return {key: [totals.get((*key, d), 0) for d in days] for key in balances}


# -----------------------------------------------------------------------------
# Écriture
# -----------------------------------------------------------------------------
def _insert_many(conn: Connection, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    conn.execute(
        text(
            f"INSERT INTO {table} ({', '.join(cols)}) "
            f"VALUES ({', '.join(':' + c for c in cols)})"
        ),
        rows,
    )


def _save_balances(conn: Connection, balances: dict, updated_at: datetime) -> None:
    rows = [
        {"product_id": p, "warehouse_id": w, "on_hand_qty": q, "updated_at": updated_at}
        for (p, w), q in sorted(balances.items())
    ]
    if rows:
        conn.execute(
            text(
                """
                INSERT INTO raw.inventory_balances (product_id, warehouse_id, on_hand_qty, updated_at)
                VALUES (:product_id, :warehouse_id, :on_hand_qty, :updated_at)
                ON CONFLICT (product_id, warehouse_id)
                DO UPDATE SET on_hand_qty = EXCLUDED.on_hand_qty,
                              updated_at  = EXCLUDED.updated_at
                """
            ),
            rows,
        )


def save_initial_inventory(conn: Connection, balances: dict, movements: list[dict], day: date) -> None:
    _insert_many(conn, "raw.stock_movements", movements)
    _save_balances(conn, balances, timestamp(day))


def save_day(conn: Connection, result: DayResult) -> None:
    # L'ordre respecte les clés étrangères : parents avant enfants.
    _insert_many(conn, "raw.clients", result.clients)
    _insert_many(conn, "raw.promotions", result.promotions)
    _insert_many(conn, "raw.orders", result.orders)
    _insert_many(conn, "raw.order_items", result.order_items)
    _insert_many(conn, "raw.purchase_orders", result.purchase_orders)
    _insert_many(conn, "raw.purchase_order_items", result.purchase_order_items)

    if result.received_po_ids:
        params = {"day": result.day, "ids": result.received_po_ids}
        conn.execute(
            text(
                "UPDATE raw.purchase_orders SET status = 'received', received_at = :day "
                "WHERE po_id = ANY(:ids)"
            ),
            params,
        )
        conn.execute(
            text(
                "UPDATE raw.purchase_order_items SET received_qty = ordered_qty "
                "WHERE po_id = ANY(:ids)"
            ),
            params,
        )

    _insert_many(conn, "raw.stock_movements", result.stock_movements)
    _save_balances(conn, result.balances, timestamp(result.day, 86_399))  # fin de journée


def sync_identity_sequences(conn: Connection) -> None:
    """Le simulateur fournit ses propres id : on recale les séquences IDENTITY
    pour qu'une insertion manuelle sans id ne provoque pas de conflit."""
    for table, col in ID_COLUMNS.values():
        conn.execute(
            text(
                f"SELECT setval(pg_get_serial_sequence('{table}', '{col}'), "
                f"greatest((SELECT max({col}) FROM {table}), 1))"
            )
        )


def record_run(conn: Connection, seed: int, mode: str, day: date, started_at: datetime) -> None:
    conn.execute(
        text(
            "INSERT INTO raw.simulation_runs (seed, mode, simulated_date, started_at, status) "
            "VALUES (:seed, :mode, :day, :started_at, 'success')"
        ),
        {"seed": seed, "mode": mode, "day": day, "started_at": started_at},
    )
