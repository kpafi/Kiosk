# Kiosk: Plan für Frontend und Backend-Erweiterungen

Stand 2026-09-26. Dieser Plan ist die Vorgabe für die Coder-Agenten. Was hier steht, gilt; Abweichungen werden im Bericht begründet.

## 0. Ziel

Die in `design/` (Claude-Design-Canvas, Design System) vereinbarte Oberfläche als echte Web-App bauen, die gegen das bestehende FastAPI-Backend läuft, plus die dafür nötigen Backend-Erweiterungen. Ein Nutzer, privat, Handy zuerst (390px), Desktop ab 900px zweispaltig. Dunkel ist Standard.

## 1. Architektur-Entscheidungen

- **Frontend ohne Build-Schritt**: statisches HTML, CSS und ES-Module unter `static/`, ausgeliefert vom bestehenden `StaticFiles`-Mount. Routing per Hash (`#/`, `#/a/123`, `#/s/spiegel`, `#/p/4`, `#/suche?q=…`, `#/gemerkt`, `#/einstellungen`, `#/quellen`, `#/profile`, `#/profile/neu`, `#/profile/4`, `#/login`). Kein Framework, keine Abhängigkeiten von CDNs (CSP: `script-src 'self'`, `connect-src 'self'`).
- **Schriften selbst gehostet**: Doto (700), Space Grotesk (400, 500, 600), Space Mono (400) als woff2 unter `static/fonts/` mit den OFL-Lizenzdateien. Die Caddy-CSP bleibt unverändert (`font-src` fällt auf `default-src 'self'`).
- **Themes über CSS-Variablen** aus `design/tokens.json`: `:root` = dark, `[data-theme="light"]` = light. Umschaltung: Einstellung `system|dark|light`; bei `system` folgt `prefers-color-scheme`. `<meta name="theme-color">` wird mitgeführt.
- **Sicherheit im Client**: Titel, Teaser, Autor, Quellennamen, Suchbegriffe, Profilnamen immer als `textContent`. Nur `body_html` (serverseitig bereinigt) darf per `innerHTML` in den Artikelcontainer. Suchtreffer-Hervorhebung arbeitet auf Textknoten, nie mit String-Konkatenation zu HTML. Alle POST/PATCH/DELETE-Aufrufe schicken `Content-Type: application/json` (Origin-Prüfung im Backend bleibt).
- **PWA light**: `manifest.webmanifest` (Name Kiosk, schwarz, Icons als SVG/PNG in Punktmatrix-Optik, `display: standalone`) und ein minimaler Service Worker, der nur die App-Shell (index, css, js, fonts) cached, nie API-Antworten. Kein Offline-Lesen in dieser Version.
- **Paywall**: Artikel mit `paywalled=1` werden nie angezeigt. Das Backend filtert standardmäßig (siehe 2.7), das Frontend schickt zusätzlich `free=true`.

## 2. Backend-Erweiterungen (API-Vertrag)

Alle Routen unter `/api/`, Login wie bisher. Schema-Migration auf **v4** in `app/db.py` (idempotent, eine Transaktion, wie v2/v3). Neue Tabellen und Spalten:

```
ALTER TABLE articles ADD COLUMN later INTEGER NOT NULL DEFAULT 0;   -- "Später lesen"
CREATE TABLE profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  builtin TEXT,                 -- 'de' | 'world' | 'business' | 'military' | NULL (eigenes)
  sources_json TEXT,            -- JSON-Liste von source_ids oder NULL = alle aktiven Quellen
  terms_json TEXT NOT NULL DEFAULT '[]',   -- JSON-Liste von Suchbegriffen (OR-verknüpft, Präfixsuche)
  in_bar INTEGER NOT NULL DEFAULT 0,
  position INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE favorite_sources (
  source_id TEXT PRIMARY KEY,
  in_bar INTEGER NOT NULL DEFAULT 1,
  position INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
```

