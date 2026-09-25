"""Ein Passwort, ein signiertes Cookie. Mehr braucht eine Ein-Personen-App nicht."""
from __future__ import annotations

import hashlib
import hmac
import time
from collections import OrderedDict

from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from .config import Settings

# Mit Secure-Flag gilt das __Host-Präfix: Cookie nur über HTTPS, Path=/, ohne Domain.
SECURE_COOKIE_NAME = "__Host-kiosk_session"
PLAIN_COOKIE_NAME = "kiosk_session"
COOKIE_NAMES = (SECURE_COOKIE_NAME, PLAIN_COOKIE_NAME)
MAX_ATTEMPTS = 8
WINDOW_SECONDS = 15 * 60
MAX_TRACKED_IPS = 1000


def password_fingerprint(secret: str, password: str) -> str:
    """Kurzer, mit dem Secret verschlüsselter Fingerabdruck des Passworts (HMAC, kein nackter Hash,
    damit das Cookie kein Offline-Raten des Passworts erlaubt). Ändert sich das Passwort,
    werden alle Sessions ungültig."""
    return hmac.new(secret.encode(), password.encode(), hashlib.sha256).hexdigest()[:16]


class Auth:
    """Zustandslose Sessions: signiertes Cookie mit Passwort-Fingerabdruck und Session-Epoche.

    `epoch` kommt aus der Tabelle meta; Abmelden erhöht sie und macht damit alle Sessions
    ungültig (bei genau einem Nutzer ist das gewollt).
    """

    def __init__(self, settings: Settings, epoch: int = 1):
        self._password = settings.password
        self._fingerprint = password_fingerprint(settings.secret, settings.password)
        self._signer = TimestampSigner(settings.secret, salt="kiosk-session-v3")
        self.epoch = epoch
        self.max_age = settings.session_max_age
        self.cookie_secure = settings.cookie_secure
        self.cookie_name = SECURE_COOKIE_NAME if settings.cookie_secure else PLAIN_COOKIE_NAME
        # IP -> Zeitpunkte der Fehlversuche; Reihenfolge = zuletzt aktualisiert zuletzt
        self._attempts: OrderedDict[str, list[float]] = OrderedDict()

    def check_password(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode(), self._password.encode())

    def _payload(self) -> bytes:
        return f"ok:{self._fingerprint}:{self.epoch}".encode()

    def issue_token(self) -> str:
        return self._signer.sign(self._payload()).decode()

    def verify_token(self, token: str | None) -> bool:
        if not token:
            return False
        try:
            payload = self._signer.unsign(token.encode(), max_age=self.max_age)
        except (BadSignature, SignatureExpired):
            return False
        return hmac.compare_digest(payload, self._payload())

    def token_from_cookies(self, cookies: dict[str, str]) -> str | None:
        """Liest nur den Cookie-Namen des aktuellen Modus: mit Secure-Flag ausschließlich
        __Host-kiosk_session (kann nicht von einer Subdomain oder über http gesetzt werden)."""
        token = cookies.get(self.cookie_name)
        return token if token and self.verify_token(token) else None

    # ---- Login-Bremse --------------------------------------------------

    def _prune(self, now: float) -> None:
        for ip in [ip for ip, ts in self._attempts.items() if not ts or now - ts[-1] >= WINDOW_SECONDS]:
            del self._attempts[ip]

    def rate_limited(self, ip: str) -> bool:
        now = time.monotonic()
        self._prune(now)
        recent = self._attempts.get(ip, [])
        return len(recent) >= MAX_ATTEMPTS

    def note_failure(self, ip: str) -> None:
        now = time.monotonic()
        recent = [t for t in self._attempts.pop(ip, []) if now - t < WINDOW_SECONDS]
        recent.append(now)
        self._attempts[ip] = recent[-MAX_ATTEMPTS:]
        while len(self._attempts) > MAX_TRACKED_IPS:
            self._attempts.popitem(last=False)  # ältesten Eintrag verwerfen

    def clear(self, ip: str) -> None:
        self._attempts.pop(ip, None)
