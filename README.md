# Kiosk

Privater Nachrichten-Aggregator: holt RSS-Feeds von über 90 seriösen Zeitungen aus aller Welt,
extrahiert den Volltext der Artikel und stellt alles gebündelt hinter einem Passwort bereit.

## Aufbau

- `app/` FastAPI-Backend (Python 3.12)
  - `sources.py` lädt den Katalog aus `sources.yaml`
  - `fetcher.py` holt alle 15 Minuten die aktivierten Feeds und extrahiert im Hintergrund den Volltext
  - `extract.py` Volltext per trafilatura, HTML auf eine Whitelist bereinigt, Paywall-Erkennung
  - `db.py` SQLite mit WAL und FTS5-Volltextsuche
  - `api_ui.py` Routen für die Oberfläche: Profile, Lieblingszeitungen, Leiste, Einstellungen, Zähler
  - `auth.py` ein Passwort, signiertes Cookie, Login-Bremse
- `static/` Web-Oberfläche (PWA ohne Build-Schritt: `index.html`, `js/`, `css/`, `sw.js`)
- `deploy/` Caddy-Block und Deploy-Skript für die Hetzner-Box

## Lokal starten

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
KIOSK_PASSWORD=test KIOSK_COOKIE_SECURE=false KIOSK_DOCS=1 .venv/bin/uvicorn app.main:app --reload --port 8100
```

Dann http://127.0.0.1:8100 öffnen, API-Doku unter `/docs` (nach Login, nur mit `KIOSK_DOCS=1`).

## API (Kurzfassung)

| Methode | Pfad | Zweck |
|---|---|---|
| POST | `/api/login` `{password}` | Anmelden, setzt Cookie |
| POST | `/api/logout` | Abmelden; macht alle Sessions ungültig |
| GET | `/api/sources` | Alle Quellen mit Status und Zählern, Kurzname `short`, `is_favorite` |
| PATCH | `/api/sources/{id}` `{enabled}` | Quelle ein-/ausschalten |
| POST | `/api/sources/bulk` `{enabled:[], disabled:[]}` | Mehrere Quellen umschalten |
| GET | `/api/articles?source=&country=&profile=&q=&unread=&starred=&later=&free=&days=&before=&offset=&limit=` | Artikelliste, siehe Blättern und Filter |
| GET | `/api/articles/{id}` | Artikel mit Volltext, siehe Abruf auf Anforderung |
| GET | `/api/articles/{id}/next?source=&country=&profile=&unread=true&starred=&later=&free=&days=` | Nächster älterer Artikel im selben Filterkontext: `{item}` oder `{item: null}` |
| POST | `/api/articles/{id}/read` `{value}` | Gelesen-Status |
| POST | `/api/articles/{id}/star` `{value}` | Favorit |
| POST | `/api/articles/{id}/later` `{value}` | Später lesen |
| POST | `/api/articles/read-all` `{sources?, source?, profile?, max_id?}` | Alles als gelesen markieren (Angaben schränken ein, Schnittmenge; `max_id`: nur Artikel mit ID ≤ max_id, die Oberfläche schickt die höchste angezeigte ID); `{marked}` zählt nur Artikel ohne Paywall |
| GET | `/api/profiles` | Profile nach `position`, mit `source_count` und `unread` (siehe Profile) |
| POST | `/api/profiles` `{name, sources, terms, in_bar}` | Profil anlegen (201); `sources: null` = alle aktiven, `[]` ist ungültig (422) |
| PATCH | `/api/profiles/{id}` (Teilfelder) | Profil ändern |
| DELETE | `/api/profiles/{id}` | Profil löschen (204); vorgegebene Profile: 409 |
| POST | `/api/profiles/reorder` `{ids}` | Reihenfolge setzen; liefert die Liste |
| GET | `/api/favorites/sources` | Lieblingszeitungen `[{source_id, name, short, country, enabled, in_bar, position, unread}]` |
| PUT | `/api/favorites/sources/{id}` `{in_bar?}` | Lieblingszeitung anlegen oder ändern; liefert den Eintrag |
| DELETE | `/api/favorites/sources/{id}` | Lieblingszeitung entfernen (204) |
| POST | `/api/favorites/sources/reorder` `{ids}` | Reihenfolge setzen; liefert die Liste |
| GET | `/api/bar` | Leiste: `{items: [{type: all\|source\|profile, id, key, label, unread}]}` |
| PATCH | `/api/bar` `{order: ["source:spiegel", "profile:3"]}` | Reihenfolge der Leiste speichern; liefert die Leiste. Unbekannte Zeitungen oder Profile: 400 |
| GET | `/api/settings` | Einstellungen der Oberfläche |
| PATCH | `/api/settings` (Teilfelder) | Einstellungen ändern (validiert); liefert alle |
| GET | `/api/counts` | `{unread, later, starred, profiles, favorites, sources_enabled, sources_total, sources_failing}` |
| POST | `/api/refresh` | Sammellauf sofort starten |
| GET | `/api/status` | Build-Zeitstempel (`build`), Sammler-Status, Datenbankgröße, Einstellungen |
| GET | `/api/build` | Ohne Login: `{build}` (Version des Service Workers) |
| GET | `/favicon.ico` | Ohne Login: Icon |
| GET | `/healthz` | Ohne Login; 503, wenn die Datenbank nicht antwortet, die Sammler-Schleife beendet ist (`fetcher`) oder hängt (`fetcher_stalled`: seit mehr als 3 Intervallen + 10 min kein Lauf fertig, bzw. der laufende Lauf dauert schon über 3 h). Im zweiten und dritten Fall beendet ein Wachhund den Prozess nach spätestens 1 min (Exit-Code 3), Docker startet neu |

Artikelfelder: `text_status` ist `full`, `partial` (Paywall oder sehr kurz), `empty`, `failed` oder `pending`.
`paywalled` kommt aus der Schema.org-Markierung der Verlage (`isAccessibleForFree: false`).

### Blättern

- Ohne Suche: Sortierung `published_at DESC, id DESC`, Keyset-Cursor. Die Antwort enthält
  `next_before` im Format `"<published_at>|<id>"` (undurchsichtig behandeln und unverändert als
  `before` zurückgeben); `null` heißt letzte Seite. Eine reine Zahl als `before` (altes Format)
  wird noch akzeptiert und als Artikel-ID gedeutet. Ein nicht URL-kodiertes `+` im Zeitstempel
  (kommt als Leerzeichen an) wird toleriert.
- Mit Suche (`q`): Sortierung nach Relevanz (bm25), deshalb Blättern per `offset`; die Antwort
  enthält `next_offset` (oder `null`). `before` wird bei Suchen ignoriert.
- Ungültige Suchanfragen oder Cursor (auch IDs über 2^63-1) liefern HTTP 400; Artikel-IDs im Pfad
  außerhalb von 1 bis 2^63-1 liefern 422. Mehr als 200 Quellen-IDs in `source`
  (bzw. in `sources`, `enabled`, `disabled` bei `read-all` und `bulk`) liefern 400.

### Filter

- `profile=<id>`: Quellen des Profils (geschnitten mit den aktivierten Quellen; `sources: null` heißt
  alle aktivierten) und, falls vorhanden, Suchbegriffe: jeder Begriff als Präfix-Phrase über den
  Volltextindex, ODER-verknüpft. Die Sortierung bleibt chronologisch (Keyset-Cursor wie ohne Profil).
  Mit `source`/`country` kombiniert gilt die Schnittmenge; mit `q` Offset-Blättern wie bei jeder Suche.
  Unbekanntes Profil: 404.
- `days=<n>` (1 bis 3650): nur Artikel mit `published_at` in den letzten n Tagen.
- `later=true`: nur „Später lesen“.
- `free`: ohne Angabe `true`, Paywall-Artikel sind also immer ausgeblendet (Listen, „nächster Artikel“,
  Zähler). Nur ein ausdrückliches `free=false` zeigt sie; die Oberfläche schickt das nie.
- Jeder Artikel enthält `later` (0/1) und `read_minutes` (`max(1, round(body_len / 1100))`, 0 ohne Text).
- Mit `q` enthält die Antwort `total` (Trefferzahl, höchstens 1000), sonst `total: null`.

### Profile, Leiste, Einstellungen

- Beim ersten Start mit Schema v4 werden vier Profile angelegt: Deutschland (alle `DE`-Quellen),
  Weltweit (alle), Wirtschaft (`category: business`, ist keine davon aktiv: alle, mit Begriffen),
  Militär (alle, mit Begriffen). Sie sind änderbar, aber nicht löschbar.
- Profil-Validierung: `name` 1 bis 40 Zeichen; `sources` `null` oder bekannte Quellen-IDs (höchstens 200);
  `terms` 0 bis 20 Begriffe à 1 bis 40 Zeichen ohne `"`; `in_bar` boolesch. Fehler: 422.
