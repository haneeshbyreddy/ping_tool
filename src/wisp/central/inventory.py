from __future__ import annotations

import ipaddress
import re

# The ONE identity normalizer for a serial/MAC (see onuroster's own docstring on
# why it is not search_key). Imported rather than mirrored: a second spelling
# rule here would let one sticker become two reference points.
from wisp.central.onuroster import _norm_mac

DEVICE_TYPES = ("core", "router", "switch", "gateway", "OLT", "AP", "CPE", "backhaul")
# Passive plant: splitters, fiber distribution boxes, splice closures. They live in
# org_devices (parent chains, map pins, routes — all shared machinery), but they
# don't ping: no IP, no probe assignment, no FSM. Three choke points keep them away
# from the monitoring path — org_device_topology (engine + /edge/devices),
# node_expected_ips (no assignment), device_reliability (no uptime math).
PASSIVE_TYPES = ("splitter", "fdb", "closure")
# How many ways a passive splits the fibre. A CLOSED vocabulary, and deliberately
# only the three an ISP actually stocks: the ratio is not decoration, it is what
# the recorded-load bar and the cumulative split down a cascade are computed
# from, so a free-form number would let "1:7" or "1:100" produce arithmetic
# nobody can act on. Widening this is a one-line edit when a real box turns up.
SPLIT_RATIOS = (2, 4, 8)
SNMP_VERSIONS = ("2c",)

def _gpon_vendors(extra: set[str] | None = None) -> frozenset[str]:
    """Vendor names an OLT may be stamped with: the edge's BUILT-IN profiles plus
    whatever `gpon_profiles` rows the caller found for this org.

    The built-ins alone are not the vocabulary — profiles are DATA, and a row
    shadows a same-named built-in — so validating against them only made every
    device on a DB profile unsavable: badri_fiber's two Syrotech OLTs 422'd on
    ANY edit (a rename, a region, a PON type) because the form faithfully sends
    back the vendor already stored. Extras are passed in rather than read here:
    this module is pure validation and never touches the store."""
    from wisp.ingress.gpon import PROFILES
    return frozenset(PROFILES) | frozenset(
        v.lower() for v in (extra or ()) if v)

_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

class InventoryError(ValueError):
    pass

def _str(data: dict, key: str, *, required: bool = False, default=None):
    v = data.get(key)
    v = v.strip() if isinstance(v, str) else (None if v is None else str(v).strip())
    if required and not v:
        raise InventoryError(f"{key.replace('_', ' ')} is required")
    return v or default

def clean_device_payload(data: dict, *, parents: dict[int, int | None],
                         device_id: int | None,
                         registered_nodes: set[str] | None = None,
                         passive_ids: set[int] | None = None,
                         gpon_vendors: set[str] | None = None) -> dict:
    name = _str(data, "name", required=True)
    device_type = _str(data, "device_type")
    if device_type and device_type not in DEVICE_TYPES + PASSIVE_TYPES:
        raise InventoryError(
            f"device type must be one of: {', '.join(DEVICE_TYPES + PASSIVE_TYPES)}")
    passive = device_type in PASSIVE_TYPES
    if passive:
        # a splitter has no address — the empty string satisfies the NOT NULL
        # column and never reaches an edge (org_device_topology filters passives)
        ip_address = _str(data, "ip_address") or ""
        if ip_address:
            raise InventoryError(
                f"a {device_type} is passive plant — it has no IP address")
    else:
        ip_address = _str(data, "ip_address", required=True)
        try:
            ipaddress.ip_address(str(ip_address))
        except ValueError:
            raise InventoryError(f"'{ip_address}' is not a valid IP address")
    region = _str(data, "region")
    # free-form tags (Network page filtering) — cosmetic, never reach the
    # engine or the edge. Accepts a list or a comma-separated string; stored
    # comma-joined, deduped case-insensitively, order preserved.
    tags_raw = data.get("tags")
    if isinstance(tags_raw, str):
        parts = tags_raw.split(",")
    elif isinstance(tags_raw, (list, tuple)):
        parts = [str(t) for t in tags_raw]
    else:
        parts = []
    tags: list[str] = []
    seen: set[str] = set()
    for t in parts:
        t = t.strip()
        if not t:
            continue
        if len(t) > 32:
            raise InventoryError("a tag must be 32 characters or fewer")
        if t.lower() in seen:
            continue
        seen.add(t.lower())
        tags.append(t)
    if len(tags) > 8:
        raise InventoryError("at most 8 tags per device")

    parent_raw = data.get("parent_device_id")
    parent_id: int | None = None
    if parent_raw not in (None, "", "null"):
        try:
            parent_id = int(parent_raw)
        except (TypeError, ValueError):
            raise InventoryError("parent node is invalid")
        if parent_id not in parents:
            raise InventoryError("parent node does not exist")
        if parent_id == device_id:
            raise InventoryError("a node can't be its own parent")
        cur, seen = parent_id, set()
        while cur is not None:
            if cur == device_id:
                raise InventoryError("that parent would create a topology loop")
            if cur in seen:
                break
            seen.add(cur)
            cur = parents.get(cur)
        # a passive has no FSM state, so suppression through it is undefined —
        # monitored gear may not hang below plant (plant hangs below gear)
        if (not passive and passive_ids is not None and parent_id in passive_ids):
            raise InventoryError(
                "a monitored device can't sit under passive plant — "
                "parent it to the powered device above instead")

    assigned_node_id = _str(data, "assigned_node_id")
    if passive and assigned_node_id:
        raise InventoryError(f"a {device_type} is passive plant — nothing probes it")
    if (assigned_node_id and registered_nodes is not None
            and assigned_node_id not in registered_nodes):
        raise InventoryError("assigned wisp client does not exist")

    gpon_vendor = _str(data, "gpon_vendor")
    if gpon_vendor:
        gpon_vendor = gpon_vendor.lower()
        if device_type != "OLT":
            raise InventoryError("GPON vendor only applies to an OLT")
        known = _gpon_vendors(gpon_vendors)
        if gpon_vendor not in known:
            raise InventoryError(
                f"GPON vendor must be one of: {', '.join(sorted(known))}")

    # which PON a splitter/FDB serves — the fault localizer binds passives to
    # onu_optics rows through it (Phase D3); free-form, e.g. "0/6"
    pon_port = _str(data, "pon_port") if passive else None
    if pon_port and len(pon_port) > 32:
        raise InventoryError("PON port must be 32 characters or fewer")

    # how many ways this box splits (1:2 / 1:4 / 1:8). Passive-only: powered gear
    # doesn't split fibre, and a ratio there would feed the load bar nonsense.
    split_ratio = _split_ratio(data) if passive else None

    # How many ONUs fit on one of this OLT's PONs before it reads as full. EPON
    # tops out at 1:64 and GPON at 1:128, so ONE global default can only be right
    # for half a mixed fleet — a 1:128 box false-pages "at capacity" at 64.
    # OLT-only: the cap is judged per PON, and nothing else has PONs. NULL = the
    # global cfg.onu_pon_limit, which is why an unset box is not silently 64.
    onu_pon_limit = (_clean_onu_limit(data, "onu_pon_limit")
                     if device_type == "OLT" else None)

    return {"name": name, "ip_address": ip_address, "device_type": device_type,
            "region": region, "tags": ",".join(tags) or None,
            "parent_device_id": parent_id,
            "assigned_node_id": assigned_node_id, "gpon_vendor": gpon_vendor,
            "pon_port": pon_port, "split_ratio": split_ratio,
            "onu_pon_limit": onu_pon_limit}


