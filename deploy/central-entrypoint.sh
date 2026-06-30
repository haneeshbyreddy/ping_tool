#!/bin/sh
# Central container entrypoint — one image, two roles (the server + the provisioning CLI).
#
#   central-entrypoint serve [--host H] [--port P]        # the ingest + dashboard server (default)
#   central-entrypoint admin <subcommand> [...]           # wisp.central.admin (bootstrap accounts,
#                                                         #   publish releases, drive rollouts)
#   central-entrypoint <anything-else...>                 # run verbatim (debugging)
#
# Examples:
#   docker exec -it wisp-central central-entrypoint admin create-superadmin --username you
#   docker run --rm -e WISP_CENTRAL_DB=/data/central.db -v wisp-central-data:/data \
#       wisp-central central-entrypoint admin list-users
set -e

case "${1:-serve}" in
  serve)
    shift 2>/dev/null || true
    exec python /app/apps/central/main.py "$@"
    ;;
  admin)
    shift
    exec python -m wisp.central.admin "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
