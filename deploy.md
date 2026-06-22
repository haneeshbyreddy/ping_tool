# Deploying WISP

How to ship and upgrade this monitor on a single always-on Linux box. Read alongside
`README.md` (what it is / how to run) and `CLAUDE.md` (invariants). The systemd units in
`deploy/` are the starting point; this doc is the production-grade version of them.

## Verdict: the code is right-sized, not bloated

For the record, so nobody "minimizes" it into a worse state later:

- **~2,300 lines of source** (non-test) for a topology-aware FSM monitor + alerting ladder +
  analytics + auth + dashboard API. That's lean.
- **~1,900 lines / 58 tests.** The right ratio — this is what makes upgrades safe. Don't cut it.
- **Near-zero dependency surface**: the dashboard/CLI/tests are pure stdlib; only the daemon
  needs `icmplib` + `httpx`. For an on-prem appliance that's an asset, keep it.

The layering (pure `MonitorEngine` with no I/O, DB-derived escalations, in-process hot reload,
watchdog-watches-daemon) is hard-won correctness. **Do not flatten it to feel "minimal"** — that
trades testability for a smaller tree and re-grows the structure six months later.

The only real weight is `apps/dashboard/static/vendor/tailwind.js` (~419 KB, the Tailwind Play
CDN runtime that JITs CSS in the browser) — ~95% of non-test bytes. It loads once over the LAN,
so it's cosmetic, not a perf problem. Precompiling it to a ~15 KB static stylesheet is the only
"minimization" with real payoff, and it costs a Node build step. Leave it until it annoys you.

## Why native systemd, not Docker

This is a **network monitor**: it wants to sit on the host's real network stack. In a container
you're forced into `network_mode: host` + `cap-add=NET_RAW` (or the ping-group sysctl on both
host and container) just to ping correctly — at which point the container buys almost no isolation
while adding a moving part. Containers win for fleets and noisy-neighbour isolation; this is one
appliance doing one job. A directory install under systemd is simpler and more debuggable.

A single PyInstaller/zipapp binary also fights us here: SQLite migrations, vendored static assets,
and a raw-socket C-ish dep (`icmplib`) make a directory install simpler than a frozen blob. Save
binaries for boxes you can't SSH into.

## Production layout (state leaves the release dir)

The single most important production change: **keep operator data out of the code directory** so
an upgrade physically cannot touch it.

```
/opt/wisp/
├── releases/<git-tag>/      # each unpacked release (code + its .venv)
└── current  ->  releases/<git-tag>/   # atomic symlink the services point at

/var/lib/wisp/               # STATE — never touched by a release
├── wisp.db (+ wal/shm)
└── session_secret           # 0600
```

Set `WISP_DB_PATH=/var/lib/wisp/wisp.db` (and keep the session secret under `/var/lib/wisp`).
Then "redeploy by swapping the folder" can never endanger the DB. (See the memory note: never
wipe the DB.)

## Shipping: private release tarball + scp (no public host)

Chosen because the box has internet (deps are easy) but the **source must stay private** — no git
hosting, nothing to leak. Flow:

1. **Build on your machine** from a clean checkout → `wisp-<tag>.tar.gz`.
2. **`scp` it to the box** — nothing is hosted anywhere.
3. **`install.sh` on the box**: unpack to `releases/<tag>/`, `uv sync` (pulls the two deps from
   PyPI — that's what the internet is for), flip `current` → new release, `systemctl restart`.
4. **Rollback** = flip `current` back to the previous release and restart.

Upgrades are then just: build → scp → install. Schema upgrades ride along automatically — the
migration runner tracks `schema_migrations` and applies `migrations/000N_*.sql` forward on start.

## On "don't leak my code" — be honest about the tiers

If someone else has root on the box, Python source is **not** truly protected. Pick your tier:

- **Your own appliance** (you control root): ship plain `.py`. The private tarball already solves
  "leak" — nothing is hosted. Don't waste effort on obfuscation. ← most likely you.
- **Customer controls the box, you want a speed bump:** ship **bytecode-only** (compile to `.pyc`,
  delete the `.py`). Stops a casual `cat`, but `.pyc` decompiles back to near-source in seconds
  (`decompyle3`/`uncompyle6`). A lock on a screen door.
- **You think bytecode = protection:** it doesn't, and neither does PyInstaller (unpacks
  trivially). Real protection is architectural — keep the secret-sauce logic server-side and ship
  a thin client — not obfuscation.

## Dependency pinning (don't get surprised on a remote box)

`requirements.txt` currently uses `>=` ranges (`icmplib>=3.0`), which can pull a breaking major
onto a box you can't easily debug. Use **`uv` with a lockfile** (`uv.lock`): reproducible, fast,
manages the venv. `uv sync` on the box installs exactly what you tested.

## First-time install on the box

```bash
# one-time host prep
sudo useradd --system --home /var/lib/wisp --shell /usr/sbin/nologin wisp
sudo mkdir -p /opt/wisp/releases /var/lib/wisp
sudo chown -R wisp:wisp /var/lib/wisp

# unprivileged ICMP (no root / no cap_net_raw needed for the daemon)
echo 'net.ipv4.ping_group_range=0 2147483647' | sudo tee /etc/sysctl.d/99-wisp-ping.conf
sudo sysctl --system

# deploy the first release (see install.sh), then:
sudo cp /opt/wisp/current/deploy/wisp-*.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now wisp-monitor wisp-dashboard
```

The dashboard is plain HTTP + a shared PIN — **keep it on the office LAN, off the public
internet** (see `plan.md` §8.2).

## Upgrades

```bash
# on your machine
scripts/build-release.sh v1.2.0           # -> dist/wisp-v1.2.0.tar.gz
scp dist/wisp-v1.2.0.tar.gz box:/tmp/

# on the box
sudo scripts/install.sh /tmp/wisp-v1.2.0.tar.gz   # unpack, uv sync, flip symlink, restart
```

`Restart=always` in the units covers crashes. Device-set changes (add/remove nodes from the UI)
hot-reload in-process — no restart. **Config tunable (`WISP_*`) changes need a daemon restart.**

## TODO to make this real (scaffolding not yet built)

- [ ] `WISP_DB_PATH` support in `config.py` (default `/var/lib/wisp/wisp.db`, dev falls back to `data/`)
- [ ] `uv.lock` pinning `icmplib` + `httpx` + transitive deps
- [ ] `scripts/build-release.sh` — clean checkout → versioned tarball (optional `--bytecode`)
- [ ] `scripts/install.sh` — atomic `releases/<tag>` + `current` symlink swap, migrate, restart
- [ ] Update `deploy/wisp-*.service` to point at `/opt/wisp/current` + `/var/lib/wisp` state
- [ ] Fix doc drift (below)

## Doc drift to fix (maintenance tax that compounds)

`CLAUDE.md` is authoritative; reconcile these against it:

- systemd unit comments say the daemon **"re-execs in place"** — it's actually **in-process hot
  reload, no `os.execv`**.
- `README.md` says Settings values are DB-editable and **"override the env var"** — config is
  **env-var only, no DB settings layer**.
- `README.md` default port is `8000`; `run.sh` defaults to `8080`. Pick one.
