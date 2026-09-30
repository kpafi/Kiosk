from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import os
import re
import sqlite3
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import (COOKIE_NAMES, DEVICE_MAX_AGE, INVALID_NAME_KEY, LOGIN_ERROR, Auth, burn_time, hash_password,
                   ip_key, needs_rehash, normalize_username, verify_password, verify_password_evenly)
from .config import docs_enabled, load_settings
from .db import Database, QueryTimeout, _iso, utcnow
from .extract import strip_reading_times
from .fetcher import LOG_FORMAT, Fetcher
from .sources import Catalog
from .state import State, state  # noqa: F401  (State für Importe von außen)
from . import api_ui, stories
from . import db as db_mod
from .api_ui import (admin_user, article_scope, current_user, effective_sources, implicit_sources, in_all_ids,
                     invalidate_unread, public_user)
from .users import seed_builtin_profiles

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("kiosk")

OPEN_PATHS = {"/healthz", "/api/login", "/api/session", "/api/build", "/", "/index.html", "/favicon.ico"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
MAX_SOURCE_IDS = 200        # höchstens so viele Quellen-IDs pro Anfrage (Query oder Body)
STALL_GRACE = timedelta(minutes=10)
# Quellen, deren gespeicherte Texte get_article beim Ausliefern von Lesezeit-Listen bereinigt (nur
# SPIEGEL war betroffen), und die Länge, bis zu der es das tut (längere gehen unbereinigt raus).
STORED_CLEANUP_SOURCES = frozenset({"spiegel"})
STORED_CLEANUP_MAX = 100_000
RUN_MAX = timedelta(hours=3)            # so lange darf ein einzelner Sammellauf höchstens dauern
WATCHDOG_INTERVAL = 60                  # Sekunden zwischen zwei Prüfungen des Wachhunds
WATCHDOG_EXIT_CODE = 3
VERIFY_SLOTS = 2                        # gleichzeitige scrypt-Prüfungen beim Login (je ca. 16 MiB)
# Mindestdauer einer Anmeldung (204/401), etwa das Dreifache einer Prüfung mit den aktuellen
# scrypt-Parametern (p=5: ca. 150-200 ms), siehe login.
LOGIN_MIN_SECONDS = 0.5
MAX_ID = 2**63 - 1                      # größter SQLite-INTEGER; größere Werte ergäben einen 500er
SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Content-Security-Policy", "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Alles, was die App anlegt (Datenbank samt -wal/-shm, secret.key), nur für den eigenen
    # Benutzer lesbar; vor dem ersten Öffnen der Datenbank. Gilt auch für die Worker-Prozesse.
    old_umask = os.umask(0o077)
    try:
        async with _running(app):
            yield
    finally:
        os.umask(old_umask)


@asynccontextmanager
async def _running(app: FastAPI):
    state.settings = load_settings()
    state.catalog = Catalog.load(state.settings.sources_file)
    s = state.settings
    # KIOSK_PASSWORD legt den Admin an, solange es keine Benutzer gibt (frische Datenbank oder
    # Umbau auf Schema 6); danach hat es keine Wirkung mehr.
    state.db = Database(s.db_path, admin=(s.admin_user, s.password) if s.password else None)
    state.db.retention_days = s.retention_days
    state.db.tz = ZoneInfo(s.tz)
    if state.db.user_count() == 0:
        state.db.close()
        raise RuntimeError(
            "Keine Benutzer in der Datenbank. Entweder KIOSK_PASSWORD setzen (Passwort des Admins "
            "KIOSK_ADMIN_USER, Standard admin) oder den ersten Admin von Hand anlegen: "
            "python -m app.users add <name> --admin, im Container "
            "docker compose run --rm kiosk python -m app.users add <name> --admin")
    if state.db.admin_created:
        log.info("Admin %r aus KIOSK_ADMIN_USER/KIOSK_PASSWORD angelegt", s.admin_user)
    elif s.password:
        log.info("KIOSK_PASSWORD ist gesetzt, wird aber ignoriert: Es gibt schon Benutzer. "
                 "Passwörter ändern mit python -m app.users passwd <name>")
    if state.db.active_admin_count() == 0:
        log.warning("Kein aktiver Admin: Zeitungen und Server lassen sich nicht verwalten. Abhilfe: "
                    "python -m app.users admin <name> --on (oder enable <name>)")
    state.db.ensure_sources([(src.id, src.default_enabled) for src in state.catalog.all()])
    _seed_profiles()
    state.auth = Auth(state.settings)
    state.verify_slots = asyncio.Semaphore(VERIFY_SLOTS)
    state.fetcher = Fetcher(state.db, state.catalog, state.settings)
    state.started_at = datetime.now(timezone.utc)
    await state.fetcher.start()
    # Geschichten einmal beim Start gruppieren (story_id und das Modell für verwandte Themen); das
    # Rechnen läuft in einem Thread, der Start wartet darauf (wenige Sekunden).
    await state.fetcher.update_stories()
    if state.settings.disable_fetcher:
        state.task = None
        log.warning("KIOSK_DISABLE_FETCHER gesetzt: keine automatischen Sammelläufe")
    else:
        state.task = asyncio.create_task(state.fetcher.run_forever())
        state.watchdog = asyncio.create_task(_watchdog())
    log.info("Kiosk gestartet: %d Quellen im Katalog, Datenbank %s",
             len(state.catalog.all()), state.settings.db_path)
    try:
        yield
    finally:
        for task in (state.watchdog, state.task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        state.watchdog = None
        await state.fetcher.close()
        state.db.close()


def _seed_profiles() -> None:
    """Vorgegebene Profile für jeden Benutzer ohne Profile anlegen (neuer Admin; Konten aus der
    Kommandozeile bekommen sie schon dort)."""
    for user_id in state.db.users_without_profiles():
        if seed_builtin_profiles(state.db, state.catalog, user_id):
            log.info("Vorgegebene Profile für Benutzer %d angelegt", user_id)
    invalidate_unread()


# API-Doku nur mit KIOSK_DOCS=1 (wird beim Import gelesen); sonst auch kein /openapi.json.
_DOCS = docs_enabled()
app = FastAPI(title="Kiosk", version="0.1.0", lifespan=lifespan, redoc_url=None,
              docs_url="/docs" if _DOCS else None, openapi_url="/openapi.json" if _DOCS else None)


@app.exception_handler(QueryTimeout)
async def query_timeout(request: Request, exc: QueryTimeout):
    """Eine Leseabfrage hat ihr Zeitlimit überschritten (siehe db.READ_TIME_LIMIT)."""
    log.warning("Abfrage abgebrochen (%s %s): %s", request.method, request.url.path, exc)
    return JSONResponse({"detail": "Die Anfrage dauert zu lange, bitte später erneut versuchen"},
                        status_code=503)


# ---- auth ----------------------------------------------------------------

def _client_ip(request: Request) -> str:
    """Schlüssel der Login-Bremse für den Absender: IPv4-Adresse bzw. IPv6-/64-Netz (auth.ip_key).
    uvicorn --proxy-headers setzt request.client bereits aus X-Forwarded-For, aber nur für
    vertrauenswürdige Proxys (--forwarded-allow-ips)."""
    return ip_key(request.client.host) if request.client else "?"


def _load_user(request: Request) -> dict[str, Any] | None:
    """Angemeldeter Benutzer aus dem Cookie, einmal pro Request (request.state.user)."""
    if not hasattr(request.state, "user"):
        request.state.user = state.auth.user_from_cookies(request.cookies, state.db.get_user)
    return request.state.user


def _origin_tuple(raw: str) -> tuple[str, str, int | None] | None:
    try:
        parts = urlsplit(raw.strip())
        port = parts.port
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname:
        return None
    scheme = parts.scheme.lower()
    default = {"http": 80, "https": 443}.get(scheme)
    return scheme, parts.hostname.lower(), (port if port is not None else default)


def _same_origin(request: Request) -> bool:
    """CSRF-Schutz. sslip.io steht nicht auf der Public Suffix List, SameSite=Lax trennt
    dort also nicht zwischen Subdomains; deshalb prüfen wir Origin bzw. Sec-Fetch-Site."""
    origin = request.headers.get("origin")
    if origin is not None:
        own = _origin_tuple(f"{request.url.scheme}://{request.url.netloc}")
        theirs = _origin_tuple(origin)
        return own is not None and own == theirs
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site.lower() in ("same-origin", "none")
    return True  # weder Origin noch Sec-Fetch-Site: curl, Skripte


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    # Leseabfragen während einer API-Anfrage bekommen eine Frist (db.READ_TIME_LIMIT); eine Aufgabe,
    # die hier gestartet wird, erbt sie (Fetcher._spawn setzt sie für Extraktionen zurück).
    if not path.startswith("/api/"):
        return _secured(await _require_login(request, call_next, path))
    token = db_mod.request_time_limit.set(db_mod.READ_TIME_LIMIT)
    try:
        return _secured(await _require_login(request, call_next, path))
    except QueryTimeout as exc:
        # Aus der Middleware selbst (z. B. Benutzer laden): Die Exception-Handler der App greifen
        # erst hinter call_next, ohne dies würde es ein 500.
        return _secured(await query_timeout(request, exc))
    finally:
        db_mod.request_time_limit.reset(token)


def _secured(response):
    """Sicherheits-Kopfzeilen auch ohne Caddy (Verteidigung in der Tiefe; Caddy setzt dieselben und die
    vollständige CSP, siehe deploy/Caddyfile.kiosk). Die CSP hier schränkt keine Skripte ein (/docs
    lädt sie vom CDN), sie verbietet nur das Einbetten."""
    for name, value in SECURITY_HEADERS:
        response.headers.setdefault(name, value)
    return response


async def _require_login(request: Request, call_next, path: str):
    if path.startswith("/api/") and request.method not in SAFE_METHODS and not _same_origin(request):
        return JSONResponse({"detail": "Fremde Herkunft abgelehnt"}, status_code=403)
    if path.startswith("/api/") or path in ("/docs", "/openapi.json"):
        if _load_user(request) is None and path not in OPEN_PATHS:
            return JSONResponse({"detail": "Nicht angemeldet"}, status_code=401)
    response = await call_next(request)
    # Statische Dateien (index, JS, CSS, sw.js, Schriften, Icons) haben keine versionierten URLs:
    # immer beim Server nachfragen (ETag/Last-Modified machen das billig), damit nach einem Deploy
    # nie ein altes app.js zum neuen index.html passt.
    if "cache-control" not in response.headers:
        # API-Antworten nie zwischenspeichern (auch nicht im Service Worker oder Browser-Cache).
        response.headers["Cache-Control"] = "no-store" if path.startswith("/api/") else "no-cache"
    return response


class HeadAsGet:
    """HEAD für /healthz und alle GET-Routen der API (Monitoring, curl -I): als GET ausführen,
    Kopfzeilen (inkl. Content-Length) behalten, den Rumpf verwerfen. Reine ASGI-Middleware,
    damit der Rumpf nicht erst gepuffert wird. Statische Dateien können HEAD selbst."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if (scope["type"] != "http" or scope["method"] != "HEAD"
                or not (path == "/healthz" or path.startswith("/api/"))):
            await self.app(scope, receive, send)
            return

        async def send_without_body(message):
            if message["type"] == "http.response.body":
                message = {**message, "body": b""}
            await send(message)

        await self.app({**scope, "method": "GET"}, receive, send_without_body)


app.add_middleware(HeadAsGet)


MAX_RANGES = 8     # Bereiche in einer Range-Kopfzeile, darüber wird sie ignoriert (ganze Datei, 200)


class LimitRange:
    """Range-Kopfzeilen mit vielen Bereichen verwerfen (die Antwort ist dann die ganze Datei).
    Starlette fasst Bereiche paarweise zusammen: ca. 8 000 getrennte Bereiche in einer Kopfzeile
    (80 KB, so viel lässt uvicorn zu) kosten auch mit 0.49 noch Sekunden auf dem einen Worker; die
    quadratische Auswertung der Kopfzeile selbst (CVE-2025-62727) behebt Starlette >= 0.49.1. Caddy
    entfernt Range ohnehin (deploy/Caddyfile.kiosk); das hier gilt auch ohne Caddy."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = scope.get("headers") or []
            if any(k == b"range" and v.count(b",") >= MAX_RANGES for k, v in headers):
                scope = {**scope, "headers": [(k, v) for k, v in headers if k not in (b"range", b"if-range")]}
        await self.app(scope, receive, send)


app.add_middleware(LimitRange)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=500)


@app.post("/api/login", status_code=204)
async def login(body: LoginBody, request: Request):
    """Anmelden. Antworten dauern mindestens LOGIN_MIN_SECONDS (per sleep, nicht per CPU), auch jeder
    Fehler (401, aber auch ein unerwarteter wie eine gesperrte Datenbank, der danach unverändert
    weitergeht): Die Dauer hängt sonst an den scrypt-Parametern des gespeicherten Hashes (ältere mit
    p=1 sind schneller geprüft als der Blindhash für unbekannte Namen) oder daran, ob der Fehler vor
    oder nach der Prüfung kam, und verriete, welche Benutzernamen es gibt. 429 kommt sofort (hängt
    nicht an der Existenz des Namens)."""
    started = time.monotonic()
    try:
        response = await _login(body, request)
    except Exception as e:
        if not (isinstance(e, HTTPException) and e.status_code == 429):
            await _login_pause(started)
        raise
    await _login_pause(started)
    return response


async def _login(body: LoginBody, request: Request) -> Response:
    """Ablauf von login ohne die Mindestdauer."""
    ip = _client_ip(request)
    username = normalize_username(body.username)
    # Ungültige Namen teilen sich einen Zähler (sie können kein Konto treffen); so verdrängen
    # beliebige Fantasienamen die Zähler echter Namen nicht aus der begrenzten Liste.
    limit_key = username or INVALID_NAME_KEY
    user = state.db.get_user_by_name(username) if username else None
    # Bekanntes Gerät (gültiges Device-Cookie für genau diesen Namen und dessen aktuellen
    # Passwort-Hash): nicht von der globalen Notbremse und der Bremse je Name betroffen, die ein
    # Angreifer sonst für alle auslösen könnte.
    device = state.auth.device_id(request.cookies, username, user["password_hash"] if user else None)
    # Der Versuch zählt schon vor der Passwortprüfung (atomar mit der Prüfung der Grenzen);
    # ein erfolgreicher Login gibt nur diese Reservierung zurück (die Zähler des Namens verfallen).
    if not state.auth.reserve(ip, limit_key, device):
        raise HTTPException(429, "Zu viele Versuche, bitte später erneut")
    user = await _check_login(user, body.password)
    state.auth.clear(ip, limit_key, device)
    state.db.update_user(user["id"], last_login_at=utcnow())
    response = Response(status_code=204)
    # Ohne Domain-Attribut; mit Secure gilt das __Host-Präfix.
    response.set_cookie(
        state.auth.cookie_name, state.auth.issue_token(user), max_age=state.auth.max_age,
        httponly=True, samesite="lax", secure=state.auth.cookie_secure, path="/",
    )
    # Bekanntes Gerät für diesen Namen und Passwort-Hash (neue ID, Laufzeit DEVICE_MAX_AGE); bleibt
    # beim Abmelden, verfällt mit einem neuen Passwort.
    response.set_cookie(
        state.auth.device_cookie_name, state.auth.issue_device(user["username"], user["password_hash"]),
        max_age=DEVICE_MAX_AGE, httponly=True, samesite="lax", secure=state.auth.cookie_secure, path="/",
    )
    return response


async def _login_pause(started: float) -> None:
    left = LOGIN_MIN_SECONDS - (time.monotonic() - started)
    if left > 0:
        await asyncio.sleep(left)


async def _check_login(user: dict[str, Any] | None, password: str) -> dict[str, Any]:
    """Prüft das Passwort (scrypt im Thread, höchstens VERIFY_SLOTS gleichzeitig; unbekannte Namen
    kosten genauso lange, gesperrte Konten bekommen dieselbe Meldung wie ein falsches Passwort) und
    stellt einen Hash mit älteren scrypt-Parametern um. Gibt den Benutzer mit dem gültigen Hash
    zurück, sonst 401."""
    async with state.verify_slots:
        if user is None:
            await asyncio.to_thread(burn_time, password)
            ok = False
        else:
            ok = await asyncio.to_thread(verify_password_evenly, password, user["password_hash"])
        rehash = None
        if ok and not user["disabled"] and needs_rehash(user["password_hash"]):
            # Hash mit älteren scrypt-Parametern (z. B. p=1): mit den aktuellen neu anlegen. Die
            # Sitzungen des Benutzers auf anderen Geräten enden dabei einmal (ihr Cookie hängt am
            # Fingerabdruck des Hashes, siehe Auth), ebenso seine anderen Device-Cookies.
            rehash = await asyncio.to_thread(hash_password, password)
    if not ok or user["disabled"]:
        raise HTTPException(401, LOGIN_ERROR)
    if rehash is None:
        return user
    # Nur ersetzen, wenn noch der geprüfte Hash gespeichert ist: Ein gleichzeitig neu gesetztes
    # Passwort bleibt, und zwei gleichzeitige erste Logins beenden sich nicht gegenseitig die Sitzung.
    if state.db.replace_password_hash(user["id"], user["password_hash"], rehash):
        log.info("Passwort-Hash von Benutzer %d auf aktuelle scrypt-Parameter umgestellt", user["id"])
        return {**user, "password_hash": rehash}
    fresh = state.db.get_user(user["id"])
    if fresh is None or fresh["disabled"]:
        raise HTTPException(401, LOGIN_ERROR)
    async with state.verify_slots:
        ok = await asyncio.to_thread(verify_password, password, fresh["password_hash"])
    if not ok:
        raise HTTPException(401, LOGIN_ERROR)
    return fresh


@app.post("/api/logout", status_code=204)
async def logout(request: Request, user: dict = Depends(current_user)):
    # Neue Session-Epoche des Benutzers: alle seine bisher ausgegebenen Cookies (auch kopierte)
    # werden ungültig, andere Benutzer bleiben angemeldet.
    state.db.end_sessions(user["id"])
    response = Response(status_code=204)
    for name in COOKIE_NAMES:
        if name in request.cookies:
            response.delete_cookie(name, path="/", secure=name.startswith("__Host-"), httponly=True,
                                   samesite="lax")
    return response


@app.get("/api/session")
async def session(request: Request):
    user = _load_user(request)
    return {"authenticated": user is not None, "user": public_user(user) if user else None}


@app.get("/api/me")
async def me(user: dict = Depends(current_user)):
    return public_user(user)


def _build() -> str:
    # Build-Zeitstempel aus deploy.sh (Docker-Build-Argument); lokal leer.
    return os.environ.get("KIOSK_BUILD", "")


@app.api_route("/api/build", methods=["GET", "HEAD"])
async def build():
    """Ohne Login: nur der Build-Zeitstempel (Version des Service Workers)."""
    return {"build": _build()}


def fetcher_stalled(fetcher_state: dict[str, Any], started_at: datetime | None,
                    fetch_interval: int, now: datetime) -> bool:
    """True, wenn die Sammler-Schleife lebt, aber hängt (reine Funktion, für healthz und Wachhund).

    Läuft gerade ein Lauf, zählt dessen Beginn gegen RUN_MAX (ein langer, aber fortschreitender
    Lauf ist kein Hänger). Sonst zählt das Ende des letzten Laufs (bzw. der Start der App, falls
    noch keiner fertig ist) gegen 3 Intervalle plus STALL_GRACE.
    """
    if fetcher_state.get("running"):
        started = fetcher_state.get("last_started")
        return bool(started) and now - datetime.fromisoformat(started) > RUN_MAX
    last = fetcher_state.get("last_finished")
    ref = datetime.fromisoformat(last) if last else started_at
    if ref is None:
        return False
    return now - ref > timedelta(seconds=3 * fetch_interval) + STALL_GRACE


def _fetcher_problem() -> str | None:
    """'fetcher' (Schleife beendet), 'fetcher_stalled' (hängt) oder None; None auch ohne Sammler."""
    if state.settings.disable_fetcher:
        return None
    if state.task is None or state.task.done():
        return "fetcher"
    if fetcher_stalled(state.fetcher.state, state.started_at, state.settings.fetch_interval,
                       datetime.now(timezone.utc)):
        return "fetcher_stalled"
    return None


def _hard_exit(code: int) -> None:
    os._exit(code)  # in Tests ersetzt


async def _watchdog() -> None:
    """Beendet den Prozess, wenn die Sammler-Schleife tot ist oder hängt; Docker startet den
    Container dann neu (restart: unless-stopped). Ein HEALTHCHECK allein löst keinen Neustart aus.
    os._exit statt sys.exit: Ein hängender Thread oder Worker würde ein sauberes Ende blockieren."""
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL)
        problem = _fetcher_problem()
        if problem:
            log.error("Wachhund: Sammler %s (Status %s), beende den Prozess für einen Neustart",
                      problem, state.fetcher.state)
            for handler in logging.getLogger().handlers:
                handler.flush()
            _hard_exit(WATCHDOG_EXIT_CODE)
            return