- `unread` eines Profils zählt ungelesene Artikel ohne Paywall (höchstens 5000) und wird höchstens alle
  60 s neu berechnet; Änderungen an Profilen oder Quellen, „alles gelesen“, das Setzen des
  Gelesen-Status eines Artikels und ein beendeter Sammellauf verwerfen den Zwischenspeicher sofort.
  Auch `unread` in `/api/sources` zählt nur Artikel ohne Paywall.
- Leiste: `ALLE` zuerst, danach Lieblingszeitungen und Profile mit `in_bar` in der Reihenfolge aus
  `bar_order`; nicht dort genannte hängen hinten an (erst Zeitungen, dann Profile). `label` ist für
  Zeitungen `short` aus `sources.yaml` (höchstens 12 Zeichen, Versalien), für Profile der Name in Versalien.
- Einstellungen: `theme` (`system`/`dark`/`light`), `text_size` (`s`/`m`/`l`), `mark_read_on_open`,
  `show_images`, `free_only` (boolesch, streng), `bar_order` (Liste aus `source:<id>`/`profile:<id>`).
  Standard: `dark`, `m`, `true`, `true`, `true`, `[]`. Unbekannte Felder oder `null`: 422.
  `free_only` bleibt nur aus Kompatibilität: `false` wird angenommen und gespeichert, hat aber keine
  Wirkung (Paywall-Artikel sind nie sichtbar); die Oberfläche zeigt den Schalter nicht mehr.
