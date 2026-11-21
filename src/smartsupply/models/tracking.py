"""Suivi des entraînements dans MLflow (carnet de bord + registre des modèles).

La base PostgreSQL (ml.model_runs) reste la RÉFÉRENCE : c'est elle qui sert le modèle
au cycle nocturne. MLflow en est le miroir consultable (http://127.0.0.1:15000) :

    - un run MLflow par entraînement : réglages, scores (WAPE, MAE, biais, méthode
      naïve, modèle en place), décision, importance des variables ;
    - chaque modèle mis en service est enregistré dans le registre
      « smartsupply-demande-7j » ; l'alias « champion » désigne celui en service ;
    - la surveillance quotidienne (dérive PSI, erreur réelle) est ajoutée au run
      du modèle en service ce jour-là : courbes en fonction du nombre de jours
      écoulés depuis la fin de son entraînement (smartsupply.models.monitoring).

La synchronisation est relançable sans risque : elle n'envoie que les entraînements
absents de MLflow. Si MLflow est arrêté, rien ne casse : le retard est rattrapé
au passage suivant.

Copie publique (facultative) : si DAGSHUB_REPO, DAGSHUB_USER et DAGSHUB_TOKEN sont dans
.env, la même chose est envoyée au serveur MLflow gratuit de DagsHub
(https://dagshub.com/<DAGSHUB_REPO>.mlflow), consultable en ligne.

    python -m smartsupply.models.tracking      # synchronise à la main (local + DagsHub)
"""
from __future__ import annotations

import json
import os
import tempfile
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

from smartsupply.db.connection import PROJECT_ROOT, get_engine

EXPERIMENT = "smartsupply-demande-7j"
REGISTERED_MODEL = "smartsupply-demande-7j"
CHAMPION_ALIAS = "champion"
RUN_ID_TAG = "smartsupply_run_id"          # identifiant de l'entraînement dans ml.model_runs

# Pas d'attente interminable si le serveur ne répond pas (MLflow réessaie 7 fois par défaut).
os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "2")
os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "60")
os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

RUN_COLUMNS = ("run_id, trained_at, model_name, data_until, eval_start, eval_end, n_train_rows, "
               "wape, mae, bias, baseline_wape, champion_run_id, champion_wape, promoted, decision, params")


def tracking_uri() -> str:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    return os.getenv("MLFLOW_TRACKING_URI", f"http://127.0.0.1:{os.getenv('MLFLOW_PORT', '15000')}")


def dagshub_target() -> tuple[str, dict[str, str]] | None:
    """(adresse MLflow DagsHub, identifiants) si DAGSHUB_* sont dans .env, sinon None."""
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    repo, user, token = (os.getenv(k, "").strip() for k in ("DAGSHUB_REPO", "DAGSHUB_USER", "DAGSHUB_TOKEN"))
    if not (repo and user and token):
        return None
    return (f"https://dagshub.com/{repo.strip('/')}.mlflow",
            {"MLFLOW_TRACKING_USERNAME": user, "MLFLOW_TRACKING_PASSWORD": token})


