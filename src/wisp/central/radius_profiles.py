from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from wisp.central import webcrypto
from wisp.central.inventory import InventoryError
from wisp.central.weboptics_profiles import (
    CHARSETS, _clean_form_field, _clean_path, _clean_static,
)

FIELDS = ("username", "name", "mac", "mobile", "alt_mobile", "acno", "status",
          "expiry", "package", "branch", "area", "address", "balance")
REQUIRED_FIELDS = ("username", "name")

STATUSES = ("active", "expired", "inactive", "unknown")

DATE_FORMATS = ("", "dmy", "mdy", "iso", "named-month")

LOGIN_FLOWS = ("form", "encrypted-nonce")

ENCRYPTABLE = ("username", "password")

ROSTER_METHODS = ("POST", "GET")

_MAX_QUERY_FIELDS = 8


def _clean_query(raw, field_name: str) -> dict[str, str]:
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise InventoryError(f"{field_name} must be an object of query parameters")
    if len(raw) > _MAX_QUERY_FIELDS:
        raise InventoryError(
            f"{field_name} may hold at most {_MAX_QUERY_FIELDS} parameters")
    out: dict[str, str] = {}
    for key, val in raw.items():
        name = _clean_form_field(key, f"{field_name} parameter name")
        text = str("" if val is None else val)
        if len(text) > 200:
            raise InventoryError(f"{field_name}.{name} must be 200 characters or fewer")
        out[name] = text
    return out


def _clean_columns(raw) -> dict[str, tuple[str, str]]:
    if not isinstance(raw, dict) or not raw:
        raise InventoryError(
            "columns must map each field to [export column, CSV heading]")
    out: dict[str, tuple[str, str]] = {}
    for key, val in raw.items():
        if key not in FIELDS:
            raise InventoryError(
                f"unknown column {key!r}: must be one of {', '.join(FIELDS)}")
        if isinstance(val, str):
            pair = ("", val)
        elif isinstance(val, (list, tuple)) and len(val) == 2:
            pair = (str(val[0] or "").strip(), str(val[1] or "").strip())
        else:
            raise InventoryError(
                f"columns.{key} must be [export column, CSV heading] or a heading")
        if not pair[1]:
            raise InventoryError(f"columns.{key} needs the CSV heading it arrives under")
        for part in pair:
            if len(part) > 64:
                raise InventoryError(f"columns.{key} entries must be 64 characters or fewer")
        out[key] = pair
    for required in REQUIRED_FIELDS:
        if required not in out:
            raise InventoryError(
                f"the {required!r} column is required: without it a customer "
                "cannot be named, and naming them is the whole point")
    return out


def _clean_status_map(raw) -> dict[str, str]:
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise InventoryError("status_map must map the panel's word to ours")
    out: dict[str, str] = {}
    for key, val in raw.items():
        word = str(key or "").strip().lower()
        ours = str(val or "").strip().lower()
        if not word:
            continue
        if ours not in STATUSES:
            raise InventoryError(
                f"status_map.{word} must be one of: {', '.join(STATUSES)}")
        out[word] = ours
    return out


def _clean_encrypt_fields(raw) -> tuple[str, ...]:
    if raw in (None, ""):
        return ENCRYPTABLE
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise InventoryError("encrypt_fields must be a list of login fields")
    out = []
    for item in raw:
        word = str(item or "").strip().lower()
        if word not in ENCRYPTABLE:
            raise InventoryError(
                f"encrypt_fields may only name {' or '.join(ENCRYPTABLE)}")
        if word not in out:
            out.append(word)
    if not out:
        raise InventoryError(
            "encrypt_fields cannot be empty on an encrypted-nonce login — "
            "leave it out to encrypt both")
    return tuple(out)


