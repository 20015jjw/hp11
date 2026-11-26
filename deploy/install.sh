#!/bin/sh
# Run as root inside the Debian container/VM, from a checkout of this repo:
#   sh deploy/install.sh
# Installs to /opt/hp11 and (re)starts the hp11 systemd service on port 8011.
# Safe to re-run after `git pull`; presets and schedules in /var/lib/hp11 are kept.
set -eu
SRC=$(cd "$(dirname "$0")/.." && pwd)
[ -f "$SRC/.env" ] || [ -f /opt/hp11/.env ] || {
  echo "missing $SRC/.env (run: python3 scripts/get_credentials.py)"; exit 1; }

apt-get update -qq
apt-get install -y -qq python3-venv openssl
id hp11 >/dev/null 2>&1 || useradd --system --home /opt/hp11 --shell /usr/sbin/nologin hp11
mkdir -p /opt/hp11
rm -rf /opt/hp11/app
cp -r "$SRC/app" "$SRC/requirements.txt" /opt/hp11/
[ -f "$SRC/.env" ] && install -m 600 -o hp11 -g hp11 "$SRC/.env" /opt/hp11/.env
python3 -m venv /opt/hp11/.venv
/opt/hp11/.venv/bin/pip install -q -r /opt/hp11/requirements.txt
chown -R hp11:hp11 /opt/hp11
install -m 644 "$SRC/deploy/hp11.service" /etc/systemd/system/hp11.service
systemctl daemon-reload
systemctl enable hp11 >/dev/null
systemctl restart hp11
sleep 3
systemctl --no-pager --lines=5 status hp11
echo "UI: http://$(hostname -I | awk '{print $1}'):8011/"
