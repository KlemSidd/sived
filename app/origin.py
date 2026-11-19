"""Scheme and host used in URLs a browser will open.

The desk serves plain HTTP, terminates TLS itself, or sits behind a proxy that
terminates TLS. A proxy on this machine is trusted. A remote load balancer is
trusted when TRUST_PROXY is set. The proxy replaces Forwarded and
X-Forwarded-* ; it does not append a value the client sent. A direct TLS
connection stays https. A forwarded header cannot turn it back into http.
"""

from __future__ import annotations

import re

_HOST = re.compile(r"^(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:]+\])(?::[0-9]{1,5})?$")


def resolve_public_origin(
    *,
    host: str,
    tls: bool,
    peer: str,
    trust_proxy: bool,
    forwarded: str = "",
    forwarded_proto: str = "",
    forwarded_host: str = "",
    forwarded_port: str = "",
) -> tuple[str, str]:
    """Return the scheme and host for this request. Host may include a port."""
    scheme = "https" if tls else "http"
    chosen = _safe_host(host)
    if not _trusted(peer, trust_proxy):
        return scheme, chosen
    proto, forwarded_name = _forwarded_first(forwarded)
    if proto not in ("http", "https"):
        proto = _first(forwarded_proto).lower()
    if not tls and proto in ("http", "https"):
        scheme = proto
    name = _safe_host(forwarded_name) or _safe_host(_first(forwarded_host))
    if name:
        chosen = name
    return scheme, _with_port(chosen, scheme, _first(forwarded_port))


def _trusted(peer: str, trust_proxy: bool) -> bool:
    if trust_proxy:
        return True
    peer = peer or ""
    if peer.startswith("::ffff:"):
        peer = peer[7:]
    return peer in ("127.0.0.1", "::1")


def _first(value: str) -> str:
    return (value or "").split(",")[0].strip()


def _forwarded_first(value: str) -> tuple[str, str]:
    """proto and host from the first RFC 7239 Forwarded element."""
    element = _first(value)
    proto = ""
    host = ""
    for part in element.split(";"):
        if "=" not in part:
            continue
        key, raw = part.split("=", 1)
        raw = raw.strip().strip('"')
        if key.strip().lower() == "proto" and raw.lower() in ("http", "https"):
            proto = raw.lower()
        elif key.strip().lower() == "host":
            host = raw
    return proto, host


def _safe_host(value: str) -> str:
    value = (value or "").strip()
    if not _HOST.fullmatch(value):
        return ""
    if ":" in value and not value.startswith("["):
        _name, port = value.rsplit(":", 1)
        if not _port_ok(port):
            return ""
    elif value.startswith("[") and "]:" in value:
        _name, port = value.rsplit(":", 1)
        if not _port_ok(port):
            return ""
    return value


def _with_port(host: str, scheme: str, port: str) -> str:
    if not host or not _port_ok(port):
        return host
    if host.startswith("["):
        if "]:" in host:
            return host
        if (scheme == "https" and port == "443") or (scheme == "http" and port == "80"):
            return host
        return f"{host}:{port}"
    if ":" in host:
        return host
    if (scheme == "https" and port == "443") or (scheme == "http" and port == "80"):
        return host
    return f"{host}:{port}"


def _port_ok(port: str) -> bool:
    return port.isdigit() and 1 <= int(port) <= 65535
