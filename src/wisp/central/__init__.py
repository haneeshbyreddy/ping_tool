"""Central aggregation plane (Phase 10 Part A — skeleton).

A SEPARATE process from the edge: edges keep detecting + paging locally and ship events /
rollups / heartbeats here over HTTPS (edge-initiated, bearer-token auth for now). At this
stage central is just a mirror + a fleet read view — the multi-tenant store, id mapping,
cross-edge watchdog (Part B), and per-org dashboard/auth (Part C) build on this. The edge
stays SQLite + stdlib forever; only THIS store may graduate to Postgres later.
"""
