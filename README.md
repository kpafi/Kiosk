# Kiosk

Kiosk ist ein privater Nachrichten-Aggregator für einen kleinen Kreis von Leuten. Er holt die RSS-Feeds
von Zeitungen aus aller Welt, lädt den Volltext der Artikel und zeigt sie in einer Web-App hinter einem Login.

## Warum

Angefangen hat es mit spiegel.de. Zwischen den frei lesbaren Artikeln stehen dort immer SPIEGEL+-Artikel,
und man scrollt ständig an der Paywall vorbei. Ich wollte wissen, ob man an die Artikel auch direkt kommt,
ohne den Umweg über die Website. Beim Nachforschen wurde daraus eine größere Idee: eine eigene Nachrichten-App,
die auch andere Zeitungen an einem Ort sammelt. Paywall-Artikel blendet Kiosk bis heute aus.

## Was es macht

Der Katalog in `sources.yaml` enthält 100 Quellen auf Deutsch und Englisch, 32 davon sind beim ersten Start
eingeschaltet. Alle 15 Minuten holt der Server die Feeds, extrahiert den Artikeltext mit trafilatura und
speichert alles in SQLite mit Volltextsuche (FTS5). Artikel hinter einer Paywall werden erkannt und
ausgeblendet. Nach 90 Tagen werden Artikel gelöscht, gemerkte bleiben.

Jedes Konto hat eigenen Lesestand, Profile (Zusammenstellungen von Zeitungen), Favoriten und Einstellungen.
Am Desktop zeigt die rechte Spalte Artikel anderer Zeitungen über dasselbe Ereignis.

Optional schreibt ein Sprachmodell über OpenRouter zweimal am Tag eine zusammenfassende Ausgabe aus allen
Artikeln. Sie erscheint als eigene Zeitung „Kiosk“ und kann per Mail an feste Empfänger gehen.

## Technik

Backend in Python 3.12 mit FastAPI 0.120, uvicorn, feedparser, trafilatura 2.2.0 und httpx (`app/`).
Datenbank ist eine SQLite-Datei mit WAL. Die Oberfläche ist eine PWA in reinem JavaScript ohne
Build-Schritt (`static/`). Betrieb in Docker hinter Caddy.

## Lokal starten

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
KIOSK_PASSWORD=test-passwort-lokal KIOSK_COOKIE_SECURE=false KIOSK_DOCS=1 .venv/bin/uvicorn app.main:app --reload --port 8100
```

Dann http://127.0.0.1:8100 öffnen und als `admin` mit dem Passwort aus `KIOSK_PASSWORD` anmelden.
Der erste Sammellauf startet sofort. Die API-Doku liegt nach dem Login unter `/docs`.

Alle Einstellungen stehen kommentiert in `.env.example`. Für die KI-Ausgabe braucht es `OPENROUTER_API_KEY`,
für den Mailversand die Variablen `KIOSK_MAIL_*`.

## Konten

Es gibt keine Registrierung und kein „Passwort vergessen“. Konten legt der Betreiber auf der Kommandozeile an:

```bash
.venv/bin/python -m app.users add anna --name "Anna"
.venv/bin/python -m app.users passwd anna
.venv/bin/python -m app.users list
```

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Die Tests für `static/js/format.js` und `static/js/route.js` laufen mit Node und werden ohne Node übersprungen.

## Deploy

`bash deploy/deploy.sh` baut das Image auf dem Server, startet es und prüft den Login über Caddy.
Das Skript ist auf genau einen Server zugeschnitten (SSH-Alias, Pfade unter `/opt/kiosk`, IP-Adressen).
Für einen anderen Server muss man es anpassen. Details stehen in `deploy/README.md`.

## Einschränkungen

- Es läuft genau ein uvicorn-Prozess. Sammler, Login-Bremse und Datenbankverbindung setzen das voraus.
- Paywall-Artikel fehlen ganz. Bei NZZ und South China Morning Post betrifft das die meisten Artikel.
- Einige Seiten sperren Server-Zugriffe (New York Times, Japan Times, The Hindu, ReliefWeb). Sie sind
  deshalb standardmäßig aus.
- Bei manchen Verlagen bleiben Reste im Text, etwa „Lesen Sie auch“-Hinweise.
- Die Zuordnung zu Geschichten arbeitet nur auf Deutsch und Englisch. Sie verbindet lieber zu wenig als
  zu viel, dieselbe Meldung landet deshalb manchmal in zwei Geschichten.
- Bilder lädt der Browser direkt von den Verlagsseiten. Einen Bild-Proxy gibt es nicht.
- Für die Oberfläche gibt es keine Browser-Tests.
- Das voreingestellte Modell der KI-Ausgabe (`KIOSK_AI_MODEL`) ist ein Platzhalter. Ein Lauf mit
  Claude Sonnet kostet bis zu etwa 0,50 USD.

Die vollständige Beschreibung von API, Konfiguration, Abruf und KI-Ausgabe steht in `docs/REFERENZ.md`.