@app.get("/healthz")
async def healthz():
    """Gesund, wenn die Datenbank antwortet und (sofern eingeschaltet) die Sammler-Schleife lebt
    und nicht hängt."""
    problems = []
    if not state.db.ping():
        problems.append("db")
    problem = _fetcher_problem()
    if problem:
        problems.append(problem)
    if problems:
        return JSONResponse({"ok": False, "failed": problems}, status_code=503)
    return {"ok": True}


# ---- sources -------------------------------------------------------------

def _source_view(s, st, counts, favorites: set[str], in_all: set[str]) -> dict[str, Any]:
    c = counts.get(s.id, {"total": 0, "unread": 0, "unread_all": 0})
    return {
        "id": s.id, "name": s.name, "short": s.short, "is_favorite": s.id in favorites,
        "country": s.country, "lang": s.lang,
        "category": s.category, "homepage": s.homepage, "note": s.note, "tags": list(s.tags),
        # false: nicht in ALLE und Profilen ohne Quellenliste; je Benutzer (all_extra_sources)
        "in_all": s.id in in_all,
        "enabled": bool(st["enabled"]) if st else s.default_enabled,
        "last_ok_at": st["last_ok_at"] if st else None,
        "last_fetch_at": st["last_fetch_at"] if st else None,
        "last_error": st["last_error"] if st else None,
        "last_new": st["last_new"] if st else 0,
        # unread: ungelesen von heute (Zähler); unread_all: alle ungelesenen (Liste, „alles gelesen“)
        "total": c["total"], "unread": c["unread"], "unread_all": c["unread_all"],
    }


