# Kiosk

Privater Nachrichten-Aggregator: holt RSS-Feeds von über 90 seriösen Zeitungen aus aller Welt,
extrahiert den Volltext der Artikel und stellt alles gebündelt hinter einer Anmeldung bereit.
Mehrere Personen aus dem engsten Kreis teilen sich die gesammelten Artikel, jede mit eigenem
Konto, Lesestand, Profilen, Lieblingszeitungen, Leiste und Einstellungen (siehe Benutzer).

## Aufbau

- `app/` FastAPI-Backend (Python 3.12)
  - `sources.py` lädt den Katalog aus `sources.yaml`
  - `fetcher.py` holt alle 15 Minuten die aktivierten Feeds und extrahiert im Hintergrund den Volltext
  - `extract.py` Volltext per trafilatura, HTML auf eine Whitelist bereinigt, Paywall-Erkennung
  - `db.py` SQLite mit WAL und FTS5-Volltextsuche
  - `api_ui.py` Routen für die Oberfläche: Profile, Lieblingszeitungen, Leiste, Einstellungen, Zähler
  - `stories.py` Geschichten: erkennt Artikel verschiedener Zeitungen über dasselbe Ereignis (de/en)
  - `auth.py` Passwort-Hashes (scrypt), signiertes Cookie pro Benutzer, Login-Bremse
  - `users.py` Konten verwalten auf der Kommandozeile (`python -m app.users`)
- `static/` Web-Oberfläche (PWA ohne Build-Schritt: `index.html`, `js/`, `css/`, `sw.js`)
- `deploy/` Caddy-Block und Deploy-Skript für die Hetzner-Box

