"""Zielregister und Grammatik für juristische Fundstellen."""

from bisect import bisect_right
from collections import Counter, defaultdict
import re

from statute_model import encode, normalized, sentence_spans
from statute_source import KNOWN, alias_key, digest


SCHEMA_VERSION = 1
ANCHOR_VERSION = 1
UNITS = {"abs": "Absatz", "s": "Satz", "nr": "Nummer", "lit": "Buchstabe"}


def selector_text(selector):
    parts = []
    for kind, value in zip(selector[::2], selector[1::2]):
        parts.append(f"{ {'p': '§', 'art': 'Art.', **UNITS}.get(kind, kind)} {value}")
    return " ".join(parts)


def target(source, record, anchor, selector, *, aliases=(), path=None, span=None,
           confidence="explicit", reason="Quellmetadaten", parent=None, text="", label=None):
    return {"law": source.slug, "path": f"Bundesrecht/{source.slug}.md", "id": anchor,
            "selector": list(selector), "aliases": [list(a) for a in aliases],
            "label": label or selector_text(selector) or record.heading or record.docid,
            "parents": [{"label": p.heading, "id": p.anchor, "key": p.key, "xml_path": p.path} for p in record.parents],
            "parent_block": parent, "doknr": record.docid, "xml_path": path or record.path,
            "span": list(span) if span is not None else None, "confidence": confidence, "reason": reason,
            "text_sha256": digest(text.encode("utf-8")), "markdown_line": None}


def flow_targets(source, record, flow, warn):
    grouped = []
    if not record.selector or not flow.length:
        return grouped
    text = flow.text
    paragraphs = list(flow.paragraphs)
    if not paragraphs or text[:paragraphs[0][0]].strip():
        paragraphs.insert(0, (0, "", flow.path, 0))
    repeated = Counter(item[1] for item in paragraphs)
    for index, (start, number, path, marker_length) in enumerate(paragraphs):
        end = paragraphs[index + 1][0] if index + 1 < len(paragraphs) else flow.length
        selector = record.selector + (("abs", number) if number else ())
        base = record.anchor + (f"-abs-{number}" if number else "")
        ambiguous = bool(number and repeated[number] > 1)
        if ambiguous:
            base += "-xml-" + encode(path)
            warn("duplicate_paragraph", record.label, path, "Mehrere Absatzmarker mit derselben Nummer.")
        parent = base if number else record.anchor
        if number:
            grouped.append((start, target(source, record, base, selector, path=path, span=(start, end),
                                          confidence="ambiguous" if ambiguous else "explicit", reason="Absatzmarker am P-Anfang",
                                          parent=record.anchor, text=text[start:end])))
        spans = sentence_spans(flow, start, end, marker_length, warn, record.label)
        for count, (a, b, confidence, reason) in enumerate(spans, 1):
            sentence = selector + ("s", str(count))
            grouped.append((a, target(source, record, f"{base}-s-{count}", sentence, path=path, span=(a, b),
                                      confidence="ambiguous" if ambiguous else confidence, reason=reason, parent=parent, text=text[a:b])))
        items = [item for item in flow.items if start <= item.start < end]
        list_targets = {}
        candidates = []
        for item in items:
            keys = tuple(value for part in item.parents + (item,) for value in part.key)
            usable = all(part.key for part in item.parents + (item,))
            sentence = next((str(n) for n, (a, b, confidence, _) in enumerate(spans, 1)
                             if a <= item.start < b and item.end <= b and confidence != "ambiguous"), None)
            canonical = selector + (("s", sentence) if sentence else ()) + keys if usable else ()
            aliases = (selector + keys,) if sentence and usable else ()
            suffix = "-".join(canonical[len(selector):]) if canonical else "liste-xml-" + encode(item.path)
            candidates.append((item, canonical, aliases, f"{base}-{suffix}", sentence))
        duplicates = Counter(data[3] for data in candidates)
        for item, canonical, aliases, anchor, sentence in candidates:
            duplicate = duplicates[anchor] > 1
            if duplicate:
                warn("duplicate_list_label", record.label, item.path, "Listenkennzeichen wiederholt sich im selben Kontext.")
                anchor += "-xml-" + encode(item.path)
            list_targets[item.path] = anchor
            item_parent = (list_targets[item.parents[-1].path] if item.parents else
                           f"{base}-s-{sentence}" if sentence else parent)
            grouped.append((item.start, target(source, record, anchor, canonical, aliases=aliases, path=item.path,
                                              span=(item.start, item.end), parent=item_parent,
                                              confidence="ambiguous" if ambiguous or not canonical or duplicate else "explicit",
                                              reason="Kennzeichen aus DT und DD" if canonical else "Unnummerierter oder strukturell offener Listeneintrag",
                                              text=text[item.start:item.end], label=item.label or "Listeneintrag")))
    return grouped


