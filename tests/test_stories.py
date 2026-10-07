"""Geschichten (app/stories.py): Qualität auf dem Goldset, Textverarbeitung, Clustering-Regeln,
Lauf mit Datenbank, Migration auf Schema 8 und der Aufruf aus dem Sammler."""
from __future__ import annotations

import gzip
import itertools
import json
import logging
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

import app.stories as stories
from app.db import SCHEMA_VERSION, Database
from app.fetcher import Fetcher
from app.sources import Catalog, Source
from app.stories import Doc, Model, Tokenizer, cluster_rows, normalize, words
from conftest import ago, iso, make_row

FIXTURES = Path(__file__).resolve().parent / "fixtures"
T0 = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


def at(hours: float) -> str:
    return iso(T0 + timedelta(hours=hours))


def row(i: int, source: str, lang: str, title: str, summary: str = "", hours: float = 0, **extra) -> dict:
    return {"id": i, "source_id": source, "lang": lang, "title": title, "summary": summary,
            "published_at": at(hours), **extra}


def canon(word: str, lang: str) -> list[str]:
    return [t for t, _ in Tokenizer().canon(normalize(word), lang)]


# ---- Qualität: Tuning- und Held-out-Fenster ------------------------------------------
#
# Zwei ganze Fenster echter Feed-Einträge, zeitlich getrennt (Titel, Anreißer bis 400 Zeichen):
#   stories_tune.json.gz     26.09. 06:00 bis 27.09. 17:16, 2180 Artikel, 90 Quellen. Hieran wurden
#                            Lexikon und Schwellen eingestellt. Beschriftet: 46 Geschichten von Hand,
#                            harte Ablenker (gleiche Person oder Familie, anderes Ereignis:
#                            Klopp-Porträts, Asienspiele-Gold in anderen Sportarten, zweiter
#                            Messerangriff, andere Filmpreise ...) und jedes Mitglied der 30 größten
#                            gefundenen Geschichten.
#   stories_heldout.json.gz  24.09. 06:00 bis 26.09. 06:00, 856 Artikel (dünneres, meist englisches
#                            Fenster). Nie zum Einstellen benutzt: beschriftet einmal mit dem
#                            eingefrorenen Verfahren (jedes Mitglied jeder gefundenen Geschichte plus
#                            per Suche gefundene fehlende Mitglieder und Ausreißer).
#   stories_c3new.json.gz    Schnappschuss des dritten Reviews (48 h bis 27.09. 20:05 UTC, 2300 Artikel),
#                            neu: 110 Artikel nach 27.09. 19:01:06 UTC, genauso beschriftet (29 Geschichten).
#   stories_c2new.json.gz    Schnappschuss eines zweiten Laufs (Review, 48 h bis 27.09. 19:01:05 UTC,
#                            2307 Artikel, Volltext-Anfang bei kurzem Anreißer). „Neu“ (new) sind die
#                            182 Artikel nach 27.09. 17:16:47 UTC, die das Tuning-Fenster nicht kennt.
#                            Beschriftet von Hand: jedes Mitglied jeder Geschichte mit einem neuen
#                            Artikel (29 Geschichten) und jeder neue Einzelartikel (solo:<id>, sonst das
#                            Ereignis). Gemessen wird die Reinheit je Geschichte (Anteil der Geschichten,
#                            deren Mitglieder alle dasselbe Ereignis tragen), alle Größen.
# Precision über alle gefundenen Paare beschrifteter Artikel, Recall über Goldpaare verschiedener
# Zeitungen (dieselbe Zeitung zweimal ist keine Geschichte), Reinheit = Anteil des häufigsten
# Ereignisses in den 30 größten Geschichten (unbeschriftete Mitglieder zählen als falsch).

def _load(name: str) -> list[dict]:
    # Die Goldsets enthalten Überschriften und Anreißer der Verlage und liegen nur lokal, nicht auf GitHub.
    path = FIXTURES / f"stories_{name}.json.gz"
    if not path.exists():
        pytest.skip(f"Goldset {path.name} fehlt (nur lokal vorhanden)")
    data = json.loads(gzip.open(path, "rt", encoding="utf-8").read())
    return [{**d, "source_id": d["source"]} for d in data]


def _quality(rows: list[dict]) -> dict[str, float]:
    pred = cluster_rows(rows)
    lab = {r["id"]: r["label"] for r in rows if r["label"]}
    info = {r["id"]: r for r in rows}
    tp = fp = rtp = rfn = xtp = xfn = 0
    for a, b in itertools.combinations(sorted(lab), 2):
        same = lab[a] == lab[b] and not lab[a].startswith("solo:")
        hit = pred.get(a) is not None and pred.get(a) == pred.get(b)
        if hit:
            tp, fp = tp + same, fp + (not same)
        if same and stories.publisher(info[a]["source_id"]) != stories.publisher(info[b]["source_id"]):
            rtp, rfn = rtp + hit, rfn + (not hit)
            if info[a]["lang"] != info[b]["lang"]:
                xtp, xfn = xtp + hit, xfn + (not hit)
    groups: dict[int, list[int]] = {}
    for a, sid in pred.items():
        if sid is not None:
            groups.setdefault(sid, []).append(a)
    top = sorted(groups.values(), key=lambda g: (-len({stories.publisher(info[i]["source_id"]) for i in g}), min(g)))[:30]
    pure = sum(Counter(lab.get(i, f"?{i}") for i in g).most_common(1)[0][1] for g in top)
    return {"precision": tp / max(tp + fp, 1), "recall": rtp / max(rtp + rfn, 1),
            "deen_recall": xtp / max(xtp + xfn, 1), "purity": pure / max(sum(map(len, top)), 1)}


def test_tuning_window_quality():
    """Erreicht: Precision 0.998, Recall 0.759, de/en-Recall 0.765 (1687 Paare), Reinheit 0.973 (nach
    dem vierten Review; die strengeren Regeln kosten Recall, nicht Precision)."""
    q = _quality(_load("tune"))
    assert q["precision"] >= 0.97, q
    assert q["purity"] >= 0.95, q
    assert q["recall"] >= 0.73, q
    assert q["deen_recall"] >= 0.73, q


def test_heldout_window_quality():
    """Erreicht beim ersten Blick mit dem eingefrorenen Verfahren: Precision 0.870, Reinheit 0.943.
    Danach kamen drei allgemeine Regeln dazu (Inhaltswörter neben Namen bei englischen Paaren,
    Ereignisarten auf Gruppenebene, Medaillen im Titel); jetzt Precision 0.901, Recall 0.477,
    de/en-Recall 0.667 (nur 15 Paare), Reinheit 0.955. Die restlichen Fehler: ein Religionstourismus-
    Stück beim Papstbesuch, drei verschiedene Meldungen zu Metas Muse, ein anderes Anthropic-Thema.
    Der niedrige Recall kommt vor allem von der Berichterstattung zum Xi-Trump-Gipfel, die in
    mehrere kleine Geschichten zerfällt oder einzeln bleibt. Nach dem dritten Review (strengere
    Regeln für Namen, Kommentare, deutsch/englische Paare): Precision 1.0, Recall 0.423, Reinheit 0.955."""
    q = _quality(_load("heldout"))
    assert q["precision"] >= 0.9, q
    assert q["purity"] >= 0.92, q
    assert q["recall"] >= 0.38, q


def _clean_share(rows: list[dict], pred: dict[int, int | None]) -> tuple[float, list[list[int]]]:
    """Anteil der Geschichten mit einem neuen Artikel, deren Mitglieder alle beschriftet sind und
    dasselbe Ereignis tragen, und die übrigen (zum Nachlesen)."""
    info = {r["id"]: r for r in rows}
    groups: dict[int, list[int]] = {}
    for a, sid in pred.items():
        if sid is not None:
            groups.setdefault(sid, []).append(a)
    sel = [g for g in groups.values() if any(info[i]["new"] for i in g)]
    bad = [g for g in sel if any(info[i]["label"] is None for i in g) or len({info[i]["label"] for i in g}) > 1]
    return (len(sel) - len(bad)) / max(len(sel), 1), bad


def test_unseen_articles_cluster_purity():
    """Auf Artikeln, die es beim Einstellen noch nicht gab (Stand viertes Review: 27 von 28, Paare
    Precision 0.997, Recall 0.66): 29 von 30 Geschichten rein (0.967; die
    eine: ein SRF-Stück über den Sturm an der US-Ostküste, das im Titel Hurrikan „Nolo“ nennt, bei den
    Meldungen zu Nolo vor Hawaii). Paare mit einem neuen Artikel: Precision 0.997, Recall 0.72.
    Nach dem ersten Lesen geändert (danach nicht mehr unberührt): „far right“/„rechtsextrem“ als ein
    Wort (ein Überblick zur extremen Rechten, Sellners Institut und ein Protest in Portugal)."""
    rows = _load("c2new")
    pred = cluster_rows(rows)
    share, bad = _clean_share(rows, pred)
    assert share >= 0.95, bad
    lab = {r["id"]: r["label"] for r in rows if r["label"]}
    new = {r["id"] for r in rows if r["new"]}
    tp = fp = 0
    for a, b in itertools.combinations(sorted(lab), 2):
        if (a in new or b in new) and pred.get(a) is not None and pred.get(a) == pred.get(b):
            tp, fp = tp + (lab[a] == lab[b]), fp + (lab[a] != lab[b])
    assert tp >= 300 and tp / (tp + fp) >= 0.97, (tp, fp)


def _inject(extra: list[dict]) -> dict[int, int | None]:
    """Clustert eigene Artikel zusammen mit dem ganzen Tuning-Fenster (realistische IDF)."""
    base = _load("tune")
    when = max(r["published_at"] for r in base)
    return cluster_rows(base + [{**r, "published_at": r.get("published_at") or when} for r in extra])


def _art(i: int, source: str, lang: str, title: str, summary: str) -> dict:
    return {"id": 100_000 + i, "source_id": source, "lang": lang, "title": title, "summary": summary,
            "published_at": None, "label": None}


ADVERSARIAL_APART = {
    "zwei Beben, gleiche Opferzahl": [
        _art(1, "bbc", "en", "Peru earthquake: 12 killed as buildings collapse in Arequipa",
             "A magnitude 6.2 quake struck southern Peru, officials said, with rescue teams searching rubble."),
        _art(2, "guardian", "en", "At least 12 dead after strong quake rocks Mindanao",
             "The magnitude 6.0 earthquake damaged homes across the southern Philippine island, disaster officials said.")],
    "zwei Messerangriffe": [
        _art(3, "spiegel", "de", "Messerangriff in Hamburg: Zwei Verletzte, Täter festgenommen",
             "In Hamburg-Altona hat ein Mann zwei Menschen mit einem Messer verletzt. Die Polizei nahm den Täter fest."),
        _art(4, "faz", "de", "Messerangriff in Leipzig: Zwei Verletzte, Täter festgenommen",
             "In Leipzig hat ein Mann zwei Menschen mit einem Messer verletzt. Die Polizei nahm den Täter fest.")],
    "zwei Streiks": [
        _art(5, "spiegel", "de", "Streik bei der Lufthansa: Tausende Flüge fallen aus",
             "Verdi hat das Bodenpersonal der Lufthansa zum Warnstreik aufgerufen. Tausende Passagiere sind betroffen."),
        _art(6, "zeit", "de", "Streik bei der Deutschen Bahn: Tausende Züge fallen aus",
             "Die GDL hat die Lokführer zum Warnstreik aufgerufen. Tausende Fahrgäste sind betroffen.")],
    "zwei Wahlen": [
        _art(7, "bbc", "en", "Conservatives win election in Norway",
             "Norway's Conservatives won the parliamentary election, official results showed on Sunday."),
        _art(8, "guardian", "en", "Conservatives win election in Portugal",
             "Portugal's Conservatives won the parliamentary election, official results showed on Sunday.")],
    "gleiche Zahl, anderes Unglück": [
        _art(9, "bbc", "en", "Ferry capsizes in Congo, 27 missing",
             "A ferry capsized on Lake Kivu, 27 passengers are missing, rescuers said."),
        _art(10, "guardian", "en", "Mine collapse in Ghana leaves 27 missing",
             "A gold mine collapsed in western Ghana, 27 miners are missing, rescuers said.")],
    "zwei Bundesliga-Spiele": [
        _art(11, "spiegel", "de", "Bundesliga: Leverkusen gewinnt gegen Mainz",
             "Bayer Leverkusen hat am Samstag in der Bundesliga gegen Mainz 05 gewonnen. Wirtz traf doppelt."),
        _art(12, "faz", "de", "Bundesliga: Stuttgart gewinnt gegen Freiburg",
             "Der VfB Stuttgart hat am Samstag in der Bundesliga gegen den SC Freiburg gewonnen. Undav traf doppelt.")],
}


