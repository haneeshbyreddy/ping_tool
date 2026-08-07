# Disaster recovery

Five ISPs run on this. Read the first section now, not on the day you need it.

---

## What is actually at risk

The code is on GitHub. **The following is not, and exists on exactly one disk:**

| thing | size | what losing it costs |
|---|---|---|
| `data/central.db` | 33 MB | every org, user, device, customer location, cable route, drop record |
| `data/secret.key` | 44 B | **every stored device web-UI password becomes permanently unreadable** |
| `data/central_session_secret` | 32 B | everyone is logged out once (harmless) |
| `deploy/central.env` | 2.4 KB | WhatsApp token, GitHub token, server config |

The irreplaceable *content* is only **804 KB** — 4 orgs, 21 users, 87 devices, 213 customer
locations, 90 drops, 23 cable routes. Everything else in the DB (ping history, rollups,
alert log, proxy audit) is 97% of the bytes and regenerates itself within a poll cycle.

`secret.key` is the one people get wrong. It is 44 bytes. Restore the database without it
and the credential vault still has all its rows — they just decode to nothing, silently.

---

## What is set up right now

**Local daily snapshots.** `wisp-backup.timer` fires at 02:30 UTC (08:00 IST), plus 5 min
after every boot, and keeps 14 bundles in `data/backups/`. Each bundle is ~4 MB and holds
the whole DB (vacuumed + integrity-checked), all four secrets, and a plain-text SQL dump of
the config/customer tables.

```bash
systemctl list-timers wisp-backup.timer     # when it last ran / runs next
.venv/bin/python tools/backup.py --list     # what is on disk
.venv/bin/python tools/backup.py            # take one right now
.venv/bin/python tools/backup.py --verify   # prove the newest one restores
```

`--verify` unpacks the bundle, checks the sha256, runs `PRAGMA integrity_check` and counts
every table. Run it after any schema change. A backup nothing has read back is a claim,
not a fact.

> **THE GAP, stated plainly:** these snapshots are on the same disk as the thing they are
> protecting. They cover a bad deploy, a bad migration, a fat-finger delete and DB
> corruption — which is most real data loss. They do **not** cover the VM being deleted,
> the disk being lost, or the region going down. See *Closing the gap* below; it is one
> command.

---

## Do this once, today, and it takes two minutes

Copy the secrets somewhere off this box. They are 2.5 KB and they are the half you cannot
regenerate:

```bash
cd ~/ping_tool
tar czf - data/secret.key data/central_session_secret deploy/central.env | base64 -w0
```

Paste that string into your password manager as one secure note. To restore it:

```bash
echo '<the string>' | base64 -d | tar xzf - -C ~/ping_tool
```

That alone turns "we lost everything" into "we lost the history" on the worst day.

---

## Restore onto a fresh machine

Assumes a clean Debian VM.

```bash
# 1. code
git clone git@github.com:haneeshbyreddy/ping_tool.git && cd ping_tool
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. data + secrets, from a bundle
mkdir -p data && tar xzf wisp-backup-YYYYmmdd-HHMMSS.tar.gz -C /tmp/restore
gunzip -c /tmp/restore/central.db.gz > data/central.db
cp /tmp/restore/secrets/secret.key data/
cp /tmp/restore/secrets/central_session_secret data/
cp /tmp/restore/secrets/central.env deploy/
chmod 600 data/secret.key data/central_session_secret deploy/central.env

# 3. sanity-check BEFORE starting
.venv/bin/python -c "import sqlite3;print(sqlite3.connect('data/central.db').execute('PRAGMA integrity_check').fetchone()[0])"
.venv/bin/python -c "import sqlite3;print(sqlite3.connect('data/central.db').execute('SELECT COUNT(*) FROM org_devices').fetchone()[0],'devices')"

# 4. service
sudo cp deploy/wisp-central.service deploy/wisp-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now wisp-central wisp-backup.timer
```

Then point DNS (`hansanet.in`) at the new box and let Caddy issue a certificate. **The edge
probes need no change** — they dial central by URL, so they reconnect on their own within
one poll interval.

**If you only have `precious.sql`** (the config/customer half, no binary DB): start central
once against an empty DB so it builds the current schema, stop it, then
`sqlite3 data/central.db < precious.sql`. This is the path that still works when the bundle
is older than the running code.

---

## Closing the gap: offsite

Not set up yet, by choice. When you want it, the bundles are 4 MB and already sitting in
`data/backups/` — any one of these finishes the job:

- **Private GitHub repo.** Your SSH key already works, so there is nothing new to create.
  Encrypt first (`age` or `gpg`) — the bundles hold subscriber names and phone numbers.
- **GCS bucket.** `gsutil` is already installed, but this VM's service account is
  `devstorage.read_only`, so it needs a one-time scope change (VM stop → set scopes →
  start) or a service-account key. About ₹2/month afterwards.
- **GCP disk snapshots.** A snapshot schedule in the console, no code at all. Coarser than
  the bundles but it protects the whole machine.

---

## Health checks worth knowing

```bash
systemctl status wisp-central                       # should be active, Restart=always
df -h /                                             # keep under ~85%
.venv/bin/python tools/prune_releases.py            # what release cache would be freed
sudo journalctl --disk-usage                        # capped at 200 MB
.venv/bin/python tools/backup.py --verify           # the one that actually matters
```
