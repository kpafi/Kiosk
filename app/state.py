"""Globaler Zustand der App (eine Instanz je Prozess), eigenes Modul gegen Import-Zyklen."""
from __future__ import annotations

import asyncio
from datetime import datetime

from .auth import Auth
from .config import Settings
from .db import Database
from .editions import EditionWriter
from .fetcher import Fetcher
from .sources import Catalog


class State:
    settings: Settings
    db: Database
    catalog: Catalog
    fetcher: Fetcher
    editions: EditionWriter
    auth: Auth
    verify_slots: asyncio.Semaphore     # begrenzt parallele scrypt-Prüfungen beim Login
    task: asyncio.Task | None = None
    watchdog: asyncio.Task | None = None
    editions_task: asyncio.Task | None = None   # Zeitplan der KI-Ausgabe (nur mit Schlüssel und Sammler)
    started_at: datetime | None = None


state = State()
