"""Moteur de simulation d'UNE journée.

simulate_day(state, ...) -> (résultat de la journée, état du lendemain)

C'est une fonction PURE : elle ne lit ni n'écrit en base. Toute la logique métier
est ici, ce qui permet de la tester sans PostgreSQL. La lecture/écriture est
dans repository.py, l'enchaînement des jours dans runner.py.

Étapes d'une journée D (README section 5.3) :
  0. Inscription (rare) de nouveaux clients Faker.
  1. Réceptions des commandes fournisseurs arrivées.
  2. Promotions actives.
  3. Demande latente par produit x entrepôt, puis SÉLECTION des clients
     qui achètent ces unités (fréquence x affinité, voir clients.py).
  4. Une commande par client sélectionné + allocation du stock (servi / perdu).
  5. Réapprovisionnement (point de commande).
  6. Planification de futures promotions.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import numpy as np
from faker import Faker

from smartsupply.generator.clients import (
    NEW_CLIENT_B2B_SHARE,
    NEW_CLIENTS_PER_DAY,
    ClientProfile,
    ClientSelector,
    build_client_profile,
    client_base_factor,
)
from smartsupply.generator.demand import (
    OVERDISPERSION_K,
    ProductProfile,
    expected_demand,
    planned_daily_demand,
    sample_demand,
)

# --- Règle de réapprovisionnement -------------------------------------------
SERVICE_Z = 1.65      # stock de sécurité visant ~95 % de couverture
REVIEW_DAYS = 14      # on commande de quoi tenir ~14 jours au-delà du délai
# L'acheteur estime la demande sur les 28 derniers jours OBSERVÉS (comme un ERP),
# au lieu d'un niveau figé au démarrage : sinon les produits en croissance
# finissent en rupture chronique (taux de service 94 % -> 79 % en 3 ans).
DEMAND_WINDOW_DAYS = 28
MIN_HISTORY_FOR_ESTIMATE = 7   # avant 7 jours d'historique : estimation catalogue

# --- Promotions ---------------------------------------------------------------
PROMO_START_PROBABILITY = 4 / 365   # ~4 promotions par produit et par an
PROMO_DISCOUNTS = (0.10, 0.15, 0.20, 0.30)

# --- Commandes ----------------------------------------------------------------
OPENING_SECONDS = 8 * 3600    # 8h
CLOSING_SECONDS = 20 * 3600   # 20h
WEB_SHARE_B2C = 0.60          # 60 % des clients particuliers commandent en ligne

ID_KEYS = ("client", "order", "order_item", "po", "po_item", "movement", "promotion")


@dataclass(frozen=True)
class Promotion:
    promotion_id: int
    product_id: int
    starts_at: date
    ends_at: date
    discount_pct: float


@dataclass
class PurchaseOrder:
    po_id: int
    supplier_id: int
    warehouse_id: int
    ordered_at: date
    expected_at: date
    reliability: float
    items: list[tuple[int, int, int]]  # (po_item_id, product_id, qty)


@dataclass
class SimulationState:
    day: date
    balances: dict[tuple[int, int], int]   # (product_id, warehouse_id) -> stock
    in_transit: list[PurchaseOrder]
    promotions: list[Promotion]            # actives ou à venir
    next_ids: dict[str, int]               # prochain identifiant par table
    # demande des derniers jours (unités demandées), du plus ancien au plus récent
    recent_demand: dict[tuple[int, int], list[int]] = field(default_factory=dict)


@dataclass
class DayResult:
    """Tout ce que la journée produit, prêt à être écrit en base."""
    day: date
    clients: list[dict] = field(default_factory=list)          # nouveaux clients Faker
    orders: list[dict] = field(default_factory=list)
    order_items: list[dict] = field(default_factory=list)
    purchase_orders: list[dict] = field(default_factory=list)
    purchase_order_items: list[dict] = field(default_factory=list)
    received_po_ids: list[int] = field(default_factory=list)
    stock_movements: list[dict] = field(default_factory=list)
    promotions: list[dict] = field(default_factory=list)
    balances: dict[tuple[int, int], int] = field(default_factory=dict)


# -----------------------------------------------------------------------------
# Outils
# -----------------------------------------------------------------------------
def _take(ids: dict[str, int], key: str) -> int:
    value = ids[key]
    ids[key] = value + 1
    return value


def timestamp(day: date, seconds: int = 0) -> datetime:
    return datetime.combine(day, time(0), tzinfo=timezone.utc) + timedelta(seconds=seconds)


def estimated_daily_demand(profile: ProductProfile, share: float, history: list[int] | None) -> float:
    """Demande journalière estimée par l'acheteur.

    Moyenne des DEMAND_WINDOW_DAYS derniers jours observés ; tant que l'historique
    est trop court, estimation « catalogue » (niveau de départ du produit).
    """
    if history and len(history) >= MIN_HISTORY_FOR_ESTIMATE:
        return max(sum(history) / len(history), 0.1)
    return planned_daily_demand(profile, share)


def order_up_to_level(
    profile: ProductProfile, share: float, history: list[int] | None = None
) -> tuple[float, float]:
    """(point de commande, niveau cible) pour un produit dans un entrepôt.

    point de commande = demande pendant le délai + stock de sécurité
    niveau cible      = point de commande + REVIEW_DAYS jours de demande
    """
    daily = estimated_daily_demand(profile, share, history)
    lead = profile.lead_time_days
    sd_daily = math.sqrt(daily + daily**2 / OVERDISPERSION_K)
    reorder_point = daily * lead + SERVICE_Z * sd_daily * math.sqrt(lead)
    return reorder_point, reorder_point + daily * REVIEW_DAYS


def arrival_date(po: PurchaseOrder, seed: int) -> date:
    """Date réelle d'arrivée, déterministe pour un po_id donné.

    Un fournisseur fiable à 0.8 livre à l'heure (ou 1 jour en avance) 8 fois sur 10,
    sinon avec 1 à ~8 jours de retard.
    """
    rng = np.random.default_rng([seed, 2, po.po_id])
    if rng.random() < po.reliability:
        delay = int(rng.integers(-1, 1))          # -1 ou 0
    else:
        delay = 1 + int(rng.poisson(3))
    return max(po.ordered_at + timedelta(days=1), po.expected_at + timedelta(days=delay))


def initial_inventory(
    profiles: list[ProductProfile],
    shares: dict[int, float],
    day: date,
    next_ids: dict[str, int],
) -> tuple[dict[tuple[int, int], int], list[dict]]:
    """Stock de départ : chaque produit commence à son niveau cible."""
    balances, movements = {}, []
    for profile in profiles:
        for warehouse_id in sorted(shares):
            _, target = order_up_to_level(profile, shares[warehouse_id])
            qty = max(1, math.ceil(target))
            balances[(profile.product_id, warehouse_id)] = qty
            movements.append(
                {
                    "movement_id": _take(next_ids, "movement"),
                    "event_time": timestamp(day),
                    "product_id": profile.product_id,
                    "warehouse_id": warehouse_id,
                    "movement_type": "initial",
                    "quantity_delta": qty,
                    "source_id": None,
                }
            )
    return balances, movements


# -----------------------------------------------------------------------------
# La journée
# -----------------------------------------------------------------------------
def simulate_day(
    state: SimulationState,
    profiles: list[ProductProfile],
    shares: dict[int, float],
    clients: list[ClientProfile],
    seed: int,
    history_start: date,
) -> tuple[DayResult, SimulationState]:
    day = state.day
    # Graine propre au jour : rejouer le jour D donne toujours le même résultat,
    # qu'on ait simulé d'une traite ou avec des redémarrages.
    rng = np.random.default_rng([seed, 0, day.toordinal()])
    balances = dict(state.balances)
    ids = dict(state.next_ids)
    result = DayResult(day=day)

    # 0) Nouveaux clients (rares) -----------------------------------------------
    clients = list(clients)
    n_new = int(rng.poisson(NEW_CLIENTS_PER_DAY))
    if n_new:
        fake = Faker("fr_FR")
        fake.seed_instance(seed * 1_000_003 + day.toordinal())   # Faker seedé par jour
        for _ in range(n_new):
            client_id = _take(ids, "client")
            segment = "B2B" if rng.random() < NEW_CLIENT_B2B_SHARE else "B2C"
            result.clients.append(
                {
                    "client_id": client_id,
                    "name": fake.company() if segment == "B2B" else fake.name(),
                    "segment": segment,
                    "city": fake.city(),
                    # inscrit dans la nuit / tôt le matin, avant l'ouverture
                    "created_at": timestamp(day, int(rng.integers(0, OPENING_SECONDS))),
                }
            )
            clients.append(build_client_profile(client_id, segment, shares, seed,
                                                created=day, history_start=history_start))

    # 1) Réceptions ------------------------------------------------------------
    still_in_transit = []
    for po in state.in_transit:
        if arrival_date(po, seed) <= day:
            result.received_po_ids.append(po.po_id)
            for po_item_id, product_id, qty in po.items:
                key = (product_id, po.warehouse_id)
                balances[key] = balances.get(key, 0) + qty
                result.stock_movements.append(
                    {
                        "movement_id": _take(ids, "movement"),
                        "event_time": timestamp(day, 6 * 3600),   # livré à 6h
                        "product_id": product_id,
                        "warehouse_id": po.warehouse_id,
                        "movement_type": "receipt",
                        "quantity_delta": qty,
                        "source_id": po_item_id,
                    }
                )
        else:
            still_in_transit.append(po)

    # 2) Promotions actives aujourd'hui ---------------------------------------
    discount = {
        p.product_id: p.discount_pct
        for p in state.promotions
        if p.starts_at <= day <= p.ends_at
    }

    # 3) Demande latente -> unités attribuées aux clients sélectionnés ---------
    selector = ClientSelector(clients, day) if clients else None
    # La demande suit la base de clients actifs (départs, arrivées, montée en puissance).
    base_factor = {
        (w, cat): client_base_factor(clients, w, cat, day, history_start)
        for w in sorted(shares) for cat in {p.category for p in profiles}
    }
    # client_id -> {(product_id, warehouse_id): quantité}
    baskets: dict[int, dict[tuple[int, int], int]] = {}
    demand_today: dict[tuple[int, int], int] = {}
    by_id = {c.client_id: c for c in clients}
    for profile in profiles:
        for warehouse_id in sorted(shares):
            if selector is None or not selector.has(warehouse_id, profile.category):
                continue          # aucun client actif pour cet entrepôt et cette catégorie
            lam = expected_demand(
                profile, shares[warehouse_id], day,
                discount.get(profile.product_id, 0.0), history_start,
            ) * base_factor[(warehouse_id, profile.category)]
            remaining = sample_demand(rng, lam)
            demand_today[(profile.product_id, warehouse_id)] = remaining
            while remaining > 0:
                client = selector.pick(rng, warehouse_id, profile.category)
                size = min(remaining, int(rng.geometric(client.line_size_p)))
                basket = baskets.setdefault(client.client_id, {})
                key = (profile.product_id, warehouse_id)
                basket[key] = basket.get(key, 0) + size
                remaining -= size

    # 4) Une commande par client sélectionné + allocation du stock -------------
    if baskets:
        ordering = sorted(baskets)                       # ordre stable
        seconds = rng.integers(OPENING_SECONDS, CLOSING_SECONDS, size=len(ordering))
        price = {p.product_id: p.unit_price for p in profiles}

        # Traitement chronologique : le premier arrivé est le premier servi.
        for second, client_id in sorted(zip(seconds.tolist(), ordering)):
            client = by_id[client_id]
            if client.segment == "B2B":
                channel = "b2b_portal"
            else:
                channel = "web" if rng.random() < WEB_SHARE_B2C else "store"
            order_id = _take(ids, "order")
            ordered_at = timestamp(day, int(second))
            requested_total = fulfilled_total = 0

            for (product_id, warehouse_id), qty in sorted(baskets[client_id].items()):
                key = (product_id, warehouse_id)
                available = balances.get(key, 0)
                fulfilled = min(qty, available)       # jamais plus que le stock
                balances[key] = available - fulfilled
                item_id = _take(ids, "order_item")
                rate = Decimal(1) - Decimal(str(discount.get(product_id, 0.0)))
                result.order_items.append(
                    {
                        "order_item_id": item_id,
                        "order_id": order_id,
                        "product_id": product_id,
                        "warehouse_id": warehouse_id,
                        "requested_qty": qty,
                        "fulfilled_qty": fulfilled,
                        "lost_sales_qty": qty - fulfilled,
                        "unit_price": (price[product_id] * rate).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        ),
                    }
                )
                if fulfilled > 0:
                    result.stock_movements.append(
                        {
                            "movement_id": _take(ids, "movement"),
                            "event_time": ordered_at,
                            "product_id": product_id,
                            "warehouse_id": warehouse_id,
                            "movement_type": "sale",
                            "quantity_delta": -fulfilled,
                            "source_id": item_id,
                        }
                    )
                requested_total += qty
                fulfilled_total += fulfilled

            if fulfilled_total == requested_total:
                status = "fulfilled"
            elif fulfilled_total == 0:
                status = "unfulfilled"
            else:
                status = "partial"
            result.orders.append(
                {
                    "order_id": order_id,
                    "client_id": client_id,
                    "ordered_at": ordered_at,
                    "status": status,
                    "channel": channel,
                }
            )

    # 5) Réapprovisionnement (en fin de journée) -------------------------------
    # Historique de demande connu en fin de journée (aujourd'hui inclus).
    recent_demand = {}
    for profile in profiles:
        for warehouse_id in sorted(shares):
            key = (profile.product_id, warehouse_id)
            history = state.recent_demand.get(key, []) + [demand_today.get(key, 0)]
            recent_demand[key] = history[-DEMAND_WINDOW_DAYS:]

    in_transit_qty: dict[tuple[int, int], int] = {}
    for po in still_in_transit:
        for _, product_id, qty in po.items:
            key = (product_id, po.warehouse_id)
            in_transit_qty[key] = in_transit_qty.get(key, 0) + qty

    by_supplier = {p.supplier_id: p for p in profiles}  # délai / fiabilité du fournisseur
    to_order: dict[tuple[int, int], list[tuple[int, int, Decimal]]] = {}
    for profile in profiles:
        for warehouse_id in sorted(shares):
            key = (profile.product_id, warehouse_id)
            reorder_point, target = order_up_to_level(profile, shares[warehouse_id], recent_demand[key])
            position = balances.get(key, 0) + in_transit_qty.get(key, 0)
            if position <= reorder_point:
                qty = math.ceil(target - position)
                if qty > 0:
                    to_order.setdefault((profile.supplier_id, warehouse_id), []).append(
                        (profile.product_id, qty, profile.unit_cost)
                    )

    new_pos = []
    for (supplier_id, warehouse_id), items in sorted(to_order.items()):
        supplier = by_supplier[supplier_id]
        po = PurchaseOrder(
            po_id=_take(ids, "po"),
            supplier_id=supplier_id,
            warehouse_id=warehouse_id,
            ordered_at=day,
            expected_at=day + timedelta(days=supplier.lead_time_days),
            reliability=supplier.reliability,
            items=[],
        )
        result.purchase_orders.append(
            {
                "po_id": po.po_id,
                "supplier_id": supplier_id,
                "warehouse_id": warehouse_id,
                "ordered_at": day,
                "expected_at": po.expected_at,
                "received_at": None,
                "status": "in_transit",
            }
        )
        for product_id, qty, unit_cost in items:
            po_item_id = _take(ids, "po_item")
            po.items.append((po_item_id, product_id, qty))
            result.purchase_order_items.append(
                {
                    "po_item_id": po_item_id,
                    "po_id": po.po_id,
                    "product_id": product_id,
                    "ordered_qty": qty,
                    "received_qty": None,
                    "unit_cost": unit_cost,
                }
            )
        new_pos.append(po)

    # 6) Planification de promotions (annoncées 3 à 10 jours à l'avance) -------
    already_planned = {p.product_id for p in state.promotions if p.ends_at >= day}
    new_promotions = []
    for profile in profiles:
        if profile.product_id in already_planned:
            continue
        if rng.random() < PROMO_START_PROBABILITY:
            starts_at = day + timedelta(days=int(rng.integers(3, 11)))
            ends_at = starts_at + timedelta(days=int(rng.integers(6, 14)))
            pct = float(rng.choice(PROMO_DISCOUNTS))
            promo = Promotion(_take(ids, "promotion"), profile.product_id, starts_at, ends_at, pct)
            new_promotions.append(promo)
            result.promotions.append(
                {
                    "promotion_id": promo.promotion_id,
                    "product_id": promo.product_id,
                    "starts_at": starts_at,
                    "ends_at": ends_at,
                    "discount_pct": Decimal(str(pct)),
                    "announced_at": day,
                }
            )

    # Garde-fou : la règle métier la plus importante.
    assert all(qty >= 0 for qty in balances.values()), "stock négatif"

    result.balances = balances
    next_day = day + timedelta(days=1)
    next_state = SimulationState(
        day=next_day,
        balances=balances,
        in_transit=still_in_transit + new_pos,
        promotions=[p for p in state.promotions + new_promotions if p.ends_at >= next_day],
        next_ids=ids,
        recent_demand=recent_demand,
    )
    return result, next_state
