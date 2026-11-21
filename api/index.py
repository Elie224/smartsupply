"""Point d'entrée de l'API EN LIGNE (Vercel, offre gratuite).

Mode public : pas de base ni de MLflow ; l'API lit les résultats publiés chaque nuit sur la
branche donnees-publiques (voir src/smartsupply/api/public_source.py). En local, on utilise
toujours `uvicorn smartsupply.api.main:app` (docker compose, service api).
"""
import ctypes
import os
import sys
from pathlib import Path

# LightGBM a besoin de libgomp.so.1, absente des machines de Vercel : on charge la copie
# livrée dans api/lib AVANT d'importer LightGBM (voir api/lib/README.md).
_GOMP = Path(__file__).resolve().parent / "lib" / "libgomp.so.1"
if sys.platform.startswith("linux") and _GOMP.exists():
    try:
        ctypes.CDLL(str(_GOMP), mode=ctypes.RTLD_GLOBAL)
    except OSError as exc:  # la bibliothèque du système servira, s'il y en a une
        print(f"libgomp fournie non chargée : {exc}")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("SMARTSUPPLY_PUBLIC_DATA_URL",
                      "https://raw.githubusercontent.com/Elie224/smartsupply/donnees-publiques")

from smartsupply.api.main import app  # noqa: E402,F401
