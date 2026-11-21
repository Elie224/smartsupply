"""Le modèle servi par l'API : le CHAMPION du registre MLflow.

    1. MLflow (référence du « serving ») : models:/smartsupply-demande-7j@champion ;
    2. si MLflow ne répond pas : le modèle en service dans la base (ml.model_runs).
Toutes les 5 minutes, l'API regarde si le champion a changé (nouveau modèle retenu
cette nuit) et le recharge, sans redémarrage.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from collections.abc import Callable

import numpy as np
import pandas as pd
from sqlalchemy.engine import Engine

log = logging.getLogger("smartsupply.api")
CHECK_EVERY_SECONDS = 300


class ModelUnavailable(RuntimeError):
    """Aucun modèle chargeable (ni MLflow, ni base)."""


@dataclass
class ChampionModel:
    predict_fn: Callable[[pd.DataFrame], object]
    run_id: str                   # identifiant de l'entraînement (ml.model_runs)
    source: str                   # "mlflow" ou "base"
    mlflow_version: str | None
    loaded_at: float

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        """Demande prévue sur 7 jours, en unités entières >= 0."""
        values = np.asarray(self.predict_fn(df), dtype=float)
        return np.clip(np.rint(values), 0, None).astype(int)


class ModelProvider:
    def __init__(self, engine: Engine, check_every: float = CHECK_EVERY_SECONDS) -> None:
        self.engine = engine
        self.check_every = check_every
        self._model: ChampionModel | None = None
        self._checked = float("-inf")
        self._lock = threading.Lock()
        self.last_error: str | None = None

    def current(self) -> ChampionModel:
        if self._model is None or time.monotonic() - self._checked > self.check_every:
            self.refresh()
        if self._model is None:
            raise ModelUnavailable(self.last_error or "aucun modèle en service")
        return self._model

    def refresh(self, force: bool = False) -> ChampionModel | None:
        with self._lock:
            self._checked = time.monotonic()
            try:
                if self._refresh_from_mlflow(force):
                    self.last_error = None
                    return self._model
            except Exception as exc:  # noqa: BLE001  (MLflow absent, alias pas encore posé...)
                log.warning("Champion MLflow indisponible : %s", exc)
                self.last_error = f"MLflow : {exc}"
            try:
                self._refresh_from_db(force)
            except Exception as exc:  # noqa: BLE001
                log.error("Modèle de la base indisponible : %s", exc)
                self.last_error = f"{self.last_error or ''} ; base : {exc}".strip(" ;")
            return self._model

    # -- sources ---------------------------------------------------------------
    def _refresh_from_mlflow(self, force: bool) -> bool:
        from smartsupply.models import tracking
        uri = tracking.tracking_uri()
        if not tracking.server_available(uri):
            raise ConnectionError(f"serveur injoignable ({uri})")
        import mlflow
        from mlflow import MlflowClient

        client = MlflowClient(uri)
        version = client.get_model_version_by_alias(tracking.REGISTERED_MODEL, tracking.CHAMPION_ALIAS)
        m = self._model
        if not force and m is not None and m.source == "mlflow" and m.mlflow_version == version.version:
            return True                                   # déjà le bon modèle
        run_id = client.get_run(version.run_id).data.tags[tracking.RUN_ID_TAG]
        mlflow.set_tracking_uri(uri)
        pyfunc = mlflow.pyfunc.load_model(f"models:/{tracking.REGISTERED_MODEL}@{tracking.CHAMPION_ALIAS}")
        self._model = ChampionModel(pyfunc.predict, run_id, "mlflow", version.version, time.time())
        log.info("Champion chargé depuis MLflow : version %s (%s)", version.version, run_id)
        return True

    def _refresh_from_db(self, force: bool) -> None:
        from smartsupply.models.nightly import current_champion
        champion = current_champion(self.engine)
        if champion is None:
            raise ModelUnavailable("aucun modèle en service dans la base")
        m = self._model
        if not force and m is not None and m.run_id == champion.run_id:
            return                    # même modèle que celui déjà chargé (même depuis MLflow) : on le garde
        self._model = ChampionModel(champion.model.predict, champion.run_id, "base", None, time.time())
        log.info("Champion chargé depuis la base : %s", champion.run_id)
