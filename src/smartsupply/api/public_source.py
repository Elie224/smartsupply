"""Mode PUBLIC de l'API : aucune base de données, aucun MLflow.

L'API lit les résultats publiés chaque nuit par le cycle (branche GitHub
`donnees-publiques`, voir smartsupply.publish) — exactement ce que lit le site Streamlit :

    previsions.csv                       prévisions officielles du jour
    produits.csv                         catalogue
    features_derniere_journee.csv        données d'entrée du modèle (pour /simuler ;
                                         .parquet en secours pour les anciennes publications)
    modele.joblib                        modèle champion
    etat.json                            date des données, modèle en service

Activé par la variable d'environnement SMARTSUPPLY_PUBLIC_DATA_URL, par exemple
https://raw.githubusercontent.com/Elie224/smartsupply/donnees-publiques
(c'est ce que fait api/index.py, le point d'entrée de la version en ligne sur Vercel). Les fichiers sont relus
au plus toutes les 10 minutes : le nouveau modèle de la nuit est pris sans redémarrage.
"""
from __future__ import annotations

import io
import json
import logging
import threading
import time
import urllib.request
from datetime import date
from pathlib import Path

import pandas as pd

from smartsupply.api.model_provider import ChampionModel, ModelUnavailable

log = logging.getLogger("smartsupply.api")
CACHE_SECONDS = 600


class PublicSource:
    """Lit les fichiers publiés (adresse web ou dossier local), avec un cache de 10 minutes."""

    def __init__(self, base: str, cache_seconds: float = CACHE_SECONDS) -> None:
        self.base = base.rstrip("/")
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, bytes]] = {}
        self._lock = threading.Lock()

    def read(self, name: str) -> bytes:
        with self._lock:
            hit = self._cache.get(name)
            if hit and time.monotonic() - hit[0] < self.cache_seconds:
                return hit[1]
        if self.base.startswith(("http://", "https://")):
            with urllib.request.urlopen(f"{self.base}/{name}", timeout=30) as resp:
                content = resp.read()
        else:
            content = (Path(self.base) / name).read_bytes()
        with self._lock:
            self._cache[name] = (time.monotonic(), content)
        return content

    # -- mêmes lectures que smartsupply.api.data -----------------------------------
    def etat(self) -> dict:
        return json.loads(self.read("etat.json"))

    def ok(self) -> bool:
        try:
            self.etat()
            return True
        except Exception:  # noqa: BLE001
            return False

    def latest_snapshots(self) -> tuple[date | None, pd.DataFrame]:
        try:   # CSV : lisible sans pyarrow (paquet trop lourd pour l'hébergement gratuit)
            df = pd.read_csv(io.BytesIO(self.read("features_derniere_journee.csv")))
        except Exception:  # noqa: BLE001  (publication plus ancienne : seulement le parquet)
            df = pd.read_parquet(io.BytesIO(self.read("features_derniere_journee.parquet")))
        if df.empty:
            return None, df
        df["as_of_date"] = pd.to_datetime(df["as_of_date"]).dt.date
        return df["as_of_date"].max(), df.reset_index(drop=True)

    def products(self) -> pd.DataFrame:
        return pd.read_csv(io.BytesIO(self.read("produits.csv")))

    def stored_forecasts(self, category: str | None = None, alert: str | None = None) -> pd.DataFrame:
        df = pd.read_csv(io.BytesIO(self.read("previsions.csv")))
        for col in ("as_of_date", "horizon_start", "horizon_end"):
            df[col] = pd.to_datetime(df[col]).dt.date
        if category:
            df = df[df["category"].str.lower() == category.lower()]
        if alert:
            df = df[df["forecast_alert"] == alert]
        return df.sort_values(["projected_stock_end", "product_id"]).reset_index(drop=True)

    def model_run(self, run_id: str) -> dict | None:
        m = self.etat().get("modele") or {}
        if m.get("run_id") != run_id:
            return None
        return {"run_id": run_id, "trained_at": m.get("entraine_le"), "data_until": m.get("donnees_jusqu_au"),
                "wape": m.get("wape"), "baseline_wape": m.get("wape_methode_naive"), "bias": m.get("biais")}


class PublicModelProvider:
    """Sert le modèle publié (modele.joblib) ; même interface que ModelProvider."""

    def __init__(self, source: PublicSource, check_every: float = CACHE_SECONDS) -> None:
        self.source = source
        self.check_every = check_every
        self._model: ChampionModel | None = None
        self._checked = float("-inf")
        self._lock = threading.Lock()
        self.last_error: str | None = None

    def current(self) -> ChampionModel:
        if self._model is None or time.monotonic() - self._checked > self.check_every:
            self.refresh()
        if self._model is None:
            raise ModelUnavailable(self.last_error or "modèle non publié")
        return self._model

    def refresh(self, force: bool = False) -> ChampionModel | None:
        import joblib
        with self._lock:
            self._checked = time.monotonic()
            try:
                run_id = (self.source.etat().get("modele") or {}).get("run_id")
                if run_id is None:
                    raise ModelUnavailable("aucun modèle publié")
                if force or self._model is None or self._model.run_id != run_id:
                    model = joblib.load(io.BytesIO(self.source.read("modele.joblib")))
                    self._model = ChampionModel(model.predict, run_id, "publication", None, time.time())
                    log.info("Modèle publié chargé : %s", run_id)
                self.last_error = None
            except Exception as exc:  # noqa: BLE001  (réseau, fichier absent...) : on garde l'ancien
                log.warning("Modèle publié indisponible : %s", exc)
                self.last_error = f"publication : {exc}"
            return self._model