## Lokal starten

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
KIOSK_PASSWORD=test-passwort-lokal KIOSK_COOKIE_SECURE=false KIOSK_DOCS=1 .venv/bin/uvicorn app.main:app --reload --port 8100
```

Dann http://127.0.0.1:8100 öffnen und als `admin` mit dem Passwort anmelden, API-Doku unter `/docs`
(nach Login, nur mit `KIOSK_DOCS=1`).

## Benutzer

Die Instanz ist nur für den engsten Kreis gedacht. Es gibt keine Registrierung und kein
„Passwort vergessen“: Konten legt der Betreiber per SSH auf der Kommandozeile an. Die Artikel,
Quellen und der Volltextindex sind gemeinsam; Lesestand, Gemerkt, Später lesen, Profile,
Lieblingszeitungen, Leiste und Einstellungen gehören jedem Benutzer allein.

- Beim ersten Start (oder beim Umbau einer älteren Datenbank auf Schema 6) legt die App den Admin an:
  Name aus `KIOSK_ADMIN_USER` (Standard `admin`), Passwort aus `KIOSK_PASSWORD`. Der bisherige
  Lesestand, die Profile, Favoriten und Einstellungen gehen dabei auf den Admin über. Gibt es schon
  Benutzer, hat `KIOSK_PASSWORD` keine Wirkung mehr (Hinweis im Log).
  Ohne `KIOSK_PASSWORD` den ersten Admin von Hand anlegen: `python -m app.users add <name> --admin`
  bzw. vor dem ersten Start `docker compose run --rm kiosk python -m app.users add <name> --admin`
  (legt die Datenbank bei Bedarf an). Solange es keinen aktiven Admin gibt, wird jeder mit `add`
  angelegte Benutzer automatisch Admin; ohne aktiven Admin warnt die App beim Start im Log.
- Nur Admins dürfen Quellen ein- und ausschalten (`PATCH /api/sources/{id}`, `POST /api/sources/bulk`),
  einen Sammellauf starten (`POST /api/refresh`) und die Server-Werte in `/api/status` sehen; sonst 403.
- Im Container (vom Laptop aus bequemer mit `deploy/user.sh`, siehe `deploy/README.md`):

```bash
python -m app.users list
python -m app.users add <name> [--admin] [--name "Anzeigename"] [--password-stdin]
python -m app.users passwd <name> [--password-stdin]   # beendet alle Sitzungen des Benutzers
python -m app.users disable <name> | enable <name>     # sperren beendet ebenfalls alle Sitzungen
python -m app.users remove <name> [--yes]              # löscht Lesestand, Profile, Favoriten, Einstellungen
python -m app.users admin <name> --on|--off
python -m app.users extra-sources <name> [quelle ...] [--profile-de]   # in_all:false-Quellen in ALLE
```

`extra-sources` setzt die Einstellung `all_extra_sources` (siehe Filter): Die genannten Quellen (nur
solche mit `in_all: false`, z. B. `focus`) stehen für diesen Benutzer in ALLE; ohne Quelle stehen keine
mehr drin. `--profile-de` nimmt sie zusätzlich in sein Profil Deutschland auf (herausnehmen geht in der
Oberfläche). Die laufende App übernimmt das sofort. Beispiel: `python -m app.users extra-sources reiner
focus --profile-de`.

Benutzernamen: 2 bis 32 Zeichen aus `a-z`, `0-9`, `.`, `_`, `-` (Groß- und Kleinschreibung zählt beim
Anmelden nicht). Passwörter: mindestens 12 Zeichen ohne Leerzeichen am Rand, nicht gleich dem
Benutzernamen, gespeichert als scrypt-Hash
(`scrypt$n$r$p$salt$hash`, n=2^14, r=8, p=1). Ohne `--password-stdin` wird das Passwort zweimal
verdeckt abgefragt. Anzeigenamen: höchstens 64 Zeichen (nach NFC-Normalisierung), keine
Steuerzeichen. Der Admin aus `KIOSK_PASSWORD` heißt anfangs wie sein Benutzername mit großem
Anfangsbuchstaben („Admin“). Der letzte aktive Admin lässt sich nicht löschen, sperren oder herabstufen.
`remove` fragt nach (ohne Terminal nur mit `--yes`). Neue Konten bekommen die vier vorgegebenen Profile.

## API (Kurzfassung)

| Methode | Pfad | Zweck |
|---|---|---|
| POST | `/api/login` `{username, password}` | Anmelden (204), setzt Cookie; Fehler immer 401 „Benutzername oder Passwort falsch“, 429 bei zu vielen Versuchen |
| POST | `/api/logout` | Abmelden; beendet alle Sitzungen dieses Benutzers auf allen Geräten |
| GET | `/api/session` | Ohne Login: `{authenticated, user}`, `user` ist `{id, username, display_name, is_admin}` oder `null` |
| GET | `/api/me` | Der angemeldete Benutzer `{id, username, display_name, is_admin}` |
| GET | `/api/sources` | Alle Quellen mit Status und Zählern, Kurzname `short`, `is_favorite` |
| PATCH | `/api/sources/{id}` `{enabled}` | Quelle ein-/ausschalten (nur Admin, sonst 403) |
| POST | `/api/sources/bulk` `{enabled:[], disabled:[]}` | Mehrere Quellen umschalten (nur Admin) |
| GET | `/api/articles?source=&country=&profile=&q=&unread=&starred=&later=&free=&days=&before=&offset=&limit=` | Artikelliste, siehe Blättern und Filter |
| GET | `/api/articles/{id}` | Artikel mit Volltext, siehe Abruf auf Anforderung |
| GET | `/api/articles/{id}/next?source=&country=&profile=&unread=true&starred=&later=&free=&days=` | Nächster älterer Artikel im selben Filterkontext: `{item}` oder `{item: null}` |
| GET | `/api/articles/{id}/related` | Rechte Seite zum Artikel: `{story_id, sources_count, same_story, topic}`, siehe Geschichten |
| GET | `/api/overview` | Tagesübersicht: `{date, now_hour, counts, top_stories, hourly}`, siehe Geschichten |
| GET | `/api/frontpage` | Titelseite: `{lead, sections}`, siehe Geschichten |
| POST | `/api/articles/{id}/read` `{value}` | Gelesen-Status |
| POST | `/api/articles/{id}/star` `{value}` | Favorit |
| POST | `/api/articles/{id}/later` `{value}` | Später lesen |
| POST | `/api/articles/read-all` `{sources?, source?, profile?, max_id?}` | Alles als gelesen markieren (Angaben schränken ein, Schnittmenge; `max_id`: nur Artikel mit ID ≤ max_id, die Oberfläche schickt die höchste angezeigte ID); `{marked}` zählt nur Artikel ohne Paywall |
| GET | `/api/profiles` | Profile nach `position`, mit `source_count` und `unread` (siehe Profile) |
| POST | `/api/profiles` `{name, sources, terms, in_bar, in_chips}` | Profil anlegen (201); `sources: null` = alle aktiven, `[]` ist ungültig (422) |
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
| POST | `/api/refresh` | Sammellauf sofort starten (nur Admin) |
| GET | `/api/status` | Admin: Build-Zeitstempel (`build`), Sammler-Status (`fetcher`), Datenbank (`db`), Einstellungen (`settings`); andere Benutzer nur `{build, fetcher}` |
| GET | `/api/build` | Ohne Login: `{build}` (Version des Service Workers) |
| GET | `/favicon.ico` | Ohne Login: Icon |
| GET | `/healthz` | Ohne Login; 503, wenn die Datenbank nicht antwortet, die Sammler-Schleife beendet ist (`fetcher`) oder hängt (`fetcher_stalled`: seit mehr als 3 Intervallen + 10 min kein Lauf fertig, bzw. der laufende Lauf dauert schon über 3 h). Im zweiten und dritten Fall beendet ein Wachhund den Prozess nach spätestens 1 min (Exit-Code 3), Docker startet neu |

Alle Artikel-Flags (`read`, `starred`, `later`), Listen, Zähler, Profile, Lieblingszeitungen, Leiste
und Einstellungen gelten für den angemeldeten Benutzer; Profile anderer Benutzer liefern 404.

Artikelfelder: `text_status` ist `full`, `partial` (Paywall oder sehr kurz), `empty`, `failed` oder `pending`.
`paywalled` kommt aus der Schema.org-Markierung der Verlage (`isAccessibleForFree: false`), bei GEA+
aus dem unscharf dargestellten Ersatzabsatz (`nfy-text-wrapper-blur` bzw. `aria-label="Unkenntlich
gemachter Text"`). Absätze mit Blindtext („Lorem ipsum dolor sit amet …“) fallen aus dem Text.

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

