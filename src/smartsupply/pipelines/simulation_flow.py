"""Pipeline Prefect nocturne — chaque nuit à 2 h 30, heure de Paris :

    1. simule les journées TERMINÉES qui manquent (calendrier réel : jusqu'à hier).
       En général 1 journée ; plusieurs si le PC était éteint (rattrapage) ;
    2. contrôle la qualité des données ;
    3. reconstruit le dataset ML (ml.feature_snapshots) ;
    4. réentraîne le modèle, et ne le met en service que s'il est assez bon ;
    5. prédit la demande des 7 prochains jours (ml.forecasts) -> visible le matin dans Power BI ;
    6. surveille le modèle en service : dérive des données (PSI) et erreur réelle
       des prévisions dont les ventes sont connues (ml.monitoring_daily) ;
    7. recopie les nouveaux entraînements et la surveillance dans MLflow (http://127.0.0.1:15000),
       et dans la copie publique DagsHub si DAGSHUB_* sont dans .env ;
    8. publie ses chiffres pour Prometheus (durées, résultat, modèle, dérive...) via le
       Pushgateway : http://127.0.0.1:19090 (smartsupply.metrics).
       MLflow arrêté ne fait jamais échouer le cycle : c'est rattrapé au passage suivant ;
    9. publie les résultats (sans secret) pour le site public Streamlit, sur la branche
       GitHub `donnees-publiques` (smartsupply.publish) — seulement si GITHUB_REPO et
       GITHUB_TOKEN sont dans .env, et seulement si quelque chose a changé.

Au démarrage, le pipeline fait d'abord un run immédiat : si le PC était éteint
cette nuit, le retard est rattrapé tout de suite. En plus du passage de 2 h 30,
des passages de RATTRAPAGE ont lieu toutes les 3 heures dans la journée : si le PC
dormait à 2 h 30, le travail est fait au plus tard 3 heures après son réveil.
Quand tout est déjà à jour, un passage ne fait rien (quelques requêtes légères).

Chaque passage est noté dans ops.pipeline_runs (réussite ou échec) : c'est ce que
lisent la vue analytics.v_pipeline_health (Power BI) et l'alerte Windows
(python -m smartsupply.health --notify).

    # Lancer une fois tout de suite (test) :
    python -m smartsupply.pipelines.simulation_flow --run-now

    # Démarrer la planification (c'est ce que fait le conteneur "pipeline") :
    python -m smartsupply.pipelines.simulation_flow

Le serveur Prefect (interface web) tourne dans Docker : http://127.0.0.1:14200
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from smartsupply.db.connection import PROJECT_ROOT

# PREFECT_API_URL doit être connu AVANT l'import de Prefect.
# On le lit dans .env pour ne pas toucher à la config Prefect de tes autres projets.
load_dotenv(PROJECT_ROOT / ".env")

from prefect import flow, get_run_logger, runtime, task  # noqa: E402
from sqlalchemy import text  # noqa: E402

from smartsupply.db.connection import get_engine  # noqa: E402
from smartsupply.features.build import build_and_save  # noqa: E402
from smartsupply.generator.reference_data import SEED  # noqa: E402
from smartsupply.metrics import StepTimer, build_registry, collect_state  # noqa: E402
from smartsupply.metrics import push as push_metrics  # noqa: E402
from smartsupply.generator.runner import simulate_pending_days  # noqa: E402
from smartsupply.models.nightly import forecast_latest, retrain  # noqa: E402
from smartsupply.models.monitoring import run_monitoring  # noqa: E402
from smartsupply.models.tracking import sync_all as sync_mlflow  # noqa: E402
from smartsupply.publish import publish as publish_public  # noqa: E402
from smartsupply.quality.checks import run_quality_checks  # noqa: E402

# Une journée se termine à minuit UTC : 1 h à Paris en hiver, 2 h en été.
# À 2 h 30, heure de Paris, la veille est donc terminée toute l'année.
CRON = "30 2 * * *"
# Rattrapage : 8 h 15, 11 h 15, 14 h 15, 17 h 15, 20 h 15, 23 h 15 (sans effet si tout est à jour).
CATCH_UP_CRON = "15 8-23/3 * * *"
TIMEZONE = "Europe/Paris"


def record_run(status: str, started_at: datetime, message: str, days_added: int = 0,
               model_status: str | None = None, forecast_status: str | None = None,
               flow_run_id: str | None = None) -> None:
    """Note le passage dans ops.pipeline_runs. Ne fait jamais échouer le cycle lui-même."""
    try:
        flow_run_id = flow_run_id or (str(runtime.flow_run.id) if runtime.flow_run.id else None)
        with get_engine().begin() as conn:
            conn.execute(text("""
                INSERT INTO ops.pipeline_runs
                    (flow_run_id, started_at, status, days_added, model_status, forecast_status, message)
                VALUES (:fr, :start, :status, :days, :model, :forecast, :msg)
            """), {"fr": flow_run_id, "start": started_at, "status": status, "days": days_added,
                   "model": model_status, "forecast": forecast_status, "msg": message[:2000]})
    except Exception as exc:   # noqa: BLE001  (base injoignable : l'échec reste visible dans Prefect)
        print(f"Journal ops.pipeline_runs non mis à jour : {exc}")


def on_flow_failure(flow, flow_run, state) -> None:
    """Appelé par Prefect quand le cycle plante (après les nouvelles tentatives)."""
    started = flow_run.start_time or datetime.now(timezone.utc)
    record_run("échec", started, state.message or "erreur inconnue", flow_run_id=str(flow_run.id))
    print("Prometheus :", push_metrics(build_registry(success=False, run_ts=time.time())))


@task(retries=2, retry_delay_seconds=60)
def simulate_days(seed: int) -> list[str]:
    # Rejouer est sans danger : un jour est écrit en entier ou pas du tout,
    # et un jour déjà enregistré n'est jamais refait.
    return [str(s) for s in simulate_pending_days(get_engine(), seed=seed, mode="pipeline")]


@task
def check_quality() -> dict[str, int]:
    return run_quality_checks(get_engine())


@task(retries=1, retry_delay_seconds=30)
def build_features() -> int:
    # Uniquement si les contrôles qualité sont passés (sinon le flow s'arrête avant).
    return build_and_save(get_engine())["rows"]


@task(retries=1, retry_delay_seconds=60)
def retrain_model() -> dict:
    # Sans effet si le modèle a déjà été entraîné sur ces données.
    return retrain(get_engine())


@task(retries=1, retry_delay_seconds=30)
def forecast_next_week() -> dict:
    # Sans effet si la prévision du jour existe déjà.
    return forecast_latest(get_engine())


@task
def monitor_model() -> dict:
    # Une erreur de surveillance est signalée sans bloquer les prévisions du matin.
    try:
        return run_monitoring(get_engine())
    except Exception as exc:  # noqa: BLE001
        get_run_logger().warning("Surveillance en échec : %s", exc)
        return {"status": f"erreur ({type(exc).__name__})", "n_strong": 0}


@task
def sync_tracking() -> str:
    # Miroir dans MLflow. Une erreur ici est signalée mais n'arrête pas le cycle :
    # la base reste la référence, et la synchronisation reprend au passage suivant.
    try:
        return sync_mlflow(get_engine())["status"]
    except Exception as exc:  # noqa: BLE001
        get_run_logger().warning("Synchronisation MLflow en échec : %s", exc)
        return f"erreur ({type(exc).__name__}) : rattrapé au prochain passage"


@task
def publish_site() -> str:
    # Site public : best-effort, comme MLflow (GitHub injoignable = rattrapé plus tard).
    return publish_public(get_engine())["status"]


def publish_metrics(durations: dict[str, float], days_added: int) -> str:
    """Dépose les chiffres du passage dans le Pushgateway (sans jamais faire échouer le cycle)."""
    try:
        state = collect_state(get_engine())
    except Exception as exc:  # noqa: BLE001
        return f"lecture de l'état impossible ({type(exc).__name__})"
    return push_metrics(build_registry(True, time.time(), durations, days_added, state))


@flow(name="cycle-nocturne", on_failure=[on_flow_failure])
def simulation_flow(seed: int = SEED) -> str:
    started_at = datetime.now(timezone.utc)
    logger = get_run_logger()
    timer = StepTimer()
    with timer("simulation"):
        summaries = simulate_days(seed)
    if summaries:
        for summary in summaries:
            logger.info(summary)
        with timer("qualite"):
            results = check_quality()
        logger.info("Contrôles qualité OK : %s", ", ".join(results))
        with timer("features"):
            rows = build_features()
        logger.info("Dataset ML mis à jour : %s lignes dans ml.feature_snapshots", rows)
    else:
        logger.info("Aucune nouvelle journée : la journée en cours n'est pas encore terminée.")

    # Toujours tenté, même sans nouvelle journée : rattrape un entraînement ou une
    # prévision qui aurait échoué la nuit précédente (sinon, ne fait rien).
    with timer("entrainement"):
        r = retrain_model()
    if "wape" in r:
        message = (f"Modèle {r['status']} : WAPE {r['wape']:.1%} sur 8 semaines "
                   f"(méthode naïve {r['baseline_wape']:.1%}), {r['reason']}")
        (logger.info if r["status"] == "mis en service" else logger.warning)(message)
    else:
        logger.info("Réentraînement : %s", r["status"])

    with timer("prevision"):
        f = forecast_next_week()
    logger.info("Prévision : %s%s", f["status"],
                f" (du {f['as_of']}, {f['total_qty']} unités sur 7 jours)" if "as_of" in f else "")
    with timer("surveillance"):
        m = monitor_model()
    (logger.warning if m.get("n_strong") else logger.info)("Surveillance : %s", m["status"])
    with timer("mlflow"):
        t = sync_tracking()
    logger.info("MLflow : %s", t)
    summary = (f"{len(summaries)} journée(s) ajoutée(s), modèle : {r['status']}, "
               f"prévision : {f['status']}, dérive : {m['status']}, MLflow : {t}")
    record_run("ok", started_at, summary, len(summaries), r["status"], f["status"])
    # Après record_run : le site affiche l'état « ok » de ce passage.
    with timer("publication"):
        logger.info("Site public : %s", publish_site())
    logger.info("Prometheus : %s", publish_metrics(timer.durations, len(summaries)))
    return summary


def _schedule_kwargs() -> dict:
    try:                                        # Prefect >= 3.1
        from prefect.schedules import Cron
        return {"schedules": [Cron(CRON, timezone=TIMEZONE), Cron(CATCH_UP_CRON, timezone=TIMEZONE)]}
    except ImportError:                         # versions plus anciennes (heure UTC)
        return {"cron": CRON}


def main() -> None:
    parser = argparse.ArgumentParser(description="Pipeline nocturne SmartSupply.")
    parser.add_argument("--run-now", action="store_true", help="exécute une seule fois et quitte")
    args = parser.parse_args()

    if args.run_now:
        print(simulation_flow())
        return
    try:
        print(simulation_flow())                # rattrapage immédiat au démarrage
    except Exception as exc:                    # l'échec est visible dans Prefect ;
        print(f"Rattrapage en échec : {exc}")   # on démarre quand même la planification
    # limit=1 : jamais deux passages en même temps (un rattrapage attend la fin du précédent).
    simulation_flow.serve(name="chaque-nuit", limit=1, **_schedule_kwargs())


if __name__ == "__main__":
    main()