@pytest.mark.parametrize("case", sorted(ADVERSARIAL_APART))
def test_same_kind_different_events_stay_apart(case):
    a, b = ADVERSARIAL_APART[case]
    result = _inject([a, b])
    assert result[a["id"]] is None or result[a["id"]] != result[b["id"]], case


def test_freshest_window_cluster_purity():
    """stories_c3new (Review hot3, 48 h bis 27.09. 20:05 UTC): neu sind 110 Artikel nach 19:01:06 UTC.
    Vor der vierten Runde maß das Review 21 von 24 reinen Geschichten (Flucht aus dem Jemen beim
    saudischen Luftangriff, „sorgt für Wirbel“ in Italien, eine dritte); danach beschriftet und die
    Regeln daran geprüft, also nicht mehr unberührt: 22 von 22 rein, Paare Precision 1.0, Recall 0.74."""
    rows = _load("c3new")
    pred = cluster_rows(rows)
    share, bad = _clean_share(rows, pred)
    assert share >= 0.95, bad
    lab = {r["id"]: r["label"] for r in rows if r["label"]}
    new = {r["id"] for r in rows if r["new"]}
    tp = fp = 0
    for a, b in itertools.combinations(sorted(lab), 2):
        if (a in new or b in new) and pred.get(a) is not None and pred.get(a) == pred.get(b):
            tp, fp = tp + (lab[a] == lab[b]), fp + (lab[a] != lab[b])
    assert tp >= 250 and tp / (tp + fp) >= 0.97, (tp, fp)


def _hot(i: int, source: str, lang: str, title: str, summary: str = "", hours: float = 3) -> dict:
    return {"id": 200_000 + i, "source_id": source, "lang": lang, "title": title, "summary": summary,
            "hours": hours, "label": None, "new": True}


# Aus dem zweiten Review (hot): Paare, die in eine Geschichte gehören oder gerade nicht, eingespielt
# in das ganze Fenster stories_c2new (realistische IDF). „hub“: ein Newsletter oder Liveblog, der
# mehrere Geschichten streift, verbindet sie nicht.
HOT_CASES = [
    ("apart", "zwei Konzerne streichen Stellen", [
        _hot(1, "spiegel", "de", "Siemens streicht 5000 Stellen in Deutschland",
             "Der Münchner Konzern Siemens will in seiner Industriesparte 5000 Arbeitsplätze abbauen. Die IG Metall kündigt Widerstand an."),
        _hot(2, "faz", "de", "BASF streicht 2000 Stellen in Ludwigshafen",
             "Der Chemiekonzern BASF will am Stammsitz Ludwigshafen 2000 Arbeitsplätze abbauen. Die IG BCE kündigt Widerstand an.")]),
    ("apart", "zwei Telefone mit KI", [
        _hot(3, "bbc", "en", "Apple unveils new iPhone with AI features",
             "Apple unveiled its latest iPhone on Tuesday, promising new artificial intelligence features and a longer battery life."),
        _hot(4, "guardian", "en", "Samsung unveils new Galaxy phone with AI features",
             "Samsung unveiled its latest Galaxy phone on Tuesday, promising new artificial intelligence features and a longer battery life.")]),
    ("apart", "Bayern, zwei Spiele 31 Stunden auseinander", [
        _hot(5, "spiegel", "de", "Bundesliga: Bayern schlägt Dortmund 3:0 – Kane trifft doppelt",
             "Der FC Bayern München hat den Klassiker gegen Borussia Dortmund klar gewonnen. Harry Kane traf zweimal.", 33),
        _hot(6, "faz", "de", "Champions League: Bayern verliert 1:2 bei Paris Saint-Germain – Kane trifft",
             "Der FC Bayern München hat in der Champions League bei PSG verloren. Harry Kane traf zum zwischenzeitlichen Ausgleich.", 2)]),
    ("apart", "zwei Referenden", [
        _hot(7, "bbc", "en", "Italian voters reject judicial reform in referendum",
             "Italians rejected Prime Minister Giorgia Meloni's overhaul of the judiciary in a referendum on Sunday, official results showed."),
        _hot(8, "guardian", "en", "Irish voters reject housing amendment in referendum",
             "Irish voters rejected a constitutional amendment on housing in a referendum on Sunday, official results showed.")]),
    ("apart", "Merz, zwei Aussagen", [
        _hot(9, "spiegel", "de", "Merz fordert mehr Tempo bei der Rentenreform",
             "Bundeskanzler Friedrich Merz hat die Koalition zu mehr Tempo bei der Rentenreform aufgefordert. Die SPD reagierte zurückhaltend."),
        _hot(10, "welt", "de", "Merz warnt Russland vor weiterer Eskalation",
             "Bundeskanzler Friedrich Merz hat Russland vor einer weiteren Eskalation gewarnt. Die Drohnenüberflüge seien eine Provokation.")]),
    ("apart", "Trump, zwei Drohungen", [
        _hot(11, "bbc", "en", "Trump threatens new tariffs on Canadian lumber",
             "President Donald Trump said on Sunday he would impose new tariffs on Canadian lumber imports, escalating a trade dispute with Ottawa."),
        _hot(12, "guardian", "en", "Trump threatens to send National Guard to Chicago",
             "President Donald Trump said on Sunday he would send National Guard troops to Chicago, escalating a dispute with Illinois officials.")]),
    ("apart", "zwei Kleinflugzeuge", [
        _hot(13, "spiegel", "de", "Kleinflugzeug stürzt in Oberbayern ab – zwei Tote",
             "In der Nähe von Rosenheim ist ein Kleinflugzeug abgestürzt. Beide Insassen kamen ums Leben, teilte die Polizei mit."),
        _hot(14, "zeit", "de", "Kleinflugzeug stürzt im Emsland ab – zwei Tote",
             "In der Nähe von Meppen ist ein Kleinflugzeug abgestürzt. Beide Insassen kamen ums Leben, teilte die Polizei mit.")]),
    ("apart", "zwei Überschwemmungen, gleiche Opferzahl", [
        _hot(15, "bbc", "en", "Floods in Pakistan kill 20 as monsoon rains lash Punjab",
             "At least 20 people were killed as floods swept through villages in Punjab province, disaster officials said."),
        _hot(16, "guardian", "en", "Floods in Nigeria kill 20 as heavy rains lash Niger state",
             "At least 20 people were killed as floods swept through villages in Niger state, disaster officials said.")]),
    ("apart", "zwei Schießereien in den USA", [
        _hot(17, "bbc", "en", "Three killed in shooting at Texas shopping mall",
             "Three people were killed and several injured when a gunman opened fire at a shopping mall in El Paso, police said."),
        _hot(18, "guardian", "en", "Three killed in shooting at Ohio high school",
             "Three people were killed and several injured when a gunman opened fire at a high school in Columbus, police said.")]),
    ("apart", "Tesla, zwei Meldungen", [
        _hot(19, "bbc", "en", "Tesla recalls two million cars over autopilot flaw",
             "Tesla is recalling more than two million vehicles in the United States to fix a flaw in its Autopilot system, regulators said."),
        _hot(20, "guardian", "en", "Tesla quarterly deliveries fall as competition from BYD grows",
             "Tesla delivered fewer vehicles in the third quarter than a year earlier, as Chinese rival BYD gained market share.")]),
    ("apart", "Lawine und Hochwasser in Tirol", [
        _hot(21, "spiegel", "de", "Lawine in den Alpen: Drei Bergsteiger in Tirol tot",
             "Im Tiroler Ötztal sind drei Bergsteiger von einer Lawine verschüttet worden. Die Bergrettung konnte sie nur noch tot bergen."),
        _hot(22, "zeit", "de", "Hochwasser in Tirol: Mehrere Orte überflutet",
             "Nach tagelangem Starkregen sind im Tiroler Oberland mehrere Orte überflutet worden. Die Bergrettung evakuierte Dutzende Menschen.")]),
    ("apart", "zwei Großbrände in zwei Städten", [
        _hot(23, "spiegel", "de", "Großbrand in Berliner Lagerhalle – Feuerwehr im Großeinsatz",
             "In Berlin-Spandau ist eine Lagerhalle in Brand geraten. Die Feuerwehr war mit 150 Kräften im Einsatz, Anwohner sollen Fenster geschlossen halten."),
        _hot(24, "zeit", "de", "Großbrand in Hamburger Lagerhalle – Feuerwehr im Großeinsatz",
             "In Hamburg-Billbrook ist eine Lagerhalle in Brand geraten. Die Feuerwehr war mit 150 Kräften im Einsatz, Anwohner sollen Fenster geschlossen halten.")]),
    ("apart", "zwei Urteile gegen Trump", [
        _hot(25, "bbc", "en", "Court blocks Trump's tariffs on Chinese goods",
             "A federal appeals court ruled that President Donald Trump exceeded his authority in imposing tariffs on Chinese imports."),
        _hot(26, "guardian", "en", "Court blocks Trump's deployment of troops to Portland",
             "A federal appeals court ruled that President Donald Trump exceeded his authority in deploying troops to Portland.")]),
    ("together", "Toyota-Rückruf deutsch/englisch", [
        _hot(27, "spiegel", "de", "Toyota ruft eine Million Autos wegen Airbag-Fehler zurück",
             "Der japanische Autobauer Toyota ruft weltweit rund eine Million Fahrzeuge zurück. Grund sind fehlerhafte Airbags."),
        _hot(28, "bbc", "en", "Toyota recalls one million cars over faulty airbags",
             "Japanese carmaker Toyota is recalling about one million vehicles worldwide because of faulty airbags.")]),
    ("together", "Beben vor Hokkaido deutsch/englisch", [
        _hot(29, "tagesschau", "de", "Erdbeben der Stärke 7,1 vor Hokkaido – Tsunami-Warnung",
             "Vor der japanischen Insel Hokkaido hat sich ein schweres Erdbeben ereignet. Die Behörden gaben eine Tsunami-Warnung aus."),
        _hot(30, "guardian", "en", "Magnitude 7.1 earthquake strikes off Hokkaido, tsunami warning issued",
             "A powerful earthquake struck off Japan's northern island of Hokkaido, prompting a tsunami warning, authorities said.")]),
    ("together", "„fires“ im Sport (Chelsea)", [
        _hot(31, "bbc", "en", "Palmer fires Chelsea past Arsenal in London derby",
             "Cole Palmer scored twice as Chelsea beat Arsenal 2-1 at Stamford Bridge."),
        _hot(32, "guardian", "en", "Chelsea beat Arsenal 2-1 as Palmer scores twice",
             "Cole Palmer struck twice as Chelsea beat Arsenal in the London derby at Stamford Bridge.")]),
    ("together", "Agenturmeldung in drei Zeitungen", [
        _hot(33, "zeit", "de", "Bremen: Brand in Lagerhalle – hoher Sachschaden",
             "In Bremen-Hemelingen ist in der Nacht eine Lagerhalle abgebrannt. Verletzt wurde niemand, der Schaden geht in die Millionen."),
        _hot(34, "handelsblatt", "de", "Bremen: Brand in Lagerhalle – hoher Sachschaden",
             "In Bremen-Hemelingen ist in der Nacht eine Lagerhalle abgebrannt. Verletzt wurde niemand, der Schaden geht in die Millionen."),
        _hot(35, "tagesspiegel", "de", "Bremen: Brand in Lagerhalle – hoher Sachschaden",
             "In Bremen-Hemelingen ist in der Nacht eine Lagerhalle abgebrannt. Verletzt wurde niemand, der Schaden geht in die Millionen.")]),
    ("hub", "Newsletter", [
        _hot(36, "zeit", "de", "Der Morgen: Schweiz stimmt gegen Neutralitätsinitiative, Papst in Lourdes, Vučić tritt zurück",
             "Die Schweizer lehnen die Neutralitätsinitiative ab. Papst Leo XIV. feiert in Lourdes eine Messe. Serbiens Präsident Vučić tritt zurück.")]),
    ("hub", "Liveblog", [
        _hot(37, "independent", "en", "Politics live: Burnham unveils social care plan; RAF Fairford terror arrests; Pope in Lourdes",
             "Andy Burnham sets out his social care reform at Labour conference, as five men are arrested near RAF Fairford and Pope Leo visits Lourdes.")]),
]


