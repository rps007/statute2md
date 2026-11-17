"""Markdown, unabhängige Exportprüfung und transaktionaler Sitzungsbestand."""

import html
import json
import re
import tempfile
import threading
import zipfile
from bisect import bisect_right
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

from statute_links import ANCHOR_VERSION, SCHEMA_VERSION, Registry, flow_targets, references, target
from statute_model import PARAGRAPH, STYLES, WRAPPERS, Flow, build_flow, plain, read_records
from statute_source import MiB, SLUG, ExportError, acquire, clean_input, digest, download, load_source, source_url, unpack


ID = r"[a-z0-9]+(?:-[a-z0-9]+)*"
BLOCK_ID = re.compile(rf"(?:^| +)\^({ID})$")
SECTION_HEADING = re.compile(r"^#{2,6} \S")
LINK = re.compile(rf"(?<!!)\[\[(?P<file>Bundesrecht/{SLUG})?#\^(?P<id>{ID})\|(?P<alias>[^\[\]\n]+)\]\]", re.ASCII)
ASSET = re.compile(rf"!\[\[(Bundesrecht/assets/{SLUG}/[a-f0-9]+\.(?:png|jpg|gif|webp))\]\]", re.ASCII)
ORDERED_LIST_START = re.compile(r"^([0-9]{1,9})([.)])(?=\s|$)")
DASH_MARKER = re.compile(r"^-(?=[ \t]|-*$)")
LABEL_START = '<span style="color:var(--text-error,#c52d3d)">'
PARAGRAPH_LABEL = re.compile(rf"^{re.escape(LABEL_START)}<strong>Absatz ([0-9]+[a-zA-Z]?)</strong></span>$")


def escaped(text):
    controls = set("\\`*_{}[]<>#+!|~^=$%")
    return "".join(f"&#{ord(c)};" if c in controls or ord(c) < 32 else "&amp;" if c == "&" else c for c in text)


def paragraph(text):
    # Protect source text that would start a list, a rule, or a Setext underline.
    text = DASH_MARKER.sub("&#45;", text)
    return ORDERED_LIST_START.sub(lambda m: m[1] + f"&#{ord(m[2])};", text)


def inline(text, styles=()):
    text = escaped(text)
    for tag in reversed(styles):
        if tag in {"em", "strong", "sup", "sub"}:
            text = f"<{tag}>{text}</{tag}>"
    if "list-label" in styles and text.strip():
        return LABEL_START + text + "</span>"
    return text


def literal_text(text):
    return "".join(f"&#{ord(c)};" if ord(c) < 32 and c not in "\n\t" else html.escape(c) for c in text)


def wiki(reference):
    target_file = reference["target_path"][:-3] if reference["target_path"] != f"Bundesrecht/{reference['source_law']}.md" else ""
    return f"[[{target_file}#^{reference['target_id']}|{reference['original']}]]"


def diagnostic(law, code, label, path, message):
    return {"law": law, "code": code, "label": label, "xml_path": path, "message": message}


def asset_label(node):
    return next((node.get(name) for name in ("alt", "Alt", "src", "file", "href", "name", "ref") if node.get(name)), "Abbildung")


def visible_xml(node):
    if node is None:
        return ""
    result = [asset_label(node)] if node.tag == "IMG" else []
    result.append(node.text or "")
    for child in node:
        result.extend((visible_xml(child), child.tail or ""))
    return "".join(result)


