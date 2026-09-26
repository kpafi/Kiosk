# Kiosk: Plan für den Mehrbenutzer-Umbau

Stand 2026-09-26. Vorgabe für die Coder-Agenten. Ziel: mehrere Personen aus dem engsten Kreis nutzen dieselbe Kiosk-Instanz mit eigenen Konten, eigenem Lesestand, eigenen Profilen, Favoriten, Leiste und Einstellungen. Die gesammelten Artikel bleiben gemeinsam. Es gibt keine Registrierung: Konten legt der Betreiber per SSH an.

## 0. Grundsätze

- **Ein Artikelbestand, viele Lesestände.** Feeds, Extraktion, Volltextindex und Quellen bleiben global. Alles, was „meins“ ist, hängt an einem Benutzer.
- **Kein Registrieren, kein Passwort-vergessen.** Konten werden mit einem Kommandozeilenwerkzeug im Container angelegt, geändert und gesperrt. Das Frontend bekommt nur ein Benutzername-Feld beim Login und eine Zeile „Angemeldet als“ in den Einstellungen.
- **Ein Admin.** Der erste Benutzer ist Admin. Nur Admins dürfen Quellen ein- und ausschalten, einen Sammellauf auslösen und die Server-Werte sehen. Alle anderen sehen die Quellenliste nur lesend.
- **Rückwärtskompatibel.** Der bestehende Lesestand, die Profile, Favoriten und Einstellungen gehen beim Umbau auf den Admin über. `KIOSK_PASSWORD` bleibt für den ersten Start als Bootstrap erhalten.
- **Keine neuen Abhängigkeiten.** Passwörter werden mit `hashlib.scrypt` (Standardbibliothek) gehasht, Vergleich über `hmac.compare_digest`.

## 1. Datenmodell (Schema v6)

```
CREATE TABLE users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT NOT NULL UNIQUE COLLATE NOCASE,   -- 2–32 Zeichen, [a-z0-9._-], kleingeschrieben gespeichert
  display_name  TEXT NOT NULL,
  password_hash TEXT NOT NULL,        -- "scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>"
  is_admin      INTEGER NOT NULL DEFAULT 0,
  disabled      INTEGER NOT NULL DEFAULT 0,
  session_epoch INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL,
  last_login_at TEXT
);

CREATE TABLE user_articles (          -- der Lesestand pro Benutzer; Zeile entsteht beim ersten Setzen eines Flags
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  read       INTEGER NOT NULL DEFAULT 0,
  starred    INTEGER NOT NULL DEFAULT 0,
  later      INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (user_id, article_id)
) WITHOUT ROWID;
CREATE INDEX idx_user_articles_flags ON user_articles (user_id, read, starred, later);
```

Änderungen an bestehenden Tabellen:
- `articles`: die Spalten `read`, `starred`, `later` bleiben physisch stehen (SQLite kann sie nicht billig entfernen), werden aber nicht mehr gelesen oder geschrieben. Ein Kommentar im Schema und ein Test stellen sicher, dass kein Code sie mehr benutzt.
- `profiles`: neue Spalte `user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE`; `position` gilt pro Benutzer. Die vier vorgegebenen Profile werden **pro Benutzer** beim Anlegen gesät.
- `favorite_sources`: Primärschlüssel wird `(user_id, source_id)`.
- `settings`: Primärschlüssel wird `(user_id, key)`. Globale Werte gibt es nicht mehr; `bar_order` ist damit automatisch pro Benutzer.
- `seen_guids`, `sources`-Zustand (enabled, etag, Fehler), `meta`: unverändert, global.
- `articles_fts`: unverändert.

Da SQLite Primärschlüssel nicht ändern kann, werden `profiles`, `favorite_sources` und `settings` in der Migration neu angelegt und umkopiert.

## 2. Migration v5 → v6

In einer Transaktion, idempotent über `schema_version`:
1. Tabellen `users` und `user_articles` anlegen.
2. Admin anlegen: `username` aus `KIOSK_ADMIN_USER` (Standard `admin`), `display_name` = Username, `password_hash` aus `KIOSK_PASSWORD` (Pflicht beim allerersten Start nach dem Umbau, danach nur noch als Fallback, wenn `users` leer ist), `is_admin=1`.
3. Lesestand übernehmen: `INSERT INTO user_articles SELECT admin_id, id, read, starred, later, now FROM articles WHERE read=1 OR starred=1 OR later=1`.
4. `profiles`, `favorite_sources`, `settings` neu anlegen, bestehende Zeilen mit `user_id = admin_id` umkopieren, alte Tabellen löschen.
5. `schema_version = 6`.

