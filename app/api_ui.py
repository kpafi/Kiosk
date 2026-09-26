"""API für die Oberfläche: Profile, Lieblingszeitungen, Leiste, Einstellungen, Zähler.

Alles hier gehört dem angemeldeten Benutzer (Dependency current_user). Dazu Hilfsfunktionen,
die auch die Artikel-Routen in main.py nutzen (angemeldeter Benutzer, Filterkontext aus
Quelle/Land/Profil, Zwischenspeicher der Profil-Zähler).
"""
from __future__ import annotations

import logging
import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints

from . import db as db_mod
from .db import QueryTimeout
from .state import state

log = logging.getLogger("kiosk.api")

MAX_ID = 2**63 - 1
MAX_SOURCE_IDS = 200
UNREAD_CAP = 5000          # Profil-Zähler werden hier gedeckelt
UNREAD_TTL = 60.0          # Sekunden, so lange gilt ein berechneter Profil-Zähler
# Alle Profil-Zähler einer Anfrage (/api/profiles, /api/bar) zusammen höchstens so viele Sekunden;
# was danach noch fehlt, kommt als unread null. Ein Profil, dessen Zählung allein das Zeitlimit
# einer Abfrage (db.READ_TIME_LIMIT) sprengt, wird SLOW_PROFILE_TTL lang gar nicht mehr gezählt (null).
PROFILE_COUNT_BUDGET = 3.0
SLOW_PROFILE_TTL = 600.0
# Höchstens so viele Profile pro Benutzer: Jedes kostet bei /api/profiles und /api/bar eine
# Volltext-Zählung (bis zu 20 Begriffe).
MAX_PROFILES = 50

SETTINGS_DEFAULTS: dict[str, Any] = {
    "theme": "dark", "text_size": "m", "mark_read_on_open": True,
    "show_images": True, "free_only": True, "bar_order": [],
}


# ---- Benutzer (auch für main.py) ---------------------------------------------

