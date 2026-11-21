# Image du code SmartSupply : cycle nocturne (simulation, features, modèle, prévision).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# LightGBM a besoin de la bibliothèque OpenMP (libgomp), absente de l'image "slim".
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1) Dépendances seules : cette couche (la plus longue) reste en cache
#    tant que pyproject.toml ne change pas, même si le code change.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" > /tmp/requirements.txt \
 && pip install --no-cache-dir -r /tmp/requirements.txt

# 2) Le code : reconstruit en quelques secondes à chaque modification.
COPY src ./src
RUN pip install --no-cache-dir --no-deps . \
 && python -c "import lightgbm, smartsupply.models.nightly"   # échoue au build plutôt qu'au démarrage

CMD ["python", "-m", "smartsupply.pipelines.simulation_flow"]
