"""Konten mit Passwort (scrypt), signiertes Cookie pro Benutzer, Login-Bremse."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import re
import time
from collections import OrderedDict
from typing import Any, Callable

from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from .config import USERNAME_RE, Settings

# Mit Secure-Flag gilt das __Host-Präfix: Cookie nur über HTTPS, Path=/, ohne Domain.
SECURE_COOKIE_NAME = "__Host-kiosk_session"
PLAIN_COOKIE_NAME = "kiosk_session"
COOKIE_NAMES = (SECURE_COOKIE_NAME, PLAIN_COOKIE_NAME)

# Login-Bremse: je IP höchstens MAX_ATTEMPTS, je Benutzername MAX_USER_ATTEMPTS Versuche in
# WINDOW_SECONDS, dazu eine Notbremse über alle Anfragen (GLOBAL_MAX_ATTEMPTS pro
# GLOBAL_WINDOW_SECONDS). Die Grenze je Benutzername ist höher als die je IP: Ein Angreifer mit
# vielen IPs kann einen Namen so erst nach 30 Fehlversuchen für 15 Minuten sperren.
MAX_ATTEMPTS = 8
MAX_USER_ATTEMPTS = 30
WINDOW_SECONDS = 15 * 60
MAX_TRACKED_KEYS = 1000
GLOBAL_MAX_ATTEMPTS = 200
GLOBAL_WINDOW_SECONDS = 60

_PAYLOAD_RE = re.compile(r"u:([0-9]{1,18}):([0-9]{1,18}):([0-9a-f]{16})")

LOGIN_ERROR = "Benutzername oder Passwort falsch"
# Gemeinsamer Zähler der Login-Bremse für alle Namen, die nicht den Regeln entsprechen (enthält "?",
# kann also kein gültiger Name sein): Solche Namen können nie ein Konto treffen und sollen die
# Zähler echter Namen nicht aus der begrenzten Liste verdrängen.
INVALID_NAME_KEY = "?ungueltig"

# scrypt-Parameter für neue Hashes (ca. 16 MiB Speicher, einige 10 ms pro Hash).
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


def normalize_username(raw: str) -> str | None:
    """Kleingeschriebener Benutzername oder None, wenn er nicht den Regeln entspricht."""
    name = raw.strip().lower()
    return name if USERNAME_RE.fullmatch(name) else None


def ip_key(ip: str) -> str:
    """Schlüssel der Login-Bremse je Adresse: IPv4 einzeln, IPv6 je /64-Netz (ein Anschluss hat
    meist ein ganzes /64 und könnte sonst mit jedem Versuch eine neue Adresse nehmen)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str) -> str:
    """scrypt-Hash im Format "scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>" (16 Byte Salt)."""
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                            dklen=SCRYPT_DKLEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Prüft ein Passwort gegen einen gespeicherten Hash; kaputte Hashes gelten als falsch."""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        n, r, p = int(n), int(r), int(p)
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(hash_b64)
    except ValueError:
        return False
    # Grenzen gegen absurde Parameter (Speicher, Laufzeit)
    if scheme != "scrypt" or not (2 <= n <= 2**20 and n & (n - 1) == 0) or not (1 <= r <= 32) \
            or not (1 <= p <= 16) or not expected:
        return False
    try:
        digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=len(expected),
                                maxmem=256 * 1024 * 1024)
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(digest, expected)


# Hash für unbekannte Benutzer: Die Prüfung dauert gleich lang wie bei einem echten Konto.
_DUMMY_HASH = hash_password("kiosk-dummy-passwort")


def burn_time(password: str) -> None:
    verify_password(password, _DUMMY_HASH)


class _Limiter:
    """Zählt Fehlversuche je Schlüssel in einem gleitenden Zeitfenster, begrenzt auf max_keys Schlüssel."""

    def __init__(self, limit: int, window: float, max_keys: int):
        self.limit, self.window, self.max_keys = limit, window, max_keys
        # Schlüssel -> Zeitpunkte der Fehlversuche; Reihenfolge = zuletzt aktualisiert zuletzt
        self.attempts: OrderedDict[str, list[float]] = OrderedDict()

    def _prune(self, now: float) -> None:
        for key in [k for k, ts in self.attempts.items() if not ts or now - ts[-1] >= self.window]:
            del self.attempts[key]

    def limited(self, key: str) -> bool:
        now = time.monotonic()
        self._prune(now)
        return len([t for t in self.attempts.get(key, []) if now - t < self.window]) >= self.limit

    def note(self, key: str) -> None:
        now = time.monotonic()
        recent = [t for t in self.attempts.pop(key, []) if now - t < self.window]
        recent.append(now)
        self.attempts[key] = recent[-self.limit:]
        while len(self.attempts) > self.max_keys:
            self.attempts.popitem(last=False)  # ältesten Eintrag verwerfen

    def clear(self, key: str) -> None:
        self.attempts.pop(key, None)

    def release(self, key: str) -> None:
        """Den zuletzt gezählten Versuch dieses Schlüssels zurücknehmen (die Reservierung eines
        erfolgreichen Logins); frühere Fehlversuche bleiben stehen."""
        ts = self.attempts.get(key)
        if ts:
            ts.pop()
            if not ts:
                del self.attempts[key]


class Auth:
    """Zustandslose Sessions: signiertes Cookie mit Payload "u:<id>:<epoch>:<fp>".

    `epoch` ist users.session_epoch (Abmelden und Passwortwechsel erhöhen sie für diesen
    Benutzer), `fp` ein mit dem Secret verschlüsselter Fingerabdruck des Passwort-Hashes:
    Ein neues Passwort macht damit alle alten Cookies ungültig, und das Cookie verrät nichts
    über das Passwort.
    """

    def __init__(self, settings: Settings):
        self._secret = settings.secret.encode()
        self._signer = TimestampSigner(settings.secret, salt="kiosk-session-v6")
        self.max_age = settings.session_max_age
        self.cookie_secure = settings.cookie_secure
        self.cookie_name = SECURE_COOKIE_NAME if settings.cookie_secure else PLAIN_COOKIE_NAME
        self._by_ip = _Limiter(MAX_ATTEMPTS, WINDOW_SECONDS, MAX_TRACKED_KEYS)
        self._by_user = _Limiter(MAX_USER_ATTEMPTS, WINDOW_SECONDS, MAX_TRACKED_KEYS)
        self._global = _Limiter(GLOBAL_MAX_ATTEMPTS, GLOBAL_WINDOW_SECONDS, 1)

    def fingerprint(self, password_hash: str) -> str:
        return hmac.new(self._secret, password_hash.encode(), hashlib.sha256).hexdigest()[:16]

    def issue_token(self, user: dict[str, Any]) -> str:
        payload = f"u:{user['id']}:{user['session_epoch']}:{self.fingerprint(user['password_hash'])}"
        return self._signer.sign(payload.encode()).decode()

    def parse_token(self, token: str | None) -> tuple[int, int, str] | None:
        """(user_id, epoch, fingerprint) aus einem gültig signierten, nicht abgelaufenen Token."""
        if not token:
            return None
        try:
            payload = self._signer.unsign(token.encode(), max_age=self.max_age).decode()
        except (BadSignature, SignatureExpired, UnicodeDecodeError):
            return None
        m = _PAYLOAD_RE.fullmatch(payload)
        return (int(m[1]), int(m[2]), m[3]) if m else None

    def user_from_cookies(self, cookies: dict[str, str],
                          load_user: Callable[[int], dict[str, Any] | None]) -> dict[str, Any] | None:
        """Angemeldeter Benutzer oder None. Liest nur den Cookie-Namen des aktuellen Modus: mit
        Secure-Flag ausschließlich __Host-kiosk_session. Gesperrte Benutzer, eine veraltete
        Epoche oder ein geändertes Passwort ergeben None."""
        parsed = self.parse_token(cookies.get(self.cookie_name))
        if parsed is None:
            return None
        user_id, epoch, fp = parsed
        user = load_user(user_id)
        if user is None or user["disabled"] or user["session_epoch"] != epoch:
            return None
        if not hmac.compare_digest(fp, self.fingerprint(user["password_hash"])):
            return None
        return user

    # ---- Login-Bremse --------------------------------------------------

    def rate_limited(self, ip: str, username: str) -> bool:
        return (self._global.limited("*") or self._by_ip.limited(ip)
                or self._by_user.limited(username))

    def note_failure(self, ip: str, username: str) -> None:
        self._global.note("*")
        self._by_ip.note(ip)
        self._by_user.note(username)

    def reserve(self, ip: str, username: str) -> bool:
        """Prüft die Grenzen und zählt den Versuch im selben synchronen Schritt (ohne await
        dazwischen), also vor der Passwortprüfung: Parallele Anfragen können die Bremse so nicht
        überholen. False = gebremst, nichts gezählt. Nach erfolgreichem Login clear() aufrufen."""
        if self.rate_limited(ip, username):
            return False
        self.note_failure(ip, username)
        return True

    def clear(self, ip: str, username: str) -> None:
        """Nach erfolgreichem Login: Die IP bekommt nur die eigene Reservierung zurück (ihre
        übrigen Fehlversuche bleiben, sonst könnte ein eigenes Konto die Bremse für Rateversuche
        gegen andere Namen immer wieder zurücksetzen); die Fehlversuche des Namens verfallen."""
        self._by_ip.release(ip)
        self._by_user.clear(username)
