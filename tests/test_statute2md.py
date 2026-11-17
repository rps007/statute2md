import io
import json
from pathlib import Path
import socket
import ssl
import subprocess
from threading import get_ident
import sys
import unittest
from unittest.mock import patch
import warnings
import zipfile


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
NB = json.loads((ROOT / "statute2md.ipynb").read_bytes())
sys.path.insert(0, str(ROOT))
import statute_source as source
from statute_export import Session
from statute_model import normalized
from repository_files import RepositoryFiles
from repository_git import GIT_TIMEOUT, RepositoryGit
from tempfile import TemporaryDirectory

Error = source.ExportError
quiet = lambda stage, message: None
PROVENANCE = json.loads((FIXTURES / "provenance.json").read_bytes())


def zipped(entries, compression=zipfile.ZIP_STORED):
    output = io.BytesIO()
    with warnings.catch_warnings(), zipfile.ZipFile(output, "w", compression=compression) as archive:
        warnings.simplefilter("ignore", UserWarning)
        for key, data in entries:
            archive.writestr(key, data)
    return output.getvalue()


def fixture(slug):
    return zipped([(PROVENANCE[slug]["archive_member"], (FIXTURES / f"{slug}.xml").read_bytes())])


def synthetic(*rows):
    xml = '<dokumente doknr="SYN0001" builddate="20260101"><norm doknr="SYN0001"><metadaten><jurabk>TstG</jurabk><langue>Testgesetz</langue></metadaten></norm>'
    for i, (label, body) in enumerate(rows):
        xml += f'<norm doknr="SYN0001N{i}"><metadaten><jurabk>TstG</jurabk><enbez>{label}</enbez></metadaten><textdaten><text><Content>{body}</Content></text></textdaten></norm>'
    return zipped([("synthetic.xml", xml.encode() + b"</dokumente>")])


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.session = Session(ROOT)
        self.addCleanup(self.session.storage.cleanup)

    def process(self, slug, data=None):
        return self.session.process(slug, data if data is not None else fixture(slug))

    def text(self, directory, slug):
        return (directory / "Bundesrecht" / f"{slug}.md").read_text(encoding="utf-8")


    def test_all_real_excerpts_and_provenance(self):
        for slug, provenance in PROVENANCE.items():
            with self.subTest(slug=slug):
                xml = (FIXTURES / f"{slug}.xml").read_bytes()
                self.assertEqual(source.digest(xml), provenance["fixture_sha256"])
                self.assertEqual(source.parse_xml(xml, "dokumente").attrib, provenance["root_attributes"])
                directory, index, _ = self.process(slug)
                self.assertEqual(index["sources"][slug]["xml_sha256"], provenance["fixture_sha256"])
                self.assertTrue(self.text(directory, slug).startswith("# "))
        with zipfile.ZipFile(self.session.current / "statute2md-bestand.zip") as archive:
            self.assertEqual(len([p for p in archive.namelist() if p.endswith(".md")]), 6)
            self.assertIn("link-index.json", archive.namelist())

    def test_bgb_sentence_after_list_and_nested_letter(self):
        directory, index, _ = self.process("bgb")
        targets = {x["id"]: x for x in index["targets"]}
        self.assertIn("p-434-abs-2-s-1-nr-3", targets)
        self.assertIn("p-434-abs-2-s-2", targets)
        self.assertNotIn("p-434-abs-2-s-3", targets)
        letter = targets["p-434-abs-3-s-1-nr-2-lit-a"]
        self.assertEqual(letter["parent_block"], "p-434-abs-3-s-1-nr-2")
        self.assertIn(["p", "434", "abs", "3", "nr", "2", "lit", "a"], letter["aliases"])
        md = self.text(directory, "bgb")
        self.assertIn("Zu der Beschaffenheit nach", md.splitlines()[targets["p-434-abs-2-s-2"]["markdown_line"] - 1])
        self.assertRegex(md, r"(?m)^#{2,6} § 434 — Sachmangel \^p-434\n\n")
        self.assertTrue(any(line.startswith("*Gliederung: ") and " › " in line for line in md.splitlines()))
        self.assertFalse(any(" › " in line for line in md.splitlines() if line.startswith("###### ")))
        self.assertIn("<strong>Absatz 1</strong></span> ^p-434-abs-1\n\nDie Sache ist frei von Sachmängeln", md)
        self.assertIn(">1.</span> die vereinbarte Beschaffenheit hat,", md)
        self.assertNotIn("§ 434 Absatz 1 ^p-434-abs-1", md)
        self.assertRegex(md.splitlines()[targets["p-434"]["markdown_line"] - 1], r"^#{2,6} § 434 — Sachmangel \^p-434$")
        self.assertNotIn("&#40;1&#41;", md)
        self.assertNotIn("Verweis zur Überschrift: [[#^p-434|§ 434]]", md)

    def test_stvg_sentence_continues_after_list_and_table_spans(self):
        directory, index, _ = self.process("stvg")
        targets = {x["id"]: x for x in index["targets"]}
        md = self.text(directory, "stvg")
        self.assertIn("p-1-abs-3-s-1-nr-2", targets)
        s1 = targets["p-1-abs-3-s-1"]["markdown_line"]
        s2 = targets["p-1-abs-3-s-2"]["markdown_line"]
        self.assertIn("unterbrochen wird", "\n".join(md.splitlines()[s1 - 1:s2 - 1]))
        self.assertIn("<th", md)
        self.assertIn('rowspan="', md)
        self.assertIn('colspan="', md)

    def test_estg_rec_and_math_sup(self):
        directory, index, _ = self.process("estg")
        targets = {x["id"]: x for x in index["targets"]}
        self.assertEqual(targets["p-39b-abs-2-s-2"]["confidence"], "explicit")
        self.assertIn("<sup>360</sup>/<sub>7</sub>", self.text(directory, "estg"))

    def test_alias_and_empty_dt_and_toc(self):
        directory, index, slug = self.process("  SGB I  ", fixture("sgb_1"))
        self.assertEqual(slug, "sgb_1")
        self.assertIn("Quellen-Inhaltsverzeichnis", self.text(directory, slug))
        self.assertTrue(any(x["label"] == "Listeneintrag" and not x["selector"] for x in index["targets"]))

    def test_interlaw_relinks_previous_export_and_project_roundtrip(self):
        first, report, _ = self.process("tstg", synthetic(("§ 1", "<P>Es gilt Artikel 93 des Grundgesetzes.</P>")))
        self.assertEqual(report["references"][0]["status"], "missing_law")
        project = (first / "statute2md-quellprojekt.zip").read_bytes()
        other = Session(ROOT)
        self.addCleanup(other.storage.cleanup)
        other.process(project=project)
        directory, index, _ = other.process("gg", fixture("gg"))
        ref = next(x for x in index["references"] if x["source_law"] == "tstg")
        self.assertEqual((ref["status"], ref["target_id"]), ("resolved", "art-93"))
        gg_lines = self.text(directory, "gg").splitlines()
        gg_targets = {x["id"]: x["markdown_line"] for x in index["targets"] if x["law"] == "gg"}
        self.assertEqual(gg_lines[gg_targets["art-93"] - 1], "### Art 93 ^art-93")
        self.assertTrue(gg_lines[gg_targets["art-93-abs-1"] - 1].endswith("<strong>Absatz 1</strong></span> ^art-93-abs-1"))
        self.assertEqual(gg_lines[gg_targets["gl-010"] - 1], "^gl-010")
        md = self.text(directory, "tstg")
        self.assertIn("[[Bundesrecht/gg#^art-93|Artikel 93 des Grundgesetzes]]", md)
        repeated, after, _ = other.process("gg", fixture("gg"))
        self.assertEqual(self.text(repeated, "tstg"), md)
        self.assertEqual(after["changes"], [])

    def test_lists_ranges_historical_and_injection(self):
        text = ('<P>(1) Siehe §§ 2, 3 und 4 sowie §§ 2 bis 4 und § 2 ff. weiter. Es gilt § 2 a. F. '
                'Ferner §§ 2 und 3 Abs. 1. Siehe &lt;script&gt;x&lt;/script&gt; [[raw#^id|x]] und '
                'https://example.org/Artikel%2093. BGBl. I S. 40.</P>')
        directory, index, _ = self.process("tstg", synthetic(("§ 1", text), ("§ 2", "<P>Text.</P>"),
                                                                ("§ 3", "<P>(1) Text.</P>"), ("§ 4", "<P>Text.</P>")))
        refs = index["references"]
        self.assertEqual(next(r for r in refs if r["original"] == "§ 2 a. F.")["status"], "version_mismatch")
        self.assertIn(["p-2", "p-3", "p-4"], [x.get("range_members") for x in refs])
        self.assertTrue(any(r.get("following") == "ff." for r in refs))
        self.assertTrue(any(r["status"] == "ambiguous" for r in refs))
        self.assertFalse(any(r["original"] == "S. 40" for r in refs))
        self.assertNotIn("<script>", self.text(directory, "tstg"))
        self.assertNotIn("[[raw", self.text(directory, "tstg"))

    def test_unicode_and_mixed_inline_links(self):
        body = '<P>(1) Laut §&#160;<B>2</B> sowie nach Absatz 2 gilt dies. Danach folgt Satz 1.<BR/>Ferner.</P><P>(2) Maß.</P>'
        directory, index, _ = self.process("tstg", synthetic(("§ 1", body), ("§ 2", "<P>Wort.</P>")))
        ref = next(x for x in index["references"] if x["original"] == "§\u00a02")
        self.assertEqual(ref["target_id"], "p-2")
        self.assertIn("<strong>2</strong>", self.text(directory, "tstg"))
        self.assertIn("[[#^p-2|§\u00a02]]", self.text(directory, "tstg"))
        self.assertEqual(normalized("A\u0308\u00a0x")[0], "Ä x")

    def test_unnumbered_provision_keeps_sentence_targets_without_paragraph_labels(self):
        directory, index, _ = self.process("tstg", synthetic(("§ 2a", "<P>Erster Satz. Zweiter Satz.</P>")))
        md = self.text(directory, "tstg")
        self.assertIn("## § 2a ^p-2a\n\nErster Satz. ^p-2a-s-1\n\nZweiter Satz. ^p-2a-s-2", md)
        self.assertNotIn("<strong>Absatz", md)
        self.assertIn("p-2a-s-2", {entry["id"] for entry in index["targets"]})

    def test_hyphens_remain_literal_in_words_and_are_escaped_as_markdown_markers(self):
        body = ("<P>Baden-Württemberg, Sachsen-<B>Anhalt</B> und Schleswig-Holstein.</P>"
                "<P>- Wortlaut.</P><P>---</P>")
        directory, _, _ = self.process("tstg", synthetic(("§ 1", body)))
        md = self.text(directory, "tstg")
        self.assertIn("Baden-Württemberg, Sachsen-<strong>Anhalt</strong> und Schleswig-Holstein.", md)
        self.assertIn("\n&#45; Wortlaut.", md)
        self.assertIn("\n&#45;--", md)

    def test_explicit_ambiguous_and_unnumbered(self):
        directory, index, _ = self.process("tstg", synthetic(("§ 2b", "<P><SUP class='Rec'>2</SUP>Etwas. Mehr.</P>"),
                                                                ("§ 3", "<P>Dr. X kam. Es gilt Satz 1.</P>"),
                                                                ("§ 4", "<P>Siehe § 2b Satz 1 und § 3 Absatz 1.</P>")))
        refs = {x["original"]: x["status"] for x in index["references"]}
        self.assertEqual(refs["§ 2b Satz 1"], "ambiguous")
        self.assertEqual(refs["§ 3 Absatz 1"], "missing_target")
        self.assertEqual(refs["Satz 1"], "resolved")

    def test_reimport_rolls_back_and_lock_is_nonblocking(self):
        directory, report, _ = self.process("gg")
        with self.assertRaises(Error):
            self.process("gg", b"broken")
        self.assertEqual(self.session.current, directory)
        self.assertIs(self.session.index, report)
        with self.session.lock, self.assertRaisesRegex(Error, "läuft noch"):
            self.process("gg")
        exported = (directory / "statute2md-quellprojekt.zip").read_bytes()
        members = source.unpack(exported, project=True)
        members["sources/gg.zip"] = fixture("bgb")
        with self.assertRaisesRegex(Error, "ungültig"):
            self.session.process(project=zipped(members.items()))
        self.assertEqual(self.session.current, directory)


