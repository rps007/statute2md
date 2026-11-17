"""Optionale Notebook-Abhängigkeiten im laufenden Python bereitstellen."""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.request import urlopen


def ensure_widgets():
    if importlib.util.find_spec("ipywidgets") is not None:
        return
    uv = shutil.which("uv")
    if uv:
        subprocess.run([uv, "pip", "install", "--python", sys.executable, "ipywidgets==8.1.7"], check=True)
        return
    if sys.platform != "linux":
        raise RuntimeError("Installiere uv oder ipywidgets und starte die GUI-Zelle erneut.")
    with TemporaryDirectory(prefix="statute2md-uv-") as folder:
        with urlopen("https://astral.sh/uv/0.8.17/install.sh", timeout=30) as response:
            installer = response.read(512 * 1024 + 1)
        if len(installer) > 512 * 1024:
            raise RuntimeError("Das uv-Installationsskript überschreitet das Größenlimit.")
        environment = dict(os.environ, UV_UNMANAGED_INSTALL=folder)
        subprocess.run(["sh"], input=installer, env=environment, timeout=120, check=True)
        subprocess.run([str(Path(folder) / "uv"), "pip", "install", "--python", sys.executable, "ipywidgets==8.1.7"],
                       timeout=120, check=True)
