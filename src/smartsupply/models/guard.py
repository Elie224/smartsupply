"""Garde-fou du modèle en service : retour arrière AUTOMATIQUE, puis rétablissement du service.

Chaque nuit, juste avant la prévision, le cycle contrôle le modèle en service. S'il a un
problème, le garde-fou :
    1. le RETIRE (même mécanisme que smartsupply.models.rollback, raison « automatique : ... ») ;
    2. remet en service le modèle retenu précédent, s'il passe lui-même les contrôles
       (sinon le suivant, jusqu'à MAX_FALLBACK modèles) ;
    3. RÉTABLIT le service : la prévision du jour faite par le modèle retiré (s'il y en a
       une) est refaite avec le modèle remis en service ; MLflow (alias champion), le site
       public et l'API suivent dans la suite du cycle ;
    4. prévient : alerte e-mail « RetourArriereAutomatique ».
Si aucun modèle de secours ne passe les contrôles, le cycle s'arrête en ÉCHEC avant de
prévoir (mieux vaut pas de prévision qu'une prévision fausse) : alerte « CycleEnEchec ».

Un modèle « a un problème » si, sur les données d'aujourd'hui ou récentes :
    - il ne se charge pas ou ne sait pas prédire ;
    - il prédit des valeurs manquantes, infinies ou négatives ;
    - sa demande totale prévue s'écarte de plus d'un facteur 2 de la méthode naïve
      (7 x moyenne 28 jours), ou plus de 20 % des produits s'en écartent d'un facteur 3 ;
    - sur les 28 derniers jours connus, il fait moins bien que la méthode naïve, ou plus de
      10 points moins bien que son erreur à l'entraînement ;
    - ses VRAIES prévisions (au moins 7 jours dont les ventes sont connues) ont une erreur
      supérieure à 18 % ET supérieure à celle de la méthode naïve sur les mêmes jours.
Seuils larges : le modèle actuel est à 0,93 de la méthode naïve et à 9 % d'erreur récente.

Un modèle retiré automatiquement ne revient jamais tout seul : le cycle de la nuit suivante
entraîne un nouveau modèle, comparé au modèle remis en service (règle habituelle).

    python -m smartsupply.models.guard          # contrôle à la main (et retour arrière si besoin)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from smartsupply.db.connection import get_engine
from smartsupply.features.build import TARGET
from smartsupply.models.baselines import predict_baseline
from smartsupply.models.metrics import scores

TOTAL_RATIO = (0.5, 2.0)        # demande totale prévue / méthode naïve
PRODUCT_RATIO = 3.0             # écart par produit jugé extrême...
PRODUCT_SHARE = 0.20            # ...au-delà de cette part des produits
RECENT_DAYS = 28                # jours récents connus utilisés pour le contrôle
RECENT_MARGIN = 0.10            # +10 points par rapport à l'erreur d'entraînement
REAL_MIN_DAYS = 7               # jours de vraies prévisions nécessaires
REAL_LIMIT = 0.18               # erreur réelle maximale (même seuil que l'alerte ErreurReelleElevee)
MAX_FALLBACK = 3                # modèles de secours essayés au plus
AUTO_PREFIX = "automatique"


class GuardError(RuntimeError):
    """Modèle en service défectueux et aucun modèle de secours valable."""


@dataclass
class Check:
    ok: bool
    reason: str = "contrôles passés"
    details: dict = field(default_factory=dict)


def assess(model, latest: pd.DataFrame, recent: pd.DataFrame, recorded_wape: float,
           real: tuple[int, float, float] | None = None) -> Check:
    """Contrôle d'un modèle (fonction sans base, testée seule).

    latest : variables du dernier jour (une ligne par produit) ;
    recent : jours récents dont la demande réelle est connue ;
    real   : (jours, erreur réelle, erreur réelle de la méthode naïve) de ses vraies prévisions."""
    try:
        pred = np.asarray(model.predict(latest), dtype=float)
    except Exception as exc:  # noqa: BLE001
        return Check(False, f"ne sait pas prédire ({type(exc).__name__} : {str(exc)[:80]})")
    if len(pred) != len(latest) or not np.all(np.isfinite(pred)):
        return Check(False, "prédictions manquantes ou infinies")
    if (pred < 0).any():
        return Check(False, f"prédictions négatives ({int((pred < 0).sum())} produit(s))")
    naive = 7 * latest["demand_rolling_mean_28"].astype(float).to_numpy()
    ratio = pred.sum() / max(naive.sum(), 1.0)
    per_product = np.clip(pred, 0.5, None) / np.clip(naive, 0.5, None)
    extreme = float(((per_product > PRODUCT_RATIO) | (per_product < 1 / PRODUCT_RATIO)).mean())
    details = {"ratio_total": round(float(ratio), 3), "part_extreme": round(extreme, 3)}
    if not TOTAL_RATIO[0] <= ratio <= TOTAL_RATIO[1]:
        return Check(False, f"demande totale prévue à {ratio:.2f} x la méthode naïve", details)
    if extreme > PRODUCT_SHARE:
        return Check(False, f"{extreme:.0%} des produits prévus très loin de la méthode naïve", details)
    if not recent.empty:
        y = recent[TARGET].astype(float)
        model_wape = scores(y, model.predict(recent))["wape"]
        naive_wape = scores(y, predict_baseline("naive_28j", recent))["wape"]
        details.update(wape_recente=round(model_wape, 4), naive_recente=round(naive_wape, 4))
        if model_wape > naive_wape:
            return Check(False, f"moins bon que la méthode naïve sur les {RECENT_DAYS} derniers jours "
                                f"({model_wape:.1%} > {naive_wape:.1%})", details)
        if model_wape > recorded_wape + RECENT_MARGIN:
            return Check(False, f"erreur récente {model_wape:.1%} bien au-dessus de son erreur "
                                f"d'entraînement ({recorded_wape:.1%})", details)
    if real is not None:
        days, real_wape, real_naive = real
        details.update(jours_reels=days, wape_reelle=round(real_wape, 4), naive_reelle=round(real_naive, 4))
        if days >= REAL_MIN_DAYS and real_wape > REAL_LIMIT and real_wape > real_naive:
            return Check(False, f"vraies prévisions : erreur {real_wape:.1%} sur {days} jours, "
                                f"pire que la méthode naïve ({real_naive:.1%})", details)
    return Check(True, details=details)


# -----------------------------------------------------------------------------
# Accès à la base
# -----------------------------------------------------------------------------
def active_models(engine: Engine) -> list[dict]:
    """Modèles retenus et non retirés, du plus récent au plus ancien (le premier est en service)."""
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(
            "SELECT run_id, wape, model_blob FROM ml.model_runs WHERE promoted AND withdrawn_at IS NULL "
            "ORDER BY trained_at DESC LIMIT :n"), {"n": MAX_FALLBACK + 1}).mappings()]


def real_errors(engine: Engine, run_id: str) -> tuple[int, float, float] | None:
    """(jours, erreur réelle du modèle, erreur réelle de la méthode naïve) sur ses vraies prévisions."""
    with engine.connect() as conn:
        row = conn.execute(text(f"""
            SELECT count(DISTINCT f.as_of_date),
                   sum(abs(f.predicted_qty - s.{TARGET}))::float / nullif(sum(s.{TARGET}), 0),
                   sum(abs(7 * s.demand_rolling_mean_28 - s.{TARGET}))::float / nullif(sum(s.{TARGET}), 0)
            FROM ml.forecasts f
            JOIN ml.feature_snapshots s ON s.as_of_date = f.as_of_date AND s.product_id = f.product_id
            WHERE f.run_id = :r AND s.{TARGET} IS NOT NULL"""), {"r": run_id}).one()
    return None if not row[0] or row[1] is None else (int(row[0]), float(row[1]), float(row[2]))


def _data(engine: Engine) -> tuple[pd.DataFrame, pd.DataFrame]:
    from smartsupply.models.nightly import latest_dates, load_snapshots
    last_day, last_labeled = latest_dates(engine)
    if last_day is None:
        return pd.DataFrame(), pd.DataFrame()
    since = min(last_day, last_labeled or last_day) - timedelta(days=RECENT_DAYS - 1)
    df = load_snapshots(engine, since=since)
    latest = df[df["as_of_date"] == last_day].reset_index(drop=True)
    recent = df[df[TARGET].notna()].reset_index(drop=True)
    return latest, recent


def _check_model(engine: Engine, row: dict, latest: pd.DataFrame, recent: pd.DataFrame) -> Check:
    from smartsupply.models.nightly import load_model
    try:
        model = load_model(bytes(row["model_blob"]))
    except Exception as exc:  # noqa: BLE001
        return Check(False, f"illisible ({type(exc).__name__})")
    return assess(model, latest, recent, float(row["wape"]), real_errors(engine, row["run_id"]))


def restore_forecast(engine: Engine, withdrawn: list[str]) -> str:
    """Refait la prévision du dernier jour si elle avait été faite par un modèle retiré."""
    from smartsupply.models.nightly import forecast_latest, latest_dates
    last_day, _ = latest_dates(engine)
    with engine.begin() as conn:
        removed = conn.execute(text("DELETE FROM ml.forecasts WHERE as_of_date = :d AND run_id = ANY(:ids)"),
                               {"d": last_day, "ids": withdrawn}).rowcount
    if not removed:
        return "aucune prévision à refaire"
    result = forecast_latest(engine)
    return f"prévision du {last_day} refaite ({result.get('rows', 0)} produits, modèle {result.get('run_id')})"


def guard(engine: Engine | None = None) -> dict:
    """Contrôle le modèle en service ; retour arrière automatique si besoin. Voir le docstring du module."""
    from smartsupply.models.rollback import withdraw

    engine = engine or get_engine()
    models = active_models(engine)
    if not models:
        return {"status": "aucun modèle en service"}
    latest, recent = _data(engine)
    if latest.empty:
        return {"status": "pas encore de données"}
    current = models[0]
    check = _check_model(engine, current, latest, recent)
    if check.ok:
        return {"status": "ok", "run_id": current["run_id"], **check.details}

    withdrawn, reasons = [current["run_id"]], [f"{current['run_id']} : {check.reason}"]
    for candidate in models[1:]:
        cand_check = _check_model(engine, candidate, latest, recent)
        if cand_check.ok:
            withdraw(engine, withdrawn, f"{AUTO_PREFIX} : {check.reason}"[:500])
            restored = restore_forecast(engine, withdrawn)
            return {"status": f"retour arrière automatique : {', '.join(withdrawn)} -> {candidate['run_id']}",
                    "reason": check.reason, "run_id": candidate["run_id"], "withdrawn": withdrawn,
                    "forecast": restored}
        withdrawn.append(candidate["run_id"])
        reasons.append(f"{candidate['run_id']} : {cand_check.reason}")
    raise GuardError("modèle en service défectueux et aucun modèle de secours valable — " + " ; ".join(reasons))


def main() -> None:
    try:
        result = guard()
    except GuardError as exc:
        raise SystemExit(f"PROBLÈME : {exc}") from None
    for key, value in result.items():
        print(f"{key} : {value}")


if __name__ == "__main__":
    main()
