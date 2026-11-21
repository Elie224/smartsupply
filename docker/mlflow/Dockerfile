# Serveur MLflow de SmartSupply (suivi des entraînements + registre des modèles).
# Version fixée : le client du pipeline utilise la même (pyproject.toml).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MLFLOW_DISABLE_AGENT_HINT=1

# prometheus-flask-exporter : métriques /metrics pour Prometheus (--expose-prometheus)
RUN pip install --no-cache-dir "mlflow==3.18.0" "psycopg2-binary==2.9.*" "prometheus-flask-exporter==0.23.*"

WORKDIR /app
COPY start.py ./

EXPOSE 5000
CMD ["python", "/app/start.py"]
