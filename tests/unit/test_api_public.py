"""API en mode public : lit les fichiers publiés (ici un dossier local), sans base ni MLflow."""
import json

import joblib
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sklearn.dummy import DummyRegressor

from smartsupply.api import main


@pytest.fixture
def published(tmp_path):
    (tmp_path / "etat.json").write_text(json.dumps({
        "modele": {"run_id": "RUNP", "entraine_le": "2026-11-21T02:40:00+00:00",
                   "donnees_jusqu_au": "2026-11-13", "wape": 0.12, "wape_methode_naive": 0.19, "biais": 0.01}}))
    pd.DataFrame({
        "as_of_date": ["2026-11-20"] * 2, "horizon_start": ["2026-11-21"] * 2, "horizon_end": ["2026-11-27"] * 2,
        "product_id": [1, 2], "sku": ["SKU-1", "SKU-2"], "product_name": ["Café", "Thé"],
        "category": ["Boissons", "Épicerie"], "predicted_qty": [30, 10], "predicted_revenue_eur": [90.0, 25.0],
        "stock_as_of": [10, 50], "incoming_qty": [5, 0], "projected_stock_end": [-15, 40],
        "forecast_alert": ["rupture probable", "ok"], "model_run_id": ["RUNP"] * 2,
    }).to_csv(tmp_path / "previsions.csv", index=False)
    pd.DataFrame({"product_id": [1, 2], "sku": ["SKU-1", "SKU-2"], "name": ["Café", "Thé"],
                  "category": ["Boissons", "Épicerie"], "unit_price": [3.0, 2.5]}
                 ).to_csv(tmp_path / "produits.csv", index=False)
    pd.DataFrame({"as_of_date": pd.to_datetime(["2026-11-20"] * 2), "product_id": [1, 2],
                  "discount_pct": [0.0, 0.0], "promotion_active": [False, False],
                  "promo_days_next_7": [0, 0], "promo_max_discount_next_7": [0.0, 0.0],
                  "price_as_of": [3.0, 2.5]}).to_parquet(tmp_path / "features_derniere_journee.parquet")
    model = DummyRegressor(strategy="constant", constant=12.0).fit(np.zeros((1, 1)), [12.0])
    joblib.dump(model, tmp_path / "modele.joblib")
    return tmp_path


@pytest.fixture
def client(published, monkeypatch):
    monkeypatch.setenv("SMARTSUPPLY_PUBLIC_DATA_URL", str(published))
    main._state.clear()
    with TestClient(main.app) as c:
        yield c
    main._state.clear()


def test_sante_et_modele_publie(client):
    s = client.get("/sante").json()
    assert s["statut"] == "ok" and s["donnees_jusqu_au"] == "2026-11-20"
    assert s["modele"]["source"] == "publication" and s["modele"]["run_id"] == "RUNP"
    assert s["modele"]["wape"] == 0.12


def test_previsions_filtrees_et_triees(client):
    rows = client.get("/previsions").json()
    assert [r["product_id"] for r in rows] == [1, 2]                  # stock projeté le plus bas d'abord
    assert [r["product_id"] for r in client.get("/previsions", params={"categorie": "boissons"}).json()] == [1]
    assert client.get("/previsions", params={"alerte": "ok"}).json()[0]["nom"] == "Thé"
    one = client.get("/previsions/1").json()
    assert one["demande_prevue"] == 30 and one["demande_modele_actuel"] == 12
    assert client.get("/previsions/99").status_code == 404


def test_simulation_avec_le_modele_publie(client):
    r = client.post("/simuler", json={"product_id": 2, "remise_pct": 20}).json()
    assert r["demande_de_base"] == 12 and r["demande_simulee"] == 12 and r["nom"] == "Thé"
    assert r["modele"]["source"] == "publication"
    assert client.get("/produits").json()[1]["prix_unitaire"] == 2.5
