# Deploying the WISP edge probe

How to run the edge probe on an always-on Linux box. Read alongside `README.md`
(what it is / how to run) and `CLAUDE.md` (invariants). The systemd unit in
`deploy/` is the starting point.

This covers the **edge box only** — a thin probe with no local database, PIN, or
dashboard (see `plan.md`). Device topology, team, alert routing, and outage
history all live on the **central server**'s dashboard, which is a separate deploy
(see README's "Central server + edge setup"; central is meant to run somewhere
always-available, e.g. a small GCP VM, not on the same box you're provisioning here).

## Why native systemd, not Docker

This is a **network monitor** — it wants the host's real network stack. In a container
you're forced into `network_mode: host` + the ping-group sysctl just to ping correctly,
at which point the container buys almost no isolation while adding a moving part. A
plain directory install under systemd is simpler and easier to debug.

## What runs

One long-lived process: **`wisp-monitor`** (`apps/daemon/main.py`) — the edge probe,
in central-brain mode. It needs the venv (`icmplib` + `httpx`) and the kernel ping
group enabled. It carries no state of its own beyond a lock file; nothing under
`data/` on this box needs backing up (that's central's job).

## Quick install (one script)

Once the code is on the box, a single idempotent script does all the OS setup, venv,
sysctl, and systemd wiring — and re-running it after a `git pull` upgrades:

```bash
# get the code to its final home (private repo → deploy key, or scp). Then:
cd /opt/wisp
sudo deploy/install.sh
```

It prints what to edit next (your central server's URL/token/tenant) and does
**not** clone the repo (that needs your credentials) or start the service for you
until you've set those values.

## Windows (native, no WSL)

Prefer Linux — it's what this tool targets, and unprivileged ICMP "just works."
But if the box must be Windows, `deploy/install.ps1` is the PowerShell sibling of
`install.sh`: venv → deps → a **Scheduled Task** that auto-starts at boot and
restarts on crash. Re-run after a `git pull` to upgrade.

```powershell
# From an ELEVATED PowerShell (Run as administrator), in the repo:
powershell -ExecutionPolicy Bypass -File deploy\install.ps1
# remove the task:   ... -File deploy\install.ps1 -Uninstall
```

Install Python from python.org first (tick **Add to PATH**). The script writes
`deploy\wisp-monitor-run.cmd` with placeholder central settings — edit it with
your real central URL/token/tenant, then `Start-ScheduledTask -TaskName WISP-Monitor`.

**The one Windows gotcha:** Windows has no unprivileged-ICMP path (no
`ping_group_range`, no datagram ICMP sockets), so icmplib falls back to **raw sockets,
which require Administrator**. That's why the task runs as **SYSTEM** — it has the
raw-socket right and starts before any user logs in. There's nothing to "enable" like
the Linux ping group. We use a Scheduled Task rather than NSSM so nothing has to be
downloaded onto a locked-down box; it gives the same auto-start + restart as the
systemd unit. Manage it with `Get-ScheduledTask WISP-Monitor` / `Restart-ScheduledTask`.

> Not verified on Windows hardware from this repo — smoke-test it on your box
> before trusting it.

## First-time install (manual walkthrough)

Install to `/opt/wisp` (what the systemd unit expects). Get the code there however you
like — `git clone` the private repo with a deploy key, or `scp` a copy across.

```bash
# 1. put the code at /opt/wisp (private repo via SSH deploy key, or scp)
sudo git clone git@github.com:haneeshbyreddy/ping_tool.git /opt/wisp
cd /opt/wisp

# 2. venv for the probe's two deps (system Python is PEP 668-locked — never install globally)
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# 3. let the probe send ICMP as a normal user (unprivileged ping sockets — no root, no cap_net_raw)
echo 'net.ipv4.ping_group_range=0 2147483647' | sudo tee /etc/sysctl.d/99-wisp-ping.conf
sudo sysctl --system

# 4. install the unit and set your central connection details
sudo cp deploy/wisp-monitor.service /etc/systemd/system/
sudo systemctl edit --full wisp-monitor    # set WISP_CENTRAL_URL / _TOKEN / WISP_TENANT_ID
sudo systemctl daemon-reload
sudo systemctl enable --now wisp-monitor
```

Check it came up:

```bash
systemctl status wisp-monitor
journalctl -u wisp-monitor -f          # live probe log — should show "reported N device(s)"
```

Then log into **central**'s dashboard and add your real devices + team from the UI.

> **Edit the unit before installing if your paths differ.** The shipped
> `deploy/wisp-monitor.service` assumes `/opt/wisp` and the venv at
> `/opt/wisp/.venv`. It runs as root by default; to run as an unprivileged user,
> create one and uncomment the `User=`/`Group=` lines (the ping-group sysctl above
> is what lets a non-root user ping).

## Upgrades

```bash
cd /opt/wisp
sudo git pull
. .venv/bin/activate && pip install -r requirements.txt   # only if requirements changed
sudo systemctl restart wisp-monitor
```

- **Device-set changes** (add/remove/reparent nodes in central's dashboard) apply
  within one poll cycle — the probe re-fetches its topology from central every
  cycle. No edge restart needed.
- **Config (`WISP_*`) changes** on the edge need a `systemctl restart wisp-monitor`.
- `Restart=always` in the unit covers crashes.

## Config (optional)

Every tunable is a `WISP_*` env var read once at startup (full list + defaults in
`src/wisp/config.py`, also summarized in `README.md`). Set them in the systemd
unit's `[Service]` block as `Environment=WISP_…=…` and restart. The common ones
beyond the required `WISP_CENTRAL_URL` / `WISP_CENTRAL_TOKEN` / `WISP_TENANT_ID`:

| Var | Default | Meaning |
|---|---|---|
| `WISP_POLL_INTERVAL_S` | `60` | steady-state seconds between polls (detection latency also depends on central's fast-confirm — see README) |
| `WISP_RETRY_INTERVAL_S` | `2` | fast-confirm: re-probe a suspect named by central every Ns; `0` disables |
| `WISP_PINGS_PER_POLL` / `_INFRA` | `5` / `2` | echoes per poll for leaf CPEs / for aggregation gear (gentle on tower control planes) |
| `WISP_MAX_INFLIGHT` | `256` | cap on concurrent probes — keeps a large fleet from exhausting file descriptors (raise `ulimit -n` too) |
| `WISP_CANARY_IP` | `1.1.1.1` | uplink check target |
| `WISP_NODE_ID` | hostname | this edge's identity within its tenant (shown on central's fleet view) |

> At fleet scale (thousands of devices) also raise the open-file limit on the probe
> (`LimitNOFILE=65535` in the unit's `[Service]` block) so the bounded fan-out has headroom.
