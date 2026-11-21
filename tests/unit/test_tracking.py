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
