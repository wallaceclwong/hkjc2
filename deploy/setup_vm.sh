#!/bin/bash
# hkjc2 — VM setup script. Run as root on Ubuntu 22.04/24.04.
set -euo pipefail

echo "=== hkjc2 VM setup ==="

# ── System deps ──
echo "[1/5] System packages..."
apt-get update
apt-get install -y --no-install-recommends python3.12 python3.12-venv python3-pip cron curl git

# ── App user ──
echo "[2/5] Creating cp user..."
if ! id -u cp >/dev/null 2>&1; then
    useradd -m -s /bin/bash cp
fi
APP_DIR="/opt/hkjc2"

# ── Clone repo ──
echo "[3/5] Cloning repository..."
if [ ! -d "$APP_DIR" ]; then
    git clone https://github.com/YOUR_USER/hkjc2.git "$APP_DIR"
    chown -R cp:cp "$APP_DIR"
fi
cd "$APP_DIR"

# ── Python venv ──
echo "[4/5] Python environment..."
if [ ! -d ".venv" ]; then
    sudo -u cp python3.12 -m venv .venv
fi
sudo -u cp .venv/bin/pip install --upgrade pip
sudo -u cp .venv/bin/pip install -r requirements.txt
sudo -u cp .venv/bin/playwright install chromium

# ── Data dirs ──
mkdir -p data models training_data
chown -R cp:cp data models training_data

# ── .env ──
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "  Created .env from .env.example — EDIT IT with your keys!"
fi

# ── systemd timers ──
echo "[5/5] Installing systemd timers..."
cp deploy/cp-racecards.service /etc/systemd/system/
cp deploy/cp-racecards.timer /etc/systemd/system/
cp deploy/cp-predict.service /etc/systemd/system/
cp deploy/cp-odds.service /etc/systemd/system/
cp deploy/cp-odds.timer /etc/systemd/system/
cp deploy/cp-learn.service /etc/systemd/system/
cp deploy/cp-learn.timer /etc/systemd/system/

systemctl daemon-reload
systemctl enable cp-racecards.timer
systemctl enable cp-odds.timer
systemctl enable cp-learn.timer

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit /opt/hkjc2/.env with DEEPSEEK_API_KEY and TELEGRAM tokens"
echo "  2. Copy models/ to VM: scp -r models/ cp@VM:/opt/hkjc2/"
echo "  3. Copy training data: scp final_feature_matrix.parquet cp@VM:/opt/hkjc2/training_data/"
echo "  4. Copy pedigree and sentiment caches: scp data/pedigree_cache.json data/ai_sentiment_cache.parquet cp@VM:/opt/hkjc2/data/"
echo "  5. Run once manually: sudo -u cp .venv/bin/python scripts/ingest_racecards.py --date $(date +%Y-%m-%d) --venue ST"
echo ""
echo "Timer status:"
echo "  systemctl list-timers cp-*"