def clean_radius_profile_payload(data: dict) -> dict:
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

    columns_field = str(data.get("columns_field") or "").strip()
    if columns_field:
        columns_field = _clean_form_field(columns_field, "columns_field")

    columns = _clean_columns(data.get("columns"))
    if columns_field and any(not req for req, _ in columns.values()):
        missing = sorted(k for k, (req, _) in columns.items() if not req)
        raise InventoryError(
            "this panel picks its own columns, so every mapped field needs the "
            f"export column that asks for it — missing: {', '.join(missing)}")

    date_format = str(data.get("date_format") or "").strip().lower()
    if date_format not in DATE_FORMATS:
        raise InventoryError(
            "date_format must be one of: "
            f"{', '.join(f or 'blank' for f in DATE_FORMATS)} — blank means the "
            "panel's date convention is unknown and its dates stay unparsed")

    login_flow = str(data.get("login_flow") or "form").strip().lower()
    if login_flow not in LOGIN_FLOWS:
        raise InventoryError(f"login_flow must be one of: {', '.join(LOGIN_FLOWS)}")

    roster_method = str(data.get("roster_method") or "POST").strip().upper()
    if roster_method not in ROSTER_METHODS:
        raise InventoryError(
            f"roster_method must be one of: {', '.join(ROSTER_METHODS)}")

    csrf_field = str(data.get("csrf_field") or "").strip()
    if csrf_field:
        csrf_field = _clean_form_field(csrf_field, "csrf_field")
    nonce_field = str(data.get("nonce_field") or "").strip()
    if nonce_field:
        nonce_field = _clean_form_field(nonce_field, "nonce_field")
    encrypt_fields = _clean_encrypt_fields(data.get("encrypt_fields"))

    if login_flow == "encrypted-nonce" and not nonce_field:
        raise InventoryError(
            "an encrypted-nonce login needs nonce_field: the panel mints a "
            "one-time key on the sign-in page and the credentials are encrypted "
            "with it, so without naming that field nothing can be sent")

    spec = {
        "login_page_path": _clean_path(data.get("login_page_path"), "login_page_path"),
        "login_path": _clean_path(data.get("login_path"), "login_path"),
        "roster_path": _clean_path(data.get("roster_path"), "roster_path"),
        "roster_query": _clean_query(data.get("roster_query"), "roster_query"),
        "roster_static": _clean_static(data.get("roster_static"), "roster_static"),
        "roster_method": roster_method,
        "username_field": _clean_form_field(
            data.get("username_field") or "username", "username_field"),
        "password_field": _clean_form_field(
            data.get("password_field") or "password", "password_field"),
        "login_static": _clean_static(data.get("login_static"), "login_static"),
        "login_flow": login_flow,
        "csrf_field": csrf_field,
        "nonce_field": nonce_field,
        "encrypt_fields": list(encrypt_fields),
        "columns_field": columns_field,
        "columns": {k: list(v) for k, v in columns.items()},
        "status_map": _clean_status_map(data.get("status_map")),
        "date_format": date_format,
        "charset": charset,
    }
    enabled = str(data.get("enabled", 1)) not in ("0", "false", "False", "", "None")
    return {"name": name, "spec": spec, "enabled": enabled}


@dataclass(frozen=True, slots=True)
class RadiusProfile:
    name: str
    login_page_path: str
    login_path: str
    roster_path: str
    roster_query: dict
    roster_static: dict
    roster_method: str
    username_field: str
    password_field: str
    login_static: dict
    login_flow: str
    csrf_field: str
    nonce_field: str
    encrypt_fields: tuple
    columns_field: str
    columns: dict
    status_map: dict
    date_format: str
    charset: str

    def login_form(self, username: str, password: str,
                   scraped: dict | None = None) -> dict[str, str]:
        values = {"username": username, "password": password}
        if self.login_flow == "encrypted-nonce":
            nonce = (scraped or {}).get(self.nonce_field) or ""
            if not nonce:
                raise ValueError("nonce")
            for role in self.encrypt_fields:
                values[role] = webcrypto.cryptojs_encrypt(values[role], nonce)
        form = {self.username_field: values["username"],
                self.password_field: values["password"],
                **self.login_static}
        for field_name in (self.csrf_field, self.nonce_field):
            if field_name and (scraped or {}).get(field_name):
                form[field_name] = scraped[field_name]
        return form

    def scraped_fields(self) -> tuple[str, ...]:
        return tuple(f for f in (self.csrf_field, self.nonce_field) if f)

    def export_form(self) -> list[tuple[str, str]] | None:
        if self.roster_method == "GET":
            return None
        out = [(k, v) for k, v in self.roster_static.items()]
        if self.columns_field:
            for req, _ in self.columns.values():
                if req:
                    out.append((self.columns_field, req))
        return out

    def heading_of(self, field_name: str) -> str | None:
        pair = self.columns.get(field_name)
        return pair[1] if pair else None

    def status_of(self, raw: str | None) -> str:
        word = str(raw or "").strip().lower()
        if not word:
            return "unknown"
        mapped = self.status_map.get(word)
        if mapped:
            return mapped
        return word if word in STATUSES else "unknown"


