"""Quellreihenfolge, Gliederung und juristische Textspannen."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import cached_property
import re
import unicodedata
from xml.etree import ElementTree as ET

from statute_source import ExportError, alias_key


def plain(node):
    return "".join(node.itertext()) if node is not None else ""


def xml_paths(root):
    paths = {root: "/dokumente"}
    for parent in root.iter():
        counts = Counter()
        for child in parent:
            counts[child.tag] += 1
            paths[child] = f"{paths[parent]}/{child.tag}[{counts[child.tag]}]"
    return paths


def normalized(text):
    """NFC/Leerraumansicht mit Zeichenintervallen im unverlinkten Original."""
    out, offsets = [], []
    for match in re.finditer(r"\S[\u0300-\u036f]*|[^\S\n]+|\n", text):
        original = match[0]
        value = " " if original[0].isspace() and original[0] != "\n" else unicodedata.normalize("NFC", original)
        value = value.translate(str.maketrans("‐‑‒–−", "-----"))
        out.append(value)
        offsets.extend([match.span()] * len(value))
    return "".join(out), offsets


def encode(value):
    return value if re.fullmatch(r"[a-z0-9]+", value, re.ASCII) else "x-" + value.encode("utf-8").hex()


def norm_selector(label):
    m = re.fullmatch(r"(§|Art\.?|Artikel)\s*(\d+[a-z]?)", alias_key(label), re.I | re.ASCII)
    if m:
        return ("p" if m[1] == "§" else "art", m[2])
    return ()


@dataclass
class Record:
    node: ET.Element
    path: str
    docid: str
    label: str
    title: str
    role: str
    parents: tuple
    key: str = ""
    selector: tuple = ()
    anchor: str = ""

    @property
    def heading(self):
        return " ".join(x for x in (self.label, self.title) if x).strip()


def read_records(source, warn):
    paths = xml_paths(source.root)
    result, stack = [], []
    for index, node in enumerate(source.root):
        meta = node.find("metadaten")
        docid = node.get("doknr", "")
        if meta is None or not docid or not meta.findtext("jurabk"):
            raise ExportError(f"Pflichtmetadaten fehlen unter {paths[node]}.")
        gl = meta.find("gliederungseinheit")
        if gl is not None:
            key = gl.findtext("gliederungskennzahl", "").strip()
            label = plain(gl.find("gliederungsbez")).strip()
            title = plain(gl.find("gliederungstitel")).strip()
            if re.fullmatch(r"(?:[0-9]{3})+", key):
                while stack and (not stack[-1].key or not key.startswith(stack[-1].key) or key == stack[-1].key):
                    stack.pop()
                if (len(stack[-1].key) if stack else 0) != len(key) - 3:
                    warn("missing_gl_parent", label, paths[node], f"Elternebene für {key} fehlt.")
            else:
                key = ""
                stack.clear()
                warn("invalid_gl_key", label, paths[node], "Gliederungstiefe ist nicht belegt.")
            record = Record(node, paths[node], docid, label, title, "gl", tuple(stack), key)
            stack.append(record)
        else:
            label = plain(meta.find("enbez")).strip()
            title = plain(meta.find("titel")).strip()
            role = "head" if index == 0 and not label else "norm"
            if role == "norm" and not (label or title):
                warn("unlabelled_record", docid, paths[node], "Datensatz ohne Kennzeichnung wird über die Dokumentkennung erhalten.")
            record = Record(node, paths[node], docid, label, title, role, tuple(stack), selector=norm_selector(label))
        result.append(record)
    counts = Counter()
    for record in result:
        if record.selector:
            base = "-".join(map(encode, record.selector))
        elif record.key:
            base = "gl-" + record.key
        else:
            base = f"{record.role}-doc-{encode(record.docid.lower())}"
        record.anchor = base
        counts[base] += 1
    used = set()
    for record in result:
        if counts[record.anchor] > 1:
            warn("duplicate_label", record.label, record.path, "Kennung mehrfach vorhanden. Quellkennung unterscheidet die Blöcke.")
            record.anchor += "-doc-" + encode(record.docid.lower())
        if record.anchor in used:
            raise ExportError(f"Datensätze unter {record.path} lassen sich nicht eindeutig unterscheiden.")
        used.add(record.anchor)
    return result, paths


@dataclass
class Piece:
    start: int
    end: int
    text: str
    path: str
    styles: tuple = ()


@dataclass
class Item:
    start: int
    end: int
    label: str
    key: tuple
    parents: tuple
    path: str
    depth: int


@dataclass
class Flow:
    path: str
    pieces: list = field(default_factory=list)
    items: list = field(default_factory=list)
    paragraphs: list = field(default_factory=list)
    markers: list = field(default_factory=list)
    blocks: dict = field(default_factory=dict)
    length: int = 0

    @cached_property
    def text(self):
        return "".join(piece.text for piece in self.pieces)

    def add(self, text, path, styles=()):
        if text:
            self.pieces.append(Piece(self.length, self.length + len(text), text, path, styles))
            self.length += len(text)

    def break_at(self, path):
        self.add("\n", path, ("layout",))


PARAGRAPH = re.compile(r"^[^\S\n]*\(([0-9]+[a-z]?)\)(?=\s|$)", re.I)
WRAPPERS = {"Content", "text", "P", "LA", "DD", "DT", "Title", "Ident", "noindex", "NB", "F", "SP", "kommentar", "Footnotes", "Footnote", "fussnoten"}
STYLES = {"I": "em", "B": "strong", "SUP": "sup", "SUB": "sub", "FnR": "sup"}


def list_key(label):
    m = re.fullmatch(r"\(?([0-9]+[a-z]?)[.)]", label.strip(), re.I | re.ASCII)
    if m:
        return ("nr", m[1].lower())
    m = re.fullmatch(r"\(?([a-z]{1,2})[.)]", label.strip(), re.ASCII)
    if m:
        return ("lit", m[1])
    return ()


def build_flow(record, parent, paths, warn, *, segment=True):
    flow = Flow(paths.get(parent, record.path))
    if parent is None:
        return flow

    def emit_list(node, parents):
        path = paths[node]
        flow.break_at(path)
        flow.add(node.text, path)
        children = list(node)
        position = 0
        while position < len(children):
            term = children[position]
            if term.tag not in {"DT", "DD"}:
                emit(term, (), parents, False)
                flow.add(term.tail, paths[term])
                position += 1
                continue
            definition = term if term.tag == "DD" else None
            if term.tag == "DT" and position + 1 < len(children) and children[position + 1].tag == "DD":
                definition = children[position + 1]
                position += 1
            label = plain(term).strip() if term.tag == "DT" else ""
            key = list_key(label) if term is not definition and definition is not None else ()
            item = Item(flow.length, flow.length, label, key, parents, paths[term], len(parents))
            flow.items.append(item)
            if term.tag == "DT":
                emit(term, ("list-label",), parents, False)
                flow.add(term.tail, paths[term])
                if label and definition is not None:
                    flow.add(" ", paths[term], ("layout",))
            if definition is not None:
                emit(definition, (), parents + (item,), False)
            item.end = flow.length
            flow.break_at(paths[term])
            if definition is not None:
                flow.add(definition.tail, paths[definition])
            if definition is None or term.tag == "DD":
                warn("unpaired_list", record.label, item.path, "DT und DD lassen sich nicht eindeutig paaren.")
            position += 1

    def emit(node, styles=(), parents=(), top=False):
        path = paths[node]
        tag = node.tag
        if tag == "DL":
            emit_list(node, parents)
            return
        if tag in {"table", "TOC", "pre", "IMG", "Footnotes"}:
            flow.break_at(path)
            flow.blocks[flow.length] = (node, path)
            flow.add("\ufffc", path, ("block",))
            flow.break_at(path)
            return
        if tag == "BR":
            flow.break_at(path)
        if tag == "P" and top and segment:
            marker = PARAGRAPH.match(node.text or "")
            if marker:
                flow.paragraphs.append((flow.length, marker[1].lower(), path, marker.end()))
        if tag == "SUP" and node.get("class") == "Rec" and re.fullmatch(r"[0-9]+", plain(node)):
            flow.markers.append((flow.length, int(plain(node)), len(parents), path))
        if tag in STYLES:
            styles += (STYLES[tag],)
        elif tag not in WRAPPERS and tag != "BR":
            warn("unknown_tag", record.label, path, f"Text von <{tag}> erhalten. Struktur wird neutral ausgegeben.")
        flow.add(node.text, path, styles)
        for child in node:
            emit(child, styles, parents, top=tag in {"Content", "text"} and top)
            flow.add(child.tail, paths[child], styles)
        if tag in {"P", "Title"}:
            flow.break_at(path)

    emit(parent, top=True)
    return flow


ABBREVIATION = re.compile(
    r"(?:\b(?:Abs|Nr|Art|Buchst|S|vgl|gem|bzw|ggf|ff?|BGBl|ABl|Bd|Aufl|Dr|Prof|St|ca|usw|Nrn|lit|einschl|sog|Anm|Bst|a|i|V|m|z|B|d|h|u)\."
    r"|\b(?:[IVX]{1,5}|[0-9]{1,2})\.(?=\s+(?:Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)))$", re.I)
END_MARK = re.compile(r"[.!?]+[»«„“”’\"')\]]*(?=\s|$)")


def sentence_spans(flow, start, end, marker_length, warn, label):
    text = flow.text
    explicit = [(pos, no, path) for pos, no, depth, path in flow.markers if start <= pos < end and depth == 0]
    left = start + marker_length
    while left < end and text[left].isspace():
        left += 1
    if explicit:
        if explicit[0][0] == left and [number for _, number, _ in explicit] == list(range(1, len(explicit) + 1)):
            starts = [start] + [pos for pos, _, _ in explicit[1:]]
            return [(a, b, "explicit", "Aufsteigende SUP-Satzmarker der Klasse Rec") for a, b in zip(starts, starts[1:] + [end])]
        warn("ambiguous_sentence_marker", label, explicit[0][2], "Die Satzmarker bilden keine eindeutige Folge.")
        return [(start, end, "ambiguous", "Widersprüchliche SUP-Satzmarker")]
    if not text[left:end].strip("\n \t\ufffc"):
        return []
    # Listenpunktlabels zählen nie als Satzende. In einer Liste kommt allein das Ende
    # des letzten äußeren Eintrags als äußere Grenze in Betracht.
    items = [item for item in flow.items if start <= item.start < end]
    outer = [item for item in items if not item.parents]
    list_tails = set()
    for item, following in zip(outer, outer[1:] + [None]):
        if following and not text[item.end:following.start].strip():
            continue
        list_tails.add(len(text[:item.end].rstrip()))
    layout_labels = [(p.start, p.end) for p in flow.pieces if "list-label" in p.styles and start <= p.start < end]
    cuts = [start]
    quote_depth = 0
    previous = left
    for match in END_MARK.finditer(text, left, end):
        pos, stop = match.span()
        for char in text[previous:stop]:
            if char in "„‚»«“”\"":
                quote_depth = 1 - quote_depth
        previous = stop
        if any(a <= pos < b for a, b in layout_labels):
            continue
        if any(item.start <= pos < item.end for item in outer) and stop not in list_tails:
            continue
        if (match[0].startswith("...") or quote_depth or ABBREVIATION.search(text[max(left, pos - 30):pos + 1])):
            continue
        next_word = re.search(r"\S", text[stop:end])
        if not next_word:
            continue
        boundary = stop + next_word.start()
        if text[boundary].isupper() or text[boundary] in "„‚»§(\"":
            cuts.append(boundary)
    return [(a, b, "inferred", "Endinterpunktion mit Schutz für Abkürzungen, Zitate und Listen")
            for a, b in zip(cuts, cuts[1:] + [end])]