Beim Anlegen von v4 werden die vier vorgegebenen Profile gesät (nur wenn `profiles` leer ist):
- `Deutschland` (`builtin='de'`, sources = alle Quellen mit `country=DE`, terms `[]`, in_bar 1, position 0)
- `Weltweit` (`builtin='world'`, sources NULL, terms `[]`, in_bar 0, position 1)
- `Wirtschaft` (`builtin='business'`, sources = alle mit `category=business` **plus** NULL-Fallback: wenn keine business-Quelle aktiv ist, sources NULL; terms `["Wirtschaft","Konjunktur","Inflation","Zinsen","Börse","economy","inflation","interest rates","markets"]`, in_bar 1, position 2)
- `Militär` (`builtin='military'`, sources NULL, terms `["Bundeswehr","NATO","Rüstung","Militär","Verteidigung","military","defence","defense","army"]`, in_bar 0, position 3)
Vorgegebene Profile sind editierbar (Name, Quellen, Begriffe, in_bar) aber nicht löschbar (DELETE → 409).

### 2.1 Artikel-Liste, erweitert (`GET /api/articles`)
Bestehende Parameter bleiben (`source`, `country`, `q`, `unread`, `starred`, `free`, `before`, `offset`, `limit`). Neu:
- `later=true`: nur „Später lesen“.
- `profile=<id>`: filtert auf die Quellen des Profils (Schnittmenge mit aktivierten Quellen) und, wenn `terms_json` nicht leer ist, auf Artikel, deren FTS-Eintrag einem der Begriffe (Präfix, OR) entspricht: `a.id IN (SELECT rowid FROM articles_fts WHERE articles_fts MATCH ?)` mit `"begriff"* OR "begriff2"*`. **Keyset-Paging bleibt chronologisch** (kein bm25), auch mit Begriffen. `q` und `profile` dürfen kombiniert werden (dann Offset-Paging wie bei `q`).
- `days=<n>`: nur Artikel mit `published_at >= now - n Tage` (für die Suche: 1, 7, 30).
- `free` hat neuen Default: der Wert der Einstellung `free_only` (siehe 2.7), Standard `true`.
- Antwortfelder pro Artikel wie bisher plus `later` (0/1) und `read_minutes` (ganzzahlig, `max(1, round(body_len / 1100))`, 0 wenn kein Text).

### 2.2 Artikel-Flags
- `POST /api/articles/{id}/later` `{value: bool}` → `{id, later}` (wie `read`/`star`).
- `POST /api/articles/read-all` erweitert um optionale Felder `profile` (id) und `source` (id) im Body; markiert nur die zugehörige Menge.

### 2.3 Nächster ungelesener Artikel
- `GET /api/articles/{id}/next?profile=&source=&unread=true` → derselbe Filterkontext wie die Liste; liefert den nächsten **älteren** Artikel (Keyset ab `published_at,id` des aktuellen) mit `unread` (Standard true) im Kontext, als Listen-Objekt, oder `{"item": null}`. Antwort: `{"item": {...}}`.

### 2.4 Profile
- `GET /api/profiles` → Liste, sortiert nach `position`: `{id, name, builtin, sources (Liste oder null), terms, in_bar, position, source_count (Zahl der wirksamen Quellen), unread (ungelesene Artikel im Profil, Paywall-frei; bei Begriffen per FTS gezählt; bei mehr als 5000 wird 5000 gemeldet)}`. `unread` wird höchstens alle 60 s neu berechnet (einfacher In-Memory-Cache pro Profil-ID, invalidiert bei Schreibzugriffen auf Profile oder Sammellauf).
- `POST /api/profiles` `{name, sources, terms, in_bar}` → 201 mit dem Objekt. Validierung: Name 1–40 Zeichen, sources null oder Liste bekannter IDs (≤ 200), terms Liste von 0–20 Strings à 1–40 Zeichen ohne `"`.
- `PATCH /api/profiles/{id}` (Teilfelder) → Objekt. `DELETE /api/profiles/{id}` → 204, bei `builtin` → 409.
- `POST /api/profiles/reorder` `{ids: [..]}` → setzt `position`.