- „Alle Quellen“ heißt überall, wo keine Quelle ausdrücklich genannt ist (ALLE-Liste, „nächster
  Artikel“, `read-all` ohne Quelle, Suche ohne Quelle, `country` ohne Quelle, Profile mit
  `sources: null`, der ALLE-Zähler in `/api/bar` und `unread` in `/api/counts`): die aktivierten Quellen
  ohne die mit `in_all: false` in `sources.yaml` (heise Security, deren Meldungen auch bei heise online
  stehen, und FOCUS online). Ausdrücklich genannt zählen sie: `source=heisesec`, Profile mit Quellenliste, die
  Zähler in `/api/sources`, Lieblingszeitungen und ihre Chips in der Leiste. Gemerkt
  (`starred`/`later`) zeigt wie bisher alles Gemerkte. Das vorgegebene Profil Deutschland lässt solche
  Quellen beim Anlegen weg.
- Je Benutzer lassen sich solche Quellen doch in „alle Quellen“ aufnehmen: Einstellung
  `all_extra_sources` (ohne Oberfläche; `python -m app.users extra-sources`). Das gilt dann für ihn
  überall oben, auch in Tagesübersicht, Titelseite und rechter Seite; `in_all` in `/api/sources` ist
  der Wert für den angemeldeten Benutzer (`true` für seine zusätzlichen Quellen). Profil-Zähler im
  Zwischenspeicher gelten nur für dieselbe Quellenmenge.
- `profile=<id>`: Quellen des Profils (geschnitten mit den aktivierten Quellen; `sources: null` heißt
  alle aktivierten außer `in_all: false`) und, falls vorhanden, Suchbegriffe: jeder Begriff als Präfix-Phrase über den
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
  `terms` 0 bis 20 Begriffe à 1 bis 40 Zeichen ohne `"`; `in_bar` und `in_chips` boolesch. Fehler: 422.
- Wo ein Profil erscheint, stellen zwei Schalter getrennt ein: `in_chips` (Standard `true`) zeigt es oben
  als Filterchip der Liste, `in_bar` (Standard `false`) unten in der Leiste. Das gerade gewählte Profil
  steht in der Chipzeile auch ohne `in_chips`, damit man es sieht und abwählen kann. Alle Antworten mit
  Profilen (`GET`/`POST /api/profiles`, `PATCH`, `reorder`) enthalten beide Felder.
- Alle Ungelesen-Zähler (`unread` in Leiste, Profilen, Lieblingszeitungen, `/api/counts` und
  `/api/sources`) zählen nur Artikel des laufenden Tages ab 00:00 Ortszeit (`KIOSK_TZ`), um Mitternacht
  beginnen sie neu. Die Ungelesen-Liste, „Nächster ungelesener“ und „alles gelesen“ nehmen weiter alle
  ungelesenen Artikel; `/api/sources` liefert dafür zusätzlich `unread_all`. `unread` in `/api/status`
  zählt ebenfalls alle. Maßgeblich ist die Erscheinungszeit (`published_at`), nicht die Abrufzeit: Artikel
  von gestern, die erst heute geholt werden (neu eingeschaltete Quelle, Feed nach Störung), stehen in
  der Ungelesen-Liste, aber nicht im Zähler.
- `unread` eines Profils zählt ungelesene Artikel ohne Paywall (höchstens 5000) und wird höchstens alle
  60 s neu berechnet; Änderungen an Profilen oder Quellen, „alles gelesen“, das Setzen des
  Gelesen-Status eines Artikels, ein beendeter Sammellauf und ein neuer Tag verwerfen den
  Zwischenspeicher sofort. Auch `unread` in `/api/sources` zählt nur Artikel ohne Paywall.
