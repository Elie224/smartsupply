"""Surveillance du modèle en service : dérive des données et erreur réelle.

1. DÉRIVE (chaque jour) : pour chaque variable du modèle, on compare
     - la RÉFÉRENCE : l'année de données qui précède la fin d'entraînement du modèle ;
     - la période RÉCENTE : les 28 derniers jours ;
   avec le PSI (Population Stability Index) :
       < 0,10          stable
       0,10 à 0,25     à surveiller
       > 0,25          dérive forte : les données ne ressemblent plus à celles
                       sur lesquelles le modèle a appris.
   Une référence d'UN AN contient toutes les saisons : l'arrivée de l'hiver n'est
   pas prise pour une dérive. Les variables de calendrier (jour, mois...) sont
   exclues : elles changent forcément d'une période à l'autre.

2. ERREUR RÉELLE : la prévision faite un jour J est comparée aux ventes réelles
   de J+1 à J+7, dès qu'elles sont connues (donc à J+7).

Résultats dans ml.drift_metrics (détail par variable) et ml.monitoring_daily (résumé),
recopiés dans MLflow sur le run du modèle concerné (smartsupply.models.tracking).

    python -m smartsupply.models.monitoring      # calcule à la main
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from smartsupply.db.connection import get_engine
from smartsupply.models.estimators import DERIVED, LGBM_NUMERIC, add_derived
from smartsupply.models.nightly import current_champion, latest_dates, load_snapshots

REFERENCE_DAYS = 365
CURRENT_DAYS = 28
BINS = 10
WATCH, STRONG = 0.10, 0.25
CALENDAR = {"weekday", "month", "day_of_year", "is_weekend"}
DRIFT_FEATURES = [f for f in LGBM_NUMERIC + DERIVED if f not in CALENDAR]


# -----------------------------------------------------------------------------
# Calcul (fonctions pures, testées sans base)
# -----------------------------------------------------------------------------
def psi(reference: pd.Series, current: pd.Series, bins: int = BINS) -> float:
    """PSI entre deux échantillons. Tranches = déciles de la référence."""
    ref = pd.to_numeric(reference, errors="coerce").dropna().to_numpy(dtype=float)
    cur = pd.to_numeric(current, errors="coerce").dropna().to_numpy(dtype=float)
    if len(ref) == 0 or len(cur) == 0:
        return 0.0
    cuts = np.unique(np.quantile(ref, np.linspace(0, 1, bins + 1)))[1:-1]
    if len(cuts) == 0:
        # Variable constante dans la référence (ex. aucune remise) : 3 tranches,
        # en dessous / égale / au-dessus de cette valeur.
        value = float(ref[0])
        cuts = np.array([value, np.nextafter(value, np.inf)])
    n = len(cuts) + 1
    r = np.bincount(np.searchsorted(cuts, ref, side="right"), minlength=n) / len(ref)
    c = np.bincount(np.searchsorted(cuts, cur, side="right"), minlength=n) / len(cur)
    r, c = np.clip(r, 1e-4, None), np.clip(c, 1e-4, None)
    return float(max(np.sum((c - r) * np.log(c / r)), 0.0))


def level(value: float) -> str:
    if value > STRONG:
        return "forte"
    if value >= WATCH:
        return "à surveiller"
    return "stable"


def windows(as_of: date, reference_end: date) -> tuple[date, date, date]:
    """(début de référence, fin de référence, début de période récente)."""
    return reference_end - timedelta(days=REFERENCE_DAYS - 1), reference_end, \
        as_of - timedelta(days=CURRENT_DAYS - 1)


def compute_drift(df: pd.DataFrame, as_of: date, reference_end: date) -> pd.DataFrame:
    """PSI de chaque variable. df = lignes de ml.feature_snapshots (au moins les fenêtres)."""
    ref_start, ref_end, cur_start = windows(as_of, reference_end)
    data = add_derived(df)
    ref = data[(data["as_of_date"] >= ref_start) & (data["as_of_date"] <= ref_end)]
    cur = data[(data["as_of_date"] >= cur_start) & (data["as_of_date"] <= as_of)]
    out = pd.DataFrame({"feature": DRIFT_FEATURES,
                        "psi": [psi(ref[f], cur[f]) for f in DRIFT_FEATURES]})
    out["level"] = out["psi"].map(level)
    return out.sort_values("psi", ascending=False, ignore_index=True)


def summarize(drift: pd.DataFrame) -> dict:
    top = drift.iloc[0]
    n_strong = int((drift["level"] == "forte").sum())
    n_watch = int((drift["level"] == "à surveiller").sum())
    if n_strong:
        status = f"dérive forte sur {n_strong} variable(s), dont {top.feature} (PSI {top.psi:.2f})"
    elif n_watch:
        status = f"à surveiller : {n_watch} variable(s), dont {top.feature} (PSI {top.psi:.2f})"
    else:
        status = f"stable (PSI max {top.psi:.2f}, {top.feature})"
    return {"max_psi": float(top.psi), "top_feature": top.feature,
            "n_watch": n_watch, "n_strong": n_strong, "status": status}


# -----------------------------------------------------------------------------
# Passage quotidien
# -----------------------------------------------------------------------------
def update_real_errors(engine: Engine) -> int:
    """Recopie l'erreur réelle des prévisions dont les ventes sont maintenant connues."""
    with engine.begin() as conn:
        result = conn.execute(text("""
            INSERT INTO ml.monitoring_daily (as_of_date, model_run_id, real_wape, real_bias)
            SELECT a.as_of_date,
                   (SELECT f.run_id FROM ml.forecasts f WHERE f.as_of_date = a.as_of_date LIMIT 1),
                   a.wape_pct / 100.0, a.bias_pct / 100.0
            FROM analytics.v_forecast_accuracy a
            WHERE a.n_products = (SELECT count(*) FROM ml.forecasts f WHERE f.as_of_date = a.as_of_date)
            ON CONFLICT (as_of_date) DO UPDATE
               SET real_wape = EXCLUDED.real_wape, real_bias = EXCLUDED.real_bias,
                   model_run_id = coalesce(ml.monitoring_daily.model_run_id, EXCLUDED.model_run_id),
                   updated_at = now()
             WHERE ml.monitoring_daily.real_wape IS NULL
        """))
    return result.rowcount


