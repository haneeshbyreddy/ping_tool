from __future__ import annotations

from datetime import datetime, timezone

from wisp.central import onuroster, radius, radius_profiles
from wisp.central.api.common import olt_liveness
from wisp.central.webmacs import normalise_mac
from wisp.config import Config
from wisp.egress.notifiers import _display_zone

NET_STATES = ("online", "dark", "frozen", "stale", "unlinked")

REASONS = {
    "no_mac": "no router MAC recorded in the billing panel",
    "mac_unseen": "their router's MAC hasn't been seen behind any ONU",
    "mac_unresolved": "their router's MAC shows on the network, but it can't be "
                      "pinned to one ONU unambiguously",
    "gone": "they aren't in the panel's latest customer list, so there is "
            "nothing to match them to",
}


def _date_formats(store, org: str) -> dict[int, str]:
    profiles = radius_profiles.ProfileSet.build(store.list_radius_profiles(org))
    out: dict[int, str] = {}
    for account in store.org_radius_accounts(org, enabled_only=True):
        prof = profiles.resolve(org, str(account.get("profile") or ""))
        out[int(account["id"])] = prof.date_format if prof else ""
    return out


def collect(store, cfg: Config, org: str, now: datetime | None = None) -> dict:

    now = now or datetime.now(timezone.utc)
    today = now.astimezone(_display_zone(cfg.display_tz)).date()

    devs = store.list_org_devices(org)
    down_ids, state_stale = olt_liveness(devs, now, cfg.central_node_stale_s)
    dev_names = {d["id"]: d.get("name") for d in devs}

    onu_rows = store.org_onu_rows(org)
    fresh_ids = onuroster.fresh_device_ids(onu_rows, now)
    roster = {(r["device_id"], str(r["onu_key"])): r
              for r in onuroster.current_roster(onu_rows, now, stale_s=None)}

    links = {(l["account_id"], l["username"]): l
             for l in store.org_radius_links(org)}
    located = store.onu_place_macs(org, witness_only=False, located_only=True)

    mac_rows, _ = store.radius_link_inputs(org)
    slots_of_mac: dict[str, set] = {}
    for m in mac_rows:
        mac = normalise_mac(m.get("mac") or "")
        if mac:
            slots_of_mac.setdefault(mac, set()).add(
                (int(m["device_id"]), str(m["onu_key"])))

    formats = _date_formats(store, org)

    # ONE BOOK'S ABSENCE IS EVIDENCE; A SIBLING'S IS NOT. A customer missing from
    # a panel's latest read is KEPT and marked (far more likely a filtered read
    # than a cancelled subscriber) — but when the SAME username is CURRENT in
    # another panel of the org, the row is provably that customer's old copy, not
    # a fact about anybody. Hansa split one book across two Excell Media panels
    # and 482 of 1779 rows were exactly that: every one with a fresh twin
    # agreeing on name, MAC, mobile and account number, none a real departure.
    # It is a DISPLAY rule — the row stays in storage under the never-delete
    # rule — and it is COUNTED, because silently folding rows away is the failure
    # "kept, not silently equal to a current one" was written against.
    cust_rows = store.org_radius_customer_rows(org)
    current = {str(c["username"]) for c in cust_rows
               if c.get("seen_seq") == c.get("account_seq")}

    superseded = 0
    out: list[dict] = []
    for c in cust_rows:
        in_last_read = c.get("seen_seq") == c.get("account_seq")
        if not in_last_read and str(c["username"]) in current:
            superseded += 1
            continue
        account_id = int(c["account_id"])
        expiry_at = radius.parse_expiry(c.get("expiry"), formats.get(account_id, ""))
        row = {
            "username": c["username"], "name": c.get("name"),
            "mobile": c.get("mobile"), "status": c.get("status") or "unknown",
            "package": c.get("package"), "branch": c.get("branch"),
            "area": c.get("area"), "address": c.get("address"),
            "acno": c.get("acno"),
            "expiry": c.get("expiry"), "expiry_at": expiry_at,
            "days_left": radius.days_until(expiry_at, today),
            "account_id": account_id,
            "account_label": c.get("account_label") or c.get("account_profile"),
            "in_last_read": in_last_read,
            "last_seen_at": c.get("last_seen_at"),
            "net": "unlinked", "reason": None, "match_by": None,
            "device_id": None, "device_name": None, "onu_mac": None,
            "onu_label": None, "onu_name": None, "pon_port": None,
            "onu_id": None, "located": False, "dark_since": None,
        }
        link = links.get((account_id, c["username"]))
        if link is None:
            mac = normalise_mac(c.get("mac") or "")
            if not in_last_read:
                # Only a panel's LATEST read is fed to the linker
                # (`radius_customers_for_link`), so this row can never match
                # whatever its MAC does. Blaming the MAC here is a guess stated
                # as a finding: 340 of Hansa's rows read "can't be pinned to one
                # ONU unambiguously" about MACs that pin perfectly well.
                row["reason"] = "gone"
            elif not mac:
                row["reason"] = "no_mac"
            elif not slots_of_mac.get(mac):
                row["reason"] = "mac_unseen"
            else:
                row["reason"] = "mac_unresolved"
        else:
            did = int(link["device_id"])
            r = roster.get((did, str(link["onu_key"])))
            row.update({"match_by": link.get("match_by"),
                        "device_id": did, "device_name": dev_names.get(did)})
            if r is not None:
                mac = onuroster._norm_mac(r.get("serial"))
                row.update({
                    "onu_mac": mac or None,
                    "onu_label": r.get("label"),
                    "onu_name": r.get("name"),
                    "pon_port": r.get("pon_port"),
                    "onu_id": r.get("onu_id"),
                    "located": bool(mac and mac in located),
                })
            if did in down_ids or did in state_stale:
                row["net"] = "frozen"
            elif r is None or did not in fresh_ids:
                row["net"] = "stale"
            elif (r.get("state") or "") == "online":
                row["net"] = "online"
            else:
                row["net"] = "dark"
                row["dark_since"] = r.get("last_online_at")
        out.append(row)

    active = [r for r in out if r["status"] == "active"]
    counts = {
        "customers": len(out),
        "active": len(active),
        # Server-only by necessity: the SPA recounts every chip off the rows it
        # was sent, and these are the rows it was NOT sent.
        "superseded": superseded,
        "linked": sum(1 for r in out if r["net"] != "unlinked"),
        "paying_dark": sum(1 for r in active if r["net"] == "dark"),
        "paying_frozen": sum(1 for r in active if r["net"] == "frozen"),
    }
    out.sort(key=lambda r: ((r.get("name") or r["username"]).lower(),
                            r["username"], r["account_id"]))
    return {"customers": out, "counts": counts,
            "reasons": REASONS,
            "panels": store.org_radius_status(org)}
