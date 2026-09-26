from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import (COOKIE_NAMES, INVALID_NAME_KEY, LOGIN_ERROR, Auth, burn_time, ip_key, normalize_username,
                   verify_password)
from .config import docs_enabled, load_settings
from .db import Database, QueryTimeout, _iso, utcnow
from .fetcher import Fetcher
from .sources import Catalog
from .state import State, state  # noqa: F401  (State für Importe von außen)
from . import api_ui
from . import db as db_mod
from .api_ui import admin_user, article_scope, current_user, invalidate_unread, public_user
from .users import seed_builtin_profiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kiosk")

OPEN_PATHS = {"/healthz", "/api/login", "/api/session", "/api/build", "/", "/index.html", "/favicon.ico"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
MAX_SOURCE_IDS = 200        # höchstens so viele Quellen-IDs pro Anfrage (Query oder Body)
STALL_GRACE = timedelta(minutes=10)
RUN_MAX = timedelta(hours=3)            # so lange darf ein einzelner Sammellauf höchstens dauern
WATCHDOG_INTERVAL = 60                  # Sekunden zwischen zwei Prüfungen des Wachhunds
WATCHDOG_EXIT_CODE = 3
VERIFY_SLOTS = 2                        # gleichzeitige scrypt-Prüfungen beim Login (je ca. 16 MiB)
MAX_ID = 2**63 - 1                      # größter SQLite-INTEGER; größere Werte ergäben einen 500er


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
        return await _require_login(request, call_next, path)
    token = db_mod.request_time_limit.set(db_mod.READ_TIME_LIMIT)
    try:
        return await _require_login(request, call_next, path)
    except QueryTimeout as exc:
        # Aus der Middleware selbst (z. B. Benutzer laden): Die Exception-Handler der App greifen
        # erst hinter call_next, ohne dies würde es ein 500.
        return await query_timeout(request, exc)
    finally:
        db_mod.request_time_limit.reset(token)


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


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=500)


@app.post("/api/login", status_code=204)
async def login(body: LoginBody, request: Request):
    ip = _client_ip(request)
    username = normalize_username(body.username)
    # Ungültige Namen teilen sich einen Zähler (sie können kein Konto treffen); so verdrängen
    # beliebige Fantasienamen die Zähler echter Namen nicht aus der begrenzten Liste.
    limit_key = username or INVALID_NAME_KEY
    # Der Versuch zählt schon vor der Passwortprüfung (atomar mit der Prüfung der Grenzen);
    # ein erfolgreicher Login gibt nur diese Reservierung zurück (die Zähler des Namens verfallen).
    if not state.auth.reserve(ip, limit_key):
        raise HTTPException(429, "Zu viele Versuche, bitte später erneut")
    user = state.db.get_user_by_name(username) if username else None
    # scrypt im Thread (blockiert sonst die Event-Loop), höchstens zwei gleichzeitig; unbekannte
    # Namen kosten genauso lange, gesperrte Konten bekommen dieselbe Meldung wie ein falsches Passwort.
    async with state.verify_slots:
        if user is None:
            await asyncio.to_thread(burn_time, body.password)
            ok = False
        else:
            ok = await asyncio.to_thread(verify_password, body.password, user["password_hash"])
    if not ok or user["disabled"]:
        raise HTTPException(401, LOGIN_ERROR)
    state.auth.clear(ip, limit_key)
    state.db.update_user(user["id"], last_login_at=utcnow())
    response = Response(status_code=204)
    # Ohne Domain-Attribut; mit Secure gilt das __Host-Präfix.
    response.set_cookie(
        state.auth.cookie_name, state.auth.issue_token(user), max_age=state.auth.max_age,
        httponly=True, samesite="lax", secure=state.auth.cookie_secure, path="/",
    )
    return response


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

def _source_view(s, st, counts, favorites: set[str]) -> dict[str, Any]:
    c = counts.get(s.id, {"total": 0, "unread": 0, "unread_all": 0})
    return {
        "id": s.id, "name": s.name, "short": s.short, "is_favorite": s.id in favorites,
        "country": s.country, "lang": s.lang,
        "category": s.category, "homepage": s.homepage, "note": s.note, "tags": list(s.tags),
        "in_all": s.in_all,   # false: nicht in ALLE und Profilen ohne Quellenliste
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
    return [_source_view(s, states.get(s.id), counts, favorites) for s in state.catalog.all()]


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
                        state.db.source_counts(user["id"]), _favorite_ids(user["id"]))


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
    cursor = None if q else _parse_cursor(before)
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
        if q:
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


@app.get("/api/articles/{article_id}/next")
async def next_article(
    article_id: int = PathParam(ge=1, le=MAX_ID),
    source: str | None = Query(None), country: str | None = Query(None),
    profile: int | None = Query(None, ge=1, le=MAX_ID),
    unread: bool = True, starred: bool = False, later: bool = False,
    free: bool | None = None, days: int | None = Query(None, ge=1, le=3650),
    user: dict = Depends(current_user),
):
    """Nächster älterer Artikel im selben Filterkontext wie die Liste (Keyset ab dem aktuellen)."""
    published = state.db.published_at_of(article_id)
    if published is None:
        raise HTTPException(404, "Artikel nicht gefunden")
    filters = _filters(user["id"], source, country, profile, unread=unread, starred=starred, later=later,
                       free=free, days=days)
    rows = state.db.list_articles(user["id"], query=None, before=(published, article_id), limit=1, **filters)
    return {"item": _decorate(dict(rows[0])) if rows else None}


_ASCII_INT = re.compile(r"[0-9]+")


def _parse_cursor(raw: str | None) -> tuple[str, int] | None:
    """Cursor "<published_at>|<id>"; eine reine Zahl (altes Format) gilt als Artikel-ID.

    Nur ASCII-Ziffern (str.isdigit() ließe z. B. "²" durch, int() scheitert dann). Der Zeitpunkt
    wird geprüft und in das gespeicherte Format (UTC, isoformat) gebracht.
    """
    if not raw:
        return None
    raw = raw.strip()
    if _ASCII_INT.fullmatch(raw):
        if int(raw) > MAX_ID:
            raise HTTPException(400, "Ungültiger Cursor")
        published = state.db.published_at_of(int(raw))
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


@app.get("/api/articles/{article_id}")
async def get_article(article_id: int = PathParam(ge=1, le=MAX_ID), retry: bool = False,
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
    if not extracting and ((row["text_status"] == "pending" and due) or forced):
        extracting = state.fetcher.start_extraction(article_id, row["url"], retry=forced)
    item = _decorate(dict(row))
    item["extracting"] = extracting
    if extracting:
        item["text_status"] = "pending"
    if item["paywalled"]:
        # Bezahlartikel: keinen Text ausliefern, die Oberfläche zeigt nur Hinweis und Original.
        item.update(body_html=None, body_text=None, summary="", paywalled=1, read_minutes=0)
    return item


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
    marked = state.db.mark_all_read(user["id"], scope, terms or None, body.max_id)
    invalidate_unread(user["id"])
    return {"marked": marked}


# ---- status --------------------------------------------------------------

@app.post("/api/refresh", dependencies=[Depends(admin_user)])
async def refresh():
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

# Das schlanke Docker-Image hat keine /etc/mime.types; Schriften und Manifest sonst als text/plain.
for _ext, _type in ((".woff2", "font/woff2"), (".woff", "font/woff"), (".webmanifest", "application/manifest+json"),
                    (".ico", "image/x-icon"), (".svg", "image/svg+xml"), (".mjs", "text/javascript")):
    mimetypes.add_type(_type, _ext)
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