Beim Start: Ist `users` leer (frische Datenbank), wird der Admin wie in Schritt 2 angelegt. Ist `KIOSK_PASSWORD` gesetzt, aber `users` nicht leer, wird die Variable ignoriert (Hinweis im Log, damit klar ist, dass sie keine Wirkung mehr hat).

Wie bisher: Backup vor dem Deploy, Test der Migration auf der Kopie der Produktionsdatenbank (v5 → v6) und aus v1 in einem Durchlauf.

## 3. Konten verwalten (Kommandozeile)

Neues Modul `app/users.py` mit `python -m app.users`:

```
python -m app.users list
python -m app.users add <username> [--admin] [--name "Anzeigename"]      # fragt das Passwort zweimal ab (getpass), oder --password-stdin
python -m app.users passwd <username>                                     # neues Passwort, beendet alle Sitzungen des Benutzers
python -m app.users disable <username> | enable <username>
python -m app.users remove <username>                                     # löscht Lesestand, Profile, Favoriten, Einstellungen des Benutzers; Artikel bleiben
python -m app.users admin <username> --on|--off
```

Regeln: Passwort mindestens 12 Zeichen; der letzte Admin kann nicht gelöscht, gesperrt oder herabgestuft werden; `remove` fragt nach Bestätigung (`--yes` für Skripte). Das Werkzeug nutzt dieselbe `Database`-Klasse, läuft im Container gegen `/data/kiosk.db` und beendet sich sofort (kein Sammler).

Bequemer Aufruf vom Laptop: `deploy/user.sh add anna` führt per SSH `docker compose exec -T kiosk python -m app.users add anna` aus; das Passwort wird lokal per `getpass` gelesen und über stdin durchgereicht, damit es nie in einer Kommandozeile oder Shell-History steht. Der Wrapper wird im `deploy/README.md` dokumentiert.

## 4. Anmeldung und Sitzung

- `POST /api/login` `{username, password}` → 204 und Cookie. Antwort bei Fehler immer „Benutzername oder Passwort falsch“ (kein Unterschied zwischen unbekanntem Benutzer und falschem Passwort). Gesperrte Konten: gleiche Meldung.
- Token-Payload: `u:<user_id>:<session_epoch>:<hash_fingerprint>`, signiert wie bisher. `POST /api/logout` erhöht die `session_epoch` **des Benutzers**, nicht global. `passwd` per CLI erhöht sie ebenfalls.
- `GET /api/session` → `{authenticated, user: {id, username, display_name, is_admin}}`.
- Login-Bremse: wie bisher pro IP, zusätzlich pro Benutzername (8 Fehlversuche in 15 Minuten), damit ein rotierender Angreifer nicht unbegrenzt probiert. Ein globales Limit (z. B. 50 Fehlversuche pro Minute über alle) als Notbremse.
- `require_login` lädt den Benutzer einmal pro Request (`request.state.user`), gesperrte Benutzer → 401. Alle Routen erhalten den Benutzer über eine Dependency `current_user`.

## 5. API-Änderungen

Alle bestehenden Routen behalten Pfad und Form; sie werden nur benutzerbezogen:
- Artikel-Flags (`read`, `star`, `later`, `read-all`) schreiben in `user_articles` des angemeldeten Benutzers. Antwort unverändert.
- `GET /api/articles`, `/next`, `/api/counts`, `/api/bar`, `/api/profiles`, `/api/favorites/sources`, `/api/settings`: Filter und Zähler per `LEFT JOIN user_articles ua ON ua.article_id=a.id AND ua.user_id=?`; „ungelesen“ heißt `COALESCE(ua.read,0)=0`.
- `mark_all_read` mit Wasserstand schreibt für den Benutzer per `INSERT … ON CONFLICT DO UPDATE`.
- Profil-Zähler-Cache wird pro `(user_id, profile_id)` geführt und bei Schreibzugriffen des jeweiligen Benutzers geleert; Sammellauf und Quellenwechsel leeren alles.
- `purge` behält Artikel, die **irgendein** Benutzer gemerkt hat (`NOT EXISTS (SELECT 1 FROM user_articles WHERE article_id=a.id AND (starred=1 OR later=1))`), und löscht Waisen in `user_articles` per Cascade.
- Admin-only (sonst 403): `PATCH /api/sources/{id}`, `POST /api/sources/bulk`, `POST /api/refresh`. `GET /api/status` liefert für Nicht-Admins nur `build` und den Sammler-Zustand (kein Intervall, keine DB-Größe).
- Neu, nur lesend: `GET /api/me` = das `user`-Objekt aus `/api/session` (für die Einstellungen).
- Keine Benutzerverwaltung über die API. Das ist bewusst so; wer Konten anlegen will, braucht SSH.

