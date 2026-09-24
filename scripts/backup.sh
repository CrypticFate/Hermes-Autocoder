#!/usr/bin/env bash
# Back up both databases, the data directory and the concierge home. Secrets are backed up separately.
set -euo pipefail
backup_root=${1:?Usage: scripts/backup.sh /absolute/backup/directory}
data_root=${AUTOCODER_DATA_DIR:-/var/lib/hermes-autocoder}
case "$backup_root" in /*) ;; *) echo 'Backup directory must be absolute' >&2; exit 1 ;; esac
case "$backup_root/" in "$data_root/"*) echo 'Backup directory must be outside data directory' >&2; exit 1 ;; esac
umask 077
mkdir -p "$backup_root"
backup_stamp=$(date -u +%Y%m%dT%H%M%SZ)
running_services=()
while IFS= read -r service; do
    case "$service" in controller|model-proxy|concierge) running_services+=("$service") ;; esac
done < <(docker compose ps --services --status running)
restart_services() {
    if ((${#running_services[@]})); then docker compose start "${running_services[@]}"; fi
}
trap restart_services EXIT
docker compose stop controller model-proxy concierge
for db in autocoder mem0; do
    docker compose exec -T database pg_dump -U postgres -d "$db" -Fc > "$backup_root/$backup_stamp.$db.dump"
done
tar -C "$data_root" -czf "$backup_root/$backup_stamp.data.tar.gz" .
docker compose run --rm --no-deps -T --entrypoint tar concierge -C /home/concierge/.hermes -czf - . \
    > "$backup_root/$backup_stamp.concierge_home.tar.gz"
(cd "$backup_root" && sha256sum "$backup_stamp".* > "$backup_stamp.sha256")
echo "Backup created: $backup_root/$backup_stamp"
