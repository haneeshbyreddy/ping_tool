from __future__ import annotations

import http.cookiejar
import logging
import re
import threading
import time as _time
import urllib.error
import urllib.parse
import urllib.request

from wisp.central import history, radius, radius_profiles
from wisp.central.secretbox import DecryptError
from wisp.central.store_util import _now_iso
from wisp.config import CONFIG, Config

log = logging.getLogger("wisp.central.radius")

_UA = "wisp-central"

_MAX_BODY = 32 * 1024 * 1024


class PanelError(Exception):

    def __init__(self, message: str, state: str = "unreachable") -> None:
        super().__init__(message)
        self.state = state


def clean_base_url(raw: str) -> str:

    text = str(raw or "").strip().rstrip("/")
    if not text:
        raise PanelError("the panel address is required")
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise PanelError("the panel address must start with http:// or https://")
    if not parts.hostname:
        raise PanelError("the panel address must name a host")
    if parts.username or parts.password:
        raise PanelError(
            "put the sign-in details in the username and password fields, "
            "not in the address")
    if parts.path.strip("/") or parts.query or parts.fragment:
        raise PanelError(
            "the panel address is the server only (like https://cbp.example.in) — "
            "the profile carries the pages")
    return f"{parts.scheme}://{parts.netloc}"


class PanelHttp:

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = clean_base_url(base_url)
        self.timeout = timeout
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar))

    def _url(self, path: str, query: dict | None = None) -> str:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(sorted(query.items()))
        return url

    def request(self, path: str, *, query: dict | None = None,
                form: list | dict | None = None,
                headers: dict | None = None) -> tuple[int, bytes, str, str]:

        data = None
        if form is not None:
            pairs = list(form.items()) if isinstance(form, dict) else list(form)
            data = urllib.parse.urlencode(pairs).encode("ascii")
        sent = {"User-Agent": _UA, "Accept": "*/*", **(headers or {})}
        req = urllib.request.Request(self._url(path, query), data=data,
                                     headers=sent)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                body = resp.read(_MAX_BODY)
                return (resp.status, body, resp.headers.get("Content-Type", ""),
                        resp.url)
        except urllib.error.HTTPError as e:
            return (e.code, e.read(_MAX_BODY) if e.fp else b"",
                    e.headers.get("Content-Type", "") if e.headers else "",
                    e.url if hasattr(e, "url") else self._url(path, query))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise PanelError(f"could not reach the panel: {e}") from e


def _scrape(html: str, field: str) -> str | None:

    pattern = (r"""<(?:input|meta)\b[^>]*?"""
               r"""(?:name|id)\s*=\s*["']%s["'][^>]*?"""
               r"""(?:value|content)\s*=\s*["']([^"']*)["']""" % re.escape(field))
    hit = re.search(pattern, html, re.I)
    if hit:
        return hit.group(1)
    pattern = (r"""<(?:input|meta)\b[^>]*?"""
               r"""(?:value|content)\s*=\s*["']([^"']*)["'][^>]*?"""
               r"""(?:name|id)\s*=\s*["']%s["']""" % re.escape(field))
    hit = re.search(pattern, html, re.I)
    return hit.group(1) if hit else None


def login(http: PanelHttp, username: str, password: str, profile) -> None:

    status, body, _, _ = http.request(profile.login_page_path)
    if status >= 400:
        raise PanelError(
            f"the sign-in page answered {status}, so the credentials were NOT "
            "sent. Check the panel address and that the profile's login page "
            "path is right for this build.")

    scraped: dict[str, str] = {}
    wanted = profile.scraped_fields()
    if wanted:
        html = body.decode(profile.charset, "replace")
        for field in wanted:
            found = _scrape(html, field)
            if found is not None:
                scraped[field] = found

    headers = {}
    if profile.login_flow == "encrypted-nonce":
        if not scraped.get(profile.nonce_field):
            raise PanelError(
                f"the sign-in page carried no {profile.nonce_field!r} field, so "
                "the credentials were NOT sent. This panel mints a one-time key "
                "there and encrypts the sign-in with it; a build that does not "
                "is a different profile.", state="login")
        headers["X-Requested-With"] = "XMLHttpRequest"

    try:
        form = profile.login_form(username, password, scraped)
    except ValueError as e:
        raise PanelError(
            "the sign-in page did not carry the one-time key this panel "
            "encrypts with, so the credentials were NOT sent.",
            state="login") from e
    http.request(profile.login_path, form=form, headers=headers)


