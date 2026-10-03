from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402

SOURCES_YAML = """
sources:
  - {id: alpha, name: Alpha, country: DE, lang: de, feed: "https://alpha.example/feed", homepage: "https://alpha.example"}
  - {id: beta,  name: Beta,  country: GB, lang: en, feed: "https://beta.example/feed",  homepage: "https://beta.example"}
  - {id: gamma, name: Gamma, country: FR, lang: fr, feed: "https://gamma.example/feed", homepage: "https://gamma.example", default_enabled: false}
"""


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def ago(**kw) -> str:
    return iso(datetime.now(timezone.utc) - timedelta(**kw))


def make_row(source_id: str, n: int, published_at: str, **extra) -> dict:
    row = {
        "source_id": source_id, "guid": f"{source_id}-{n}", "url": f"https://{source_id}.example/a/{n}",
        "title": f"Titel {n}", "summary": f"Zusammenfassung {n}", "author": None, "image_url": None,
        "published_at": published_at, "fetched_at": ago(seconds=0),
    }
    row.update(extra)
    return row


@pytest.fixture(autouse=True)
def day_began_yesterday(request, monkeypatch):
    """Die Ungelesen-Zähler zählen nur Artikel ab 00:00 Ortszeit. Damit Tests mit Artikeln von
    „vor ein paar Stunden“ nicht von der Uhrzeit des Testlaufs abhängen, beginnt der Tag hier vor
    24 Stunden. Tests der echten Tagesgrenze markieren sich mit @pytest.mark.real_day."""
    if request.node.get_closest_marker("real_day") is None:
        monkeypatch.setattr(Database, "today_start", lambda self, now=None: ago(days=1))


@pytest.fixture(autouse=True)
def fast_login(request, monkeypatch):
    """Die Mindestdauer einer Anmeldung (main.LOGIN_MIN_SECONDS, gegen Zeitmessung) kostete jeden Test
    mit Login eine halbe Sekunde. Tests der Mindestdauer markieren sich mit @pytest.mark.login_timing."""
    main = sys.modules.get("app.main")
    if main is not None and request.node.get_closest_marker("login_timing") is None:
        monkeypatch.setattr(main, "LOGIN_MIN_SECONDS", 0.0)


# Benutzer der db-Fixture (Admin "tester", angelegt beim Öffnen der frischen Datenbank).
UID = 1
ADMIN_PASSWORD = "geheim-und-lang"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "kiosk.db", admin=("tester", ADMIN_PASSWORD))
    assert d.get_user(UID)["username"] == "tester"
    d.ensure_sources([("alpha", True), ("beta", True), ("gamma", False)])
    yield d
    d.close()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path, db_path=tmp_path / "kiosk.db", sources_file=tmp_path / "sources.yaml",
        static_dir=ROOT / "static", password="geheim", secret="s" * 48, fetch_interval=900,
        retention_days=90, extract_concurrency=2, user_agent="test-agent", session_max_age=3600,
        cookie_secure=True, disable_fetcher=True,
    )


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """Umgebung für die App: eigenes Datenverzeichnis, kleiner Katalog, kein Sammler."""
    (tmp_path / "sources.yaml").write_text(SOURCES_YAML, encoding="utf-8")
    monkeypatch.setenv("KIOSK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KIOSK_SOURCES", str(tmp_path / "sources.yaml"))
    monkeypatch.setenv("KIOSK_PASSWORD", "geheim-und-lang")
    monkeypatch.setenv("KIOSK_SECRET", "x" * 48)
    monkeypatch.setenv("KIOSK_DISABLE_FETCHER", "1")
    monkeypatch.delenv("KIOSK_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("KIOSK_TZ", raising=False)
    # KI-Ausgabe: nie ein Schlüssel aus der Umgebung des Entwicklers (kein Netz, Standardwerte)
    for name in ("OPENROUTER_API_KEY", "KIOSK_AI_MODEL", "KIOSK_AI_BASE_URL", "KIOSK_AI_MORNING", "KIOSK_AI_EVENING",
                 "KIOSK_AI_REASONING"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path
