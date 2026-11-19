"""Public scheme and host, and the desk's own TLS listener."""

import ssl
import subprocess
import tempfile
import threading
import unittest
import warnings
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path

from app import config
from app import patreon
from app.origin import resolve_public_origin
from app.web import App, _server_tls
from http.server import ThreadingHTTPServer


class OriginTests(unittest.TestCase):
    def test_a_local_proxy_sets_https_and_the_public_host(self):
        scheme, host = resolve_public_origin(
            host="127.0.0.1:8080",
            tls=False,
            peer="127.0.0.1",
            trust_proxy=False,
            forwarded_proto="https",
            forwarded_host="desk.example.com",
        )
        self.assertEqual((scheme, host), ("https", "desk.example.com"))

    def test_envoy_forwarded_header_and_a_nonstandard_port(self):
        scheme, host = resolve_public_origin(
            host="10.0.0.3:8080",
            tls=False,
            peer="10.0.0.8",
            trust_proxy=True,
            forwarded='for=203.0.113.5;proto=https;host="desk.example.com"',
            forwarded_port="8443",
        )
        self.assertEqual((scheme, host), ("https", "desk.example.com:8443"))

    def test_default_https_port_is_left_off(self):
        scheme, host = resolve_public_origin(
            host="desk.example.com",
            tls=False,
            peer="10.0.0.8",
            trust_proxy=True,
            forwarded_proto="https",
            forwarded_port="443",
        )
        self.assertEqual((scheme, host), ("https", "desk.example.com"))

    def test_a_remote_client_cannot_spoof_the_proxy_headers(self):
        scheme, host = resolve_public_origin(
            host="203.0.113.9:8080",
            tls=False,
            peer="203.0.113.9",
            trust_proxy=False,
            forwarded_proto="https",
            forwarded_host="evil.example",
        )
        self.assertEqual((scheme, host), ("http", "203.0.113.9:8080"))

    def test_direct_tls_cannot_be_downgraded(self):
        scheme, host = resolve_public_origin(
            host="desk.example.com",
            tls=True,
            peer="203.0.113.9",
            trust_proxy=True,
            forwarded_proto="http",
        )
        self.assertEqual((scheme, host), ("https", "desk.example.com"))

    def test_a_broken_host_is_ignored(self):
        scheme, host = resolve_public_origin(
            host="desk.example.com",
            tls=False,
            peer="127.0.0.1",
            trust_proxy=False,
            forwarded_proto="https",
            forwarded_host="desk.example.com\r\nX-Injected: 1",
        )
        self.assertEqual((scheme, host), ("https", "desk.example.com"))

    def test_redirect_and_webhook_use_the_request_scheme(self):
        previous = config.PATREON_REDIRECT_URI
        config.PATREON_REDIRECT_URI = ""
        try:
            self.assertEqual(
                patreon.redirect_uri("desk.example.com", "https"),
                "https://desk.example.com/patreon/callback",
            )
            self.assertEqual(
                patreon.webhook_url("desk.example.com", "https"),
                "https://desk.example.com/patreon/webhook",
            )
        finally:
            config.PATREON_REDIRECT_URI = previous


class TlsListenerTests(unittest.TestCase):
    def test_tls_requires_both_files(self):
        previous = (config.TLS_CERT, config.TLS_KEY)
        config.TLS_CERT = "/tmp/editting-cert-only"
        config.TLS_KEY = ""
        try:
            with self.assertRaises(SystemExit):
                _server_tls()
        finally:
            config.TLS_CERT, config.TLS_KEY = previous

    def test_the_listener_uses_tls_1_2_or_newer(self):
        previous = (config.TLS_CERT, config.TLS_KEY)
        tmp = Path(tempfile.mkdtemp(prefix="editting-tls-"))
        cert = tmp / "cert.pem"
        key = tmp / "key.pem"
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", str(key), "-out", str(cert),
                "-days", "1", "-nodes", "-subj", "/CN=localhost",
            ],
            check=True,
            capture_output=True,
        )
        config.TLS_CERT = str(cert)
        config.TLS_KEY = str(key)
        context = _server_tls()
        server = ThreadingHTTPServer(("127.0.0.1", 0), App)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
            self.assertIn(
                context.maximum_version,
                (ssl.TLSVersion.TLSv1_3, ssl.TLSVersion.MAXIMUM_SUPPORTED),
            )
            client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client.minimum_version = ssl.TLSVersion.TLSv1_2
            client.check_hostname = False
            client.verify_mode = ssl.CERT_NONE
            conn = HTTPSConnection("127.0.0.1", port, context=client, timeout=10)
            conn.request("GET", "/healthz")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"ok")
            self.assertEqual(response.getheader("Strict-Transport-Security"), "max-age=31536000")

            weak = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            weak.check_hostname = False
            weak.verify_mode = ssl.CERT_NONE
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    weak.minimum_version = ssl.TLSVersion.TLSv1
                    weak.maximum_version = ssl.TLSVersion.TLSv1
            except ValueError:
                weak = None
            if weak is not None:
                old = HTTPSConnection("127.0.0.1", port, context=weak, timeout=10)
                with self.assertRaises(ssl.SSLError):
                    old.request("GET", "/healthz")
                    old.getresponse()
        finally:
            server.shutdown()
            server.server_close()
            config.TLS_CERT, config.TLS_KEY = previous