def _split_ratio(data: dict) -> int | None:
    """The split ratio as a bare denominator (8 for a 1:8), or None when the
    operator hasn't recorded one. Absent and "not a splitter" both read as None:
    a closure that only splices has no ratio, and that is a fact, not a gap."""
    raw = data.get("split_ratio")
    if raw in (None, "", "null"):
        return None
    if isinstance(raw, str):
        # tolerate the way it is actually written down: "1:8", "1/8", "8"
        raw = raw.strip().replace("1:", "").replace("1/", "") or "0"
    try:
        ratio = int(raw)
    except (TypeError, ValueError):
        raise InventoryError("split ratio is invalid")
    if ratio not in SPLIT_RATIOS:
        raise InventoryError(
            "split ratio must be one of: "
            + ", ".join(f"1:{r}" for r in SPLIT_RATIOS))
    return ratio

def clean_location_payload(data: dict) -> dict:
    """Map pin for a device: both coordinates, or both null (= remove the pin)."""
    lat_raw, lng_raw = data.get("lat"), data.get("lng")
    if lat_raw in (None, "", "null") and lng_raw in (None, "", "null"):
        return {"lat": None, "lng": None}
    try:
        lat, lng = float(lat_raw), float(lng_raw)
    except (TypeError, ValueError):
        raise InventoryError("lat and lng must both be numbers (or both null to clear)")
    if not (-90.0 <= lat <= 90.0):
        raise InventoryError("lat must be between -90 and 90")
    if not (-180.0 <= lng <= 180.0):
        raise InventoryError("lng must be between -180 and 180")
    # ~1e-6° ≈ 0.1 m — anything longer is float noise from a drag event
    return {"lat": round(lat, 6), "lng": round(lng, 6)}

# How a field capture claims to know where it is. CLOSED, like every other
# vocabulary here: 'gps' is a fix the phone took while standing at the device,
# 'manual' is a point somebody nudged on the map because the fix was hopeless
# (dense canopy, indoor rack). The two must never render alike — a `manual`
# pin's accuracy is unknowable, which is different from bad.
PLACE_SOURCES = ("gps", "manual")

# Above this a fix is a cell-tower/wifi estimate, not a position. It is NOT a
# refusal — a worker under canopy still needs to record something, and blocking
# the save is how coordinates end up in a WhatsApp message instead of the DB.
# The UI demotes the primary button past it; the server only rejects the absurd.
GPS_ACCURACY_HINT_M = 25.0
_MAX_ACCURACY_M = 10_000.0


