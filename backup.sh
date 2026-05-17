#!/bin/bash

BOT_TOKEN="8632788426:AAE6ZibScunVhsdDjJUB32pulMJ7EmWyGTE"
CHAT_ID="6306041301"
BACKUP_DIR="/root/backups"
DATE=$(date +%Y-%m-%d)
BACKUP_FILE="$BACKUP_DIR/fdbackup_$DATE.tar.gz"

mkdir -p $BACKUP_DIR

echo "DB backup নিচ্ছি..."
PGPASSWORD=fdpass123 pg_dump -U fduser -h localhost fdbot > $BACKUP_DIR/fdbot_$DATE.sql

echo "n8n backup নিচ্ছি..."
docker exec n8n_n8n_1 n8n export:workflow --all --output=/home/node/.n8n/workflows.json 2>/dev/null
cp /var/lib/docker/volumes/n8n_n8n_data/_data/workflows.json $BACKUP_DIR/n8n_workflows_$DATE.json 2>/dev/null

echo ".env backup নিচ্ছি..."
cp /root/favouritedeals-bot/.env $BACKUP_DIR/env_$DATE.txt

echo "Compress করছি..."
tar -czf $BACKUP_FILE -C $BACKUP_DIR fdbot_$DATE.sql env_$DATE.txt 2>/dev/null

echo "Telegram এ পাঠাচ্ছি..."
curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/sendDocument" \
    -F chat_id="$CHAT_ID" \
    -F document=@"$BACKUP_FILE" \
    -F caption="✅ FD System Backup — $DATE"

find $BACKUP_DIR -name "*.tar.gz" -mtime +30 -delete
find $BACKUP_DIR -name "*.sql" -mtime +1 -delete
find $BACKUP_DIR -name "*.txt" -mtime +1 -delete

echo "Backup সম্পন্ন!"
