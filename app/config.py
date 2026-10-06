from __future__ import annotations

import ipaddress
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
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
# KI-Ausgabe (app/editions.py): Modell und Endpunkt bei OpenRouter, Uhrzeiten der beiden Ausgaben.
# Das Modell ist nur ein Platzhalter, der Betreiber legt es per KIOSK_AI_MODEL fest.
DEFAULT_AI_MODEL = "anthropic/claude-sonnet-5.5"
DEFAULT_AI_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_AI_MORNING = "06:00"
DEFAULT_AI_EVENING = "20:00"
# Denkbudget (OpenRouter `reasoning.effort`): none = nichts senden (Standard des Modells).
AI_REASONING_LEVELS = ("none", "low", "medium", "high")
_HHMM_RE = re.compile(r"([01]?[0-9]|2[0-3]):([0-5][0-9])")
# Versand der KI-Ausgabe per Mail (app/mail.py): SMTP mit TLS, Standardport 465 (SMTPS); 587 = STARTTLS.
DEFAULT_MAIL_PORT = 465
MAIL_TO_MAX = 50
# Mailadresse: ein lokaler Teil ohne Leerzeichen, Klammern oder Anführungszeichen und ein Hostname mit Punkt.
_MAIL_ADDR_RE = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
                           r"[A-Za-z]{2,63}")


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
    # KI-Ausgabe: OPENROUTER_API_KEY ("" = aus; nie in repr, Log oder Status), KIOSK_AI_MODEL,
    # KIOSK_AI_BASE_URL (ohne / am Ende), KIOSK_AI_MORNING/KIOSK_AI_EVENING ("HH:MM", Ortszeit KIOSK_TZ),
    # KIOSK_AI_REASONING (AI_REASONING_LEVELS).
    ai_api_key: str = field(default="", repr=False)
    ai_model: str = DEFAULT_AI_MODEL
    ai_base_url: str = DEFAULT_AI_BASE_URL
    ai_morning: str = DEFAULT_AI_MORNING
    ai_evening: str = DEFAULT_AI_EVENING
    ai_reasoning: str = "none"
    # Versand der KI-Ausgabe per Mail: KIOSK_MAIL_HOST ("" = aus), KIOSK_MAIL_PORT (465 TLS, 587 STARTTLS),
    # KIOSK_MAIL_USER/KIOSK_MAIL_PASSWORD (Anmeldung, Passwort nie in repr oder Log), KIOSK_MAIL_FROM
    # (Absender, Standard der Benutzer), KIOSK_MAIL_TO (Empfänger, kommagetrennt), KIOSK_PUBLIC_URL (Adresse
    # der App für Links in der Mail, "" = Links als Text).
    mail_host: str = ""
    mail_port: int = DEFAULT_MAIL_PORT
    mail_user: str = ""
    mail_password: str = field(default="", repr=False)
    mail_from: str = ""
    mail_to: tuple[str, ...] = ()
    public_url: str = ""

    @property
    def mail_enabled(self) -> bool:
        return bool(self.mail_host and self.mail_to)


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


def _hhmm(name: str, default: str) -> str:
    """Uhrzeit "HH:MM" (auch "6:00") aus der Umgebung, normalisiert auf zwei Stellen; sonst RuntimeError."""
    raw = os.environ.get(name, "").strip() or default
    m = _HHMM_RE.fullmatch(raw)
    if not m:
        raise RuntimeError(f"{name} muss eine Uhrzeit HH:MM sein (z. B. 06:00), ist {raw!r}")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


