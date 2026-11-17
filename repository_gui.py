"""Notebook-Oberfläche für Arbeitskopie, Quellen und Markdown-Exporte."""

from datetime import datetime
from html import escape
from pathlib import Path
from queue import Queue
from tempfile import TemporaryDirectory
from threading import Thread

import ipywidgets as widgets
from IPython.display import FileLink, display

from repository_files import RepositoryFiles
from repository_git import RepositoryGit
from statute_source import BASE, ExportError, MiB

NOTEBOOK_GUI_API = 15

try:
    from google.colab import files as colab_files, output as colab_output
except ImportError:
    colab_files = None
else:
    colab_output.enable_custom_widget_manager()


class RepositoryGUI:
    def __init__(self, root, *, token=None, email=None, username="rps007"):
        self.repo = RepositoryFiles(root)
        self.git = RepositoryGit(self.repo.root, token=token, email=email, username=username, log=self.record)
        self.running = False
        self.temp = TemporaryDirectory(prefix=".statute2md-download-", dir=self.repo.root)
        self.xml = widgets.SelectMultiple(rows=5, layout=widgets.Layout(width="100%"))
        self.md = widgets.SelectMultiple(rows=5, layout=widgets.Layout(width="100%"))
        self.abbr = widgets.Text(description="Kürzel", placeholder="GG, BGB oder SGB I")
        self.zip_path = widgets.Text(description="ZIP-Pfad", placeholder="Dateiname oder vollständiger Pfad")
        self.message = widgets.Text(description="Commit", placeholder="Commit-Nachricht")
        self.status = widgets.HTML()
        self.transform_bar = widgets.IntProgress(min=0, max=100, layout=widgets.Layout(width="100%"))
        self.transform_message = widgets.HTML("Wähle die XML-Dateien für den Export.")
        self.log = widgets.Output(layout=widgets.Layout(height="clamp(320px, 65vh, 640px)", overflow="auto",
                                                         border="1px solid #b8bec6"))
        self.links = widgets.Output()
        self.buttons = []
        # In Colab Git samt Abschlussmeldung im Widget-Callback ausführen.
        clone_row = widgets.HBox([self.button("Clone", self.clone)],
                                 layout=widgets.Layout(flex_flow="row wrap"))
        commit_row = widgets.HBox([self.message, self.button("Commit", lambda: self.git.commit(self.message.value)),
                                   self.button("Push", self.push)], layout=widgets.Layout(flex_flow="row wrap"))
        source_row = widgets.HBox([self.abbr, self.zip_path, self.button("Add Sources", self.add)],
                                  layout=widgets.Layout(flex_flow="row wrap"))
        actions = widgets.GridBox([self.button("Transform", self.transform, style="primary"),
                                   self.button("Delete", self.delete),
                                   self.button("Download", self.download),
                                   self.button("Obsidian-ZIP", self.download_bundle),
                                   self.button("Quellprojekt-ZIP", self.download_project),
                                   self.button("Refresh", self.refresh)],
                                  layout=widgets.Layout(grid_template_columns="repeat(3, minmax(0, 1fr))",
                                                        grid_gap="4px"))
        for button in actions.children:
            button.layout = widgets.Layout(width="auto", min_width="0", margin="0")
        lists = widgets.HBox([widgets.VBox([widgets.HTML("<b>xml-sources</b>"), self.xml], layout=widgets.Layout(width="50%")),
                             widgets.VBox([widgets.HTML("<b>md_files</b>"), self.md], layout=widgets.Layout(width="50%"))])
        # Colab can omit Tab children even when the containing VBox renders.
        sections = [widgets.VBox([widgets.HTML(f"<h3>{title}</h3>"), *children],
                                 layout=widgets.Layout(border="1px solid #b8bec6", padding="0 12px 10px", margin="0 0 8px"))
                    for title, children in ((self.source_title(), (source_row,)),
                                            ("Dateioperationen auf Colab", (lists, actions, self.transform_bar, self.transform_message)),
                                            ("Anbindung an Github", (clone_row, commit_row)))]
        self.source_heading = sections[0].children[0]
        self.controls = [self.xml, self.md, self.abbr, self.zip_path, self.message, *self.buttons]
        controls = widgets.VBox([*sections[:2], self.status, self.links, sections[-1]])
        log_panel = widgets.VBox([widgets.HTML("<h3>Protokoll</h3>"), self.log])
        columns = widgets.GridBox([controls, log_panel], layout=widgets.Layout(
            grid_template_columns="minmax(0, 2fr) minmax(0, 1fr)", grid_gap="12px", align_items="flex-start"))
        self.panel = widgets.VBox([widgets.HTML("<h2>xml2obsidian.ipynb with github connection</h2>"), columns],
                                  layout=widgets.Layout(width="100%"))
        self.status.value = escape(self.refresh())

    def source_title(self):
        url = BASE + "/"
        return (f'XML von <a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a> herunterladen / '
                f'Lokale ZIP-Files in {escape(str(self.repo.xml))} hochladen')

    def button(self, label, action, *, style=""):
        button = widgets.Button(description=label, tooltip=label, button_style=style)
        button.on_click(lambda _: self.start(label, action))
        self.buttons.append(button)
        return button

    def refresh(self):
        for control, kind in ((self.xml, "xml"), (self.md, "md")):
            selected = control.value
            names = self.repo.files(kind)
            control.options = names
            control.value = tuple(name for name in selected if name in names)
        return f"{len(self.xml.options)} XML- und {len(self.md.options)} Markdown-Datei(en)."

    def record(self, stage, message):
        self.status.value = f"<b>{escape(stage)}</b> · {escape(message)}"
        self.log.append_stdout(f"{datetime.now():%H:%M:%S} · {stage} · {message}\n")

    def start(self, label, action):
        if self.running:
            return
        self.running = True
        if label == "Transform":
            self.transform_bar.bar_style = "info"
            self.set_transform_progress(0, "Transformation startet")
        for widget in self.controls:
            widget.disabled = True
        self.status.value = f"<b>{escape(label)}</b> läuft …"
        self.record(label, "Gestartet.")
        self.run(label, action)

    def run(self, label, action):
        try:
            message = action()
            self.refresh()
            if label == "Transform":
                self.transform_bar.bar_style = "success"
                message = "Transformation abgeschlossen. " + message
                self.set_transform_progress(100, message)
            self.record("Fertig", message)
        except Exception as error:
            message = str(error) or type(error).__name__
            if label == "Transform":
                self.transform_bar.bar_style = "danger"
                self.set_transform_progress(self.transform_bar.value, "Transformation abgebrochen. " + message)
            self.record("Abbruch", message)
            self.status.value = "<b>Abbruch</b> · " + escape(message)
            if archive := getattr(error, "archive_url", None):
                self.status.value += (f'<br><a href="{escape(archive, quote=True)}" target="_blank" rel="noopener">'
                                      "Original-ZIP im Browser öffnen</a>. In der Colab-Dateileiste hochladen und den ZIP-Pfad eintragen.")
        finally:
            for widget in self.controls:
                widget.disabled = False
            self.running = False

    def set_transform_progress(self, percent, message):
        self.transform_bar.value = percent
        self.transform_message.value = f"<b>{percent} %</b> · {escape(message)}"

    def transform(self):
        names = self.xml.value
        return self.run_worker(lambda log, progress: self.repo.transform(names, log, progress=progress))

    def delete(self):
        xml, md = self.xml.value, self.md.value
        return self.run_worker(lambda log, _: self.repo.delete(xml, md, log))

    def run_worker(self, action):
        # Keep the widget callback active and publish widget updates from that callback.
        # Colab can drop comm updates made by a detached thread after the callback returns.
        events = Queue()

        def process():
            try:
                result = action(lambda *args: events.put(("log", args)),
                                lambda *args: events.put(("progress", args)))
            except BaseException as error:
                events.put(("error", error))
            else:
                events.put(("result", result))

        Thread(target=process, name="statute2md-worker", daemon=True).start()
        while True:
            kind, value = events.get()
            if kind == "log":
                self.record(*value)
            elif kind == "progress":
                self.set_transform_progress(*value)
            elif kind == "error":
                raise value
            else:
                return value

    def add(self):
        upload = None
        if self.zip_path.value.strip():
            path = Path(self.zip_path.value.strip()).expanduser()
            if path.parent == Path(".") and not path.is_absolute():
                uploaded = self.repo.xml / path
                if uploaded.is_file() or not path.is_file():
                    path = uploaded
            with path.open("rb") as handle:
                upload = handle.read(32 * MiB + 1)
        result = self.repo.add_source(self.abbr.value, self.record, upload)
        self.zip_path.value = ""
        return result

    def clone(self):
        if colab_files is not None and not self.git.token:
            raise ExportError("Gib dem Notebook Zugriff auf das Colab-Secret gittoken und führe die Startzelle erneut aus.")
        return self.git.clone_or_pull()

    def send(self, path):
        if not path.is_file():
            raise ExportError("Es gibt noch keinen Export. Führe zuerst Transform aus.")
        if colab_files is not None:
            colab_files.download(str(path))
        else:
            with self.links:
                try:
                    path = path.relative_to(Path.cwd())
                except ValueError:
                    pass
                display(FileLink(str(path)))
        return f"{path.name} · {path}"

    def download(self):
        markdown = self.md.value
        xml = () if len(markdown) == 1 else self.xml.value
        return self.send(self.repo.download(xml, markdown, self.temp.name))

    def download_bundle(self):
        return self.send(self.repo.md / "statute2md-bestand.zip")

    def download_project(self):
        return self.send(self.repo.md / "statute2md-quellprojekt.zip")

    def push(self):
        if colab_files is not None and not self.git.token:
            raise ExportError("Gib dem Notebook Zugriff auf das Colab-Secret gittoken und führe die Startzelle erneut aus.")
        return self.git.push()

    def close(self):
        self.panel.close()
        for control in (*self.controls, self.transform_bar, self.transform_message):
            control.close()
        self.temp.cleanup()


def start(root, *, token=None, email=None, username="rps007", log_outputs=()):
    gui = RepositoryGUI(root, token=token, email=email, username=username)
    gui.log.outputs = log_outputs
    display(gui.panel)
    return gui
