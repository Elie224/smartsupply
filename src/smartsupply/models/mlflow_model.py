"""Enveloppe MLflow (« pyfunc ») du modèle de demande.

Permet de recharger n'importe quelle version depuis MLflow, sans connaître nos classes :

    import mlflow
    model = mlflow.pyfunc.load_model("models:/smartsupply-demande-7j@champion")
    model.predict(df)   # df = colonnes de ml.feature_snapshots ; renvoie la demande sur 7 jours

Le code du paquet smartsupply est joint au modèle (code_paths) : il se recharge
aussi dans un environnement où smartsupply n'est pas installé.
Importé seulement au moment d'envoyer un modèle à MLflow.
"""
from __future__ import annotations

import mlflow.pyfunc
import numpy as np


class DemandModel(mlflow.pyfunc.PythonModel):
    def load_context(self, context) -> None:
        import joblib
        self.model = joblib.load(context.artifacts["model"])

    def predict(self, context, model_input, params=None):
        return np.rint(self.model.predict(model_input)).astype(int)
