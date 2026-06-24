# Deploying WISP

How to run this monitor on a single always-on Linux box you own (your father's
server). Read alongside `README.md` (what it is / how to run) and `CLAUDE.md`
(invariants). The systemd units in `deploy/` are the starting point.

This is the **simple, single-box** deployment: one server, you control root, plain
Python under systemd. No containers, no release pipeline, no code obfuscation — none
of that earns its keep for one appliance you own.

## Why native systemd, not Docker

This is a **network monitor** — it wants the host's real network stack. In a container
you're forced into `network_mode: host` + the ping-group sysctl just to ping correctly,
at which point the container buys almost no isolation while adding a moving part. A
plain directory install under systemd is simpler and easier to debug.

## What runs

Two long-lived processes, both pointed at the same SQLite DB (WAL lets them coexist):

- **`wisp-monitor`** — the polling daemon (`apps/daemon/main.py`). Needs the venv
  (`icmplib` + `httpx`) and the kernel ping group enabled.
- **`wisp-dashboard`** — the operator web UI (`apps/dashboard/main.py`). Pure stdlib,
  but uses the venv too so the "Send test alert" button (httpx → ntfy) works.

State lives under `data/` in the install dir: `wisp.db` (+ wal/shm) and `session_secret`.
That directory is **git-ignored**, so upgrading with `git pull` never touches your
database or history.

## Quick install (one script)

Once the code is on the box, a single idempotent script does all the OS setup, venv,
sysctl, DB migrate, and systemd wiring — and re-running it after a `git pull` upgrades:

```bash
# get the code to its final home (private repo → deploy key, or scp). Then:
cd /opt/wisp
sudo deploy/install.sh
```

It prints the dashboard URL and the firewall commands to run next. It deliberately does
**not** clone the repo (that needs your credentials) or touch the firewall (that needs
your LAN subnet — locking yourself out of SSH is no fun). Everything else is automatic.
The manual walkthrough below is the same steps, broken out, if you want to understand or
adjust any of them.

## Windows (native, no WSL)

Prefer Linux — it's what this tool targets, and unprivileged ICMP "just works."
But if the box must be Windows, `deploy/install.ps1` is the PowerShell sibling of
`install.sh`: venv → deps → DB migrate → two **Scheduled Tasks** that auto-start at
boot and restart on crash. Re-run after a `git pull` to upgrade.

```powershell
# From an ELEVATED PowerShell (Run as administrator), in the repo:
powershell -ExecutionPolicy Bypass -File deploy\install.ps1
# different port:   ... -File deploy\install.ps1 -Port 9000
# remove the tasks: ... -File deploy\install.ps1 -Uninstall
```

Install Python from python.org first (tick **Add to PATH**). The script prints the
dashboard URL and the `netsh` firewall command to lock it to your LAN.

**The one Windows gotcha:** Windows has no unprivileged-ICMP path (no
`ping_group_range`, no datagram ICMP sockets), so icmplib falls back to **raw sockets,
which require Administrator**. That's why the tasks run as **SYSTEM** — it has the
raw-socket right and starts before any user logs in. There's nothing to "enable" like
the Linux ping group. We use Scheduled Tasks rather than NSSM so nothing has to be
downloaded onto a locked-down box; they give the same auto-start + restart as the
systemd units. Manage them with `Get-ScheduledTask WISP-*` / `Restart-ScheduledTask`.

> Not verified on Windows hardware from this repo — the logic mirrors the tested
> `install.sh`, but smoke-test it on your box (open the dashboard, fire a test alert)
> before trusting it.

## First-time install (manual walkthrough)

Install to `/opt/wisp` (what the systemd units expect). Get the code there however you
like — `git clone` the private repo with a deploy key, or `scp` a copy across.

```bash
# 1. put the code at /opt/wisp (private repo via SSH deploy key, or scp)
sudo git clone git@github.com:haneeshbyreddy/ping_tool.git /opt/wisp
cd /opt/wisp

# 2. venv for the daemon's two deps (system Python is PEP 668-locked — never install globally)
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# 3. let the daemon send ICMP as a normal user (unprivileged ping sockets — no root, no cap_net_raw)
echo 'net.ipv4.ping_group_range=0 2147483647' | sudo tee /etc/sysctl.d/99-wisp-ping.conf
sudo sysctl --system

# 4. create the DB + run migrations
PYTHONPATH=src python -m wisp.database.client

# 5. install + start both services
sudo cp deploy/wisp-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wisp-monitor wisp-dashboard
```

Check it came up:

```bash
systemctl status wisp-monitor wisp-dashboard
journalctl -u wisp-monitor -f          # live daemon log
```