### 2.5 Lieblingszeitungen und Leiste
- `GET /api/favorites/sources` → `[{source_id, name, country, in_bar, position, unread}]`.
- `PUT /api/favorites/sources/{source_id}` `{in_bar?: bool}` → legt an oder ändert; `DELETE /api/favorites/sources/{source_id}` → 204.
- `POST /api/favorites/sources/reorder` `{ids}`.
- `GET /api/bar` → geordnete Leiste: `{"items": [{"type": "all", "label": "ALLE", "unread": n}, {"type": "source", "id": "spiegel", "label": "SPIEGEL", "unread": 3}, {"type": "profile", "id": 3, "label": "WIRTSCHAFT", "unread": 12}]}`. Reihenfolge: `all` zuerst, danach Quellen und Profile mit `in_bar=1` gemischt nach einer gemeinsamen Reihenfolge aus `settings.bar_order` (JSON-Liste von `"source:spiegel"` / `"profile:3"`; Einträge, die dort fehlen, hängen hinten an). `label` = Kurzname in Versalien: für Quellen ein neues optionales Feld `short` in `sources.yaml` (z. B. `short: SPIEGEL`), sonst der Name in Versalien, auf 12 Zeichen gekürzt.
- `PATCH /api/bar` `{order: ["source:spiegel","profile:3", ...]}` → speichert `settings.bar_order`.

### 2.6 Suche
- Bestehendes `q` mit `offset`; neu `days` (2.1). Die Antwort enthält zusätzlich `total` (Trefferzahl, per `COUNT(*)` auf der FTS-Filterung, gedeckelt bei 1000).

### 2.7 Einstellungen
- `GET /api/settings` → `{theme: "system"|"dark"|"light", text_size: "s"|"m"|"l", mark_read_on_open: bool, show_images: bool, free_only: bool, bar_order: [..]}`; Defaults: `system`, `m`, true, true, true, `[]`.
- `PATCH /api/settings` (Teilfelder, validiert) → vollständiges Objekt.
- `GET /api/status` bleibt und wird vom Einstellungs-Screen für Intervall, Aufbewahrung, Build, Artikelzahl und DB-Größe gelesen (diese Werte sind nur lesbar; die Oberfläche zeigt sie mit dem Hinweis „per .env“).

### 2.8 Zähler
- `GET /api/counts` → `{unread, later, starred, profiles, favorites, sources_enabled, sources_total, sources_failing}` (für Menü-Untertitel und Kopfzeilen; `unread` Paywall-frei).

### 2.9 Quellen
- `GET /api/sources` liefert zusätzlich `short` und `lang` (existiert) und `is_favorite`.
- Sonst unverändert (`PATCH` enabled, `bulk`).

### 2.10 Tests
Für jede neue Route und die Migration v4 (aus v3 und direkt aus v1) Tests im Stil der vorhandenen: Profile mit Begriffen und Keyset-Paging (kein Duplikat/keine Lücke), `next` im Profil-Kontext, `days`, `later`, Bar-Reihenfolge, Settings-Validierung, `counts`, builtin-Profile nicht löschbar, `free_only`-Default. Die Suite bleibt grün (`pytest -q`).

## 3. Frontend, Screen für Screen

Jeder Screen entspricht einer Mockup-Datei in `design/mockups/`. Maße, Abstände, Farben und Schriftgrößen aus den Mockups und `design/tokens.json` sind verbindlich; Texte sind Deutsch wie im Mockup.