def fetch_roster(http: PanelHttp, profile) -> str:

    asked = profile.roster_path
    status, body, ctype, final = http.request(
        asked, query=profile.roster_query, form=profile.export_form())
    if status == 404:
        raise PanelError(
            f"this panel has no {asked} (404). It answers on the "
            "web UI but not on that export; it needs its own capture and profile.")
    if status >= 400:
        raise PanelError(f"the customer export answered {status}")
    text = body.decode(profile.charset, "replace")
    if "html" not in ctype.lower() and text.lstrip()[:1] != "<":
        return text

    landed = urllib.parse.urlsplit(final or "").path or asked
    if landed.rstrip("/") == asked.rstrip("/"):
        raise PanelError(
            "the panel answered with a page instead of the export, which is what "
            "it does when the sign-in did not take — check the username and "
            "password.", state="login")
    if _looks_like_login(landed, profile):
        raise PanelError(
            f"the export bounced back to the sign-in page ({landed}), which is "
            "what this panel does when the credentials are refused — check the "
            "username and password.", state="login")
    raise PanelError(
        f"this account signed in, but the panel sent the export to {landed} "
        "instead: the login is fine and it is not allowed to export the "
        "customer list. Ask whoever administers the panel to give this login "
        "the export permission, or use one that already has it.",
        state="forbidden")


def _looks_like_login(landed: str, profile) -> bool:
    for known in (profile.login_page_path, profile.login_path):
        if known and landed.rstrip("/") == known.rstrip("/"):
            return True
    return "login" in landed.lower()


