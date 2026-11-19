"""Optional Let's Encrypt certificate for the desk's own TLS listener.

Leave this off when a proxy terminates TLS, or when TLS_CERT and TLS_KEY are
set. HTTP-01 needs port 80 to reach this process, or a proxy that forwards
/.well-known/acme-challenge/ to it. openssl does the key and certificate work.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import ssl
import subprocess
import sys
import threading
import time
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from app import config

PRODUCTION = "https://acme-v02.api.letsencrypt.org/directory"
STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"
_RENEW_WITHIN = 30 * 24 * 60 * 60
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_DOMAIN = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)
_challenges: dict[str, str] = {}
_lock = threading.Lock()


class AcmeError(Exception):
    """The certificate was not issued. The message is safe to show."""


def write_challenge(handler) -> bool:
    """Answer HTTP-01. False means this request is not a challenge."""
    path = urlsplit(handler.path).path
    prefix = "/.well-known/acme-challenge/"
    if not path.startswith(prefix):
        return False
    token = path[len(prefix):]
    body = ""
    if _TOKEN.fullmatch(token):
        with _lock:
            body = _challenges.get(token, "")
    raw = body.encode()
    handler.send_response(200 if body else 404)
    handler.send_header("Content-Type", "text/plain")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    if raw:
        handler.wfile.write(raw)
    return True


def server_context() -> ssl.SSLContext | None:
    """Issue or load the Let's Encrypt certificate. None when ACME is off."""
    domain = _domain()
    if not domain:
        return None
    email = (config.ACME_EMAIL or "").strip()
    _check_email(email)
    root = Path(config.DATA_DIR) / "acme"
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    cert = root / "cert.pem"
    key = root / "domain.pem"
    needed = not _fresh(cert)
    try:
        _start_challenge_port()
    except AcmeError:
        if needed:
            raise
        print("certificate challenge port is not available", file=sys.stderr)
    if needed:
        issue_certificate(domain, email, root)
    context = _context(cert, key)
    threading.Thread(
        target=_renew, args=(context, domain, email, root), name="acme-renew", daemon=True
    ).start()
    return context


def issue_certificate(domain: str, email: str, root: Path) -> None:
    """Request a certificate into root. The HTTP-01 listener is already up."""
    domain = _check_domain(domain)
    _check_email(email)
    root.mkdir(parents=True, exist_ok=True)
    account_key = root / "account.pem"
    domain_key = root / "domain.pem"
    if not account_key.is_file():
        _openssl(["genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", str(account_key)])
        account_key.chmod(0o600)
    if not domain_key.is_file():
        _openssl(["genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", str(domain_key)])
        domain_key.chmod(0o600)
    directory = _json(_http("GET", _directory_url(), None, {}))
    nonce_url = directory["newNonce"]
    nonce = [_nonce(nonce_url)]
    account = _signed(
        directory["newAccount"],
        {"termsOfServiceAgreed": True, "contact": [f"mailto:{email}"]},
        account_key,
        nonce,
        nonce_url=nonce_url,
    )
    kid = account["location"]
    if not kid:
        raise AcmeError("the certificate authority did not return an account")
    (root / "account.url").write_text(kid)
    (root / "account.url").chmod(0o600)
    order = _signed(
        directory["newOrder"],
        {"identifiers": [{"type": "dns", "value": domain}]},
        account_key,
        nonce,
        kid,
        nonce_url=nonce_url,
    )
    order_url = order["location"]
    body = order["body"]
    thumb = _thumbprint(account_key)
    try:
        for auth_url in body["authorizations"]:
            auth = _signed(auth_url, None, account_key, nonce, kid, nonce_url=nonce_url)["body"]
            challenge = next((item for item in auth["challenges"] if item["type"] == "http-01"), None)
            if challenge is None:
                raise AcmeError("the certificate authority did not offer an HTTP challenge")
            token = challenge["token"]
            if not _TOKEN.fullmatch(token):
                raise AcmeError("the certificate authority sent a challenge that could not be published")
            with _lock:
                _challenges[token] = token + "." + _b64(thumb)
            _signed(challenge["url"], {}, account_key, nonce, kid, nonce_url=nonce_url)
            _wait_valid(auth_url, account_key, nonce, kid, nonce_url)
        csr = _csr(domain, domain_key)
        finalized = _signed(body["finalize"], {"csr": _b64(csr)}, account_key, nonce, kid, nonce_url=nonce_url)
        current = finalized["body"]
        current_url = finalized["location"] or order_url
        while current.get("status") != "valid":
            if current.get("status") == "invalid":
                raise AcmeError("the certificate authority rejected the domain")
            time.sleep(1)
            polled = _signed(current_url, None, account_key, nonce, kid, nonce_url=nonce_url)
            current = polled["body"]
            current_url = polled["location"] or current_url
        if "certificate" not in current:
            raise AcmeError("the certificate authority did not return a certificate")
        downloaded = _signed(current["certificate"], None, account_key, nonce, kid, nonce_url=nonce_url, pem=True)
    finally:
        with _lock:
            _challenges.clear()
    pem = downloaded["raw"]
    if b"BEGIN CERTIFICATE" not in pem:
        raise AcmeError("the certificate authority did not return a certificate")
    cert = root / "cert.pem"
    cert.write_bytes(pem if pem.endswith(b"\n") else pem + b"\n")
    cert.chmod(0o600)


