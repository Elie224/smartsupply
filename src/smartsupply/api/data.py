"""Lectures pour l'API (aucune écriture).

`engine` est soit la base PostgreSQL (mode normal), soit une PublicSource (mode public :
résultats publiés, sans base — voir public_source.py) ; chaque lecture choisit la bonne source."""
from __future__ import annotations

from datetime import date

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from smartsupply.api.public_source import PublicSource
from smartsupply.models.nightly import latest_dates, load_snapshots

# Même règle que la vue analytics.v_forecasts (Power BI, Grafana).
WATCH_FACTOR = 1.5


def stock_alert(stock: float, incoming: float, predicted: float) -> str:
    available = (stock or 0) + (incoming or 0)
    if available < predicted:
        return "rupture probable"
    if available < WATCH_FACTOR * predicted:
        return "à surveiller"
    return "ok"


def latest_snapshots(engine: Engine) -> tuple[date | None, pd.DataFrame]:
    """Features de la dernière journée terminée (une ligne par produit)."""
    if isinstance(engine, PublicSource):
        return engine.latest_snapshots()
    last_day, _ = latest_dates(engine)
    if last_day is None:
        return None, pd.DataFrame()
    df = load_snapshots(engine, since=last_day)
    return last_day, df[df["as_of_date"] == last_day].reset_index(drop=True)


def products(engine: Engine) -> pd.DataFrame:
    if isinstance(engine, PublicSource):
        return engine.products()
    with engine.connect() as conn:
        return pd.read_sql(text("SELECT product_id, sku, name, category, unit_price "
                                "FROM raw.products ORDER BY product_id"), conn)


def stored_forecasts(engine: Engine, category: str | None = None, alert: str | None = None) -> pd.DataFrame:
    """Prévisions officielles du jour (faites par le cycle nocturne, jamais modifiées)."""
    if isinstance(engine, PublicSource):
        return engine.stored_forecasts(category, alert)
    sql = ("SELECT as_of_date, horizon_start, horizon_end, product_id, sku, product_name, category, "
           "predicted_qty, predicted_revenue_eur, stock_as_of, incoming_qty, projected_stock_end, "
           "forecast_alert, model_run_id FROM analytics.v_forecasts WHERE is_latest")
    params: dict = {}
    if category:
        sql += " AND lower(category) = lower(:cat)"
        params["cat"] = category
    if alert:
        sql += " AND forecast_alert = :alert"
        params["alert"] = alert
    with engine.connect() as conn:
        return pd.read_sql(text(sql + " ORDER BY projected_stock_end, product_id"), conn, params=params)


def model_run(engine: Engine, run_id: str) -> dict | None:
    if isinstance(engine, PublicSource):
        return engine.model_run(run_id)
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT run_id, trained_at, data_until, wape, baseline_wape, bias, decision "
            "FROM ml.model_runs WHERE run_id = :r"), {"r": run_id}).mappings().first()
    return dict(row) if row else None


def database_ok(engine: Engine) -> bool:
    if isinstance(engine, PublicSource):
        return engine.ok()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False
