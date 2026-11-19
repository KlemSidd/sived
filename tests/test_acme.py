"""Let's Encrypt is optional. These tests use a local certificate directory."""

import json
import subprocess
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app import acme, config
from app.web import App

TOKEN = "token123456"


class Directory(BaseHTTPRequestHandler):
    desk_port = 0
    seen = ""

    def do_HEAD(self):
        if self.path != "/nonce":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Replay-Nonce", "nonce-1")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path == "/directory":
            self._json(
                {
                    "newNonce": self._url("/nonce"),
                    "newAccount": self._url("/new-account"),
                    "newOrder": self._url("/new-order"),
                }
            )
            return
        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        self.rfile.read(length)
        if self.path == "/new-account":
            self._json({"status": "valid"}, 201, self._url("/acct/1"))
            return
        if self.path == "/new-order":
            self._json(
                {
                    "status": "pending",
                    "authorizations": [self._url("/authz/1")],
                    "finalize": self._url("/finalize"),
                },
                201,
                self._url("/order/1"),
            )
            return
        if self.path == "/authz/1":
            if Directory.seen:
                self._json({"status": "valid"})
            else:
                self._json(
                    {
                        "status": "pending",
                        "challenges": [
                            {
                                "type": "http-01",
                                "status": "pending",
                                "url": self._url("/chall/1"),
                                "token": TOKEN,
                            }
                        ],
                    }
                )
            return
        if self.path == "/chall/1":
            conn = HTTPConnection("127.0.0.1", Directory.desk_port, timeout=5)
            conn.request("GET", f"/.well-known/acme-challenge/{TOKEN}")
            response = conn.getresponse()
            body = response.read().decode()
            conn.close()
            if response.status != 200 or not body.startswith(TOKEN + "."):
                self._json({"detail": "the challenge was not published"}, 400)
                return
            Directory.seen = body
            self._json({"status": "valid"})
            return
        if self.path == "/finalize":
            self._json({"status": "valid", "certificate": self._url("/cert")})
            return
        if self.path == "/cert":
            raw = Directory.certificate
            self.send_response(200)
            self.send_header("Replay-Nonce", "nonce-cert")
            self.send_header("Content-Type", "application/pem-certificate-chain")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self._json({"detail": "unexpected request"}, 404)

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}{path}"

    def _json(self, body: dict, status: int = 200, location: str = "") -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Replay-Nonce", "nonce-next")
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        return


class AcmeTests(unittest.TestCase):
    def setUp(self):
        self.previous = (
            config.DATA_DIR,
            config.ACME_DOMAIN,
            config.ACME_EMAIL,
            config.ACME_DIRECTORY,
            config.ACME_STAGING,
            config.ACME_HTTP_PORT,
        )
        self.tmp = Path(tempfile.mkdtemp(prefix="editting-acme-"))
        config.DATA_DIR = self.tmp
        config.ACME_DOMAIN = ""
        config.ACME_EMAIL = ""
        config.ACME_DIRECTORY = ""
        config.ACME_STAGING = False
        config.ACME_HTTP_PORT = "0"
        Directory.seen = ""
        Directory.certificate = b""

    def tearDown(self):
        (
            config.DATA_DIR,
            config.ACME_DOMAIN,
            config.ACME_EMAIL,
            config.ACME_DIRECTORY,
            config.ACME_STAGING,
            config.ACME_HTTP_PORT,
        ) = self.previous

    def test_http_challenge_issues_the_certificate(self):
        cert, _key = _self_signed(self.tmp)
        Directory.certificate = cert.read_bytes()
        desk = _listen(App)
        directory = _listen(Directory)
        Directory.desk_port = desk.server_address[1]
        config.ACME_DIRECTORY = f"http://127.0.0.1:{directory.server_address[1]}/directory"
        try:
            acme.issue_certificate("desk.example.com", "editor@example.com", self.tmp / "acme")
        finally:
            desk.shutdown()
            directory.shutdown()
            desk.server_close()
            directory.server_close()
        saved = (self.tmp / "acme" / "cert.pem").read_bytes()
        self.assertEqual(saved, Directory.certificate)
        self.assertTrue(Directory.seen.startswith(TOKEN + "."))
        self.assertGreater(len(Directory.seen.split(".", 1)[1]), 20)
        self.assertIn("/acct/1", (self.tmp / "acme" / "account.url").read_text())
        self.assertEqual((self.tmp / "acme" / "account.pem").stat().st_mode & 0o077, 0)

    def test_a_name_and_an_email_are_required(self):
        with self.assertRaises(acme.AcmeError) as caught:
            acme.issue_certificate("localhost", "editor@example.com", self.tmp)
        self.assertEqual(str(caught.exception), "a domain is required to request a certificate")
        config.ACME_DOMAIN = "desk.example.com"
        config.ACME_EMAIL = ""
        with self.assertRaises(acme.AcmeError) as caught:
            acme.server_context()
        self.assertEqual(str(caught.exception), "an email is required to request a certificate")

    def test_a_fresh_certificate_is_used_without_the_directory(self):
        root = self.tmp / "acme"
        root.mkdir()
        cert, key = _self_signed(root)
        cert.replace(root / "cert.pem")
        key.replace(root / "domain.pem")
        config.ACME_DOMAIN = "desk.example.com"
        config.ACME_EMAIL = "editor@example.com"
        config.ACME_DIRECTORY = "http://127.0.0.1:1/directory"
        context = acme.server_context()
        self.assertIsNotNone(context)
        self.assertEqual(context.minimum_version, __import__("ssl").TLSVersion.TLSv1_2)

    def test_staging_is_the_lets_encrypt_staging_directory(self):
        config.ACME_STAGING = True
        self.assertEqual(acme._directory_url(), acme.STAGING)
        config.ACME_DIRECTORY = "http://127.0.0.1:9/directory"
        self.assertEqual(acme._directory_url(), "http://127.0.0.1:9/directory")


def _listen(handler) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _self_signed(directory: Path) -> tuple[Path, Path]:
    cert = directory / "issued.pem"
    key = directory / "issued.key"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(key), "-out", str(cert),
            "-days", "90", "-nodes", "-subj", "/CN=desk.example.com",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key
