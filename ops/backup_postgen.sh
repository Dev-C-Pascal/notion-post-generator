#!/bin/bash
# Нічний дамп бази драфтів postgen. Запуск — crontab користувача ubuntu на EC2:
#   20 1 * * * /home/ubuntu/post-generator/ops/backup_postgen.sh >> /home/ubuntu/backups/postgen-backup.log 2>&1
# Дампи лежать на тому самому диску: рятують від помилки чи видалення даних, але не від втрати інстансу
# (для цього — EBS-снапшоти, їх налаштовує власник AWS-акаунта). Відновлення:
#   docker compose exec -T db pg_restore -U postgen -d postgen --clean < ~/backups/postgen-YYYY-MM-DD.dump
set -euo pipefail

DIR=/home/ubuntu/backups
KEEP_DAYS=14
OUT="$DIR/postgen-$(date -u +%F).dump"

mkdir -p "$DIR"
chmod 700 "$DIR"
cd /home/ubuntu/post-generator
docker compose exec -T db pg_dump -U postgen -d postgen -Fc > "$OUT.tmp" < /dev/null
mv "$OUT.tmp" "$OUT"
chmod 600 "$OUT"
find "$DIR" -name 'postgen-*.dump' -mtime +"$KEEP_DAYS" -delete
echo "$(date -u +%FT%TZ) ok $(du -h "$OUT" | cut -f1) $OUT"
