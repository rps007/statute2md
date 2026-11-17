"""Download und Prüfung der Quellen für statute2md."""

import base64
import hashlib
import http.client
import io
import re
import socket
import ssl
import stat
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import getproxies, proxy_bypass
from xml.etree import ElementTree as ET
from xml.parsers import expat


MiB = 1024 * 1024
HOSTS = {"www.gesetze-im-internet.de", "gesetze-im-internet.de"}
BASE = "https://www.gesetze-im-internet.de"
SLUG = r"[a-z0-9][a-z0-9_-]{0,127}"
CATALOG = f"{BASE}/gii-toc.xml"
DTDS = {f"{scheme}://www.gesetze-im-internet.de/dtd/1.01/gii-norm.dtd"
        for scheme in ("http", "https")}
KNOWN = {
    "bverfgg": ("BJNR002430951", ("BVerfGG", "Bundesverfassungsgerichtsgesetz", "Bundesverfassungsgerichtsgesetzes")),
    "gg": ("BJNR000010949", ("GG", "Grundgesetz", "Grundgesetzes")),
    "bgb": ("BJNR001950896", ("BGB", "Bürgerliches Gesetzbuch", "Bürgerlichen Gesetzbuches")),
    "sgb_1": ("BJNR030150975", ("SGB I", "SGB 1", "SGB-I", "Erstes Buch Sozialgesetzbuch", "Ersten Buches Sozialgesetzbuch")),
    "stvg": ("BJNR004370909", ("StVG", "Straßenverkehrsgesetz", "Straßenverkehrsgesetzes")),
    "estg": ("BJNR010050934", ("EStG", "Einkommensteuergesetz", "Einkommensteuergesetzes")),
}


class ExportError(ValueError):
    pass


class SourceHTTPError(ExportError):
    def __init__(self, status, url=None):
        self.status = status
        self.archive_url = original_archive_url(url) if status != 404 else None
        message = {403: "HTTP 403. Bitte lade das Original-ZIP hoch.",
                   404: "HTTP 404. Bitte prüfe Gesetzeskürzel oder Slug."}
        super().__init__(message.get(status, f"HTTP {status}. Bitte versuche es erneut oder lade das Original-ZIP hoch."))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def alias_key(value):
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def clean_input(value):
    value = unicodedata.normalize("NFC", value.strip())
    if not value or len(value) > 240 or any(unicodedata.category(c) == "Cc" for c in value):
        raise ExportError("Gib ein einzelnes Gesetzeskürzel oder einen Slug ein.")
    return value


def source_url(url):
    if any(ord(c) <= 32 for c in url):
        raise ExportError("Unzulässige Quellenadresse.")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc not in HOSTS or parsed.query or parsed.fragment
            or not re.fullmatch(rf"/(?:{SLUG}/xml\.zip|gii-toc\.xml)", parsed.path, re.ASCII)):
        raise ExportError("Unzulässige Quellenadresse oder Weiterleitung.")
    return parsed


def original_archive_url(url):
    if url is None:
        return None
    parsed = source_url(url)
    if parsed.path.endswith("/xml.zip"):
        return BASE + parsed.path
    return None


class SourceConnectionError(ExportError):
    def __init__(self, message, url):
        super().__init__(message)
        self.archive_url = original_archive_url(url)


