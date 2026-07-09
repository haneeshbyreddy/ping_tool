import dataclasses
import hashlib
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.version import version_tuple, is_newer
from wisp.config import CONFIG
from wisp.central import releasesync
from wisp.central.store import CentralStore

class VersionTupleTest(unittest.TestCase):
    def test_parses_and_pads(self):
        self.assertEqual(version_tuple("0.12.0"), (0, 12, 0))
        self.assertEqual(version_tuple("v0.12.0"), (0, 12, 0))
        self.assertEqual(version_tuple("1"), (1, 0, 0))
        self.assertEqual(version_tuple("v2"), (2, 0, 0))

    def test_ignores_git_describe_suffix(self):
        self.assertEqual(version_tuple("0.12.0-3-gabc"), (0, 12, 0))
        self.assertEqual(version_tuple("0.12.0-dirty"), (0, 12, 0))

    def test_garbage_and_empty_sort_oldest(self):
        self.assertEqual(version_tuple(""), (0, 0, 0))
        self.assertEqual(version_tuple(None), (0, 0, 0))
        self.assertEqual(version_tuple("nonsense"), (0, 0, 0))

    def test_is_newer(self):
        self.assertTrue(is_newer("0.12.0", "0.11.2"))
        self.assertFalse(is_newer("0.11.2", "0.12.0"))
        self.assertFalse(is_newer("0.12.0", "0.12.0"))
        self.assertTrue(is_newer("0.12.0", None))
        self.assertFalse(is_newer("0.12.0-3-gabc", "0.12.0"))


