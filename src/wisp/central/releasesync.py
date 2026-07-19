from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

from wisp.config import CONFIG, Config

log = logging.getLogger("wisp.central.releasesync")

_UA = "wisp-central-releasesync"
_API = "https://api.github.com"

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
    # First-install artifacts: the Windows setup exe + the .debs. Deliberately NOT
    # in the self-update manifest; mirrored only so the dashboard install card can
    # link them from central instead of GitHub.
    return name.startswith("wisp-edge-setup") or name.endswith(".deb")

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # GitHub's asset API 302s to a short-lived signed blob URL that rejects an
    # Authorization header ("only one auth mechanism allowed"). We must NOT follow
    # the redirect with the token still attached — capture the Location and re-fetch
    # it clean. Doing this by hand is deterministic across Python versions (urllib's
    # own auth-stripping-on-redirect behaviour has changed between releases).
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None

class GithubReleases:
    """Minimal GitHub REST client for mirroring a repo's releases (stdlib only).

    Works unauthenticated against a public repo (no token = no Authorization
    header); a token is only needed for a private repo or to dodge the 60/h
    anonymous rate limit.
    """

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
        """Return {'tag_name': str, 'assets': {name: asset_api_url}} for the latest release."""
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
        """Download one release asset to `dest` (atomic tmp+replace, two-hop auth)."""
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
    """Mirror the latest GitHub release into central and publish it.

    Downloads the release's manifest.json + every agent binary it lists (each
    verified against the manifest sha256) + the installer assets, into
    `cfg.release_cache_dir/<version>/`, then rewrites the artifact URLs to
    central-relative `/download/<version>/<name>` and records the release.
    """
    gh = gh or GithubReleases(cfg.releases_repo, cfg.github_token)
    rel = gh.latest()
    assets = rel["assets"]
    if "manifest.json" not in assets:
        raise ReleaseSyncError("latest release has no manifest.json asset")

    # We don't know the version until we read the manifest, but we need a dir to
    # land manifest.json in first — use the tag, then reconcile with the manifest.
    tag_ver = rel["tag_name"].lstrip("v").strip() or "unknown"
    version_dir = cfg.release_cache_dir / tag_ver
    manifest_path = version_dir / "manifest.json"
    gh.download(assets["manifest.json"], manifest_path)
    version, artifacts, channel = _clean_manifest(manifest_path.read_bytes())
    if version != tag_ver and version_dir.name != version:
        # Manifest is the source of truth for the version string; move the dir.
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

    # Installers ride along for the dashboard install card (served at /download/latest/).
    # Best-effort: a missing/failed installer must not sink the self-update publish.
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
    """Mirror the field app's latest release .apk asset(s) into a FIXED dir.

    `release_cache_dir/app/<name>` serves at /download/app/<name> through the
    existing release route with no store involvement — deliberately: the store's
    release table drives edge self-update "latest", and an app release must
    never be able to poison it. The asset keeps its name (CI publishes
    `wisp-field.apk`), so the worker-facing URL is stable across versions.
    Unauthenticated: the app repo is public, and the release-sync token is
    fine-grained to the main repo (it would 403 here). Returns (tag, names),
    or None when no app repo is configured.
    """
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
    """Run sync_release, stamp the outcome in the store, page on transitions only.

    The update channel is itself monitored: every attempt writes `release_sync`
    status (surfaced on /api/system), and `cfg.central_ntfy_topic` gets one page
    when syncs start failing and one when they recover — never per-run, the timer
    fires every 15 min. A failed sync still raises so the CLI exits nonzero.
    """
    try:
        version, n = sync_release(store, cfg=cfg, gh=gh)
    except ReleaseSyncError as exc:
        prev = store.set_release_sync_status(False, str(exc))
        if notifier and (prev is None or prev.get("ok")) and cfg.central_ntfy_topic:
            try:
                notifier.send(cfg.central_ntfy_topic, "🚨 RELEASE SYNC FAILING",
                              f"central can no longer mirror releases: {exc}\n"
                              "Fleet self-updates are stalled until this is fixed.", 4)
            except Exception:
                log.exception("release-sync failure page could not be sent")
        raise
    prev = store.set_release_sync_status(True, version)
    if notifier and prev is not None and not prev.get("ok") and cfg.central_ntfy_topic:
        try:
            notifier.send(cfg.central_ntfy_topic, "✅ Release sync recovered",
                          f"release mirror is healthy again; latest mirrored: {version}", 3)
        except Exception:
            log.exception("release-sync recovery page could not be sent")
    # The field-app APK rides the same timer, best-effort: a broken app mirror
    # must never sink (or page about) the edge self-update channel above.
    try:
        sync_app_release(cfg=cfg, gh=app_gh)
    except ReleaseSyncError as exc:
        log.warning("app release sync failed: %s", exc)
    return version, n