def _renew(context: ssl.SSLContext, domain: str, email: str, root: Path) -> None:
    while True:
        time.sleep(12 * 60 * 60)
        try:
            if _fresh(root / "cert.pem"):
                continue
            issue_certificate(domain, email, root)
            context.load_cert_chain(root / "cert.pem", root / "domain.pem")
        except Exception:
            print("certificate renewal failed", file=sys.stderr)


def _wait_valid(url: str, key: Path, nonce: list[str], kid: str, nonce_url: str) -> None:
    for _ in range(30):
        body = _signed(url, None, key, nonce, kid, nonce_url=nonce_url)["body"]
        if body.get("status") == "valid":
            return
        if body.get("status") == "invalid":
            raise AcmeError("the certificate authority rejected the domain")
        time.sleep(1)
    raise AcmeError("the certificate authority did not confirm the domain")


def _signed(
    url, payload, key: Path, nonce: list[str], kid: str = "", nonce_url: str = "", pem: bool = False
) -> dict:
    last = ""
    for _ in range(2):
        if not nonce[0]:
            nonce[0] = _nonce(nonce_url or url)
        header = {"alg": "ES256", "nonce": nonce[0], "url": url}
        if kid:
            header["kid"] = kid
        else:
            header["jwk"] = _jwk(key)
        raw_payload = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        protected = _b64(json.dumps(header, separators=(",", ":")).encode())
        signature = _b64(_sign(key, (protected + "." + _b64(raw_payload)).encode()))
        status, headers, raw = _http(
            "POST",
            url,
            json.dumps(
                {"protected": protected, "payload": _b64(raw_payload), "signature": signature}
            ).encode(),
            {"Content-Type": "application/jose+json"},
        )
        if headers.get("Replay-Nonce"):
            nonce[0] = headers.get("Replay-Nonce")
        if status == 400 and b"badNonce" in raw:
            last = _detail(raw)
            continue
        if status not in (200, 201):
            raise AcmeError(_detail(raw) or "the certificate authority refused the request")
        if pem or b"BEGIN CERTIFICATE" in raw:
            return {"location": headers.get("Location", ""), "body": {}, "raw": raw}
        try:
            body = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError as exc:
            raise AcmeError("the certificate authority sent a response that could not be read") from exc
        return {"location": headers.get("Location", ""), "body": body, "raw": raw}
    raise AcmeError(last or "the certificate authority rejected the nonce")


def _directory_url() -> str:
    chosen = (config.ACME_DIRECTORY or "").strip()
    if chosen:
        return chosen
    if config.ACME_STAGING:
        return STAGING
    return PRODUCTION


def _domain() -> str:
    return (config.ACME_DOMAIN or "").strip().lower().rstrip(".")


def _check_domain(domain: str) -> str:
    domain = (domain or "").strip().lower().rstrip(".")
    if not _DOMAIN.fullmatch(domain):
        raise AcmeError("a domain is required to request a certificate")
    return domain


def _check_email(email: str) -> None:
    if (
        not email
        or len(email) > 120
        or "@" not in email
        or email.startswith("@")
        or email.endswith("@")
        or any(ch.isspace() for ch in email)
    ):
        raise AcmeError("an email is required to request a certificate")


def _fresh(cert: Path) -> bool:
    if not cert.is_file():
        return False
    result = _run(["openssl", "x509", "-checkend", str(_RENEW_WITHIN), "-noout", "-in", str(cert)])
    return result.returncode == 0


def _start_challenge_port() -> None:
    port = _challenge_port()
    if port is None or port == config.PORT:
        return
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), _ChallengeHandler)
    except OSError as exc:
        raise AcmeError(f"ACME challenge port {port} is not available") from exc
    threading.Thread(target=server.serve_forever, name="acme-challenge", daemon=True).start()


