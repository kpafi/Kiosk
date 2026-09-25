from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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
                )
            )
        return cls(sources)

    def get(self, sid: str) -> Source | None:
        return self._by_id.get(sid)

    def all(self) -> list[Source]:
        return [self._by_id[i] for i in self._order]

    def __contains__(self, sid: str) -> bool:
        return sid in self._by_id