### 3.1 App-Shell (`static/index.html`, `static/css/tokens.css`, `static/css/app.css`, `static/js/app.js`)
- `index.html`: `<html lang="de" data-theme="dark">`, Meta viewport, theme-color, Manifest, Fonts per `@font-face` in `tokens.css`, ein `<main id="view">`, ein `<nav id="bar">`, ein `<div id="sheet">` für Blätter (Menü, Profile), Skript `js/app.js` als Modul.
- `tokens.css`: alle Tokens als `--bg`, `--raised`, … in beiden Themes; Typo-Klassen `.t-wordmark`, `.t-section`, `.t-counter`, `.t-headline`, `.t-article`, `.t-article-h`, `.t-teaser`, `.t-ui`, `.t-meta`, `.t-caption`; Textgröße per `html[data-text="s|m|l"]` skaliert `--scale` (0.92 / 1 / 1.1) auf Titel, Teaser, Artikeltext.
- `app.js`: Hash-Router, globaler State (Session, Settings, Bar, aktueller Kontext {profile|source|all, unread}), `api.js` mit `fetch`-Hülle (401 → Login-Screen), View-Module unter `static/js/views/`, Bausteine unter `static/js/ui/` (row, bar, chips, toggle, sheet, header, icons als Funktionen, die SVG-Strings der Lucide-Pfade aus den Mockups liefern).
- Fokus-Ring 2px `--ink`, Tap-Ziele ≥ 44px, `prefers-reduced-motion` respektieren (Puls der Ladepunkte aus).

### 3.2 Anmelden (`E2Login`)
Wortmarke KIOSK groß, Tagline, Passwortfeld als Pille, Knopf „Anmelden“, Fehlerzeile mit blauem Punkt und Text aus der API (`detail`). Nach Erfolg → `#/`. Enter sendet. `autocomplete="current-password"`.

### 3.3 Liste (`E2Liste`, hell `E2Hell`)
- Kopf: KIOSK (Wortmarke), Lupe → `#/suche`, Drei-Punkte → Menü-Blatt.
- Filterzeile: Chip „Ungelesen“ (Toggle), dann die Profile in Reihenfolge (aktives Profil als gefüllter Chip), am Ende Stift → `#/profile`. Horizontal scrollbar ohne sichtbare Scrollleiste.
- Artikel: Tagesgruppen (HEUTE, GESTERN, sonst „24. SEP“) in Punktmatrix; Zeile = Meta (Punkt bei ungelesen, Quelle in `--ink`, `/ CC / ZEIT`, Stern oder Lesezeichen rechts), Titel (max. 3 Zeilen, `-webkit-line-clamp`), Teaser (max. 2 Zeilen), quadratisches Bild 80px rechts falls `image_url` und Einstellung `show_images`; kein Platzhalter ohne Bild. Gelesene Titel in `--muted`.
- Zeitangabe: `< 60 Min` → „12 MIN“, `< 24 h` → „3 STD“, gestern → „GESTERN 22:10“, sonst „24. SEP“.
- Endloses Nachladen per `next_before` beim Scrollen (IntersectionObserver). Pull-to-refresh nicht nötig; ein Knopf „Jetzt abrufen“ sitzt in der Quellenverwaltung.
- Leiste unten (`E2Liste` unten): Pille mit Chips aus `GET /api/bar`; aktiver Kontext gefüllt; Zähler in Punktmatrix blau; Plus → Profile-Blatt. Chips scrollen horizontal, „ALLE“ und Plus bleiben stehen (sticky innerhalb der Pille).
- Kontextwechsel über Leiste oder Filterchips setzt `#/` mit Query (`#/?p=3` oder `#/s/spiegel`), damit die Auswahl beim Zurück erhalten bleibt.
- Lade- und Leerzustand (`E2Leer`): schmale Pille „HOLE NEUE ARTIKEL · 25 ZEITUNGEN“ solange `status.fetcher.running`; Leerzustand mit Punktmuster, „NICHTS NEUES“, Satz mit Profilname und Minuten bis zum nächsten Lauf (aus `last_finished + fetch_interval`), zwei Pillen.

### 3.4 Menü-Blatt (`E2Menu`)
Blatt von unten über gedimmter Liste (Overlay `rgba(0,0,0,.6)`, Blatt `--raised`, Griff, Schließen-Kreuz), Einträge mit rundem Icon-Kreis: Profile (`#/profile`), Lieblingszeitungen (`#/profile#zeitungen`), Gemerkt (`#/gemerkt`), Einstellungen (`#/einstellungen`), Untertitel aus `GET /api/counts` und `settings`; unten „Alles als gelesen markieren“ (Kontext-bezogen, mit kurzer Bestätigung im selben Blatt: „Wirklich? 63 Artikel“ → Ja). Escape und Tippen auf das Overlay schließen.