def clean_field_location_payload(data: dict) -> dict:
    """A placement taken in the field, with its provenance.

    Deliberately NOT `clean_location_payload` with extra keys: that function's
    contract includes both-null = DELETE THE PIN, and this payload arrives from
    the one role that may not remove plant from the map. Coordinates are
    REQUIRED here, so the delete branch is unreachable rather than merely
    unused — a worker-facing route should not be one missing UI guard away from
    clearing a surveyed fleet."""
    lat_raw, lng_raw = data.get("lat"), data.get("lng")
    if lat_raw in (None, "", "null") or lng_raw in (None, "", "null"):
        raise InventoryError("a field placement needs both lat and lng")
    loc = clean_location_payload(data)

    source = _str(data, "source") or "gps"
    if source not in PLACE_SOURCES:
        raise InventoryError("source must be one of: " + ", ".join(PLACE_SOURCES))

    acc_raw = data.get("accuracy_m")
    accuracy = None
    if acc_raw not in (None, "", "null"):
        try:
            accuracy = float(acc_raw)
        except (TypeError, ValueError):
            raise InventoryError("accuracy_m must be a number")
        if accuracy < 0 or accuracy > _MAX_ACCURACY_M:
            raise InventoryError("accuracy_m is out of range")
        accuracy = round(accuracy, 1)
    # A 'gps' claim with no accuracy figure is not a GPS claim — every browser
    # that can produce a fix produces `coords.accuracy` alongside it, so an
    # absent one means the number came from somewhere else. Downgrade rather
    # than reject: the coordinates are still worth keeping, just not as a
    # measurement.
    if source == "gps" and accuracy is None:
        source = "manual"

    return {"lat": loc["lat"], "lng": loc["lng"],
            "accuracy_m": accuracy, "source": source}


def clean_field_passive_payload(data: dict) -> dict:
    """Passive plant discovered in the field: a splitter/FDB/closure at a fix.

    A worker may create this and nothing else. What makes that safe is what is
    ABSENT rather than any check here: no IP, no probe, no parent, no SNMP. A
    passive is excluded from `org_device_topology`, so it joins no engine,
    rebuilds no fingerprint, and cannot re-page a fleet; billing skips it too.
    The parent link — the one field that would give it consequences — is the
    owner's job on the desktop, exactly as the plant record is meant to work."""
    name = _str(data, "name", required=True)
    if len(name) > 120:
        raise InventoryError("name is too long")

    device_type = _str(data, "device_type", required=True)
    if device_type not in PASSIVE_TYPES:
        raise InventoryError(
            "field-created plant must be one of: " + ", ".join(PASSIVE_TYPES))

    loc = clean_field_location_payload(data)

    region = _str(data, "region")
    if region and len(region) > 120:
        raise InventoryError("region is too long")
    pon_port = _str(data, "pon_port")
    if pon_port and len(pon_port) > 60:
        raise InventoryError("PON label is too long")

    return {"name": name, "device_type": device_type,
            "ip_address": "", "parent_device_id": None,
            "assigned_node_id": None, "region": region,
            "pon_port": pon_port or None,
            "split_ratio": _split_ratio(data),
            "lat": loc["lat"], "lng": loc["lng"],
            "accuracy_m": loc["accuracy_m"], "source": loc["source"]}


def _onu_label(data: dict) -> str | None:
    """A subscriber's operator-given name, UPPERCASED. None when blank.

    Operator's call (2026-07-29): an ONU's customer name always reads as caps,
    whatever case it was typed in. It is normalized on the WRITE path — here, the
    one function all three writers to `onu_places.label` share — rather than at
    render time, because the field survey, the reference-ONU dialog and the
    rename route are three entry points and a display-time `.toUpperCase()` would
    have to be remembered at every screen that ever names an ONU (the same
    forgetting that made a typed name invisible on the OLT page to begin with).
    Storing the canonical form also means SEARCH matches what the operator sees.

    Consequence worth stating: case typed in the field is discarded, which is
    what was asked for. The WALKED name (`onu_optics.name`) is NOT touched — that
    string belongs to the OLT, and restyling somebody else's data is how a
    dashboard starts disagreeing with the box a tech is logged into."""
    label = _str(data, "label")
    if label and len(label) > 120:
        raise InventoryError("label is too long")
    return label.upper() if label else None


def clean_field_onu_payload(data: dict) -> dict:
    """A subscriber's ONU located in the field, keyed on the sticker MAC.

    LOCATING IS NOT WITNESSING, and this payload carries no way to say otherwise
    — there is no `witness` key to set. Placing a reference ONU is the operator's
    claim that a subscriber's power is reliable, which nothing detects and which
    flips a PON mass-drop verdict from "fibre cut" to "area power cut"; a tech
    recording where a box physically sits is making no such claim. Letting one
    write express both is how a street's worth of geo-tags silently becomes a
    street's worth of witnesses.

    Identity normalization is `onuroster._norm_mac` — the same one
    `clean_onu_place_payload` uses, deliberately, since both write the same
    table and two spellings of one sticker must not become two rows."""
    mac = _norm_mac(_str(data, "mac", required=True))
    if not mac:
        raise InventoryError("a MAC is required")
    if len(mac) > 64:
        raise InventoryError("MAC is too long")
    loc = clean_field_location_payload(data)
    return {"mac": mac, "lat": loc["lat"], "lng": loc["lng"],
            "accuracy_m": loc["accuracy_m"], "source": loc["source"],
            "label": _onu_label(data)}