- `unread` eines Profils kann `null` sein („nicht gezählt“), und zwar in `GET /api/profiles`,
  `POST /api/profiles`, `PATCH /api/profiles/{id}`, `POST /api/profiles/reorder` (je Profil `unread`) und
  in `GET`/`PUT /api/bar` (`unread` der Einträge mit `type: profile`). Alle Profil-Zähler einer Anfrage
  haben zusammen höchstens 3 s; was dann noch fehlt, ist `null` und wird bei der nächsten Anfrage
  gezählt. Braucht die Zählung eines Profils allein länger als 2 s, bleibt es 10 Minuten lang `null`
  (Lesestand-Änderungen setzen das nicht zurück, eine Änderung des Profils schon). Alle anderen
  `unread`-Werte (`ALLE` und Zeitungen in der Leiste, `/api/sources`, `/api/counts`) sind nie `null`.
- Leiste: `ALLE` zuerst, danach Lieblingszeitungen und Profile mit `in_bar` in der Reihenfolge aus
  `bar_order`; nicht dort genannte hängen hinten an (erst Zeitungen, dann Profile). `label` ist für
  Zeitungen `short` aus `sources.yaml` (höchstens 12 Zeichen, Versalien), für Profile der Name in Versalien.
- Einstellungen: `theme` (`system`/`dark`/`light`), `text_size` (`s`/`m`/`l`), `mark_read_on_open`,
  `show_images`, `free_only` (boolesch, streng), `bar_order` (Liste aus `source:<id>`/`profile:<id>`),
  `desktop_empty` (`overview`/`frontpage`: was am Desktop rechts steht, wenn kein Artikel offen ist),
  `focus_mode` (boolesch, Fokusmodus am Desktop), `all_extra_sources` (Liste von Quellen-IDs mit
  `in_all: false`, die für den Benutzer in ALLE stehen, siehe Filter; höchstens 200, Dubletten fallen
  weg, unbekannte Quellen: 400). Standard: `dark`, `m`, `true`, `true`, `true`, `[]`,
  `overview`, `false`, `[]`. Unbekannte Felder, andere Werte oder `null`: 422; ein ungültiger gespeicherter
  Wert zählt nicht (Standard), eine gespeicherte, nicht mehr vorhandene Quelle auch nicht.
  `free_only` bleibt nur aus Kompatibilität: `false` wird angenommen und gespeichert, hat aber keine
  Wirkung (Paywall-Artikel sind nie sichtbar); die Oberfläche zeigt den Schalter nicht mehr.
- `counts` zählt `unread`, `later` und `starred` immer ohne Paywall-Artikel (wie die Listen).

### Geschichten (rechte Seite am Desktop)

- `app/stories.py` gruppiert nach jedem Abruf der Feeds und einmal beim Start alle Artikel der letzten
  48 h: Artikel verschiedener Zeitungen (auch deutsch und englisch gemischt) über dasselbe Ereignis
  bekommen dieselbe `articles.story_id` (`NULL` = keine Geschichte). Eine neue Geschichte bekommt die
  kleinste Artikel-ID der Gruppe; eine bestehende behält ihre ID, solange ihre Artikel zusammenbleiben
  (auch wenn der erste aus dem Fenster wandert oder ein kleinerer dazukommt), aber nie über mehr als 36 h:
  Liegt ihr ältester Artikel weiter zurück, beginnt eine neue Geschichte (die älteren Artikel behalten die
  alte ID, außer sie stammen dann nur noch aus einer Zeitung). Ohne neue Artikel gruppiert der Sammler
  höchstens alle 30 Minuten neu.
  Eine Geschichte braucht mindestens zwei Zeitungen, ihre Artikel liegen höchstens 36 h auseinander.
  Gruppiert wird nach Ereignis, nicht nach Thema: zwei Beben, zwei Streiks oder zwei Goldmedaillen am
  selben Tag bleiben getrennt, ebenso zwei Geschichten über dieselbe Person. Kommentare und Analysen
  (Titel oder URL) gehören nur bei sehr hoher Ähnlichkeit dazu und verbinden nie andere Artikel;
  Liveblogs, Ticker, Newsletter und Sendungen gehören nie zu einer Geschichte. Namen allein verbinden
  nicht (Spielbericht und Vertragsverlängerung desselben Vereins), verschiedene Spielstände trennen.
  Verfahren und Messung stehen in `app/stories.py` und `tests/test_stories.py` (vier
  zeitlich getrennte, beschriftete Fenster echter Feed-Einträge unter `tests/fixtures/`, darunter zwei
  mit Artikeln, die es beim Einstellen noch nicht gab). Das Rechnen läuft in einem Thread, Fehler beenden
  den Sammler nie; ein Lauf, der während eines anderen fällig wird, folgt gleich danach. Der Volltext zählt
  nur bei fehlendem oder sehr kurzem Anreißer (unter 80 Zeichen); kam so ein Volltext bei der Extraktion,
  gruppiert der Sammler gleich noch einmal.