Then open the dashboard at `http://<box-lan-ip>:8000`, set the PIN on first visit, and
add your real devices + team from the UI.

> **Edit the units before installing if your paths differ.** The shipped
> `deploy/wisp-*.service` assume `/opt/wisp` and the venv at `/opt/wisp/.venv`. They run
> as root by default; to run as an unprivileged user, create one and uncomment the
> `User=`/`Group=` lines (the ping-group sysctl above is what lets a non-root user ping).

## The one security rule

The dashboard is **plain HTTP + a shared PIN**. Keep it on the office LAN, **off the
public internet** — no port-forward, no "just for me to check from outside." If your
father needs to reach it remotely, put him on the LAN with a VPN (WireGuard / Tailscale);
do not expose port 8000. (See `plan.md` §8.2.)

## Alert channels (ntfy topics)

Both units set the same three `WISP_NTFY_TOPIC_*` env vars — they **must match between
the two units**, or pages route to a different topic than the dashboard shows. The
defaults are unguessable strings so they can't be read on the public `ntfy.sh`. Each
person subscribes (in the ntfy app) to the topic for their role. Change them by editing
both unit files and `systemctl daemon-reload && systemctl restart wisp-monitor
wisp-dashboard`.

## Upgrades

Because the DB lives in the git-ignored `data/` dir, upgrading is just pull + restart:

```bash
cd /opt/wisp
sudo git pull
. .venv/bin/activate && pip install -r requirements.txt   # only if requirements changed
sudo systemctl restart wisp-monitor wisp-dashboard
```

Schema upgrades ride along automatically — the migration runner tracks
`schema_migrations` and applies any new `migrations/000N_*.sql` forward on start.

- **Device-set changes** (add/remove nodes in the UI) hot-reload in-process — no restart.
- **Config (`WISP_*`) changes** need a daemon restart to take effect.
- `Restart=always` in the units covers crashes.

## Back up the database

The DB is the whole memory — PIN, team, device inventory, outage history. Two ways to
keep a copy:

- **From the UI:** Settings ▸ **Download backup** — a consistent `VACUUM INTO` snapshot
  (safe to take while the daemon is writing).
- **From the shell:** copy `data/wisp.db` while the services are stopped, or run
  `sqlite3 data/wisp.db "VACUUM INTO 'wisp-backup.db'"` while they're live.

Keep a backup off the box (the whole point is surviving a dead disk). Restoring is just
dropping the file back at `data/wisp.db` and restarting.

## Config (optional)

Every tunable is a `WISP_*` env var read once at startup (full list + defaults in
`src/wisp/config.py`). Set them in the systemd units' `[Service]` block as
`Environment=WISP_…=…` and restart. The common ones:

| Var | Default | Meaning |
|---|---|---|
| `WISP_POLL_INTERVAL_S` | `60` | steady-state seconds between polls (detection no longer hinges on this — see fast-confirm) |
| `WISP_RETRY_INTERVAL_S` | `2` | fast-confirm: re-probe a lossy device every Ns until DOWN is confirmed (~4s detection); `0` disables |
| `WISP_POLL_INTERVAL_ADAPTIVE` | `0` | `1` = poll every `WISP_POLL_INTERVAL_SMALL_S` (30) while the fleet ≤ `WISP_SMALL_FLEET_MAX` (1000), else fall back to `WISP_POLL_INTERVAL_S` |
| `WISP_PINGS_PER_POLL` / `_INFRA` | `5` / `2` | echoes per poll for leaf CPEs / for aggregation gear (gentle on tower control planes) |
| `WISP_MAX_INFLIGHT` | `256` | cap on concurrent probes — keeps a large fleet from exhausting file descriptors (raise `ulimit -n` too) |
| `WISP_POLL_RETENTION_DAYS` | `7` | days of raw poll samples kept (scratch); hourly rollups (`poll_rollups`) + `outages` are the durable record |
| `WISP_ESCALATE_EVERY_MIN` | `60` | minutes between all-hands re-pages while an outage stays open |
| `WISP_CANARY_IP` | `1.1.1.1` | uplink check target |
| `WISP_NTFY_URL` | `https://ntfy.sh` | ntfy base URL |
| `WISP_DB` | `data/wisp.db` | DB location — leave it unless you want state elsewhere |
| `WISP_DASHBOARD_PIN` | — | seed the PIN on first run (else set it in the UI) |

> At fleet scale (thousands of devices) also raise the open-file limit on the daemon
> (`LimitNOFILE=65535` in the unit's `[Service]` block) so the bounded fan-out has headroom.