def _hot3(i: int, source: str, lang: str, title: str, summary: str = "", hours: float = 3) -> dict:
    return {"id": 300_000 + i, "source_id": source, "lang": lang, "title": title, "summary": summary,
            "hours": hours, "label": None, "new": True}


# Aus dem dritten Review (hot3), eingespielt in das Fenster stories_c3new. Nicht dabei: zwei Aussagen
# von der Leyens nach demselben EU-Gipfel (Grenzfall) und zwei Berliner Demonstrationen an zwei Tagen
# (verschiedener Anlass, sonst fast derselbe Text: bleibt eine bekannte Schwäche).
HOT3_CASES = [
    ("apart", "Bayern: Spielbericht und Vertragsverlängerung", [
        _hot3(3, "spiegel", "de", "Bundesliga: Bayern schlägt Freiburg 3:1 – Musiala trifft doppelt",
              "Der FC Bayern München hat beim SC Freiburg mit 3:1 gewonnen. Jamal Musiala traf zweimal, Harry Kane per Elfmeter."),
        _hot3(4, "faz", "de", "FC Bayern: Musiala verlängert Vertrag bis 2031",
              "Jamal Musiala bleibt langfristig beim FC Bayern München. Der Nationalspieler verlängerte seinen Vertrag vorzeitig bis 2031, teilte der Verein mit.")]),
    ("apart", "zwei BGH-Urteile", [
        _hot3(5, "tagesschau", "de", "Bundesgerichtshof kippt Klausel zu Schönheitsreparaturen in Mietverträgen",
              "Der Bundesgerichtshof in Karlsruhe hat eine gängige Klausel zu Schönheitsreparaturen für unwirksam erklärt. Mieter müssen bei Auszug nicht mehr streichen."),
        _hot3(6, "zeit", "de", "Bundesgerichtshof: Meta muss Nutzer nach Datenleck entschädigen",
              "Der Bundesgerichtshof in Karlsruhe hat entschieden, dass Meta Facebook-Nutzern nach einem Datenleck Schadenersatz zahlen muss. Betroffen sind Millionen Konten.")]),
    ("apart", "zwei Stürme in zwei Ländern", [
        _hot3(7, "bbc", "en", "Storm leaves thousands without power in northern England",
              "High winds and heavy rain have left thousands of homes without power across Cumbria and Northumberland, with trains cancelled and roads closed."),
        _hot3(8, "euronews", "en", "Storm leaves thousands without power in southern France",
              "High winds and heavy rain have left thousands of homes without power across Var and Alpes-Maritimes, with trains cancelled and roads closed.")]),
    ("apart", "Fed und EZB", [
        _hot3(9, "bbc", "en", "Federal Reserve holds interest rates steady as inflation lingers",
              "The US Federal Reserve kept its benchmark interest rate unchanged on Wednesday, with chair Jerome Powell saying inflation remained too high."),
        _hot3(10, "spiegel", "de", "EZB lässt Leitzins unverändert – Lagarde warnt vor Inflation",
              "Die Europäische Zentralbank hat den Leitzins nicht angetastet. EZB-Präsidentin Christine Lagarde sagte, die Inflation sei weiterhin zu hoch.")]),
    ("apart", "Nachruf und frühere Ehrung", [
        _hot3(11, "spiegel", "de", "Regisseur Anton Halbach im Alter von 84 Jahren gestorben",
              "Der Filmregisseur Anton Halbach ist tot. Er starb am Samstag in München, wie seine Familie mitteilte. Halbach prägte das Neue Deutsche Kino.", 2),
        _hot3(12, "faz", "de", "Anton Halbach erhält Ehrenpreis der Deutschen Filmakademie",
              "Der Regisseur Anton Halbach wird für sein Lebenswerk ausgezeichnet. Die Deutsche Filmakademie verleiht ihm den Ehrenpreis im Frühjahr in Berlin.", 30)]),
    ("apart", "zwei Bundestagsbeschlüsse", [
        _hot3(15, "zeit", "de", "Bundestag beschließt neues Wehrdienstgesetz",
              "Der Bundestag hat das neue Wehrdienstgesetz beschlossen. Junge Männer müssen künftig einen Fragebogen ausfüllen, die Musterung wird wieder Pflicht."),
        _hot3(16, "welt", "de", "Bundestag beschließt Reform des Heizungsgesetzes",
              "Der Bundestag hat die Reform des Heizungsgesetzes beschlossen. Die Pflicht zu 65 Prozent erneuerbarer Energie beim Heizungstausch fällt weg.")]),
    ("apart", "zwei Reden vor der UN-Vollversammlung", [
        _hot3(17, "guardian", "en", "Zelensky tells UN general assembly Russia must pay for the war",
              "Ukraine's president Volodymyr Zelensky told the UN general assembly in New York that Russia must be held accountable and pay reparations."),
        _hot3(18, "tagesschau", "de", "Lula fordert vor UN-Generalversammlung Reform des Sicherheitsrats",
              "Brasiliens Präsident Luiz Inácio Lula da Silva hat vor der UN-Generalversammlung in New York eine Reform des Sicherheitsrats gefordert.")]),
    ("apart", "zwei Wahlen", [
        _hot3(19, "spiegel", "de", "Wahl in Norwegen: Sozialdemokraten bleiben stärkste Kraft",
              "Bei der Parlamentswahl in Norwegen haben die Sozialdemokraten von Ministerpräsident Jonas Gahr Støre gewonnen. Die rechte Fortschrittspartei legt deutlich zu."),
        _hot3(20, "bbc", "en", "Czech election: Babiš's party wins most votes",
              "Andrej Babiš's populist ANO party has won the most votes in the Czech parliamentary election, according to official results, and he is set to form a government.")]),
    ("together", "Fed deutsch/englisch", [
        _hot3(21, "tagesschau", "de", "US-Notenbank Fed lässt Leitzins unverändert",
              "Die US-Notenbank Federal Reserve hat den Leitzins nicht verändert. Fed-Chef Jerome Powell sagte, die Inflation sei weiterhin zu hoch."),
        _hot3(22, "guardian", "en", "Fed leaves interest rates unchanged as Powell warns on inflation",
              "The Federal Reserve left interest rates unchanged on Wednesday, with Jerome Powell warning that inflation remained too high.")]),
    ("together", "Nachruf in zwei Zeitungen", [
        _hot3(23, "zeit", "de", "Anton Halbach ist tot: Der Regisseur starb mit 84 Jahren",
              "Der Regisseur Anton Halbach ist im Alter von 84 Jahren in München gestorben. Das teilte seine Familie am Samstag mit.", 1),
        _hot3(111, "spiegel", "de", "Regisseur Anton Halbach im Alter von 84 Jahren gestorben",
              "Der Filmregisseur Anton Halbach ist tot. Er starb am Samstag in München, wie seine Familie mitteilte. Halbach prägte das Neue Deutsche Kino.", 2)]),
]


def _inject_window(name: str, cases: list) -> tuple[list[dict], dict[int, int | None]]:
    base = _load(name)
    newest = max(datetime.fromisoformat(r["published_at"]) for r in base)
    extra = [{**r, "published_at": iso(newest - timedelta(hours=r["hours"]))} for _, _, ex in cases for r in ex]
    rows = base + extra
    return rows, cluster_rows(rows)


@pytest.fixture(scope="module")
def hot_result():
    return _inject_window("c2new", HOT_CASES)


@pytest.fixture(scope="module")
def hot3_result():
    return _inject_window("c3new", HOT3_CASES)


def _check_case(case, rows, result) -> None:
    want, name, extra = case
    ids = {r["id"] for r in extra}
    sids = [result[r["id"]] for r in extra]
    if want == "together":
        assert sids[0] is not None and len(set(sids)) == 1, name
    elif want == "apart":
        assert sids[0] is None or len(set(sids)) > 1, name
    # Keiner der eingespielten Artikel hängt sich an eine echte Geschichte des Fensters (die erfundenen
    # Ereignisse gibt es dort nicht): „Merz warnt Russland“ nicht an Kommentare über Merz, Chelsea gegen
    # Arsenal der Männer nicht an das Derby der Frauen, der Liveblog nicht an Burnhams Pflegereform.
    real = {r["id"] for r in rows} - {r["id"] for _, _, ex in HOT_CASES + HOT3_CASES for r in ex}
    joined = [a for a, sid in result.items() if sid is not None and sid in set(sids) and a in real]
    assert not joined, (name, joined)


@pytest.mark.parametrize("case", range(len(HOT_CASES)), ids=[c[1] for c in HOT_CASES])
def test_review_cases_in_the_unseen_window(case, hot_result):
    _check_case(HOT_CASES[case], *hot_result)


@pytest.mark.parametrize("case", range(len(HOT3_CASES)), ids=[c[1] for c in HOT3_CASES])
def test_third_review_cases_in_the_freshest_window(case, hot3_result):
    _check_case(HOT3_CASES[case], *hot3_result)


def test_injected_articles_do_not_join_similar_stories_of_the_window():
    """Aus dem Review: Ein Hamburger Messerangriff gehört nicht zum Berliner (513), ein
    Lufthansa-Streik nicht zur brennenden Powerbank im Lufthansa-Flug, Selenskyjs Patriot-Zusage von
    Merz nicht zum taz-Stück „Friedrich Merz – unser letzter Sponti“ (346)."""
    knife = _art(20, "spiegel", "de", "Hamburg: Mann verletzt zwei Menschen mit Messer – Festnahme",
                 "Nach einem Messerangriff in Altona sucht die Polizei nach dem Motiv. Der Tatverdächtige wurde festgenommen.")
    strike = _art(21, "spiegel", "de", "Warnstreik: Lufthansa streicht Hunderte Flüge in Frankfurt und München",
                  "Verdi ruft das Bodenpersonal zum Ausstand auf. Passagiere müssen mit Ausfällen rechnen.")
    pledge = _art(22, "guardian", "en", "Zelensky secures Patriot pledge from Merz in Berlin",
                  "Friedrich Merz promised more Patriot systems during Zelensky's Berlin visit.")
    result = _inject([knife, strike, pledge])
    powerbank = {r["id"] for r in _load("tune") if "Powerbank" in r["title"]}
    assert result[knife["id"]] is None or result[knife["id"]] != result[513]
    assert result[strike["id"]] is None or all(result[strike["id"]] != result[i] for i in powerbank)
    assert result[pledge["id"]] is None or result[pledge["id"]] != result[346]


def test_same_event_in_both_languages_is_found_in_a_full_window():
    volcano = [_art(30, "tagesschau", "de", "Vulkanausbruch auf Island: Grindavík erneut evakuiert",
                    "Auf der isländischen Halbinsel Reykjanes ist erneut ein Vulkan ausgebrochen. Der Ort Grindavík wurde evakuiert."),
               _art(31, "bbc", "en", "Iceland volcano erupts again, Grindavik evacuated",
                    "A volcano on Iceland's Reykjanes peninsula has erupted again, forcing the evacuation of the town of Grindavik.")]
    boeing = [_art(32, "spiegel", "de", "Boeing-Arbeiter treten in den Streik",
                   "Rund 33.000 Mechaniker des Flugzeugbauers Boeing in Seattle haben die Arbeit niedergelegt."),
              _art(33, "bbc", "en", "Boeing workers walk out in Seattle strike",
                   "About 33,000 Boeing machinists in the Seattle area have gone on strike after rejecting a contract offer.")]
    result = _inject(volcano + boeing)
    assert result[volcano[0]["id"]] is not None and result[volcano[0]["id"]] == result[volcano[1]["id"]]
    assert result[boeing[0]["id"]] is not None and result[boeing[0]["id"]] == result[boeing[1]["id"]]