- `sources_count` ist die Zahl der Zeitungen: verschiedene Verlage, also zählen die Ausgaben von
  The Local (Deutschland, Österreich, ...) und heise/heise Security je einmal, dieselbe Agenturmeldung
  in zwei Zeitungen aber doppelt. Gezählt werden nur Artikel, die zum Zeitraum passen (Übersicht und
  Aufmacher: heute; zum offenen Artikel: höchstens 36 h davor oder danach). Alle Listen hier zeigen nur
  Quellen wie ALLE (aktiviert, ohne `in_all: false` außer `all_extra_sources`), keine Paywall-Artikel, und beachten die
  Aufbewahrungsgrenze; die Einträge haben die Form der Artikelliste.
- `GET /api/articles/{id}/related` (404 wie `GET /api/articles/{id}`): `same_story` sind die anderen
  Artikel der Geschichte aus anderen Zeitungen (die eigene fehlt), höchstens 12, je Zeitung einer, in
  der Reihenfolge der Vertreter (siehe unten); wörtliche Kopien des offenen Artikels fehlen, ebenso
  anderssprachige Artikel, die mit dem offenen nicht erkennbar dasselbe erzählen (zwei gemeinsame
  Inhaltswörter in Titel und erstem Satz oder sehr hohe Ähnlichkeit). Ohne Geschichte (`story_id:
  null`) stehen hier Artikel anderer Zeitungen, die trotzdem dasselbe Ereignis erzählen (ähnlich genug
  für eine Kante, höchstens 36 h auseinander). Eine Geschichte, die im Zeitraum nur eine Zeitung
  hat, gilt als keine. `sources_count` zählt die Zeitungen, die die Seite zeigt:
  die des offenen Artikels und die gelisteten (ohne Treffer 1). `topic` sind bis zu 5 Artikel der
  letzten 7 Tage zum selben Thema, aber ein anderes Ereignis: nie derselben Geschichte, nie aus derselben
  Zeitung, nie ein Artikel, der das Ereignis erzählt, ohne in der Geschichte zu stehen (ähnlich wie zwei
  ihrer Artikel oder, ohne Geschichte, wie der offene; oder ihr Stichwort wie „Fairford“ im Titel), nie
  Liveblogs, Ticker, Newsletter oder Sendungen; das Thema muss im Titel stehen, ein besonderes Wort
  (Name, Ort) gemeinsam haben, höchstens einer je Geschichte und je Ereignis, höchstens 2 je Zeitung.
  Zu einem Liveblog oder einer Sendung gibt es keine Themen.
- `GET /api/overview`: `date` und `now_hour` in `KIOSK_TZ`; `counts` = `{new_today, unread_today, later,
  active_sources}` (heute ab 00:00 `KIOSK_TZ`; `unread_today` wie der ALLE-Zähler, `later` wie in
  `/api/counts`, `active_sources` = Zeitungen mit Artikeln heute); `top_stories` bis zu 5 Geschichten mit
  mindestens 3 Zeitungen heute, meiste zuerst, dann die neueste: `{story_id, sources_count, sources,
  article}` mit bis zu 4 Kurznamen (einer je Zeitung) in der Reihenfolge der Auswahl. Hat heute weniger
  als 3 solche Geschichten (kurz nach Mitternacht), kommen sie aus den letzten 24 Stunden, falls das
  mehr sind: `window` ist dann `"24h"`, sonst `"today"`; Zähler und Stunden bleiben bei heute. `hourly` =
  24 Zahlen (Artikel je Stunde heute, wie `new_today`). Vertreter einer Geschichte: ein Artikel, der sie
  erzählt (mittig: mindestens 85 % der größten Ähnlichkeit zum Schwerpunkt der Gruppe), darunter
  deutschsprachig vor anderen, mit Bild, mit Volltext, dann der neueste. Reicht das Zeitbudget für die
  Zähler nicht, sind `unread_today` und `later` `null`; für die Artikel des Tages nicht, sind `new_today`
  und `active_sources` `null` und `hourly` leer (`[]`).
- `GET /api/frontpage`: `date` und `now_hour` in `KIOSK_TZ` wie in der Übersicht; `lead` ist die
  Geschichte mit den meisten Zeitungen heute (mindestens 2; gibt es heute keine, die der letzten 24
  Stunden, dann `window: "24h"`, sonst `"today"`; ohne beides `null`), Vertreter bevorzugt mit Bild:
  `{story_id, sources_count, article}`. `sections` für die Profile mit `in_chips` (keines: alle), in ihrer
  Reihenfolge, höchstens die ersten 4: `{profile_id, name, items: [{article, sources_count}]}` mit bis zu
  3 Artikeln der letzten 24 h, je Geschichte einer (Vertreter wie oben, das Bild vor der Sprache), meiste
  Zeitungen zuerst, dann die neuesten. Der Aufmacher und schon gezeigte Geschichten fehlen in späteren
  Rubriken; Rubriken ohne Artikel fallen weg. Gruppiert wird in SQL über alle Artikel der 24 h.
