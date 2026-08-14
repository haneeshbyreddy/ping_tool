from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from wisp.central.inventory import InventoryError
from wisp.central.weboptics_profiles import (
    CHARSETS, _clean_form_field, _clean_path, _clean_static,
)

FIELDS = ("port", "mac", "vlan", "kind")
REQUIRED_FIELDS = ("port", "mac")

PORT_SHAPES = {
    "epon-slash-colon": re.compile(r"^E?PON(\d+)/(\d+):(\d+)$", re.I),
    "pon-onu": re.compile(r"^PON(\d+):ONU(\d+)$", re.I),
}

_SHAPE_GROUPS = {"epon-slash-colon": (2, 3), "pon-onu": (1, 2)}

_MAX_ROWS = 20000


def _clean_columns(raw) -> dict[str, str]:
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise InventoryError("columns must map a field to its table heading")
    out: dict[str, str] = {}
    for key, val in raw.items():
        if key not in FIELDS:
            raise InventoryError(
                f"unknown column {key!r}: must be one of {', '.join(FIELDS)}")
        head = str(val or "").strip()
        if not head:
            continue
        if len(head) > 64:
            raise InventoryError(f"columns.{key} must be 64 characters or fewer")
        out[key] = head
    return out


def _clean_order(raw) -> tuple[str, ...]:
    if raw in (None, ""):
        return ()
    if not isinstance(raw, (list, tuple)):
        raise InventoryError("column_order must be a list of fields, in table order")
    out: list[str] = []
    for item in raw:
        val = str(item or "").strip()
        if val and val not in FIELDS:
            raise InventoryError(
                f"unknown column {val!r} in column_order: must be one of "
                f"{', '.join(FIELDS)} (or blank to skip a column)")
        out.append(val)
    if len(out) > 40:
        raise InventoryError("column_order may list at most 40 columns")
    return tuple(out)


def clean_web_mac_profile_payload(data: dict) -> dict:
    name = str(data.get("name") or "").strip().lower()
    if not name:
        raise InventoryError("profile name is required")
    if len(name) > 64:
        raise InventoryError("profile name must be 64 characters or fewer")
    if not re.match(r"^[a-z0-9][a-z0-9_.-]*$", name):
        raise InventoryError(
            "profile name must be lowercase letters, digits, '.', '_' or '-'")

    charset = str(data.get("charset") or "utf-8").strip().lower()
    if charset not in CHARSETS:
        raise InventoryError(f"charset must be one of: {', '.join(CHARSETS)}")

    shape = str(data.get("port_shape") or "").strip().lower()
    if shape not in PORT_SHAPES:
        raise InventoryError(
            f"port_shape must be one of: {', '.join(sorted(PORT_SHAPES))}")

    columns = _clean_columns(data.get("columns"))
    order = _clean_order(data.get("column_order"))
    if not columns and not order:
        raise InventoryError(
            "the profile must say where the columns are: map them by heading, "
            "or give the table's column order")
    for required in REQUIRED_FIELDS:
        if required not in columns and required not in order:
            raise InventoryError(
                f"the {required!r} column is required: without it a learned "
                "address cannot be tied to the ONU it sits behind")

    total_field = str(data.get("total_field") or "").strip()
    if total_field and not re.match(r"^[A-Za-z0-9_.-]{1,64}$", total_field):
        raise InventoryError("total_field must be a form field name")

    spec = {
        "login_page_path": _clean_path(data.get("login_page_path"), "login_page_path"),
        "login_path": _clean_path(data.get("login_path"), "login_path"),
        "mac_path": _clean_path(data.get("mac_path"), "mac_path"),
        "username_field": _clean_form_field(
            data.get("username_field") or "user", "username_field"),
        "password_field": _clean_form_field(
            data.get("password_field") or "pass", "password_field"),
        "login_static": _clean_static(data.get("login_static"), "login_static"),
        "charset": charset,
        "port_shape": shape,
        "columns": columns,
        "column_order": list(order),
        "total_field": total_field,
    }
    enabled = str(data.get("enabled", 1)) not in ("0", "false", "False", "", "None")
    return {"name": name, "spec": spec, "enabled": enabled}


