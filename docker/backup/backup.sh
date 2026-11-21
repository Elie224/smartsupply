#!/bin/sh
# Sauvegarde automatique des bases SmartSupply (service « backup » du docker-compose).
#
#   - toutes les heures, regarde l'âge de la dernière sauvegarde ; si elle a plus de
#     BACKUP_EVERY_DAYS jours (7 par défaut), sauvegarde les bases « smartsupply » et « mlflow » ;
#   - fichiers dans ./backups sur le PC : <base>_AAAAMMJJ_HHMMSS.dump (format pg_dump -Fc) ;
#   - chaque fichier est relu (pg_restore --list) avant d'être gardé : un fichier abîmé
#     n'est jamais compté comme une sauvegarde ;
#   - garde les BACKUP_KEEP dernières (8 par défaut = 2 mois) de chaque base ;
#   - dépose l'heure et la taille de la sauvegarde dans le Pushgateway (alerte
#     SauvegardeAncienne si plus rien depuis 8 jours).
#
#   sh /scripts/backup.sh --maintenant     # une sauvegarde tout de suite, puis s'arrête
set -u
DIR=${BACKUP_DIR:-/backups}
EVERY_DAYS=${BACKUP_EVERY_DAYS:-7}
KEEP=${BACKUP_KEEP:-8}
DATABASES=${BACKUP_DATABASES:-"$PGDATABASE mlflow"}
PUSHGATEWAY=${PUSHGATEWAY_URL:-http://pushgateway:9091}

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

push_metrics() {   # $1 = lignes « taille par base » déjà au format Prometheus
    body="# HELP smartsupply_backup_last_success_timestamp_seconds Heure de la dernière sauvegarde réussie
# TYPE smartsupply_backup_last_success_timestamp_seconds gauge
smartsupply_backup_last_success_timestamp_seconds $(date +%s)
# HELP smartsupply_backup_size_bytes Taille de la dernière sauvegarde, par base
# TYPE smartsupply_backup_size_bytes gauge
$1"
    wget -q -O /dev/null --post-data="$body" "$PUSHGATEWAY/metrics/job/smartsupply_backup" \
        || log "Pushgateway injoignable (sans gravité)"
}

backup_now() {
    stamp=$(date '+%Y%m%d_%H%M%S')
    sizes=""
    for db in $DATABASES; do
        if ! psql -tAc "SELECT 1" -d "$db" >/dev/null 2>&1; then
            log "base « $db » absente : ignorée"
            continue
        fi
        file="$DIR/${db}_$stamp.dump"
        if pg_dump -Fc -d "$db" -f "$file.part" && pg_restore --list "$file.part" >/dev/null; then
            mv "$file.part" "$file"
            size=$(wc -c < "$file" | tr -d ' ')
            sizes="${sizes}smartsupply_backup_size_bytes{database=\"$db\"} $size
"
            log "sauvegarde OK : $(basename "$file") ($((size / 1024 / 1024)) Mo)"
            # rotation : on garde les KEEP plus récentes de cette base
            ls -1t "$DIR/${db}"_*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
                rm -f "$old" && log "ancienne sauvegarde supprimée : $(basename "$old")"
            done
        else
            rm -f "$file.part"
            log "ÉCHEC de la sauvegarde de « $db »"
            return 1
        fi
    done
    push_metrics "$sizes"
}

last_age_days() {   # âge en jours de la sauvegarde la plus récente de la base principale (999 si aucune)
    newest=$(ls -1t "$DIR/${PGDATABASE}"_*.dump 2>/dev/null | head -n 1)
    [ -z "$newest" ] && { echo 999; return; }
    echo $(( ($(date +%s) - $(stat -c %Y "$newest")) / 86400 ))
}

mkdir -p "$DIR"
until pg_isready -q; do log "en attente de PostgreSQL..."; sleep 5; done

if [ "${1:-}" = "--maintenant" ]; then
    backup_now
    exit $?
fi

log "sauvegarde automatique : toutes les $EVERY_DAYS jours, $KEEP gardées par base ($DATABASES)"
while true; do
    age=$(last_age_days)
    if [ "$age" -ge "$EVERY_DAYS" ]; then
        log "dernière sauvegarde : il y a $age jour(s) -> nouvelle sauvegarde"
        backup_now || log "nouvel essai dans une heure"
    fi
    sleep 3600
done