- Übersicht, Titelseite und die Seite zum Artikel (`related`) haben je höchstens 3 s für ihre Abfragen;
  was dann fehlt (eine Rubrik, die Geschichten des Tages, die Stunden, dieselbe Geschichte, die Themen),
  bleibt leer statt einer 503.

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

- Feeds und Artikelseiten maximal 5 MB; eine größere Artikelseite ist sofort endgültig `failed`. Von
  einem Feed zählen nur die 300 neuesten Einträge (nach Datum, Einträge ohne Datum dahinter), je Quelle
  und Lauf kommen höchstens 150 neue Artikel dazu (der Rest beim nächsten Lauf; wird die Grenze erreicht,
  speichert der Abruf kein ETag/Last-Modified, damit kein 304 den Rest zurückhält). feedparser läuft in
  den Worker-Prozessen der Textextraktion (höchstens 30 s, sonst wie eine hängende Extraktion).
  ETag und Last-Modified werden nur gespeichert und gesendet, wenn sie aus druckbarem ASCII bestehen;
  ein gescheiterter Abruf löscht beide (der nächste Abruf geht ohne Bedingung). Angefordert wird unkomprimiert
  (`Accept-Encoding: identity`); schickt ein Server trotzdem gzip/deflate (auch rohes deflate), wird
  selbst und begrenzt entpackt (das Limit gilt für übertragene und entpackte Bytes); ein abgeschnittener
  komprimierter Datenstrom gilt als vorübergehender Fehler. `identity` und unbekannte Werte im Header
  gelten als unkomprimiert; br, zstd, compress und gestapelte Kodierungen gelten als endgültig `failed`. Ein Abrufversuch dauert höchstens 45 s inklusive
  Umleitungen (maximal 5); bei 403/406/429 oder leerer Antwort folgt ein zweiter Versuch mit anderem
  User-Agent, beide zusammen höchstens 60 s.
- `feed` in `sources.yaml` darf statt RSS/Atom auch eine Google-News-Sitemap (`urlset`) oder ein
  Sitemap-Index sein, erkannt am Wurzelelement im sitemaps.org-Namensraum (GEA). Aus einem Index wird
  die Sitemap mit dem jüngsten `lastmod` genommen (bei gleichem oder fehlendem Datum die mit der
  lexikographisch größten URL, bei GEA also das neueste Quartal), nur auf dem Host des Index
  und auf Port 80/443, höchstens eine je Lauf und über denselben geprüften Abruf wie jeder Feed; ein
  Index, der wieder auf einen Index zeigt, gilt als Fehler. Titel und Datum kommen aus `news:title`
  und `news:publication_date` (sonst `lastmod`), das Bild aus `image:loc`, eine Zusammenfassung gibt
  es nicht. Geparst wird mit expat im Worker-Prozess (höchstens 30 s wie feedparser); DOCTYPE und
  Entity-Deklarationen werden abgelehnt. Für Sitemaps werden nie ETag/Last-Modified gespeichert (ein
  304 auf den Index sagte nichts über die eigentliche Liste), jeder Abruf geht ohne Bedingung.
- `include_paths` (optional, je Quelle in `sources.yaml`): Liste regulärer Ausdrücke; nur Einträge,
  deren URL-Pfad auf einen davon passt (`re.search`), werden angelegt. Gilt für Feeds und Sitemaps,
  vor der Grenze von 300 Einträgen und vor dem Zählen neuer Artikel (GEA: nur Lokalressorts; FOCUS
  online per negativem Lookahead ohne `kultur/stars`, `kultur/kino-tv`, `kultur/royals`, `die-debatte`,
  `tagesthema` und die Ratgeber `gesundheit`, `familie`, `panorama/oktoberfest`, `reisen/fernreisen`,
  `finanzen/altersvorsorge`, `finanzen/karriere`, `immobilien/kaufen`, `auto/fahrberichte`). Hat der Filter alles verworfen, gilt der Abruf als erfolgreich (0 neue), ebenso
  eine leere Sitemap.
- `guid_pattern` (optional): regulärer Ausdruck mit genau einer Gruppe, auf die Eintrags-URL angewandt;
  die Gruppe ist die GUID (GEA: `_arid,(\d+)`, die Artikelnummer), passt er nicht, bleibt die übliche.
- Private, lokale und Metadaten-Adressen werden bei jedem Hop abgelehnt, zusätzlich wird nach dem
  Verbindungsaufbau die tatsächliche Gegenstelle geprüft (DNS-Rebinding). Rest-Risiko: Die Anfrage
  ist dann bereits gesendet, verworfen wird nur die Antwort. Erlaubt sind nur die Standard-Ports
  (80 für http, 443 für https); andere Ports sind sofort endgültig `failed`. Weitere Ziele sperrt
  `KIOSK_FETCH_DENY_IPS` (z. B. öffentliche Adressen des eigenen Servers), geprüft wie die privaten
  Adressen bei der Namensauflösung und an der Gegenstelle.
