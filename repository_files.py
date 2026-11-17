"""Gesetzesdateien in der Arbeitskopie verwalten und gemeinsam exportieren."""

from contextlib import contextmanager
from collections import Counter
import json
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory

from statute_export import (read_project, render_export, write_json, zip_file, write_bundles,
                           inspect_markdown, check_link_index)
from statute_source import MiB, SLUG, ExportError, acquire, digest, download, load_source, source_url, unpack


@contextmanager
def edit_directory(folder):
    """Erst das vollständige Ergebnis bereitstellen, dann den alten Ordner austauschen."""
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise ExportError(f"{folder.name} ist kein regulärer Ordner.")
    with TemporaryDirectory(prefix=".statute2md-", dir=folder.parent) as temporary:
        stage = Path(temporary) / "new"
        backup = Path(temporary) / "previous"
        if folder.exists():
            if any(path.is_symlink() for path in folder.rglob("*")):
                raise ExportError(f"{folder.name} enthält einen symbolischen Link.")
            shutil.copytree(folder, stage)
        else:
            stage.mkdir()
        yield stage
        if folder.exists():
            folder.rename(backup)
        try:
            stage.rename(folder)
        except OSError:
            if backup.exists():
                backup.rename(folder)
            raise


class RepositoryFiles:
    def __init__(self, root, *, fetch=download):
        self.root = Path(root).resolve()
        self.xml = self.root / "xml-sources"
        self.md = self.root / "md_files"
        self.fetch = fetch

    def files(self, kind):
        folder = self.xml if kind == "xml" else self.md
        if folder.is_symlink():
            raise ExportError(f"{folder.name} darf kein symbolischer Link sein.")
        return sorted(p.name for p in folder.glob(f"*.{kind}") if p.is_file() and not p.is_symlink())

    def selected(self, kind, names):
        names = tuple(dict.fromkeys(names))
        if set(names) - set(self.files(kind)):
            raise ExportError("Die Auswahl enthält inzwischen verschobene oder gelöschte Dateien. Aktualisiere die Liste.")
        folder = self.xml if kind == "xml" else self.md
        return [folder / name for name in names]

    def add_source(self, value, log, upload=None):
        source = acquire(value, upload, log, self.fetch)
        provenance = source.provenance
        xml_bytes = unpack(source.archive)[provenance["member"]]
        with edit_directory(self.xml) as stage:
            archive = stage / "_original"
            archive.mkdir(exist_ok=True)
            (stage / f"{source.slug}.xml").write_bytes(xml_bytes)
            (archive / f"{source.slug}.zip").write_bytes(source.archive)
            write_json(archive / f"{source.slug}.json", provenance)
        return f"{source.slug}.xml gespeichert. Wähle die Datei für Transform aus."

    def read_source(self, path, log, workspace):
        slug = path.stem
        if not re.fullmatch(SLUG, slug, re.ASCII):
            raise ExportError(f"{path.name} benötigt einen kanonischen Slug als Dateinamen.")
        with path.open("rb") as handle:
            xml = handle.read(64 * MiB + 1)
        if len(xml) > 64 * MiB:
            raise ExportError(f"{path.name} überschreitet 64 MiB.")
        archived = path.parent / "_original"
        original, metadata = archived / f"{slug}.zip", archived / f"{slug}.json"
        if archived.is_symlink() or original.is_symlink() or metadata.is_symlink():
            raise ExportError(f"{path.name} verweist auf einen symbolischen Link.")
        if original.exists() or metadata.exists():
            try:
                with metadata.open("rb") as handle:
                    provenance = json.loads(handle.read(MiB + 1))
                with original.open("rb") as handle:
                    archive = handle.read(32 * MiB + 1)
                if digest(xml) != provenance["xml_sha256"] or digest(archive) != provenance["zip_sha256"]:
                    raise ValueError
                if provenance["url"] and source_url(provenance["url"]).path != f"/{slug}/xml.zip":
                    raise ValueError
                source = load_source(slug, archive, log, url=provenance["url"], expected=provenance["expected_alias"],
                                     catalog_titles=provenance["catalog_titles"])
                if source.provenance != provenance:
                    raise ValueError
                return source
            except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
                raise ExportError(f"{path.name} und sein Originalarchiv stimmen nicht überein. Importiere die Quelle erneut.") from error
        archive_path = workspace / f"{slug}.zip"
        zip_file(archive_path, {path.name: xml})
        with archive_path.open("rb") as handle:
            return load_source(slug, handle.read(32 * MiB + 1), log, expected=slug)

    def previous(self, log):
        project = self.md / "statute2md-quellprojekt.zip"
        index = self.md / "link-index.json"
        if project.is_symlink() or index.is_symlink() or self.md.is_symlink():
            raise ExportError("Der Markdown-Bestand enthält einen symbolischen Link.")
        sources = {}
        if project.is_file():
            with project.open("rb") as handle:
                sources = read_project(handle.read(96 * MiB + 1), log)
        previous = json.loads(index.read_bytes()).get("targets", []) if sources and index.is_file() else []
        return sources, previous

    def publish(self, stage, export, old_sources):
        for slug in old_sources:
            (stage / f"{slug}.md").unlink(missing_ok=True)
        for name in ("link-index.json", "statute2md-bestand.zip", "statute2md-quellprojekt.zip"):
            (stage / name).unlink(missing_ok=True)
        if old_sources and (stage / "assets").is_dir():
            shutil.rmtree(stage / "assets")
        if export is not None:
            for file in (export / "Bundesrecht").iterdir():
                if file.is_dir():
                    shutil.copytree(file, stage / file.name, dirs_exist_ok=True)
                else:
                    shutil.copy2(file, stage / file.name)
            for name in ("link-index.json", "statute2md-bestand.zip", "statute2md-quellprojekt.zip"):
                shutil.copy2(export / name, stage / name)

    def render(self, sources, previous, workspace, log, **options):
        if not sources:
            return None
        if len(sources) > 64 or sum(len(source.archive) for source in sources.values()) > 92 * MiB:
            raise ExportError("Der Quellbestand überschreitet das Bestandslimit.")
        export = workspace / "export"
        export.mkdir()
        render_export(export, sources, previous, log, **options)
        return export

    def combine(self, export, active, selected, log):
        """Nicht ausgewählte Exporte übernehmen; nur Auswahl wurde neu gerendert."""
        if self.md.exists() and any(path.is_symlink() for path in self.md.rglob("*")):
            raise ExportError("Der Markdown-Bestand enthält einen symbolischen Link.")
        retained = set(active) - set(selected)
        fresh = json.loads((export / "link-index.json").read_bytes()) if (export / "link-index.json").is_file() else None
        if retained:
            index_path = self.md / "link-index.json"
            if not index_path.is_file():
                raise ExportError("Der Link-Index des Altbestands fehlt. Wähle alle Gesetze einmal für Transform aus.")
            old = json.loads(index_path.read_bytes())
            if any(old.get("sources", {}).get(slug) != active[slug].provenance for slug in retained):
                raise ExportError("Quellbestand und Link-Index passen nicht zusammen. Wähle alle Gesetze für Transform aus.")
        else:
            old = {}
        index = fresh if fresh is not None else {**old, "targets": [], "references": [], "diagnostics": [], "changes": []}
        for field, key in (("targets", "law"), ("references", "source_law"), ("diagnostics", "law")):
            index[field].extend(item for item in old.get(field, []) if item[key] in retained)
        for ref in index["references"]:
            if ref["status"] == "resolved":
                ref.update(status="unchecked", reason="Ungeprüfter Vorratslink aus einem früheren Export",
                           retained_legacy=True, candidates=[])
                ref.pop("range_members", None)
        index["targets"].sort(key=lambda item: (item["law"], item["markdown_line"] or 0, item["id"]))
        index["references"].sort(key=lambda item: (item["source_law"], item["xml_path"], item["offsets"]))
        index["diagnostics"].sort(key=lambda item: (item["law"], item["xml_path"], item["code"]))
        index["sources"] = {slug: source.provenance for slug, source in sorted(active.items())}
        index["reference_counts"] = dict(sorted(Counter(ref["status"] for ref in index["references"]).items()))
        index["status"] = "limited" if any(d["code"] in {"missing_asset", "unknown_tag"} for d in index["diagnostics"]) else "complete"
        index["link_mode"] = "unchecked"
        for slug in sorted(retained):
            shutil.copy2(self.md / f"{slug}.md", export / "Bundesrecht" / f"{slug}.md")
        assets = self.md / "assets"
        if assets.is_dir():
            for child in assets.iterdir():
                if child.name in retained:
                    shutil.copytree(child, export / "Bundesrecht" / "assets" / child.name, dirs_exist_ok=True)
        write_json(export / "link-index.json", index)
        # Der neue Teil wurde bereits gegen XML geprüft. Für unverändert übernommene
        # Dateien und den Gesamtindex werden Blockpositionen und Linktexte geprüft;
        # die Existenz zitierter Ziele wird ausdrücklich nicht geprüft.
        paths = {f"Bundesrecht/{slug}.md": [] for slug in active}
        anchors, actual = inspect_markdown(export, index, paths)
        check_link_index(index, anchors, actual)
        log("Bestand", f"{len(selected)} Gesetz(e) neu gerendert; {len(retained)} unverändert übernommen. Alle ausgegebenen Links sind ungeprüft.")
        write_bundles(export, active)
        return export

    def transform(self, names, log, *, progress=lambda percent, message: None):
        selection = self.selected("xml", names)
        if not selection:
            raise ExportError("Wähle mindestens eine XML-Datei aus.")
        progress(0, "Bisherigen Bestand laden")
        old_sources, previous = self.previous(log)
        active = {slug: source for slug, source in old_sources.items() if (self.md / f"{slug}.md").is_file()}
        with TemporaryDirectory(prefix=".statute2md-", dir=self.root) as temporary:
            workspace = Path(temporary)
            chosen = {}
            for position, path in enumerate(selection):
                progress(3 + 12 * position // len(selection), f"XML lesen · {path.name} · {position + 1}/{len(selection)}")
                source = self.read_source(path, log, workspace)
                active[source.slug] = source
                chosen[source.slug] = source
            def render_progress(percent, message):
                progress(15 + 82 * percent // 100, message)

            if len(active) > 64 or sum(len(source.archive) for source in active.values()) > 92 * MiB:
                raise ExportError("Der Quellbestand überschreitet das Bestandslimit.")
            export = self.render(chosen, [t for t in previous if t["law"] in chosen], workspace, log, progress=render_progress)
            export = self.combine(export, active, chosen, log)
            progress(98, "Geprüfte Dateien in md_files übernehmen")
            with edit_directory(self.md) as stage:
                self.publish(stage, export, old_sources)
        progress(100, "Export abgeschlossen")
        return f"{len(selection)} XML-Datei(en) transformiert. {len(active) - len(chosen)} vorhandene(s) Gesetz(e) unverändert. Links sind ungeprüfte Vorratslinks."

    def delete(self, xml_names, md_names, log):
        xml = self.selected("xml", xml_names)
        md = self.selected("md", md_names)
        if not xml and not md:
            raise ExportError("Wähle mindestens eine Datei zum Löschen aus.")
        if md:
            old_sources, previous = self.previous(log)
            removed = {path.stem for path in md}
            kept = {slug: source for slug, source in old_sources.items()
                    if slug not in removed and (self.md / f"{slug}.md").is_file()}
            with TemporaryDirectory(prefix=".statute2md-", dir=self.root) as temporary:
                export = None
                if kept:
                    export = Path(temporary) / "export"
                    (export / "Bundesrecht").mkdir(parents=True)
                    export = self.combine(export, kept, {}, log)
                with edit_directory(self.md) as stage:
                    if old_sources:
                        self.publish(stage, export, old_sources)
                    for path in md:
                        (stage / path.name).unlink(missing_ok=True)
        if xml:
            with edit_directory(self.xml) as stage:
                for path in xml:
                    (stage / path.name).unlink()
                    (stage / "_original" / f"{path.stem}.zip").unlink(missing_ok=True)
                    (stage / "_original" / f"{path.stem}.json").unlink(missing_ok=True)
        return f"{len(xml)} XML- und {len(md)} Markdown-Datei(en) gelöscht."

    def download(self, xml_names, md_names, folder):
        xml = self.selected("xml", xml_names)
        md = self.selected("md", md_names)
        if not xml and not md:
            raise ExportError("Wähle mindestens eine Datei zum Herunterladen aus.")
        if len(xml) + len(md) == 1:
            return (xml + md)[0]
        path = Path(folder) / "statute2md-auswahl.zip"
        files = {f"{file.parent.name}/{file.name}": file.read_bytes() for file in xml + md}
        zip_file(path, files)
        return path
