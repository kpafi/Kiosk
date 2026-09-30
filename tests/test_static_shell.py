"""Service Worker und index.html verweisen nur auf Dateien, die es unter static/ gibt; dazu feste Stellen
in index.html und app.css."""
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



def test_index_html_has_one_build_placeholder():
    """app.main.index setzt die Build-Kennung in genau dieses leere data-build an <html> ein."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert html.count('data-build=""') == 1 and html.count("data-build") == 1
    assert re.search(r'<html [^>]*data-build=""', html)


def _css_rule(css: str, selector: str) -> str:
    m = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, selector
    return m.group(1)


def test_source_error_wraps_inline_after_country():
    """Zeitungen mit Fehler: „DE · • Fehler: …“ läuft als Text weiter und bricht um; kein Block
    (inline-flex), der in die nächste Zeile rutscht und „DE ·“ allein stehen lässt."""
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert "display" not in _css_rule(css, ".err-inline")
    assert "white-space: nowrap" in _css_rule(css, ".err-inline-label")
    dot = _css_rule(css, ".err-inline-label::before")
    assert "display: inline-block" in dot and "vertical-align: middle" in dot
    js = (STATIC / "js" / "views" / "sources.js").read_text(encoding="utf-8")
    assert "h('span', { class: 'err-inline-label', text: 'Fehler:' }), ` ${s.last_error}`" in js


def test_gap_after_body_figure_matches_hero():
    """Nach einem Bild samt Bildzeile im Text 16px bis zum nächsten Absatz, wie nach dem Titelbild."""
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert "--space-4: 16px" in (STATIC / "css" / "tokens.css").read_text(encoding="utf-8")
    assert "--art-gap: var(--space-4)" in css
    assert "margin-top: var(--space-4)" in _css_rule(css, ".article-body figure + *, .article-body figcaption + *")
    assert "margin-top: var(--space-4)" in _css_rule(css, ".article-body > * + *")