@app.get("/api/sources")
async def list_sources(user: dict = Depends(current_user)):
    states = state.db.source_states()
    counts = state.db.source_counts(user["id"])
    favorites = _favorite_ids(user["id"])
    in_all = in_all_ids(user["id"])
    return [_source_view(s, states.get(s.id), counts, favorites, in_all) for s in state.catalog.all()]


def _favorite_ids(user_id: int) -> set[str]:
    return {f["source_id"] for f in state.db.list_favorites(user_id)}


class SourcePatch(BaseModel):
    enabled: bool


@app.patch("/api/sources/{source_id}")
async def patch_source(source_id: str, body: SourcePatch, user: dict = Depends(admin_user)):
    if source_id not in state.catalog:
        raise HTTPException(404, "Unbekannte Quelle")
    state.db.set_source_enabled(source_id, body.enabled)
    invalidate_unread()
    states = state.db.source_states()
    return _source_view(state.catalog.get(source_id), states.get(source_id),
                        state.db.source_counts(user["id"]), _favorite_ids(user["id"]), in_all_ids(user["id"]))


class BulkSources(BaseModel):
    enabled: list[str] = Field(default_factory=list)
    disabled: list[str] = Field(default_factory=list)


def _check_source_count(ids: list[str]) -> None:
    if len(ids) > MAX_SOURCE_IDS:
        raise HTTPException(400, f"Höchstens {MAX_SOURCE_IDS} Quellen-IDs pro Anfrage")


