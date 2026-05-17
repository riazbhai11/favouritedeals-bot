#!/bin/bash

echo "🚀 FavouriteDeals Bot Setup শুরু হচ্ছে..."

# ===== 1. System Update =====
apt update && apt upgrade -y
apt install -y git python3 python3-pip python3-venv postgresql postgresql-contrib nginx certbot python3-certbot-nginx docker.io docker-compose curl

# ===== 2. Services চালু =====
systemctl enable postgresql docker
systemctl start postgresql docker

# ===== 3. PostgreSQL Setup =====
echo "🗄️ Database setup করছি..."
sudo -u postgres psql -c "CREATE USER fduser WITH PASSWORD 'fdpass123';" 2>/dev/null || true
sudo -u postgres psql -c "CREATE DATABASE fdbot OWNER fduser;" 2>/dev/null || true

# ===== 4. Bot Clone =====
echo "🤖 Bot clone করছি..."
cd /root
git clone https://github.com/riazbhai11/favouritedeals-bot.git
cd favouritedeals-bot

# ===== 5. Python Setup =====
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# ===== 6. .env File =====
echo "⚙️ .env file বানাচ্ছি..."
cat > /root/favouritedeals-bot/.env << 'EOF'
BOT_TOKEN=8632788426:AAE6ZibScunVhsdDjJUB32pulMJ7EmWyGTE
CHAT_ID=6306041301
DATABASE_URL=postgresql://fduser:fdpass123@localhost:5432/fdbot
WC_KEY=ck_d5582984aa2714c8302cb67ea036cafc4afd4094
WC_SECRET=cs_12a91e864bec01a728383bf7e412f66862998477
EOF

# ===== 7. DB Restore (backup থাকলে) =====
if [ -f /root/restore/fdbot.sql ]; then
    echo "🗄️ Database restore করছি..."
    sudo -u postgres psql -d fdbot -f /root/restore/fdbot.sql
fi

# ===== 8. Bot Service =====
echo "⚙️ Service setup করছি..."
cat > /etc/systemd/system/fdbot.service << 'EOF'
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
EOF

systemctl daemon-reload
systemctl enable fdbot
systemctl start fdbot

# ===== 9. n8n Docker =====
echo "🐳 n8n setup করছি..."
mkdir -p /root/n8n
cat > /root/n8n/docker-compose.yml << 'EOF'
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
EOF

cd /root/n8n && docker-compose up -d

# ===== 10. Backup Script =====
cp /root/favouritedeals-bot/backup.sh /root/backup.sh 2>/dev/null || true
chmod +x /root/backup.sh

echo ""
echo "✅ সব setup সম্পন্ন!"
echo "🤖 Bot status: $(systemctl is-active fdbot)"
echo "🐳 n8n: http://localhost:5678"
echo ""
echo "📌 Restore করতে হলে backup file টা /root/restore/fdbot.sql এ রাখো"