# Hosts, für die KIOSK_AI_BASE_URL auch http sein darf (lokaler Modell-Server).
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def _ai_settings() -> dict:
    """Einstellungen der KI-Ausgabe aus der Umgebung, geprüft (RuntimeError bei Unsinn). Fehlermeldungen
    nennen nie den Schlüssel selbst."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key and (any(c.isspace() for c in key) or not key.isprintable()):
        raise RuntimeError("OPENROUTER_API_KEY enthält Leerzeichen oder Steuerzeichen")
    if not key.isascii():
        # z. B. ein typografischer Bindestrich aus Copy-Paste: scheiterte sonst erst beim Aufruf im Header
        raise RuntimeError("OPENROUTER_API_KEY enthält Zeichen außerhalb von ASCII (kopiert mit falschem Bindestrich?)")
    model = os.environ.get("KIOSK_AI_MODEL", "").strip() or DEFAULT_AI_MODEL
    if any(c.isspace() for c in model) or not model.isprintable() or len(model) > 200:
        raise RuntimeError(f"KIOSK_AI_MODEL ist kein gültiger Modellname: {model!r}")
    base = (os.environ.get("KIOSK_AI_BASE_URL", "").strip() or DEFAULT_AI_BASE_URL).rstrip("/")
    m = re.fullmatch(r"(https?)://([^\s/?#]+)(/[^\s?#]*)?", base)
    if not m:
        raise RuntimeError(f"KIOSK_AI_BASE_URL muss eine https-Adresse sein, ist {base!r}")
    try:
        host = urlparse(base).hostname
    except ValueError:      # z. B. „[::1“ ohne schließende Klammer
        raise RuntimeError(f"KIOSK_AI_BASE_URL ist keine gültige Adresse: {base!r}") from None
    if m.group(1) == "http" and host not in _LOCAL_HOSTS:
        # Der Schlüssel ginge sonst im Klartext übers Netz; http nur für einen Server auf diesem Rechner.
        raise RuntimeError(f"KIOSK_AI_BASE_URL muss https sein (http nur für localhost), ist {base!r}")
    morning = _hhmm("KIOSK_AI_MORNING", DEFAULT_AI_MORNING)
    evening = _hhmm("KIOSK_AI_EVENING", DEFAULT_AI_EVENING)
    if morning >= evening:
        raise RuntimeError(f"KIOSK_AI_MORNING ({morning}) muss vor KIOSK_AI_EVENING ({evening}) liegen")
    reasoning = os.environ.get("KIOSK_AI_REASONING", "").strip().lower() or "none"
    if reasoning not in AI_REASONING_LEVELS:
        raise RuntimeError(f"KIOSK_AI_REASONING muss {', '.join(AI_REASONING_LEVELS)} sein, ist {reasoning!r}")
    return {"ai_api_key": key, "ai_model": model, "ai_base_url": base, "ai_morning": morning,
            "ai_evening": evening, "ai_reasoning": reasoning}


def mail_addresses(raw: str, name: str = "KIOSK_MAIL_TO") -> tuple[str, ...]:
    """Kommagetrennte Mailadressen (ohne Namen, nur adresse@host), doppelte entfernt (Groß/Klein egal);
    ungültig oder mehr als MAIL_TO_MAX: RuntimeError."""
    out: list[str] = []
    for part in raw.replace(";", ",").split(","):
        addr = part.strip()
        if not addr:
            continue
        if not _MAIL_ADDR_RE.fullmatch(addr):
            raise RuntimeError(f"{name}: keine gültige Mailadresse: {addr!r}")
        if addr.lower() not in {a.lower() for a in out}:
            out.append(addr)
    if len(out) > MAIL_TO_MAX:
        raise RuntimeError(f"{name}: höchstens {MAIL_TO_MAX} Empfänger")
    return tuple(out)


def _mail_settings() -> dict:
    """Einstellungen des Mailversands aus der Umgebung, geprüft (RuntimeError bei Unsinn). Ohne
    KIOSK_MAIL_HOST ist der Versand aus; die anderen Werte werden dann nicht verlangt. Fehlermeldungen nennen nie
    das Passwort."""
    host = os.environ.get("KIOSK_MAIL_HOST", "").strip()
    password = os.environ.get("KIOSK_MAIL_PASSWORD", "")
    public = os.environ.get("KIOSK_PUBLIC_URL", "").strip().rstrip("/")
    if public and not re.fullmatch(r"https://[^\s/?#]+(/[^\s?#]*)?", public):
        raise RuntimeError(f"KIOSK_PUBLIC_URL muss eine https-Adresse sein, ist {public!r}")
    if not host:
        return {"mail_host": "", "mail_port": DEFAULT_MAIL_PORT, "mail_user": "", "mail_password": "",
                "mail_from": "", "mail_to": (), "public_url": public}
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host):
        raise RuntimeError(f"KIOSK_MAIL_HOST ist kein Hostname: {host!r}")
    port = _int("KIOSK_MAIL_PORT", DEFAULT_MAIL_PORT, lo=1, hi=65535)
    user = os.environ.get("KIOSK_MAIL_USER", "").strip()
    if not user or not password:
        raise RuntimeError("KIOSK_MAIL_HOST gesetzt, aber KIOSK_MAIL_USER oder KIOSK_MAIL_PASSWORD fehlt")
    if any(c.isspace() for c in user) or not user.isprintable() or not user.isascii() or len(user) > 254:
        raise RuntimeError(f"KIOSK_MAIL_USER enthält Leerzeichen, Steuerzeichen oder Nicht-ASCII: {user!r}")
    if any(c in "\r\n\0" for c in password) or not password.isascii():
        raise RuntimeError("KIOSK_MAIL_PASSWORD enthält Zeilenumbrüche oder Zeichen außerhalb von ASCII")
    sender = os.environ.get("KIOSK_MAIL_FROM", "").strip() or user
    if not _MAIL_ADDR_RE.fullmatch(sender):
        raise RuntimeError(f"KIOSK_MAIL_FROM (bzw. KIOSK_MAIL_USER als Absender) ist keine Mailadresse: {sender!r}")
    to = mail_addresses(os.environ.get("KIOSK_MAIL_TO", ""))
    return {"mail_host": host, "mail_port": port, "mail_user": user, "mail_password": password,
            "mail_from": sender, "mail_to": to, "public_url": public}


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
        **_ai_settings(),
        **_mail_settings(),
    )