def _challenge_port() -> int | None:
    raw = (config.ACME_HTTP_PORT or "").strip()
    if raw in ("", "0"):
        return None
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise AcmeError("ACME_HTTP_PORT is not a port")
    return int(raw)


class _ChallengeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        write_challenge(self)

    def log_message(self, fmt, *args):
        return


def _context(cert: Path, key: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(cert, key)
    except (OSError, ssl.SSLError) as exc:
        raise AcmeError("the certificate could not be loaded") from exc
    return context


def _jwk(key: Path) -> dict:
    x, y = _public_xy(key)
    return {"crv": "P-256", "kty": "EC", "x": _b64(x), "y": _b64(y)}


def _thumbprint(key: Path) -> bytes:
    jwk = _jwk(key)
    encoded = json.dumps(
        {"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"], "y": jwk["y"]},
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).digest()


def _public_xy(key: Path) -> tuple[bytes, bytes]:
    der = _openssl(["pkey", "-in", str(key), "-pubout", "-outform", "DER"])
    if len(der) < 65 or der[-65] != 0x04:
        raise AcmeError("the account key could not be read")
    point = der[-64:]
    return point[:32], point[32:]


def _sign(key: Path, data: bytes) -> bytes:
    return _der_to_raw(_openssl(["dgst", "-sha256", "-sign", str(key)], data))


def _csr(domain: str, key: Path) -> bytes:
    return _openssl(
        [
            "req", "-new", "-key", str(key), "-subj", f"/CN={domain}",
            "-addext", f"subjectAltName=DNS:{domain}", "-outform", "DER",
        ]
    )


def _der_to_raw(der: bytes) -> bytes:
    if not der or der[0] != 0x30:
        raise AcmeError("the certificate request could not be signed")
    length, index = _der_len(der, 1)
    end = index + length
    r, index = _der_int(der, index)
    s, index = _der_int(der, index)
    if index != end:
        raise AcmeError("the certificate request could not be signed")
    return _fixed(r) + _fixed(s)


def _der_len(buf: bytes, index: int) -> tuple[int, int]:
    size = buf[index]
    index += 1
    if size < 0x80:
        return size, index
    count = size & 0x7F
    return int.from_bytes(buf[index:index + count], "big"), index + count


def _der_int(buf: bytes, index: int) -> tuple[bytes, int]:
    if buf[index] != 0x02:
        raise AcmeError("the certificate request could not be signed")
    length, index = _der_len(buf, index + 1)
    return buf[index:index + length], index + length


def _fixed(value: bytes) -> bytes:
    value = value.lstrip(b"\x00")
    if len(value) > 32:
        raise AcmeError("the certificate request could not be signed")
    return value.rjust(32, b"\x00")


def _nonce(url: str) -> str:
    status, headers, _raw = _http("HEAD", url, None, {})
    nonce = headers.get("Replay-Nonce", "")
    if status not in (200, 204) or not nonce:
        raise AcmeError("the certificate authority did not offer a nonce")
    return nonce


def _json(response: tuple) -> dict:
    status, _headers, raw = response
    if status != 200:
        raise AcmeError("the certificate directory could not be read")
    try:
        body = json.loads(raw.decode())
    except json.JSONDecodeError as exc:
        raise AcmeError("the certificate directory could not be read") from exc
    for name in ("newNonce", "newAccount", "newOrder"):
        if name not in body:
            raise AcmeError("the certificate directory could not be read")
    return body


def _detail(raw: bytes) -> str:
    try:
        detail = json.loads(raw.decode()).get("detail", "")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(detail, str) or any(ord(ch) < 32 for ch in detail):
        return ""
    return detail[:200]


def _http(method: str, url: str, data: bytes | None, headers: dict) -> tuple[int, object, bytes]:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise AcmeError("the certificate directory could not be read")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    connection = HTTPSConnection if parts.scheme == "https" else HTTPConnection
    try:
        conn = connection(parts.hostname, port, timeout=30)
        conn.request(method, path, body=data, headers={"User-Agent": "SIVED", **headers})
        response = conn.getresponse()
        raw = response.read()
        status = response.status
        response_headers = response.headers
        conn.close()
    except OSError as exc:
        raise AcmeError("the certificate authority could not be reached") from exc
    return status, response_headers, raw


def _openssl(args: list[str], data: bytes | None = None) -> bytes:
    result = _run(["openssl", *args], data)
    if result.returncode != 0:
        raise AcmeError("openssl could not complete the certificate request")
    return result.stdout


def _run(args: list[str], data: bytes | None = None):
    try:
        return subprocess.run(args, input=data, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise AcmeError("openssl is required to request a certificate") from exc


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()
