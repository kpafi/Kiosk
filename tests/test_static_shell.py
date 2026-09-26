"""Service Worker und index.html verweisen nur auf Dateien, die es unter static/ gibt."""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
# Pfade, die nicht 1:1 als Datei unter static/ liegen (eigene Route bzw. Verzeichnis-Index).
SPECIAL = {"/": STATIC / "index.html", "/favicon.ico": STATIC / "icons" / "favicon.ico"}


def _file(path: str) -> Path:
    return SPECIAL.get(path, STATIC / path.lstrip("/"))


def _shell() -> list[str]:
    src = (STATIC / "sw.js").read_text(encoding="utf-8")
    block = re.search(r"const SHELL = \[(.*?)\];", src, re.S)
    assert block, "SHELL-Liste in sw.js nicht gefunden"
    return re.findall(r"'([^']+)'", block.group(1))


def test_sw_shell_paths_exist():
    paths = _shell()
    assert len(paths) > 10
    missing = [p for p in paths if not _file(p).is_file()]
    assert not missing, missing


def test_sw_shell_lists_every_js_module():
    listed = set(_shell())
    modules = {"/" + p.relative_to(STATIC).as_posix() for p in (STATIC / "js").rglob("*.js")}
    assert not modules - listed, sorted(modules - listed)


def test_index_html_references_exist():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r'<(?:script|link)\b[^>]*?(?:src|href)="([^"]+)"', html)
    assert len(refs) >= 5
    missing = [r for r in refs if r.startswith("/") and not _file(r).is_file()]
    assert not missing, missing