### 3.5 Leseansicht (`E2Lese`, `#/a/{id}`)
- Kopf: Zurück, Lesezeichen (later), Stern (starred), Haken (read). Aktive Zustände: Kreis gefüllt `--ink` mit Icon in `--on-ink`.
- Meta-Zeile mit Lesezeit; Titel; Bild (`image_url`) mit `radius-md`, nur wenn vorhanden und `show_images`; `body_html` in `.article-body` (Absätze, h2–h4, Listen, Zitate, Bilder mit `radius-md`, Links in `--ink` unterstrichen, `target=_blank` bleibt vom Server); bei `text_status` `partial`/`empty`/`failed` eine Hinweiszeile in Mono („NUR ANFANG VERFÜGBAR“ / „KEIN TEXT, ORIGINAL ÖFFNEN“) und bei `pending` ein „Hole Text …“ mit erneutem Abruf nach 2 s (`?retry=true` einmal).
- Wenn `mark_read_on_open` an: beim Öffnen `POST read`. Sonst nur über den Haken.
- Unten: Pille „Original öffnen“ (externer Link), Trennlinie, Karte „Nächster ungelesener Artikel“ aus `GET /api/articles/{id}/next` im aktuellen Kontext; Tippen öffnet ihn (History-Replace, damit Zurück zur Liste führt).
- Desktop: Liste links (520px) mit Leiste, Text rechts (max. 680px Lesebreite), geöffneter Artikel in der Liste als `--raised`-Karte markiert (`E2Desktop`).

### 3.6 Gemerkt (`E2Gemerkt`, `#/gemerkt`)
Kopf mit Zurück und GEMERKT, zwei Chips „Später lesen · n“ / „Favoriten · n“ (Zähler aus `counts`), Liste wie 3.3 mit `later=true` bzw. `starred=true`, Markierung rechts blau (Lesezeichen) bzw. Stern.

### 3.7 Einstellungen (`E2Einstellungen`, `#/einstellungen`)
Abschnitte in Punktmatrix: ERSCHEINUNGSBILD (Chips System/Dunkel/Hell), TEXTGRÖSSE (Klein/Normal/Groß), LESEN (drei Schalter: Gelesen beim Öffnen, Bilder in der Liste, Nur kostenlose Artikel), QUELLEN (Abrufintervall und Aufbewahrung nur lesend mit Wert aus `/api/status` und Hinweis „per .env“, „Alle Zeitungen“ → `#/quellen`), unten Pille „Abmelden“ (`POST logout` → Login) und die Mono-Zeile „KIOSK · BUILD … · n ARTIKEL · x MB“. Jede Änderung wird sofort per `PATCH /api/settings` gespeichert und angewendet.

### 3.8 Zeitungen verwalten (`E2Quellen`, `#/quellen`)
Kopf mit Zähler „25 von 92 aktiv · 1 mit Fehler“, Uhr-Knopf „Jetzt abrufen“ (`POST refresh`, danach 409-tolerant), Chips Alle/Aktive/Mit Fehler/Deutsch/Englisch, Länder-Gruppen mit „n von m“, Zeile je Quelle: Name, `CC · n Artikel · vor x Min` oder blau „Fehler: …“ (`last_error`), Schalter (`PATCH /api/sources/{id}`). Land-Titel aus einer kleinen Tabelle CC → Name (DE Deutschland, GB Großbritannien, US USA, …), unbekannt → Code.

