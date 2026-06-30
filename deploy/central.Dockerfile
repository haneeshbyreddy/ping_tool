# WISP central server — the aggregation plane, containerized for cloud hosting (Phase 10 Part D).
#
# Why a container for CENTRAL but NOT the edge: the edge is a network monitor — it wants the
# host's real network stack for ICMP/SNMP (see deploy.md "Why native systemd, not Docker"). The
# central server does none of that; it's a plain HTTP aggregation/dashboard service, which is a
# clean fit for a container and for cloud hosting (GCE / Cloud Run / anything that runs OCI).
#
# Build (from the repo root):
#     docker build -f deploy/central.Dockerfile -t wisp-central:latest .
#
# Run (DB + session secret MUST live on a persistent volume, or you lose all data + log
# everyone out on restart — both live in the WISP_CENTRAL_DB directory):
#     docker run -d --name wisp-central -p 8443:8443 \
#       -e WISP_CENTRAL_TOKEN=$(openssl rand -hex 32) \
#       -v wisp-central-data:/data \
#       wisp-central:latest
#
# Bootstrap the first superadmin in the running container:
#     docker exec -it wisp-central central admin create-superadmin --username you
#
# Put it behind a TLS terminator in production (the server speaks plain HTTP to stay
# dependency-free). See deploy/central-gcloud.md for the full GCP playbook.
FROM python:3.12-slim

# httpx is the only runtime dep (fleet watchdog -> ntfy). No build toolchain needed.
COPY requirements-central.txt /tmp/requirements-central.txt
RUN pip install --no-cache-dir -r /tmp/requirements-central.txt && rm /tmp/requirements-central.txt

WORKDIR /app
# Only what central needs: the package source + the central entrypoint. (The central store
# self-manages its schema, so the edge migrations/ dir is intentionally NOT copied.)
COPY src/ ./src/
COPY apps/central/ ./apps/central/
COPY deploy/central-entrypoint.sh /usr/local/bin/central-entrypoint
RUN chmod +x /usr/local/bin/central-entrypoint

# Zero-install: the package lives under /app/src. apps/central/main.py adds this itself, but
# exporting it lets `central admin ...` (the provisioning CLI) resolve too.
ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    WISP_CENTRAL_DB=/data/central.db \
    WISP_CENTRAL_BIND=0.0.0.0 \
    WISP_CENTRAL_PORT=8443

# Persistent state: the SQLite DB (+ wal/shm) AND the central_session_secret file both live
# here. Mount a real volume over it in production.
RUN install -d -o 10001 -g 10001 /data
VOLUME ["/data"]

# Run unprivileged — central needs no special capabilities.
USER 10001:10001
EXPOSE 8443

# Liveness/readiness off the unauthenticated /healthz endpoint (no curl in slim; use stdlib).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('WISP_CENTRAL_PORT','8443')+'/healthz',timeout=4).status==200 else 1)"

ENTRYPOINT ["central-entrypoint"]
CMD ["serve"]