class SourceTests(unittest.TestCase):
    def test_zip_guardrails(self):
        for entries in ([], [("x.xml", b""), ("y.xml", b"")], [("../a.xml", b"")],
                        [("C:/a.xml", b"")], [("/a.xml", b"")], [("a.xml", b""), ("A.XML", b"")],
                        [("a.xml", b""), ("inner.zip", b"")], [("a.xml", b""), ("inner.bin", b"PK\x03\x04")]):
            with self.subTest(entries=entries), self.assertRaises(Error):
                source.unpack(zipped(entries))
        windows_path = zipped([("x/a.xml", b"")]).replace(b"x/a.xml", b"x\\a.xml")
        with self.assertRaises(Error):
            source.unpack(windows_path)
        broken = zipped([("x.xml", b"marker123")]).replace(b"marker123", b"marker124", 1)
        with self.assertRaises(Error):
            source.unpack(broken)
        symlink = zipfile.ZipInfo("link.xml")
        symlink.create_system = 3
        symlink.external_attr = 0o120777 << 16
        with self.assertRaises(Error):
            source.unpack(zipped([(symlink, b"elsewhere")]))
        with self.assertRaisesRegex(Error, "HTML"):
            source.unpack(b"<!doctype html>")

    def test_xml_prolog_is_passive_and_limited(self):
        parse = source.parse_xml
        with patch("socket.create_connection", side_effect=AssertionError), patch("builtins.open", side_effect=AssertionError):
            for protocol in ("http", "https"):
                xml = f'<!DOCTYPE dokumente SYSTEM "{protocol}://www.gesetze-im-internet.de/dtd/1.01/gii-norm.dtd"><dokumente/>'
                self.assertEqual(parse(xml.encode("utf-16"), "dokumente").tag, "dokumente")
            for xml in ('<!DOCTYPE dokumente [<!ENTITY x SYSTEM "file:///etc/passwd">]><dokumente>&x;</dokumente>',
                        '<!DOCTYPE dokumente [<!ENTITY x "y">]><dokumente>&x;&x;</dokumente>',
                        '<!DOCTYPE dokumente SYSTEM "https://example.org/a"><dokumente/>',
                        '<dokumente xmlns:x="http://www.w3.org/2001/XInclude"><x:include href="file:///etc/passwd"/></dokumente>',
                        '<dokumente><unclosed></dokumente>'):
                with self.assertRaises(Error):
                    parse(xml.encode(), "dokumente")
            with self.assertRaises(Error):
                parse(b"<dokumente><a><b/></a></dokumente>", "dokumente", max_depth=2)
            with self.assertRaises(Error):
                parse(b"<dokumente>abcd</dokumente>", "dokumente", max_text=3)

    def test_http_redirect_retry_limits_and_statuses(self):
        class Response:
            def __init__(self, status, location=None, data=b"zip"):
                self.status, self.location, self.stream = status, location, io.BytesIO(data)

            def __enter__(self): return self
            def __exit__(self, *args): return False
            def getheader(self, key): return self.location if key == "Location" else None
            def read1(self, count): return self.stream.read(count)

        class Connection:
            def __init__(self, response): self.response, self.sock = response, self
            def connect(self):
                if isinstance(self.response, (socket.gaierror, ssl.SSLError)): raise self.response
            def settimeout(self, timeout): pass
            def request(self, *args, **kwargs): pass
            def close(self): pass
            def getresponse(self):
                if isinstance(self.response, Exception): raise self.response
                return self.response

        def download(*responses, limit=20, log=quiet):
            iterator = iter(responses)
            with patch("time.sleep"):
                return source.download(source.BASE + "/gg/xml.zip", limit, log, connect=lambda *a, **k: Connection(next(iterator)))

        logs = []
        self.assertEqual(download(Response(429), Response(503), Response(200), log=lambda _, msg: logs.append(msg))[0], b"zip")
        self.assertTrue(any("Verbindung" in message for message in logs))
        self.assertTrue(any("HTTP 200" in message for message in logs))
        self.assertEqual(logs[-1], "3 Bytes empfangen.")
        self.assertEqual(download(Response(302, "/bgb/xml.zip"), Response(200))[1], source.BASE + "/bgb/xml.zip")
        for status in [403, 404]:
            with self.assertRaisesRegex(Error, str(status)):
                download(Response(status))
        with self.assertRaisesRegex(Error, "Quellenadresse"):
            download(Response(302, "https://example.org/a"))
        with self.assertRaisesRegex(Error, "Zeitlimit beim Warten auf die HTTP-Antwort") as timed_out:
            download(socket.timeout(), socket.timeout(), socket.timeout())
        self.assertEqual(timed_out.exception.archive_url, source.BASE + "/gg/xml.zip")
        with self.assertRaisesRegex(Error, "DNS-Auflösung"):
            download(socket.gaierror())
        with self.assertRaisesRegex(Error, "TLS-Verbindung"):
            download(ssl.SSLError())
        with self.assertRaisesRegex(Error, "Größenlimit"):
            download(Response(200, data=b"abcdef"), limit=3)

    def test_catalog_alias_and_identity(self):
        toc = b'<items><item><title>Sozialgesetzbuch (SGB I)</title><link>http://www.gesetze-im-internet.de/sgb_1/xml.zip</link></item><item><title>SGB I</title><link>https://evil.example/other/xml.zip</link></item></items>'
        self.assertEqual(set(source.catalog_entries(toc)), {"sgb_1"})
        def fetch(url, limit, log):
            return (toc if url == source.CATALOG else fixture("sgb_1")), url
        self.assertEqual(source.acquire("SGB I", None, quiet, fetch).slug, "sgb_1")
        with self.assertRaisesRegex(Error, "Gesetzesidentität"):
            source.acquire("gg", fixture("bgb"), quiet)

    def test_https_connection_honors_proxy_and_no_proxy(self):
        host = "www.gesetze-im-internet.de"
        context = ssl.create_default_context()
        with (patch("statute_source.getproxies", return_value={"https": "http://proxy.example:8080"}),
              patch("statute_source.proxy_bypass", return_value=False) as bypass,
              patch("statute_source.SourceHTTPSConnection") as https):
            connection = source.connect_https(host, timeout=5, context=context)
            https.assert_called_once_with("proxy.example", 8080, timeout=5, context=context)
            connection.set_tunnel.assert_called_once_with(host, port=443, headers={})
            bypass.return_value = True
            https.reset_mock()
            connection = source.connect_https(host, timeout=5, context=context)
            https.assert_called_once_with(host, timeout=5, context=context)
            connection.set_tunnel.assert_not_called()


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.workspace = TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)

    def test_notebook_has_three_cells_and_imports_gui(self):
        self.assertEqual([cell["cell_type"] for cell in NB["cells"]], ["markdown", "markdown", "code"])
        code = "".join(NB["cells"][2]["source"])
        compile(code, "oberflaeche", "exec")
        self.assertIn("repository_gui.start(root, token=token, email=email, username=username, log_outputs=log_outputs)", code)

    def test_notebook_bootstrap_uses_username_secret_and_passes_it_to_gui(self):
        checkout = self.root / "code"
        code = "".join(NB["cells"][2]["source"]).replace("/content/statute2md", str(checkout))
        # Isolate imports and simulate only Colab secrets and the network clone.
        harness = f"""
import pathlib, shutil, subprocess, sys, types
from unittest.mock import patch
checkout = pathlib.Path({str(checkout)!r})
source = pathlib.Path({str(ROOT)!r})
secrets = {{'gittoken': 'test-token', 'github_username': 'another-user', 'gitemail': 'test@example.org'}}
colab = types.ModuleType('google.colab')
colab.userdata = types.SimpleNamespace(get=secrets.get)
commands = []
def clone(args, **kwargs):
    commands.append(args)
    assert args == ['git', 'clone', '--branch', 'main', '--', 'https://github.com/another-user/statute2md.git', str(checkout)]
    env = kwargs['env']
    assert env['GIT_CONFIG_KEY_0'] == 'http.https://github.com/another-user/statute2md.git.extraHeader'
    checkout.mkdir()
    for module in source.glob('*.py'):
        shutil.copy2(module, checkout / module.name)
    return subprocess.CompletedProcess(args, 0, '', '')
with patch.dict(sys.modules, {{'google.colab': colab}}), patch('subprocess.run', clone):
    exec({code!r})
    assert statute_gui.git.username == 'another-user'
    assert statute_gui.git.remote == 'https://github.com/another-user/statute2md.git'
    assert statute_gui.repo.xml == checkout / 'xml-sources'
    assert statute_gui.repo.md == checkout / 'md_files'
    statute_gui.close()
    with patch('subprocess.check_output', side_effect=['https://github.com/another-user/statute2md.git', 'main']):
        exec({code!r})
        statute_gui.close()
    secrets['github_username'] = 'different-user'
    with patch('subprocess.check_output', side_effect=['https://github.com/another-user/statute2md.git', 'main']):
        try:
            exec({code!r})
        except RuntimeError as error:
            assert 'different-user/statute2md' in str(error)
        else:
            raise AssertionError('Mismatching repository must be rejected')
    secrets['github_username'] = None
    try:
        exec({code!r})
    except RuntimeError as error:
        assert 'github_username' in str(error)
    else:
        raise AssertionError('Missing username secret must be rejected')
assert len(commands) == 1
"""
        result = subprocess.run([sys.executable, "-c", harness], cwd=self.root,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_custom_username_verifies_origin_and_controls_pull(self):
        git = RepositoryGit(self.root, username="another-user", email="test@example.org")
        git.call("init", "-b", "main")
        git.call("remote", "add", "origin", git.remote)
        git.verify()
        real_call = git.call
        def local_call(*args, **kwargs):
            if kwargs.get("network"):
                return subprocess.CompletedProcess([], 0, "", "")
            return real_call(*args, **kwargs)
        with patch.object(git, "call", side_effect=local_call) as call:
            git.clone_or_pull()
            self.assertEqual(call.call_args.args, ("pull", "--ff-only", git.remote, "main"))
        git.call("remote", "set-url", "origin", "https://github.com/rps007/statute2md.git")
        with self.assertRaisesRegex(Error, "anderes Repository"):
            git.verify()
        for username in ("../other", "", "other/repo", "-option", "name@github.com"):
            with self.subTest(username=username), self.assertRaises(Error):
                RepositoryGit(self.root, username=username)

    def test_add_transform_download_and_delete(self):
        calls = []

        def fetch(url, limit, log):
            calls.append(url)
            return fixture("gg"), url

        repo = RepositoryFiles(self.root, fetch=fetch)
        repo.add_source("GG", quiet)
        repo.add_source("bverfgg", quiet, fixture("bverfgg"))
        self.assertEqual(calls, [source.BASE + "/gg/xml.zip"])
        self.assertEqual(repo.files("xml"), ["bverfgg.xml", "gg.xml"])
        self.assertEqual(repo.files("md"), [])
        progress = []

        def record(percent, message):
            progress.append((percent, message))
            if percent == 100:
                self.assertEqual(repo.files("md"), ["bverfgg.md", "gg.md"])

        repo.transform(repo.files("xml"), quiet, progress=record)
        amounts = [percent for percent, _ in progress]
        self.assertEqual((amounts[0], amounts[-1]), (0, 100))
        self.assertEqual(amounts, sorted(amounts))
        self.assertIn(98, amounts)
        self.assertTrue(any("prüfen" in message for _, message in progress))
        self.assertEqual(repo.files("md"), ["bverfgg.md", "gg.md"])
        index = json.loads((repo.md / "link-index.json").read_bytes())
        self.assertTrue(any(ref["target_path"] == "Bundesrecht/gg.md" and ref["source_law"] == "bverfgg"
                            for ref in index["references"]))
        download = repo.download(["gg.xml"], ["bverfgg.md"], self.root)
        with zipfile.ZipFile(download) as archive:
            self.assertEqual(set(archive.namelist()), {"xml-sources/gg.xml", "md_files/bverfgg.md"})
        with zipfile.ZipFile(repo.md / "statute2md-bestand.zip") as archive:
            self.assertIn("Bundesrecht/gg.md", archive.namelist())
        repo.delete(["gg.xml"], ["gg.md"], quiet)
        self.assertEqual(repo.files("xml"), ["bverfgg.xml"])
        self.assertEqual(repo.files("md"), ["bverfgg.md"])
        self.assertNotIn("[[Bundesrecht/gg#", (repo.md / "bverfgg.md").read_text(encoding="utf-8"))

    def test_plain_xml_and_invalid_batch_preserve_existing_export(self):
        repo = RepositoryFiles(self.root)
        repo.xml.mkdir()
        (repo.xml / "gg.xml").write_bytes((FIXTURES / "gg.xml").read_bytes())
        repo.transform(["gg.xml"], quiet)
        original = (repo.md / "gg.md").read_bytes()
        (repo.xml / "bgb.xml").write_bytes(b"broken")
        with self.assertRaises(Error):
            repo.transform(["gg.xml", "bgb.xml"], quiet)
        self.assertEqual((repo.md / "gg.md").read_bytes(), original)
        with self.assertRaises(Error):
            repo.delete(["../gg.xml"], [], quiet)

    def test_git_commit_excludes_unrelated_staging(self):
        git = RepositoryGit(self.root, username="another-user", email="test@example.org")
        git.call("init", "-b", "main")
        git.call("remote", "add", "origin", git.remote)
        git.call("config", "user.name", "Test")
        git.call("config", "user.email", "test@example.org")
        for folder in ("xml-sources", "md_files"):
            (self.root / folder).mkdir()
            (self.root / folder / ".gitkeep").touch()
        git.call("add", ".")
        git.call("commit", "-m", "Initial")
        (self.root / "outside.txt").touch()
        git.call("add", "outside.txt")
        (self.root / "xml-sources" / "gg.xml").touch()
        git.commit("Add GG")
        self.assertEqual(git.call("show", "-s", "--format=%an", "HEAD").stdout.strip(), "another-user")
        self.assertEqual(git.call("show", "--format=", "--name-only", "HEAD").stdout.strip(), "xml-sources/gg.xml")
        self.assertEqual(git.call("diff", "--cached", "--name-only").stdout.strip(), "outside.txt")

    def test_clone_and_push_use_the_expected_remote_and_branch(self):
        remote = "https://github.com/another-user/statute2md.git"
        git = RepositoryGit(self.root / "checkout", username="another-user")
        result = subprocess.CompletedProcess([], 0, "", "")
        with patch("repository_git.subprocess.run", return_value=result) as run:
            self.assertEqual(git.clone_or_pull(), "Repository geklont.")
            self.assertEqual(run.call_args.args[0], ["git", "-C", str(self.root), "clone", "--branch", "main", "--", remote, str(git.root)])
        with patch.object(git, "verify"), patch.object(git, "call") as call:
            git.push()
            call.assert_called_once_with("push", remote, "main:main", network=True)

    def test_git_network_worker_uses_supplied_credentials_and_times_out(self):
        logs = []
        git = RepositoryGit(self.root, username="another-user", token="fixture-token", log=lambda stage, message: logs.append((stage, message)))
        with patch("repository_git.subprocess.run", side_effect=subprocess.TimeoutExpired(["git", "pull"], GIT_TIMEOUT)) as run:
            with self.assertRaisesRegex(Error, f"nach {GIT_TIMEOUT} Sekunden"):
                git.call("pull", "--ff-only", "origin", "main", network=True)
        self.assertEqual(run.call_args.kwargs["timeout"], GIT_TIMEOUT)
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GCM_INTERACTIVE"], "never")
        credential_index = int(env["GIT_CONFIG_COUNT"]) - 1
        self.assertEqual(env[f"GIT_CONFIG_KEY_{credential_index}"], "http.https://github.com/another-user/statute2md.git.extraHeader")
        self.assertTrue(env[f"GIT_CONFIG_VALUE_{credential_index}"].startswith("Authorization: Basic "))
        self.assertEqual(logs[0][0], "Git")
        self.assertIn("pull", logs[0][1])
        self.assertNotIn("fixture-token", str(logs))

    def test_clone_button_updates_ui_after_git_success_and_timeout(self):
        try:
            from repository_gui import RepositoryGUI
        except ModuleNotFoundError as error:
            if error.name not in {"ipywidgets", "IPython"}:
                raise
            self.skipTest("ipywidgets is an optional notebook dependency")

        git = RepositoryGit(self.root)
        remote = self.root / "remote.git"
        git.call("init", "-b", "main")
        git.call("init", "--bare", str(remote))
        git.call("-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "--allow-empty", "-m", "Initial")
        git.call("remote", "add", "origin", str(remote))
        git.call("push", "origin", "main")
        gui = RepositoryGUI(self.root)
        self.addCleanup(gui.close)
        clone = next(button for button in gui.buttons if button.description == "Clone")
        with patch("repository_git.repository_remote", return_value=str(remote)):
            gui.git.remote = str(remote)
            clone.click()
            self.assertIn("Arbeitskopie aktualisiert", gui.status.value)
            self.assertTrue(any("pull" in output["text"] for output in gui.log.outputs))
            self.assertFalse(hasattr(gui, "checkout"))
            self.assertEqual(gui.repo.root, self.root)
            git.call("switch", "-c", "other-branch")
            clone.click()
            self.assertIn("muss auf main stehen", gui.status.value)
            self.assertEqual(gui.repo.root, self.root)
            git.call("switch", "main")
            with patch.object(gui.git, "verify"), patch("repository_git.subprocess.run",
                    side_effect=subprocess.TimeoutExpired(["git", "pull"], GIT_TIMEOUT)):
                clone.click()
        self.assertIn(f"nach {GIT_TIMEOUT} Sekunden", gui.status.value)
        self.assertFalse(gui.running)
        self.assertTrue(all(not control.disabled for control in gui.controls))

    def test_widget_sections_keep_controls_visible_and_transform_reports_result(self):
        try:
            from repository_gui import RepositoryGUI
        except ModuleNotFoundError as error:
            if error.name not in {"ipywidgets", "IPython"}:
                raise
            self.skipTest("ipywidgets is an optional notebook dependency")

        gui = RepositoryGUI(self.root)
        self.addCleanup(gui.close)
        heading, columns = gui.panel.children
        self.assertEqual(heading.value, "<h2>xml2obsidian.ipynb with github connection</h2>")
        self.assertEqual(columns.layout.grid_template_columns, "minmax(0, 2fr) minmax(0, 1fr)")
        controls, log_panel = columns.children
        self.assertIs(log_panel.children[-1], gui.log)
        self.assertEqual(gui.log.layout.overflow, "auto")
        sections = (*controls.children[:2], controls.children[-1])
        self.assertIn('href="https://www.gesetze-im-internet.de/"', sections[0].children[0].value)
        self.assertIn(f"Lokale ZIP-Files in {gui.repo.xml} hochladen", sections[0].children[0].value)
        self.assertEqual(sections[1].children[0].value, "<h3>Dateioperationen auf Colab</h3>")
        self.assertEqual(sections[2].children[0].value, "<h3>Anbindung an Github</h3>")
        actions = sections[1].children[2]
        self.assertEqual(actions.layout.grid_template_columns, "repeat(3, minmax(0, 1fr))")
        self.assertEqual(tuple(button.description for button in actions.children),
                         ("Transform", "Delete", "Download", "Obsidian-ZIP", "Quellprojekt-ZIP", "Refresh"))
        for section, expected in zip(sections, (("Add Sources",), ("Transform", "Delete", "Download", "Refresh"),
                                                ("Clone", "Commit", "Push"))):
            children = [section]
            for widget in children:
                self.assertNotIn(widget._view_name, {"TabView", "AccordionView"})
                self.assertNotEqual(widget.layout.display, "none")
                children.extend(getattr(widget, "children", ()))
            self.assertTrue(set(expected) <= {getattr(widget, "description", None) for widget in children})
        gui.repo.xml.mkdir(exist_ok=True)
        (gui.repo.xml / "original-gg.zip").write_bytes(fixture("gg"))
        gui.abbr.value = "GG"
        gui.zip_path.value = "original-gg.zip"
        next(button for button in gui.buttons if button.description == "Add Sources").click()
        self.assertIn("gg.xml", gui.xml.options)
        gui.xml.value = ("gg.xml",)
        transform = next(button for button in gui.buttons if button.description == "Transform")
        callback_thread = get_ident()
        progress_threads, progress_values, log_threads, worker_threads = [], [], [], []
        gui.transform_message.observe(lambda _: progress_threads.append(get_ident()), names="value")
        gui.transform_bar.observe(lambda change: progress_values.append(change.new), names="value")
        gui.log.observe(lambda _: log_threads.append(get_ident()), names="outputs")
        convert = gui.repo.transform

        def transform_in_worker(*args, **kwargs):
            worker_threads.append(get_ident())
            return convert(*args, **kwargs)

        with patch.object(gui.repo, "transform", side_effect=transform_in_worker):
            transform.click()
            self.assertFalse(gui.running)
            self.assertEqual(gui.transform_bar.value, 100)
            self.assertEqual(gui.transform_bar.bar_style, "success")
            self.assertIn("Transformation abgeschlossen", gui.transform_message.value)
            self.assertIn("gg.md", gui.md.options)
            self.assertTrue(any(0 < value < 100 for value in progress_values))
            gui.xml.value = ()
            transform.click()
        self.assertTrue(worker_threads and all(thread != callback_thread for thread in worker_threads))
        self.assertEqual(set(progress_threads + log_threads), {callback_thread})
        self.assertLess(gui.transform_bar.value, 100)
        self.assertEqual(gui.transform_bar.bar_style, "danger")
        self.assertIn("Transformation abgebrochen", gui.transform_message.value)
        self.assertTrue(all(not control.disabled for control in gui.controls))

    def test_widget_downloads_single_markdown_and_keeps_session_log(self):
        try:
            from repository_gui import RepositoryGUI, start
        except ModuleNotFoundError as error:
            if error.name not in {"ipywidgets", "IPython"}:
                raise
            self.skipTest("ipywidgets is an optional notebook dependency")

        gui = RepositoryGUI(self.root)
        self.addCleanup(gui.close)
        for folder, name in ((gui.repo.xml, "gg.xml"), (gui.repo.md, "gg.md")):
            folder.mkdir()
            (folder / name).touch()
        gui.refresh()
        gui.xml.value = ("gg.xml",)
        gui.md.value = ("gg.md",)
        for number in range(225):
            gui.record("Test", f"Protokolleintrag {number}")
        previous = gui.log.outputs
        download = next(button for button in gui.buttons if button.description == "Download")
        with patch("repository_gui.colab_files") as files:
            download.click()
            files.download.assert_called_once_with(str(gui.repo.md / "gg.md"))
        self.assertEqual(len(previous), 225)
        self.assertEqual(gui.log.outputs[:len(previous)], previous)
        self.assertIn("Download · Gestartet", gui.log.outputs[-2]["text"])
        self.assertIn("Fertig · gg.md", gui.log.outputs[-1]["text"])
        with patch("repository_gui.display"):
            restarted = start(self.root, log_outputs=gui.log.outputs)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.log.outputs, gui.log.outputs)


if __name__ == "__main__":
    unittest.main()
