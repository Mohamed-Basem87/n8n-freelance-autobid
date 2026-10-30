#!/usr/bin/env bash
# Install the host service on a Debian/Ubuntu machine.
#
#   sudo ./deploy/install.sh
#
# Creates a service user, installs the Python venv, prepares the data folder
# and installs the systemd unit. It never starts bidding on its own: the
# environment file ships with ALLOW_SUBMIT=0.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/freelance-autobid}"
APP_USER="${APP_USER:-autobid}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "run with sudo" >&2
  exit 1
fi

echo "==> packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip firefox-esr firefox-geckodriver \
  >/dev/null

echo "==> service user"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"

echo "==> files into $APP_DIR"
mkdir -p "$APP_DIR"
install -m 0644 "$REPO_DIR/backend/host_app.py" "$APP_DIR/host_app.py"
install -m 0644 "$REPO_DIR/backend/login.py" "$APP_DIR/login.py"
install -m 0644 "$REPO_DIR/backend/requirements.txt" "$APP_DIR/requirements.txt"
install -m 0644 "$REPO_DIR/backend/freelance-autobid.service" /etc/systemd/system/freelance-autobid.service
sed -i "s#/opt/freelance-autobid#$APP_DIR#g; s#^User=.*#User=$APP_USER#; s#^Group=.*#Group=$APP_USER#" \
  /etc/systemd/system/freelance-autobid.service

echo "==> venv"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> data dirs"
mkdir -p "$APP_DIR/data/test-profile" "$APP_DIR/data/screenshots" "$APP_DIR/data/attachments"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

if [[ ! -f "$APP_DIR/host-api.env" ]]; then
  cp "$REPO_DIR/backend/host-api.env.example" "$APP_DIR/host-api.env"
  sed -i "s#^LOKI_DATA_DIR=.*#LOKI_DATA_DIR=$APP_DIR/data#" "$APP_DIR/host-api.env"
  sed -i "s#^API_KEY=.*#API_KEY=$(openssl rand -hex 32)#" "$APP_DIR/host-api.env"
  chmod 600 "$APP_DIR/host-api.env"
  chown "$APP_USER:$APP_USER" "$APP_DIR/host-api.env"
  echo "    generated $APP_DIR/host-api.env with a random API_KEY"
  echo "    your API_KEY: $(grep '^API_KEY=' "$APP_DIR/host-api.env" | cut -d= -f2)"
fi

echo "==> systemd"
systemctl daemon-reload
systemctl enable freelance-autobid.service >/dev/null
systemctl restart freelance-autobid.service
sleep 3
systemctl is-active freelance-autobid.service

cat <<EOF

Next steps
  1. Copy exactly four portfolio images into $APP_DIR/data/attachments/
  2. Log in to Mostaql and Nafezly once, with a display attached:

       sudo systemctl stop freelance-autobid
       sudo -u $APP_USER DISPLAY=:0 $APP_DIR/venv/bin/python $APP_DIR/login.py
       sudo systemctl start freelance-autobid

     The session cookies live in the profile, so this is a one-time step.
  3. Import workflow/freelance-autobid.json into n8n and set the env vars below.
  4. Keep ALLOW_SUBMIT=0 until a dry run looks right on a real project.

EOF