class RadiusSyncer:

    def __init__(self, store, secretbox, cfg: Config = CONFIG,
                 http_factory=None) -> None:
        self.store = store
        self.secretbox = secretbox
        self.cfg = cfg
        self._http = http_factory or PanelHttp
        self._lock = threading.Lock()

    def _profiles(self, org_id: str):
        try:
            rows = self.store.list_radius_profiles(org_id)
        except Exception:
            log.exception("radius: could not read profiles for org=%s", org_id)
            rows = []
        return radius_profiles.ProfileSet.build(rows)

    def _credentials(self, account: dict) -> tuple[str, str] | None:
        user = (account.get("username") or "").strip()
        enc = account.get("password_enc")
        if not user or not enc:
            return None
        try:
            return user, self.secretbox.decrypt(enc)
        except DecryptError:
            log.warning("radius: stored password for org=%s will not decrypt "
                        "(key rotated?)", account.get("org_id"))
            return None

    def targets(self) -> list[dict]:
        try:
            return self.store.list_radius_accounts(enabled_only=True)
        except Exception:
            log.exception("radius: could not list accounts")
            return []

    def sync_once(self) -> int:
        done = 0
        orgs: set[str] = set()
        for account in self.targets():
            orgs.add(str(account.get("org_id") or ""))
            try:
                if self.sync_org(account):
                    done += 1
            except Exception:
                log.exception("radius: sync failed for org=%s", account.get("org_id"))
        if done:
            history.record_radius_day(self.store, self.cfg, orgs)
        return done

    def relink_org(self, org_id: str, ts: str | None = None):

        customers = self.store.radius_customers_for_link(org_id)
        macs, onus = self.store.radius_link_inputs(org_id)
        result = radius.link_customers(customers, macs, onus)
        self.store.replace_radius_links(org_id, result.links, ts or _now_iso())
        if result.crowded_slots:
            log.info("radius: org=%s skipped %d ONU slot(s) carrying more than %d "
                     "MACs — an aggregate port, not a subscriber", org_id,
                     result.crowded_slots, radius.MAX_SLOT_MACS)
        if result.cross_panel:
            log.info("radius: org=%s %d MAC(s) claimed by more than one panel; the "
                     "panel connected first won", org_id, result.cross_panel)
        return result

    def sync_org(self, account: dict) -> bool:

        org_id = account["org_id"]
        account_id = int(account["id"])
        name = str(account.get("profile") or "").strip().lower()
        with self._lock:
            profile = self._profiles(org_id).resolve(org_id, name)
            if profile is None:
                self.store.set_radius_status(
                    org_id, account_id, "no_profile",
                    f"no recipe named {name!r} — the panel's pages and columns are "
                    "a profile, and nothing can be read without one", profile=name)
                return False

            creds = self._credentials(account)
            if creds is None:
                self.store.set_radius_status(
                    org_id, account_id, "no_credentials",
                    "no sign-in details stored for this panel", profile=name)
                return False

            started = _time.monotonic()
            try:
                http = self._http(account["base_url"],
                                  timeout=max(5, int(self.cfg.radius_timeout_s)))
                login(http, creds[0], creds[1], profile)
                text = fetch_roster(http, profile)
            except PanelError as e:
                self.store.set_radius_status(org_id, account_id, e.state, str(e),
                                             profile=name)
                log.warning("radius: org=%s account=%s %s: %s", org_id, account_id,
                            e.state, e)
                return False

            roster = radius.parse_roster(text, profile)
            if not roster.customers:
                self.store.set_radius_status(
                    org_id, account_id, "error",
                    "the export carried no customer rows this profile could read: "
                    "either it is genuinely empty, or its columns are named "
                    "differently on this build", profile=name)
                return False

            ts = _now_iso()
            self.store.upsert_radius_customers(org_id, account_id,
                                               roster.customers, ts)
            result = self.relink_org(org_id, ts)
            mine = sum(1 for l in result.links if l.account_id == account_id)

            state = "partial" if roster.missing_headings else "ok"
            detail = None
            if roster.missing_headings:
                detail = (
                    "the export did not carry these columns: "
                    f"{', '.join(roster.missing_headings)} — they are mapped in the "
                    "profile but absent from the page, so those details will be "
                    "blank for every customer")
            self.store.set_radius_status(
                org_id, account_id, state, detail, profile=name,
                customers=len(roster.customers), linked=mine)
            log.info("radius: org=%s account=%s %d customers, %d linked here of %d "
                     "for the org (%d by mac, %d by name, %d ambiguous) in %.1fs",
                     org_id, account_id, len(roster.customers), mine,
                     len(result.links), result.by_mac, result.by_name,
                     result.ambiguous_mac + result.ambiguous_name,
                     _time.monotonic() - started)
            return True


def build_syncer(cfg: Config = CONFIG, store=None, secretbox=None) -> RadiusSyncer | None:
    if not cfg.radius_enabled:
        return None
    if store is None or secretbox is None:
        log.warning("radius syncer unavailable — store/secretbox missing")
        return None
    return RadiusSyncer(store, secretbox, cfg)


def start_radius_thread(cfg: Config = CONFIG, store=None, secretbox=None,
                        syncer: RadiusSyncer | None = None) -> threading.Thread | None:

    syncer = syncer or build_syncer(cfg, store, secretbox)
    if syncer is None:
        return None
    interval = max(300, int(cfg.radius_interval_s))

    def _loop() -> None:
        log.info("radius syncer started (every %ss)", interval)
        _time.sleep(min(90.0, interval))
        while True:
            try:
                syncer.sync_once()
            except Exception:
                log.exception("radius sync failed")
            _time.sleep(interval)

    t = threading.Thread(target=_loop, name="wisp-central-radius", daemon=True)
    t.start()
    return t
