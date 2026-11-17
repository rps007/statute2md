from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import statute_source as source


class ConnectionTests(unittest.TestCase):
    def test_ipv6_failure_falls_back_to_ipv4_before_verified_tls(self):
        logs = []
        context = ssl.create_default_context()
        connection = source.SourceHTTPSConnection("www.gesetze-im-internet.de", context=context, timeout=2)
        connection.log = lambda stage, message: logs.append(message)
        ipv6, ipv4, tls = Mock(), Mock(), Mock()
        ipv6.connect.side_effect = TimeoutError()
        addresses = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0)),
                     (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with (patch("statute_source.socket.getaddrinfo", return_value=addresses),
              patch("statute_source.socket.socket", side_effect=[ipv6, ipv4]),
              patch.object(context, "wrap_socket", return_value=tls) as wrap):
            connection.connect()
            wrap.assert_called_once_with(ipv4, server_hostname="www.gesetze-im-internet.de")
        ipv6.close.assert_called_once()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        text = "\n".join(logs)
        self.assertIn("IPv6 fehlgeschlagen", text)
        self.assertIn("IPv4 hergestellt", text)
        self.assertLess(text.index("DNS-Auflösung abgeschlossen"), text.index("TCP-Verbindung"))
        self.assertLess(text.index("IPv4 hergestellt"), text.index("TLS-Handschlag"))
        connection.close()

    def test_tcp_timeout_reports_the_phase_and_retries(self):
        transport = Mock()
        transport.connect.side_effect = TimeoutError()
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with (patch("statute_source.getproxies", return_value={}),
              patch("statute_source.socket.getaddrinfo", return_value=addresses),
              patch("statute_source.socket.socket", return_value=transport),
              patch("statute_source.time.sleep")):
            with self.assertRaisesRegex(source.SourceConnectionError, "Zeitlimit beim TCP-Verbindungsaufbau"):
                source.download(source.BASE + "/sgg/xml.zip", source.MiB, lambda *x: None)
        self.assertEqual(transport.connect.call_count, 3)
        self.assertEqual(transport.close.call_count, 3)

    def test_proxy_rejection_is_distinct_and_does_not_log_credentials(self):
        logs = []
        context = ssl.create_default_context()
        with (patch("statute_source.getproxies", return_value={"https": "http://secret-user:secret-password@proxy.test"}),
              patch("statute_source.proxy_bypass", return_value=False)):
            connection = source.connect_https("www.gesetze-im-internet.de", context=context, timeout=2)
        connection.log = lambda stage, message: logs.append(message)
        with (patch.object(connection, "_create_connection", return_value=Mock()),
              patch("http.client.HTTPConnection._tunnel", side_effect=OSError("Tunnel connection failed: 403 Forbidden secret-password"))):
            with self.assertRaises(OSError):
                connection.connect()
        self.assertEqual(connection.phase, "beim Proxy-CONNECT (HTTP 403)")
        text = "\n".join(logs)
        self.assertIn("Proxy-CONNECT abgewiesen: HTTP 403", text)
        self.assertNotIn("secret-password", text)
        self.assertNotIn("secret-user", text)
        self.assertNotIn("proxy.test", text)
        connection.close()

    def test_local_https_request_and_untrusted_certificate(self):
        if shutil.which("openssl") is None:
            self.skipTest("openssl is required to generate a local test certificate")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        with TemporaryDirectory() as folder:
            cert, key = Path(folder) / "cert.pem", Path(folder) / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
                            "-addext", "subjectAltName=DNS:localhost"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(cert, key)
            with HTTPServer(("127.0.0.1", 0), Handler) as server:
                server.socket = server_context.wrap_socket(server.socket, server_side=True)
                server.timeout = 5
                # Trust this test certificate explicitly, then verify that default trust rejects it.
                for trusted in (True, False):
                    with self.subTest(trusted=trusted):
                        context = ssl.create_default_context(cafile=str(cert)) if trusted else ssl.create_default_context()
                        connection = source.SourceHTTPSConnection("localhost", server.server_port, context=context, timeout=2)
                        worker = Thread(target=server.handle_request, daemon=True)
                        worker.start()
                        try:
                            if trusted:
                                connection.request("GET", "/")
                                with connection.getresponse() as response:
                                    self.assertEqual(response.status, 200)
                                    self.assertEqual(response.read(), b"ok")
                            else:
                                with self.assertRaises(ssl.SSLCertVerificationError):
                                    connection.connect()
                                self.assertEqual(connection.phase, "beim TLS-Handschlag")
                        finally:
                            connection.close()
                            worker.join(timeout=6)
                        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
