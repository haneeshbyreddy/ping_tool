from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

from wisp.config import CONFIG, Config
from wisp.egress.notifiers import WhatsAppFacts

log = logging.getLogger("wisp.central.releasesync")

_UA = "wisp-central-releasesync"
_API = "https://api.github.com"


def _admin_numbers(store, cfg: Config) -> list[str]:
    try:
        num = (store.whatsapp_settings().get("admin_number") or "").strip()
    except Exception:
        num = ""
    num = num or cfg.whatsapp_admin_number
    return [num] if num else []

class ReleaseSyncError(Exception):
    pass

def _clean_manifest(raw: bytes) -> tuple[str, dict, str]:
    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ReleaseSyncError(f"manifest is not JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ReleaseSyncError("manifest is not a JSON object")
    version = doc.get("version")
    artifacts = doc.get("artifacts")
    if not isinstance(version, str) or not version.strip():
        raise ReleaseSyncError("manifest has no 'version'")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ReleaseSyncError("manifest has no 'artifacts'")
    for plat, art in artifacts.items():
        if not isinstance(art, dict) or not art.get("url") or not art.get("sha256"):
            raise ReleaseSyncError(f"artifact {plat!r} is missing url/sha256")
    channel = doc.get("channel", "stable")
    return version.strip(), artifacts, channel

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def _is_installer(name: str) -> bool:
    return name.startswith("wisp-edge-setup") or name.endswith(".deb")

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None

class GithubReleases:

    def __init__(self, repo: str, token: str = "", *, timeout: float = 30.0) -> None:
        if not repo:
            raise ReleaseSyncError("WISP_RELEASES_REPO is not set")
        self.repo = repo
        self.token = token
        self.timeout = timeout

    def _headers(self, accept: str) -> dict:
        headers = {"Accept": accept, "User-Agent": _UA,
                   "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def latest(self) -> dict:
        url = f"{_API}/repos/{self.repo}/releases/latest"
        req = urllib.request.Request(url, headers=self._headers("application/vnd.github+json"))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                doc = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                hint = (" (check the token's repo scope)" if self.token
                        else " (repo private or rate-limited? set WISP_GITHUB_TOKEN)")
            else:
                hint = ""
            raise ReleaseSyncError(f"GitHub API {exc.code} for {url}{hint}") from exc
        except Exception as exc:
            raise ReleaseSyncError(f"could not reach {url}: {exc}") from exc
        assets = {a["name"]: a["url"] for a in doc.get("assets", []) if a.get("name") and a.get("url")}
        return {"tag_name": (doc.get("tag_name") or "").strip(), "assets": assets}

    def download(self, asset_url: str, dest: Path) -> None:
        req = urllib.request.Request(asset_url, headers=self._headers("application/octet-stream"))
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                data = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                loc = exc.headers["Location"]
                clean = urllib.request.Request(loc, headers={"User-Agent": _UA})
                with urllib.request.urlopen(clean, timeout=self.timeout) as resp:
                    data = resp.read()
            else:
                raise ReleaseSyncError(f"GitHub API {exc.code} downloading {asset_url}") from exc
        except Exception as exc:
            raise ReleaseSyncError(f"could not download {asset_url}: {exc}") from exc
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dest)

def sync_release(store, *, cfg: Config = CONFIG, gh: GithubReleases | None = None,
                 url: str | None = None) -> tuple[str, int]:

    gh = gh or GithubReleases(cfg.releases_repo, cfg.github_token)
    rel = gh.latest()
    assets = rel["assets"]
    if "manifest.json" not in assets:
        raise ReleaseSyncError("latest release has no manifest.json asset")

    tag_ver = rel["tag_name"].lstrip("v").strip() or "unknown"
    version_dir = cfg.release_cache_dir / tag_ver
    manifest_path = version_dir / "manifest.json"
    gh.download(assets["manifest.json"], manifest_path)
    version, artifacts, channel = _clean_manifest(manifest_path.read_bytes())
    if version != tag_ver and version_dir.name != version:
        version_dir = cfg.release_cache_dir / version
        version_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_path.replace(version_dir / "manifest.json")

    out: dict[str, dict] = {}
    for plat, art in artifacts.items():
        name = art["url"].rsplit("/", 1)[-1]
        if name not in assets:
            raise ReleaseSyncError(f"manifest lists {name!r} but it's not a release asset")
        dest = version_dir / name
        gh.download(assets[name], dest)
        got = _sha256(dest)
        if got != art["sha256"]:
            dest.unlink(missing_ok=True)
            raise ReleaseSyncError(
                f"sha256 mismatch for {name}: manifest={art['sha256']} mirrored={got}")
        out[plat] = {"url": f"/download/{version}/{name}", "sha256": art["sha256"]}

    for name, asset_url in assets.items():
        if not _is_installer(name):
            continue
        try:
            gh.download(asset_url, version_dir / name)
        except ReleaseSyncError as exc:
            log.warning("could not mirror installer %s: %s", name, exc)

    store.set_release(version, out, channel)
    log.info("release sync: mirrored %s (%s), %d agent artifact(s) + installers into %s",
             version, channel, len(out), version_dir)
    return version, len(out)

def sync_app_release(*, cfg: Config = CONFIG,
                     gh: GithubReleases | None = None) -> tuple[str, list[str]] | None:

    if not cfg.app_releases_repo:
        return None
    gh = gh or GithubReleases(cfg.app_releases_repo, "")
    rel = gh.latest()
    names = [n for n in rel["assets"] if n.endswith(".apk")]
    if not names:
        raise ReleaseSyncError(
            f"latest {cfg.app_releases_repo} release has no .apk asset")
    for name in names:
        gh.download(rel["assets"][name], cfg.release_cache_dir / "app" / name)
    log.info("app sync: mirrored %s (%s) into %s",
             rel["tag_name"], ", ".join(names), cfg.release_cache_dir / "app")
    return rel["tag_name"], names


def sync_and_record(store, notifier=None, *, cfg: Config = CONFIG,
                    gh: GithubReleases | None = None,
                    app_gh: GithubReleases | None = None) -> tuple[str, int]:

    numbers = _admin_numbers(store, cfg)
    try:
        version, n = sync_release(store, cfg=cfg, gh=gh)
    except ReleaseSyncError as exc:
        prev = store.set_release_sync_status(False, str(exc))
        if notifier and (prev is None or prev.get("ok")) and numbers:
            try:
                detail = (f"central can no longer mirror releases: {exc} — "
                          "fleet self-updates are stalled until this is fixed.")
                notifier.send("🚨 RELEASE SYNC FAILING", detail, 4, whatsapp=numbers,
                              facts=WhatsAppFacts.derive("Release mirror", detail,
                                                         "SYNC FAILING"))
            except Exception:
                log.exception("release-sync failure page could not be sent")
        raise
    prev = store.set_release_sync_status(True, version)
    if notifier and prev is not None and not prev.get("ok") and numbers:
        try:
            detail = f"release mirror is healthy again; latest mirrored: {version}"
            notifier.send("✅ Release sync recovered", detail, 3, whatsapp=numbers,
                          facts=WhatsAppFacts.derive("Release mirror", detail,
                                                     "SYNC OK"))
        except Exception:
            log.exception("release-sync recovery page could not be sent")
    try:
        sync_app_release(cfg=cfg, gh=app_gh)
    except ReleaseSyncError as exc:
        log.warning("app release sync failed: %s", exc)
    return version, n