@app.post("/api/sources/bulk")
async def bulk_sources(body: BulkSources, user: dict = Depends(admin_user)):
    _check_source_count(body.enabled)
    _check_source_count(body.disabled)
    for sid in body.enabled:
        if sid in state.catalog:
            state.db.set_source_enabled(sid, True)
    for sid in body.disabled:
        if sid in state.catalog:
            state.db.set_source_enabled(sid, False)
    invalidate_unread()
    return await list_sources(user)


# ---- articles ------------------------------------------------------------

def _csv(raw: str | None) -> list[str]:
    return [x.strip() for x in raw.split(",") if x.strip()] if raw else []


def _read_minutes(body_len: int) -> int:
    """Lesezeit in Minuten (ca. 1100 Zeichen pro Minute), 0 ohne Text."""
    return max(1, round(body_len / 1100)) if body_len > 0 else 0


def _decorate(item: dict[str, Any]) -> dict[str, Any]:
    """Ergänzt Quellenname, Land, Sprache und Lesezeit."""
    src = state.catalog.get(item["source_id"])
    item["source_name"] = src.name if src else item["source_id"]
    item["country"] = src.country if src else None
    item["lang"] = src.lang if src else None
    item["read_minutes"] = _read_minutes(item.get("body_len") or 0)
    return item


def _filters(user_id: int, source: str | None, country: str | None, profile: int | None, *, unread: bool,
             starred: bool, later: bool, free: bool | None, days: int | None) -> dict[str, Any]:
    """Gemeinsamer Filterkontext für Liste und "nächster Artikel" (Argumente für db.list_articles)."""
    sources = _csv(source)
    _check_source_count(sources)
    saved = starred or later
    # Ohne Quelle/Profil nur aktivierte Quellen; Gemerktes bleibt auch aus ausgeschalteten sichtbar.
    scope, terms = article_scope(user_id, sources, {c.upper() for c in _csv(country)}, profile,
                                 only_enabled=not saved)
    since = _iso(datetime.now(timezone.utc) - timedelta(days=days)) if days else None
    # Paywall-Artikel sind ausgeblendet, außer in Gemerkt (die Leseansicht zeigt dort den
    # Paywall-Hinweis) oder mit ausdrücklichem free=false. Die Einstellung free_only wirkt nicht mehr.
    free_only = (not saved) if free is None else free
    return {"sources": scope, "terms": terms or None, "unread": unread, "starred": starred,
            "later": later, "free_only": free_only, "since": since}


@app.get("/api/articles")
async def list_articles(
    source: str | None = Query(None, description="Quellen-IDs, kommagetrennt"),
    country: str | None = Query(None, description="Ländercodes, kommagetrennt"),
    profile: int | None = Query(None, ge=1, le=MAX_ID, description="Profil-ID (Quellen und Begriffe)"),
    q: str | None = Query(None, max_length=200),
    unread: bool = False,
    starred: bool = False,
    later: bool = Query(False, description="Nur \"Später lesen\""),
    free: bool | None = Query(None, description="Nur Artikel ohne Paywall-Markierung; Standard: true"),
    days: int | None = Query(None, ge=1, le=3650, description="Nur Artikel der letzten n Tage"),
    before: str | None = Query(None, max_length=100,
                               description="Cursor aus `next_before` (\"<published_at>|<id>\"); ohne Suche"),
    offset: int = Query(0, ge=0, le=10_000, description="Versatz für Suchergebnisse (mit `q`)"),
    limit: int = Query(50, ge=1, le=200),
    user: dict = Depends(current_user),
):
    filters = _filters(user["id"], source, country, profile, unread=unread, starred=starred, later=later,
                       free=free, days=days)
    cursor = None if q else _parse_cursor(before, user["id"])
    # Wasserstand vor der Abfrage lesen: Die Oberfläche schickt ihn an read-all, damit später
    # geholte Artikel nicht ungesehen als gelesen gelten.
    max_id = state.db.max_article_id()
    try:
        rows = state.db.list_articles(user["id"], query=q, before=cursor, limit=limit, offset=offset if q else 0,
                                      **filters)
        # Trefferzahl nur bei Suchen, gedeckelt
        total = state.db.count_articles(user["id"], cap=1000, query=q, **filters) if q else None
    except QueryTimeout as e:
        if q:
            raise HTTPException(503, "Die Suche dauert zu lange, bitte genauer suchen") from e
        raise
    except sqlite3.OperationalError as e:
        # Nur Syntaxfehler der FTS5-Abfrage sind ein Fehler der Anfrage; alles andere (z. B.
        # "database is locked") ist einer des Servers und darf nicht als 400 durchgehen.
        if q and _fts_syntax_error(e):
            raise HTTPException(400, "Ungültige Suchanfrage") from e
        raise
    items = [_decorate(dict(r)) for r in rows]
    for it in items:
        if it["paywalled"]:
            it["summary"] = ""  # Bezahlartikel (nur in Gemerkt sichtbar): keinen Anreißer zeigen
    full_page = len(items) == limit
    next_before = f'{items[-1]["published_at"]}|{items[-1]["id"]}' if (full_page and not q) else None
    next_offset = offset + len(items) if (full_page and q) else None
    return {"items": items, "next_before": next_before, "next_offset": next_offset, "total": total,
            "max_id": max_id}


