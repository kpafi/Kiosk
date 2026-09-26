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
```

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
| POST | `/api/refresh` | Sammellauf sofort starten (nur Admin) |
| GET | `/api/status` | Admin: Build-Zeitstempel (`build`), Sammler-Status (`fetcher`), Datenbank (`db`), Einstellungen (`settings`); andere Benutzer nur `{build, fetcher}` |
| GET | `/api/build` | Ohne Login: `{build}` (Version des Service Workers) |
| GET | `/favicon.ico` | Ohne Login: Icon |
| GET | `/healthz` | Ohne Login; 503, wenn die Datenbank nicht antwortet, die Sammler-Schleife beendet ist (`fetcher`) oder hängt (`fetcher_stalled`: seit mehr als 3 Intervallen + 10 min kein Lauf fertig, bzw. der laufende Lauf dauert schon über 3 h). Im zweiten und dritten Fall beendet ein Wachhund den Prozess nach spätestens 1 min (Exit-Code 3), Docker startet neu |

Alle Artikel-Flags (`read`, `starred`, `later`), Listen, Zähler, Profile, Lieblingszeitungen, Leiste
und Einstellungen gelten für den angemeldeten Benutzer; Profile anderer Benutzer liefern 404.

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
  Aufbewahrungsfrist nicht mehr im Feed stand.
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
aktuell 6: Tabellen `users` und `user_articles`, Profile/Favoriten/Einstellungen mit `user_id`). Die alten
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
