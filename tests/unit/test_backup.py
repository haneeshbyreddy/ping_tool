import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import backup


def _git(repo: Path, *args: str) -> None:
    subprocess.run(("git", "-c", "user.email=t@example.com", "-c", "user.name=t",
                    "-c", "commit.gpgsign=false") + args,
                   cwd=repo, check=True, capture_output=True, text=True)


def _seed(repo: Path) -> None:
    _git(repo, "init", "-q")
    (repo / ".gitignore").write_text("ignored/\n*.db\n")
    (repo / "tracked.py").write_text("original\n")
    (repo / "untouched.py").write_text("untouched\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")


def _dirty(repo: Path) -> None:
    (repo / "tracked.py").write_text("changed\n")
    (repo / "new file.py").write_text("brand new\n")
    (repo / "ignored").mkdir()
    (repo / "ignored" / "huge.bin").write_text("x" * 4096)
    (repo / "local.db").write_text("not source")


class DirtySourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wisp-backup-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _seed(self.repo)
        self.real_repo = backup.REPO
        backup.REPO = self.repo
        self.addCleanup(setattr, backup, "REPO", self.real_repo)

    def _names(self, tar_path: Path) -> set:
        with tarfile.open(tar_path, "r:gz") as tar:
            return set(tar.getnames())

    def test_only_changed_and_untracked_source_is_tarred(self):
        _dirty(self.repo)
        dest = self.root / "dirty-source.tar.gz"
        state = backup._dirty_source(dest)

        self.assertTrue(state["present"])
        self.assertEqual(self._names(dest), {"tracked.py", "new file.py"})
        self.assertEqual(state["files"], 2)
        self.assertEqual(state["paths"], ["new file.py", "tracked.py"])

    def test_ignored_and_unchanged_files_are_never_tarred(self):
        _dirty(self.repo)
        dest = self.root / "dirty-source.tar.gz"
        backup._dirty_source(dest)

        names = self._names(dest)
        self.assertNotIn("ignored/huge.bin", names)
        self.assertNotIn("local.db", names)
        self.assertNotIn("untouched.py", names)
        self.assertNotIn(".gitignore", names)

    def test_a_staged_file_is_captured_too(self):
        (self.repo / "staged.py").write_text("staged\n")
        _git(self.repo, "add", "staged.py")
        dest = self.root / "dirty-source.tar.gz"
        backup._dirty_source(dest)
        self.assertIn("staged.py", self._names(dest))

    def test_the_tar_extracts_with_the_working_tree_contents(self):
        _dirty(self.repo)
        dest = self.root / "dirty-source.tar.gz"
        backup._dirty_source(dest)

        out = self.root / "restored"
        with tarfile.open(dest, "r:gz") as tar:
            tar.extractall(out, filter="data")
        self.assertEqual((out / "tracked.py").read_text(), "changed\n")
        self.assertEqual((out / "new file.py").read_text(), "brand new\n")

    def test_a_clean_repo_writes_no_tar(self):
        dest = self.root / "dirty-source.tar.gz"
        state = backup._dirty_source(dest)

        self.assertFalse(state["present"])
        self.assertEqual(state["files"], 0)
        self.assertFalse(dest.exists())

    def test_a_file_that_vanishes_is_skipped_not_fatal(self):
        _dirty(self.repo)
        real = backup._dirty_paths
        self.addCleanup(setattr, backup, "_dirty_paths", real)
        listed = real() + ["gone.py"]
        backup._dirty_paths = lambda: listed

        dest = self.root / "dirty-source.tar.gz"
        state = backup._dirty_source(dest)
        self.assertEqual(state["vanished"], ["gone.py"])
        self.assertEqual(self._names(dest), {"tracked.py", "new file.py"})

    def test_a_deleted_tracked_file_leaves_no_tar_but_shows_in_status(self):
        (self.repo / "tracked.py").unlink()
        (self.repo / "untouched.py").unlink()
        dest = self.root / "dirty-source.tar.gz"
        state = backup._dirty_source(dest)

        self.assertFalse(state["present"])
        self.assertFalse(dest.exists())
        self.assertIn("tracked.py", backup._run("git", "status", "--porcelain"))


class BundleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wisp-backup-bundle-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _seed(self.repo)
        self.real_repo = backup.REPO
        backup.REPO = self.repo
        self.addCleanup(setattr, backup, "REPO", self.real_repo)

        self.db = self.root / "central.db"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE orgs (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO orgs (name) VALUES ('acme')")
        conn.commit()
        conn.close()
        self.out = self.root / "backups"

    def _manifest(self, bundle: Path) -> dict:
        import json
        with tarfile.open(bundle, "r:gz") as tar:
            return json.loads(tar.extractfile("MANIFEST.json").read())

    def test_a_dirty_repo_puts_the_source_in_the_bundle(self):
        _dirty(self.repo)
        bundle = backup.backup(self.out, self.db, 14)

        with tarfile.open(bundle, "r:gz") as tar:
            self.assertIn("dirty-source.tar.gz", tar.getnames())
        man = self._manifest(bundle)
        self.assertTrue(man["git_dirty"])
        self.assertEqual(man["dirty_source"]["files"], 2)
        self.assertIn("new file.py", man["dirty_source"]["paths"])
        self.assertIn("M tracked.py", man["git_status"])
        self.assertEqual(len(man["git_commit"]), 40)

    def test_verify_accepts_a_bundle_carrying_dirty_source(self):
        _dirty(self.repo)
        bundle = backup.backup(self.out, self.db, 14)
        r = backup.verify(bundle)
        self.assertEqual(r["integrity"], "ok")
        self.assertEqual(r["dirty_files"], 2)

    def test_verify_rejects_a_bundle_whose_dirty_source_is_corrupt(self):
        _dirty(self.repo)
        bundle = backup.backup(self.out, self.db, 14)
        man = self._manifest(bundle)
        self.assertNotEqual(man["dirty_source"]["sha256"], "0" * 64)

        broken = self.root / "broken.tar.gz"
        with tarfile.open(bundle, "r:gz") as src, tarfile.open(broken, "w:gz") as dst:
            for m in src.getmembers():
                data = src.extractfile(m) if m.isreg() else None
                if m.name == "dirty-source.tar.gz":
                    continue
                dst.addfile(m, data)
        with self.assertRaises(RuntimeError):
            backup.verify(broken)

    def test_a_clean_repo_says_so_and_ships_no_tar(self):
        bundle = backup.backup(self.out, self.db, 14)

        with tarfile.open(bundle, "r:gz") as tar:
            self.assertNotIn("dirty-source.tar.gz", tar.getnames())
        man = self._manifest(bundle)
        self.assertFalse(man["git_dirty"])
        self.assertEqual(man["git_status"], "")
        self.assertFalse(man["dirty_source"]["present"])
        self.assertEqual(backup.verify(bundle)["dirty_files"], 0)


if __name__ == "__main__":
    unittest.main()
