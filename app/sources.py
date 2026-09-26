from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    country: str        # ISO 3166-1 alpha-2, "EU" für europaweite Medien
    lang: str           # ISO 639-1 der Artikelsprache
    feed: str
    homepage: str
    category: str = "general"   # general | tech | business
    default_enabled: bool = True
    note: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)
    short: str = ""     # Kurzname für die Leiste, Versalien, höchstens SHORT_MAX Zeichen
    # Nur Einträge, deren URL-Pfad (ohne Query und Fragment, mit ;Parametern) auf mindestens einen
    # dieser regulären Ausdrücke passt (re.search); leer = alle. Gilt für Feed- und Sitemap-Einträge,
    # vor der Obergrenze je Abruf.
    include_paths: tuple[str, ...] = field(default_factory=tuple)
    # Regulärer Ausdruck mit genau einer Gruppe, angewandt (re.search) auf die URL eines Eintrags: die
    # Gruppe ist dann die GUID (z. B. die Artikelnummer), damit ein geänderter Titel im URL-Slug kein
    # Duplikat erzeugt. Passt er nicht, bleibt die GUID wie ohne Angabe.
    guid_pattern: str = ""
    # False: Die Quelle wird geholt und ist in ihrer eigenen Ansicht und in Profilen sichtbar, die sie
    # ausdrücklich nennen, zählt aber nicht zu „alle Quellen“ (ALLE, Profile ohne Quellenliste, Suche
    # ohne Quelle). Regel an einer Stelle: api_ui.implicit_sources().
    in_all: bool = True

    def wants_url(self, url: str) -> bool:
        """True, wenn die Einträge dieser URL angelegt werden sollen (siehe include_paths)."""
        if not self.include_paths:
            return True
        try:
            path = urlsplit(url).path
        except ValueError:
            return False
        return any(re.search(p, path) for p in self.include_paths)

    def guid_for(self, url: str, default: str) -> str:
        """GUID eines Eintrags nach guid_pattern, sonst `default`."""
        m = re.search(self.guid_pattern, url) if self.guid_pattern else None
        return m.group(1) if m and m.group(1) else default


SHORT_MAX = 12
INCLUDE_PATHS_MAX = 20


def _short(sid: str, name: str, raw) -> str:
    """Kurzname aus sources.yaml prüfen; ohne Angabe der Name in Versalien, gekürzt."""
    if raw is None:
        return name.upper()[:SHORT_MAX].rstrip()
    if not isinstance(raw, str) or not raw.strip() or len(raw.strip()) > SHORT_MAX:
        raise ValueError(f"Quelle {sid}: short muss ein Text mit 1 bis {SHORT_MAX} Zeichen sein, ist {raw!r}")
    return raw.strip().upper()


def _include_paths(sid: str, raw) -> tuple[str, ...]:
    """include_paths aus sources.yaml prüfen: Liste gültiger regulärer Ausdrücke (nicht leer)."""
    if raw is None:
        return ()
    if (not isinstance(raw, list) or not raw or len(raw) > INCLUDE_PATHS_MAX
            or not all(isinstance(p, str) and p for p in raw)):
        raise ValueError(f"Quelle {sid}: include_paths muss eine Liste von 1 bis {INCLUDE_PATHS_MAX} "
                         f"regulären Ausdrücken sein, ist {raw!r}")
    for p in raw:
        try:
            re.compile(p)
        except re.error as e:
            raise ValueError(f"Quelle {sid}: include_paths enthält ungültigen Ausdruck {p!r}: {e}") from None
    return tuple(raw)


def _guid_pattern(sid: str, raw) -> str:
    """guid_pattern aus sources.yaml prüfen: gültiger regulärer Ausdruck mit genau einer Gruppe."""
    if raw is None:
        return ""
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"Quelle {sid}: guid_pattern muss ein regulärer Ausdruck sein, ist {raw!r}")
    try:
        groups = re.compile(raw).groups
    except re.error as e:
        raise ValueError(f"Quelle {sid}: guid_pattern ist ungültig ({raw!r}): {e}") from None
    if groups != 1:
        raise ValueError(f"Quelle {sid}: guid_pattern braucht genau eine Gruppe, hat {groups} ({raw!r})")
    return raw


def _in_all(sid: str, raw) -> bool:
    """in_all aus sources.yaml: nur true oder false (kein Text, keine Zahl)."""
    if raw is None:
        return True
    if not isinstance(raw, bool):
        raise ValueError(f"Quelle {sid}: in_all muss true oder false sein, ist {raw!r}")
    return raw


class Catalog:
    def __init__(self, sources: list[Source]):
        self._by_id = {s.id: s for s in sources}
        self._order = [s.id for s in sources]

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        sources: list[Source] = []
        seen: set[str] = set()
        for item in raw.get("sources", []):
            sid = item["id"]
            if sid in seen:
                raise ValueError(f"Doppelte Quellen-ID in sources.yaml: {sid}")
            seen.add(sid)
            country = item.get("country")
            # YAML 1.1 liest ein unquotiertes NO (Norwegen) als False, ~ als None.
            if not isinstance(country, str) or not country.strip():
                raise ValueError(
                    f"Quelle {sid}: country muss ein Text sein (z. B. \"NO\" in Anführungszeichen), "
                    f"ist {country!r}"
                )
            sources.append(
                Source(
                    id=sid,
                    name=item["name"],
                    country=country.strip().upper(),
                    lang=item.get("lang", "en"),
                    feed=item["feed"],
                    homepage=item.get("homepage", ""),
                    category=item.get("category", "general"),
                    default_enabled=bool(item.get("default_enabled", True)),
                    note=item.get("note", ""),
                    tags=tuple(item.get("tags", [])),
                    short=_short(sid, item["name"], item.get("short")),
                    include_paths=_include_paths(sid, item.get("include_paths")),
                    guid_pattern=_guid_pattern(sid, item.get("guid_pattern")),
                    in_all=_in_all(sid, item.get("in_all")),
                )
            )
        return cls(sources)

    def get(self, sid: str) -> Source | None:
        return self._by_id.get(sid)

    def all(self) -> list[Source]:
        return [self._by_id[i] for i in self._order]

    def __contains__(self, sid: str) -> bool:
        return sid in self._by_id
