# Hosting the WISP central server on Google Cloud

This is the playbook for running the **central** aggregation server (`apps/central`) on GCP.
It is the *other* deployment from the on-prem edge: the edge stays native systemd on a box you
control (it needs the host network for ICMP/SNMP — see `deploy.md`); **central is a plain HTTP
service and belongs in the cloud.**

> Read `README.md` §"Central reporting" for what central *is*. This file is purely *how to host
> it on GCP*. The container is `deploy/central.Dockerfile`; the one-VM shape is
> `deploy/docker-compose.central.yml` + `deploy/Caddyfile`.

## The one thing that decides your architecture: central is SQLite

Central stores everything in **one SQLite file** (`WISP_CENTRAL_DB`, default `/data/central.db`)
behind a process-wide write lock, and writes its **session-signing secret** next to it
(`/data/central_session_secret`). Two consequences drive every choice below:

1. **State must live on a persistent disk that survives restarts and redeploys.** Lose `/data`
   and you lose the whole fleet history *and* log every operator out.
2. **Exactly one writer.** SQLite is single-writer; you cannot run 2+ central instances against
   the same file. So central is **scale-to-one**, not horizontally autoscaled. That is fine for
   a long time (one small VM aggregates a large fleet — events + hourly rollups, not raw polls),
   and when you outgrow it the documented upgrade is **Cloud SQL Postgres behind the same
   `CentralStore` surface** (the edge stays SQLite forever).

**Recommendation:** a single small **Compute Engine** VM (e2-small is plenty) with a persistent
disk and Caddy for automatic TLS. It maps cleanly onto SQLite, costs a few dollars a month, and
is trivial to reason about. The Cloud Run path is documented at the end with its caveats — it is
viable only pinned to one instance, and you still need somewhere durable for the DB.

---

## Path A — Compute Engine VM + Docker Compose (recommended)

