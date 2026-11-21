#!/bin/sh
# Démarre Prometheus. Si GRAFANA_CLOUD_PROM_URL, GRAFANA_CLOUD_PROM_USER et GRAFANA_CLOUD_TOKEN
# sont dans .env, ajoute l'envoi vers Grafana Cloud (remote_write.yml) à une copie de la
# configuration ; le jeton va dans un fichier à part, jamais dans la configuration.
set -e
CONFIG=/etc/prometheus/prometheus.yml
if [ -n "$GRAFANA_CLOUD_PROM_URL" ] && [ -n "$GRAFANA_CLOUD_PROM_USER" ] && [ -n "$GRAFANA_CLOUD_TOKEN" ]; then
  printf '%s' "$GRAFANA_CLOUD_TOKEN" > /tmp/grafana_cloud_token
  cat /etc/prometheus/prometheus.yml /etc/prometheus/remote_write.yml \
    | sed -e "s|__GRAFANA_CLOUD_PROM_URL__|$GRAFANA_CLOUD_PROM_URL|" \
          -e "s|__GRAFANA_CLOUD_PROM_USER__|$GRAFANA_CLOUD_PROM_USER|" > /tmp/prometheus.yml
  CONFIG=/tmp/prometheus.yml
  echo "Grafana Cloud : envoi activé vers $GRAFANA_CLOUD_PROM_URL"
else
  echo "Grafana Cloud : non configuré (GRAFANA_CLOUD_* absents de .env)"
fi
unset GRAFANA_CLOUD_TOKEN
exec /bin/prometheus --config.file="$CONFIG" "$@"