- `counts` zählt `unread`, `later` und `starred` immer ohne Paywall-Artikel (wie die Listen).

### Text- und HTML-Vertrag

- Nur `body_html` ist HTML, und zwar serverseitig auf eine Whitelist bereinigt (keine Skripte,
  keine Event-Attribute, Links und Bilder nur http(s)).
- `title`, `summary`, `author` sind reiner Text und müssen in jeder Oberfläche als Text
  (z. B. `textContent`) ausgegeben werden, nie als HTML. Platzhalter aus Feeds („None“, „null“,
  „undefined“) und ein Teaser, der nur den Titel wiederholt, werden beim Abruf verworfen (Schema v5
  räumt bestehende Einträge einmalig auf).
- `image_url` ist entweder `null` oder eine absolute http(s)-URL.
- `body_len` ist die Länge des reinen Artikeltexts; die Volltextsuche läuft über Titel,
  Zusammenfassung und reinen Artikeltext.

### Abruf und Wiederholungen

- Feeds maximal 10 MB, Artikelseiten maximal 5 MB. Angefordert wird unkomprimiert
  (`Accept-Encoding: identity`); schickt ein Server trotzdem gzip/deflate (auch rohes deflate), wird
  selbst und begrenzt entpackt (das Limit gilt für übertragene und entpackte Bytes); ein abgeschnittener
  komprimierter Datenstrom gilt als vorübergehender Fehler. `identity` und unbekannte Werte im Header
  gelten als unkomprimiert; br, zstd, compress und gestapelte Kodierungen gelten als endgültig `failed`. Ein Abrufversuch dauert höchstens 45 s inklusive
  Umleitungen (maximal 5); bei 403/406/429 oder leerer Antwort folgt ein zweiter Versuch mit anderem
  User-Agent, beide zusammen höchstens 60 s.
- Private, lokale und Metadaten-Adressen werden bei jedem Hop abgelehnt, zusätzlich wird nach dem
  Verbindungsaufbau die tatsächliche Gegenstelle geprüft (DNS-Rebinding). Rest-Risiko: Die Anfrage
  ist dann bereits gesendet, verworfen wird nur die Antwort.
- Die Textextraktion (trafilatura) läuft in 2 eigenen Worker-Prozessen und darf höchstens 90 s reine
  Laufzeit brauchen (es sind nie mehr Aufträge im Pool als Worker, die Wartezeit auf einen freien Worker
  zählt nicht mit). Hängt sie, wird der Worker-Pool beendet und neu angelegt; der Artikel gilt als
  vorübergehend gescheitert (`Extraktion Zeitüberschreitung`, zählt als Versuch). Ein Artikel, dessen
  Extraktion dabei nur mit abgebrochen wurde, bleibt ohne gezählten Versuch sofort fällig. Stirbt ein
  Worker von selbst (z. B. Speicher), zählt der Versuch für alle betroffenen Artikel. Jeder Worker wird
  nach 25 Aufträgen durch einen frischen Prozess ersetzt (begrenzt den Speicherverbrauch).
- Ungültige URLs (z. B. kaputte IPv6-Klammer, Port außerhalb des Bereichs, nicht unterstütztes Schema,
  auch in einer Umleitung) sind sofort endgültig `failed`.
