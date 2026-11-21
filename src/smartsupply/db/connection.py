"""Connexion à PostgreSQL à partir du fichier .env.

Le mot de passe n'est jamais écrit dans le code : il est lu dans .env
(qui est ignoré par Git).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine

# connection.py -> db -> smartsupply -> src -> racine du projet
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def get_database_url() -> URL:
    """Construit l'URL de connexion.

    URL.create échappe correctement les caractères spéciaux du mot de passe
    (@, :, / ...), contrairement à une simple f-string.
    """
    load_dotenv(PROJECT_ROOT / ".env")
    return URL.create(
        drivername="postgresql+psycopg",
        username=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        # 127.0.0.1 plutôt que localhost : Docker n'écoute qu'en IPv4.
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "15432")),
        database=os.environ["POSTGRES_DB"],
    )


@lru_cache
def get_engine() -> Engine:
    """Un seul moteur (pool de connexions) partagé par tout le programme."""
    # connect_timeout : si la base ne répond pas (Docker arrêté, port bloqué après une
    # mise en veille), on échoue en 10 s au lieu d'attendre indéfiniment.
    return create_engine(get_database_url(), pool_pre_ping=True,
                         connect_args={"connect_timeout": 10})
