"""API de prévision : réponses, erreurs, simulation (sans base ni MLflow : tout est simulé)."""
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from smartsupply.api import data, main
from smartsupply.api.model_provider import ChampionModel, ModelUnavailable

DAY = date(2026, 11, 20)


class FakeProvider:
    def __init__(self, available=True):
        # Modèle factice : demande = 10 x (1 + 2 x remise) -> une remise de 20 % donne +40 %.
        fn = lambda df: 10 * (1 + 2 * df["discount_pct"].to_numpy(dtype=float))  # noqa: E731
        self.model = ChampionModel(fn, "RUN1", "mlflow", "3", 0.0) if available else None

    def current(self):
        if self.model is None:
            raise ModelUnavailable("aucun modèle en service")
        return self.model

    def refresh(self, force=False):
        return self.model


SNAPS = pd.DataFrame({"as_of_date": [DAY, DAY], "product_id": [1, 2], "discount_pct": [0.0, 0.1],
                      "promotion_active": [False, True], "promo_days_next_7": [0, 2],
                      "promo_max_discount_next_7": [0.0, 0.1], "price_as_of": [10.0, 5.0]})
STORED = pd.DataFrame([{
    "as_of_date": DAY, "horizon_start": date(2026, 11, 21), "horizon_end": date(2026, 11, 27),
    "product_id": pid, "sku": f"SKU{pid}", "product_name": f"Produit {pid}", "category": "Boissons",
    "predicted_qty": q, "predicted_revenue_eur": q * 10.0, "stock_as_of": st, "incoming_qty": 0,
    "projected_stock_end": st - q, "forecast_alert": data.stock_alert(st, 0, q), "model_run_id": "RUN1",
} for pid, q, st in [(1, 10, 5), (2, 12, 100)]])


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(data, "database_ok", lambda engine: True)
    monkeypatch.setattr(data, "latest_snapshots", lambda engine: (DAY, SNAPS))
    monkeypatch.setattr(data, "stored_forecasts", lambda engine, c=None, a=None: STORED[
        (STORED.category.str.lower() == c.lower()) if c else slice(None)].pipe(
        lambda d: d[d.forecast_alert == a] if a else d))
    monkeypatch.setattr(data, "products", lambda engine: pd.DataFrame(
        {"product_id": [1, 2], "sku": ["SKU1", "SKU2"], "name": ["Produit 1", "Produit 2"],
         "category": ["Boissons"] * 2, "unit_price": [10.0, 5.0]}))
    monkeypatch.setattr(data, "model_run", lambda engine, run_id: {
        "trained_at": datetime(2026, 11, 21, tzinfo=timezone.utc), "data_until": date(2026, 11, 13),
        "wape": 0.124, "baseline_wape": 0.191, "bias": 0.004})
    main.app.dependency_overrides[main.engine_dep] = lambda: None
    main.app.dependency_overrides[main.provider_dep] = lambda: FakeProvider()
    main._state.update(engine=None, provider=FakeProvider())     # pour le démarrage (lifespan)
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()
    main._state.clear()


@pytest.mark.parametrize("stock, incoming, predicted, expected", [
    (5, 0, 10, "rupture probable"), (10, 2, 10, "à surveiller"), (15, 0, 10, "ok"), (0, 0, 0, "ok")])
def test_regle_d_alerte_identique_a_la_vue(stock, incoming, predicted, expected):
    assert data.stock_alert(stock, incoming, predicted) == expected


def test_sante(client):
    r = client.get("/sante").json()
    assert r["statut"] == "ok" and r["donnees_jusqu_au"] == "2026-11-20"
    assert r["modele"]["source"] == "mlflow" and r["modele"]["version_mlflow"] == "3"


def test_sante_degradee_sans_modele(client):
    main.app.dependency_overrides[main.provider_dep] = lambda: FakeProvider(available=False)
    r = client.get("/sante").json()
    assert r["statut"] == "dégradé" and r["modele"] is None and "aucun modèle" in r["erreur"]
    assert client.get("/modele").status_code == 503


def test_previsions_triees_et_filtrees(client):
    rows = client.get("/previsions").json()
    assert [r["product_id"] for r in rows] == [1, 2]
    assert rows[0]["alerte"] == "rupture probable" and rows[0]["stock_projete_fin"] == -5
    assert len(client.get("/previsions", params={"alerte": "ok"}).json()) == 1
    assert client.get("/previsions", params={"categorie": "boissons"}).status_code == 200
    assert client.get("/previsions", params={"alerte": "n'importe quoi"}).status_code == 422


def test_prevision_d_un_produit(client):
    r = client.get("/previsions/2").json()
    assert r["demande_prevue"] == 12 and r["demande_modele_actuel"] == 12     # 10 x (1 + 0,2)
    assert client.get("/previsions/999").status_code == 404


def test_simulation_d_une_remise(client):
    r = client.post("/simuler", json={"product_id": 1, "remise_pct": 20}).json()
    assert (r["demande_de_base"], r["demande_simulee"], r["ecart"], r["ecart_pct"]) == (10, 14, 4, 40.0)
    assert r["hypotheses"] == {"remise_pct": 20.0}


def test_simulation_refuse_les_requetes_invalides(client):
    assert client.post("/simuler", json={"product_id": 1}).status_code == 422          # aucune hypothèse
    assert client.post("/simuler", json={"product_id": 1, "remise_pct": 150}).status_code == 422
    assert client.post("/simuler", json={"product_id": 999, "prix": 3}).status_code == 404


def test_metriques_prometheus(client):
    client.get("/sante")
    client.post("/simuler", json={"product_id": 1, "remise_pct": 10})
    text = client.get("/metrics/").text
    assert 'smartsupply_api_requests_total{route="/sante",status="200"}' in text
    assert 'smartsupply_api_predictions_total{endpoint="simuler"}' in text


def test_modele_arrondi_et_jamais_negatif():
    m = ChampionModel(lambda df: np.array([-3.2, 4.6]), "R", "base", None, 0.0)
    assert m.predict(pd.DataFrame({"x": [1, 2]})).tolist() == [0, 5]