def test_same_family_results_are_separate_stories():
    """Asienspiele: Gold im Säbel der Männer, Silber im Florett der Frauen, Esports-Gold für Korea
    (Kampfspiele) und Japan (Puzzle), Basketball- und Handball-Gold; dazu verschiedene Filmpreise."""
    rows = _load("tune")
    pred = cluster_rows(rows)
    family = {"kr_men_sabre_gold", "kr_women_foil_silver", "kr_women_sabre_gold", "kr_men_epee_silver",
              "kr_esports_fighting_gold", "jp_esports_puzzle_gold", "kr_equestrian_gold", "kr_baseball_gold",
              "kr_women_basketball_gold", "kr_women_handball_gold", "kr_skateboard_gold", "iran_volleyball",
              "pakistan_volleyball", "farhadi_award", "japan_critics_award", "ibsen_award"}
    story_labels: dict[int, set[str]] = {}
    for r in rows:
        if r["label"] in family and pred[r["id"]] is not None:
            story_labels.setdefault(pred[r["id"]], set()).add(r["label"])
    assert story_labels and all(len(v) == 1 for v in story_labels.values()), story_labels


# ---- Textverarbeitung ---------------------------------------------------------------

def test_normalize_folds_case_and_diacritics():
    assert normalize("Zölle, Straße, Ærø, São Paulo, ERDOĞAN") == "zolle, strasse, ærø, sao paulo, erdogan"


def test_words_maps_acronyms_and_phrases():
    assert words("US-Präsident trifft UN-Chef") == ["usa", "prasident", "trifft", "un", "chef"]
    assert words("The U.S. and the U.K.") == ["the", "usa", "and", "the", "britain"]
    assert words("talks at the White House on the Strait of Hormuz") == [
        "talks", "at", "the", "white_house", "on", "the", "hormuz"]
    assert words("us and them") == ["us", "and", "them"]      # klein geschrieben: das Pronomen


def test_feed_boilerplate_is_removed():
    text = ("Portugal faces a squeeze The post Portugal faces a squeeze appeared first on Portugal Resident .")
    assert "resident" not in words(text)
    assert words("(ANSA) - ROME, 27 SET - Italy wins") == ["italy", "wins"]
    assert words("SEOUL, Sept. 27 (Yonhap) -- Korea wins") == ["korea", "wins"]
    assert "mehr" not in words("Tote bei Unfall [ mehr ]")


def test_stopwords_and_news_filler_are_dropped():
    tk = Tokenizer()
    for w, lang in (("liveblog", "de"), ("newsblog", "de"), ("ticker", "de"), ("sagt", "de"), ("sonntag", "de"),
                    ("says", "en"), ("live", "en"), ("update", "en"), ("new", "en"), ("video", "en"),
                    ("podcast", "en"), ("sunday", "en"), ("september", "en"), ("report", "en")):
        assert tk.canon(w, lang) == (), w
    # Stoppwörter gelten je Sprache: "war" ist im Deutschen „war“, im Englischen Krieg.
    assert canon("war", "de") == [] and canon("war", "en") == ["war"]


def test_numbers_that_matter_are_kept():
    assert canon("737", "en") == ["737"] and canon("87", "de") == ["87"]
    for w in ("2026", "1999", "000", "5", "1st", "3rd"):
        assert canon(w, "en") == [], w


def test_light_stemming_de_and_en():
    assert stories._stem_de("angriffen") == stories._stem_de("angriffe") == "angriff"
    assert stories._stem_de("russischen") == stories._stem_de("russische") == "russisch"
    assert stories._stem_de("ministerinnen") == "minister"
    assert stories._stem_de("zeugnis") == "zeugnis" and stories._stem_de("merz") == "merz"
    assert stories._stem_en("arrested") == stories._stem_en("arrests") == "arrest"
    assert stories._stem_en("countries") == "country"
    assert stories._stem_en("shootings") == stories._stem_en("shooting") == "shoot"
    assert stories._stem_en("crisis") == "crisis" and stories._stem_en("virus") == "virus"


def test_lexicon_bridges_german_and_english():
    same = [
        (("Selenskyj", "de"), ("Zelensky", "en"), ("Zelenskyy", "en"), ("Selenski", "de")),
        (("Russland", "de"), ("russischen", "de"), ("Russia", "en"), ("Russian", "en")),
        (("Zölle", "de"), ("Strafzölle", "de"), ("tariffs", "en")),
        (("Waffenruhe", "de"), ("Waffenstillstand", "de"), ("ceasefire", "en"), ("truce", "en")),
        (("Wahl", "de"), ("Wahlen", "de"), ("elections", "en")),
        (("Erdbeben", "de"), ("earthquake", "en"), ("quake", "en")),
        (("Kanzler", "de"), ("Bundeskanzler", "de"), ("chancellor", "en")),
        (("Lawrow", "de"), ("Lavrov", "en")),
        (("Festnahmen", "de"), ("festgenommen", "de"), ("arrested", "en")),
        (("Kiew", "de"), ("Kyjiw", "de"), ("Kyiv", "en")),
    ]
    for group in same:
        forms = {tuple(canon(w, lang)) for w, lang in group}
        assert len(forms) == 1 and forms != {()}, group
    assert canon("Bundesregierung", "de") == ["germany", "government"]
    assert canon("Nordkorea", "de") == ["north", "korea"]


def test_german_compounds_split_into_known_parts():
    tk = Tokenizer(known={"pflege", "reform", "ukraine", "krieg", "neutralitat", "abstimmung"})
    toks = dict(tk.canon("pflegereform", "de"))
    assert toks["care"] == stories.PART_FACTOR and toks["reform"] == stories.PART_FACTOR
    assert toks["pflegereform"] == 1.0                          # das ganze Wort zählt auch
    assert {t for t, _ in tk.canon("ukrainekrieg", "de")} >= {"ukraine", "war"}
    # Fugen-s: „Neutralitäts|abstimmung“
    assert {t for t, _ in tk.canon("neutralitatsabstimmung", "de")} >= {"neutrality", "vote"}
    assert tk.canon("kurz", "de") == (("kurz", 1.0),)


def test_names_in_both_languages_stay_unstemmed():
    """„Linnemann“ würde deutsch zu „linneman“ gestemmt, englisch nicht: Ein Wort, das im Fenster in
    beiden Sprachen vorkommt, bleibt, wie es ist."""
    rows = [row(1, "a", "de", "Linnemann lehnt Pflegedeckel ab"),
            row(2, "b", "en", "Linnemann rejects care cap")]
    m = Model(rows)
    assert m.tokenizer.canon("linnemann", "de") == m.tokenizer.canon("linnemann", "en") == (("linnemann", 1.0),)


def test_german_iceland_and_other_lexicon_collisions():
    assert canon("Island", "de") == ["iceland"] and canon("island", "en") == ["island"]
    assert canon("Nordirland", "de") == ["northern", "ireland"]
    assert canon("Oranier", "de") == words("the Orange Order")[-1:] == ["orange_order"]
    assert canon("Messe", "de") != canon("mass", "en")          # Messe ist auch die Handelsmesse
    assert canon("kurz", "de") == ["kurz"] and canon("Mittel", "de") != canon("funds", "en")


def test_event_kinds_and_conflicts():
    """Arten aus den Wörtern (Substantiven) von Titel und erstem Satz, keine Verben."""
    quake = stories._kinds(words("Earthquake in Peru"))
    other_quake = stories._kinds(words("Schweres Erdbeben erschüttert Peru"))
    strike = stories._kinds(words("Warnstreik bei der Lufthansa"))
    fire = stories._kinds(words("Großbrand in Berliner Lagerhalle"))
    assert quake == other_quake == {"event:quake"} and strike == {"event:labour_strike"} and fire == {"event:fire"}
    assert not stories.kinds_conflict(quake, other_quake)
    assert stories.kinds_conflict(strike, fire)
    assert not stories.kinds_conflict(strike, frozenset())          # nur wenn beide eine Art nennen
    # Übertragen gemeinte Verben sind keine Ereignisse: „fires“ (Tor), „derailed“, „bewaffnete“
    assert stories._kinds(words("Palmer fires Chelsea past Arsenal")) == frozenset()
    assert stories._kinds(words("Talks derailed as Hamas rejects plan")) == frozenset()
    assert stories._kinds(words("Bewaffnete Gruppen im Sudan")) == frozenset()
    assert stories._kinds(words("Kleinflugzeug stürzt ab")) == frozenset()
    # Erster Satz zählt, der Rest des Anreißers nicht
    lead = words(stories._first_sentence("Eine Lawine hat drei Bergsteiger verschüttet. Später kam Hochwasser."))
    assert stories._kinds(words("Tirol"), lead) == {"event:avalanche"}
    assert stories.kinds_conflict(stories._kinds(words("Lawine in Tirol")), stories._kinds(words("Hochwasser in Tirol")))
    gold = stories._kinds(words("Fencing gold for Korea"))
    silver = stories._kinds(words("Korea"), words("Fencing silver in Aichi"))
    assert gold == {"sport:fencing", "medal:gold"} and "medal:silver" not in silver    # Medaille nur im Titel
    assert stories.kinds_conflict(gold, stories._kinds(words("Fencing silver for Korea")))
    assert stories._kinds(words("Gold price hits record")) == frozenset()                 # Goldpreis: kein Sport
    assert stories.kinds_conflict(stories._kinds(["basketball"]), stories._kinds(["handball"]))


def test_scores_central_banks_and_club_prefixes():
    assert stories._scores("Bundesliga: Bayern schlägt Freiburg 3:1") == {"score:3-1"}
    assert stories._scores("Arsenal lose 1-2 at Chelsea") == stories._scores("Chelsea beat Arsenal 2-1")
    assert stories._scores("tagesschau 20:00 Uhr", "Wahl vom 24-26 September") == frozenset()
    assert stories.kinds_conflict(stories._scores("Chelsea beat Arsenal 2-1"), stories._scores("Chelsea 1-0 Arsenal"))
    got = words("Federal Reserve, Reserve Bank of Australia, EZB und Europäische Zentralbank, Bank of England")
    assert got == ["fed", "rba", "ecb", "und", "ecb", "boe"] and "reserve" not in got
    assert {"fc", "sv", "vfl", "tsv"} <= stories._STOP_DE and {"sorgt", "wirbel"} <= stories._STOP_DE
    assert canon("Bundestag", "de") == ["bundestag"] and "bundestag" in stories._PLAIN_DE


def test_recurring_pieces_and_tickers():
    for title in ("tagesschau 20:00 Uhr", "tagesschau in 100 Sekunden", "+++ Ukraine-Krieg +++: Tote",
                  "Liveblog Irankrieg: Trump erwartet Gespräche", "Russia has lost 1,105,690 troops in Ukraine"):
        assert stories.is_recurring({"title": title, "url": ""}), title
    assert stories.is_recurring({"title": "Wahl", "url": "https://x.example/newsblog-wahl-1"})
    assert stories.is_recurring({"title": "tagesschau", "url": ""})
    assert not stories.is_recurring({"title": "Tagesschau-Sprecherin wechselt zum ZDF", "url": ""})
    assert not stories.is_recurring({"title": "Papst in Lourdes", "url": "https://x.example/papst"})


def test_summary_trim_is_the_same_in_sql_and_python(db):
    from app import db as db_mod
    assert db_mod.SUMMARY_TRIM == stories.SUMMARY_TRIM
    text = "\xa0\n  " + "x" * 79 + "\t "
    db.upsert_articles([make_row("alpha", 1, ago(hours=1), summary=text)])
    db.q("UPDATE articles SET body_text='Volltext hier'")
    row = dict(db.story_window(ago(hours=5), 600, 80)[0])
    assert row["body_text"] == "Volltext hier"                          # 79 Zeichen: kurz, in SQL ...
    fields = stories._fields({**row, "lang": "de", "source_id": "alpha"})
    assert any("Volltext" in text for text, _ in fields)               # ... wie in Python