def structure(node, paths, record, warn, files, source):
    """Erzeugt HTML ausschließlich mit fest definierten Tags und numerischen Spans."""
    tag = node.tag

    def children(specs=None, section=""):
        return inline(node.text or "") + "".join(
            structure(child, paths, record, warn, files, source) + inline(child.tail or "")
            if specs is None else table_node(child, specs, section) + inline(child.tail or "") for child in node)

    def table_node(element, specs, section):
        key = element.tag
        inside = inline(element.text or "") + "".join(
            table_node(child, specs, key if key in {"thead", "tbody", "tfoot"} else section) + inline(child.tail or "")
            for child in element)
        if key in {"thead", "tbody", "tfoot", "row"}:
            name = "tr" if key == "row" else key
            return f"<{name}>{inside}</{name}>"
        if key == "entry":
            start, stop = specs.get(element.get("namest")), specs.get(element.get("nameend"))
            attrs = ""
            if start is not None and stop is not None and 0 <= stop - start < 100:
                attrs += f' colspan="{stop - start + 1}"'
            if re.fullmatch(r"[0-9]{1,3}", element.get("morerows", "")):
                attrs += f' rowspan="{int(element.get("morerows")) + 1}"'
            name = "th" if section == "thead" else "td"
            return f"<{name}{attrs}>{inside}</{name}>"
        if key in {"colspec", "spanspec"}:
            return inside
        return structure(element, paths, record, warn, files, source) if key != "tgroup" else inside

    if tag == "table":
        table = [inline(node.text or "")]
        for group in node:
            specs = {col.get("colname"): i for i, col in enumerate(group.findall("colspec"), 1)}
            table.append(table_node(group, specs, "") + inline(group.tail or ""))
        return "<table>" + "".join(table) + "</table>"
    if tag == "IMG":
        label = asset_label(node)
        uri = next((node.get(key) for key in ("src", "file", "href", "name", "ref") if node.get(key)), "")
        matches = [key for key in source.assets if key == uri or PurePosixPath(key).name == uri]
        payload = source.assets[matches[0]] if len(matches) == 1 else b""
        extension = next((ext for ext, signatures in {"png": (b"\x89PNG\r\n\x1a\n",), "jpg": (b"\xff\xd8\xff",),
                                                       "gif": (b"GIF87a", b"GIF89a"), "webp": (b"RIFF",)}.items()
                          if payload.startswith(signatures) and (ext != "webp" or payload[8:12] == b"WEBP")), None)
        text = inline(label) + children()
        if extension and len(payload) <= 16 * MiB:
            path = f"Bundesrecht/assets/{source.slug}/{digest(payload)}.{extension}"
            files[path] = payload
            return f"{text}\n\n![[{path}]]"
        warn("missing_asset", record.label, paths[node], f"Abbildung ohne verfügbares Raster-Asset: {label}")
        return text
    if tag == "BR":
        return "<br>" + children()
    if tag == "pre":
        def preserve(element):
            prefix = "\n" if element.tag == "BR" else ""
            return prefix + literal_text(element.text or "") + "".join(
                preserve(child) + literal_text(child.tail or "") for child in element)

        return "<pre>" + preserve(node) + "</pre>"
    if tag == "TOC":
        return "<div>" + children() + "</div>"
    tags = {"Title": "p", "P": "p", "DT": "dt", "DD": "dd", "DL": "dl", **STYLES}
    if tag in tags:
        name = tags[tag]
        return f"<{name}>{children()}</{name}>"
    if tag not in WRAPPERS:
        warn("unknown_tag", record.label, paths[node], f"Text von <{tag}> erhalten. Struktur wird neutral ausgegeben.")
    return children()


