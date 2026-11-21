"""Registre MLflow cohérent avec la base (lecture seule).

Ignoré si le serveur MLflow ne répond pas : docker compose up -d mlflow
"""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from smartsupply.db.connection import get_engine
from smartsupply.models import tracking


@pytest.fixture(scope="module")
def client():
    uri = tracking.tracking_uri()
    if not tracking.server_available(uri):
        pytest.skip(f"MLflow injoignable ({uri})")
    mlflow = pytest.importorskip("mlflow")
    return mlflow.MlflowClient(uri)


@pytest.fixture(scope="module")
def champion_run_id():
    try:
        with get_engine().connect() as conn:
            run_id = conn.execute(text(
                "SELECT run_id FROM ml.model_runs WHERE promoted ORDER BY trained_at DESC LIMIT 1")).scalar()
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL injoignable : {exc.orig}")
    if run_id is None:
        pytest.skip("Aucun modèle en service")
    return run_id


def test_alias_champion_designe_le_modele_en_service(client, champion_run_id):
    try:
        version = client.get_model_version_by_alias(tracking.REGISTERED_MODEL, tracking.CHAMPION_ALIAS)
    except Exception:  # noqa: BLE001
        pytest.skip("Registre pas encore synchronisé : python -m smartsupply.models.tracking")
    assert client.get_run(version.run_id).data.tags[tracking.RUN_ID_TAG] == champion_run_id
