"""Cycle nocturne du modèle : réentraînement avec garde-fou, puis prédiction.

Chaque nuit, après la simulation de la veille et la mise à jour des features :

1. RÉENTRAÎNEMENT (modèle en place vs nouveau modèle)
   - Un nouveau modèle est entraîné SANS les 8 dernières semaines dont la cible est
     connue, puis évalué sur ces 8 semaines (fenêtre glissante, trou de 7 jours entre
     les deux pour que les cibles d'entraînement ne débordent pas sur l'évaluation).
   - Il est mis en service seulement s'il :
       * fait mieux que la méthode naïve 28 j sur la même fenêtre ;
       * a un biais raisonnable (|biais| <= 5 %) ;
       * n'est pas plus mauvais que le modèle en place (tolérance : 1 point de WAPE).
     Le modèle en place a été évalué de la même façon la nuit où il a été retenu,
     sur une fenêtre décalée de quelques jours : les deux scores sont comparables.
   - S'il est retenu, il est réentraîné sur TOUTES les données étiquetées
     (y compris les 8 semaines) et enregistré dans ml.model_runs.

2. PRÉDICTION
   Le modèle en place prédit, pour chaque produit, la demande des 7 prochains jours
   à partir des features de la dernière journée terminée (hier). Les prévisions vont
   dans ml.forecasts et n'y sont JAMAIS modifiées : 7 jours plus tard, on les compare
   au réel (vue analytics.v_forecast_accuracy).

    python -m smartsupply.models.nightly          # lance le cycle à la main
"""
from __future__ import annotations

import io
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import joblib
import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from smartsupply.db.connection import get_engine
from smartsupply.features.build import ALL_COLUMNS, HORIZON_DAYS, NULLABLE_FEATURES, TARGET
from smartsupply.models.baselines import predict_baseline
from smartsupply.models.estimators import LightGBMRatioModel
from smartsupply.models.metrics import scores

MODEL_NAME = "lightgbm"
EVAL_DAYS = 56            # 8 semaines d'évaluation
BIAS_LIMIT = 0.05         # |biais| maximum accepté
WAPE_TOLERANCE = 0.01     # le nouveau peut être jusqu'à 1 point moins bon (bruit d'une fenêtre à l'autre)


# -----------------------------------------------------------------------------
# Données
# -----------------------------------------------------------------------------
def load_snapshots(engine: Engine, since: date | None = None) -> pd.DataFrame:
    """Lit ml.feature_snapshots avec les mêmes types que le dataset Parquet.

    since : ne lit que les journées à partir de cette date (tout l'historique sinon).
    """
    where = "WHERE as_of_date >= :since " if since else ""
    with engine.connect() as conn:
        df = pd.read_sql(text(f"SELECT {', '.join(ALL_COLUMNS)} FROM ml.feature_snapshots "
                              f"{where}ORDER BY as_of_date, product_id"), conn,
                         params={"since": since} if since else None)
    df["as_of_date"] = pd.to_datetime(df["as_of_date"]).dt.date
    df["price_as_of"] = df["price_as_of"].astype(float)
    df[TARGET] = df[TARGET].astype("Int64")
    for col in NULLABLE_FEATURES:
        df[col] = df[col].astype(float)
    return df


def backtest_window(labeled: pd.DataFrame, eval_days: int = EVAL_DAYS) -> tuple[date, date, date]:
    """(fin d'entraînement, début d'évaluation, fin d'évaluation)."""
    eval_end = labeled["as_of_date"].max()
    eval_start = eval_end - timedelta(days=eval_days - 1)
    train_end = eval_start - timedelta(days=HORIZON_DAYS + 1)
    return train_end, eval_start, eval_end


# -----------------------------------------------------------------------------
# Décision : mettre en service ou non
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Decision:
    promote: bool
    reason: str


def decide(challenger: dict, baseline: dict, champion_wape: float | None) -> Decision:
    """Règle de mise en service (fonction pure, testée sans base)."""
    if not challenger["wape"] < baseline["wape"]:
        return Decision(False, f"pas meilleur que la méthode naïve ({challenger['wape']:.1%} >= {baseline['wape']:.1%})")
    if abs(challenger["bias"]) > BIAS_LIMIT:
        return Decision(False, f"biais trop fort ({challenger['bias']:+.1%})")
    if champion_wape is not None and challenger["wape"] > champion_wape + WAPE_TOLERANCE:
        return Decision(False, f"moins bon que le modèle en place ({challenger['wape']:.1%} > {champion_wape:.1%})")
    if champion_wape is None:
        return Decision(True, "premier modèle mis en service")
    return Decision(True, f"retenu ({challenger['wape']:.1%} vs {champion_wape:.1%} pour le modèle en place)")


