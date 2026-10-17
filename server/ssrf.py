"""SSRF protection helpers."""

import ipaddress
import re
import socket
from urllib.parse import urlparse

from config import settings


class SSRFError(Exception):
    pass


def validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise SSRFError(f"Only http/https allowed: {url}")
    if not parsed.hostname:
        raise SSRFError(f"Invalid URL: {url}")

    hostname = parsed.hostname
    if hostname.startswith("["):
        raise SSRFError(f"IPv6 literals not allowed: {url}")

    if re.match(r"^\d+\.\d+\.\d+\.\d+$", hostname):
        raise SSRFError(f"IP literals not allowed: {url}")

    try:
        addr = ipaddress.ip_address(hostname)
        addrs = {str(addr)}
    except ValueError:
        try:
            resolved = socket.getaddrinfo(hostname, None)
            addrs = {info[4][0] for info in resolved}
        except socket.gaierror as exc:
            raise SSRFError(f"Could not resolve {hostname}: {exc}")

    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            if not settings.allow_local_upstream:
                raise SSRFError(f"Private/loopback/link-local/reserved IP blocked: {a}")

    if parsed.port is not None and parsed.port not in (80, 443):
        if not settings.allow_local_upstream:
            raise SSRFError(f"Only standard ports allowed: {url}")