def current_user(request: Request) -> dict[str, Any]:
    """Der angemeldete Benutzer; die Middleware in main.py lädt ihn einmal pro Request
    (request.state.user). Fehlt er, 401 (zweite Sicherung neben der Middleware)."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "Nicht angemeldet")
    return user


def admin_user(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if not user["is_admin"]:
        raise HTTPException(403, "Nur für den Admin")
    return user


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    """Das Benutzer-Objekt für die Oberfläche (/api/session, /api/me)."""
    return {"id": user["id"], "username": user["username"], "display_name": user["display_name"],
            "is_admin": bool(user["is_admin"])}


User = Annotated[dict[str, Any], Depends(current_user)]

router = APIRouter(prefix="/api", dependencies=[Depends(current_user)])


# ---- Hilfsfunktionen (auch für main.py) ------------------------------------

def ui_settings(user_id: int) -> dict[str, Any]:
    """Einstellungen mit Standardwerten; unbekannte oder ungültige gespeicherte Werte zählen nicht."""
    stored = state.db.get_settings(user_id)
    out = dict(SETTINGS_DEFAULTS)
    for key, default in SETTINGS_DEFAULTS.items():
        if key in stored and type(stored[key]) is type(default):
            out[key] = stored[key]
    return out


def enabled_sources() -> list[str]:
    """Aktivierte Quellen in Katalog-Reihenfolge."""
    enabled = set(state.db.enabled_source_ids())
    return [s.id for s in state.catalog.all() if s.id in enabled]


def implicit_sources() -> list[str]:
    """„Alle Quellen“, wo keine Quelle ausdrücklich genannt ist (ALLE, Profile ohne Quellenliste,
    Suche ohne Quelle, auch mit Länderfilter): die aktivierten ohne die mit in_all: false in
    sources.yaml. Wird eine Quelle ausdrücklich genannt (?source=, Profil-Liste), zählt sie trotzdem."""
    enabled = set(state.db.enabled_source_ids())
    return [s.id for s in state.catalog.all() if s.id in enabled and s.in_all]


def effective_sources(profile: dict[str, Any]) -> list[str]:
    """Wirksame Quellen eines Profils: seine Liste geschnitten mit den aktivierten; ohne Liste
    implicit_sources()."""
    if profile["sources"] is None:
        return implicit_sources()
    wanted = set(profile["sources"])
    return [s for s in enabled_sources() if s in wanted]


def _intersect(scope: list[str] | None, other: list[str]) -> list[str]:
    if scope is None:
        return list(other)
    keep = set(other)
    return [s for s in scope if s in keep]


def article_scope(user_id: int, sources: list[str], countries: set[str], profile_id: int | None,
                  *, only_enabled: bool = True) -> tuple[list[str] | None, list[str]]:
    """Filterkontext: (Quellen oder None = alle, Profil-Begriffe). Leere Liste = nichts passt.

    Ohne ausdrückliche Quelle und ohne Profil gelten nur implicit_sources() (ALLE, auch mit
    Länderfilter oder Suche); only_enabled=False (Gemerkt) lässt dann alle Quellen zu."""
    scope: list[str] | None = list(sources) if sources else None
    if scope is None and profile_id is None and only_enabled:
        scope = implicit_sources()
    if countries:
        scope = _intersect(scope, [s.id for s in state.catalog.all() if s.country in countries])
    terms: list[str] = []
    if profile_id is not None:
        profile = state.db.get_profile(user_id, profile_id)
        if profile is None:
            raise HTTPException(404, "Profil nicht gefunden")
        scope = _intersect(scope, effective_sources(profile))
        terms = profile["terms"]
    return scope, terms


# (Benutzer-ID, Profil-ID) -> (Zeitpunkt, Ende des letzten Sammellaufs, Tagesbeginn, Zahl);
# Zahl None: Zählung zu teuer (Zeitlimit), gilt SLOW_PROFILE_TTL lang.
_unread_cache: dict[tuple[int, int], tuple[float, Any, str, int | None]] = {}


def invalidate_unread(user_id: int | None = None, *, profile_id: int | None = None) -> None:
    """Profil-Zähler verwerfen: die eines Benutzers (sein Lesestand hat sich geändert) oder alle
    (Quellen umgeschaltet, Start). Die Markierung „zu teuer“ bleibt dabei (am Lesestand liegt es
    nicht), außer für das geänderte Profil `profile_id`."""
    if user_id is None:
        _unread_cache.clear()
        return
    for key in [k for k, v in _unread_cache.items()
                if k[0] == user_id and (v[3] is not None or k[1] == profile_id)]:
        del _unread_cache[key]


class CountBudget:
    """Gemeinsames Zeitbudget der Profil-Zähler einer Anfrage (PROFILE_COUNT_BUDGET Sekunden)."""

    def __init__(self, seconds: float | None = None):
        self.deadline = time.monotonic() + (PROFILE_COUNT_BUDGET if seconds is None else seconds)

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


def profile_unread(user_id: int, profile: dict[str, Any], sources: list[str],
                   budget: CountBudget | None = None) -> int | None:
    """Ungelesene, Paywall-freie Artikel von heute im Profil (höchstens UNREAD_CAP), höchstens alle
    60 s neu berechnet; ein beendeter Sammellauf oder ein neuer Tag macht den Wert sofort ungültig.

    None („nicht gezählt“): Das Zeitbudget der Anfrage ist verbraucht (nicht gespeichert, die nächste
    Anfrage versucht es wieder) oder die Zählung allein überschritt db.READ_TIME_LIMIT (dann
    SLOW_PROFILE_TTL lang kein neuer Versuch)."""
    run = state.fetcher.state.get("last_finished")
    day = state.db.today_start()
    key = (user_id, profile["id"])
    hit = _unread_cache.get(key)
    now = time.monotonic()
    if hit and hit[3] is None and now - hit[0] < SLOW_PROFILE_TTL:
        return None
    if hit and hit[3] is not None and now - hit[0] < UNREAD_TTL and hit[1] == run and hit[2] == day:
        return hit[3]
    budget = budget or CountBudget()
    left = budget.remaining()
    if left <= 0:
        return None
    limit = min(db_mod.READ_TIME_LIMIT, left)
    try:
        n = state.db.count_articles(user_id, cap=UNREAD_CAP, sources=sources, terms=profile["terms"] or None,
                                    unread=True, free_only=True, since=day, time_limit=limit)
    except QueryTimeout:
        if limit < db_mod.READ_TIME_LIMIT:
            return None  # nur das Restbudget war zu knapp: beim nächsten Mal wieder versuchen
        # Zu teures Profil: nicht zählen, statt Profilliste und Leiste scheitern zu lassen.
        log.warning("Zähler für Profil %s (Benutzer %s) abgebrochen: Zeitlimit", profile["id"], user_id)
        _unread_cache[key] = (now, run, day, None)
        return None
    _unread_cache[key] = (now, run, day, n)
    return n


def _profile_view(user_id: int, profile: dict[str, Any], budget: CountBudget | None = None) -> dict[str, Any]:
    sources = effective_sources(profile)
    return {**profile, "source_count": len(sources),
            "unread": profile_unread(user_id, profile, sources, budget)}


def _check_reorder(ids: list, known: set) -> None:
    if len(set(ids)) != len(ids):
        raise HTTPException(400, "Doppelte IDs")
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise HTTPException(400, f"Unbekannte IDs: {', '.join(map(str, unknown[:5]))}")


# ---- Profile ----------------------------------------------------------------

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
Term = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40, pattern=r'^[^"]*$')]


class ProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name
    sources: list[str] | None = Field(None, max_length=MAX_SOURCE_IDS)
    terms: list[Term] = Field(default_factory=list, max_length=20)
    in_bar: StrictBool = False
    in_chips: StrictBool = True


class ProfilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name | None = None
    sources: list[str] | None = Field(None, max_length=MAX_SOURCE_IDS)
    terms: list[Term] | None = Field(None, max_length=20)
    in_bar: StrictBool | None = None
    in_chips: StrictBool | None = None


def _clean_fields(body: BaseModel) -> dict[str, Any]:
    """Nur gesetzte Felder; Quellen müssen bekannt sein, Dubletten fallen weg."""
    fields = {k: getattr(body, k) for k in body.model_fields_set}
    for key in ("name", "terms", "in_bar", "in_chips"):
        if key in fields and fields[key] is None:
            raise HTTPException(422, f"{key} darf nicht null sein")
    if fields.get("sources") == []:
        raise HTTPException(422, "sources darf nicht leer sein (null heißt alle aktiven Zeitungen)")
    if fields.get("sources") is not None:
        unknown = [s for s in fields["sources"] if s not in state.catalog]
        if unknown:
            raise HTTPException(422, f"Unbekannte Quellen: {', '.join(unknown[:5])}")
        fields["sources"] = list(dict.fromkeys(fields["sources"]))
    if "terms" in fields:
        fields["terms"] = list(dict.fromkeys(fields["terms"]))
    return fields


@router.get("/profiles")
async def list_profiles(user: User):
    budget = CountBudget()   # eines für alle Profile der Liste
    return [_profile_view(user["id"], p, budget) for p in state.db.list_profiles(user["id"])]


@router.post("/profiles", status_code=201)
async def create_profile(body: ProfileIn, user: User):
    if len(state.db.list_profiles(user["id"])) >= MAX_PROFILES:
        raise HTTPException(409, f"Höchstens {MAX_PROFILES} Profile möglich, bitte erst eines löschen")
    fields = _clean_fields(body)
    pid = state.db.create_profile(user["id"], name=body.name, sources=fields.get("sources", body.sources),
                                  terms=fields.get("terms", []), in_bar=body.in_bar,
                                  in_chips=body.in_chips)
    invalidate_unread(user["id"])
    return _profile_view(user["id"], state.db.get_profile(user["id"], pid))


class IdOrder(BaseModel):
    ids: list[int] = Field(max_length=1000)


@router.post("/profiles/reorder")
async def reorder_profiles(body: IdOrder, user: User):
    _check_reorder(body.ids, {p["id"] for p in state.db.list_profiles(user["id"])})
    state.db.reorder(user["id"], "profiles", "id", body.ids)
    return await list_profiles(user)


def _profile_or_404(user_id: int, profile_id: int) -> dict[str, Any]:
    profile = state.db.get_profile(user_id, profile_id)
    if profile is None:
        raise HTTPException(404, "Profil nicht gefunden")
    return profile


@router.patch("/profiles/{profile_id}")
async def patch_profile(body: ProfilePatch, user: User, profile_id: int = PathParam(ge=1, le=MAX_ID)):
    _profile_or_404(user["id"], profile_id)
    state.db.update_profile(user["id"], profile_id, _clean_fields(body))
    invalidate_unread(user["id"], profile_id=profile_id)
    return _profile_view(user["id"], state.db.get_profile(user["id"], profile_id))


@router.delete("/profiles/{profile_id}", status_code=204)
async def delete_profile(user: User, profile_id: int = PathParam(ge=1, le=MAX_ID)):
    if _profile_or_404(user["id"], profile_id)["builtin"]:
        raise HTTPException(409, "Vorgegebene Profile lassen sich nicht löschen")
    state.db.delete_profile(user["id"], profile_id)
    invalidate_unread(user["id"], profile_id=profile_id)
    return Response(status_code=204)


# ---- Lieblingszeitungen -----------------------------------------------------

def _favorites_view(user_id: int) -> list[dict[str, Any]]:
    unread = state.db.unread_free_by_source(user_id)
    enabled = set(state.db.enabled_source_ids())
    out = []
    for f in state.db.list_favorites(user_id):
        src = state.catalog.get(f["source_id"])
        if src is None:  # aus dem Katalog entfernt
            continue
        out.append({
            "source_id": src.id, "name": src.name, "short": src.short, "country": src.country,
            "enabled": src.id in enabled, "in_bar": bool(f["in_bar"]), "position": f["position"],
            "unread": unread.get(src.id, 0),
        })
    return out


@router.get("/favorites/sources")
async def list_favorites(user: User):
    return _favorites_view(user["id"])


class FavoriteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    in_bar: StrictBool | None = None


class SourceOrder(BaseModel):
    ids: list[str] = Field(max_length=MAX_SOURCE_IDS)


@router.post("/favorites/sources/reorder")
async def reorder_favorites(body: SourceOrder, user: User):
    _check_reorder(body.ids, {f["source_id"] for f in state.db.list_favorites(user["id"])})
    state.db.reorder(user["id"], "favorite_sources", "source_id", body.ids)
    return _favorites_view(user["id"])


@router.put("/favorites/sources/{source_id}")
async def put_favorite(source_id: str, user: User, body: FavoriteBody = FavoriteBody()):
    if source_id not in state.catalog:
        raise HTTPException(404, "Unbekannte Quelle")
    state.db.put_favorite(user["id"], source_id, body.in_bar)
    return next(f for f in _favorites_view(user["id"]) if f["source_id"] == source_id)


@router.delete("/favorites/sources/{source_id}", status_code=204)
async def delete_favorite(source_id: str, user: User):
    state.db.delete_favorite(user["id"], source_id)
    return Response(status_code=204)


# ---- Leiste -----------------------------------------------------------------

BarKey = Annotated[str, StringConstraints(pattern=r"^(source:[A-Za-z0-9_-]{1,64}|profile:[0-9]{1,18})$")]


def _bar(user_id: int) -> dict[str, Any]:
    unread = state.db.unread_free_by_source(user_id)
    entries: dict[str, dict[str, Any]] = {}
    for f in state.db.list_favorites(user_id):
        src = state.catalog.get(f["source_id"])
        if f["in_bar"] and src is not None:
            entries[f"source:{src.id}"] = {"type": "source", "id": src.id, "key": f"source:{src.id}",
                                           "label": src.short, "unread": unread.get(src.id, 0)}
    budget = CountBudget()   # eines für alle Profile der Leiste
    for p in state.db.list_profiles(user_id):
        if p["in_bar"]:
            entries[f"profile:{p['id']}"] = {"type": "profile", "id": p["id"], "key": f"profile:{p['id']}",
                                             "label": p["name"].upper(),
                                             "unread": profile_unread(user_id, p, effective_sources(p), budget)}
    ordered = [entries.pop(k) for k in ui_settings(user_id)["bar_order"] if k in entries]
    total = state.db.article_counts(user_id, implicit_sources())["unread"]
    return {"items": [{"type": "all", "key": "all", "label": "ALLE", "unread": total},
                      *ordered, *entries.values()]}


@router.get("/bar")
async def get_bar(user: User):
    return _bar(user["id"])


class BarBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order: list[BarKey] = Field(max_length=400)


def _check_bar_keys(user_id: int, keys: list[str]) -> None:
    """Nur Schlüssel bekannter Zeitungen und eigener Profile (sonst 400)."""
    profile_ids = {str(p["id"]) for p in state.db.list_profiles(user_id)}
    unknown = [k for k in keys
               if not ((k.startswith("source:") and k[7:] in state.catalog)
                       or (k.startswith("profile:") and k[8:] in profile_ids))]
    if unknown:
        raise HTTPException(400, f"Unbekannte Einträge: {', '.join(unknown[:5])}")


@router.patch("/bar")
async def patch_bar(body: BarBody, user: User):
    _check_bar_keys(user["id"], body.order)
    state.db.set_settings(user["id"], {"bar_order": list(dict.fromkeys(body.order))})
    return _bar(user["id"])


# ---- Einstellungen ----------------------------------------------------------

class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    theme: Literal["system", "dark", "light"] | None = None
    text_size: Literal["s", "m", "l"] | None = None
    mark_read_on_open: StrictBool | None = None
    show_images: StrictBool | None = None
    free_only: StrictBool | None = None
    bar_order: list[BarKey] | None = Field(None, max_length=400)


@router.get("/settings")
async def get_settings(user: User):
    return ui_settings(user["id"])


@router.patch("/settings")
async def patch_settings(body: SettingsPatch, user: User):
    values = body.model_dump(exclude_unset=True)
    nulls = [k for k, v in values.items() if v is None]
    if nulls:
        raise HTTPException(422, f"{', '.join(nulls)} darf nicht null sein")
    if "bar_order" in values:
        _check_bar_keys(user["id"], values["bar_order"])
        values["bar_order"] = list(dict.fromkeys(values["bar_order"]))
    state.db.set_settings(user["id"], values)
    return ui_settings(user["id"])


# ---- Zähler -----------------------------------------------------------------

@router.get("/counts")
async def counts(user: User):
    states = state.db.source_states()
    catalog_ids = [s.id for s in state.catalog.all()]
    enabled = [sid for sid in catalog_ids if sid in states and states[sid]["enabled"]]
    return {
        # unread: wie ALLE (implicit_sources), ohne Paywall; later/starred: alle gemerkten (wie Gemerkt)
        **state.db.article_counts(user["id"], implicit_sources()),
        "profiles": len(state.db.list_profiles(user["id"])),
        "favorites": sum(1 for f in state.db.list_favorites(user["id"]) if f["source_id"] in state.catalog),
        "sources_enabled": len(enabled),
        "sources_total": len(catalog_ids),
        "sources_failing": sum(1 for sid in enabled if states[sid]["last_error"]),
    }