def profile_from_spec(name: str, spec: dict) -> RadiusProfile:
    clean = clean_radius_profile_payload({"name": name, **(spec or {})})
    s = clean["spec"]
    return RadiusProfile(
        name=clean["name"], login_page_path=s["login_page_path"],
        login_path=s["login_path"], roster_path=s["roster_path"],
        roster_query=dict(s["roster_query"]), roster_static=dict(s["roster_static"]),
        roster_method=s["roster_method"],
        username_field=s["username_field"], password_field=s["password_field"],
        login_static=dict(s["login_static"]), login_flow=s["login_flow"],
        csrf_field=s["csrf_field"], nonce_field=s["nonce_field"],
        encrypt_fields=tuple(s["encrypt_fields"]),
        columns_field=s["columns_field"],
        columns={k: tuple(v) for k, v in s["columns"].items()},
        status_map=dict(s["status_map"]), date_format=s["date_format"],
        charset=s["charset"])


_CBP = {
    "login_page_path": "/admin/login",
    "login_path": "/admin/login/process",
    "roster_path": "/admin/user/export",
    "roster_query": {"h": "1", "rp": "5000"},
    "roster_static": {"filter": "1", "ex_093847393949393[]": "csv"},
    "username_field": "unme",
    "password_field": "passd",
    "login_static": {"remember": "on"},
    "columns_field": "sl_09287373872[]",
    "columns": {
        "username": ["uname", "Username"],
        "name": ["name", "Name"],
        "mac": ["muname", "MAC"],
        "mobile": ["mobile", "Mobile"],
        "alt_mobile": ["mobile1", "Alt. Mobile"],
        "acno": ["caf_no", "CAF No"],
        "status": ["sts", "Status"],
        "expiry": ["expiration", "Expiry Date"],
        "package": ["pkgname", "Package Name"],
        "branch": ["branch", "Branch"],
        "area": ["area", "Area"],
        "address": ["inst_addr", "Installation Address"],
        "balance": ["balance_amount", "Balance Amount"],
    },
    "status_map": {"active": "active", "expired": "expired",
                   "inactive": "inactive", "disabled": "inactive",
                   "suspended": "inactive"},
    "date_format": "dmy",
    "charset": "utf-8",
}

_ONERADIUS = {
    "login_page_path": "/login",
    "login_path": "/login",
    "roster_path": "/common/export-list",
    "roster_query": {"params": "[]"},
    "roster_method": "GET",
    "username_field": "LoginForm[username]",
    "password_field": "LoginForm[password]",
    "login_flow": "encrypted-nonce",
    "csrf_field": "_csrf-backend-admin",
    "nonce_field": "enckey",
    "encrypt_fields": ["username", "password"],
    "columns": {
        "username": ["", "Username"],
        "name": ["", "First Name"],
        "mac": ["", "MAC"],
        "mobile": ["", "Mobile"],
        "acno": ["", "CAF Number"],
        "status": ["", "Status"],
        "expiry": ["", "Expiration"],
        "package": ["", "Package"],
        "branch": ["", "Branch"],
        "area": ["", "Area"],
        "address": ["", "Installation Address"],
        "balance": ["", "Balance"],
    },
    "status_map": {"active": "active", "expired": "expired",
                   "disabled": "inactive", "suspended": "inactive",
                   "inactive": "inactive", "new": "inactive"},
    "date_format": "named-month",
    "charset": "utf-8",
}

BUILTIN_SPECS: dict[str, dict] = {"cbp": _CBP, "oneradius": _ONERADIUS}


def builtin(name: str) -> RadiusProfile | None:
    spec = BUILTIN_SPECS.get(str(name or "").strip().lower())
    return profile_from_spec(name, spec) if spec else None


def builtin_names() -> tuple[str, ...]:
    return tuple(BUILTIN_SPECS)


@dataclass(slots=True)
class ProfileSet:

    _by_org: dict[tuple[str | None, str], RadiusProfile] = field(default_factory=dict)
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

    def resolve(self, org_id: str | None, name: str) -> RadiusProfile | None:

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