### 1. DNS
Reserve a static external IP and point a hostname at it (Caddy needs a real domain to get a
Let's Encrypt cert):

```bash
gcloud compute addresses create wisp-central-ip --region=asia-south1
gcloud compute addresses describe wisp-central-ip --region=asia-south1 --format='value(address)'
# create an A record:  central.example.net -> <that IP>   (in your DNS provider / Cloud DNS)
```

### 2. The VM + firewall
```bash
gcloud compute instances create wisp-central \
  --zone=asia-south1-a --machine-type=e2-small \
  --image-family=debian-12 --image-project=debian-cloud \
  --address=wisp-central-ip \
  --tags=wisp-central --boot-disk-size=20GB

# open only 80 (ACME + redirect) and 443 (the service). NOT 8443 — that stays internal.
gcloud compute firewall-rules create wisp-central-web \
  --allow=tcp:80,tcp:443 --target-tags=wisp-central --direction=INGRESS
```

> **Do not expose 8443.** Central speaks plain HTTP; only Caddy (on the same host) should reach
> it. The compose file keeps 8443 on the internal network and never publishes it.

### 3. Install Docker + bring central up
SSH in (`gcloud compute ssh wisp-central --zone=asia-south1-a`), then:

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin git
sudo git clone https://github.com/haneeshbyreddy/ping_tool.git /opt/wisp && cd /opt/wisp

# the two required secrets/values (kept out of git):
sudo tee deploy/.env >/dev/null <<EOF
WISP_CENTRAL_TOKEN=$(openssl rand -hex 32)
CENTRAL_DOMAIN=central.example.net
ACME_EMAIL=you@example.net
EOF
sudo chmod 600 deploy/.env

sudo docker compose -f deploy/docker-compose.central.yml up -d --build
```

Caddy fetches the TLS cert automatically once DNS resolves. Verify:

```bash
curl -s https://central.example.net/healthz        # {"ok": true, "counts": {...}}
```

### 4. Bootstrap the first account
```bash
sudo docker compose -f deploy/docker-compose.central.yml exec central \
  central-entrypoint admin create-superadmin --username you
# then publish releases / drive rollouts (Part D) the same way:
sudo docker compose -f deploy/docker-compose.central.yml exec central \
  central-entrypoint admin publish-release --version 0.11.0 \
    --artifact linux-amd64 https://github.com/.../wisp-edge-linux-amd64 <sha256>
```

Log in at `https://central.example.net/`.

### 5. Point edges at it
On each edge box (in `/etc/wisp/edge.env`, or via `deploy/install-edge.sh`):

```ini
WISP_CENTRAL_URL=https://central.example.net
WISP_CENTRAL_TOKEN=<the same token from deploy/.env>
WISP_TENANT_ID=ispA
WISP_NODE_ID=edge-a1
```

> The token in `deploy/.env` (central) and on every edge **must match** — it is the shared
> ingest bearer. (mTLS per-node enrollment is the planned upgrade; the static token is the
> documented Part A/D stopgap.)

### 6. Persistence + backup
The compose file keeps `/data` in a named Docker volume (`central-data`) on the VM's boot disk,
so it survives `docker compose down/up` and reboots. Two hardening steps:

- **Put `/data` on a dedicated persistent disk** (so you can grow/snapshot it independently of
  the boot disk): create + attach a disk, mount it at e.g. `/mnt/wisp`, and change the volume in
  `docker-compose.central.yml` from `central-data:/data` to `/mnt/wisp:/data`.
- **Snapshot it on a schedule** — the DB is the entire fleet memory:
  ```bash
  gcloud compute disks snapshot wisp-central --zone=asia-south1-a   # or a snapshot schedule
  ```
  A consistent app-level copy any time: `docker compose ... exec central \
  sh -c 'sqlite3 /data/central.db ".backup /data/central-backup.db"'` (sqlite3 isn't in the
  slim image — simplest is a disk snapshot, which is crash-consistent for WAL SQLite).

### 7. Updates
```bash
cd /opt/wisp && sudo git pull
sudo docker compose -f deploy/docker-compose.central.yml up -d --build   # rebuild + restart
```
The `/data` volume is untouched by a rebuild, so the DB + sessions survive. (Central's schema is
self-applied on start.)

---

## Path B — Cloud Run (only if you accept the caveats)

Cloud Run is attractive (managed TLS, scale-to-zero, no VM to patch) but fights SQLite:

- **Ephemeral filesystem.** The container's disk is per-instance and discarded on stop — you
  **must** mount durable storage for `/data`, or every redeploy wipes the fleet history and the
  session secret. Use a **Cloud Run volume mount backed by a GCS bucket** (gen2) or a **Filestore
  (NFS) mount**. GCS-FUSE + SQLite is risky (file locking over FUSE) — Filestore is the safer of
  the two, but at that point a VM is simpler.
- **Pin to one instance.** SQLite is single-writer:
  `--min-instances=1 --max-instances=1 --concurrency=...`. No autoscaling.
- **The watchdog thread needs the instance alive.** With `--min-instances=1` the central process
  (and its fleet-watchdog thread) keeps running; never let it scale to zero or stale-node paging
  stops.

If you still want it:
```bash
# build + push to Artifact Registry
gcloud artifacts repositories create wisp --repository-format=docker --location=asia-south1
docker build -f deploy/central.Dockerfile \
  -t asia-south1-docker.pkg.dev/$PROJECT/wisp/central:latest .
docker push asia-south1-docker.pkg.dev/$PROJECT/wisp/central:latest

# store the ingest token as a secret
echo -n "$(openssl rand -hex 32)" | gcloud secrets create wisp-central-token --data-file=-

gcloud run deploy wisp-central \
  --image=asia-south1-docker.pkg.dev/$PROJECT/wisp/central:latest \
  --region=asia-south1 --port=8443 --allow-unauthenticated \
  --min-instances=1 --max-instances=1 \
  --set-secrets=WISP_CENTRAL_TOKEN=wisp-central-token:latest \
  --set-env-vars=WISP_CENTRAL_DB=/data/central.db
  # + a volume mount for /data (GCS/Filestore) — REQUIRED, or state is ephemeral.
```
Bootstrap accounts via `gcloud run jobs` (or a one-off `gcloud run deploy` exec) running
`central-entrypoint admin create-superadmin ...`. Cloud Run terminates TLS for you, so no Caddy.

**Honest take:** for a SQLite, single-writer, stateful service with a background watchdog,
**the VM is the right tool.** Reach for Cloud Run after you migrate central to Cloud SQL Postgres
— then it autoscales cleanly and Cloud Run shines.

---

## Security checklist (don't skip)

- **`WISP_CENTRAL_TOKEN` is mandatory in production.** Empty = unauthenticated ingest (the server
  warns loudly on boot). It is the shared edge↔central bearer; rotate it by updating `deploy/.env`
  + every edge's `edge.env`.
- **TLS always.** Caddy (Path A) or Cloud Run (Path B) terminates it; central itself is plain
  HTTP and must never face the internet directly.
- **`/data` perms.** The container runs as uid 10001; on a bind mount, `chown 10001:10001` the
  host dir. The session secret + DB are 0600-class secrets — back them up encrypted.
- **Firewall.** Only 80/443 inbound. Lock SSH to your IP / IAP.
- **Two auth planes stay separate** (by design): ingest = the bearer token (machines); the
  dashboard = per-user accounts. Don't hand the ingest token to humans as a login.