# Meldungen, die nur FTS5 beim Lesen einer MATCH-Abfrage gibt, im Unterschied zu Betriebsfehlern und
# Fehlern im SQL des Servers (dort heißt es 'near "x": syntax error', "no such column: a.x"). Ein
# "no such column" gibt FTS5 zwar auch (Spaltenfilter "spalte:wort"), aber _fts_query setzt jedes Wort
# in Anführungszeichen: Aus der Suchanfrage kann es nicht kommen, nur aus einem Fehler des Servers.
_FTS_ERROR_RE = re.compile(r'fts5: syntax error near "|unterminated string\Z|unknown special query: '
                           r'|expected integer, got "')


def _fts_syntax_error(e: sqlite3.OperationalError) -> bool:
    """True, wenn SQLite die FTS5-Abfrage (MATCH mit der Suchanfrage) nicht lesen konnte: 400 statt 500."""
    return bool(_FTS_ERROR_RE.match(str(e)))


@app.get("/api/articles/{article_id}/next")
async def next_article(
    article_id: int = PathParam(ge=1, le=MAX_ID),
    source: str | None = Query(None), country: str | None = Query(None),
    profile: int | None = Query(None, ge=1, le=MAX_ID),
    unread: bool = True, starred: bool = False, later: bool = False,
    free: bool | None = None, days: int | None = Query(None, ge=1, le=3650),
    user: dict = Depends(current_user),
):
    """Nächster älterer Artikel im selben Filterkontext wie die Liste (Keyset ab dem aktuellen).
    Einen Artikel, den der Benutzer nicht sehen darf (hinter der Aufbewahrungsgrenze, nur von
    anderen gemerkt), gibt es hier so wenig wie in GET /api/articles/{id}."""
    published = state.db.published_at_of(article_id, user["id"])
    if published is None:
        raise HTTPException(404, "Artikel nicht gefunden")
    filters = _filters(user["id"], source, country, profile, unread=unread, starred=starred, later=later,
                       free=free, days=days)
    rows = state.db.list_articles(user["id"], query=None, before=(published, article_id), limit=1, **filters)
    return {"item": _decorate(dict(rows[0])) if rows else None}


_ASCII_INT = re.compile(r"[0-9]+")


def _parse_cursor(raw: str | None, user_id: int) -> tuple[str, int] | None:
    """Cursor "<published_at>|<id>"; eine reine Zahl (altes Format) gilt als Artikel-ID, die der
    Benutzer sehen dürfen muss (sonst wie eine unbekannte ID).

    Nur ASCII-Ziffern (str.isdigit() ließe z. B. "²" durch, int() scheitert dann). Der Zeitpunkt
    wird geprüft und in das gespeicherte Format (UTC, isoformat) gebracht.
    """
    if not raw:
        return None
    raw = raw.strip()
    if _ASCII_INT.fullmatch(raw):
        if int(raw) > MAX_ID:
            raise HTTPException(400, "Ungültiger Cursor")
        published = state.db.published_at_of(int(raw), user_id)
        if published is None:
            raise HTTPException(400, "Unbekannter Cursor")
        return published, int(raw)
    published, sep, id_part = raw.rpartition("|")
    if not sep or not published or not _ASCII_INT.fullmatch(id_part) or int(id_part) > MAX_ID:
        raise HTTPException(400, "Ungültiger Cursor")
    # Ein nicht kodiertes "+" (aus "+00:00") kommt in der Query als Leerzeichen an.
    published = published.replace(" ", "+")
    try:
        dt = datetime.fromisoformat(published)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        published = dt.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        raise HTTPException(400, "Ungültiger Cursor") from None
    return published, int(id_part)


def _from_own_script(request: Request) -> bool:
    """False, wenn der Browser sagt, dass die Anfrage nicht von fetch() der eigenen Seite kommt:
    ein Bild, Frame oder Skript (Sec-Fetch-Dest ungleich "empty") oder eine fremde Herkunft
    (Sec-Fetch-Site "cross-site" / "same-site"). Ohne diese Kopfzeilen (curl, ältere Browser) True.

    Gespeicherte Artikel dürfen Bilder von beliebigen Adressen einbinden, auch
    <img src="https://<kiosk>/api/articles/5?retry=true"> (same-origin, mit Cookie): Ein GET soll
    dann nichts anstoßen, nur lesen."""
    dest = request.headers.get("sec-fetch-dest")
    site = request.headers.get("sec-fetch-site")
    if dest is not None and dest.lower() != "empty":
        return False
    return site is None or site.lower() in ("same-origin", "none")


@app.get("/api/articles/{article_id}")
async def get_article(request: Request, article_id: int = PathParam(ge=1, le=MAX_ID), retry: bool = False,
                      user: dict = Depends(current_user)):
    row = state.db.get_article(article_id, user["id"])
    if not row:
        raise HTTPException(404, "Artikel nicht gefunden")
    # Auf Anforderung nur extrahieren, wenn fällig; ?retry=true darf den Zeitplan nur für
    # abgeschlossene Artikel übergehen (Fehlschläge zählen dabei weiter als Versuche).
    # Nicht blockierend: Die Extraktion läuft im Hintergrund, die Antwort meldet dann
    # text_status "pending" und extracting=true, die Oberfläche fragt erneut nach.
    # Gedeckelt: Laufen schon zu viele Abrufe auf Anforderung oder liegt der letzte erneute
    # Versuch keine zwei Minuten zurück, startet keiner (extracting bleibt false).
    due = row["next_attempt_at"] is None or row["next_attempt_at"] <= utcnow()
    forced = retry and row["text_status"] in ("failed", "empty", "partial")
    extracting = state.fetcher.is_extracting(article_id)
    # Als Bild, Frame o. Ä. eingebunden oder von fremder Seite (siehe _from_own_script): nur lesen.
    may_start = _from_own_script(request)
    if may_start and not extracting and ((row["text_status"] == "pending" and due) or forced):
        extracting = state.fetcher.start_extraction(article_id, row["url"], retry=forced, source=row["source_id"])
    item = _decorate(dict(row))
    item["extracting"] = extracting
    if extracting:
        item["text_status"] = "pending"
    if item["paywalled"]:
        # Bezahlartikel: keinen Text ausliefern, die Oberfläche zeigt nur Hinweis und Original.
        item.update(body_html=None, body_text=None, summary="", paywalled=1, read_minutes=0)
    elif item["source_id"] in STORED_CLEANUP_SOURCES and len(item.get("body_html") or "") <= STORED_CLEANUP_MAX:
        # Vor der Bereinigung gespeicherte SPIEGEL-Artikel (Liste „3 Min / 6 Min“ aus „Mehr zum Thema“-
        # Kästen) beim Ausliefern bereinigen statt die Datenbank umzuschreiben. Nur body_html: body_text
        # liest die Oberfläche nicht; Suche (FTS) und Lesezeit bleiben beim gespeicherten Stand.
        item["body_html"] = strip_reading_times(item["body_html"])
    return item


# ---- Geschichten (rechte Seite am Desktop) --------------------------------------------