def clean_field_onu_name_payload(data: dict) -> dict:
    """The operator's own name for a located subscriber. Label only, no geometry.

    A BLANK label is allowed and means "clear it" — descriptive text, unlike a
    pin, can honestly be absent. Same `_norm_mac` identity as every other write
    to this table."""
    mac = _norm_mac(_str(data, "mac", required=True))
    if not mac:
        raise InventoryError("a MAC is required")
    if len(mac) > 64:
        raise InventoryError("MAC is too long")
    return {"mac": mac, "label": _onu_label(data)}


def clean_onu_place_payload(data: dict) -> dict:
    """A reference ONU's map placement, keyed on the MAC off its sticker.

    Identity is normalized HERE and nowhere else on the write path, so two
    spellings of one sticker can never become two witnesses. `onuroster._norm_mac`
    is the right normalizer of the three: identity, not the punctuation-blind
    search key (which would collapse genuinely different serials) and not the
    weboptics match key.

    Both coordinates null means REMOVE — the table is sparse, so an unplaced
    reference point is the absence of a row, never a row with empty columns."""
    mac = _norm_mac(_str(data, "mac", required=True))
    if len(mac) > 64:
        raise InventoryError("MAC is too long")
    loc = clean_location_payload(data)
    notes = _str(data, "notes")
    if notes and len(notes) > 500:
        raise InventoryError("notes are too long")
    # Same uppercase rule as the field paths — one table, one spelling of a name,
    # or the desktop dialog and the handset would disagree about the same drop.
    return {"mac": mac, "lat": loc["lat"], "lng": loc["lng"],
            "label": _onu_label(data), "notes": notes}


MAX_DROPS_PER_WRITE = 512


def clean_onu_drops_payload(data: dict) -> dict:
    """Which passive box a set of subscriber ONUs takes its drop from.

    A BULK write by design: the question an operator answers is "which customers
    hang off this splitter", asked once per box, not once per subscriber. So the
    payload is {passive_id, macs[]} and `passive_id` null means DETACH the listed
    MACs — the table is sparse like onu_places, so "no splitter recorded" is the
    absence of a row rather than a row pointing nowhere.

    Identity is normalized HERE and nowhere else on this write path (the same
    single-choke-point rule reference points follow), or one sticker becomes two
    drops and a splitter over-counts its own load."""
    raw = data.get("macs")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise InventoryError("macs must be a list")
    macs: list[str] = []
    seen: set[str] = set()
    for m in raw:
        mac = _norm_mac(str(m))
        if not mac or mac in seen:
            continue
        if len(mac) > 64:
            raise InventoryError("MAC is too long")
        seen.add(mac)
        macs.append(mac)
    if not macs:
        raise InventoryError("no ONUs given")
    # A cap so one request can't rewrite a fleet's plant record in a single
    # mis-clicked "select all"; a PON tops out at 64 ONUs, so this is generous.
    if len(macs) > MAX_DROPS_PER_WRITE:
        raise InventoryError(
            f"at most {MAX_DROPS_PER_WRITE} ONUs per request")

    passive_raw = data.get("passive_id")
    passive_id: int | None = None
    if passive_raw not in (None, "", "null"):
        try:
            passive_id = int(passive_raw)
        except (TypeError, ValueError):
            raise InventoryError("serving splitter is invalid")
    return {"macs": macs, "passive_id": passive_id}


def clean_web_access_payload(data: dict) -> dict:
    """Web-UI proxy address override for a device. All three fields are optional
    and independent: a blank/absent IP means 'proxy the probe IP', a blank port
    means 'the scheme default', a blank scheme means 'infer from the port'. All
    blank clears the override. The IP (when given) must parse; the port must be
    1..65535; the scheme must be http or https."""
    ip_raw = _str(data, "web_ip") or ""
    web_ip = ip_raw.strip()
    if web_ip:
        try:
            ipaddress.ip_address(web_ip)
        except ValueError:
            raise InventoryError(f"'{web_ip}' is not a valid IP address")

    port_raw = data.get("web_port")
    web_port: int | None = None
    if port_raw not in (None, "", "null"):
        try:
            web_port = int(port_raw)
        except (TypeError, ValueError):
            raise InventoryError("web port must be a whole number")
        if not (1 <= web_port <= 65535):
            raise InventoryError("web port must be between 1 and 65535")

    scheme_raw = (_str(data, "web_scheme") or "").strip().lower()
    if scheme_raw and scheme_raw not in ("http", "https"):
        raise InventoryError("web scheme must be http or https")
    web_scheme = scheme_raw or None

    return {"web_ip": web_ip or None, "web_port": web_port, "web_scheme": web_scheme}


# The two ports the plain http/https "Connect" buttons already reach on the
# device's own IP — an override naming one of these on the same host reaches
# nowhere new.
_STD_WEB_PORTS = (80, 443)


