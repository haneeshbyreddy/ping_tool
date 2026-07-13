"""Org-level and superadmin routes: org CRUD, server-wide settings, system
stats, coverage overview, test alerts."""
from __future__ import annotations

from wisp.central import inventory, sysinfo
from wisp.central.api.common import (DENIED, body_org_write, public_user,
                                     reader_or_401, superadmin_or_403)


def healthz(h, qs):
    h._reply(200, {"ok": True, "counts": h.store.counts()})


def me(h, qs):
    user = h._user()
    if not user:
        h._reply(401, {"error": "unauthorized"})
        return
    h._reply(200, {"user": public_user(user, h.store),
                   "channels": {"central": h.cfg.central_ntfy_topic}})


def system(h, qs):
    if not superadmin_or_403(h):
        return
    doc = sysinfo.snapshot(h.cfg.central_db)
    # Monitor-the-monitor: a dead release mirror stalls fleet
    # self-updates, so its health rides the superadmin box-stats card.
    doc["release_sync"] = h.store.release_sync_status()
    releases = h.store.list_releases()
    doc["latest_release"] = releases[0]["version"] if releases else None
    h._reply(200, doc)


def admin_overview(h, qs):
    if not superadmin_or_403(h):
        return
    h._reply(200, h.store.admin_overview())


def admin_settings(h, qs):
    if not superadmin_or_403(h):
        return
    h._reply(200, {"google_maps_key": h.store.get_setting("google_maps_key")})


def list_orgs(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = h._scope_org(user, qs)
    orgs = h.store.orgs()
    if org:
        orgs = [o for o in orgs if o["org_id"] == org]
    # the ONE superadmin-pasted Google Maps key rides every org
    # row, so each org's Map view lights up without its own key
    gkey = h.store.get_setting("google_maps_key")
    for o in orgs:
        o["google_maps_key"] = gkey
    h._reply(200, {"orgs": orgs})


def create(h, user, body):
    if not user["is_superadmin"]:
        h._reply(403, {"error": "forbidden"})
        return
    org = inventory.clean_org_id(body.get("org_id"))
    if h.store.org_exists(org):
        h._reply(409, {"error": f"org {org!r} already exists"})
        return
    h.store.set_org(org, name=body.get("name"))
    h._reply(200, {"org_id": org})


def update(h, user, body):
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    map_region = body.get("map_region")
    if map_region is not None:
        map_region = str(map_region).strip().lower()[:64] or None
    h.store.set_org(org, name=body.get("name"), ntfy_topic=body.get("ntfy_topic"),
                    ntfy_topic_owner=body.get("ntfy_topic_owner"),
                    ntfy_topic_operator=body.get("ntfy_topic_operator"),
                    ntfy_topic_tech=body.get("ntfy_topic_tech"),
                    map_region=map_region)
    h._reply(200, {"ok": True})


def admin_settings_write(h, user, body):
    # server-wide, superadmin-only: the Google Maps key is pasted
    # ONCE here and served to every org (browser-exposed by design,
    # referrer-restricted — central never calls Google)
    if not user["is_superadmin"]:
        h._reply(403, {"error": "forbidden"})
        return
    google_key = body.get("google_maps_key")
    if google_key is not None:
        h.store.set_setting("google_maps_key",
                            str(google_key).strip()[:128])
    h._reply(200, {"ok": True})


def test_alert(h, user, body):
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    role = str(body.get("role") or "").strip().lower()
    if role not in ("owner", "operator", "tech"):
        h._reply(422, {"error": "role must be one of: owner, operator, tech"})
        return
    topic = h.store.org_role_topic(org, role)
    if not topic:
        h._reply(422, {"error": f"no {role} channel configured — set it in "
                                "Settings first"})
        return
    res = h.notifier.send(topic, "✅ WISP Central test alert",
                          f"This is a test alert for {org}'s {role} channel.", 3)
    h._reply(200, {"ok": res.ok, "detail": res.detail, "channel": h.notifier.channel,
                   "recipient": topic, "role": role})