@contextmanager
def _credentials(creds: dict[str, str]):
    """Identifiants MLflow le temps d'une synchronisation (le serveur local n'en a pas)."""
    old = {k: os.environ.get(k) for k in creds}
    os.environ.update(creds)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def server_available(uri: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(uri.rstrip("/") + "/health", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001  (refus, délai, nom inconnu : le serveur n'est pas là)
        return False


# -----------------------------------------------------------------------------
# Ce qu'on envoie pour un entraînement (fonctions pures, testées sans serveur)
# -----------------------------------------------------------------------------
def run_metrics(row: dict) -> dict[str, float]:
    metrics = {"wape": row["wape"], "mae": row["mae"], "bias": row["bias"],
               "baseline_wape": row["baseline_wape"],
               "gain_vs_naive_pts": (row["baseline_wape"] - row["wape"]) * 100}
    if row.get("champion_wape") is not None:
        metrics["champion_wape"] = row["champion_wape"]
    return {k: float(v) for k, v in metrics.items()}


def run_params(row: dict, model_params: dict | None) -> dict[str, str]:
    params = {"model_name": row["model_name"], "data_until": row["data_until"],
              "eval_start": row["eval_start"], "eval_end": row["eval_end"],
              "n_train_rows": row["n_train_rows"]}
    params.update({f"lgbm_{k}": v for k, v in (model_params or {}).items()})
    return {k: str(v) for k, v in params.items()}


def run_tags(row: dict) -> dict[str, str]:
    return {
        RUN_ID_TAG: row["run_id"],
        "promoted": "oui" if row["promoted"] else "non",
        "decision": row["decision"],
        "champion_run_id": row["champion_run_id"] or "aucun",
        "mlflow.note.content": f"{'Mis en service' if row['promoted'] else 'Refusé'} : {row['decision']}",
    }


def _ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


# -----------------------------------------------------------------------------
# Envoi
# -----------------------------------------------------------------------------
def _log_one(client, experiment_id: str, row: dict, blob: bytes | None) -> str | None:
    """Crée le run MLflow d'un entraînement. Renvoie la version enregistrée, s'il y en a une."""
    from smartsupply.models.nightly import load_model

    model = load_model(blob) if blob is not None else None
    model_params = row["params"] or (model.params if model is not None else None)
    start = _ms(row["trained_at"])
    run = client.create_run(experiment_id, start_time=start, run_name=row["run_id"],
                            tags=run_tags(row))
    rid = run.info.run_id
    try:
        return _fill_run(client, rid, row, model, model_params, blob, start)
    except Exception:
        # Envoi interrompu : on supprime le run incomplet, il sera renvoyé en entier
        # au prochain passage (sinon il serait considéré comme déjà fait).
        client.delete_run(rid)
        raise


def _fill_run(client, rid: str, row: dict, model, model_params, blob, start: int) -> str | None:
    from mlflow.entities import Metric, Param, RunTag

    client.log_batch(rid,
                     metrics=[Metric(k, v, start, 0) for k, v in run_metrics(row).items()],
                     params=[Param(k, v) for k, v in run_params(row, model_params).items()],
                     tags=[RunTag("params_connus", "oui" if model_params else "non")])
    client.log_dict(rid, {k: str(v) for k, v in row.items()}, "decision.json")

    version = None
    if model is not None:
        client.log_text(rid, model.feature_importance().rename("importance").to_csv(),
                        "feature_importance.csv")
        version = _log_model(rid, model, blob)
    client.set_terminated(rid, end_time=start)
    return version


def _log_model(mlflow_run_id: str, model, blob: bytes) -> str:
    import lightgbm
    import mlflow
    import pandas
    import sklearn

    from smartsupply.models.mlflow_model import DemandModel

    package_dir = Path(__file__).resolve().parents[1]      # .../src/smartsupply

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "model.joblib"
        path.write_bytes(blob)
        with mlflow.start_run(run_id=mlflow_run_id):
            info = mlflow.pyfunc.log_model(
                name="model",
                python_model=DemandModel(),
                artifacts={"model": str(path)},
                code_paths=[str(package_dir)],
                pip_requirements=[f"lightgbm=={lightgbm.__version__}",
                                  f"scikit-learn=={sklearn.__version__}",
                                  f"pandas=={pandas.__version__}", "joblib"],
                registered_model_name=REGISTERED_MODEL,
            )
    return str(info.registered_model_version)


def _set_champion(client, champion_run_id: str | None) -> str | None:
    """Place l'alias « champion » sur la version du modèle en service dans la base."""
    if champion_run_id is None:
        return None
    versions = client.search_model_versions(f"name = '{REGISTERED_MODEL}'")
    by_run = {client.get_run(v.run_id).data.tags.get(RUN_ID_TAG): v.version for v in versions}
    version = by_run.get(champion_run_id)
    if version is None:
        return None
    try:
        current = client.get_model_version_by_alias(REGISTERED_MODEL, CHAMPION_ALIAS).version
    except Exception:  # noqa: BLE001  (alias pas encore posé)
        current = None
    if current != version:
        client.set_registered_model_alias(REGISTERED_MODEL, CHAMPION_ALIAS, version)
    return version


def sync(engine: Engine | None = None) -> dict:
    """Envoie au MLflow LOCAL les entraînements qui n'y sont pas encore, puis met à jour l'alias champion."""
    uri = tracking_uri()
    if not server_available(uri):
        return {"status": f"MLflow injoignable ({uri}) : rattrapé au prochain passage", "logged": 0}
    return _sync_to(engine or get_engine(), uri, use_flags=True)


def sync_dagshub(engine: Engine | None = None) -> dict:
    """Même synchronisation vers la copie publique DagsHub (si configurée)."""
    target = dagshub_target()
    if target is None:
        return {"status": "non configuré", "logged": 0}
    uri, creds = target
    # mlflow.set_tracking_uri() écrit aussi MLFLOW_TRACKING_URI : on remet l'adresse locale
    # après coup, sinon la synchronisation locale suivante partirait vers DagsHub.
    local_uri = os.environ.get("MLFLOW_TRACKING_URI")
    try:
        with _credentials(creds):
            return _sync_to(engine or get_engine(), uri, use_flags=False)
    except Exception as exc:  # noqa: BLE001  (réseau, jeton...) : rattrapé au prochain passage
        return {"status": f"échec ({type(exc).__name__} : {str(exc)[:150]})", "logged": 0}
    finally:
        import mlflow
        if local_uri is None:
            os.environ.pop("MLFLOW_TRACKING_URI", None)
        else:
            os.environ["MLFLOW_TRACKING_URI"] = local_uri
        mlflow.set_tracking_uri(tracking_uri())


def sync_all(engine: Engine | None = None) -> dict:
    """Serveur local puis DagsHub. Statut lisible des deux."""
    engine = engine or get_engine()
    local = sync(engine)
    remote = sync_dagshub(engine)
    status = local["status"] if remote["status"] == "non configuré" \
        else f"{local['status']} | DagsHub : {remote['status']}"
    return {"status": status, "local": local, "dagshub": remote}


def _sync_to(engine: Engine, uri: str, *, use_flags: bool) -> dict:
    import mlflow
    from mlflow import MlflowClient

    mlflow.set_tracking_uri(uri)
    client = MlflowClient(uri)
    experiment_id = mlflow.set_experiment(EXPERIMENT).experiment_id
    # Ouvre l'expérience en mode « Model training » (et non « GenAI ») dans l'interface.
    client.set_experiment_tag(experiment_id, "mlflow.experimentKind", "custom_model_development")

    known = {r.data.tags.get(RUN_ID_TAG): r.info.run_id
             for r in client.search_runs([experiment_id], max_results=50000)}
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(text(
            f"SELECT {RUN_COLUMNS} FROM ml.model_runs ORDER BY trained_at")).mappings()]
        champion = conn.execute(text(
            "SELECT run_id FROM ml.model_runs WHERE promoted ORDER BY trained_at DESC LIMIT 1")).scalar()

    logged, registered = 0, 0
    for row in rows:
        if row["run_id"] in known:
            continue
        blob = None
        if row["promoted"]:
            with engine.connect() as conn:
                blob = bytes(conn.execute(text("SELECT model_blob FROM ml.model_runs WHERE run_id = :r"),
                                          {"r": row["run_id"]}).scalar_one())
        if isinstance(row["params"], str):
            row["params"] = json.loads(row["params"])
        if _log_one(client, experiment_id, row, blob) is not None:
            registered += 1
        logged += 1

    version = _set_champion(client, champion)
    if logged:   # nouveaux runs : on relit la correspondance base -> MLflow
        known = {r.data.tags.get(RUN_ID_TAG): r.info.run_id
                 for r in client.search_runs([experiment_id], max_results=50000)}
    days = _sync_monitoring(client, engine, known, {r["run_id"]: r["data_until"] for r in rows},
                            use_flags=use_flags)
    status = (f"{logged} entraînement(s) envoyé(s), {registered} modèle(s) enregistré(s)"
              if logged else "à jour")
    if version is not None:
        status += f", champion = version {version}"
    if days:
        status += f", surveillance : {days} journée(s)"
    return {"status": status, "logged": logged, "registered": registered,
            "champion_version": version, "monitoring_days": days}


def monitoring_metrics(day: dict, psi_by_feature: dict[str, float]) -> dict[str, float]:
    """Métriques MLflow d'une journée de surveillance (fonction pure)."""
    metrics = {}
    if day["max_psi"] is not None and not day["drift_logged"]:
        metrics.update(drift_max_psi=day["max_psi"], drift_n_watch=day["n_watch"],
                       drift_n_strong=day["n_strong"])
        metrics.update({f"psi_{f}": v for f, v in psi_by_feature.items()})
    if day["real_wape"] is not None and not day["real_logged"]:
        metrics.update(real_wape=day["real_wape"], real_bias=day["real_bias"])
    return {k: float(v) for k, v in metrics.items()}


def _sync_monitoring(client, engine: Engine, mlflow_runs: dict, data_until: dict,
                     use_flags: bool = True) -> int:
    """Ajoute aux runs MLflow les journées de surveillance pas encore recopiées.

    Serveur local : les drapeaux drift_logged / real_logged de la base disent ce qui est déjà
    parti. Autre serveur (DagsHub) : on regarde directement les jours déjà présents dans
    MLflow, pour que les deux copies restent indépendantes."""
    from mlflow.entities import Metric

    where = ("(NOT drift_logged AND max_psi IS NOT NULL) OR (NOT real_logged AND real_wape IS NOT NULL)"
             if use_flags else "max_psi IS NOT NULL OR real_wape IS NOT NULL")
    with engine.connect() as conn:
        days = [dict(r) for r in conn.execute(text(f"""
            SELECT as_of_date, model_run_id, max_psi, n_watch, n_strong, drift_logged,
                   real_wape, real_bias, real_logged
            FROM ml.monitoring_daily WHERE {where} ORDER BY as_of_date""")).mappings()]
    sent, history = 0, {}
    for day in days:
        mlflow_run, until = mlflow_runs.get(day["model_run_id"]), data_until.get(day["model_run_id"])
        if mlflow_run is None or until is None:
            continue                                  # modèle pas encore dans MLflow : plus tard
        step = (day["as_of_date"] - until).days
        if not use_flags:
            if mlflow_run not in history:
                history[mlflow_run] = tuple(
                    {m.step for m in client.get_metric_history(mlflow_run, name)}
                    for name in ("drift_max_psi", "real_wape"))
            drift_steps, real_steps = history[mlflow_run]
            day = {**day, "drift_logged": step in drift_steps, "real_logged": step in real_steps}
        with engine.connect() as conn:
            psi_by_feature = dict(conn.execute(text(
                "SELECT feature, psi FROM ml.drift_metrics WHERE as_of_date = :d"),
                {"d": day["as_of_date"]}).all())
        metrics = monitoring_metrics(day, psi_by_feature)
        if not metrics:
            continue
        ts = _ms(datetime.combine(day["as_of_date"], datetime.min.time()).astimezone())
        client.log_batch(mlflow_run, metrics=[Metric(k, v, ts, step) for k, v in metrics.items()])
        if not use_flags:
            sent += 1
            continue
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE ml.monitoring_daily
                SET drift_logged = drift_logged OR max_psi IS NOT NULL,
                    real_logged  = real_logged OR real_wape IS NOT NULL
                WHERE as_of_date = :d"""), {"d": day["as_of_date"]})
        sent += 1
    return sent


def main() -> None:
    print(f"MLflow : {tracking_uri()}")
    print(sync()["status"])
    target = dagshub_target()
    if target:
        print(f"DagsHub : {target[0]}")
        print(sync_dagshub()["status"])


if __name__ == "__main__":
    main()