### 3.9 Profile und Lieblingszeitungen (`E2Profile`, `#/profile`)
Blatt (oder auf Desktop Seite) mit PROFILE: je Profil Name, Untertitel („15 Zeitungen · 3 Begriffe“ / „Alle 92 Zeitungen“), Schalter „In der Leiste“ (`PATCH in_bar`), Pfeil → Editor; „Profil anlegen“. FAVORITEN (Lieblingszeitungen): je Zeitung Name, `CC · n ungelesen`, Schalter in_bar, Pfeil → Zeitungsansicht; „Zeitung hinzufügen“ öffnet eine Auswahl-Liste aller aktiven Zeitungen (Suche im Kopf, Tippen fügt hinzu). Reihenfolge per Halten-und-Ziehen ist **nicht** nötig; stattdessen Pfeile ▲▼ am Zeilenende im Bearbeiten-Modus (Knopf „Sortieren“ im Kopf) → `reorder` bzw. `PATCH /api/bar`.

### 3.10 Profil-Editor (`E2ProfilEditor`, `#/profile/neu`, `#/profile/{id}`)
Kopf: Zurück, PROFIL, „Speichern“ (gefüllt). Name (Pille), Suchbegriffe als Chips mit Kreuz und Plus (Eingabe erscheint als Pille, Enter fügt hinzu), Schalter „In der Leiste anzeigen“, dann Länder-Gruppen mit „Alle wählen/Keine“ und Schalter je Zeitung (nur aktivierte Quellen; deaktivierte erscheinen grau mit Hinweis „aus“). Bei vorgegebenen Profilen gibt es keinen Löschen-Knopf; bei eigenen unten „Profil löschen“ (Kontur) mit Bestätigung.

### 3.11 Suche (`E2Suche`, `#/suche`)
Kopf: Zurück, Suchfeld (Pille, `--ink`-Kontur, Kreuz löscht), Chips: Zeitungen (Alle / aktuelle Zeitung, wenn aus Zeitungsansicht gekommen), Zeitraum (1 Tag / 7 Tage / 30 Tage / Alle), Trefferzeile „14 TREFFER“ + „Neueste zuerst“. Suche startet 300 ms nach der letzten Eingabe (min. 2 Zeichen), Ergebnisse per `offset` nachladen. Hervorhebung: jedes Wort der Anfrage in Titel und Teaser als `<mark>` (blau, `--on-signal`-Text, 3px Radius), erzeugt durch Aufteilen von Textknoten.
Zeitungen (`E4SucheZeile`, `E4SucheMehrere`): Mit „Alle Zeitungen“ steht über den Artikeln ein Block ZEITUNGEN mit den passenden Quellen (`static/js/sourceMatch.js`). Verglichen wird kleingeschrieben, ß als ss, ohne Akzente und zusätzlich mit ä/ö/ü als ae/oe/ue; FTS-Zeichen und AND/OR/NOT/NEAR fallen weg. Eine Zeitung passt, wenn jedes Wort der Anfrage ein Wortanfang in Name oder Kurzname ist oder die Anfrage ohne Leerzeichen der Anfang von Name, Kurzname oder ID ohne Leerzeichen ist („derspiegel“). Hat die Anfrage ohne Leerzeichen weniger als 3 Zeichen, zählt nur ein genau gleicher Kurzname oder die ID („sz“, „dw“, „fr“; „de“ bringt nichts); unter 2 Zeichen nie. Ausschlüsse gibt es wie in der Artikelsuche nicht („-spiegel“ sucht „spiegel“). Ein Umlaut kann eine Zwei-Zeichen-Anfrage über die ae/oe/ue-Form auf 3 Zeichen bringen („sü“ → „sue“ findet SZ); gewollt. Reihenfolge: genaue Treffer (Name, Kurzname oder ID gleich der Anfrage), dann Lieblingszeitungen, eingeschaltete vor ausgeschalteten, Treffer am ersten Wort vor Treffern weiter hinten, zuletzt nach Name ohne Artikel vorn („The Irish Times“ unter I). Höchstens 3 Zeilen, der Rest per „N weitere Zeitungen“ (bleibt aufgeklappt, solange Anfrage und Zeitungs-Chip gleich bleiben). Je Zeile Monogramm (Kurzname: zwei Wörter → Anfangsbuchstaben, mit Ländercode ganz: LDE, bei kurzem ersten Wort dieses: „ABC AU“ → ABC; ein Wort bis 3 Zeichen ganz, sonst 2; gleiche in der Trefferliste werden dreistellig: TET/TAT, FOC, und einwortige, die dann noch gleich sind, nach der Stelle, an der sie sich trennen: TSC/TSP), Name, `CC / n UNGELESEN HEUTE` bzw. `AUSGESCHALTET`, Pfeil → `#/s/{id}`. Die Artikelzeile heißt dann „ARTIKEL“ + „n · Nach Relevanz“; bei 0 Artikeln entfällt sie, darunter nur „Keine Artikel mit „q“.“

