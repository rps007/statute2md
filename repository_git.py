"""Git-Aktionen für die Colab-Arbeitskopie."""

import base64
import os
import re
from pathlib import Path
import subprocess

from statute_source import ExportError


REMOTE = "https://github.com/rps007/statute2md.git"
BRANCH = "main"
NOTEBOOK_AUTH_API = 2
GIT_TIMEOUT = 45


def repository_remote(username):
    username = username.strip()
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", username):
        raise ExportError("github_username muss ein gültiger GitHub-Benutzername sein.")
    return f"https://github.com/{username}/statute2md.git"


def remote_environment(token, remote=REMOTE):
    environment = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never",
                       GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=10")
    if token:
        credential = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        count = int(environment.get("GIT_CONFIG_COUNT", "0"))
        environment.update({"GIT_CONFIG_COUNT": str(count + 1),
                            f"GIT_CONFIG_KEY_{count}": f"http.{remote}.extraHeader",
                            f"GIT_CONFIG_VALUE_{count}": f"Authorization: Basic {credential}"})
    return environment


class RepositoryGit:
    def __init__(self, root, *, token=None, email=None, username="rps007", log=lambda stage, message: None):
        self.root = Path(root)
        self.username = username.strip()
        self.remote = repository_remote(self.username)
        self.token = token
        self.email = email
        self.log = log

    def call(self, *args, network=False, check=True):
        location = self.root if self.root.is_dir() else self.root.parent
        command = ["git", "-C", str(location), *args]
        if network:
            self.log("Git", f"{args[0]} · {self.root} · Zeitlimit {GIT_TIMEOUT} Sekunden.")
        try:
            result = subprocess.run(command, check=False, capture_output=True, text=True, errors="replace", timeout=GIT_TIMEOUT,
                                    env=remote_environment(self.token, self.remote) if network else None)
        except subprocess.TimeoutExpired as error:
            raise ExportError(f"Git hat nach {GIT_TIMEOUT} Sekunden abgebrochen. Prüfe die Verbindung zu GitHub.") from error
        except OSError as error:
            raise ExportError("Git konnte nicht gestartet werden.") from error
        if check and result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or "Git meldet einen Fehler."
            raise ExportError(detail)
        return result

    def verify(self):
        self.log("Git", f"Prüfe die Arbeitskopie {self.root}.")
        origin = self.call("remote", "get-url", "origin").stdout.strip().removesuffix(".git")
        if origin not in {self.remote.removesuffix(".git"), f"git@github.com:{self.username}/statute2md"}:
            raise ExportError("Die Arbeitskopie verweist auf ein anderes Repository.")
        if self.call("branch", "--show-current").stdout.strip() != BRANCH:
            raise ExportError(f"Die Arbeitskopie muss auf {BRANCH} stehen.")

    def clone_or_pull(self):
        if not self.root.exists():
            self.call("clone", "--branch", BRANCH, "--", self.remote, str(self.root), network=True)
            return "Repository geklont."
        self.verify()
        self.call("pull", "--ff-only", self.remote, BRANCH, network=True)
        return "Arbeitskopie aktualisiert. Starte bei geändertem Python-Code die Laufzeit neu."

    def commit(self, message):
        if not message.strip():
            raise ExportError("Gib eine Commit-Nachricht ein.")
        self.verify()
        email = self.email
        identity = ["-c", f"user.name={self.username}", "-c", f"user.email={email}"] if email else []
        if not email and not self.call("config", "user.email", check=False).stdout.strip():
            raise ExportError("Hinterlege gitemail in den Colab-Secrets oder konfiguriere die Git-Identität lokal.")
        self.call("add", "--all", "--", "xml-sources", "md_files")
        if not self.call("diff", "--cached", "--name-only", "--", "xml-sources", "md_files").stdout.strip():
            return "Keine Änderungen in xml-sources oder md_files."
        self.call(*identity, "commit", "--only", "-m", message.strip(), "--", "xml-sources", "md_files")
        return "Dateiänderungen eingecheckt."

    def push(self):
        self.verify()
        self.call("push", self.remote, f"{BRANCH}:{BRANCH}", network=True)
        return f"Commits nach {BRANCH} übertragen."
