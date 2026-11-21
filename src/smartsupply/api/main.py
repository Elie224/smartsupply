"""API de prévision SmartSupply.

    uvicorn smartsupply.api.main:app --port 18800      # en local
    docker compose up -d api                           # dans Docker

Documentation interactive : http://127.0.0.1:18800/docs
Le modèle servi est le CHAMPION du registre MLflow (voir model_provider.py).
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Literal

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from prometheus_client import Counter, Gauge, Histogram, make_asgi_app
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine

from smartsupply.api import data
from smartsupply.api.model_provider import ModelProvider, ModelUnavailable
from smartsupply.db.connection import get_engine

# -----------------------------------------------------------------------------
# Chiffres pour Prometheus (GET /metrics)
# -----------------------------------------------------------------------------
REQUESTS = Counter("smartsupply_api_requests_total", "Requêtes reçues", ["route", "status"])
LATENCY = Histogram("smartsupply_api_request_duration_seconds", "Temps de réponse", ["route"],
                    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
PREDICTIONS = Counter("smartsupply_api_predictions_total", "Prévisions calculées par l'API", ["endpoint"])
MODEL_INFO = Gauge("smartsupply_api_model_info", "Modèle servi (valeur toujours 1)",
                   ["run_id", "source", "mlflow_version"])


# -----------------------------------------------------------------------------
# Dépendances (remplacées par des faux dans les tests)
# -----------------------------------------------------------------------------
_state: dict = {}


def engine_dep() -> Engine:
    if "engine" not in _state:
        _state["engine"] = get_engine()
    return _state["engine"]


def provider_dep(engine: Engine = Depends(engine_dep)) -> ModelProvider:
    if "provider" not in _state:
        _state["provider"] = ModelProvider(engine)
    return _state["provider"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Charge le champion dès le démarrage (sans bloquer si rien n'est encore disponible).
    provider_dep(engine_dep()).refresh()
    yield


TAGS = [
    {"name": "état", "description": "L'API, le modèle et la base répondent-ils ?"},
    {"name": "prévisions", "description": "Demande prévue sur les 7 prochains jours, par produit."},
    {"name": "simulation", "description": "« Et si ? » : effet d'une promotion ou d'un prix sur la demande."},
]
app = FastAPI(
    title="SmartSupply — API de prévision",
    description=("Prévision de la demande à 7 jours (LightGBM) et risque de rupture de stock. "
                 "Le modèle servi est le **champion** du registre MLflow ; les prévisions "
                 "« officielles » sont celles faites chaque nuit par le cycle automatique. "
                 "Données synthétiques (projet d'apprentissage)."),
    version="1.0.0", openapi_tags=TAGS, lifespan=lifespan,
)
app.mount("/metrics", make_asgi_app())


@app.middleware("http")
async def measure(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    path = getattr(route, "path", "autre")
    if path != "/metrics":
        LATENCY.labels(path).observe(time.perf_counter() - start)
        REQUESTS.labels(path, str(response.status_code)).inc()
    return response


# -----------------------------------------------------------------------------
# Schémas de réponse
# -----------------------------------------------------------------------------
Alerte = Literal["ok", "à surveiller", "rupture probable"]


class ModeleInfo(BaseModel):
    run_id: str = Field(description="Identifiant de l'entraînement (ml.model_runs, tag MLflow smartsupply_run_id)")
    source: Literal["mlflow", "base"] = Field(description="D'où vient le modèle servi")
    version_mlflow: str | None = Field(description="Version dans le registre MLflow (alias champion)")
    entraine_le: datetime | None = None
    donnees_jusqu_au: date | None = Field(None, description="Dernière journée dont la cible était connue")
    wape: float | None = Field(None, description="Erreur sur 8 semaines à l'entraînement (0,124 = 12,4 %)")
    wape_methode_naive: float | None = None
    biais: float | None = None


class Sante(BaseModel):
    statut: Literal["ok", "dégradé"]
    base_de_donnees: bool
    donnees_jusqu_au: date | None
    modele: ModeleInfo | None
    erreur: str | None = None


class Produit(BaseModel):
    product_id: int
    sku: str
    nom: str
    categorie: str
    prix_unitaire: float


class Prevision(BaseModel):
    product_id: int
    sku: str
    nom: str
    categorie: str
    du: date = Field(description="Premier jour prévu")
    au: date = Field(description="Dernier jour prévu")
    demande_prevue: int = Field(description="Unités demandées prévues sur les 7 jours")
    ca_prevu_eur: float
    stock: int
    receptions_attendues: int
    stock_projete_fin: int = Field(description="stock + réceptions − demande prévue")
    alerte: Alerte
    modele: str = Field(description="Entraînement qui a fait la prévision")


class PrevisionProduit(Prevision):
    demande_modele_actuel: int = Field(
        description="Recalculée maintenant avec le champion servi (peut différer si le modèle a changé)")


class Simulation(BaseModel):
    product_id: int = Field(examples=[1])
    promotion_active: bool | None = Field(None, description="Promotion en cours aujourd'hui ?")
    remise_pct: float | None = Field(None, ge=0, le=90, examples=[20],
                                     description="Remise du jour, en %. Remises observées dans les données : "
                                                 "0 à 30 % ; au-delà, le modèle extrapole.")
    jours_promo_7j: int | None = Field(None, ge=0, le=7, description="Jours de promotion sur les 7 prochains")
    remise_max_7j_pct: float | None = Field(None, ge=0, le=90, description="Plus forte remise des 7 prochains jours, en %")
    prix: float | None = Field(None, gt=0, description="Prix unitaire, en €")


class ResultatSimulation(BaseModel):
    product_id: int
    nom: str
    demande_de_base: int = Field(description="Demande prévue sans changement")
    demande_simulee: int
    ecart: int
    ecart_pct: float | None
    hypotheses: dict
    modele: ModeleInfo


# -----------------------------------------------------------------------------
# Outils
# -----------------------------------------------------------------------------
def model_info(engine: Engine, provider: ModelProvider) -> ModeleInfo:
    m = provider.current()
    run = data.model_run(engine, m.run_id) or {}
    MODEL_INFO.clear()
    MODEL_INFO.labels(m.run_id, m.source, m.mlflow_version or "-").set(1)
    return ModeleInfo(run_id=m.run_id, source=m.source, version_mlflow=m.mlflow_version,
                      entraine_le=run.get("trained_at"), donnees_jusqu_au=run.get("data_until"),
                      wape=run.get("wape"), wape_methode_naive=run.get("baseline_wape"),
                      biais=run.get("bias"))


def model_or_503(provider: ModelProvider):
    try:
        return provider.current()
    except ModelUnavailable as exc:
        raise HTTPException(503, f"Aucun modèle disponible : {exc}") from exc


def to_prevision(row) -> dict:
    return {"product_id": int(row.product_id), "sku": row.sku, "nom": row.product_name,
            "categorie": row.category, "du": row.horizon_start, "au": row.horizon_end,
            "demande_prevue": int(row.predicted_qty), "ca_prevu_eur": float(row.predicted_revenue_eur),
            "stock": int(row.stock_as_of), "receptions_attendues": int(row.incoming_qty),
            "stock_projete_fin": int(row.projected_stock_end), "alerte": row.forecast_alert,
            "modele": row.model_run_id}


# -----------------------------------------------------------------------------
# Points d'entrée
# -----------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")


@app.get("/sante", response_model=Sante, tags=["état"], summary="État de l'API, du modèle et de la base")
def sante(engine: Engine = Depends(engine_dep), provider: ModelProvider = Depends(provider_dep)):
    db = data.database_ok(engine)
    last_day = data.latest_snapshots(engine)[0] if db else None
    try:
        info = model_info(engine, provider) if db else None
        erreur = None if db else "base de données injoignable"
    except ModelUnavailable as exc:
        info, erreur = None, str(exc)
    return Sante(statut="ok" if db and info else "dégradé", base_de_donnees=db,
                 donnees_jusqu_au=last_day, modele=info, erreur=erreur)


@app.get("/modele", response_model=ModeleInfo, tags=["état"], summary="Le modèle champion servi")
def modele(engine: Engine = Depends(engine_dep), provider: ModelProvider = Depends(provider_dep)):
    model_or_503(provider)
    return model_info(engine, provider)


@app.get("/produits", response_model=list[Produit], tags=["prévisions"], summary="Liste des produits")
def produits(engine: Engine = Depends(engine_dep)):
    return [Produit(product_id=r.product_id, sku=r.sku, nom=r.name, categorie=r.category,
                    prix_unitaire=float(r.unit_price)) for r in data.products(engine).itertuples()]


@app.get("/previsions", response_model=list[Prevision], tags=["prévisions"],
         summary="Prévisions du jour (toutes ou filtrées)")
def previsions(categorie: str | None = Query(None, description="ex. Boissons"),
               alerte: Alerte | None = Query(None, description="ne garder qu'un niveau d'alerte"),
               engine: Engine = Depends(engine_dep)):
    """Prévisions « officielles » faites cette nuit par le cycle automatique,
    triées du stock projeté le plus bas au plus haut."""
    return [to_prevision(r) for r in data.stored_forecasts(engine, categorie, alerte).itertuples()]


@app.get("/previsions/{product_id}", response_model=PrevisionProduit, tags=["prévisions"],
         summary="Prévision d'un produit")
def prevision_produit(product_id: int, engine: Engine = Depends(engine_dep),
                      provider: ModelProvider = Depends(provider_dep)):
    stored = data.stored_forecasts(engine)
    row = stored[stored["product_id"] == product_id]
    if row.empty:
        raise HTTPException(404, f"Produit {product_id} inconnu ou sans prévision")
    m = model_or_503(provider)
    _, snaps = data.latest_snapshots(engine)
    snap = snaps[snaps["product_id"] == product_id]
    live = int(m.predict(snap)[0]) if not snap.empty else int(row.iloc[0].predicted_qty)
    PREDICTIONS.labels("prevision_produit").inc()
    return PrevisionProduit(**to_prevision(next(row.itertuples())), demande_modele_actuel=live)


@app.post("/simuler", response_model=ResultatSimulation, tags=["simulation"],
          summary="Et si ? Effet d'une promotion ou d'un prix")
def simuler(sim: Simulation, engine: Engine = Depends(engine_dep),
            provider: ModelProvider = Depends(provider_dep)):
    """Part des données réelles du produit (dernière journée), applique les hypothèses
    données, et compare la demande prévue avant / après. Rien n'est enregistré."""
    m = model_or_503(provider)
    _, snaps = data.latest_snapshots(engine)
    base = snaps[snaps["product_id"] == sim.product_id]
    if base.empty:
        raise HTTPException(404, f"Produit {sim.product_id} inconnu")
    changed = base.copy()
    hypotheses = {}
    mapping = {"promotion_active": "promotion_active", "remise_pct": "discount_pct",
               "jours_promo_7j": "promo_days_next_7", "remise_max_7j_pct": "promo_max_discount_next_7",
               "prix": "price_as_of"}
    for field, column in mapping.items():
        value = getattr(sim, field)
        if value is None:
            continue
        if field in ("remise_pct", "remise_max_7j_pct"):
            value = value / 100.0                         # les features stockent 0,20 pour 20 %
        changed[column] = value
        hypotheses[field] = getattr(sim, field)
    if not hypotheses:
        raise HTTPException(422, "Donner au moins une hypothèse (promotion, remise, prix...)")
    if sim.remise_pct is not None and sim.promotion_active is None:
        changed["promotion_active"] = sim.remise_pct > 0      # comme dans les données : remise > 0 = promo
    before, after = (int(v) for v in m.predict(pd.concat([base, changed], ignore_index=True)))
    PREDICTIONS.labels("simuler").inc()
    names = data.products(engine).set_index("product_id")["name"]
    return ResultatSimulation(product_id=sim.product_id, nom=names.get(sim.product_id, "?"),
                              demande_de_base=before, demande_simulee=after, ecart=after - before,
                              ecart_pct=round(100 * (after - before) / before, 1) if before else None,
                              hypotheses=hypotheses, modele=model_info(engine, provider))