- Bereits gesehene Feed-Einträge (Quelle + GUID) stehen in `seen_guids` und werden nicht erneut angelegt,
  auch nachdem der Artikel gelöscht wurde; diese Einträge bleiben doppelt so lange wie die Artikel.
- Vorübergehende Fehler (Timeouts, Netzwerk- und DNS-Fehler, 5xx, 408/425/429, Größenlimit) werden
  nach 1 h, 4 h und 12 h erneut versucht, danach gilt der Artikel als `failed`. Ein `Retry-After`
  bei 429 wird respektiert (höchstens 12 h). Andere 4xx sind sofort endgültig `failed`.
- Hat ein Artikel schon Text (`partial`/`full`), löscht ein gescheiterter oder leerer neuer Versuch
  ihn nicht: Text und Status bleiben, nur `text_error` wird gesetzt.

### Abruf auf Anforderung

`GET /api/articles/{id}` extrahiert einen `pending`-Artikel sofort, wenn er fällig ist
(`next_attempt_at` leer oder erreicht); sonst kommt der Artikel unverändert zurück. `?retry=true`
holt Artikel mit Status `failed`, `empty` oder `partial` auch außerhalb des Zeitplans neu,
Fehlschläge zählen dabei weiter als Versuche. Derselbe Artikel wird nie gleichzeitig zweimal
geladen; Abrufe auf Anforderung haben eine eigene Grenze von 2 gleichzeitigen Abrufen und warten
nicht auf die Warteschlange des Hintergrundlaufs (ein Hintergrund-Abruf belegt einen Artikel erst,
wenn er selbst an der Reihe ist, und prüft dann erneut, ob der Artikel noch fällig ist). Für die
Textextraktion selbst teilen sie sich die 2 Worker-Prozesse mit dem Hintergrund und warten dort
höchstens auf einen frei werdenden Worker (also höchstens ca. 90 s).
Hat ein `pending`-Artikel schon Text, wird er nach einem Fehlschlag `partial` statt endlos neu versucht.

## Konfiguration

Siehe `.env.example`. `KIOSK_PASSWORD` muss mindestens 12 Zeichen lang sein; der Beispielwert aus
`.env.example` wird abgelehnt. Artikel werden nach `KIOSK_RETENTION_DAYS` (Standard 90) gelöscht, Favoriten bleiben.

| Variable | Standard | Bedeutung |
|---|---|---|
| `KIOSK_COOKIE_SECURE` | `true` | Cookie `__Host-kiosk_session` mit Secure-Flag; für lokale Entwicklung über http `false` (dann `kiosk_session`) |
| `KIOSK_DISABLE_FETCHER` | `false` | Kein automatischer Sammellauf (für Tests, keine Netzwerkzugriffe) |
| `KIOSK_FETCH_INTERVAL` | `900` | Sekunden zwischen Sammelläufen, mindestens 60 |
| `KIOSK_RETENTION_DAYS` | `90` | 1 bis 3650 |
| `KIOSK_EXTRACT_CONCURRENCY` | `6` | Gleichzeitige Hintergrund-Extraktionen, 1 bis 32 |
| `KIOSK_SESSION_DAYS` | `30` | Gültigkeit der Session, 1 bis 365 |
| `KIOSK_DOCS` | `0` | `1` schaltet die API-Doku unter `/docs` und `/openapi.json` ein (nach Login); in Produktion aus |

Werte außerhalb der Grenzen brechen den Start mit einer klaren Fehlermeldung ab.

Sessions sind an das Passwort gebunden: Wird `KIOSK_PASSWORD` geändert, sind alle Sessions ungültig.
Abmelden macht ebenfalls alle Sessions ungültig (Zähler `session_epoch` in der Tabelle `meta`).
Mit `KIOSK_COOKIE_SECURE=true` wird nur `__Host-kiosk_session` akzeptiert, sonst nur `kiosk_session`;
nach einem Umschalten ist also eine neue Anmeldung nötig.
Schreibende API-Aufrufe mit fremdem `Origin` (bzw. `Sec-Fetch-Site: cross-site`) werden mit 403 abgelehnt.

Statische Dateien (auch `sw.js`, Schriften, Icons) werden mit `Cache-Control: no-cache` ausgeliefert
(die URLs sind nicht versioniert; per ETag nachgefragt), `/api/` ohne diesen Kopf.

Die App muss mit genau einem uvicorn-Worker laufen (Login-Bremse im Speicher, eine Sammler-Schleife,
eine SQLite-Verbindung). Das Datenbankschema wird beim Start automatisch migriert (`meta.schema_version`).

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## Deploy

Siehe `deploy/README.md`.