def render_flow(flow, markers, refs, source, record, paths, warn, assets, offset=0):
    text = flow.text
    by_position = defaultdict(list)
    for position, item in markers:
        by_position[position].append(item)
    paragraph_markers = {start: length for start, _, _, length in flow.paragraphs}
    breaks = {0, len(text), *by_position, *flow.blocks}
    breaks.update(position for p in flow.pieces if p.styles == ("layout",) and p.text == "\n" for position in (p.start, p.end))
    breaks.update(position + 1 for position in flow.blocks)
    direct = {}
    extras = defaultdict(list)
    pieces = flow.pieces
    piece_starts = [piece.start for piece in pieces]

    def source_runs(a, b):
        for piece in pieces[max(0, bisect_right(piece_starts, a) - 1):]:
            if piece.start >= b:
                break
            if piece.end > a:
                yield piece, text[max(a, piece.start):min(b, piece.end)]

    sorted_breaks = sorted(breaks)
    for ref in refs:
        if ref["status"] != "resolved":
            continue
        a, b = (x - offset for x in ref["offsets"])
        next_break = sorted_breaks[bisect_right(sorted_breaks, a)]
        styling = {tuple(x for x in piece.styles if x in {"em", "strong", "sup", "sub"}) for piece, _ in source_runs(a, b)}
        if next_break < b or len(styling) > 1:
            extras[next_break].append(ref)
        else:
            direct[a] = (b, ref, next(iter(styling), ()))
    direct_starts = sorted(direct)
    lines = []
    source_line_indices = []
    auxiliary = []

    def push(line, source_line=False):
        if line:
            if source_line:
                source_line_indices.append(len(lines))
            lines.extend((line, ""))

    def add_extras(pos):
        for ref in extras.get(pos, ()):
            push("Verweis: " + wiki(ref))

    for a, b in zip(sorted_breaks, sorted_breaks[1:]):
        add_extras(a)
        landings = by_position.get(a, [])
        if a in flow.blocks:
            node, _ = flow.blocks[a]
            for item in landings:
                push(paragraph(escaped(item["label"])) + " ^" + item["id"])
            if node.tag == "TOC":
                push("**Quellen-Inhaltsverzeichnis**")
            push(structure(node, paths, record, warn, assets, source), True)
            detail = Flow(paths[node])
            detail.add(visible_xml(node), paths[node])
            auxiliary.append((detail, len(text) + offset + sum(x.length + 1 for x, _ in auxiliary) + 1))
            continue
        content = []
        cursor = a
        marker_length = paragraph_markers.get(a, 0)
        paragraph_target = next((item for item in landings if item["selector"][2::2] == ["abs"]), None)
        marker = PARAGRAPH.match(text[a:a + marker_length]) if paragraph_target and a + marker_length <= b else None
        if paragraph_target and marker:
            push(f'{LABEL_START}<strong>Absatz {marker[1]}</strong></span> ^{paragraph_target["id"]}', True)
            landings = [item for item in landings if item is not paragraph_target]
            cursor += marker.end()
        link_index = bisect_right(direct_starts, a - 1)
        for start in direct_starts[link_index:]:
            if start >= b:
                break
            end, ref, styles = direct[start]
            content.extend(inline(value, piece.styles) for piece, value in source_runs(cursor, start))
            fragment = wiki(ref)
            for name in reversed(styles):
                fragment = f"<{name}>{fragment}</{name}>"
            content.append(fragment)
            cursor = end
        content.extend(inline(value, piece.styles) for piece, value in source_runs(cursor, b))
        rendered = "".join(content).strip() if text[a:b].strip() else ""
        rendered = paragraph(rendered)
        anchored = landings[-1] if rendered and landings else None
        for item in landings[:-1] if anchored else landings:
            push(paragraph(escaped(item["label"])) + " ^" + item["id"])
        if anchored:
            rendered += " ^" + anchored["id"]
        push(rendered, True)
    for item in by_position.get(len(text), ()):
        push(paragraph(escaped(item["label"])) + " ^" + item["id"])
    add_extras(len(text))
    return lines, source_line_indices, auxiliary


