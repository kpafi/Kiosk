"""Globaler Zustand der App (eine Instanz je Prozess), eigenes Modul gegen Import-Zyklen."""
from __future__ import annotations

import asyncio
from datetime import datetime

from .auth import Auth
from .config import Settings
from .db import Database
from .fetcher import Fetcher
from .sources import Catalog


class State:
    settings: Settings
    db: Database
    catalog: Catalog
    fetcher: Fetcher
    auth: Auth
    verify_slots: asyncio.Semaphore     # begrenzt parallele scrypt-Prüfungen beim Login
    task: asyncio.Task | None = None
    watchdog: asyncio.Task | None = None
    started_at: datetime | None = None


state = State()
