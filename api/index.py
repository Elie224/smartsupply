"""Point d'entrée de l'API EN LIGNE (Vercel, offre gratuite).

Mode public : pas de base ni de MLflow ; l'API lit les résultats publiés chaque nuit sur la
branche donnees-publiques (voir src/smartsupply/api/public_source.py). En local, on utilise
toujours `uvicorn smartsupply.api.main:app` (docker compose, service api).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("SMARTSUPPLY_PUBLIC_DATA_URL",
                      "https://raw.githubusercontent.com/Elie224/smartsupply/donnees-publiques")

from smartsupply.api.main import app  # noqa: E402,F401