- Extrahierter Text: höchstens ca. 500 000 Zeichen bereinigtes HTML (danach endet er am nächsten
  Element, Status `partial`) und höchstens 100 Bilder.
- Die Textextraktion (trafilatura) läuft in 2 eigenen Worker-Prozessen und darf höchstens 90 s reine
  Laufzeit brauchen (es sind nie mehr Aufträge im Pool als Worker, die Wartezeit auf einen freien Worker
  zählt nicht mit). Hängt sie, wird der Worker-Pool beendet und neu angelegt; der Artikel gilt als
  vorübergehend gescheitert (`Extraktion Zeitüberschreitung`, zählt als Versuch). Ein Artikel, dessen
  Extraktion dabei nur mit abgebrochen wurde, bleibt ohne gezählten Versuch sofort fällig. Stirbt ein
  Worker von selbst (z. B. Speicher, auch durch einen feindlichen Feed im selben Pool), ist der Schuldige
  unbekannt: Betroffene Artikel werden beim ersten Mal ohne gezählten Versuch neu eingeplant, erst ein
  zweiter Absturz mit demselben Artikel zählt. Jeder Worker wird
  nach 25 Aufträgen durch einen frischen Prozess ersetzt (begrenzt den Speicherverbrauch).
- Ungültige URLs (z. B. kaputte IPv6-Klammer, Port außerhalb des Bereichs, nicht unterstütztes Schema,
  auch in einer Umleitung) sind sofort endgültig `failed`.
