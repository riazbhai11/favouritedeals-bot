#!/bin/bash

echo "🚀 FavouriteDeals Setup শুরু হচ্ছে..."

BOT_TOKEN="8632788426:AAE6ZibScunVhsdDjJUB32pulMJ7EmWyGTE"
CHAT_ID="6306041301"

# 1. System Update
apt update && apt upgrade -y
apt install -y git python3 python3-pip python3-venv postgresql postgresql-contrib nginx certbot python3-certbot-nginx docker.io docker-compose curl

# 2. Services চালু
systemctl enable postgresql docker
systemctl start postgresql docker

# 3. Telegram থেকে backup নামাও
echo "📥 Telegram থেকে ba
cat > /root/setup.sh << 'SCRIPT'
#!/bin/bash

echo "🚀 FavouriteDeals Setup শুরু হচ্ছে..."

BOT_TOKEN="8632788426:AAE6ZibScunVhsdDjJUB32pulMJ7EmWyGTE"
CHAT_ID="6306041301"

# 1. System Update
apt update && apt upgrade -y
apt install -y git python3 python3-pip python3-venv postgresql postgresql-contrib nginx certbot python3-certbot-nginx docker.io docker-compose curl

# 2. Services চালু
systemctl enable postgresql docker
systemctl start postgresql docker

# 3. Telegram থেকে backup নামাও
echo "📥 Telegram থেকে backup নামাচ্ছি..."
mkdir -p /root/restore

FILE_ID=$(curl -s "https://api.telegram.org/bot$BOT_TOKEN/getUpdates?limit=100" | \
    python3 -c "
import sys, json
data = json.load(sys.stdin)
docs = []
for r in data.get('result', []):
    msg = r.get('message', {})
    if 'document' in msg and msg['document']['file_name'].startswith('fdbackup'):
        docs.append((msg['date'], msg['document']['file_id']))
docs.sort(reverse=True)
print(docs[0][1] if docs else '')
")

if [ -n "$FILE_ID" ]; then
    FILE_PATH=$(curl -s "https://api.telegram.org/bot$BOT_TOKEN/getFile?file_id=$FILE_ID" | \
        python3 -c "import sys,json; print(json.load(sys.stdin)['result']['file_path'])")
    curl -s "https://api.telegram.org/file/bot$BOT_TOKEN/$FILE_PATH" -o /root/restore/backup.tar.gz
    cd /root/restore && tar -xzf backup.tar.gz
    echo "✅ Backup extract হয়েছে"
else
    echo "⚠️ Backup পাওয়া যায়নি, fresh install হবে"
fi

# 4. PostgreSQL Setup
sudo -u postgres psql -c "CREATE USER fduser WITH PASSWORD 'fdpass123';" 2>/dev/null || true
sudo -u postgres psql -c "CREATE DATABASE fdbot OWNER fduser;" 2>/dev/null || true

# 5. DB Restore
SQL_FILE=$(ls /root/restore/fdbot_*.sql 2>/dev/null | head -1)
if [ -f "$SQL_FILE" ]; then
    echo "🗄️ Database restore করছি..."
    PGPASSWORD=fdpass123 psql -U fduser -h localhost fdbot < $SQL_FILE
fi

# 6. Bot Clone
cd /root
git clone https://github.com/riazbhai11/favouritedeals-bot.git
cd favouritedeals-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 7. .env Restore
ENV_FILE=$(ls /root/restore/env_*.txt 2>/dev/null | head -1)
if [ -f "$ENV_FILE" ]; then
    cp $ENV_FILE /root/favouritedeals-bot/.env
    echo "✅ .env restore হয়েছে"
else
    cat > /root/favouritedeals-bot/.env << 'ENV'
BOT_TOKEN=8632788426:AAE6ZibScunVhsdDjJUB32pulMJ7EmWyGTE
CHAT_ID=6306041301
DATABASE_URL=postgresql://fduser:fdpass123@localhost:5432/fdbot
WC_KEY=ck_d5582984aa2714c8302cb67ea036cafc4afd4094
WC_SECRET=cs_12a91e864bec01a728383bf7e412f66862998477
ENV
fi

# 8. Bot Service
cat > /etc/systemd/system/fdbot.service << 'SERVICE'
[Unit]
Description=FavouriteDeals Telegram Bot
After=network.target postgresql.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/favouritedeals-bot
ExecStart=/root/favouritedeals-bot/venv/bin/python bot.py
Restart=always
RestartSec=10
EnvironmentFile=/root/favouritedeals-bot/.env

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable fdbot
systemctl start fdbot

# 9. n8n Docker
mkdir -p /root/n8n
cat > /root/n8n/docker-compose.yml << 'COMPOSE'
version: "3.8"
services:
  n8n:
    image: n8nio/n8n
    restart: always
    ports:
      - "5678:5678"
    environment:
      - N8N_HOST=n8n.favouritedeals.online
      - N8N_PORT=5678
      - N8N_PROTOCOL=https
      - WEBHOOK_URL=https://n8n.favouritedeals.online/
      - N8N_BASIC_AUTH_ACTIVE=true
      - N8N_BASIC_AUTH_USER=admin
      - N8N_BASIC_AUTH_PASSWORD=RiazBhaierFavouriteDeals!!
    volumes:
      - n8n_data:/home/node/.n8n
volumes:
  n8n_data:
COMPOSE

cd /root/n8n && docker-compose up -d

# 10. Nginx
cat > /etc/nginx/sites-available/n8n << 'NGINX'
server {
    listen 80;
    server_name n8n.favouritedeals.online;
    location / {
        proxy_pass http://localhost:5678;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/n8n /etc/nginx/sites-enabled/
systemctl restart nginx

# 11. Backup Script
cp /root/favouritedeals-bot/backup.sh /root/backup.sh
chmod +x /root/backup.sh
(crontab -l 2>/dev/null; echo "0 10 1 * * /root/backup.sh >> /root/backup.log 2>&1") | crontab -

echo ""
echo "✅ সব setup সম্পন্ন!"
echo "🤖 Bot: $(systemctl is-active fdbot)"
echo "🐳 n8n: চলছে"
echo ""
echo "⚠️ এখন SSL নাও:"
echo "certbot --nginx -d n8n.favouritedeals.online"