def test_specific_words_exclude_common_german_nouns_and_verbs():
    rows = [row(1, "alpha", "de", "Messerangriff in Hamburg: Mann verletzt", "Die Polizei nahm den Täter fest, sagte ein Sprecher."),
            row(2, "delta", "de", "Messerangriff in Leipzig: Mann verletzt", "Die Polizei nahm den Täter fest."),
            row(3, "beta", "en", "Knife attack in Hamburg", "Police said a man was injured.")]
    m = Model(rows)
    assert m.specific("hamburg") and not m.specific("mann") and not m.specific("knife")
    assert not m.specific("nahm")                          # klein geschrieben: kein Name
    assert not m.linkable(m.docs[0], m.docs[1])            # nur Allgemeines gemeinsam, keine Stadt


def test_body_start_counts_only_for_short_summaries_and_without_captions():
    body = "(Bild: dpa) Foto: Max Muster / dpa Artikeltyp:Video Der Bundestag beschließt das Heizungsgesetz."
    assert "dpa" not in stories.clean_body(body) and "Heizungsgesetz" in stories.clean_body(body)
    long_summary = "x" * 120
    assert len(stories._fields({"title": "T", "summary": long_summary, "body_text": body})) == 2
    assert len(stories._fields({"title": "T", "summary": "", "body_text": body})) == 3


# ---- Clustering ---------------------------------------------------------------------

QUAKE = [
    row(1, "alpha", "de", "Erdbeben in der Türkei: Mindestens 40 Tote in Malatya",
        "Ein Erdbeben der Stärke 7,1 hat die Provinz Malatya erschüttert. Rettungskräfte suchen nach Verschütteten."),
    row(2, "beta", "en", "Earthquake in Turkey's Malatya kills at least 40",
        "A magnitude 7.1 earthquake struck Malatya province, rescuers are searching the rubble for survivors.", 1),
    row(3, "delta", "en", "Malatya earthquake: death toll rises to 40 as rescuers search rubble",
        "Rescuers in Turkey's Malatya province search for survivors after the 7.1 magnitude quake.", 2),
]
OTHER = [
    row(10, "beta", "en", "Trump announces tariffs on Chinese steel imports",
        "The US president said tariffs of 50 percent on steel from China will take effect in October.", 1),
    row(11, "delta", "en", "Trump hosts Japanese prime minister Takaichi at the White House",
        "The talks focused on defence cooperation and North Korea.", 2),
    row(12, "alpha", "de", "Bundesliga: Bayern gewinnt gegen Dortmund",
        "Harry Kane trifft doppelt, der FC Bayern bleibt Tabellenführer.", 3),
    row(13, "epsilon", "en", "Apple unveils new iPhone with satellite messaging",
        "The company showed the device at its campus in Cupertino.", 4),
]


def test_same_event_in_three_papers_and_two_languages_is_one_story():
    result = cluster_rows(QUAKE + OTHER)
    assert result[1] == result[2] == result[3] == 1          # kleinste Artikel-ID der Gruppe
    assert all(result[i] is None for i in (10, 11, 12, 13))   # Einzelartikel: keine Geschichte


def test_selenskyj_and_zelensky_meet_in_one_story():
    rows = [
        row(1, "alpha", "de", "Selenskyj trifft Merz in Berlin: Neue Patriot-Systeme für Kiew",
            "Bundeskanzler Merz sagt der Ukraine bei einem Treffen mit Selenskyj weitere Patriot-Systeme zu."),
        row(2, "beta", "en", "Zelensky meets Merz in Berlin, secures more Patriot systems for Kyiv",
            "German Chancellor Merz pledged additional Patriot air defence systems to Ukraine.", 2),
    ] + OTHER
    result = cluster_rows(rows)
    assert result[1] == result[2] == 1


def test_two_trump_stories_on_the_same_day_stay_apart():
    rows = OTHER + [
        row(20, "gamma", "en", "Trump says tariffs on Chinese steel will start next month",
            "Donald Trump said on Sunday that steel imports from China face tariffs of 50 percent.", 2),
        row(21, "epsilon", "en", "Takaichi and Trump discuss North Korea at White House meeting",
            "Japan's prime minister Sanae Takaichi met Donald Trump for talks on defence and North Korea.", 3),
    ]
    result = cluster_rows(rows)
    assert result[10] == result[20] is not None
    assert result[11] == result[21] is not None
    assert result[10] != result[11]


def test_frequent_entity_alone_does_not_make_a_story():
    """Zwei Artikel, die nur „Trump“ (und ein allgemeines Wort) teilen, bleiben einzeln."""
    rows = [
        row(1, "beta", "en", "Trump attends golf tournament in Virginia", "Trump watched the final round."),
        row(2, "delta", "en", "Trump signs order on federal hiring", "Trump signed the order at the White House.", 1),
    ] + OTHER
    result = cluster_rows(rows)
    assert result[1] is None and result[2] is None


def test_generic_events_in_different_places_are_not_one_story():
    """Drei Autounfälle an drei Orten teilen nur allgemeine Wörter: keine Geschichte (Namensanker)."""
    rows = [
        row(1, "beta", "en", "Woman dies in car crash in Wexford", "A woman in her 70s died after a crash on a rural road."),
        row(2, "delta", "en", "One-year-old dies in car crash in central Queensland",
            "A toddler died after a crash on a rural road, police said.", 2),
        row(3, "epsilon", "en", "One dead after two-car crash on Massey road", "One person died after a crash on the road.", 3),
    ] + OTHER
    result = cluster_rows(rows)
    assert {result[1], result[2], result[3]} == {None}


def test_articles_more_than_36_hours_apart_are_not_one_story():
    base = QUAKE[1]
    late = {**QUAKE[2], "published_at": iso(datetime.fromisoformat(base["published_at"]) + timedelta(hours=37))}
    assert cluster_rows([base, late] + OTHER)[2] is None
    near = {**QUAKE[2], "published_at": iso(datetime.fromisoformat(base["published_at"]) + timedelta(hours=30))}
    assert cluster_rows([base, near] + OTHER)[2] == 2


def test_story_needs_two_newspapers_and_counts_copies_per_publisher():
    """Mehrere Artikel nur einer Quelle sind keine Geschichte. Wörtliche Kopien (Agenturtext) zählen
    als ein Artikel beim Rechnen, aber jede Zeitung zählt: dieselbe Meldung in zwei Zeitungen ist eine
    Geschichte, in zwei Ausgaben von The Local nicht (ein Verlag)."""
    same_source = [{**r, "source_id": "beta"} for r in QUAKE]
    assert set(cluster_rows(same_source + OTHER).values()) == {None}
    local = [{**QUAKE[1], "source_id": "thelocalde"}, {**QUAKE[1], "id": 7, "source_id": "thelocalat"}]
    assert set(cluster_rows(local + OTHER).values()) == {None}
    copy = {**QUAKE[1], "id": 7, "source_id": "gamma"}
    result = cluster_rows([QUAKE[1], copy] + OTHER)
    assert result[2] == result[7] == 2
    result = cluster_rows(QUAKE + [copy] + OTHER)
    assert result[7] == result[2] == result[1] == 1


def test_copies_need_a_real_summary_and_another_source():
    """Wiederkehrende Rubriken mit leerem Anreißer („Der Morgen“) sind keine Kopien voneinander, ein
    Artikel derselben Quelle mit gleichem Text auch nicht (Neufassung, bleibt eigener Artikel)."""
    assert stories.copy_key({"title": "Der Morgen", "summary": ""}) is None
    assert stories.copy_key({"title": "Der Morgen", "summary": "Kurz."}) is None
    assert stories.copy_key(QUAKE[1]) == stories.copy_key({**QUAKE[1], "title": QUAKE[1]["title"].upper()})
    m = Model([QUAKE[1], {**QUAKE[1], "id": 8}, {**QUAKE[1], "id": 9, "source_id": "gamma"},
               row(10, "alpha", "de", "Der Morgen"), row(11, "beta", "de", "Der Morgen")])
    assert m.copies == {2: [9]} and m.index_of(9) == m.index_of(2) and m.index_of(8) != m.index_of(2)


def test_publishers():
    assert stories.publisher("thelocalde") == stories.publisher("thelocalat") == "thelocal"
    assert stories.publisher("heisesec") == stories.publisher("heise") == "heise"
    assert stories.publisher("tagesspiegel") == "tagesspiegel"


def test_story_id_is_deterministic_and_smallest_id():
    shuffled = list(reversed(QUAKE + OTHER))
    assert cluster_rows(shuffled) == cluster_rows(QUAKE + OTHER)
    renumbered = [{**r, "id": r["id"] + 100} for r in QUAKE] + OTHER
    assert cluster_rows(renumbered)[102] == 101


def test_story_keeps_id_of_older_member_outside_the_window():
    """Liegt der erste Artikel einer Geschichte nicht mehr im Fenster, übernehmen die übrigen seine
    story_id (die Geschichte zerfällt nicht)."""
    rows = [{**r, "story_id": 1} for r in QUAKE[1:]] + OTHER
    assert cluster_rows(rows)[2] == cluster_rows(rows)[3] == 1


def test_story_keeps_its_id_when_its_first_article_leaves_the_group():
    """Alle Artikel mit der bisherigen ID 2 landen wieder in einer Gruppe: Sie behält 2, auch wenn jetzt
    ein kleinerer Artikel dazugekommen ist (die offene Geschichte bleibt dieselbe)."""
    rows = [QUAKE[0]] + [{**r, "story_id": 2} for r in QUAKE[1:]] + OTHER
    result = cluster_rows(rows)
    assert result[1] == result[2] == result[3] == 2
    # Trägt ein Mitglied außerhalb dieser Gruppe die ID auch, geht sie nicht mit.
    rows = [QUAKE[0]] + [{**r, "story_id": 2} for r in QUAKE[1:]] + [{**OTHER[0], "story_id": 2}] + OTHER[1:]
    assert cluster_rows(rows)[1] == 1


def test_story_continues_only_within_max_gap_of_its_oldest_article():
    """outside_oldest: Eine Geschichte (ID 1, ihr Artikel 1 liegt im Fenster) mit einem älteren Artikel vor
    dem Fenster läuft weiter, solange ihr neuester Artikel höchstens MAX_GAP nach dem ältesten erschien;
    sonst bekommt die Gruppe eine neue ID und die alte steht in model.retired. Auch wenn die alte ID die
    kleinste Artikel-ID der Gruppe ist (Artikel vor dem Fenster mit höherer ID), gibt Schritt 3 sie nicht
    zurück (Review cold8)."""
    rows = [{**r, "story_id": 1} for r in QUAKE] + OTHER
    model = Model(rows)
    newest = max(d.ts for d in model.docs if d.id in (1, 2, 3))
    result = stories.assign(model, reserved={1}, outside_oldest={1: newest - 30 * 3600})
    assert result[1] == result[2] == result[3] == 1 and model.retired == set()
    model = Model(rows)
    result = stories.assign(model, reserved={1}, outside_oldest={1: newest - 50 * 3600})
    assert model.retired == {1}
    assert result[1] == result[2] == result[3] not in (None, 1)
    # Eine Geschichte vor dem Fenster (ID 99), zu alt: neue ID; knapp genug: weiter.
    rows = [{**r, "story_id": 99} for r in QUAKE] + OTHER
    model = Model(rows)
    assert stories.assign(model, outside_oldest={99: newest - 40 * 3600})[1] == 1 and model.retired == {99}
    model = Model(rows)
    assert stories.assign(model, outside_oldest={99: newest - 20 * 3600})[1] == 99 and model.retired == set()


def test_group_never_takes_a_reserved_id():
    """Sind alle Mitglieds-IDs noch als story_id alter Artikel vergeben (reserved), bleibt die Gruppe
    diesmal ohne Geschichte, statt eine fremde Geschichte fortzusetzen."""
    model = Model(QUAKE + OTHER)
    assert stories.assign(model, reserved={2, 3})[1] == 1
    result = stories.assign(model, reserved={1, 2, 3})
    assert result[1] is None and result[2] is None


