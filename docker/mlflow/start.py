"""Démarrage du serveur MLflow.

1. crée la base PostgreSQL « mlflow » si elle n'existe pas (dans le même serveur
   Postgres que SmartSupply, mais séparée des données métier) ;
2. lance `mlflow server` avec :
   - les runs et le registre dans cette base ;
   - les fichiers (modèles, importances...) dans le volume /mlartifacts,
     servis par MLflow lui-même : le pipeline et ton PC n'ont pas besoin d'y accéder.

Le mot de passe vient de .env (variables d'environnement), jamais du code.
"""
import os
import shutil
import time
from urllib.parse import quote

import psycopg2
from psycopg2 import sql

user = os.environ["POSTGRES_USER"]
password = os.environ["POSTGRES_PASSWORD"]
host = os.environ.get("POSTGRES_HOST", "postgres")
port = os.environ.get("POSTGRES_PORT", "5432")
main_db = os.environ["POSTGRES_DB"]
mlflow_db = os.environ.get("MLFLOW_DB", "mlflow")

for attempt in range(30):                      # Postgres peut mettre quelques secondes
    try:
        conn = psycopg2.connect(host=host, port=port, user=user, password=password, dbname=main_db)
        break
    except psycopg2.OperationalError as exc:
        print(f"Postgres pas encore prêt ({exc.__class__.__name__}), nouvel essai dans 2 s")
        time.sleep(2)
else:
    raise SystemExit("Postgres injoignable : abandon")

conn.autocommit = True                         # CREATE DATABASE interdit dans une transaction
with conn.cursor() as cur:
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (mlflow_db,))
    if cur.fetchone() is None:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(mlflow_db)))
        print(f"Base « {mlflow_db} » créée")
conn.close()

backend = f"postgresql+psycopg2://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{mlflow_db}"
public_port = os.environ.get("MLFLOW_PORT", "15000")
# Dossier de travail des métriques Prometheus : vidé à chaque démarrage.
metrics_dir = "/tmp/prometheus_metrics"
shutil.rmtree(metrics_dir, ignore_errors=True)
os.makedirs(metrics_dir)
os.execvp("mlflow", [
    "mlflow", "server",
    "--host", "0.0.0.0", "--port", "5000",
    "--workers", "2",
    "--backend-store-uri", backend,
    "--artifacts-destination", "/mlartifacts",
    "--expose-prometheus", metrics_dir,          # /metrics pour Prometheus
    # Noms sous lesquels on joint le serveur : depuis le pipeline (réseau Docker)
    # et depuis ton navigateur / ton PC. Les autres sont refusés (sécurité MLflow).
    "--allowed-hosts", f"mlflow:5000,mlflow,127.0.0.1:{public_port},localhost:{public_port}",
])
