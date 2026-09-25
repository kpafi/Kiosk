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