# -----------------------------------------------------------------------------
# Registre des modèles (ml.model_runs)
# -----------------------------------------------------------------------------
def dump_model(model) -> bytes:
    buffer = io.BytesIO()
    joblib.dump(model, buffer)
    return buffer.getvalue()


def load_model(blob: bytes):
    return joblib.load(io.BytesIO(blob))


@dataclass
class Champion:
    run_id: str
    model: object
    wape: float
    data_until: date


def current_champion(engine: Engine) -> Champion | None:
    """Le modèle en place = le dernier modèle mis en service, sauf s'il a été retiré
    (retour arrière : smartsupply.models.rollback)."""
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT run_id, model_blob, wape, data_until FROM ml.model_runs "
            "WHERE promoted AND withdrawn_at IS NULL ORDER BY trained_at DESC LIMIT 1"
        )).first()
    if row is None:
        return None
    return Champion(row.run_id, load_model(bytes(row.model_blob)), float(row.wape), row.data_until)


def latest_dates(engine: Engine) -> tuple[date | None, date | None]:
    """(dernière journée du dataset, dernière journée dont la cible est connue) — requête légère."""
    with engine.connect() as conn:
        row = conn.execute(text(
            f"SELECT max(as_of_date), max(as_of_date) FILTER (WHERE {TARGET} IS NOT NULL) "
            "FROM ml.feature_snapshots"
        )).one()
    return row[0], row[1]


def forecast_total(engine: Engine, as_of: date) -> int | None:
    """Total prévu pour cette journée, ou None si la prévision n'existe pas encore."""
    with engine.connect() as conn:
        total = conn.execute(text("SELECT sum(predicted_qty) FROM ml.forecasts WHERE as_of_date = :d"),
                             {"d": as_of}).scalar()
    return None if total is None else int(total)


def already_trained(engine: Engine, data_until: date) -> bool:
    with engine.connect() as conn:
        return conn.execute(text("SELECT 1 FROM ml.model_runs WHERE data_until = :d"),
                            {"d": data_until}).first() is not None