def model_sources(sources, log, progress):
    models, targets, diagnostics = {}, [], []
    completed = 0
    total = sum(len(source.root) for source in sources.values())
    for slug, source in sorted(sources.items()):
        log("Gliederung", f"{slug}: Ordne die Datensätze in Quellenreihenfolge.")

        def warn(code, label, path, message, law=slug):
            diagnostics.append(diagnostic(law, code, label, path, message))

        records, paths = read_records(source, warn)
        log("Segmentierung", f"{slug}: Analysiere {len(records)} Datensätze.")
        rows = []
        for record in records:
            text_node = record.node.find("textdaten/text")
            flow = build_flow(record, text_node, paths, warn)
            record_target = target(source, record, record.anchor, record.selector or (("gl", record.key) if record.key else ()),
                                   text=visible_xml(text_node), parent=record.parents[-1].anchor if record.parents else None)
            targets.append(record_target)
            markers = sorted(flow_targets(source, record, flow, warn), key=lambda item: item[0])
            targets.extend(item for _, item in markers)
            rows.append((record, text_node, flow, markers))
            completed += 1
            if completed % 64 == 0 or len(rows) == len(records):
                progress(28 * completed // max(1, total), f"Datensätze analysieren · {slug} · {completed}/{total}")
        models[slug] = (rows, paths)
    return models, targets, diagnostics


def normalized_visible(text):
    # Zeilen-, Absatz- und Tabellengrenzen dürfen Layout-Leerraum hinzufügen.
    return "".join(text.split())


def visible_markdown(text):
    text = ASSET.sub("", text)
    text = LINK.sub(lambda m: m["alias"], text)
    text = BLOCK_ID.sub("", text)
    if marker := PARAGRAPH_LABEL.fullmatch(text):
        return f"({marker[1]})"
    text = text.replace(LABEL_START, "").replace("</span>", "")
    text = re.sub(r"</?(?:table|thead|tbody|tfoot|tr|th|td|p|div|dl|dt|dd|em|strong|sup|sub|pre|br)(?: (?:colspan|rowspan)=\"[0-9]+\")*>", "", text)
    return html.unescape(text)


def changes(previous, current):
    before = {(x["path"], x["id"]): x for x in previous}
    after = {(x["path"], x["id"]): x for x in current}
    result = []
    for key in sorted(before.keys() | after.keys()):
        a, b = before.get(key), after.get(key)
        if a and b and a["selector"] != b["selector"]:
            status = "reassigned"
        elif a and b and a["text_sha256"] != b["text_sha256"]:
            status = "changed"
        elif a and b:
            continue
        else:
            status = "removed" if a else "added"
        result.append({"path": key[0], "id": key[1], "status": status, "before": a and a["selector"], "after": b and b["selector"]})
    return result


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def render_export(directory, sources, previous, log, *, progress=lambda percent, message: None):
    progress(0, "Datensätze analysieren")
    models, targets, diagnostics = model_sources(sources, log, progress)
    progress(29, "Zielregister aufbauen")
    log("Index", f"Baue das Zielregister mit {len(targets)} Blöcken.")
    registry = Registry(sources, targets)
    refs, assets, witnesses, inventories = [], {}, {}, {}
    folder = directory / "Bundesrecht"
    folder.mkdir()
    completed = 0
    total = sum(len(records) for records, _ in models.values())
    for slug, (records, paths) in models.items():
        source = sources[slug]
        meta = source.root.find("norm/metadaten")
        title = next((meta.findtext(key) for key in ("langue", "kurzue", "jurabk") if meta.findtext(key)), slug)
        lines = ["# " + escaped(" ".join(title.split())), ""]
        for stand in source.provenance["stand"]:
            lines.extend(("Stand: " + escaped(stand), ""))
        log("Rendern", f"{slug}: Erzeuge Markdown und prüfbare Verweise.")

        def warn(code, label, path, message, law=slug):
            diagnostics.append(diagnostic(law, code, label, path, message))

        def add_lines(fragments):
            # HTML-Blöcke können mehrere physische Zeilen enthalten.
            lines.extend(fragments)

        checks, texts = [], {}
        last_overflow_path = None
        for record, text_node, flow, markers in records:
            if completed % 64 == 0:
                progress(35 + 45 * completed // max(1, total), f"Markdown erzeugen · {slug} · {completed}/{total}")
            completed += 1
            if record.role != "head":
                depth = len(record.parents) + 2
                heading = (f"{record.label} — {record.title}" if record.selector and record.title
                           else record.heading or record.docid)
                heading_line = "#" * min(6, depth) + " " + escaped(" ".join(heading.split()))
                if record.selector:
                    add_lines([f"{heading_line} ^{record.anchor}", ""])
                else:
                    add_lines([heading_line, "", "^" + record.anchor, ""])
                overflow_path = tuple(p.anchor for p in record.parents) if depth > 6 else None
                if overflow_path is not None and overflow_path != last_overflow_path:
                    parents = " › ".join(" ".join((p.heading or p.docid).split()) for p in record.parents)
                    add_lines(["*Gliederung: " + escaped(parents) + "*", ""])
                last_overflow_path = overflow_path
            else:
                label = record.label or record.title or meta.findtext("jurabk") or record.docid
                add_lines([paragraph(escaped(label)) + " ^" + record.anchor, ""])
            found = references(flow, source, record, registry, markers)
            refs.extend(found)
            texts[flow.path, 0] = flow.text
            rendered, source_indices, auxiliary = render_flow(flow, markers, found, source, record, paths, warn, assets)
            checks.append(([len(lines) + index for index in source_indices], visible_xml(text_node), record.path))
            add_lines(rendered)
            cursor = flow.length + 1
            for detail, offset in auxiliary:
                cursor = max(cursor, offset + detail.length + 1)
                for ref in references(detail, source, record, registry, [], offset=offset):
                    refs.append(ref)
                    if ref["status"] == "resolved":
                        add_lines(["Verweis: " + wiki(ref), ""])
                texts[detail.path, offset] = detail.text
            for field_name in ("enbez", "titel", "gliederungseinheit/gliederungsbez", "gliederungseinheit/gliederungstitel"):
                if field_name == "enbez" and record.selector:
                    continue  # The provision's own identifier is a label, not a cross-reference.
                node = record.node.find(f"metadaten/{field_name}")
                if node is None:
                    continue
                detail = Flow(paths[node])
                detail.add(plain(node), paths[node])
                for ref in references(detail, source, record, registry, [], offset=cursor):
                    refs.append(ref)
                    if ref["status"] == "resolved":
                        add_lines(["Verweis zur Überschrift: " + wiki(ref), ""])
                texts[detail.path, cursor] = detail.text
                cursor += detail.length + 1
            notes = record.node.find("textdaten")
            for note in notes if notes is not None else ():
                if note.tag == "text" or not (plain(note).strip() or len(note)):
                    continue
                add_lines(["**Hinweise und Fußnoten**", ""])
                note_flow = build_flow(record, note, paths, warn, segment=False)
                note_refs = references(note_flow, source, record, registry, [], offset=cursor)
                refs.extend(note_refs)
                texts[note_flow.path, cursor] = note_flow.text
                note_output, indices, note_aux = render_flow(note_flow, [], note_refs, source, record, paths, warn, assets, cursor)
                checks.append(([len(lines) + index for index in indices], visible_xml(note), paths[note]))
                add_lines(note_output)
                cursor += note_flow.length + 1
                for detail, offset in note_aux:
                    cursor = max(cursor, offset + detail.length + 1)
                    for ref in references(detail, source, record, registry, [], offset=offset):
                        refs.append(ref)
                        if ref["status"] == "resolved":
                            add_lines(["Verweis: " + wiki(ref), ""])
                    texts[detail.path, offset] = detail.text
        physical = []
        line_map = []
        for line in lines:
            parts = line.split("\n")
            line_map.append(range(len(physical), len(physical) + len(parts)))
            physical.extend(parts)
        path = f"Bundesrecht/{slug}.md"
        witnesses[path] = [([n for i in selected for n in line_map[i]], expected, where) for selected, expected, where in checks]
        streams = defaultdict(list)
        for (xml_path, offset), text in texts.items():
            streams[xml_path].append((offset, text))
        inventories[slug] = streams
        (directory / path).write_text("\n".join(physical) + "\n", encoding="utf-8", newline="\n")
        progress(35 + 45 * completed // max(1, total), f"Markdown erzeugen · {slug} · {completed}/{total}")
    progress(81, "Block-IDs und Link-Index erfassen")
    for path, payload in assets.items():
        dest = directory / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
    anchor_lines = {}
    for path in witnesses:
        for line_no, line in enumerate((directory / path).read_text(encoding="utf-8").splitlines(), 1):
            if match := BLOCK_ID.search(line):
                anchor_lines[path, match[1]] = line_no
    for item in targets:
        item["markdown_line"] = anchor_lines.get((item["path"], item["id"]))
    limited = any(x["code"] in {"missing_asset", "unknown_tag"} for x in diagnostics)
    index = {"schema_version": SCHEMA_VERSION, "anchor_version": ANCHOR_VERSION,
             "offset_basis": "Unicode-Zeichen im unverlinkten XML-Piece-Text pro XML-Pfad und Streamsegment",
             "status": "limited" if limited else "complete",
             "sources": {slug: source.provenance for slug, source in sorted(sources.items())},
             "targets": targets, "references": refs, "diagnostics": diagnostics,
             "reference_counts": dict(sorted(Counter(x["status"] for x in refs).items())),
             "changes": changes(previous, targets) if previous else []}
    write_json(directory / "link-index.json", index)
    log("Validierung", "Lese Markdown und Link-Index erneut ein.")
    progress(86, "Markdown und Verweise prüfen")
    validate_export(directory, witnesses, inventories, progress)
    progress(96, "Download-ZIPs erstellen")
    write_bundles(directory, sources)
    progress(100, "Export geprüft und gepackt")
    return index


def validate_export(directory, witnesses, inventories, progress=lambda percent, message: None):
    index = json.loads((directory / "link-index.json").read_text(encoding="utf-8"))
    provision_targets = {(x["path"], x["id"]) for x in index["targets"]
                         if len(x["selector"]) == 2 and x["selector"][0] in {"p", "art"}}
    anchors, actual = {}, Counter()
    for completed, (path, checks) in enumerate(witnesses.items(), 1):
        if not re.fullmatch(rf"Bundesrecht/{SLUG}\.md", path, re.ASCII):
            raise ExportError("Ungültiger Pfad im Export.")
        raw = (directory / path).read_bytes()
        if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw:
            raise ExportError(f"UTF-8/LF-Prüfung fehlgeschlagen: {path}.")
        lines = raw.decode("utf-8").split("\n")
        for index_line, line in enumerate(lines):
            if match := BLOCK_ID.search(line):
                key = path, match[1]
                if match.start() == 0:
                    valid_position = (index_line >= 2 and not lines[index_line - 1].strip()
                                      and SECTION_HEADING.match(lines[index_line - 2]))
                elif line.startswith("#"):
                    valid_position = key in provision_targets and SECTION_HEADING.match(line[:match.start()])
                else:
                    valid_position = bool(line[:match.start()].strip())
                if (key in anchors or not valid_position or index_line + 1 == len(lines)
                        or lines[index_line + 1].strip()):
                    raise ExportError(f"Blockposition oder Block-ID ungültig: {path} / {match[1]}.")
                anchors[key] = index_line + 1
            for match in LINK.finditer(line):
                target_path = match["file"] + ".md" if match["file"] else path
                actual[path, target_path, match["id"], match["alias"]] += 1
            for match in ASSET.finditer(line):
                if not (directory / match[1]).is_file():
                    raise ExportError(f"Asset fehlt: {match[1]}.")
        for selected, expected, where in checks:
            reconstructed = "".join(visible_markdown(lines[i]) for i in selected)
            if normalized_visible(reconstructed) != normalized_visible(expected):
                raise ExportError(f"Die sichtbare Ausgabe weicht vom XML-Text ab: {path}, {where}.")
        progress(86 + 7 * completed // len(witnesses), f"Markdown geprüft · {path} · {completed}/{len(witnesses)}")
    recorded = {(x["path"], x["id"]): x["markdown_line"] for x in index["targets"]}
    if len(recorded) != len(index["targets"]) or anchors != recorded:
        raise ExportError("Blockpositionen in Markdown und Link-Index stimmen nicht überein.")
    expected_links = Counter()
    for ref in index["references"]:
        a, b = ref["offsets"]
        streams = inventories[ref["source_law"]][ref["xml_path"]]
        if not any(0 <= a - offset < b - offset <= len(text)
                   and text[a - offset:b - offset] == ref["original"] for offset, text in streams):
            raise ExportError("Ein Index-Verweis stimmt nicht mit seinem Quelltext überein.")
        if ref["status"] == "resolved":
            key = ref["target_path"], ref["target_id"]
            if key not in anchors:
                raise ExportError("Ein ausgegebener Link hat kein gültiges Ziel.")
            expected_links[f"Bundesrecht/{ref['source_law']}.md", *key, ref["original"]] += 1
    if expected_links != actual:
        raise ExportError("Markdown-Links und Link-Index weichen voneinander ab.")


def zip_file(path, files):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            archive.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED)


def write_bundles(directory, sources):
    files = {path.relative_to(directory).as_posix(): path.read_bytes() for path in (directory / "Bundesrecht").rglob("*") if path.is_file()}
    files["link-index.json"] = (directory / "link-index.json").read_bytes()
    zip_file(directory / "statute2md-bestand.zip", files)
    manifest = {"schema_version": SCHEMA_VERSION, "sources": {slug: src.provenance for slug, src in sorted(sources.items())}}
    files = {f"sources/{slug}.zip": src.archive for slug, src in sources.items()}
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
    zip_file(directory / "statute2md-quellprojekt.zip", files)


def read_project(data, log):
    files = unpack(data, project=True)
    try:
        manifest = json.loads(files.pop("manifest.json"))
        declared = manifest["sources"]
        if manifest["schema_version"] != SCHEMA_VERSION or not isinstance(declared, dict) or not declared:
            raise ValueError
        if set(files) != {f"sources/{slug}.zip" for slug in declared}:
            raise ValueError
        sources = {}
        for slug, metadata in sorted(declared.items()):
            archive = files[f"sources/{slug}.zip"]
            url = metadata["url"]
            if url and source_url(url).path != f"/{slug}/xml.zip":
                raise ValueError
            if digest(archive) != metadata["zip_sha256"]:
                raise ValueError
            expected = metadata["expected_alias"]
            clean_input(expected or slug)
            titles = metadata["catalog_titles"]
            if not isinstance(titles, list) or len(titles) > 20 or any(not isinstance(t, str) or len(t) > 4096 for t in titles):
                raise ValueError
            source = load_source(slug, archive, log, url=url, expected=expected, catalog_titles=titles)
            if source.provenance != metadata:
                raise ValueError
            sources[slug] = source
        return sources
    except (KeyError, TypeError, ValueError, UnicodeError, AttributeError) as exc:
        raise ExportError("Der Quellbestand ist ungültig oder passt nicht zu den gespeicherten Quellen.") from exc


class Session:
    def __init__(self, folder=None, fetch=download):
        self.storage = tempfile.TemporaryDirectory(prefix="statute2md-", dir=folder)
        self.fetch = fetch
        self.lock = threading.Lock()
        self.sources = {}
        self.current = None
        self.index = None
        self.last_slug = None
        self.generation = 0

    def process(self, value="", upload=None, *, project=None, log=lambda stage, message: None):
        if not self.lock.acquire(blocking=False):
            raise ExportError("Die vorige Verarbeitung läuft noch.")
        try:
            log("Eingabe", "Beginne die Verarbeitung.")
            new = read_project(project, log) if project is not None else {}
            if project is None:
                source = acquire(value, upload, log, self.fetch)
                new[source.slug] = source
            latest = next(reversed(new))
            candidate = {**self.sources, **new}
            if len(candidate) > 64 or sum(len(src.archive) for src in candidate.values()) > 92 * MiB:
                raise ExportError("Das Quellprojekt überschreitet das Bestandslimit.")
            root = Path(self.storage.name)
            with tempfile.TemporaryDirectory(prefix="lauf-", dir=root) as workspace:
                staged = Path(workspace) / "export"
                staged.mkdir()
                index = render_export(staged, candidate, self.index["targets"] if self.index else [], log)
                complete = root / f"export-{self.generation + 1}"
                staged.rename(complete)
            self.sources, self.current, self.index, self.last_slug = candidate, complete, index, latest
            self.generation += 1
            log("Fertig", f"{len(candidate)} Gesetz(e), {len(index['targets'])} Ziele und {len(index['references'])} erkannte Verweise.")
            return complete, index, latest
        finally:
            self.lock.release()