class _FakeGh:
    """Stands in for GithubReleases: serves in-memory bytes, writes to disk on download."""

    def __init__(self, tag, files, *, manifest_override=None):
        self.files = dict(files)  # name -> bytes
        if manifest_override is not None:
            self.files["manifest.json"] = manifest_override
        self.tag = tag
        self.downloaded = []

    def latest(self):
        return {"tag_name": self.tag, "assets": {n: f"api://{n}" for n in self.files}}

    def download(self, asset_url, dest):
        name = asset_url.split("://", 1)[1]
        if name not in self.files:
            raise releasesync.ReleaseSyncError(f"no such asset {name}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.files[name])
        self.downloaded.append(name)


def _scenario(version="0.13.0", plats=("linux-amd64", "win-amd64"), *, corrupt=None,
              installers=("wisp-edge-linux-amd64.deb", "wisp-edge-setup-win-amd64.exe")):
    """Build (files, manifest_bytes) for a release with matching sha256s."""
    files, artifacts = {}, {}
    for p in plats:
        name = f"wisp-edge-{p}"
        body = f"BINARY::{p}::{version}".encode()
        files[name] = body
        sha = hashlib.sha256(body).hexdigest()
        if corrupt == p:
            sha = "0" * 64  # manifest claims a hash the bytes don't match
        artifacts[p] = {"url": f"https://gh/v{version}/{name}", "sha256": sha}
    for inst in installers:
        files[inst] = f"INSTALLER::{inst}".encode()
    files["manifest.json"] = json.dumps({"version": version, "artifacts": artifacts}).encode()
    return files


class SyncReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = CentralStore(root / "c.db")
        self.cache = root / "releases"
        self.cfg = dataclasses.replace(CONFIG, release_cache_dir=self.cache,
                                       github_token="x", releases_repo="o/r")

    def tearDown(self):
        self.tmp.cleanup()

    def _sync(self, files, tag="v0.13.0"):
        gh = _FakeGh(tag, files)
        return releasesync.sync_release(self.store, cfg=self.cfg, gh=gh), gh

    def test_sync_publishes_mirrors_and_rewrites_urls(self):
        self.store.set_release("0.11.2", {"linux-amd64": {"url": "u", "sha256": "h"}})
        (version, n), gh = self._sync(_scenario("0.13.0"))
        self.assertEqual((version, n), ("0.13.0", 2))
        self.assertEqual(self.store.list_releases()[0]["version"], "0.13.0")
        rel = self.store.get_release("0.13.0")
        self.assertEqual(sorted(rel["artifacts"]), ["linux-amd64", "win-amd64"])
        # URLs are rewritten to central-relative /download paths, sha256 preserved.
        self.assertEqual(rel["artifacts"]["linux-amd64"]["url"],
                         "/download/0.13.0/wisp-edge-linux-amd64")
        self.assertEqual(len(rel["artifacts"]["linux-amd64"]["sha256"]), 64)
        # Binaries + installers landed on disk under the version dir.
        vdir = self.cache / "0.13.0"
        self.assertTrue((vdir / "wisp-edge-linux-amd64").is_file())
        self.assertTrue((vdir / "wisp-edge-setup-win-amd64.exe").is_file())
        self.assertTrue((vdir / "wisp-edge-linux-amd64.deb").is_file())

    def test_older_synced_release_does_not_become_latest(self):
        self.store.set_release("0.12.0", {"linux-amd64": {"url": "u", "sha256": "h"}})
        self._sync(_scenario("0.11.9", plats=("linux-amd64",)), tag="v0.11.9")
        self.assertEqual(self.store.list_releases()[0]["version"], "0.12.0")

    def test_sha256_mismatch_raises_and_publishes_nothing(self):
        with self.assertRaises(releasesync.ReleaseSyncError):
            self._sync(_scenario("0.13.0", corrupt="win-amd64"))
        self.assertEqual(self.store.list_releases(), [])

    def test_manifest_lists_missing_asset_raises(self):
        files = _scenario("0.13.0", plats=("linux-amd64",))
        del files["wisp-edge-linux-amd64"]  # manifest references it, but it's gone
        with self.assertRaises(releasesync.ReleaseSyncError):
            self._sync(files)
        self.assertEqual(self.store.list_releases(), [])

    def test_no_manifest_asset_raises(self):
        files = _scenario("0.13.0")
        del files["manifest.json"]
        with self.assertRaises(releasesync.ReleaseSyncError):
            self._sync(files)

    def test_malformed_manifest_raises(self):
        for bad in (b"<html>404</html>", b'{"version":"1.0.0"}',
                    b'{"artifacts":{"linux-amd64":{"url":"u","sha256":"h"}}}'):
            gh = _FakeGh("v1.0.0", {"manifest.json": bad})
            with self.assertRaises(releasesync.ReleaseSyncError):
                releasesync.sync_release(self.store, cfg=self.cfg, gh=gh)
        self.assertEqual(self.store.list_releases(), [])

    def test_installer_download_failure_is_non_fatal(self):
        files = _scenario("0.13.0", plats=("linux-amd64",))
        gh = _FakeGh("v0.13.0", files)
        orig = gh.download

        def flaky(asset_url, dest):
            if asset_url.endswith(".deb"):
                raise releasesync.ReleaseSyncError("boom")
            return orig(asset_url, dest)

        gh.download = flaky
        version, n = releasesync.sync_release(self.store, cfg=self.cfg, gh=gh)
        self.assertEqual((version, n), ("0.13.0", 1))  # publish still succeeds

    def test_missing_token_is_fine_public_repo(self):
        # Public repo: no token = unauthenticated sync, no Authorization header.
        gh = releasesync.GithubReleases("o/r")
        self.assertNotIn("Authorization", gh._headers("application/vnd.github+json"))

    def test_missing_repo_raises_before_network(self):
        with self.assertRaises(releasesync.ReleaseSyncError):
            releasesync.GithubReleases("", "tok")


class _RecordingNotifier:
    def __init__(self):
        self.sent = []

    def send(self, recipient, title, body, priority):
        self.sent.append((recipient, title, priority))
        class R:  # matches NotifyResult's .ok surface
            ok = True
        return R()


class SyncAndRecordTest(unittest.TestCase):
    """Monitor-the-monitor: sync outcome is stamped, pages fire on transitions only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = CentralStore(root / "c.db")
        self.cfg = dataclasses.replace(CONFIG, release_cache_dir=root / "releases",
                                       github_token="", releases_repo="o/r")
        self.notifier = _RecordingNotifier()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, gh):
        return releasesync.sync_and_record(self.store, self.notifier,
                                           cfg=self.cfg, gh=gh)

    def test_success_records_status_and_stays_quiet(self):
        self._run(_FakeGh("v0.13.0", _scenario()))
        st = self.store.release_sync_status()
        self.assertTrue(st["ok"])
        self.assertEqual(st["detail"], "0.13.0")
        self.assertEqual(self.notifier.sent, [])  # healthy from the start: no page

    def test_failure_pages_once_then_recovery_pages_once(self):
        broken = _FakeGh("v0.13.0", {})  # no manifest.json asset
        for _ in range(3):  # timer fires repeatedly; page only on the transition
            with self.assertRaises(releasesync.ReleaseSyncError):
                self._run(broken)
        self.assertFalse(self.store.release_sync_status()["ok"])
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("RELEASE SYNC FAILING", self.notifier.sent[0][1])

        self._run(_FakeGh("v0.13.0", _scenario()))
        self.assertTrue(self.store.release_sync_status()["ok"])
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertIn("recovered", self.notifier.sent[1][1])


class GithubDownloadRedirectTest(unittest.TestCase):
    """The riskiest bit: the token must be dropped when following GitHub's 302 to S3."""

    def test_redirect_refetches_without_authorization(self):
        import urllib.error, urllib.request

        seen_headers = {}
        payload = b"the-real-bytes"

        class FakeResp:
            def __init__(self, data):
                self._data = data
            def read(self):
                return self._data
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        class FakeOpener:
            def open(self, req, timeout=None):
                # First hop: asset API returns a 302 to the signed blob URL.
                seen_headers["hop1"] = dict(req.headers)
                raise urllib.error.HTTPError(
                    req.full_url, 302, "Found",
                    {"Location": "https://blob.example/signed"}, None)

        def fake_urlopen(req, timeout=None):
            # Second hop: must NOT carry Authorization.
            seen_headers["hop2"] = dict(req.headers)
            return FakeResp(payload)

        gh = releasesync.GithubReleases("o/r", "secret-token")
        with unittest.mock.patch.object(urllib.request, "build_opener",
                                        return_value=FakeOpener()), \
             unittest.mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            with tempfile.TemporaryDirectory() as d:
                dest = Path(d) / "asset.bin"
                gh.download("api://asset", dest)
                self.assertEqual(dest.read_bytes(), payload)

        # header keys are title-cased by urllib
        self.assertIn("Authorization", seen_headers["hop1"])
        self.assertNotIn("Authorization", seen_headers["hop2"])


if __name__ == "__main__":
    unittest.main()