### 3.12 Zeitungsansicht (`E2Zeitung`, `#/s/{source_id}`)
Kopf: Zurück, Name in Punktmatrix (DER SPIEGEL) mit `CC · n ungelesen · vor x Min geholt`, Stern (Lieblingszeitung an/aus → `PUT/DELETE favorites`), Haken (alle gelesen dieser Quelle mit Bestätigung), Chips Ungelesen/Alle/Mit Bild; Liste mit `source=id`; Leiste unten mit dieser Zeitung als aktivem Chip (auch wenn sie nicht in der Leiste ist: dann temporär eingeblendet).

### 3.13 Desktop-Layout (`E2Desktop`)
Ab 900px: `body` als Grid `520px 1fr`; links Liste + Leiste, rechts Leseansicht oder ein Leerzustand („Wähle einen Artikel“ in Punktmatrix). Blätter (Menü, Profile) erscheinen als zentrierte Dialoge (`radius-md`, max. 480px) statt von unten. Tastatur: `j`/`k` nächster/vorheriger Artikel, `Enter` öffnen, `m` gelesen, `s` Stern, `l` später, `/` Suche.

### 3.14 PWA
`static/manifest.webmanifest`, `static/sw.js` (App-Shell-Cache mit Versionsschlüssel `KIOSK_BUILD` aus `/api/status`, Netzwerk zuerst für alles unter `/api/`), Icons `static/icons/icon-192.png`, `icon-512.png`, `icon.svg` (schwarzer Kreis, weißes K in Punktmatrix-Anmutung, ohne Schriftdatei: aus Kreisen aufgebaut).

## 4. Arbeitsteilung und Reihenfolge

- **Agent Backend**: Abschnitt 2 komplett, inkl. Tests, `sources.yaml` um `short` für alle Quellen ergänzen (sinnvolle Kurznamen, ≤ 12 Zeichen, Versalien), README-API-Tabelle nachziehen. Arbeitet nur in `app/`, `tests/`, `sources.yaml`, `README.md`.
- **Agent Frontend**: Abschnitt 3 komplett. Arbeitet nur in `static/`. Entwickelt gegen den Vertrag aus Abschnitt 2; solange Routen noch fehlen, lokal gegen den laufenden Backend-Stand testen (`KIOSK_DISABLE_FETCHER=1`, Kopie der Datenbank) und für fehlende Routen die Oberfläche so bauen, dass sie mit einer 404 nicht bricht (Abschnitt leer, Hinweis in Mono). Nach Abschluss des Backend-Agenten gegen die echten Routen nachtesten.
- Beide: keine Deploys, kein SSH, keine Anfragen an die Box. Kommentare deutsch. Bericht deutsch mit Testlauf.
- Danach: Review-Runden (Hot, Cold, Design-Kritiker), Fixes, Deploy.

## 5. Abnahme

- Alle Screens aus `design/mockups/` sind vorhanden und in beiden Themes stimmig.
- `pytest -q` grün; Backend-Migration auf der Kopie der Produktionsdatenbank läuft durch.
- Handy 390px ohne horizontales Scrollen; Desktop 1280px zweispaltig.
- Keine externen Requests außer zur eigenen API (Netzwerk-Tab), CSP ohne Verstöße in der Konsole.
- Tastatur-Bedienung der Liste und Leseansicht; alle Icon-Knöpfe mit `aria-label`.