- Bereits gesehene Feed-Einträge (Quelle + GUID) stehen in `seen_guids` und werden nicht erneut angelegt,
  auch nachdem der Artikel gelöscht wurde. Solange ein Eintrag im Feed steht, wird sein Zeitstempel
  (höchstens täglich) aufgefrischt; gelöscht wird er erst, wenn er doppelt so lange wie die
  Aufbewahrungsfrist nicht mehr im Feed stand. Nur für das Muster der BBC (sie hängt eine wechselnde Nummer an
  die GUID: „…#1“, „…#8“): Ist die GUID ohne „#…“ die Adresse oder die GUID ohne „#…“ eines Artikels
  derselben Quelle mit derselben Adresse und demselben Titel, der weniger als 6 Stunden davor oder danach
  erschien, ist es derselbe Artikel und wird nicht angelegt. Ein täglicher Eintrag unter fester Adresse
  mit neuem Titel und stündliche Sendungen mit fester Adresse und festem Titel, aber eigener GUID bleiben neu.
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
höchstens auf einen frei werdenden Worker (also höchstens ca. 90 s). Höchstens 20 Abrufe auf
Anforderung laufen oder warten gleichzeitig; darüber hinaus startet keiner (`extracting: false`).
`?retry=true` startet frühestens 2 Minuten nach dem letzten Abruf auf Anforderung desselben Artikels neu.
Hat ein `pending`-Artikel schon Text, wird er nach einem Fehlschlag `partial` statt endlos neu versucht.

## Konfiguration

Siehe `.env.example`. `KIOSK_PASSWORD` (Startpasswort des Admins, siehe Benutzer) muss, wenn gesetzt,
mindestens 12 Zeichen lang sein; der Beispielwert aus `.env.example` wird abgelehnt. Ohne Benutzer in der
Datenbank und ohne `KIOSK_PASSWORD` startet die App nicht. Artikel werden nach `KIOSK_RETENTION_DAYS` (Standard 90) gelöscht;
was irgendein Benutzer als Favorit oder „Später lesen“ gemerkt hat, bleibt erhalten, ist aber nach
Ablauf der Aufbewahrung nur noch für die Benutzer sichtbar, die es selbst gemerkt haben (Liste,
Suche, Zähler, Artikel per ID).

| Variable | Standard | Bedeutung |
|---|---|---|
| `KIOSK_ADMIN_USER` | `admin` | Name des Admins, den `KIOSK_PASSWORD` beim ersten Start anlegt |
| `KIOSK_COOKIE_SECURE` | `true` | Cookie `__Host-kiosk_session` mit Secure-Flag; für lokale Entwicklung über http `false` (dann `kiosk_session`) |
| `KIOSK_DISABLE_FETCHER` | `false` | Kein automatischer Sammellauf (für Tests, keine Netzwerkzugriffe) |
| `KIOSK_FETCH_INTERVAL` | `900` | Sekunden zwischen Sammelläufen, mindestens 60 |
| `KIOSK_RETENTION_DAYS` | `90` | 1 bis 3650 |
| `KIOSK_EXTRACT_CONCURRENCY` | `6` | Gleichzeitige Hintergrund-Extraktionen, 1 bis 32 |
| `KIOSK_SESSION_DAYS` | `30` | Gültigkeit der Session, 1 bis 365 |
| `KIOSK_TZ` | `Europe/Berlin` | Zeitzone (IANA-Name), in der der Tag für die Ungelesen-Zähler um 00:00 beginnt |
| `KIOSK_DOCS` | `0` | `1` schaltet die API-Doku unter `/docs` und `/openapi.json` ein (nach Login); in Produktion aus |
| `KIOSK_SECRET` | – | Secret der Session-Cookies, mindestens 32 Zeichen; sonst wird `/data/secret.key` einmalig erzeugt |
| `KIOSK_FETCH_DENY_IPS` | – | Zusätzlich gesperrte Ziele für Feed- und Artikelabrufe: IPs oder Netze (CIDR), kommagetrennt, z. B. die Adressen des eigenen Servers `203.0.113.10,2001:db8:1234:5678::/64` (kein ganzes Provider-Netz wie `2a01:4f8::/32`, das sperrte auch dort gehostete Nachrichtenseiten); `deploy.sh` trägt diesen Wert ein, wenn er in der `.env` fehlt |

Werte außerhalb der Grenzen, unbekannte Zeitzonen, ungültige Einträge in `KIOSK_FETCH_DENY_IPS` und ein
Secret unter 32 Zeichen (auch eine leere oder zu kurze `secret.key`) brechen den Start mit einer klaren
Fehlermeldung ab. Die App legt Dateien nur für den eigenen Benutzer lesbar an (umask 077).

Schutz der einen Event-Loop: Jede Leseabfrage während einer API-Anfrage darf höchstens 2 s laufen,
sonst bricht SQLite sie ab (503, bei der Suche „Die Suche dauert zu lange“). Hintergrundarbeit
(Sammler, Extraktion, Aufräumen, Migration) hat keine solche Frist. Die Suche nutzt höchstens 8 Wörter; Präfix-Suche gilt
erst ab 3 Zeichen, kürzere Wörter (auch in Profil-Begriffen) zählen nur exakt. Jeder Benutzer hat
höchstens 50 Profile (409).

Sessions gehören einem Benutzer (signiertes Cookie mit Benutzer-ID, `users.session_epoch` und einem
Fingerabdruck des Passwort-Hashes). Abmelden und `passwd` erhöhen die Epoche und beenden damit alle
Sitzungen dieses Benutzers, andere bleiben angemeldet; gesperrte Benutzer bekommen 401. Abmelden
gilt also auf allen Geräten dieses Benutzers (Handy, Laptop), nicht nur im aktuellen Browser.
Login-Bremse: höchstens 8 Versuche in 15 Minuten je IP (IPv6: je /64-Netz) und 30 je Benutzername,
dazu höchstens 200 pro Minute insgesamt (danach 429). Namen, die nicht den Regeln für Benutzernamen
entsprechen, teilen sich einen gemeinsamen Zähler. Jeder Versuch zählt schon vor der Passwortprüfung (auch
parallele Anfragen überholen die Bremse nicht); ein erfolgreicher Login setzt die Zähler für IP und
Namen zurück. Höchstens zwei scrypt-Prüfungen laufen gleichzeitig. Abwägung: Wer viele IPs hat, kann
einen Benutzernamen mit 30 falschen Versuchen für 15 Minuten sperren; die höhere Grenze je Name
macht das teurer, ohne Passwort-Raten über viele IPs wirklich zu erleichtern.
Mit `KIOSK_COOKIE_SECURE=true` wird nur `__Host-kiosk_session` akzeptiert, sonst nur `kiosk_session`;
nach einem Umschalten ist also eine neue Anmeldung nötig.
Schreibende API-Aufrufe mit fremdem `Origin` (bzw. `Sec-Fetch-Site: cross-site`) werden mit 403 abgelehnt.

Statische Dateien (auch `sw.js`, Schriften, Icons) werden mit `Cache-Control: no-cache` ausgeliefert
(die URLs sind nicht versioniert; per ETag nachgefragt), `/api/` ohne diesen Kopf.

Die App muss mit genau einem uvicorn-Worker laufen (Login-Bremse im Speicher, eine Sammler-Schleife,
eine SQLite-Verbindung). Das Datenbankschema wird beim Start automatisch migriert (`meta.schema_version`,
aktuell 8: Tabellen `users` und `user_articles`, Profile/Favoriten/Einstellungen mit `user_id`; seit 7
`profiles.in_chips`, bestehende Profile bekommen 1; seit 8 `articles.story_id` mit Index, gefüllt vom
ersten Lauf nach dem Start). Die alten
Spalten `articles.read/starred/later` bleiben physisch stehen, werden aber nicht mehr benutzt.
Den Test der Migration auf einer Kopie der Produktionsdatenbank aktiviert
`KIOSK_PRODCOPY_DB=/pfad/zur/kopie.db .venv/bin/python -m pytest tests/test_users.py`.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## Deploy

Siehe `deploy/README.md`.
