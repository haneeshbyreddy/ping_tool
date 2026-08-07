#!/usr/bin/env bash
# Bare Debian VM -> WISP central serving traffic, in one command.
#
# This exists because the thing that actually blows a recovery target is not compute, it is
# a person reading a runbook at 2am and mistyping step 6. Measured on the live box, every
# step below costs seconds: the venv builds in 6.3s (three pure-Python deps), the Caddyfile
# is three lines, and the backup bundle is 4 MB. The only genuinely slow parts are apt and
# DNS, and neither gets faster by being done by hand.
#
#   ./deploy/bootstrap.sh /path/to/wisp-backup-YYYYmmdd-HHMMSS.tar.gz
#
# Idempotent: safe to re-run. It will NOT overwrite an existing data/central.db unless you
# pass --force, because the most likely way to lose data during a recovery is to "restore"
# over a database that was actually fine.
set -euo pipefail

BUNDLE="${1:-}"
FORCE=0
[[ "${2:-}" == "--force" || "${1:-}" == "--force" ]] && FORCE=1
[[ "${1:-}" == "--force" ]] && BUNDLE="${2:-}"

REPO_URL="git@github.com:haneeshbyreddy/ping_tool.git"
APP_DIR="${APP_DIR:-$HOME/ping_tool}"
DOMAIN="${DOMAIN:-hansanet.in}"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[[ -n "$BUNDLE" ]] || die "usage: $0 <backup-bundle.tar.gz> [--force]"
[[ -f "$BUNDLE" ]] || die "no such bundle: $BUNDLE"
BUNDLE="$(readlink -f "$BUNDLE")"

say "1/7  OS packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip git curl openssl debian-keyring \
    debian-archive-keyring apt-transport-https
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -qq && sudo apt-get install -y -qq caddy
fi

say "2/7  code"
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" fetch --all -q && git -C "$APP_DIR" checkout -q main && git -C "$APP_DIR" pull -q
else
  git clone -q "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"

say "3/7  python env"
[[ -d .venv ]] || python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt

say "4/7  verify the bundle BEFORE trusting it"
# Never restore from a bundle nothing has read back. This checks the sha256 and runs
# PRAGMA integrity_check on the actual database inside it.
./.venv/bin/python tools/backup.py --verify "$BUNDLE"

say "5/7  restore data + secrets"
mkdir -p data
if [[ -f data/central.db && $FORCE -eq 0 ]]; then
  die "data/central.db already exists. Re-run with --force ONLY if you are certain the
     existing database is the bad one. Recovering over a healthy DB is the classic way to
     turn an outage into data loss."
fi
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
tar xzf "$BUNDLE" -C "$TMP"
gunzip -c "$TMP/central.db.gz" > data/central.db
cp "$TMP/secrets/secret.key"              data/           2>/dev/null || true
cp "$TMP/secrets/central_session_secret"  data/           2>/dev/null || true
cp "$TMP/secrets/central.env"             deploy/         2>/dev/null || true
chmod 600 data/secret.key data/central_session_secret deploy/central.env 2>/dev/null || true
# secret.key decrypts the device web-UI vault. Its absence is silent at runtime — the rows
# survive and decode to nothing — so it is worth failing loudly here instead.
[[ -f data/secret.key ]] || echo "WARNING: no secret.key in the bundle; device credentials will be unreadable"

say "6/7  services"
sudo cp deploy/wisp-central.service deploy/wisp-backup.service deploy/wisp-backup.timer \
        /etc/systemd/system/
[[ -f deploy/wisp-release-sync.service ]] && sudo cp deploy/wisp-release-sync.service \
        deploy/wisp-release-sync.timer /etc/systemd/system/ || true
sudo sed -i "s|^User=.*|User=$SERVICE_USER|;s|^WorkingDirectory=.*|WorkingDirectory=$APP_DIR|" \
        /etc/systemd/system/wisp-central.service /etc/systemd/system/wisp-backup.service
printf '%s {\n\treverse_proxy 127.0.0.1:8443\n}\n' "$DOMAIN" | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now wisp-central wisp-backup.timer
sudo systemctl enable --now wisp-release-sync.timer 2>/dev/null || true
sudo systemctl reload caddy || sudo systemctl restart caddy

say "7/7  verify"
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8443/ || true)
  [[ "$code" == "200" ]] && break
  sleep 2
done
[[ "${code:-}" == "200" ]] || die "central did not answer on 127.0.0.1:8443 (journalctl -u wisp-central)"

./.venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect('file:data/central.db?mode=ro', uri=True)
for t in ('orgs','users','org_devices','onu_places','onu_drops'):
    print(f"    {t:14} {c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:>6,}")
PY

cat <<EOF

  central is up locally and the data is in place.

  REMAINING, and neither is something this script should do for you:
    1. Point $DOMAIN at this machine's IP.
       - If you reserved a static IP: reattach it. No DNS change, no wait.
       - Otherwise update the A record. TTL is 600s, so allow ~10 min.
    2. Watch the edges reconnect (they dial central by URL, so they need no change):
         journalctl -u wisp-central -f | grep report

EOF
