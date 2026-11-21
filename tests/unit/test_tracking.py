"""Suivi MLflow : contenu envoyé et comportement sans serveur (aucun serveur nécessaire)."""
from datetime import date, datetime, timezone

from smartsupply.models import tracking

ROW = {
    "run_id": "20261121T013000Z", "trained_at": datetime(2026, 11, 21, 1, 30, tzinfo=timezone.utc),
    "model_name": "lightgbm", "data_until": date(2026, 11, 13), "eval_start": date(2026, 9, 19),
    "eval_end": date(2026, 11, 13), "n_train_rows": 47900, "wape": 0.124, "mae": 3.1, "bias": 0.004,
    "baseline_wape": 0.191, "champion_run_id": None, "champion_wape": None, "promoted": True,
    "decision": "premier modèle mis en service", "params": {"learning_rate": 0.03, "num_leaves": 31},
}


def test_metriques_avec_gain_sur_la_methode_naive():
    m = tracking.run_metrics(ROW)
    assert m["wape"] == 0.124 and m["baseline_wape"] == 0.191
    assert round(m["gain_vs_naive_pts"], 1) == 6.7
    assert "champion_wape" not in m                       # pas de modèle en place au 1er entraînement
    assert "champion_wape" in tracking.run_metrics({**ROW, "champion_wape": 0.13})


def test_parametres_en_texte_avec_reglages_lightgbm():
    p = tracking.run_params(ROW, ROW["params"])
    assert p["lgbm_learning_rate"] == "0.03" and p["data_until"] == "2026-11-13"
    assert all(isinstance(v, str) for v in p.values())
    assert "lgbm_num_leaves" not in tracking.run_params(ROW, None)   # anciens entraînements


def test_etiquettes_relient_mlflow_a_la_base():
    t = tracking.run_tags(ROW)
    assert t[tracking.RUN_ID_TAG] == ROW["run_id"]
    assert t["promoted"] == "oui" and t["champion_run_id"] == "aucun"
    assert t["mlflow.note.content"].startswith("Mis en service")
    assert tracking.run_tags({**ROW, "promoted": False})["promoted"] == "non"


def test_serveur_absent_ne_bloque_pas(monkeypatch):
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:9")    # port fermé
    assert tracking.server_available("http://127.0.0.1:9", timeout=1) is False
    result = tracking.sync(engine=None)                                 # ne touche pas à la base
    assert result["logged"] == 0 and "injoignable" in result["status"]


def test_dagshub_non_configure(monkeypatch):
    for k in ("DAGSHUB_REPO", "DAGSHUB_USER", "DAGSHUB_TOKEN"):
        monkeypatch.setenv(k, "")
    assert tracking.dagshub_target() is None
    assert tracking.sync_dagshub(engine=None)["status"] == "non configuré"


def test_dagshub_adresse_et_identifiants(monkeypatch):
    monkeypatch.setenv("DAGSHUB_REPO", "moi/projet/")
    monkeypatch.setenv("DAGSHUB_USER", "moi")
    monkeypatch.setenv("DAGSHUB_TOKEN", "secret")
    uri, creds = tracking.dagshub_target()
    assert uri == "https://dagshub.com/moi/projet.mlflow"
    assert creds == {"MLFLOW_TRACKING_USERNAME": "moi", "MLFLOW_TRACKING_PASSWORD": "secret"}


def test_identifiants_retires_apres_usage(monkeypatch):
    import os
    monkeypatch.delenv("MLFLOW_TRACKING_USERNAME", raising=False)
    with tracking._credentials({"MLFLOW_TRACKING_USERNAME": "moi"}):
        assert os.environ["MLFLOW_TRACKING_USERNAME"] == "moi"
    assert "MLFLOW_TRACKING_USERNAME" not in os.environ


def test_echec_dagshub_remet_le_serveur_local(monkeypatch):
    import os
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:9")
    monkeypatch.setattr(tracking, "dagshub_target",
                        lambda: ("http://127.0.0.1:8", {"MLFLOW_TRACKING_USERNAME": "u"}))

    def boom(engine, uri, use_flags):
        os.environ["MLFLOW_TRACKING_URI"] = uri          # comme mlflow.set_tracking_uri
        raise ConnectionError("réseau")
    monkeypatch.setattr(tracking, "_sync_to", boom)
    result = tracking.sync_dagshub(engine=object())
    assert result["status"].startswith("échec (ConnectionError")
    assert os.environ["MLFLOW_TRACKING_URI"] == "http://127.0.0.1:9"
    assert "MLFLOW_TRACKING_USERNAME" not in os.environ