@dataclass(frozen=True, slots=True)
class WebMacProfile:
    name: str
    login_page_path: str
    login_path: str
    mac_path: str
    username_field: str
    password_field: str
    login_static: dict
    charset: str
    port_shape: str
    columns: dict
    column_order: tuple
    total_field: str = ""

    @property
    def port_re(self) -> re.Pattern:
        return PORT_SHAPES[self.port_shape]

    @property
    def port_groups(self) -> tuple[int, int]:
        return _SHAPE_GROUPS[self.port_shape]

    def login_form(self, username: str, password: str) -> dict[str, str]:
        return {self.username_field: username, self.password_field: password,
                **self.login_static}

    def slot_of(self, port_label: str) -> str | None:

        m = self.port_re.match(str(port_label or "").strip())
        if not m:
            return None
        pon_at, onu_at = self.port_groups
        try:
            return f"{int(m.group(pon_at))}.{int(m.group(onu_at))}"
        except (TypeError, ValueError, IndexError):
            return None

    def declared_total(self, html: str) -> int | None:

        if not self.total_field:
            return None
        rx = re.compile(
            rf"getElementById\(\s*[\"']{re.escape(self.total_field)}[\"']\s*\)"
            r"\s*;?\s*\w*\.?value\s*=\s*['\"](\d+)['\"]")
        hit = rx.search(html)
        if hit:
            return int(hit.group(1))
        rx2 = re.compile(
            rf"name\s*=\s*['\"]?{re.escape(self.total_field)}['\"]?[^>]*?"
            r"value\s*=\s*['\"](\d+)['\"]", re.I)
        hit = rx2.search(html)
        return int(hit.group(1)) if hit else None


def profile_from_spec(name: str, spec: dict) -> WebMacProfile:
    clean = clean_web_mac_profile_payload({"name": name, **(spec or {})})
    s = clean["spec"]
    return WebMacProfile(
        name=clean["name"], login_page_path=s["login_page_path"],
        login_path=s["login_path"], mac_path=s["mac_path"],
        username_field=s["username_field"], password_field=s["password_field"],
        login_static=dict(s["login_static"]), charset=s["charset"],
        port_shape=s["port_shape"], columns=dict(s["columns"]),
        column_order=tuple(s["column_order"]), total_field=s["total_field"])


_CDATA_LOGIN = {
    "login_page_path": "/action/login.html",
    "login_path": "/action/main.html",
    "mac_path": "/action/macinfo.html",
    "username_field": "user",
    "password_field": "pass",
    "login_static": {"button": "Login", "who": "100"},
    "charset": "gb2312",
    "columns": {"vlan": "VLAN ID", "mac": "MAC Address", "kind": "Type",
                "port": "Port ID"},
    "column_order": ["vlan", "mac", "kind", "port"],
}

BUILTIN_SPECS: dict[str, dict] = {
    "dbc": {**_CDATA_LOGIN, "port_shape": "epon-slash-colon",
            "total_field": "macCount"},
    "cdata_54824": {**_CDATA_LOGIN, "port_shape": "epon-slash-colon",
                    "total_field": "macCount"},
    "syrotech_gpon": {**_CDATA_LOGIN, "port_shape": "pon-onu"},
}


def builtin(name: str) -> WebMacProfile | None:
    spec = BUILTIN_SPECS.get(str(name or "").strip().lower())
    return profile_from_spec(name, spec) if spec else None


def builtin_names() -> tuple[str, ...]:
    return tuple(BUILTIN_SPECS)


@dataclass(slots=True)
class ProfileSet:

    _by_org: dict[tuple[str | None, str], WebMacProfile] = field(default_factory=dict)
    _disabled: set[tuple[str | None, str]] = field(default_factory=set)

    @classmethod
    def build(cls, rows) -> "ProfileSet":
        out = cls()
        for row in rows or ():
            name = str(row.get("name") or "").strip().lower()
            if not name:
                continue
            org = row.get("org_id") or None
            if not row.get("enabled", True):
                out._disabled.add((org, name))
                continue
            spec = row.get("spec")
            if isinstance(spec, str):
                try:
                    spec = json.loads(spec)
                except (TypeError, ValueError):
                    continue
            try:
                out._by_org[(org, name)] = profile_from_spec(name, spec)
            except InventoryError:
                continue
        return out

    def resolve(self, org_id: str | None, name: str) -> WebMacProfile | None:

        key = str(name or "").strip().lower()
        if not key:
            return None
        for scope in (org_id, None):
            if (scope, key) in self._disabled:
                return None
            hit = self._by_org.get((scope, key))
            if hit is not None:
                return hit
        return builtin(key)

    def names(self) -> tuple[str, ...]:

        seen = {n for _, n in self._by_org}
        seen.update(builtin_names())
        seen.difference_update(
            n for org, n in self._disabled
            if org is None and (None, n) not in self._by_org)
        return tuple(sorted(seen))