class Registry:
    def __init__(self, sources, targets):
        self.selectors = defaultdict(list)
        self.order = defaultdict(list)
        self.aliases = defaultdict(set)
        for item in targets:
            for selector in {tuple(s) for s in [item["selector"], *item["aliases"]] if s}:
                self.selectors[item["law"], selector].append(item)
            if len(item["selector"]) == 2 and item["selector"][0] in {"p", "art"}:
                self.order[item["law"], item["selector"][0]].append(item)
        names = defaultdict(set)
        for slug, (_, aliases) in KNOWN.items():
            for value in (*aliases, slug):
                names[normalized(value)[0]].add(slug)
        for source in sources.values():
            for value in source.aliases:
                names[normalized(value)[0]].add(source.slug)
        self.laws = set(sources)
        for name, slugs in names.items():
            self.aliases[alias_key(normalized(name)[0])].update(slugs)
        alternatives = "|".join(re.escape(x) for x in sorted(names, key=lambda x: (-len(x), x)))
        self.qualifier = re.compile(rf" +(?:(?:des|der|dem|den) +)?(?P<name>{alternatives})(?![\w-])", re.I)

    def resolve(self, laws, selector, problem):
        if problem:
            return problem, [], None
        if len(laws) != 1:
            return "ambiguous", sorted(laws), None
        law, = laws
        if law not in self.laws:
            return "missing_law", [law], None
        candidates = self.selectors.get((law, selector), [])
        descriptors = [{"path": x["path"], "id": x["id"]} for x in candidates]
        if not candidates:
            return "missing_target", [], None
        if len(candidates) != 1 or candidates[0]["confidence"] == "ambiguous":
            return "ambiguous", descriptors, None
        return "resolved", descriptors, candidates[0]


NUMBER = r"[0-9]+[a-z]?(?![\w])"
LAW_HEAD = re.compile(rf"(?<![\w§])(?P<kind>§§?|Art\.|Art\b|Artikel) *(?P<value>{NUMBER})", re.I)
UNIT = re.compile(rf"(?<![\w])(?P<kind>Abs\.|Absatz|Absätze|Absätzen|S\.|Satz|Sätze|Sätzen|Nr\.|Nummer|Nummern|Buchst\.|Buchstabe|Buchstaben|lit\.) +(?P<value>{NUMBER}|[a-z]{{1,2}}(?!\w))", re.I)
CONNECTOR = re.compile(r" *(?P<con>,|und\b|oder\b|bis\b) *", re.I)
VERSION = re.compile(r" +(?:a\. *F\.|n\. *F\.|in der Fassung\b|in seiner früheren Fassung\b)", re.I)
FOLLOWING = re.compile(r" +(?:f|ff)\.", re.I)
UNKNOWN_LAW = re.compile(r" +(?:(?:des|der) +(?:(?:vorstehenden|genannten) +)?(?:[\w-]+ +){0,4}[\w-]*(?:gesetz(?:es)?|verordnung)|(?:(?:[A-ZÄÖÜ][a-zäöüß0-9]*){2,}|[A-ZÄÖÜ]{2,})(?:-[A-Z0-9]+)?)(?![\w-])")
PROTECTED = re.compile(r"\[\[[^\n]*?\]\]|`+[^\n]*?`+|https?://[^\s<>]+|\b(?:BGBl\.|ABl\.)[^;\n)]*?\bS\. +\d+")
STATUS_REASON = {"resolved": "Ein eindeutiges Ziel im Bestand", "missing_law": "Das zitierte Gesetz fehlt im Bestand",
                 "missing_target": "Der Selektor ist im Zielgesetz nicht belegt", "ambiguous": "Kontext oder Ziel ist nicht eindeutig",
                 "unsupported": "Der Ausdruck lässt sich nicht sicher als aktueller Normverweis auflösen",
                 "version_mismatch": "Kein passender Nachweis für die zitierte Gesetzesfassung"}


def unit_kind(token):
    lower = token.lower()
    if lower.startswith("abs"):
        return "abs"
    if lower.startswith("s"):
        return "s"
    if lower.startswith("n"):
        return "nr"
    return "lit"


def read_units(text, cursor, base):
    while True:
        start = cursor
        while cursor < len(text) and text[cursor] == " ":
            cursor += 1
        unit = UNIT.match(text, cursor)
        if unit is None or cursor == start:
            return base, start
        kind, value = unit_kind(unit["kind"]), unit["value"].lower()
        if (kind == "lit") == value[0].isdigit():
            return base, start
        base += (kind, value)
        cursor = unit.end()