SAME_STORY_MAX = 12         # „dieselbe Geschichte“: höchstens so viele andere Artikel (einer je Zeitung)
TOPIC_MAX = stories.TOPIC_LIMIT
TOP_STORIES_MAX = 5         # Tagesübersicht: so viele Geschichten ...
TOP_STORY_MIN_SOURCES = 3   # ... mit mindestens so vielen Zeitungen heute
TOP_STORY_MIN_TODAY = 3     # weniger Geschichten heute (kurz nach Mitternacht): die letzten 24 Stunden
TOP_STORY_SOURCES = 4       # Kurznamen je Geschichte
FRONTPAGE_SECTIONS = 4      # Titelseite: höchstens so viele Profile ...
FRONTPAGE_ITEMS = 3         # ... mit je so vielen Artikeln ...
FRONTPAGE_HOURS = 24        # ... aus den letzten so vielen Stunden
FRONTPAGE_GROUPS = 12       # Geschichten je Profil, aus denen die Titelseite wählt
USER_LANG = "de"            # Sprache der Oberfläche: deutschsprachige Artikel zuerst
# Übersicht und Titelseite haben zusammen höchstens so viele Sekunden für ihre Abfragen; was dann
# fehlt (eine Rubrik, die Geschichten des Tages), bleibt leer statt einer 503.
STORY_PAGE_BUDGET = 3.0
CENTRAL_SHARE = 0.85        # Vertreter einer Geschichte: so mittig wie der mittigste (Anteil)