def normalize_web_access(clean: dict, device_ip: str | None) -> dict:
    """Collapse a web-UI override that points nowhere the plain http/https buttons
    can't already reach, so a redundant entry is never stored.

    The override earns its keep ONLY when it names a genuinely distinct endpoint:
    a DIFFERENT host, or a NON-standard port on the same host. Re-typing the
    device's own IP on 80/443 (or a bare scheme) resolves to exactly what
    'Connect -> http/https' already does — storing it would gain nothing and,
    worse, collapse the http/https split button into a single pinned Connect
    (any override field set does that), stranding the tech on the wrong scheme
    if they guessed it. So we drop the redundant bits to NULL and keep the
    scheme fallback for the common case. Same-host-distinct-port keeps the port
    (+scheme) but drops the redundant IP, so a later re-parent can't pin a stale
    host. Takes the already-cleaned/validated payload from
    ``clean_web_access_payload``."""
    device_ip = (device_ip or "").strip()
    web_ip = clean.get("web_ip")
    web_port = clean.get("web_port")
    web_scheme = clean.get("web_scheme")
    distinct_ip = bool(web_ip) and web_ip != device_ip
    distinct_port = web_port is not None and web_port not in _STD_WEB_PORTS
    if distinct_ip:
        return {"web_ip": web_ip, "web_port": web_port, "web_scheme": web_scheme}
    if distinct_port:
        return {"web_ip": None, "web_port": web_port, "web_scheme": web_scheme}
    return {"web_ip": None, "web_port": None, "web_scheme": None}


ROUTE_MAX_WAYPOINTS = 200

# Per-link map colours: a CLOSED palette of names, never a free hex field.
#
# Two reasons it's names. (1) The map's loudest colours must stay the status
# tones — a free picker lets an operator paint a healthy link the same red as a
# down one, which fakes an alarm on the one screen that exists to show alarms.
# Every name here is deliberately clear of --destructive / --warning / --success
# / --primary, and the renderer only paints a link that ISN'T in trouble anyway.
# (2) The actual values live in index.css (--map-line-*), so they stay theme
# data rather than being frozen into DB rows the day someone picked them —
# the same argument as theme_overrides storing a sparse diff.
#
# These distinguish PARALLEL cables from each other; they encode nothing. Keep
# the vocabulary tiny.
LINK_COLORS = ("violet", "magenta", "teal", "lime", "indigo", "chalk")

# ONE colour vocabulary for the whole product, not one per feature. The map's
# link palette came first; TAG and PROBE colours (org_colors) draw from the same
# names, so a colour means the same thing wherever an operator meets it — and
# every argument above carries over unchanged, in particular that none of these
# can impersonate a status tone. Anything colour-coded later reuses this too.
PALETTE = LINK_COLORS

# What a colour is attached to. 'tag' keys on the tag text, 'node' on node_id —
# neither is a foreign key, deliberately: a tag exists only as text inside
# org_devices.tags, and a probe lives in node_tokens OR nodes (or both), so a
# colour that insisted on a real row would vanish on rotation or re-enrollment.
COLOR_KINDS = ("tag", "node")


def clean_color(raw) -> str | None:
    """A palette name, or None to clear. Free hex is refused — see PALETTE."""
    if raw in (None, "", "none", "null"):
        return None
    if raw in PALETTE:
        return raw
    raise InventoryError(f"colour must be one of: {', '.join(PALETTE)}")


def clean_color_key(kind: str, raw) -> str:
    """The thing being coloured. Bounded because it becomes a DB key."""
    if kind not in COLOR_KINDS:
        raise InventoryError("unknown colour kind")
    key = (raw or "").strip()
    if not key:
        raise InventoryError("nothing to colour")
    if len(key) > 64:
        raise InventoryError("name is too long")
    return key


def clean_route_payload(data: dict) -> dict:
    """Drawn cable path for a link: intermediate vertices only, parent→child order.

    An empty waypoint list clears the route. Endpoint devices are validated by the
    caller (the pair must be a real link in this org)."""
    try:
        child_id = int(data.get("child_id"))
        parent_id = int(data.get("parent_id"))
    except (TypeError, ValueError):
        raise InventoryError("child_id and parent_id are required")
    raw = data.get("waypoints")
    if raw in (None, "", "null"):
        raw = []
    if not isinstance(raw, list):
        raise InventoryError("waypoints must be a list of [lat, lng] pairs")
    if len(raw) > ROUTE_MAX_WAYPOINTS:
        raise InventoryError(f"a route can hold at most {ROUTE_MAX_WAYPOINTS} waypoints")
    waypoints: list[list[float]] = []
    for pair in raw:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise InventoryError("each waypoint must be a [lat, lng] pair")
        try:
            lat, lng = float(pair[0]), float(pair[1])
        except (TypeError, ValueError):
            raise InventoryError("waypoint coordinates must be numbers")
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
            raise InventoryError("waypoint coordinates are out of range")
        waypoints.append([round(lat, 6), round(lng, 6)])
    return {"child_id": child_id, "parent_id": parent_id, "waypoints": waypoints}


