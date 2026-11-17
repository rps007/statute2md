import base64
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class NotebookUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.remote = self.directory / "remote.git"
        self.seed = self.directory / "seed"
        self.checkout = self.directory / "statute2md"
        self.git(self.directory, "init", "--bare", str(self.remote))
        self.git(self.directory, "init", "-b", "main", str(self.seed))
        self.git(self.seed, "config", "user.name", "Test")
        self.git(self.seed, "config", "user.email", "test@example.org")
        (self.seed / "README.md").write_text("Original code")
        for name in ("xml-sources", "md_files"):
            (self.seed / name).mkdir()
            (self.seed / name / ".gitkeep").touch()
        self.commit_seed()
        self.git(self.directory, "clone", "--branch", "main", str(self.remote), str(self.checkout))
        code = "".join(json.loads((ROOT / "statute2md.ipynb").read_text())["cells"][2]["source"])
        self.namespace = {}
        exec(code.rsplit("statute_gui = start_statute2md()", 1)[0], self.namespace)
        self.update = self.namespace["update_code_checkout"]

    def git(self, directory, *args):
        return subprocess.check_output(["git", "-C", str(directory), *args], text=True, stderr=subprocess.PIPE).strip()

    def commit_seed(self):
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "Change")
        self.git(self.seed, "push", str(self.remote), "main")

    def replace_history(self):
        (self.seed / "README.md").write_text("Current code")
        self.git(self.seed, "add", ".")
        tree = self.git(self.seed, "write-tree")
        commit = self.git(self.seed, "commit-tree", tree, "-m", "New root")
        self.git(self.seed, "update-ref", "refs/heads/main", commit)
        self.git(self.seed, "push", "--force", str(self.remote), "main")

    def test_replaced_history_preserves_all_old_files_and_imports_user_data(self):
        self.git(self.checkout, "-c", "user.name=Test", "-c", "user.email=test@example.org",
                 "commit", "--allow-empty", "-m", "Local commit")
        old_head = self.git(self.checkout, "rev-parse", "HEAD")
        (self.checkout / "xml-sources" / "sgg.zip").write_bytes(b"original zip")
        (self.checkout / "md_files" / "sgg.md").write_text("User export")
        (self.checkout / "notes.txt").write_text("Untracked notes")
        (self.checkout / "README.md").write_text("Local code edits")
        self.replace_history()
        self.update(self.checkout, str(self.remote), dict(os.environ))
        self.assertEqual((self.checkout / "README.md").read_text(), "Current code")
        self.assertEqual((self.checkout / "xml-sources" / "sgg.zip").read_bytes(), b"original zip")
        self.assertEqual((self.checkout / "md_files" / "sgg.md").read_text(), "User export")
        backup, = self.directory.glob("statute2md-recovery-*/previous")
        self.assertEqual(self.git(backup, "rev-parse", "HEAD"), old_head)
        self.assertEqual((backup / "notes.txt").read_text(), "Untracked notes")
        self.assertEqual((backup / "README.md").read_text(), "Local code edits")
        self.assertEqual(self.git(self.checkout, "branch", "--show-current"), "main")
        self.update(self.checkout, str(self.remote), dict(os.environ))
        self.assertEqual(len(list(self.directory.glob("statute2md-recovery-*"))), 1)

    def test_normal_fast_forward_does_not_replace_checkout(self):
        (self.seed / "README.md").write_text("Updated code")
        self.commit_seed()
        (self.checkout / "md_files" / "local.md").write_text("User export")
        self.update(self.checkout, str(self.remote), dict(os.environ))
        self.assertEqual((self.checkout / "README.md").read_text(), "Updated code")
        self.assertEqual((self.checkout / "md_files" / "local.md").read_text(), "User export")
        self.assertFalse(list(self.directory.glob("statute2md-recovery-*")))

    def test_failed_fetch_shows_git_error_and_preserves_checkout(self):
        head = self.git(self.checkout, "rev-parse", "HEAD")
        with self.assertRaisesRegex(RuntimeError, "Git fehlgeschlagen.*128"):
            self.update(self.checkout, str(self.directory / "missing.git"), dict(os.environ))
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), head)
        self.assertFalse(list(self.directory.glob("statute2md-recovery-*")))

    def test_failed_clone_and_failed_directory_swap_preserve_original(self):
        self.replace_history()
        old_head = self.git(self.checkout, "rev-parse", "HEAD")
        original_run = subprocess.run
        def fail_clone(command, **kwargs):
            if command[:2] == ["git", "clone"]:
                return subprocess.CompletedProcess(command, 128, "", "Clone failed")
            return original_run(command, **kwargs)
        with patch("subprocess.run", side_effect=fail_clone):
            with self.assertRaisesRegex(RuntimeError, "Clone failed"):
                self.update(self.checkout, str(self.remote), dict(os.environ))
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), old_head)
        rename = Path.rename
        def fail_swap(path, target):
            if path.name == "checkout":
                raise OSError("Directory swap failed")
            return rename(path, target)
        with patch.object(Path, "rename", fail_swap):
            with self.assertRaisesRegex(OSError, "Directory swap failed"):
                self.update(self.checkout, str(self.remote), dict(os.environ))
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), old_head)

    def test_git_errors_redact_token_and_encoded_authorization(self):
        token = "test-secret-token"
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        environment = dict(os.environ, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
                           GIT_CONFIG_VALUE_0=f"Authorization: Basic {encoded}")
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 128, "", f"denied {token} {encoded}")):
            with self.assertRaises(RuntimeError) as error:
                self.update(self.checkout, str(self.remote), environment)
        self.assertNotIn(token, str(error.exception))
        self.assertNotIn(encoded, str(error.exception))


if __name__ == "__main__":
    unittest.main()