# -----------------------------------------------------------------------------
# 1) Réentraînement
# -----------------------------------------------------------------------------
def retrain(engine: Engine, df: pd.DataFrame | None = None, params: dict | None = None) -> dict:
    """Entraîne un nouveau modèle, le compare au modèle en place, l'enregistre.

    Sans effet si un entraînement existe déjà pour ces données (relançable sans risque).
    """
    if df is None:
        # Vérification légère AVANT de charger tout le dataset : les passages de
        # rattrapage de la journée ne coûtent presque rien quand tout est déjà fait.
        _, last_labeled = latest_dates(engine)
        if last_labeled is None:
            return {"status": "pas encore de données étiquetées"}
        if already_trained(engine, last_labeled):
            return {"status": "déjà fait", "data_until": str(last_labeled)}
        df = load_snapshots(engine)
    labeled = df[df[TARGET].notna()]
    train_end, eval_start, eval_end = backtest_window(labeled)
    if already_trained(engine, eval_end):
        return {"status": "déjà fait", "data_until": str(eval_end)}

    train = labeled[labeled["as_of_date"] <= train_end]
    evaluation = labeled[(labeled["as_of_date"] >= eval_start) & (labeled["as_of_date"] <= eval_end)]
    y = evaluation[TARGET].astype(float)
    challenger_model = LightGBMRatioModel(params)
    challenger = scores(y, challenger_model.fit(train).predict(evaluation))
    baseline = scores(y, predict_baseline("naive_28j", evaluation))

    champion = current_champion(engine)
    decision = decide(challenger, baseline, champion.wape if champion else None)
    # Modèle final : toutes les données étiquetées, mêmes réglages.
    blob = dump_model(LightGBMRatioModel(params).fit(labeled)) if decision.promote else None

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO ml.model_runs
                    (run_id, model_name, data_until, eval_start, eval_end, n_train_rows,
                     wape, mae, bias, baseline_wape, champion_run_id, champion_wape,
                     promoted, decision, model_blob, params)
                VALUES (:run_id, :name, :until, :es, :ee, :n, :wape, :mae, :bias, :bw,
                        :crid, :cw, :promoted, :decision, :blob, CAST(:params AS JSONB))
            """),
            {"run_id": run_id, "name": MODEL_NAME, "until": eval_end, "es": eval_start, "ee": eval_end,
             "n": len(train), "wape": challenger["wape"], "mae": challenger["mae"], "bias": challenger["bias"],
             "bw": baseline["wape"], "crid": champion.run_id if champion else None,
             "cw": champion.wape if champion else None, "promoted": decision.promote,
             "decision": decision.reason, "blob": blob,
             "params": json.dumps(challenger_model.params)},
        )
    return {"status": "mis en service" if decision.promote else "refusé", "run_id": run_id,
            "data_until": str(eval_end), "wape": challenger["wape"], "baseline_wape": baseline["wape"],
            "bias": challenger["bias"], "reason": decision.reason}


# -----------------------------------------------------------------------------
# 2) Prédiction
# -----------------------------------------------------------------------------
def forecast_latest(engine: Engine, df: pd.DataFrame | None = None) -> dict:
    """Prévision des 7 prochains jours, par produit, depuis la dernière journée terminée.

    On ne prévoit QUE la dernière journée : refaire des prévisions « dans le passé »
    avec un modèle qui a déjà vu la suite fausserait le suivi prévu / réel.
    Une prévision déjà faite n'est jamais remplacée.
    """
    champion = current_champion(engine)
    if champion is None:
        return {"status": "aucun modèle en service"}
    if df is None:
        last_day, _ = latest_dates(engine)          # vérification légère, comme pour retrain
        if last_day is None:
            return {"status": "pas encore de données"}
        total = forecast_total(engine, last_day)
        if total is not None:
            return {"status": "déjà faites", "as_of": str(last_day), "rows": 0, "total_qty": total,
                    "run_id": champion.run_id}
        df = load_snapshots(engine)
    as_of = df["as_of_date"].max()
    latest = df[df["as_of_date"] == as_of]
    predicted = np.rint(champion.model.predict(latest)).astype(int)
    rows = [
        {"as_of": as_of, "pid": int(pid), "hs": as_of + timedelta(days=1),
         "he": as_of + timedelta(days=HORIZON_DAYS), "qty": int(q), "run_id": champion.run_id}
        for pid, q in zip(latest["product_id"], predicted)
    ]
    with engine.begin() as conn:
        before = conn.execute(text("SELECT count(*) FROM ml.forecasts WHERE as_of_date = :d"), {"d": as_of}).scalar()
        conn.execute(
            text("""
                INSERT INTO ml.forecasts (as_of_date, product_id, horizon_start, horizon_end, predicted_qty, run_id)
                VALUES (:as_of, :pid, :hs, :he, :qty, :run_id)
                ON CONFLICT (as_of_date, product_id) DO NOTHING
            """),
            rows,
        )
        after = conn.execute(text("SELECT count(*) FROM ml.forecasts WHERE as_of_date = :d"), {"d": as_of}).scalar()
    inserted = after - before
    status = "prévisions enregistrées" if inserted else "déjà faites"
    return {"status": status, "as_of": str(as_of), "rows": inserted, "total_qty": int(predicted.sum()),
            "run_id": champion.run_id}


def run_nightly(engine: Engine | None = None) -> dict:
    engine = engine or get_engine()
    df = load_snapshots(engine)
    return {"retrain": retrain(engine, df), "forecast": forecast_latest(engine, df)}


def main() -> None:
    result = run_nightly()
    r, f = result["retrain"], result["forecast"]
    print(f"Réentraînement : {r['status']} (données jusqu'au {r['data_until']})")
    if "wape" in r:
        print(f"   WAPE sur 8 semaines : {r['wape']:.1%}  |  méthode naïve : {r['baseline_wape']:.1%}"
              f"  |  biais : {r['bias']:+.1%}")
        print(f"   Décision : {r['reason']}")
    print(f"Prévision : {f['status']}", end="")
    if "as_of" in f:
        print(f" — du {f['as_of']} pour les 7 jours suivants, {f['total_qty']} unités au total (modèle {f['run_id']})")
    else:
        print()


if __name__ == "__main__":
    main()