def test_aliases_fold_first_names_directly_without_chains():
    """„Donald“ geht in trump auf, „Delcy“ in rodriguez; „President“ (Amt, allgemeines Wort) nie, und
    Ketten („Vice President Delcy Rodríguez“) machen aus president kein rodriguez. Unabhängig von der Reihenfolge."""
    m = Model.__new__(Model)
    m.plain_de, m.plain_en, m.tokenizer = set(), set(), Tokenizer()
    m.df = Counter({"president": 50, "vice": 5, "delcy": 4, "rodriguez": 8, "donald": 10, "trump": 60,
                    "tigst": 6, "assefa": 9})
    pairs = {("president", "vice"): 5, ("delcy", "rodriguez"): 4, ("delcy", "president"): 4,
             ("donald", "trump"): 10, ("president", "tigst"): 6, ("assefa", "tigst"): 6}
    a = m._aliases(Counter(pairs))
    b = m._aliases(Counter(dict(reversed(list(pairs.items())))))
    assert a == b and a["donald"] == "trump" and a["delcy"] == "rodriguez" and a["tigst"] == "assefa"
    assert "president" not in a and "vice" not in a


def test_commentary_and_newsletters_are_soft():
    assert stories.is_soft({"title": "Kommentar: Die Koalition wankt", "url": ""})
    assert stories.is_soft({"title": "Der Morgen: Papst in Lourdes, Vučić tritt zurück", "url": ""})
    assert stories.is_soft({"title": "Pope in Lourdes", "url": "https://www.theguardian.com/commentisfree/2026/x"})
    assert stories.is_soft({"title": "Politics live: Burnham speech", "url": "https://x.example/politics/live/1"})
    assert not stories.is_soft({"title": "Papst Leo besucht Lourdes", "url": "https://x.example/panorama/papst"})


def test_soft_piece_never_bridges_two_stories():
    """Ein Überblick (Briefing), der zwei Geschichten streift, verbindet sie nicht; er hängt höchstens
    an einer (nur bei sehr hoher Ähnlichkeit, SOFT_SIM_MIN)."""
    digest = row(30, "gamma", "en", "Morning briefing: Malatya earthquake death toll rises; Trump steel tariffs on China",
                 "Rescuers search the rubble in Malatya after the earthquake; Trump announces steel tariffs on China.", 3)
    tariffs = row(31, "epsilon", "en", "Trump steel tariffs on China to take effect in October",
                  "The US president said the 50 percent tariffs on Chinese steel will start in October.", 2)
    result = cluster_rows(QUAKE + OTHER + [digest, tariffs])
    assert result[1] == result[2] == result[3] and result[10] == result[31] is not None and result[1] != result[10]
    assert Model(QUAKE + [digest]).docs[-1].soft


def test_late_joiner_comes_to_a_grown_group(monkeypatch):
    """Die Kanten des Nachzüglers (3) kommen dran, als seine Gruppe noch nicht zu ihm passt (zu 2 und 4
    gar nicht ähnlich); danach wächst sie um 5 und 6, denen er ähnlich ist. Der dritte Durchgang holt
    ihn dann (zwei Kanten des ersten Durchgangs, Zusammenhalt stimmt), mit nur einer Kante nicht."""
    core = [0, 1, 2, 4]

    def sims_with(second: float) -> dict[tuple[int, int], float]:
        sims = {(a, b): 0.8 for a, b in itertools.combinations(core, 2)}
        sims.update({(m, k): 0.32 for m in core for k in (5, 6)})
        sims.update({(5, 6): 0.32, (0, 3): 0.35, (1, 3): second, (3, 5): 0.29, (3, 6): 0.29})
        return sims

    for second, want in ((0.33, [0, 1, 2, 3, 4, 5, 6]), (0.1, [0, 1, 2, 4, 5, 6])):
        sims = sims_with(second)
        monkeypatch.setattr(stories, "_candidate_pairs", lambda m, sims=sims: {k: 3 for k in sims})
        assert stories.cluster(_FakeModel(7, sims)) == [want], second


def test_member_that_hangs_on_one_pair_is_pruned(monkeypatch):
    """Früh über ein einzelnes Paar gekommen (5 an 0), zum Rest der gewachsenen Geschichte unähnlich:
    fällt am Ende heraus (PRUNE_SHARE)."""
    sims = {(a, b): 0.8 for a, b in itertools.combinations(range(5), 2)}
    sims[(0, 5)] = 0.9
    monkeypatch.setattr(stories, "_candidate_pairs", lambda m: {k: 3 for k in sims})
    assert stories.cluster(_FakeModel(6, sims)) == [[0, 1, 2, 3, 4]]


def test_centrality_prefers_the_article_that_tells_the_story():
    rows = QUAKE + OTHER + [row(4, "gamma", "en", "Malatya quake: aid convoy from Greece reaches Turkey",
                                "A convoy with tents from Greece reached Malatya after the earthquake.", 3)]
    model = Model(rows)
    result = stories.assign(model)
    cent = stories.centrality(model, result)
    members = [a for a, sid in result.items() if sid == result[2]]
    assert set(cent) >= set(members) and all(0 < cent[a] <= 1 for a in members)
    if 4 in members:
        assert cent[4] < max(cent[a] for a in members)


class _FakeModel:
    """Modell mit vorgegebenen Ähnlichkeiten (für die reine Clustering-Logik)."""

    def __init__(self, n: int, sims: dict[tuple[int, int], float]):
        self.docs = [Doc(id=i + 1, source=f"s{i}", lang="de", ts=T0.timestamp() + i, title=frozenset({"a", "b"}))
                     for i in range(n)]
        self._sims = sims
        self.postings: dict = {}
        self.copy_sources: dict = {}

    def sim(self, a: Doc, b: Doc) -> float:
        i, j = sorted((a.id - 1, b.id - 1))
        return self._sims.get((i, j), 0.0)

    def linkable(self, a: Doc, b: Doc) -> bool:
        return True


def test_average_link_prevents_chaining(monkeypatch):
    """A~B, B~C, C~D ... aber A!~C: keine Kette zu einem großen Klumpen."""
    n = 8
    sims = {(i, i + 1): 0.36 for i in range(n - 1)}
    model = _FakeModel(n, sims)
    monkeypatch.setattr(stories, "_candidate_pairs", lambda m: {k: 3 for k in sims})
    groups = stories.cluster(model)
    assert groups and max(len(g) for g in groups) <= 2
    # Ein dichtes Trio (alle Paare ähnlich) bleibt zusammen, der Randartikel, der nur an einem hängt, nicht.
    sims = {(0, 1): 0.6, (0, 2): 0.55, (1, 2): 0.5, (2, 3): 0.4}
    model = _FakeModel(4, sims)
    monkeypatch.setattr(stories, "_candidate_pairs", lambda m: {k: 3 for k in sims})
    assert stories.cluster(model) == [[0, 1, 2]]


def test_merge_needs_coherence_not_only_the_average(monkeypatch):
    """Ein Artikel, der zu einem von drei Mitgliedern sehr ähnlich ist, zu den anderen gar nicht, kommt
    nicht hinein, auch wenn der Mittelwert reichte (weniger als die Hälfte der Paare ist ähnlich)."""
    sims = {(0, 1): 0.95, (0, 2): 0.95, (1, 2): 0.95, (2, 3): 0.9}   # Mittelwert zu 3: 0.3
    model = _FakeModel(4, sims)
    monkeypatch.setattr(stories, "_candidate_pairs", lambda m: {k: 3 for k in sims})
    assert stories.cluster(model) == [[0, 1, 2]]


def test_realistic_chain_does_not_merge_two_stories():
    """Ein Überblicksartikel, der zwei Geschichten erwähnt, verbindet sie nicht."""
    bridge = row(30, "gamma", "en", "Malatya earthquake and Trump steel tariffs dominate Sunday",
                 "Rescuers search the rubble in Malatya; Trump announces steel tariffs on China.", 3)
    tariffs = row(31, "epsilon", "en", "Trump steel tariffs on China to take effect in October",
                  "The US president said the 50 percent tariffs on Chinese steel will start in October.", 2)
    result = cluster_rows(QUAKE + OTHER + [bridge, tariffs])
    assert result[1] == result[2] == result[3]
    assert result[10] == result[31] is not None
    assert result[1] != result[10]


def test_related_topic_needs_real_overlap_and_skips_own_story():
    """Im ganzen Tuning-Fenster (realistische IDF): Zu einem Drohnenangriff auf Kyjiw kommen andere
    Angriffe als Thema, nie die eigene Geschichte, die eigene Zeitung oder Trump beim Golf."""
    base = _load("tune")
    when = max(r["published_at"] for r in base)
    kyiv = {"id": 900_040, "source_id": "bbc", "lang": "en", "published_at": when,
            "title": "Russian drones hit Kyiv power grid, Zelensky says",
            "summary": "Zelensky said Russian drones struck energy infrastructure in Kyiv overnight."}
    golf = {"id": 900_042, "source_id": "guardian", "lang": "en", "published_at": when,
            "title": "Trump attends golf tournament in Virginia", "summary": "Trump watched the final round."}
    model = Model(base + [kyiv, golf])
    model.story_of = stories.assign(model)
    model.langs = {r["source_id"]: r["lang"] for r in base} | {"bbc": "en", "guardian": "en"}
    own = {a for a, sid in model.story_of.items() if sid is not None and sid == model.story_of[kyiv["id"]]}
    info = {r["id"]: r for r in base}

    class NoFts:
        def topic_candidates(self, *a, **kw):
            return []

    class CountingFts(NoFts):
        calls = 0

        def topic_candidates(self, *a, **kw):
            CountingFts.calls += 1
            return []

    stories._model = model
    try:
        ids = stories.topic_ids(NoFts(), kyiv, exclude=own)
        assert ids and golf["id"] not in ids and kyiv["id"] not in ids and not own & set(ids)
        assert all(stories.publisher(info[i]["source_id"]) != "bbc" for i in ids)
        # Die Volltextsuche nur, wenn der Index weniger als `limit` fand
        stories.topic_ids(CountingFts(), kyiv, exclude=own, limit=1)
        assert CountingFts.calls == 0
        stories.topic_ids(CountingFts(), golf, exclude=set(), limit=5)
        assert CountingFts.calls == 1
    finally:
        stories._model = None


def test_topic_lists_hold_at_most_two_per_paper_and_distinct_events():
    """Über viele Artikel des Tuning-Fensters: höchstens TOPIC_PER_PAPER Themen je Zeitung, keine zwei
    Themen, die dasselbe Ereignis erzählen, keine wiederkehrenden Sammelstücke; ohne Geschichte stehen
    Artikel desselben Ereignisses bei same_event_ids und nie unter den Themen."""
    base = _load("tune")
    info = {r["id"]: r for r in base}
    model = Model(base)
    model.story_of = stories.assign(model)
    model.langs = {r["source_id"]: r["lang"] for r in base}

    class NoFts:
        def topic_candidates(self, *a, **kw):
            return []

    stories._model = model
    try:
        checked = moved = 0
        for r in base[::25]:
            ids = stories.topic_ids(NoFts(), r, exclude={r["id"]})
            docs = [model.docs[model.index_of(i)] for i in ids]
            papers = Counter(stories.publisher(d.source) for d in docs)
            assert all(n <= stories.TOPIC_PER_PAPER for n in papers.values()), (r["id"], papers)
            assert not any(d.recurring for d in docs)
            assert not any(stories._near_same_event(model, x, y) for x, y in itertools.combinations(docs, 2))
            if model.story_of.get(r["id"]) is None:
                same = stories.same_event_ids(r, {r["id"]})
                moved += bool(same)
                assert not set(same) & set(ids)
                doc = model.docs[model.index_of(r["id"])]
                for i in same:                                   # Name gemeinsam oder gleiche Sprache, sehr ähnlich
                    other = model.docs[model.index_of(i)]
                    assert model._any_name(doc, other) or (
                        doc.lang == other.lang and model.sim(doc, other) >= stories.SAME_EVENT_PLAIN_SIM), (r["id"], i)
                assert not (doc.recurring and same)
                # Symmetrisch: Wer hier „dieselbe Geschichte“ ist, zeigt diesen Artikel nicht als Thema
                # (Irland–Israel aus dem Review: von der einen Seite Geschichte, von der anderen Thema).
                for i in same:
                    back = info[i]
                    osid = model.story_of.get(i)
                    ex = {i} | ({a for a, s in model.story_of.items() if s == osid} if osid is not None else set())
                    if osid is None:
                        ex |= set(stories.same_event_ids(back, {i}))
                    assert r["id"] not in stories.topic_ids(NoFts(), back, exclude=ex), (r["id"], i)
            checked += bool(ids)
        assert checked >= 10 and moved >= 1, (checked, moved)
    finally:
        stories._model = None


