"""Chiffres du cycle publiés pour Prometheus (via le Pushgateway).

Le cycle nocturne n'est pas un service qui tourne en continu : Prometheus ne peut pas
venir le « lire ». À la fin de chaque passage, le pipeline DÉPOSE donc ses chiffres
dans le Pushgateway, que Prometheus relève toutes les 30 secondes.

Chiffres publiés (préfixe smartsupply_) :
    pipeline_last_run_timestamp_seconds     heure du dernier passage
    pipeline_last_success_timestamp_seconds heure de la dernière réussite
    pipeline_last_run_success               1 = réussi, 0 = échec
    pipeline_step_duration_seconds{step}    durée de chaque étape
    pipeline_days_added                     journées ajoutées au dernier passage
    data_last_day_timestamp_seconds         dernière journée de données
    model_wape / model_baseline_wape / model_bias      modèle en service (8 semaines)
    model_trained_timestamp_seconds, model_runs{decision}
    drift_max_psi, drift_features{level}, drift_psi{feature}
    real_wape, real_bias (dernière prévision vérifiée), real_wape_28d (moyenne 4 semaines)
    forecast_units_7d, forecast_stockout_products

Si le Pushgateway est arrêté, rien ne casse : le cycle continue.
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime, timezone

from prometheus_client import CollectorRegistry, Gauge, pushadd_to_gateway
from sqlalchemy import text
from sqlalchemy.engine import Engine

from smartsupply.db.connection import PROJECT_ROOT

JOB = "smartsupply_pipeline"


def gateway_url() -> str:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    return os.getenv("PUSHGATEWAY_URL", f"http://127.0.0.1:{os.getenv('PUSHGATEWAY_PORT', '19091')}")


def _day_ts(d: date | None) -> float | None:
    return None if d is None else datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


def collect_state(engine: Engine) -> dict:
    """État du système lu dans la base (modèle, dérive, erreur réelle, prévision)."""
    with engine.connect() as conn:
        one = lambda sql: conn.execute(text(sql)).first()
        champion = one("SELECT wape, baseline_wape, bias, trained_at FROM ml.model_runs "
                       "WHERE promoted AND withdrawn_at IS NULL ORDER BY trained_at DESC LIMIT 1")
        runs = dict(conn.execute(text(
            "SELECT CASE WHEN withdrawn_at IS NOT NULL THEN 'retiré' WHEN promoted THEN 'retenu' "
            "ELSE 'refusé' END, count(*) "
            "FROM ml.model_runs GROUP BY 1")).all())
        drift = one("SELECT as_of_date, max_psi, n_watch, n_strong FROM ml.monitoring_daily "
                    "WHERE max_psi IS NOT NULL ORDER BY as_of_date DESC LIMIT 1")
        psi = dict(conn.execute(text(
            "SELECT feature, psi FROM ml.drift_metrics WHERE as_of_date = :d"),
            {"d": drift.as_of_date if drift else None}).all()) if drift else {}
        real = one("SELECT real_wape, real_bias FROM ml.monitoring_daily "
                   "WHERE real_wape IS NOT NULL ORDER BY as_of_date DESC LIMIT 1")
        real_28d = conn.execute(text(
            "SELECT avg(real_wape) FROM ml.monitoring_daily WHERE real_wape IS NOT NULL AND as_of_date > "
            "(SELECT max(as_of_date) - 28 FROM ml.monitoring_daily WHERE real_wape IS NOT NULL)")).scalar()
        forecast = one("SELECT sum(predicted_qty) FROM ml.forecasts "
                       "WHERE as_of_date = (SELECT max(as_of_date) FROM ml.forecasts)")
        stockouts = conn.execute(text(
            "SELECT count(*) FROM analytics.v_forecasts "
            "WHERE is_latest AND forecast_alert = 'rupture probable'")).scalar()
        last_day = conn.execute(text("SELECT max(simulated_date) FROM raw.simulation_runs")).scalar()
        auto_rollback = conn.execute(text(
            "SELECT max(withdrawn_at) FROM ml.model_runs WHERE withdrawn_reason LIKE 'automatique%'")).scalar()
    return {
        "data_last_day": last_day,
        "model": None if champion is None else {
            "wape": champion.wape, "baseline_wape": champion.baseline_wape, "bias": champion.bias,
            "trained_at": champion.trained_at.timestamp()},
        "model_runs": runs,
        "drift": None if drift is None else {
            "max_psi": drift.max_psi, "n_watch": drift.n_watch, "n_strong": drift.n_strong, "psi": psi},
        "real": None if real is None else {"wape": real.real_wape, "bias": real.real_bias,
                                           "wape_28d": real_28d},
        "forecast_units": None if forecast is None or forecast[0] is None else int(forecast[0]),
        "stockouts": int(stockouts or 0),
        "auto_rollback_at": None if auto_rollback is None else auto_rollback.timestamp(),
    }


def build_registry(success: bool, run_ts: float, durations: dict[str, float] | None = None,
                   days_added: int | None = None, state: dict | None = None) -> CollectorRegistry:
    """Chiffres à publier (fonction pure : testée sans Pushgateway ni base)."""
    reg = CollectorRegistry()

    def gauge(name, doc, value, labels=None):
        if value is None:
            return
        g = Gauge(f"smartsupply_{name}", doc, list(labels or {}), registry=reg)
        (g.labels(**labels) if labels else g).set(float(value))

    def gauge_by(name, doc, label, values: dict):
        if not values:
            return
        g = Gauge(f"smartsupply_{name}", doc, [label], registry=reg)
        for key, value in values.items():
            g.labels(**{label: key}).set(float(value))

    gauge("pipeline_last_run_timestamp_seconds", "Heure du dernier passage du cycle", run_ts)
    gauge("pipeline_last_run_success", "1 si le dernier passage a réussi, 0 sinon", 1 if success else 0)
    if not success:
        return reg              # en cas d'échec, on garde les autres chiffres du passage précédent
    gauge("pipeline_last_success_timestamp_seconds", "Heure de la dernière réussite", run_ts)
    gauge_by("pipeline_step_duration_seconds", "Durée de chaque étape du cycle", "step", durations or {})
    gauge("pipeline_days_added", "Journées ajoutées au dernier passage", days_added)

    state = state or {}
    gauge("data_last_day_timestamp_seconds", "Dernière journée de données (minuit UTC)",
          _day_ts(state.get("data_last_day")))
    if state.get("model"):
        m = state["model"]
        gauge("model_wape", "WAPE du modèle en service (8 semaines)", m["wape"])
        gauge("model_baseline_wape", "WAPE de la méthode naïve 28 j, même fenêtre", m["baseline_wape"])
        gauge("model_bias", "Biais du modèle en service", m["bias"])
        gauge("model_trained_timestamp_seconds", "Date d'entraînement du modèle en service", m["trained_at"])
    gauge_by("model_runs", "Entraînements par décision", "decision", state.get("model_runs") or {})
    gauge("model_last_auto_rollback_timestamp_seconds",
          "Heure du dernier retour arrière automatique (garde-fou)", state.get("auto_rollback_at"))
    if state.get("drift"):
        d = state["drift"]
        gauge("drift_max_psi", "PSI le plus élevé du jour", d["max_psi"])
        gauge_by("drift_features", "Nombre de variables par niveau de dérive", "level",
                 {"forte": d["n_strong"], "à surveiller": d["n_watch"]})
        gauge_by("drift_psi", "PSI de chaque variable", "feature", d["psi"])
    if state.get("real"):
        r = state["real"]
        gauge("real_wape", "Erreur réelle de la dernière prévision vérifiée", r["wape"])
        gauge("real_bias", "Biais réel de la dernière prévision vérifiée", r["bias"])
        gauge("real_wape_28d", "Erreur réelle moyenne des 4 dernières semaines", r["wape_28d"])
    gauge("forecast_units_7d", "Demande prévue sur 7 jours, tous produits", state.get("forecast_units"))
    gauge("forecast_stockout_products", "Produits en rupture probable", state.get("stockouts"))
    return reg


def push(registry: CollectorRegistry, timeout: float = 5.0) -> str:
    """Dépose les chiffres. Ne lève jamais d'exception : renvoie un statut lisible."""
    url = gateway_url()
    try:
        pushadd_to_gateway(url, job=JOB, registry=registry, timeout=timeout)
        return "publiés"
    except Exception as exc:  # noqa: BLE001  (Pushgateway arrêté : on continue)
        return f"Pushgateway injoignable ({url}) : {type(exc).__name__}"


class StepTimer:
    """Mesure la durée des étapes : `with timer("forecast"): ...`."""

    def __init__(self) -> None:
        self.durations: dict[str, float] = {}

    def __call__(self, step: str):
        timer = self

        class _Ctx:
            def __enter__(self):
                self.start = time.perf_counter()

            def __exit__(self, *exc):
                timer.durations[step] = time.perf_counter() - self.start
                return False
        return _Ctx()
