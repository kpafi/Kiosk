"""Schutz vor SSRF: Nur öffentliche http(s)-Ziele auf den Standard-Ports dürfen abgerufen werden."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from urllib.parse import urlparse

import httpcore

BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
METADATA_IP = ipaddress.ip_address("169.254.169.254")
# Nur die Standard-Ports: Feeds und Artikel liegen dort; andere Ports öffentlicher Hosts (Admin-
# Oberflächen, Datenbanken) sind kein Ziel für einen Leser.
DEFAULT_PORTS = {"http": 80, "https": 443}
# GuardedBackend: Mindestzeit für den Verbindungsaufbau zu einer einzelnen Adresse eines Namens mit
# mehreren Adressen (die Frist des Clients wird auf die Adressen verteilt).
CONNECT_MIN_SLICE = 2.0

# Zusätzlich gesperrte Netze (KIOSK_FETCH_DENY_IPS, siehe config.fetch_deny_networks), gesetzt vom
# Fetcher; gelten für die Namensprüfung und die tatsächliche Gegenstelle.
_deny_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()


def set_deny_networks(networks) -> None:
    global _deny_networks
    _deny_networks = tuple(networks)


class BlockedURL(Exception):
    """Ziel ist nicht erlaubt (Schema, Hostname oder private Adresse)."""


class ResolveError(Exception):
    """Name ließ sich (vorerst) nicht auflösen. Gilt als vorübergehend, nicht als Sperre."""


def ip_is_blocked(ip: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True für alles, was nicht eindeutig eine öffentliche Unicast-Adresse ist, und für Adressen
    aus KIOSK_FETCH_DENY_IPS."""
    addr = ipaddress.ip_address(ip) if isinstance(ip, str) else ip
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if any(addr in net for net in _deny_networks):
        return True
    return (
        addr == METADATA_IP
        or addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or not addr.is_global
    )


async def check_url(url: str) -> None:
    """Prüft Schema und Hostname und löst den Namen auf. Wirft BlockedURL bei unzulässigem Ziel."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise BlockedURL(f"Schema nicht erlaubt: {parsed.scheme or '(leer)'}")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        raise BlockedURL("Kein Hostname")
    port = parsed.port  # ValueError bei ungültigem Port (Aufrufer: ungültige URL)
    if port is not None and port != DEFAULT_PORTS[parsed.scheme]:
        raise BlockedURL(f"Port nicht erlaubt: {port} (nur {DEFAULT_PORTS[parsed.scheme]} für {parsed.scheme})")
    if host in BLOCKED_HOSTNAMES or host.endswith(".localhost"):
        raise BlockedURL(f"Host nicht erlaubt: {host}")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if ip_is_blocked(literal):
            raise BlockedURL(f"Adresse nicht erlaubt: {host}")
        return
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, DEFAULT_PORTS[parsed.scheme], type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ResolveError(f"Name nicht auflösbar: {host}") from e
    if not infos:
        raise ResolveError(f"Name nicht auflösbar: {host}")
    for info in infos:
        ip = info[4][0].split("%", 1)[0]  # IPv6-Zonen-Index abschneiden
        if ip_is_blocked(ip):
            raise BlockedURL(f"{host} löst auf nicht erlaubte Adresse auf: {ip}")


def server_addr_blocked(server_addr: object) -> bool:
    """Prüft die tatsächliche Gegenstelle einer Verbindung (Tupel aus getpeername()).

    None heißt "unbekannt" und wird nicht geprüft. Alles andere, was sich nicht als
    (IP, Port, ...) lesen lässt, gilt vorsichtshalber als blockiert.
    """
    if server_addr is None:
        return False
    if not isinstance(server_addr, (tuple, list)) or not server_addr or not isinstance(server_addr[0], str):
        return True
    try:
        return ip_is_blocked(server_addr[0].split("%", 1)[0])
    except ValueError:
        return True


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """Netzwerk-Backend für httpcore gegen DNS-Rebinding: löst den Namen beim Verbindungsaufbau selbst
    auf, verwirft die Verbindung, wenn eine der Adressen nicht erlaubt ist (wie check_url), und verbindet
    sich mit genau der geprüften Adresse. Ohne das prüfte check_url eine Auflösung und httpx verband sich
    nach einer zweiten, die ein feindlicher DNS-Server anders beantworten kann (die Anfrage war dann
    schon gesendet, bevor die Prüfung der Gegenstelle griff). Hostname für SNI, Zertifikatsprüfung und
    Host-Kopfzeile bleiben unverändert (httpcore übergibt sie getrennt)."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None):
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                          local_address: str | None = None, socket_options=None):
        """Versucht die geprüften Adressen der Reihe nach (z. B. IPv4 nach IPv6). Die Frist `timeout`
        gilt für alle zusammen: Jede Adresse bekommt den Rest geteilt durch die Zahl der noch offenen,
        mindestens CONNECT_MIN_SLICE Sekunden (nie mehr als den Rest). Sonst kostete eine tote
        IPv6-Adresse die ganze Frist, bevor IPv4 dran war."""
        ips = await _allowed_addresses(host, port)
        deadline = None if timeout is None else time.monotonic() + timeout
        last: Exception | None = None
        for i, ip in enumerate(ips):
            slice_ = None
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0 and last is not None:
                    break
                slice_ = min(max(CONNECT_MIN_SLICE, left / (len(ips) - i)), max(left, 0.0))
            try:
                return await self._inner.connect_tcp(ip, port, timeout=slice_, local_address=local_address,
                                                     socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                last = e      # nächste geprüfte Adresse
        assert last is not None
        raise last

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options=None):
        raise BlockedURL("Unix-Sockets sind nicht erlaubt")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


async def _allowed_addresses(host: str, port: int) -> list[str]:
    """Alle Adressen des Namens, wenn keine davon gesperrt ist (sonst BlockedURL), in der Reihenfolge
    von getaddrinfo, ohne Doppelte."""
    host = host.rstrip(".").lower()
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        if ip_is_blocked(literal):
            raise BlockedURL(f"Verbindung zu nicht erlaubter Adresse: {literal}")
        return [str(literal)]
    if host in BLOCKED_HOSTNAMES or host.endswith(".localhost"):
        raise BlockedURL(f"Host nicht erlaubt: {host}")
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ResolveError(f"Name nicht auflösbar: {host}") from e
    ips = list(dict.fromkeys(info[4][0].split("%", 1)[0] for info in infos))
    if not ips:
        raise ResolveError(f"Name nicht auflösbar: {host}")
    for ip in ips:
        if ip_is_blocked(ip):
            raise BlockedURL(f"Verbindung zu nicht erlaubter Adresse: {host} -> {ip}")
    return ips