def test_topic_needs_content_beside_a_place_name():
    """Ein Land, eine Stadt, ein Kontinent allein neben einem Wort macht kein Thema („Europa“ und
    „Gespräche“); zwei Inhaltswörter oder eines neben zwei Namen schon."""
    film = row(60, "alpha", "de", "Regisseurin im Gespräch: Orpheus unter Gangstern in Europa",
               "Die Regisseurin spricht über ihren Film, der in Europa spielt, und über Gespräche am Set.", 1)
    putin = row(61, "beta", "en", "Putin says Russia will not attack Europe",
                "Putin said Russia will not attack Europe and blamed Ukraine for the breakdown in talks.", 2)
    pubs = row(62, "gamma", "en", "Pubs in Ireland refuse to screen Israel match in protest",
               "Pubs across Ireland refused to screen the football match against Israel.", 2)
    game = row(63, "delta", "en", "Irish football association fined over armbands at Israel match",
               "UEFA fined the Football Association of Ireland after players wore black armbands at the match.", 3)
    base = _load("tune")
    when = max(r["published_at"] for r in base)
    model = Model(base + [{**r, "published_at": when} for r in (film, putin, pubs, game)])
    d = {r["id"]: model.docs[model.index_of(r["id"])] for r in (film, putin, pubs, game)}
    assert stories._topic_score(model, d[60], d[61]) is None
    assert stories._topic_score(model, d[62], d[63]) is not None


def test_related_topic_is_never_the_same_event_or_the_same_paper():
    """„Zum Thema“ ist ein anderes Ereignis: Artikel, die das Ereignis der eigenen Geschichte erzählen
    (Stichwort der Geschichte im Titel, ähnlich genug für eine Kante), fehlen, auch wenn sie nicht in
    der Geschichte stehen; ebenso Artikel derselben Zeitung und Übersichten, die das Thema nur im Text
    streifen."""
    follow = row(50, "epsilon", "en", "Malatya: aid convoy reaches quake zone",
                 "Trucks with tents reached Malatya province, where rescuers search the rubble.", 3)
    own = row(51, "alpha", "de", "Türkei: Opferzahl nach Beben in Malatya steigt",
              "Rettungskräfte suchen in Malatya weiter nach Verschütteten.", 3)
    other_quake = row(52, "gamma", "en", "Turkey marks anniversary of Izmir earthquake with rebuilding push",
                      "Turkey's government says rebuilding after the Izmir earthquake is almost complete.", 4)
    digest = row(53, "delta", "en", "Evening news", "Malatya earthquake death toll rises; Trump steel tariffs.", 4)
    model = Model(QUAKE + OTHER + [follow, own, other_quake, digest])
    model.story_of = stories.assign(model)
    model.langs = {"alpha": "de", "beta": "en", "delta": "en", "epsilon": "en", "gamma": "en"}
    assert stories._topic_score(model, model.docs[model.index_of(1)], model.docs[model.index_of(51)]) is None

    class NoFts:
        def topic_candidates(self, *a, **kw):
            return []

    stories._model = model
    try:
        ids = stories.topic_ids(NoFts(), QUAKE[0], exclude={1, 2, 3})
    finally:
        stories._model = None
    # (Das Izmir-Beben teilt nur „Türkei“ und „Erdbeben“: zu wenig für ein Thema, siehe _topic_score.)
    assert not {50, 51, 53} & set(ids)


# ---- Lauf mit Datenbank -------------------------------------------------------------

CATALOG = Catalog([
    Source(id="alpha", name="Alpha", country="DE", lang="de", feed="https://a.example/f", homepage="https://a.example"),
    Source(id="beta", name="Beta", country="GB", lang="en", feed="https://b.example/f", homepage="https://b.example"),
    Source(id="gamma", name="Gamma", country="FR", lang="fr", feed="https://c.example/f", homepage="https://c.example"),
])


def _insert_quake(db, hours_ago: float = 3) -> list[int]:
    db.upsert_articles([
        make_row("alpha", 1, ago(hours=hours_ago), title=QUAKE[0]["title"], summary=QUAKE[0]["summary"]),
        make_row("beta", 2, ago(hours=hours_ago - 1), title=QUAKE[1]["title"], summary=QUAKE[1]["summary"]),
        make_row("gamma", 3, ago(hours=hours_ago - 2), title=QUAKE[2]["title"], summary=QUAKE[2]["summary"]),
        make_row("beta", 4, ago(hours=1), title=OTHER[0]["title"], summary=OTHER[0]["summary"]),
    ])
    return [r["id"] for r in db.q("SELECT id FROM articles ORDER BY id")]


async def test_refresh_writes_story_ids_and_logs(db, caplog):
    ids = _insert_quake(db)
    with caplog.at_level(logging.INFO, logger="kiosk.stories"):
        stats = await stories.refresh(db, CATALOG)
    got = {r["id"]: r["story_id"] for r in db.q("SELECT id, story_id FROM articles")}
    assert got == {ids[0]: ids[0], ids[1]: ids[0], ids[2]: ids[0], ids[3]: None}
    assert stats["stories"] == 1 and stats["in_stories"] == 3 and stats["changed"] == 3
    assert "Geschichten:" in caplog.text
    # Zweiter Lauf: nichts zu ändern, das Modell für verwandte Themen ist da.
    assert (await stories.refresh(db, CATALOG))["changed"] == 0
    assert stories.current_model().index_of(ids[1]) is not None


async def test_refresh_only_touches_the_window_and_clears_dissolved_stories(db):
    ids = _insert_quake(db)
    db.upsert_articles([make_row("alpha", 9, ago(days=5), title="Alte Meldung")])
    old = db.one("SELECT id FROM articles WHERE guid='alpha-9'")["id"]
    db.set_story_ids([(old, old), (ids[0], ids[3])])      # alte Geschichte, falsche Zuordnung
    await stories.refresh(db, CATALOG)
    assert db.story_id_of(old) == old                        # außerhalb des Fensters: unberührt
    assert db.story_id_of(ids[3]) is None                    # im Fenster, keine Geschichte: NULL
    assert db.story_id_of(ids[0]) == ids[0]


async def test_refresh_does_not_block_the_event_loop(db, monkeypatch):
    """Das Rechnen läuft in einem Thread (asyncio.to_thread)."""
    _insert_quake(db)
    called = {}
    real = stories.asyncio.to_thread

    async def spy(fn, *args):
        called["fn"] = fn
        return await real(fn, *args)

    monkeypatch.setattr(stories.asyncio, "to_thread", spy)
    await stories.refresh(db, CATALOG)
    assert called["fn"] is stories._compute


async def test_refresh_dissolves_old_ids_and_keeps_ids_of_other_stories(db):
    """Eine Geschichte, die es im Fenster nicht mehr gibt (zwei Artikel im Fenster trugen sie), verliert
    ihre ID auch bei älteren Mitgliedern. Ein einzelner Nachzügler, der jetzt allein steht, löst eine
    ältere Geschichte nicht auf. Eine ID, die ältere Artikel einer anderen Geschichte tragen, nimmt keine
    neue Gruppe."""
    ids = _insert_quake(db)
    db.upsert_articles([make_row("alpha", 9, ago(days=4), title="Alte Meldung"),
                        make_row("beta", 10, ago(days=4), title="Noch eine alte Meldung"),
                        make_row("gamma", 11, ago(days=4), title="Dritte alte Meldung"),
                        make_row("beta", 14, ago(days=4), title="Dritte alte Meldung, andere Zeitung"),
                        make_row("gamma", 12, ago(hours=2), title="Ganz andere Sache im Fenster"),
                        make_row("alpha", 13, ago(hours=2), title="Noch etwas ganz anderes")])
    old = [db.one("SELECT id FROM articles WHERE guid=?", (g,))["id"] for g in ("alpha-9", "beta-10", "gamma-11")]
    extra, straggler = (db.one("SELECT id FROM articles WHERE guid=?", (g,))["id"] for g in ("gamma-12", "alpha-13"))
    db.set_story_ids([(ids[3], old[0]), (ids[0], old[1]), (old[2], old[2])])
    db.set_story_ids([(ids[3], ids[3]), (ids[3], extra)])    # zwei Artikel im Fenster trugen ids[3]
    old_other = db.one("SELECT id FROM articles WHERE guid='beta-14'")["id"]
    db.set_story_ids([(old[2], straggler), (old[2], old_other)])   # ... einer Nachzügler von old[2] (2 Zeitungen)
    await stories.refresh(db, CATALOG)
    assert db.story_id_of(old[0]) is None                      # Geschichte ids[3] gibt es nicht mehr
    assert db.story_id_of(old[1]) == ids[0]                    # fremde alte Geschichte bleibt ...
    assert db.story_id_of(ids[0]) == ids[1] == db.story_id_of(ids[2])   # ... und die neue nimmt eine andere ID
    assert db.story_id_of(old[2]) == old[2]                    # ein Nachzügler löst nichts auf


async def test_refresh_clears_a_story_left_with_one_paper_before_the_window(db):
    """Bleibt von einer Geschichte vor dem Fenster nur eine Zeitung übrig (ihr Nachzügler steht jetzt
    allein), trägt auch der ältere Artikel keine story_id mehr."""
    _insert_quake(db)
    db.upsert_articles([make_row("gamma", 11, ago(days=4), title="Alte Meldung"),
                        make_row("alpha", 13, ago(hours=2), title="Etwas ganz anderes")])
    old, straggler = (db.one("SELECT id FROM articles WHERE guid=?", (g,))["id"] for g in ("gamma-11", "alpha-13"))
    db.set_story_ids([(old, old), (old, straggler)])
    await stories.refresh(db, CATALOG)
    assert db.story_id_of(old) is None and db.story_id_of(straggler) is None


async def test_refresh_clears_untitled_and_logs_skipped_runs(db, caplog, monkeypatch):
    ids = _insert_quake(db)
    db.q("UPDATE articles SET title='', story_id=? WHERE id=?", (ids[0], ids[3]))
    await stories.refresh(db, CATALOG)
    assert db.story_id_of(ids[3]) is None
    monkeypatch.setattr(stories, "_busy", True)
    with caplog.at_level(logging.INFO, logger="kiosk.stories"):
        assert await stories.refresh(db, CATALOG) is None
    assert "übersprungen" in caplog.text


async def test_refresh_requested_during_a_run_follows_right_after(db, monkeypatch):
    """Ein Lauf, der während eines anderen kommt, geht nicht verloren: gleich danach noch einmal."""
    _insert_quake(db)
    runs = []
    real = stories.asyncio.to_thread

    async def spy(fn, *args):
        runs.append(fn)
        if len(runs) == 1:
            assert await stories.refresh(db, CATALOG) is None      # läuft schon: übersprungen, vorgemerkt
        return await real(fn, *args)

    monkeypatch.setattr(stories.asyncio, "to_thread", spy)
    assert await stories.refresh(db, CATALOG) is not None
    assert len(runs) == 2 and not stories._dirty and not stories._busy
    assert await stories.refresh(db, CATALOG) is not None and len(runs) == 3


def test_tokenizer_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(stories, "TOKEN_CACHE_MAX", 5)
    t = Tokenizer()
    for i in range(20):
        t.canon(f"wort{i}", "de")
    assert len(t._cache) <= 5