class _Budget:
    """Gemeinsames Zeitbudget der Abfragen einer Anfrage; jede einzelne höchstens db.READ_TIME_LIMIT."""

    def __init__(self, seconds: float | None = None):
        self.deadline = time.monotonic() + (STORY_PAGE_BUDGET if seconds is None else seconds)

    def limit(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise QueryTimeout("Zeitbudget der Anfrage verbraucht")
        return min(db_mod.READ_TIME_LIMIT, left)


def _item(row) -> dict[str, Any]:
    """Listeneintrag wie in GET /api/articles; die mitgelesene story_id fällt weg."""
    item = _decorate(dict(row))
    item.pop("story_id", None)
    if item["paywalled"]:
        item["summary"] = ""
    return item


def _lang_of(row) -> str | None:
    src = state.catalog.get(row["source_id"])
    return src.lang if src else None


def _count_sources(rows) -> int:
    """Zahl der Zeitungen einer Geschichte: verschiedene Verlage (stories.publisher). Dieselbe
    Agenturmeldung in zwei Zeitungen zählt doppelt, die Ausgaben von The Local oder heise und heise
    Security zählen einmal."""
    return len({stories.publisher(r["source_id"]) for r in rows})


def _publishers() -> dict[str, str]:
    """Quelle -> Verlag für Quellen, die mit anderen als eine Zeitung zählen (für die SQL-Zählung)."""
    return {s.id: stories.publisher(s.id) for s in state.catalog.all() if stories.publisher(s.id) != s.id}


def _ranked(rows, *, image_first: bool = False) -> list:
    """Artikel einer Geschichte, der beste Vertreter zuerst: Er soll die Geschichte erzählen, also
    mittig stehen (stories.centrality: mindestens CENTRAL_SHARE der größten Mittigkeit der Gruppe);
    unter den mittigen deutschsprachig, mit Bild, mit Volltext, dann der neueste (der aktuelle Stand).
    Titelseite: das Bild vor der Sprache. Ohne Lauf der Gruppierung zählt jeder als mittig."""
    cent = stories.central_scores([r["id"] for r in rows])
    top = max(cent.values(), default=0.0)

    def key(row) -> tuple:
        central = cent.get(row["id"], 0.0) >= CENTRAL_SHARE * top
        de, img = _lang_of(row) == USER_LANG, bool(row["image_url"])
        first = (img, de) if image_first else (de, img)
        return (central, *first, row["text_status"] == "full", row["published_at"], row["id"])

    return sorted(rows, key=key, reverse=True)


def _short(source_id: str) -> str:
    src = state.catalog.get(source_id)
    return src.short if src else source_id


def _top_stories(user_id: int, scope: list[str], since: str, budget: _Budget, *, min_sources: int, limit: int,
                 image_first: bool = False) -> list[dict[str, Any]]:
    """Geschichten mit den meisten Zeitungen seit `since` (Artikel ohne Paywall aus `scope`, nur deren
    Artikel zählen), zwei Abfragen für alle. Sortiert nach Zahl der Zeitungen, dann dem neuesten Artikel."""
    cand = state.db.top_stories(sources=scope, since=since, min_sources=min_sources, limit=3 * limit,
                                publishers=_publishers(), time_limit=budget.limit())
    by_story: dict[int, list] = defaultdict(list)
    for r in state.db.story_articles(user_id, [c["story_id"] for c in cand], sources=scope, since=since,
                                     time_limit=budget.limit()):
        by_story[r["story_id"]].append(r)
    out = []
    for c in cand:
        rows = by_story.get(c["story_id"], [])
        n = _count_sources(rows)
        if n < min_sources:
            continue
        ranked = _ranked(rows, image_first=image_first)
        # Je Zeitung ein Kurzname (der erste in der Reihenfolge der Auswahl): nie mehr Namen als Zeitungen.
        papers: dict[str, str] = {}
        for r in ranked:
            papers.setdefault(stories.publisher(r["source_id"]), _short(r["source_id"]))
        shorts = list(dict.fromkeys(papers.values()))
        out.append({"story_id": c["story_id"], "sources_count": n, "sources": shorts[:TOP_STORY_SOURCES],
                    "article": _item(ranked[0]), "_newest": max(r["published_at"] for r in rows)})
    out.sort(key=lambda s: s["_newest"], reverse=True)
    out.sort(key=lambda s: s["sources_count"], reverse=True)
    for s in out:
        del s["_newest"]
    return out[:limit]


@app.get("/api/articles/{article_id}/related")
async def related(article_id: int = PathParam(ge=1, le=MAX_ID), user: dict = Depends(current_user)):
    """Rechte Seite zu einem Artikel: dieselbe Geschichte in anderen Zeitungen (eine je Zeitung, ohne
    die eigene, deutschsprachige zuerst, dann die neuesten) und verwandte Themen. Zur Geschichte zählen
    nur Artikel, die höchstens stories.MAX_GAP vom offenen Artikel entfernt erschienen sind."""
    row = state.db.get_article(article_id, user["id"])
    if not row:
        raise HTTPException(404, "Artikel nicht gefunden")
    row = dict(row)
    scope = implicit_sources(user["id"])
    budget = _Budget()                  # alle Abfragen der Seite zusammen (wie Übersicht und Titelseite)
    story_id = state.db.story_id_of(article_id)
    row["story_id"] = story_id          # get_article liefert sie nicht; topic_ids braucht sie (vor dem Fenster)
    exclude = {article_id}
    members: list = []
    try:
        if story_id is not None:
            at = datetime.fromisoformat(row["published_at"])
            members = [m for m in state.db.story_articles(user["id"], [story_id], sources=scope,
                                                          since=_iso(at - stories.MAX_GAP),
                                                          until=_iso(at + stories.MAX_GAP),
                                                          time_limit=budget.limit())
                       if m["id"] != article_id]
            if _count_sources([*members, row]) < 2:
                # Eine „Geschichte“ aus nur einer Zeitung (übrig geblieben, nachdem die anderen Artikel
                # anders gruppiert wurden) ist keine: wie ohne Geschichte.
                story_id = row["story_id"] = None
                members = []
            else:
                exclude.update(state.db.story_member_ids(story_id, time_limit=budget.limit()))
        if story_id is None:
            # Ohne Geschichte: Artikel anderer Zeitungen, die trotzdem dasselbe Ereignis erzählen
            # (stories.same_event_ids), stehen hier und nicht unter den Themen.
            ids = stories.same_event_ids(row, {article_id})
            exclude.update(ids)
            found = {r["id"]: r for r in state.db.articles_by_ids(user["id"], ids, sources=scope,
                                                                  time_limit=budget.limit())}
            members = [found[i] for i in ids if i in found]
            # Gehört so ein Artikel zu einer Geschichte, ist deren ganzer Rest auch kein anderes Thema.
            for sid in sorted({r["story_id"] for r in found.values() if r["story_id"] is not None}):
                exclude.update(state.db.story_member_ids(sid, time_limit=budget.limit()))
    except (QueryTimeout, sqlite3.OperationalError) as e:
        log.warning("Dieselbe Geschichte zu %s: %s", article_id, e)
        members = []
    own, own_key = stories.publisher(row["source_id"]), stories.copy_key(row)
    # Je Zeitung der beste Vertreter (siehe _ranked); wortgleiche Kopien und englische Artikel, die mit
    # dem offenen nur über andere Mitglieder zusammenhängen (stories.shown_with), fehlen.
    best: dict[str, Any] = {}
    for m in _ranked(members):
        paper = stories.publisher(m["source_id"])
        if paper == own or (own_key and stories.copy_key(m) == own_key):
            continue
        if paper not in best and stories.shown_with(article_id, m["id"]):
            best[paper] = m
    shown = list(best.values())[:SAME_STORY_MAX]
    same = [_item(m) for m in shown]
    # Zeitungen, die die Seite auch zeigt (und die eigene): Kopf und Liste passen zusammen.
    count = _count_sources([*shown, row])
    try:
        ids = stories.topic_ids(state.db, row, exclude, time_limit=budget.limit())
        found = {r["id"]: r for r in state.db.articles_by_ids(user["id"], ids, sources=scope,
                                                              time_limit=budget.limit())}
        topic = [_item(found[i]) for i in ids if i in found][:TOPIC_MAX]
    except (QueryTimeout, sqlite3.OperationalError) as e:
        log.warning("Verwandte Themen zu %s: %s", article_id, e)
        topic = []
    return {"story_id": story_id, "sources_count": count, "same_story": same, "topic": topic}


@app.get("/api/overview")
async def overview(user: dict = Depends(current_user)):
    """Tagesübersicht (rechte Seite ohne offenen Artikel): Zähler, Geschichten mit den meisten
    Zeitungen heute, Artikel je Stunde. „Heute“ ab 00:00 KIOSK_TZ wie die Ungelesen-Zähler, Quellen
    wie ALLE, ohne Paywall. Reicht das Zeitbudget nicht, bleiben Stunden bzw. Geschichten leer."""
    budget = _Budget()
    local = datetime.now(timezone.utc).astimezone(state.db.tz)
    today = state.db.today_start()
    scope = implicit_sources(user["id"])
    counts = {"unread": None, "later": None}
    try:
        counts = state.db.article_counts(user["id"], scope, time_limit=budget.limit())
    except QueryTimeout:
        log.warning("Tagesübersicht: Zähler abgebrochen (Zeitlimit)")
    hourly: list[int] = [0] * 24
    new_today = active = None
    try:
        day = state.db.published_since(sources=scope, since=today, time_limit=budget.limit())
        for r in day:
            try:
                hourly[datetime.fromisoformat(r["published_at"]).astimezone(state.db.tz).hour] += 1
            except ValueError:
                continue
        new_today = len(day)
        active = len({stories.publisher(r["source_id"]) for r in day})      # Zeitungen, nicht Quellen
    except QueryTimeout:
        log.warning("Tagesübersicht: Artikel des Tages abgebrochen (Zeitlimit)")
        hourly = []                     # die Oberfläche zeigt dann keine Stunden (Zähler: „–“)
    top, window = [], "today"
    try:
        top = _top_stories(user["id"], scope, today, budget, min_sources=TOP_STORY_MIN_SOURCES,
                           limit=TOP_STORIES_MAX)
        # Kurz nach Mitternacht hat „heute“ noch kaum Geschichten: dann die der letzten 24 Stunden.
        if len(top) < TOP_STORY_MIN_TODAY:
            day_before = _top_stories(user["id"], scope, _iso(datetime.now(timezone.utc) - timedelta(hours=24)),
                                      budget, min_sources=TOP_STORY_MIN_SOURCES, limit=TOP_STORIES_MAX)
            if len(day_before) > len(top):
                top, window = day_before, "24h"
    except QueryTimeout:
        log.warning("Tagesübersicht: Geschichten des Tages abgebrochen (Zeitlimit)")
    return {
        "date": local.date().isoformat(), "now_hour": local.hour,
        "counts": {"new_today": new_today, "unread_today": counts["unread"], "later": counts["later"],
                   "active_sources": active},
        "top_stories": top,
        "window": window,
        "hourly": hourly,
    }


@app.get("/api/frontpage")
async def frontpage(user: dict = Depends(current_user)):
    """Titelseite (rechte Seite ohne offenen Artikel): die größte Geschichte des Tages als Aufmacher,
    dazu je Profil (die oben als Filterchip, sonst alle; höchstens FRONTPAGE_SECTIONS) die Geschichten
    der letzten FRONTPAGE_HOURS Stunden mit den meisten Zeitungen. Jede Geschichte erscheint nur
    einmal auf der Seite (Aufmacher und frühere Rubriken gehen vor). Eine Rubrik, deren Abfrage das
    Zeitbudget sprengt, fehlt."""
    uid = user["id"]
    budget = _Budget()
    local = datetime.now(timezone.utc).astimezone(state.db.tz)
    scope = implicit_sources(user["id"])
    lead = None
    window = "today"
    try:
        top = _top_stories(uid, scope, state.db.today_start(), budget, min_sources=2, limit=1, image_first=True)
        if not top:                     # kurz nach Mitternacht: der Aufmacher der letzten 24 Stunden
            top = _top_stories(uid, scope, _iso(datetime.now(timezone.utc) - timedelta(hours=24)), budget,
                               min_sources=2, limit=1, image_first=True)
            window = "24h" if top else "today"
    except QueryTimeout:
        log.warning("Titelseite: Aufmacher abgebrochen (Zeitlimit)")
        top = []
    if top:
        lead = {k: top[0][k] for k in ("story_id", "sources_count", "article")}
    profiles = state.db.list_profiles(uid)
    chosen = ([p for p in profiles if p["in_chips"]] or profiles)[:FRONTPAGE_SECTIONS]
    now = datetime.now(timezone.utc)
    since = _iso(now - timedelta(hours=FRONTPAGE_HOURS))
    used_stories = {lead["story_id"]} if lead else set()
    used_ids = {lead["article"]["id"]} if lead else set()
    sections = []
    for p in chosen:
        sources, terms = effective_sources(uid, p), p["terms"] or None
        try:
            groups = [g for g in state.db.profile_story_groups(uid, sources=sources, terms=terms, since=since,
                                                               limit=FRONTPAGE_GROUPS + len(used_stories),
                                                               publishers=_publishers(),
                                                               time_limit=budget.limit())
                      if g["k"] not in used_stories]
            rows = state.db.profile_group_articles(uid, [g["k"] for g in groups], sources=sources, terms=terms,
                                                   since=since, time_limit=budget.limit())
            story_ids = [g["k"] for g in groups if g["k"] > 0]
            # Zeitungen je Geschichte wie in der Übersicht (Quellen wie ALLE), aus denselben 24 Stunden
            members: dict[int, list] = defaultdict(list)
            for r in state.db.story_articles(uid, story_ids, sources=scope, since=since,
                                             time_limit=budget.limit()):
                members[r["story_id"]].append(r)
        except QueryTimeout:
            log.warning("Titelseite: Profil %s (Benutzer %s) abgebrochen: Zeitlimit", p["id"], uid)
            continue
        by_key: dict[int, list] = defaultdict(list)
        for r in rows:
            by_key[r["story_id"] if r["story_id"] is not None else -r["id"]].append(r)
        entries = []
        for g in groups:
            grp = [r for r in by_key.get(g["k"], []) if r["id"] not in used_ids]
            if not grp:
                continue
            pick = _ranked(grp, image_first=True)[0]
            n = max(_count_sources(members.get(g["k"], [])), 1) if g["k"] > 0 else 1
            entries.append((n, pick["published_at"], g["k"], pick))
        entries.sort(key=lambda e: (e[0], e[1]), reverse=True)
        items = []
        for n, _, key, pick in entries[:FRONTPAGE_ITEMS]:
            used_stories.add(key)
            used_ids.add(pick["id"])
            items.append({"article": _item(pick), "sources_count": n})
        if items:
            sections.append({"profile_id": p["id"], "name": p["name"], "items": items})
    # Tag und Stunde des Servers (KIOSK_TZ) wie in der Übersicht: Die Oberfläche lädt um Mitternacht neu.
    return {"date": local.date().isoformat(), "now_hour": local.hour, "window": window, "lead": lead,
            "sections": sections}


class FlagBody(BaseModel):
    value: bool = True


@app.post("/api/articles/{article_id}/read")
async def set_read(article_id: int = PathParam(ge=1, le=MAX_ID), body: FlagBody = FlagBody(),
                   user: dict = Depends(current_user)):
    if not state.db.set_flag(user["id"], article_id, "read", body.value):
        raise HTTPException(404, "Artikel nicht gefunden")
    invalidate_unread(user["id"])
    return {"id": article_id, "read": body.value}


@app.post("/api/articles/{article_id}/star")
async def set_star(article_id: int = PathParam(ge=1, le=MAX_ID), body: FlagBody = FlagBody(),
                   user: dict = Depends(current_user)):
    if not state.db.set_flag(user["id"], article_id, "starred", body.value):
        raise HTTPException(404, "Artikel nicht gefunden")
    return {"id": article_id, "starred": body.value}


@app.post("/api/articles/{article_id}/later")
async def set_later(article_id: int = PathParam(ge=1, le=MAX_ID), body: FlagBody = FlagBody(),
                    user: dict = Depends(current_user)):
    if not state.db.set_flag(user["id"], article_id, "later", body.value):
        raise HTTPException(404, "Artikel nicht gefunden")
    return {"id": article_id, "later": body.value}


class ReadAllBody(BaseModel):
    sources: list[str] = Field(default_factory=list)
    source: str | None = Field(None, max_length=100)
    profile: int | None = Field(None, ge=1, le=MAX_ID)
    max_id: int | None = Field(None, ge=1, le=MAX_ID, description="Nur Artikel mit ID <= max_id")


@app.post("/api/articles/read-all")
async def read_all(body: ReadAllBody = ReadAllBody(), user: dict = Depends(current_user)):
    """Alles als gelesen markieren; `sources`, `source` und `profile` schränken die Menge ein
    (mehrere Angaben: Schnittmenge), `max_id` begrenzt auf die bereits angezeigten Artikel.
    `marked` zählt nur Artikel ohne Paywall (wie die Zähler der Oberfläche)."""
    _check_source_count(body.sources)
    sources = list(body.sources)
    if body.source is not None:
        sources = [s for s in sources if s == body.source] if sources else [body.source]
        if not sources:  # Widerspruch zwischen sources und source: nichts markieren
            return {"marked": 0}
    scope, terms = article_scope(user["id"], sources, set(), body.profile)
    # Blockweise (je Block eine Transaktion), dazwischen kommen andere Anfragen dran. Die Zähler
    # verwerfen auch, wenn die Anfrage mittendrin abbricht: Die fertigen Blöcke sind gespeichert.
    marked = 0
    try:
        for n in state.db.mark_all_read_steps(user["id"], scope, terms or None, body.max_id):
            marked += n
            await asyncio.sleep(0)
    finally:
        invalidate_unread(user["id"])
    return {"marked": marked}


# ---- status --------------------------------------------------------------

@app.post("/api/refresh", dependencies=[Depends(admin_user)])
async def refresh():
    # Ohne Sammler-Schleife (KIOSK_DISABLE_FETCHER) weckt trigger() niemanden: 503 statt eines
    # {"started": true}, auf das nie ein Lauf folgt. Nicht 409: Das zeigt die Oberfläche als
    # "Läuft bereits" bzw. "Wird im nächsten Lauf geholt", und einen nächsten Lauf gibt es nicht.
    if state.settings.disable_fetcher or state.task is None:
        raise HTTPException(503, "Der Sammler ist ausgeschaltet")
    if not state.fetcher.trigger():
        raise HTTPException(409, "Ein Sammellauf läuft bereits")
    return {"started": True}


@app.get("/api/status")
async def status(user: dict = Depends(current_user)):
    # Nicht-Admins sehen nur Build und Sammler-Zustand, keine Server-Werte.
    if not user["is_admin"]:
        return {"build": _build(), "fetcher": state.fetcher.state}
    return {
        "build": _build(),
        "fetcher": state.fetcher.state,
        "db": state.db.stats(user["id"]),
        "settings": {
            "fetch_interval": state.settings.fetch_interval,
            "retention_days": state.settings.retention_days,
            "sources_total": len(state.catalog.all()),
            "sources_enabled": len(state.db.enabled_source_ids()),
        },
    }


# ---- Oberfläche (Profile, Lieblingszeitungen, Leiste, Einstellungen, Zähler) ----
# Muss vor dem StaticFiles-Mount stehen, der alles Übrige unter "/" abfängt.
app.include_router(api_ui.router)


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.api_route("/favicon.ico", methods=["GET", "HEAD"], include_in_schema=False)
async def favicon():
    return FileResponse(STATIC_DIR / "icons" / "favicon.ico", media_type="image/x-icon")


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
@app.api_route("/index.html", methods=["GET", "HEAD"], include_in_schema=False)
async def index(request: Request):
    """index.html mit der Build-Kennung der ausliefernden App als data-build an <html> (sonst ist die
    Seite statisch). Die Oberfläche vergleicht damit /api/build: Kam die Seite aus dem Cache des
    Service Workers (der sie mit der Kennung von damals gespeichert hat), fällt ein Deploy auf,
    auch wenn /api/build danach wieder normal antwortet. ETag für bedingte Abrufe (Cache-Control
    no-cache setzt die Middleware)."""
    page = (STATIC_DIR / "index.html").read_bytes().replace(
        b'data-build=""', f'data-build="{html_escape(_build(), quote=True)}"'.encode(), 1)
    etag = f'"{hashlib.sha256(page).hexdigest()[:32]}"'
    match = request.headers.get("if-none-match", "")
    if match.strip() == "*" or etag in (t.strip().removeprefix("W/") for t in match.split(",")):
        return Response(status_code=304, headers={"ETag": etag})
    return Response(page, media_type="text/html; charset=utf-8", headers={"ETag": etag})

# Das schlanke Docker-Image hat keine /etc/mime.types; Schriften und Manifest sonst als text/plain.
for _ext, _type in ((".woff2", "font/woff2"), (".woff", "font/woff"), (".webmanifest", "application/manifest+json"),
                    (".ico", "image/x-icon"), (".svg", "image/svg+xml"), (".mjs", "text/javascript")):
    mimetypes.add_type(_type, _ext)
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