def run_monitoring(engine: Engine | None = None) -> dict:
    """Dérive du jour (si pas encore calculée) + erreurs réelles nouvellement connues."""
    engine = engine or get_engine()
    real = update_real_errors(engine)
    champion = current_champion(engine)
    last_day, _ = latest_dates(engine)
    if champion is None or last_day is None:
        return {"status": "aucun modèle en service", "real_updated": real}

    with engine.connect() as conn:
        done = conn.execute(text("SELECT max_psi, top_feature, n_watch, n_strong FROM ml.monitoring_daily "
                                 "WHERE as_of_date = :d AND max_psi IS NOT NULL"), {"d": last_day}).first()
    if done is not None:
        summary = summarize(pd.DataFrame([{"feature": done.top_feature, "psi": done.max_psi,
                                           "level": level(done.max_psi)}]))
        summary.update(n_watch=done.n_watch, n_strong=done.n_strong)
        return {**summary, "status": f"déjà fait — {summary['status']}", "as_of": str(last_day),
                "real_updated": real}

    ref_start, ref_end, cur_start = windows(last_day, champion.data_until)
    df = load_snapshots(engine, since=min(ref_start, cur_start))
    drift = compute_drift(df, last_day, champion.data_until)
    summary = summarize(drift)
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO ml.drift_metrics (as_of_date, feature, psi, level, reference_start,
                                          reference_end, current_start, model_run_id)
            VALUES (:d, :feature, :psi, :level, :rs, :re, :cs, :run)
            ON CONFLICT (as_of_date, feature) DO NOTHING
        """), [{"d": last_day, "feature": r.feature, "psi": r.psi, "level": r.level, "rs": ref_start,
                "re": ref_end, "cs": cur_start, "run": champion.run_id} for r in drift.itertuples()])
        conn.execute(text("""
            INSERT INTO ml.monitoring_daily (as_of_date, model_run_id, max_psi, top_feature, n_watch, n_strong)
            VALUES (:d, :run, :max_psi, :top, :nw, :ns)
            ON CONFLICT (as_of_date) DO UPDATE
               SET model_run_id = EXCLUDED.model_run_id, max_psi = EXCLUDED.max_psi,
                   top_feature = EXCLUDED.top_feature, n_watch = EXCLUDED.n_watch,
                   n_strong = EXCLUDED.n_strong, updated_at = now()
        """), {"d": last_day, "run": champion.run_id, "max_psi": summary["max_psi"],
               "top": summary["top_feature"], "nw": summary["n_watch"], "ns": summary["n_strong"]})
    return {**summary, "as_of": str(last_day), "real_updated": real}


def main() -> None:
    result = run_monitoring()
    print(f"Dérive du {result.get('as_of', '?')} : {result['status']}")
    print(f"Erreurs réelles nouvellement connues : {result['real_updated']}")


if __name__ == "__main__":
    main()