async def test_related_topic_via_full_text_search_for_older_articles(db):
    """Artikel vor dem Fenster: Die Volltextsuche findet das Thema (auch mit „ß“ im Wort), bewertet mit
    den Vektoren; Wörter, die das Fenster nicht kennt, zählen als selten. Die Folgemeldung zum selben
    Unfall ist kein anderes Thema."""
    cat = Catalog([Source(id=s, name=s, country="DE", lang="de", feed=f"https://{s}.example/f",
                          homepage=f"https://{s}.example") for s in ("alpha", "gamma")]
                  + [Source(id="beta", name="B", country="GB", lang="en", feed="https://b.example/f",
                            homepage="https://b.example")])
    db.upsert_articles([
        make_row("alpha", 1, ago(days=4), title="Straßenbahn in Görlitz entgleist",
                 summary="Die Straßenbahn entgleiste in Görlitz, die Verkehrsbetriebe prüfen die Weichen."),
        make_row("gamma", 2, ago(days=6), title="Görlitz: Verkehrsbetriebe bestellen neue Straßenbahnen und Weichen",
                 summary="Die Verkehrsbetriebe in Görlitz kaufen zwölf neue Straßenbahnen, auch die Weichen der "
                         "Straßenbahn werden erneuert."),
        make_row("gamma", 6, ago(days=4), title="Görlitz: Weichen nach Straßenbahn-Unfall geprüft",
                 summary="Nach dem Unfall der Straßenbahn in Görlitz prüfen die Verkehrsbetriebe alle Weichen."),
        make_row("gamma", 3, ago(days=3), title="Görlitz: Neues Theater eröffnet", summary="Ein Theaterabend."),
        make_row("beta", 4, ago(hours=2), title="Something else entirely", summary="Nothing here."),
        make_row("beta", 5, ago(hours=3), title="Another thing", summary="Nothing either."),
    ])
    await stories.refresh(db, cat)
    first = dict(db.one("SELECT id, source_id, title, summary, body_text, published_at FROM articles WHERE guid='alpha-1'"))
    query = stories._fts_topic_query(stories.current_model(), first)
    assert "straßenbahn" in query and query.startswith("{title summary} : (")   # ohne Volltexte
    ids = stories.topic_ids(db, first, {first["id"]})
    second = db.one("SELECT id FROM articles WHERE guid='gamma-2'")["id"]
    theatre = db.one("SELECT id FROM articles WHERE guid='gamma-3'")["id"]
    follow_up = db.one("SELECT id FROM articles WHERE guid='gamma-6'")["id"]     # dasselbe Ereignis
    assert second in ids and theatre not in ids and first["id"] not in ids and follow_up not in ids


async def test_sliding_window_never_chains_stories_past_max_gap(tmp_path):
    """Wie im Betrieb: Held-out- und Tuning-Fenster (24.09. 06:00 bis 27.09. 17:16) laufen alle drei
    Stunden durch refresh auf einer echten Datenbank. Danach umfasst keine Geschichte mehr als MAX_GAP
    (früher kettete sich Enis Preisdeckel vom 25.9. an den von SOCAR am 27.9.), und keine story_id hängt
    an Artikeln aus nur einer Zeitung."""
    from types import SimpleNamespace
    rows = sorted(_load("heldout") + _load("tune"), key=lambda r: (r["published_at"], r["id"]))
    langs = {r["source"]: r["lang"] for r in rows}
    catalog = SimpleNamespace(all=lambda: [SimpleNamespace(id=s, lang=l) for s, l in langs.items()])
    db = Database(tmp_path / "sim.db")
    try:
        start = datetime.fromisoformat(rows[0]["published_at"])
        end = datetime.fromisoformat(rows[-1]["published_at"])
        now, i = start + timedelta(hours=3), 0
        while now <= end + timedelta(hours=3):
            batch = []
            while i < len(rows) and datetime.fromisoformat(rows[i]["published_at"]) <= now:
                r = rows[i]
                i += 1
                batch.append({"source_id": r["source"], "guid": str(r["id"]), "url": r["url"] or f"https://x.example/{r['id']}",
                              "title": r["title"], "summary": r["summary"], "author": None, "image_url": None,
                              "published_at": r["published_at"], "fetched_at": r["published_at"]})
            db.upsert_articles(batch)
            await stories.refresh(db, catalog, now=now)
            now += timedelta(hours=3)
        by: dict[int, list] = {}
        for r in db.q("SELECT published_at, story_id, source_id FROM articles WHERE story_id IS NOT NULL",
                      time_limit=None):
            by.setdefault(r["story_id"], []).append(r)
        assert len(by) >= 50
        for sid, members in by.items():
            ts = [datetime.fromisoformat(m["published_at"]) for m in members]
            assert max(ts) - min(ts) <= stories.MAX_GAP, (sid, len(members), max(ts) - min(ts))
            assert len({stories.publisher(m["source_id"]) for m in members}) >= 2, sid
    finally:
        db.close()
        stories._model = None


# ---- Migration auf Schema 8 --------------------------------------------------------

def test_migration_v7_to_v8_adds_story_id(tmp_path):
    path = tmp_path / "v7.db"
    db = Database(path)
    db.upsert_articles([make_row("alpha", 1, ago(hours=1), title="Energiewende kommt")])
    db.close()
    # Stand v7 nachbauen: Index und Spalte weg, Version zurück.
    conn = sqlite3.connect(path)
    conn.execute("DROP INDEX idx_articles_story")
    conn.execute("ALTER TABLE articles DROP COLUMN story_id")
    conn.execute("UPDATE meta SET value='7' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    for _ in range(2):  # zweites Öffnen: nichts mehr zu tun
        db = Database(path)
        try:
            assert db.get_meta("schema_version") == str(SCHEMA_VERSION) == "8"
            assert "story_id" in {r["name"] for r in db.q("PRAGMA table_info(articles)")}
            assert "idx_articles_story" in {r["name"] for r in db.q("PRAGMA index_list(articles)")}
            aid = db.one("SELECT id FROM articles")["id"]
            assert db.story_id_of(aid) is None
            db.set_story_ids([(aid, aid)])
            assert db.story_id_of(aid) == aid
            db.set_story_ids([(None, aid)])
            # FTS bleibt intakt (story_id löst die Trigger nicht aus).
            db.q("INSERT INTO articles_fts(articles_fts, rank) VALUES('integrity-check', 1)")
            assert db.one("SELECT COUNT(*) AS n FROM articles_fts WHERE articles_fts MATCH 'energiewende'")["n"] == 1
        finally:
            db.close()


def test_story_lookups_use_the_index(db, monkeypatch):
    plan = " ".join(r["detail"] for r in db.q("EXPLAIN QUERY PLAN SELECT id FROM articles WHERE story_id=?", (1,)))
    assert "idx_articles_story" in plan
    # Auch die Form der API (IN-Liste, Zeitraum, Quellen): nicht über Quelle und Zeit das Fenster lesen.
    seen = []
    real = db.q

    def spy(sql, params=(), **kw):
        seen.append((sql, params))
        return real(sql, params, **kw)

    monkeypatch.setattr(db, "q", spy)
    db.story_articles(1, [1, 2, 3], sources=["alpha", "beta"], since=ago(hours=36), until=ago(hours=0))
    sql, params = seen[-1]
    plan = " ".join(r["detail"] for r in real("EXPLAIN QUERY PLAN " + sql, params))
    assert "idx_articles_story" in plan, plan


def test_short_summaries_with_body(db):
    db.upsert_articles([make_row("alpha", 1, ago(hours=1), summary="kurz"),
                        make_row("alpha", 2, ago(hours=1), summary="Ein langer Anreißer " * 10)])
    ids = [r["id"] for r in db.q("SELECT id FROM articles ORDER BY id")]
    assert db.short_summaries_with_body(ids, 80) == 0
    db.q("UPDATE articles SET body_text='Volltext' WHERE id IN (?, ?)", tuple(ids))
    assert db.short_summaries_with_body(ids, 80) == 1 and db.short_summaries_with_body([], 80) == 0


# ---- Aufruf aus dem Sammler ---------------------------------------------------------

def _fetcher(db, settings) -> Fetcher:
    f = Fetcher(db, CATALOG, settings)
    f._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    return f


async def test_fetch_cycle_runs_the_clustering(db, settings, monkeypatch):
    calls = []

    async def fake_refresh(d, catalog, now=None):
        calls.append((d, catalog))

    monkeypatch.setattr(stories, "refresh", fake_refresh)
    f = _fetcher(db, settings)
    await f.run_once()
    assert calls == [(db, CATALOG)]


async def test_fetch_cycle_regroups_after_bodies_for_short_summaries(db, settings, monkeypatch):
    """Kam bei der Extraktion der Volltext eines Artikels ohne brauchbaren Anreißer, gruppiert der
    Lauf gleich noch einmal (dessen Geschichte hängt am Volltext)."""
    calls = []

    async def fake_refresh(d, catalog, now=None):
        calls.append(1)

    monkeypatch.setattr(stories, "refresh", fake_refresh)
    f = _fetcher(db, settings)
    db.upsert_articles([make_row("alpha", 1, ago(hours=1), summary="")])
    aid = db.one("SELECT id FROM articles")["id"]

    async def extract():
        db.q("UPDATE articles SET body_text='Der Volltext' WHERE id=?", (aid,))
        f._extracted_ids = [aid]
        return 1

    monkeypatch.setattr(f, "_extract_pending", extract)
    await f.run_once()
    assert len(calls) == 2

    async def nothing():
        f._extracted_ids = []
        return 0

    calls.clear()
    monkeypatch.setattr(f, "_extract_pending", nothing)
    f._stories_at = float("-inf")
    await f.run_once()
    assert len(calls) == 1


async def test_fetch_cycle_without_new_articles_regroups_only_every_30_minutes(db, settings, monkeypatch):
    calls = []

    async def fake_refresh(d, catalog, now=None):
        calls.append(1)

    monkeypatch.setattr(stories, "refresh", fake_refresh)
    f = _fetcher(db, settings)                   # alle Feeds antworten 503: keine neuen Artikel
    await f.run_once()
    await f.run_once()
    assert len(calls) == 1                       # der zweite Lauf ohne Neues kurz danach: übersprungen
    f._stories_at -= stories_idle()
    await f.run_once()
    assert len(calls) == 2


def stories_idle() -> float:
    from app.fetcher import STORIES_IDLE_REFRESH
    return STORIES_IDLE_REFRESH.total_seconds()


async def test_error_in_the_body_check_never_stops_the_fetcher(db, settings, monkeypatch, caplog):
    async def fake_refresh(d, catalog, now=None):
        return None

    def broken(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(stories, "refresh", fake_refresh)
    f = _fetcher(db, settings)
    monkeypatch.setattr(f.db, "short_summaries_with_body", broken)
    with caplog.at_level(logging.ERROR, logger="kiosk.fetcher"):
        await f.run_once()
    assert f.state["runs"] == 1 and "Geschichten: Lauf fehlgeschlagen" in caplog.text


def test_storyless_article_in_a_liveblog_gets_no_same_event():
    base = _load("tune")
    when = max(r["published_at"] for r in base)
    blog = {"id": 900_100, "source_id": "faz", "lang": "de", "published_at": when,
            "title": "Liveblog Fairford: Fünf Festnahmen nahe US-Militärbasis in England",
            "summary": "Die britische Polizei hat nahe der Militärbasis Fairford fünf Männer wegen Terrorverdachts festgenommen."}
    model = Model(base + [blog])
    model.story_of = stories.assign(model)
    stories._model = model
    try:
        assert model.story_of[blog["id"]] is None and stories.same_event_ids(blog, {blog["id"]}) == []
    finally:
        stories._model = None


async def test_clustering_error_never_stops_the_fetcher(db, settings, monkeypatch, caplog):
    async def broken(*a, **kw):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(stories, "refresh", broken)
    f = _fetcher(db, settings)
    with caplog.at_level(logging.ERROR, logger="kiosk.fetcher"):
        await f.run_once()
    assert f.state["runs"] == 1 and f.state["running"] is False
    assert "Geschichten: Lauf fehlgeschlagen" in caplog.text
    await f.update_stories()   # auch direkt aufgerufen (Start): kein Fehler nach außen
