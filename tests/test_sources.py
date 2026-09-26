"""sources.yaml: Typprüfung beim Laden."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.sources import Catalog

ROOT = Path(__file__).resolve().parent.parent


def _load(tmp_path, country_yaml: str) -> Catalog:
    path = tmp_path / "sources.yaml"
    path.write_text(
        "sources:\n  - {id: x, name: X, " + country_yaml + " feed: \"https://x.example/feed\"}\n",
        encoding="utf-8",
    )
    return Catalog.load(path)


@pytest.mark.parametrize("country_yaml", ["country: NO,", "country: ~,", "country: 49,", "country: '',", ""])
def test_country_must_be_text(tmp_path, country_yaml):
    with pytest.raises(ValueError, match="Quelle x: country muss ein Text sein"):
        _load(tmp_path, country_yaml)


def test_quoted_country_is_accepted(tmp_path):
    assert _load(tmp_path, 'country: "no",').get("x").country == "NO"


def test_shipped_catalog_loads():
    catalog = Catalog.load(ROOT / "sources.yaml")
    assert catalog.all() and all(isinstance(s.country, str) and s.country for s in catalog.all())
    assert catalog.get("thelocalno").country == "NO"


def test_short_defaults_to_upper_name_and_is_validated(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text(
        "sources:\n"
        "  - {id: a, name: Sehr langer Zeitungsname, country: DE, feed: \"https://a.example/f\"}\n"
        "  - {id: b, name: B, short: bee, country: DE, feed: \"https://b.example/f\"}\n",
        encoding="utf-8",
    )
    catalog = Catalog.load(path)
    assert catalog.get("a").short == "SEHR LANGER"  # 12 Zeichen, Leerzeichen am Ende weg
    assert catalog.get("b").short == "BEE"
    path.write_text("sources:\n  - {id: c, name: C, short: VIEL-ZU-LANGER-NAME, country: DE, feed: \"x\"}\n",
                    encoding="utf-8")
    with pytest.raises(ValueError, match="Quelle c: short"):
        Catalog.load(path)


def test_shipped_catalog_has_distinct_short_names():
    catalog = Catalog.load(ROOT / "sources.yaml")
    shorts = [s.short for s in catalog.all()]
    assert len(shorts) == 92 and len(set(shorts)) == 92
    assert all(0 < len(x) <= 12 and x == x.upper() for x in shorts)
    assert catalog.get("spiegel").short == "SPIEGEL"