class SourceHTTPSConnection(http.client.HTTPSConnection):
    """Protokolliert Verbindungsphasen ohne Proxy-Adressen oder Zugangsdaten."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.log = lambda stage, message: None
        self.phase = "beim HTTPS-Verbindungsaufbau"
        self._create_connection = self._connect_tcp

    def _begin(self, phase, message):
        self.phase = phase
        self._phase_started = time.monotonic()
        self.log("Download", message)

    def _done(self, message):
        self.log("Download", f"{message} ({time.monotonic() - self._phase_started:.2f} s).")

    def _begin_tls(self):
        self._begin("beim TLS-Handschlag", "TLS-Handschlag mit Zertifikatsprüfung starten.")

    def _connect_tcp(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        destination = "Proxy-Host" if self._tunnel_host else "Quellhost"
        self._begin("bei der DNS-Auflösung", f"DNS-Auflösung des {destination}s starten.")
        addresses = socket.getaddrinfo(*address, 0, socket.SOCK_STREAM)
        self._done(f"DNS-Auflösung abgeschlossen: {len(addresses)} Adresse(n)")
        error = OSError("Keine Zieladresse verfügbar")
        for family, kind, protocol, _, target in addresses:
            version = "IPv6" if family == socket.AF_INET6 else "IPv4"
            self._begin(f"beim TCP-Verbindungsaufbau ({version})", f"TCP-Verbindung zum {destination} über {version} starten.")
            transport = None
            try:
                transport = socket.socket(family, kind, protocol)
                if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    transport.settimeout(timeout)
                if source_address:
                    transport.bind(source_address)
                transport.connect(target)
            except OSError as exc:
                error = exc
                self._done(f"TCP-Verbindung über {version} fehlgeschlagen: {type(exc).__name__}")
                if transport is not None:
                    transport.close()
                continue
            self._done(f"TCP-Verbindung über {version} hergestellt")
            if not self._tunnel_host:
                self._begin_tls()
            return transport
        raise error

    def _tunnel(self):
        self._begin("beim Proxy-CONNECT", "HTTPS-Tunnel über den konfigurierten Proxy aufbauen.")
        try:
            super()._tunnel()
        except OSError as exc:
            if match := re.search(r"Tunnel connection failed: (\d{3})\b", str(exc)):
                self.phase = f"beim Proxy-CONNECT (HTTP {match[1]})"
                self.log("Download", f"Proxy-CONNECT abgewiesen: HTTP {match[1]}. Der Quellserver wurde noch nicht per HTTPS erreicht.")
            raise
        self._done("Proxy-CONNECT erfolgreich")
        self._begin_tls()

    def connect(self):
        try:
            super().connect()
        except (OSError, http.client.HTTPException) as exc:
            self.log("Download", f"Abbruch {self.phase}: {type(exc).__name__}.")
            raise
        self._done("TLS-Handschlag mit Zertifikatsprüfung abgeschlossen")


def connect_https(host, *, timeout, context):
    proxy_url = getproxies().get("https")
    if not proxy_url or proxy_bypass(host):
        return SourceHTTPSConnection(host, timeout=timeout, context=context)
    proxy = urlsplit(proxy_url if "://" in proxy_url else "http://" + proxy_url)
    if proxy.scheme != "http" or not proxy.hostname:
        raise ValueError("Der HTTPS-Proxy muss als HTTP-CONNECT-Proxy eingerichtet sein.")
    headers = {}
    if proxy.username is not None:
        auth = f"{unquote(proxy.username)}:{unquote(proxy.password or '')}"
        headers["Proxy-Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode("ascii")
    connection = SourceHTTPSConnection(proxy.hostname, proxy.port or 80, timeout=timeout, context=context)
    connection.set_tunnel(host, port=443, headers=headers)
    return connection


def download(url, limit, log, *, connect=connect_https):
    requested_url = url
    deadline = time.monotonic() + 90
    redirects = retries = 0
    while True:
        current = source_url(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SourceConnectionError("Zeitlimit beim Abruf. Bitte lade das Original-ZIP hoch.", requested_url)
        try:
            conn = connect(current.hostname, timeout=min(10, remaining), context=ssl.create_default_context())
        except ValueError as exc:
            raise SourceConnectionError("HTTPS-Proxy-Konfiguration ungültig. Bitte lade das Original-ZIP hoch.",
                                        requested_url) from exc
        phase = "beim HTTPS-Verbindungsaufbau"
        try:
            log("Download", f"HTTPS-Verbindung zu {current.hostname} aufbauen.")
            if isinstance(conn, SourceHTTPSConnection):
                conn.log = log
            try:
                conn.connect()
            except (OSError, http.client.HTTPException):
                phase = getattr(conn, "phase", phase)
                raise
            phase = "beim Warten auf die HTTP-Antwort"
            log("Download", "HTTPS verbunden. Warte auf die HTTP-Antwort.")
            transport = conn.sock
            transport.settimeout(min(30, max(0.01, deadline - time.monotonic())))
            conn.request("GET", current.path, headers={"User-Agent": "statute2md/1", "Accept-Encoding": "identity"})
            with conn.getresponse() as response:
                status = response.status
                log("Download", f"HTTP {status} von {current.hostname}.")
                if status in {301, 302, 303, 307, 308}:
                    redirects += 1
                    location = response.getheader("Location")
                    if redirects > 3 or not location:
                        raise ExportError("Zu viele oder ungültige Weiterleitungen.")
                    url = urljoin(url, location)
                    source_url(url)
                    continue
                if status == 200:
                    phase = "beim Lesen der HTTP-Antwort"
                    declared = response.getheader("Content-Length")
                    if declared and (not declared.isdecimal() or int(declared) > limit):
                        raise ExportError("Der Download überschreitet das Größenlimit.")
                    data = bytearray()
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError
                        transport.settimeout(min(30, remaining))
                        chunk = response.read1(min(65536, limit - len(data) + 1))
                        if not chunk:
                            break
                        data.extend(chunk)
                        if len(data) > limit:
                            raise ExportError("Der Download überschreitet das Größenlimit.")
                    if declared and len(data) != int(declared):
                        raise ExportError("Der Download ist unvollständig. Bitte erneut versuchen.")
                    log("Download", f"{len(data):,} Bytes empfangen.")
                    return bytes(data), url
                if status not in {429, 500, 502, 503, 504} or retries == 2:
                    raise SourceHTTPError(status, requested_url)
        except TimeoutError as exc:
            if retries == 2 or time.monotonic() >= deadline:
                raise SourceConnectionError(f"Zeitlimit {phase} zu {current.hostname}. Bitte lade das Original-ZIP hoch.",
                                            requested_url) from exc
            status = f"Zeitlimit {phase} zu {current.hostname}"
        except socket.gaierror as exc:
            raise SourceConnectionError(f"DNS-Auflösung für {current.hostname} fehlgeschlagen. Bitte lade das Original-ZIP hoch.",
                                        requested_url) from exc
        except ssl.SSLError as exc:
            raise SourceConnectionError(f"TLS-Verbindung zu {current.hostname} fehlgeschlagen. Bitte lade das Original-ZIP hoch.",
                                        requested_url) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise SourceConnectionError(f"Netzwerkfehler {phase} zu {current.hostname}. Bitte lade das Original-ZIP hoch.",
                                        requested_url) from exc
        finally:
            conn.close()
        retries += 1
        log("Download", f"{status}. Neuer Versuch {retries}/2.")
        time.sleep(min(2 ** (retries - 1), max(0, deadline - time.monotonic())))


def read_member(archive, info, maximum):
    data = bytearray()
    with archive.open(info) as member:
        while chunk := member.read(min(65536, maximum - len(data) + 1)):
            data.extend(chunk)
            if len(data) > maximum:
                raise ExportError("Ein ZIP-Mitglied überschreitet das Größenlimit.")
    if len(data) != info.file_size:
        raise ExportError("Die tatsächliche ZIP-Mitgliedsgröße weicht von der Angabe ab.")
    return bytes(data)


def unpack(data, *, project=False):
    compressed, total, count = (96 * MiB, 96 * MiB, 65) if project else (32 * MiB, 128 * MiB, 256)
    if len(data) > compressed:
        raise ExportError("Das ZIP überschreitet das Größenlimit.")
    if not data.startswith(b"PK"):
        if data.lstrip().startswith(b"<"):
            raise ExportError("Die Quelle liefert HTML/XML statt eines ZIP. Bitte lade das Original-ZIP hoch.")
        raise ExportError("Kein ZIP erkannt. Bitte verwende das Originalarchiv.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > count or sum(x.file_size for x in entries) > total:
                raise ExportError("Das ZIP enthält zu viele Mitglieder oder zu viele entpackte Bytes.")
            checked = set()
            for entry in entries:
                name = entry.orig_filename
                parts = name.rstrip("/").split("/")
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if (name.casefold().rstrip("/") in checked or any(p in {"", ".", ".."} for p in parts)
                        or any(c in name for c in "\\:\x00") or kind not in {0, stat.S_IFREG, stat.S_IFDIR}
                        or entry.flag_bits & 1):
                    raise ExportError("Unsicherer oder mehrfach vorhandener ZIP-Eintrag.")
                checked.add(name.casefold().rstrip("/"))
                suffix = PurePosixPath(name).suffix.lower()
                if not project and suffix in {".zip", ".gz", ".bz2", ".xz", ".7z", ".rar", ".tar", ".jar"}:
                    raise ExportError("Verschachtelte Archive sind in einer Gesetzesquelle unzulässig.")
                if project and not re.fullmatch(rf"manifest\.json|sources/{SLUG}\.zip", name, re.ASCII):
                    raise ExportError("Unbekannter Eintrag im Projektbestand.")
            files = [x for x in entries if not x.is_dir()]
            if not project and sum(x.filename.lower().endswith(".xml") for x in files) != 1:
                raise ExportError("Das Original-ZIP muss genau eine XML-Datei enthalten.")
            result = {}
            for entry in files:
                name = entry.filename
                maximum = (MiB if name == "manifest.json" else 32 * MiB) if project else 64 * MiB
                if entry.file_size > maximum:
                    raise ExportError(f"Ein ZIP-Mitglied überschreitet {maximum // MiB} MiB.")
                contents = read_member(archive, entry, maximum)
                if not project and contents.startswith((b"PK\x03\x04", b"PK\x05\x06", b"\x1f\x8b", b"7z\xbc\xaf\x27\x1c", b"Rar!")):
                    raise ExportError("Verschachteltes Archiv anhand der Dateisignatur erkannt.")
                result[name] = contents
            return result
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError, EOFError) as exc:
        raise ExportError("Beschädigtes oder nicht unterstütztes ZIP. Bitte lade das Original erneut herunter.") from exc


def parse_xml(data, expected_root, *, max_depth=100, max_elements=500_000, max_text=48 * MiB):
    """Expat prüft Deklarationen und Grenzen beim Aufbau desselben Baums."""
    parser = expat.ParserCreate(namespace_separator="}")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    builder = ET.TreeBuilder()
    depth = elements = text_size = 0
    roots = {expected_root} if isinstance(expected_root, str) else expected_root

    def reject(*_):
        raise ExportError("XML-Entitäten, interne DTDs und externe Ressourcen sind unzulässig.")

    def doctype(name, system, public, internal):
        if (internal or public or name != "dokumente" or roots != {"dokumente"} or system not in DTDS):
            reject()

    def start(tag, attributes):
        nonlocal depth, elements
        if "}" in tag or any("}" in key and not key.startswith("http://www.w3.org/XML/1998/namespace}") for key in attributes):
            raise ExportError("Nicht unterstützter XML-Namensraum.")
        if depth == 0 and tag not in roots:
            raise ExportError(f"Unbekannte XML-Struktur. Erwartet wird {', '.join(sorted(roots))}.")
        depth += 1
        elements += 1
        if depth > max_depth or elements > max_elements:
            raise ExportError("Die XML-Struktur überschreitet die zulässigen Grenzen.")
        builder.start(tag, attributes)

    def end(tag):
        nonlocal depth
        builder.end(tag)
        depth -= 1

    def character_data(value):
        nonlocal text_size
        text_size += len(value)
        if text_size > max_text:
            raise ExportError("Der XML-Text überschreitet das Größenlimit.")
        builder.data(value)

    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = character_data
    try:
        for start_byte in range(0, len(data), 65536):
            parser.Parse(data[start_byte:start_byte + 65536], False)
        parser.Parse(b"", True)
    except expat.ExpatError as exc:
        raise ExportError(f"XML ungültig (Zeile {exc.lineno}, Spalte {exc.offset}).") from exc
    return builder.close()


def catalog_entries(data):
    root = parse_xml(data, {"toc", "items"}, max_depth=12, max_elements=100_000, max_text=8 * MiB)
    entries = {}
    for item in root.findall("item") + root.findall("items/item"):
        title = item.findtext("title", "").strip()
        link = item.findtext("link", "").strip()
        if link.startswith("http://"):
            link = "https://" + link[len("http://"):]
        if not title or not link:
            continue
        try:
            path = source_url(link).path
        except ExportError:
            continue
        if path.endswith("/xml.zip"):
            entries.setdefault(path.split("/")[1], set()).add(title)
    if not entries:
        raise ExportError("Das Quellenverzeichnis enthält keine gültigen Gesetzeseinträge.")
    return entries


def catalog_names(title):
    # Der Katalog enthält Langtitel. Ein abschließendes Klammerkürzel ist ein eigener Alias.
    names = {alias_key(title)}
    suffix = re.search(r"\s+\(([^()]+)\)\s*$", title)
    if suffix:
        names.add(alias_key(suffix[1]))
        names.add(alias_key(title[:suffix.start()]))
    return names


@dataclass
class Source:
    slug: str
    archive: bytes
    root: ET.Element
    provenance: dict
    aliases: set
    assets: dict


def load_source(slug, archive, log, *, url=None, expected="", catalog_titles=()):
    if not re.fullmatch(SLUG, slug, re.ASCII):
        raise ExportError("Für den Upload wird ein gültiger Slug benötigt, etwa sgb_1.")
    log("ZIP", "Prüfe das Archiv und die XML-Datei.")
    files = unpack(archive)
    member, = (x for x in files if x.lower().endswith(".xml"))
    xml = files.pop(member)
    log("XML", f"Lese {PurePosixPath(member).name}.")
    root = parse_xml(xml, "dokumente")
    head = root.find("norm/metadaten")
    docid = root.get("doknr", "")
    if (head is None or not head.findtext("jurabk") or not re.fullmatch(r"[A-Za-z0-9_-]{3,80}", docid)
            or len(root) < 2 or any(node.tag != "norm" for node in root)):
        raise ExportError("Gesetzeskopf oder direkte Normdatensätze fehlen.")
    names = {head.findtext(tag, "").strip() for tag in ("jurabk", "amtabk", "langue", "kurzue")}
    names.discard("")
    doc_known = KNOWN.get(slug)
    if doc_known and (doc_known[0] != docid or not {alias_key(s) for s in names}.intersection(map(alias_key, doc_known[1]))):
        raise ExportError(f"Die XML-Gesetzesidentität passt nicht zu {slug}.")
    if any(known_slug != slug and known[0] == docid for known_slug, known in KNOWN.items()):
        raise ExportError(f"Die XML-Gesetzesidentität passt nicht zu {slug}.")
    metadata_keys = {alias_key(s) for s in names}
    accepted = {alias_key(expected), alias_key(slug), alias_key(slug.replace("_", " "))}
    for title in catalog_titles:
        accepted.update(catalog_names(title))
    if not doc_known and not metadata_keys.intersection(accepted):
        raise ExportError("Kürzel und Slug sind anhand der XML-Metadaten nicht eindeutig zuzuordnen.")
    if doc_known:
        names.update(doc_known[1])
    names.add(slug)
    provenance = {"method": "download" if url else "upload", "url": url,
                  "expected_slug": slug, "zip_sha256": digest(archive), "xml_sha256": digest(xml),
                  "member": member, "doknr": docid, "builddate": root.get("builddate"),
                  "expected_alias": expected, "catalog_titles": sorted(catalog_titles),
                  "stand": ["".join(el.itertext()).strip() for el in head.findall("standangabe/standkommentar")]}
    return Source(slug, archive, root, provenance, names, files)


def acquire(value, upload, log, fetch=download):
    value = clean_input(value)
    direct = value.lower()
    key = alias_key(value)
    safe_direct = bool(re.fullmatch(SLUG, direct, re.ASCII))
    local = {slug for slug, (_, aliases) in KNOWN.items() if key in map(alias_key, aliases)}
    if upload is not None:
        slug = next(iter(local)) if len(local) == 1 else direct
        log("Upload", f"Prüfe Original-ZIP für {slug}.")
        return load_source(slug, upload, log, expected=value)
    if safe_direct:
        url = f"{BASE}/{direct}/xml.zip"
        log("Download", f"Lade {url}.")
        try:
            data, final_url = fetch(url, 32 * MiB, log)
        except SourceHTTPError as exc:
            if exc.status != 404:
                raise
        else:
            final_slug = source_url(final_url).path.split("/")[1]
            if final_slug == direct and (not local or direct in local):
                try:
                    return load_source(direct, data, log, url=final_url, expected=value)
                except ExportError as exc:
                    if "nicht eindeutig zuzuordnen" not in str(exc):
                        raise
    log("Quelle", "Suche das Kürzel im Quellenverzeichnis.")
    titles = {}
    try:
        data, _ = fetch(CATALOG, 8 * MiB, log)
        titles = catalog_entries(data)
    except ExportError:
        if not local:
            raise
        log("Quelle", "Quellenverzeichnis nicht verfügbar. Verwende den geprüften Alias.")
    matches = local | {slug for slug, values in titles.items() if any(key in catalog_names(v) for v in values)}
    if safe_direct and direct in titles:
        matches.add(direct)
    if len(matches) != 1:
        options = ", ".join(sorted(matches))
        raise ExportError(f"Mehrere Treffer ({options}). Bitte gib einen Slug ein." if options else
                          "Kein eindeutiges Gesetz gefunden. Bitte prüfe das Kürzel oder gib den Slug aus der Quellen-URL ein.")
    slug, = matches
    archive_url = f"{BASE}/{slug}/xml.zip"
    log("Download", f"Lade {archive_url}.")
    data, url = fetch(archive_url, 32 * MiB, log)
    if source_url(url).path != f"/{slug}/xml.zip":
        raise ExportError("Die Weiterleitung hat den erwarteten Gesetzesslug geändert.")
    return load_source(slug, data, log, url=url, expected=value, catalog_titles=titles.get(slug, ()))