def references(flow, source, record, registry, markers, *, offset=0):
    original = flow.text
    text, spans = normalized(original)
    if not text:
        return []
    protected = [m.span() for m in PROTECTED.finditer(text)]
    out = []
    cursor = 0
    explicit_law_end = -1
    explicit_laws = set()
    marker_offsets = [position for position, _ in markers]

    def context(at):
        values = {}
        block = record.anchor
        for position, item in reversed(markers[:bisect_right(marker_offsets, at)]):
            selector = item["selector"]
            if not selector or not item["span"][0] <= at < item["span"][1]:
                continue
            if block == record.anchor:
                block = item["id"]
            for kind, value in zip(selector[::2], selector[1::2]):
                if kind not in values and item["confidence"] != "ambiguous":
                    values[kind] = value
        return values, block

    while cursor < len(text):
        head = LAW_HEAD.search(text, cursor)
        relative = UNIT.search(text, cursor)
        if head is None and relative is None:
            break
        m = head if head is not None and (relative is None or head.start() <= relative.start()) else relative
        start, end = m.span()
        cursor = end
        if any(left <= start < right for left, right in protected):
            continue
        law_prefix = m is head
        original_start = spans[start][0]
        local, block = context(original_start)
        problem = None
        if law_prefix:
            kind = "p" if m["kind"].startswith("§") else "art"
            base = (kind, m["value"].lower())
        else:
            kind, value = unit_kind(m["kind"]), m["value"].lower()
            if (kind == "lit") == value[0].isdigit() or (m["kind"].lower() == "s." and not record.selector):
                continue
            # Standalone S. is ordinarily a page. Only a preceding norm or an explicit unit licenses it.
            if m["kind"].lower() == "s.":
                continue
            base = record.selector
            if kind in {"s", "nr", "lit"} and "abs" in local:
                base += ("abs", local["abs"])
            if kind == "lit" and "nr" in local:
                base += ("nr", local["nr"])
            if not record.selector:
                problem = "unsupported"
            base += (kind, value)
        selector, end = read_units(text, end, base)
        parts = [(start, end, selector)]
        connectors = []
        while join := CONNECTOR.match(text, end):
            number = re.compile(r"[a-z]{1,2}(?!\w)" if kind == "lit" else NUMBER, re.I).match(text, join.end())
            if not number:
                break
            following, after = read_units(text, number.end(), base[:-1] + (number[0].lower(),))
            connectors.append(join["con"].lower())
            parts.append((number.start(), after, following))
            end = after
        ff = FOLLOWING.match(text, end)
        if ff:
            end = ff.end()
        qualifier = registry.qualifier.match(text, end)
        laws = {source.slug}
        if qualifier:
            laws = registry.aliases[alias_key(qualifier["name"])]
            end = qualifier.end()
            explicit_law_end, explicit_laws = end, laws
            a, _, selector = parts[-1]
            parts[-1] = (a, end, selector)
            if not law_prefix and laws != {source.slug}:
                problem = "unsupported"
        elif unknown := UNKNOWN_LAW.match(text, end):
            name = unknown[0].strip()
            if name not in {"I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "EU", "EG", "EWG"}:
                end = unknown.end()
                problem = "missing_law" if name[0].isupper() else "unsupported"
                if problem == "missing_law":
                    laws = {name}
                a, _, selector = parts[-1]
                parts[-1] = (a, end, selector)
        elif explicit_law_end >= 0 and re.fullmatch(r" *(?:,|und|oder) *", text[explicit_law_end:start], re.I):
            laws = explicit_laws
            if not law_prefix:
                problem = "unsupported"
        if version := VERSION.match(text, end):
            end = version.end()
            problem = "version_mismatch"
            a, _, selector = parts[-1]
            parts[-1] = (a, end, selector)
        if len(parts) > 1 and any(len(part[2]) != len(parts[0][2]) for part in parts):
            problem = problem or "ambiguous"
        cursor = max(end, cursor)
        batch = []
        for a, b, selector in parts:
            begin, finish = spans[a][0], spans[b - 1][1]
            raw = original[begin:finish]
            reason = problem
            if any(c in raw for c in "\n\r|[]\\<>&`*_^"):
                reason = "unsupported"
            status, candidates, found = registry.resolve(laws, selector, reason)
            entry = {"source_law": source.slug, "source_block": block, "xml_path": flow.path,
                     "offsets": [begin + offset, finish + offset], "stream_start": offset, "original": raw, "selector": list(selector),
                     "law_candidates": sorted(laws), "status": status, "reason": STATUS_REASON[status],
                     "candidates": candidates, "target_path": found["path"] if found else None,
                     "target_id": found["id"] if found else None}
            if connectors:
                entry["group"] = {"start": spans[start][0] + offset, "connectors": connectors}
            if ff:
                entry["following"] = ff[0].strip()
            batch.append(entry)
        if len(batch) == 2 and connectors == ["bis"] and all(x["status"] == "resolved" for x in batch):
            first, last = batch
            if len(first["selector"]) == len(last["selector"]) == 2:
                seq = [x["id"] for x in registry.order[next(iter(laws)), first["selector"][0]]]
                left, right = seq.index(first["target_id"]), seq.index(last["target_id"])
                if left <= right:
                    first["range_members"] = seq[left:right + 1]
        out.extend(batch)
    return out