def clean_link_style_payload(data: dict) -> dict:
    """A link's map styling: colour and/or bandwidth-label position.

    SPARSE — a key absent from the body means "leave it alone", so the colour
    picker and the label drag can write independently without either clobbering
    the other. An explicit null clears. Endpoint devices are validated by the
    caller (the pair must be a real link in this org)."""
    try:
        child_id = int(data.get("child_id"))
        parent_id = int(data.get("parent_id"))
    except (TypeError, ValueError):
        raise InventoryError("child_id and parent_id are required")
    fields: dict = {}
    if "color" in data:
        raw = data.get("color")
        if raw in (None, "", "null", "default"):
            fields["color"] = None
        elif raw in LINK_COLORS:
            fields["color"] = raw
        else:
            raise InventoryError(f"colour must be one of: {', '.join(LINK_COLORS)}")
    if "label_pos" in data:
        raw = data.get("label_pos")
        if raw in (None, "", "null"):
            fields["label_pos"] = None
        else:
            try:
                pos = float(raw)
            except (TypeError, ValueError):
                raise InventoryError("label_pos must be a number between 0 and 1")
            if not (0.0 <= pos <= 1.0):
                raise InventoryError("label_pos must be between 0 and 1")
            # 4dp ≈ 10cm on a 1km span — finer than a label can be dragged, and
            # it keeps the row byte-stable so an idle drag isn't a write.
            fields["label_pos"] = round(pos, 4)
    if not fields:
        raise InventoryError("nothing to set — pass color and/or label_pos")
    return {"child_id": child_id, "parent_id": parent_id, "fields": fields}

def clean_region_name(raw) -> str:
    name = str(raw).strip() if raw is not None else ""
    if not name:
        raise InventoryError("region name is required")
    if len(name) > 64:
        raise InventoryError("region name must be 64 characters or fewer")
    return name

def clean_backup_link(child_id: int, parent_id: int, *,
                      parents: dict[int, int | None],
                      backups: dict[int, set[int]]) -> None:
    if child_id not in parents:
        raise InventoryError("node not found")
    if parent_id not in parents:
        raise InventoryError("backup parent does not exist")
    if parent_id == child_id:
        raise InventoryError("a node can't be its own backup parent")
    if parents.get(child_id) == parent_id:
        raise InventoryError("that node is already the primary parent")
    if parent_id in backups.get(child_id, set()):
        raise InventoryError("that backup link already exists")
    edges_of: dict[int, set[int]] = {}
    for cid, pid in parents.items():
        if pid is not None:
            edges_of.setdefault(cid, set()).add(pid)
    for cid, pids in backups.items():
        edges_of.setdefault(cid, set()).update(pids)
    stack, seen = [parent_id], set()
    while stack:
        cur = stack.pop()
        if cur == child_id:
            raise InventoryError("that backup link would create a topology loop")
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(edges_of.get(cur, ()))

def clean_peer_link(a_id: int, b_id: int, *,
                    parents: dict[int, int | None],
                    backups: dict[int, set[int]],
                    peers: dict[int, set[int]]) -> None:
    """Validate a switch-to-switch cross-link.

    Deliberately has NO cycle check — unlike a backup edge, a peer link is not a
    dependency, and a ring of cross-linked switches IS a cycle. That's the whole
    reason peers are a separate kind: forcing them through clean_backup_link
    would reject exactly the topology an operator is trying to record.

    A pair already joined by a dependency edge is refused in either direction: the
    two would render as two lines between the same pins and leave the port
    bindings ambiguous about which link they belong to.
    """
    if a_id not in parents:
        raise InventoryError("node not found")
    if b_id not in parents:
        raise InventoryError("that device does not exist")
    if a_id == b_id:
        raise InventoryError("a device can't cross-link to itself")
    if parents.get(a_id) == b_id or parents.get(b_id) == a_id:
        raise InventoryError("those devices are already linked as parent and child")
    if b_id in backups.get(a_id, set()) or a_id in backups.get(b_id, set()):
        raise InventoryError("those devices are already linked as a backup uplink")
    if b_id in peers.get(a_id, set()):
        raise InventoryError("that cross-link already exists")

BW_DIRECTIONS = ("in", "out", "either", "total")

def _clean_bw_bound(data: dict, key: str) -> float | None:
    raw = data.get(key)
    if raw in (None, "", "null"):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise InventoryError(f"{key} must be a number")
    if value <= 0:
        raise InventoryError(f"{key} must be positive")
    return value

def clean_port_bandwidth_payload(data: dict) -> dict:
    threshold = _clean_bw_bound(data, "threshold_mbps")
    max_mbps = _clean_bw_bound(data, "max_mbps")
    if threshold is not None and max_mbps is not None and max_mbps <= threshold:
        raise InventoryError("max_mbps must be greater than threshold_mbps")
    direction = (str(data.get("direction") or "either")).strip().lower()
    if direction not in BW_DIRECTIONS:
        raise InventoryError(f"direction must be one of: {', '.join(BW_DIRECTIONS)}")
    return {"threshold_mbps": threshold, "max_mbps": max_mbps, "direction": direction}

def _clean_dbm(data: dict, key: str) -> float | None:
    raw = data.get(key)
    if raw in (None, "", "null"):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise InventoryError(f"{key} must be a number")
    if value > 0:
        raise InventoryError(f"{key} must be negative (dBm, e.g. -27)")
    return value

def _clean_onu_limit(data: dict, key: str) -> int | None:
    raw = data.get(key)
    if raw in (None, "", "null"):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise InventoryError(f"{key} must be a whole number")
    if value < 1:
        raise InventoryError(f"{key} must be at least 1")
    return value

def clean_optical_thresholds(data: dict) -> dict:
    warn = _clean_dbm(data, "warn_dbm")
    crit = _clean_dbm(data, "crit_dbm")
    if warn is not None and crit is not None and crit > warn:
        raise InventoryError("crit_dbm must be lower (weaker) than warn_dbm")
    # per-OLT ONU-per-PON cap override (NULL = the global cfg.onu_pon_limit)
    return {"warn_dbm": warn, "crit_dbm": crit,
            "onu_pon_limit": _clean_onu_limit(data, "onu_pon_limit")}

