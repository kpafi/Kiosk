from __future__ import annotations

import ipaddress
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

# Passwort aus .env.example: darf nie produktiv laufen.
EXAMPLE_PASSWORD = "bitte-aendern"
MIN_PASSWORD_LEN = 12
# Benutzernamen: 2 bis 32 Zeichen, klein geschrieben gespeichert.
USERNAME_RE = re.compile(r"[a-z0-9._-]{2,32}")
# Zeitzone, in der ein Tag für die Ungelesen-Zähler um 00:00 beginnt (KIOSK_TZ).
DEFAULT_TZ = "Europe/Berlin"
# Mindestlänge des Secrets für die Session-Cookies (KIOSK_SECRET oder secret.key).
MIN_SECRET_LEN = 32


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    sources_file: Path
    static_dir: Path
    password: str               # KIOSK_PASSWORD; nur noch Bootstrap des Admins, "" = nicht gesetzt
    secret: str
    fetch_interval: int
    retention_days: int
    extract_concurrency: int
    user_agent: str
    session_max_age: int
    cookie_secure: bool
    disable_fetcher: bool
    admin_user: str = "admin"   # KIOSK_ADMIN_USER: Name des Admins beim Bootstrap
    tz: str = DEFAULT_TZ        # KIOSK_TZ: Zeitzone für den Tagesbeginn der Zähler
    # KIOSK_FETCH_DENY_IPS: zusätzlich gesperrte Ziel-Netze für Feed- und Artikelabrufe
    fetch_deny_ips: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()