## 6. Frontend

- **Login** (`E2Login`): zusätzliches Feld „Benutzername“ über dem Passwort, gleiche Pille, `autocomplete="username"`. Fehlerzeile wie gehabt.
- **Einstellungen**: neue erste Zeile „Angemeldet als anna“ (Mono-Untertitel „Admin“ wo zutreffend). Die Zeile „Alle Zeitungen“ bleibt für alle, in der Zeitungsverwaltung sind Schalter und „Jetzt abrufen“ für Nicht-Admins ausgeblendet, stattdessen ein Mono-Hinweis „Nur der Admin kann Zeitungen ein- und ausschalten“. Abrufintervall, Aufbewahrung und die Build-Zeile mit DB-Größe nur für Admins.
- **Zeitungsansicht**: „Einschalten“ im Leerzustand nur für Admins; andere sehen „Diese Zeitung ist ausgeschaltet.“ ohne Pille.
- Alles andere bleibt: Lesestand, Leiste, Profile, Gemerkt und Einstellungen kommen ohnehin von der API und sind damit automatisch pro Benutzer.
- Der Service Worker cached weiterhin keine API-Antworten; die localStorage-Kopie der Einstellungen wird beim Login und Logout geleert, damit Theme und Textgröße nicht vom vorherigen Benutzer bleiben.

## 7. Deploy

- `deploy/deploy.sh`: der Login-Smoke-Test schickt `{username, password}` mit `KIOSK_ADMIN_USER` (Standard `admin`) aus der `.env`. `.env.example` bekommt `KIOSK_ADMIN_USER`.
- Neu `deploy/user.sh` (siehe Abschnitt 3). Backup und Rollback unverändert; die Migration ist über das Pre-Deploy-Backup rückrollbar.
- README: Abschnitt „Benutzer“ mit den CLI-Befehlen und dem Hinweis, dass die Instanz nur für den engsten Kreis gedacht ist.

## 8. Tests

- Migration v5 → v6 auf der Kopie der Produktionsdatenbank: Lesestand, Profile, Favoriten, Einstellungen landen beim Admin; Zahlen vorher/nachher identisch.
- Isolation: Benutzer A liest einen Artikel, B sieht ihn ungelesen; A merkt, B nicht; A legt ein Profil an, B sieht es nicht; Zähler, Leiste und `counts` pro Benutzer.
- `purge` behält Artikel, die nur B gemerkt hat.
- Login: falscher Benutzer und falsches Passwort liefern dieselbe Meldung; gesperrter Benutzer 401; Logout beendet nur die eigene Sitzung; `passwd` beendet die Sitzungen des Benutzers; Bremse pro Benutzername.
- Rechte: Nicht-Admin bekommt 403 bei Quellen-Schaltern und `refresh`; Admin darf.
- CLI: `add`, `passwd`, `disable`, `remove`, letzter Admin nicht löschbar; `--password-stdin`.
- Kein Code liest `articles.read/starred/later` mehr (grep-Test wie bei den SHELL-Dateien).

## 9. Reihenfolge und Abnahme

1. Backend-Agent: Abschnitte 1–5, 7 (deploy.sh, .env.example), 8. Frontend-Agent parallel: Abschnitt 6 gegen den Vertrag aus Abschnitt 5.
2. Lokaler Lauf auf der Produktionskopie, Migration prüfen.
3. Review-Runden Hot, Cold, Design-Kritiker (Login-Screen und Einstellungen gegen die Mockups), bis sauber.
4. Deploy. Danach lege ich den ersten weiteren Benutzer zusammen mit dem Betreiber per `deploy/user.sh` an.

Abnahme: zwei Benutzer im Browser parallel (zwei Profile des Browsers oder ein privates Fenster), getrennte Lesestände und Profile sichtbar; Admin-Schalter für den zweiten Benutzer unsichtbar und per API 403; alte Anmeldung des Admins funktioniert mit `KIOSK_ADMIN_USER` und dem bisherigen Passwort weiter.