def clean_ack_until(data: dict) -> str | None:
    from datetime import datetime, timedelta, timezone
    raw = data.get("until")
    if raw in (None, "", "null", "clear") and data.get("hours") in (None, "", "null"):
        return None
    hours = data.get("hours")
    if hours not in (None, "", "null"):
        try:
            h = float(hours)
        except (TypeError, ValueError):
            raise InventoryError("hours must be a number")
        if h <= 0:
            raise InventoryError("hours must be positive")
        return (datetime.now(timezone.utc) + timedelta(hours=h)).isoformat(timespec="seconds")
    try:
        return datetime.fromisoformat(str(raw)).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        raise InventoryError("until must be an ISO8601 timestamp")

def clean_snmp_payload(data: dict) -> dict:
    enabled = 0 if str(data.get("snmp_enabled", 0)) in ("0", "false", "False", "", "None") else 1
    version = (str(data.get("snmp_version") or "2c")).strip().lower()
    if version not in SNMP_VERSIONS:
        raise InventoryError(f"SNMP version must be one of: {', '.join(SNMP_VERSIONS)}")
    community = _str(data, "snmp_community")
    if enabled and not community:
        raise InventoryError("an SNMP community is required to enable SNMP")
    try:
        port = int(data.get("snmp_port") or 161)
    except (TypeError, ValueError):
        raise InventoryError("SNMP port must be a number")
    if not (1 <= port <= 65535):
        raise InventoryError("SNMP port must be 1–65535")
    return {"snmp_enabled": enabled, "snmp_version": version,
            "snmp_community": community, "snmp_port": port}

_OID_RE = re.compile(r"^\d+(\.\d+){0,127}$")

# Diagnostic walk bounds: a full enterprise-tree walk of a loaded OLT can run to
# hundreds of thousands of varbinds — the cap keeps one click from turning into a
# multi-megabyte upload and a minutes-long UDP storm inside the customer's network.
WALK_DEFAULT_MAX_VARBINDS = 2000
WALK_CAP_MAX_VARBINDS = 20000

def clean_oid(raw, *, default: str | None = None, field: str = "oid") -> str:
    oid = str(raw or "").strip().strip(".")
    if not oid and default:
        return default
    if not _OID_RE.match(oid):
        raise InventoryError(
            f"{field} must be a dotted numeric OID, e.g. 1.3.6.1.4.1")
    return oid

def clean_walk_payload(data: dict) -> dict:
    root_oid = clean_oid(data.get("root_oid"), default="1.3.6.1", field="root_oid")
    raw_max = data.get("max_varbinds")
    if raw_max in (None, "", "null"):
        max_varbinds = WALK_DEFAULT_MAX_VARBINDS
    else:
        try:
            max_varbinds = int(raw_max)
        except (TypeError, ValueError):
            raise InventoryError("max_varbinds must be a number")
        if max_varbinds <= 0:
            raise InventoryError("max_varbinds must be positive")
    return {"root_oid": root_oid,
            "max_varbinds": min(max_varbinds, WALK_CAP_MAX_VARBINDS)}

# Subsystems an operator can mark "not supported by this hardware" — mirrors
# store.SNMP_SUBSYSTEMS (the edge's snmp_status vocabulary).
CAPABILITY_SUBSYSTEMS = ("health", "ports", "optics")

def clean_capability_payload(data: dict) -> dict:
    try:
        device_id = int(data.get("device_id"))
    except (TypeError, ValueError):
        raise InventoryError("device_id required")
    subsystem = str(data.get("subsystem") or "").strip().lower()
    if subsystem not in CAPABILITY_SUBSYSTEMS:
        raise InventoryError(
            f"subsystem must be one of: {', '.join(CAPABILITY_SUBSYSTEMS)}")
    supported = str(data.get("supported", 1)) not in ("0", "false", "False", "", "None")
    note = str(data.get("note") or "").strip()[:200] or None
    return {"device_id": device_id, "subsystem": subsystem,
            "supported": supported, "note": note}

# The closed decode/select vocabulary the edge's profile interpreter understands
# (ingress/health.py). Deliberately tiny — a vendor encoding this can't express is
# the rare case that still warrants edge code, not a reason to grow this into a DSL.
PROFILE_METRICS = ("cpu_pct", "mem_pct", "mem_used_bytes", "mem_total_bytes", "temp_c")
PROFILE_DECODES = ("as_is", "div10", "div100", "signed_div100")
PROFILE_SELECTS = ("first", "avg", "max", "sum")