def _int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    """Ganzzahl aus der Umgebung, optional mit Grenzen (einschließlich); sonst RuntimeError."""
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        raise RuntimeError(f"{name} muss eine ganze Zahl sein, ist {raw!r}") from None
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        bounds = f"mindestens {lo}" if hi is None else f"zwischen {lo} und {hi}"
        raise RuntimeError(f"{name} muss {bounds} sein, ist {value}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _tz(name: str, default: str) -> str:
    """Name einer Zeitzone (IANA, z. B. Europe/Berlin) aus der Umgebung; unbekannt: RuntimeError."""
    raw = os.environ.get(name, "").strip() or default
    try:
        ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise RuntimeError(f"{name} ist keine bekannte Zeitzone: {raw!r} (z. B. Europe/Berlin)") from None
    return raw


def fetch_deny_networks(name: str = "KIOSK_FETCH_DENY_IPS") -> tuple:
    """Kommagetrennte IPs oder Netze (CIDR) aus der Umgebung; ungültig: RuntimeError."""
    nets = []
    for part in os.environ.get(name, "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            raise RuntimeError(f"{name}: keine gültige IP-Adresse bzw. kein Netz (CIDR): {part!r}") from None
    return tuple(nets)


def _remove_stale_secret_temps(data_dir: Path) -> None:
    """Entfernt Hilfsdateien .secret.key.<pid>.<zufall> eines abgebrochenen Starts (Absturz zwischen
    link() und unlink()). Sonst bliebe secret.key mit zwei harten Links liegen, und backup.sh
    kopiert sie dann nicht mehr. Nur Dateien von Prozessen, die es nicht mehr gibt, oder mit der
    eigenen PID (im Container bekommt die App nach einem Neustart oft dieselbe)."""
    for tmp in data_dir.glob(".secret.key.*"):
        parts = tmp.name.split(".")   # ["", "secret", "key", "<pid>", "<zufall>"]
        if len(parts) != 5 or not parts[3].isdigit():
            continue
        pid = int(parts[3])
        if pid != os.getpid():
            try:
                os.kill(pid, 0)
                continue  # läuft noch: legt gerade selbst ein Secret an
            except ProcessLookupError:
                pass
            except PermissionError:
                continue  # gehört einem anderen Benutzer und läuft
        tmp.unlink(missing_ok=True)


def _load_secret(data_dir: Path) -> str:
    """KIOSK_SECRET oder der Inhalt von <data_dir>/secret.key; fehlt beides, wird die Datei mit einem
    neuen Secret angelegt. Kürzer als MIN_SECRET_LEN: RuntimeError."""
    secret = os.environ.get("KIOSK_SECRET", "").strip()
    if secret:
        if len(secret) < MIN_SECRET_LEN:
            raise RuntimeError(f"KIOSK_SECRET muss mindestens {MIN_SECRET_LEN} Zeichen lang sein")
        return secret
    secret_file = data_dir / "secret.key"
    _remove_stale_secret_temps(data_dir)
    if not secret_file.exists():
        new = secrets.token_urlsafe(48)
        # Erst vollständig in eine eigene Datei schreiben, dann per link() unter den endgültigen
        # Namen: Eine leere oder halbe secret.key (Absturz, voller Datenträger) kann so nie
        # entstehen, und eine vorhandene Datei wird nie überschrieben (link scheitert dann).
        # O_EXCL + Modus beim Anlegen: nie, auch nicht kurz, für andere lesbar (die umask kann
        # Rechte nur wegnehmen).
        tmp = data_dir / f".secret.key.{os.getpid()}.{secrets.token_hex(4)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(new)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp, secret_file)
            except FileExistsError:
                pass  # gleichzeitig von einem anderen Prozess angelegt: dessen Secret gilt
        finally:
            tmp.unlink(missing_ok=True)
    secret = secret_file.read_text().strip()
    if len(secret) < MIN_SECRET_LEN:
        raise RuntimeError(
            f"{secret_file} ist leer oder kürzer als {MIN_SECRET_LEN} Zeichen. Datei löschen (beim "
            "nächsten Start entsteht ein neues Secret, alle Sitzungen enden) oder KIOSK_SECRET setzen")
    return secret


def docs_enabled() -> bool:
    """API-Doku (/docs, /openapi.json) nur mit KIOSK_DOCS=1; in Produktion standardmäßig aus."""
    return _bool("KIOSK_DOCS", False)


def admin_username() -> str:
    """KIOSK_ADMIN_USER (Standard "admin"), klein geschrieben; ungültig: RuntimeError."""
    raw = os.environ.get("KIOSK_ADMIN_USER", "").strip().lower() or "admin"
    if not USERNAME_RE.fullmatch(raw):
        raise RuntimeError("KIOSK_ADMIN_USER: 2 bis 32 Zeichen aus a-z, 0-9, Punkt, Unterstrich, Bindestrich")
    return raw


def bootstrap_password() -> str:
    """KIOSK_PASSWORD (ohne Leerzeichen am Rand), "" = nicht gesetzt; ungültig: RuntimeError.

    Nur noch das Startpasswort des Admins (Umbau auf Schema 6 oder frische Datenbank). Ob es
    gebraucht wird, entscheidet die Datenbank; gesetzt muss es gültig sein. Dieselben Regeln für
    die App (load_settings) und die Kommandozeile (app.users)."""
    password = os.environ.get("KIOSK_PASSWORD", "").strip()
    if password == EXAMPLE_PASSWORD:
        raise RuntimeError("KIOSK_PASSWORD ist noch das Beispiel aus .env.example, bitte ändern")
    if password and len(password) < MIN_PASSWORD_LEN:
        raise RuntimeError(f"KIOSK_PASSWORD muss mindestens {MIN_PASSWORD_LEN} Zeichen lang sein")
    return password


def load_settings() -> Settings:
    root = Path(__file__).resolve().parent.parent
    data_dir = Path(os.environ.get("KIOSK_DATA_DIR", root / "data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    secret = _load_secret(data_dir)
    password = bootstrap_password()
    admin_user = admin_username()

    return Settings(
        data_dir=data_dir,
        db_path=data_dir / "kiosk.db",
        sources_file=Path(os.environ.get("KIOSK_SOURCES", root / "sources.yaml")),
        static_dir=root / "static",
        password=password,
        secret=secret,
        fetch_interval=_int("KIOSK_FETCH_INTERVAL", 900, lo=60),
        retention_days=_int("KIOSK_RETENTION_DAYS", 90, lo=1, hi=3650),
        extract_concurrency=_int("KIOSK_EXTRACT_CONCURRENCY", 6, lo=1, hi=32),
        user_agent=os.environ.get("KIOSK_USER_AGENT", DEFAULT_UA),
        session_max_age=_int("KIOSK_SESSION_DAYS", 30, lo=1, hi=365) * 86400,
        # Nur für lokale Entwicklung über http auf false setzen.
        cookie_secure=_bool("KIOSK_COOKIE_SECURE", True),
        # Für Tests: kein Sammler, keine Netzwerkzugriffe.
        disable_fetcher=_bool("KIOSK_DISABLE_FETCHER", False),
        admin_user=admin_user,
        tz=_tz("KIOSK_TZ", DEFAULT_TZ),
        fetch_deny_ips=fetch_deny_networks(),
    )
