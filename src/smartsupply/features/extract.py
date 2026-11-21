"""Extraction depuis PostgreSQL des données brutes nécessaires aux features.

On extrait des tables PETITES et DATÉES (un événement = une date), ce qui permet :
  * de construire les features en pandas (build.py) ;
  * de "tronquer" les données à une date D pour prouver l'absence de fuite
    (truncate_inputs) : les features calculées à D doivent être identiques
    avec toutes les données ou avec seulement les données connues à D.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine


@dataclass(frozen=True)
class FeatureInputs:
    products: pd.DataFrame      # product_id, category, supplier_id, unit_price, lead_time_days
    demand: pd.DataFrame        # product_id, day, requested_qty, lost_sales_qty
    movements: pd.DataFrame     # product_id, day, quantity_delta (net du jour)
    purchases: pd.DataFrame     # product_id, ordered_at, expected_at, received_at, qty
    promotions: pd.DataFrame    # product_id, announced_at, starts_at, ends_at, discount_pct
    first_day: date
    last_day: date              # dernier jour simulé = dernière date connue


def _read(engine: Engine, sql: str) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn)


def _to_date(df: pd.DataFrame, *cols: str) -> pd.DataFrame:
    for col in cols:
        df[col] = pd.to_datetime(df[col]).dt.date
    return df


def load_inputs(engine: Engine) -> FeatureInputs:
    bounds = _read(
        engine, "SELECT min(simulated_date) AS first, max(simulated_date) AS last FROM raw.simulation_runs"
    )
    if bounds.empty or pd.isna(bounds.loc[0, "first"]):
        raise RuntimeError("Aucune journée simulée : lance d'abord le bootstrap.")

    products = _read(
        engine,
        """
        SELECT p.product_id, p.category, p.supplier_id, p.unit_price::float AS unit_price,
               s.lead_time_days
        FROM raw.products p JOIN raw.suppliers s USING (supplier_id)
        WHERE p.active
        ORDER BY p.product_id
        """,
    )
    demand = _to_date(
        _read(
            engine,
            """
            SELECT oi.product_id,
                   (o.ordered_at AT TIME ZONE 'UTC')::date AS day,
                   sum(oi.requested_qty)  AS requested_qty,
                   sum(oi.lost_sales_qty) AS lost_sales_qty
            FROM raw.order_items oi JOIN raw.orders o USING (order_id)
            GROUP BY 1, 2
            """,
        ),
        "day",
    )
    movements = _to_date(
        _read(
            engine,
            """
            SELECT product_id, (event_time AT TIME ZONE 'UTC')::date AS day,
                   sum(quantity_delta) AS quantity_delta
            FROM raw.stock_movements
            GROUP BY 1, 2
            """,
        ),
        "day",
    )
    purchases = _to_date(
        _read(
            engine,
            """
            SELECT poi.product_id, po.ordered_at, po.expected_at, po.received_at,
                   poi.ordered_qty AS qty
            FROM raw.purchase_orders po JOIN raw.purchase_order_items poi USING (po_id)
            """,
        ),
        "ordered_at", "expected_at",
    )
    # Pas encore reçue = None (et non NaT) pour pouvoir comparer avec des dates.
    purchases["received_at"] = [
        None if pd.isna(v) else pd.Timestamp(v).date() for v in purchases["received_at"]
    ]
    purchases["received_at"] = purchases["received_at"].astype(object)
    promotions = _to_date(
        _read(
            engine,
            """
            SELECT product_id, announced_at, starts_at, ends_at, discount_pct::float AS discount_pct
            FROM raw.promotions
            """,
        ),
        "announced_at", "starts_at", "ends_at",
    )
    return FeatureInputs(
        products=products,
        demand=demand,
        movements=movements,
        purchases=purchases,
        promotions=promotions,
        first_day=pd.to_datetime(bounds.loc[0, "first"]).date(),
        last_day=pd.to_datetime(bounds.loc[0, "last"]).date(),
    )


def truncate_inputs(inputs: FeatureInputs, as_of: date) -> FeatureInputs:
    """Ce qu'on savait à la fin du jour `as_of` : rien de ce qui s'est passé après."""
    purchases = inputs.purchases[inputs.purchases["ordered_at"] <= as_of].copy()
    # Une commande reçue APRÈS as_of était encore "en transit" à as_of.
    purchases["received_at"] = [
        None if (r is None or pd.isna(r) or r > as_of) else r for r in purchases["received_at"]
    ]
    purchases["received_at"] = purchases["received_at"].astype(object)
    return replace(
        inputs,
        demand=inputs.demand[inputs.demand["day"] <= as_of],
        movements=inputs.movements[inputs.movements["day"] <= as_of],
        purchases=purchases,
        promotions=inputs.promotions[inputs.promotions["announced_at"] <= as_of],
        last_day=as_of,
    )