def clean_profile_payload(data: dict) -> dict:
    name = _str(data, "name", required=True)
    if len(name) > 64:
        raise InventoryError("profile name must be 64 characters or fewer")
    match = clean_oid(data.get("match_sysobjectid"), field="match_sysobjectid")
    raw_metrics = data.get("metrics")
    if not isinstance(raw_metrics, dict) or not raw_metrics:
        raise InventoryError("metrics must map at least one metric to an OID")
    metrics: dict = {}
    for key, spec in raw_metrics.items():
        if key not in PROFILE_METRICS:
            raise InventoryError(
                f"unknown metric {key!r} — must be one of: {', '.join(PROFILE_METRICS)}")
        if not isinstance(spec, dict):
            raise InventoryError(f"metric {key!r} must be an object with an oid")
        oid = clean_oid(spec.get("oid"), field=f"{key}.oid")
        decode = (str(spec.get("decode") or "as_is")).strip().lower()
        if decode not in PROFILE_DECODES:
            raise InventoryError(
                f"{key}.decode must be one of: {', '.join(PROFILE_DECODES)}")
        select = (str(spec.get("select") or "first")).strip().lower()
        if select not in PROFILE_SELECTS:
            raise InventoryError(
                f"{key}.select must be one of: {', '.join(PROFILE_SELECTS)}")
        metrics[key] = {"oid": oid, "decode": decode, "select": select}
    enabled = str(data.get("enabled", 1)) not in ("0", "false", "False", "", "None")
    return {"name": name, "match_sysobjectid": match, "metrics": metrics,
            "enabled": enabled}

# The GPON counterpart — must mirror ingress/gpon.py's gpon_profile_from_dict
# vocabulary exactly (the edge revalidates and silently drops what it can't
# express; rejecting here is what gives the operator an error message instead).
GPON_PROFILE_OIDS = ("rx", "tx", "state", "distance", "serial", "name",
                     "ident_key", "ident_pon", "ident_onu", "ident_state",
                     "ident_distance", "ident_name")
GPON_PROFILE_STATES = ("online", "offline", "dying_gasp", "los", "unknown")
GPON_PON_INDEX_STRATEGIES = ("as_is", "first_segment")

def clean_gpon_profile_payload(data: dict) -> dict:
    name = _str(data, "name", required=True).lower()
    if len(name) > 64:
        raise InventoryError("profile name must be 64 characters or fewer")
    match = str(data.get("match_sysobjectid") or "").strip().strip(".")
    if match:
        match = clean_oid(match, field="match_sysobjectid")
    raw_oids = data.get("oids")
    if not isinstance(raw_oids, dict):
        raise InventoryError("oids must map ONU columns to OIDs")
    oids: dict = {}
    for key, val in raw_oids.items():
        if key not in GPON_PROFILE_OIDS:
            raise InventoryError(
                f"unknown oid field {key!r} — must be one of: {', '.join(GPON_PROFILE_OIDS)}")
        if str(val or "").strip():
            oids[key] = clean_oid(val, field=f"oids.{key}")
    if not oids:
        raise InventoryError("profile must map at least one OID")
    scales: dict = {}
    for key, val in (data.get("scales") or {}).items():
        if key not in ("rx", "tx", "distance"):
            raise InventoryError("scales apply only to rx, tx, distance")
        try:
            f = float(val)
        except (TypeError, ValueError):
            raise InventoryError(f"scales.{key} must be a number")
        if not 0 < f <= 1000:
            raise InventoryError(f"scales.{key} must be between 0 and 1000")
        scales[key] = f
    state_map_raw = data.get("state_map") or {}
    if not isinstance(state_map_raw, dict):
        raise InventoryError("state_map must be an object")
    state_map: dict = {}
    for k, v in state_map_raw.items():
        if v not in GPON_PROFILE_STATES:
            raise InventoryError(
                f"state_map[{k!r}] must be one of: {', '.join(GPON_PROFILE_STATES)}")
        state_map[str(k).strip()] = v
    state_default = str(data.get("state_default") or "unknown").strip().lower()
    if state_default not in GPON_PROFILE_STATES:
        raise InventoryError(
            f"state_default must be one of: {', '.join(GPON_PROFILE_STATES)}")
    pon_index = str(data.get("pon_index") or "as_is").strip().lower()
    if pon_index not in GPON_PON_INDEX_STRATEGIES:
        raise InventoryError(
            f"pon_index must be one of: {', '.join(GPON_PON_INDEX_STRATEGIES)}")
    pon_label = str(data.get("pon_label") or "").strip()
    if pon_label and "{pon}" not in pon_label:
        raise InventoryError("pon_label template must contain '{pon}'")
    if len(pon_label) > 32:
        raise InventoryError("pon_label must be 32 characters or fewer")
    enabled = str(data.get("enabled", 1)) not in ("0", "false", "False", "", "None")
    spec = {"oids": oids, "scales": scales, "state_map": state_map,
            "state_default": state_default, "pon_index": pon_index,
            "pon_label": pon_label}
    return {"name": name, "match_sysobjectid": match, "spec": spec,
            "enabled": enabled}

def clean_node_id(raw) -> str:
    node_id = str(raw or "").strip()
    if not node_id:
        raise InventoryError("node id is required")
    if not _NODE_ID_RE.match(node_id):
        raise InventoryError(
            "node id must be 1-64 characters, starting with a letter or digit, and "
            "contain only letters, digits, '.', '_', or '-'")
    return node_id

def clean_org_id(raw) -> str:
    org_id = str(raw or "").strip()
    if not org_id:
        raise InventoryError("org id is required")
    if not _NODE_ID_RE.match(org_id):
        raise InventoryError(
            "org id must be 1-64 characters, starting with a letter or digit, and "
            "contain only letters, digits, '.', '_', or '-'")
    return org_id
