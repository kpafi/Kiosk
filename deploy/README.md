# Kiosk auf der Hetzner-Box

Gleicher Server wie secure-chat und MailDigest (203.0.113.10, SSH-Alias `kiosk-server`).

- Code liegt unter `/opt/kiosk`, Daten (SQLite, Secret) unter `/opt/kiosk/data`, Passwort in `/opt/kiosk/.env`.
- Besitzer und Rechte (setzt jeder Deploy idempotent): Code und Konfiguration gehören `root:root` und sind
  für andere nicht schreibbar (rsync mit `--chown=root:root --chmod=D755,F644`, danach `chown`/`chmod go-w`
  über alles außer `data/`). `data/` gehört dem Container, Modus 700; `kiosk.db*` und `secret.key` 600
  (dieses `chmod` läuft per `setpriv` als 10100, damit root keinem Symlink aus dem Container folgt). Die
  App selbst legt Dateien mit umask 077 an.
- Eigene uid/gid 10100 (`kiosk`): `USER` im Dockerfile und `user: "10100:10100"` in `docker-compose.yml`.
  Die uid 1000 gehört auf der Box `maildigest` (dessen Container läuft ebenfalls als 1000), frühere
  Kiosk-Versionen liefen auch so. deploy.sh stellt `data/` idempotent erst direkt vor dem Start des neuen
  Containers auf `10100:10100` um (Schritt „Datenverzeichnis für uid 10100 und Container starten“, nach
  Bauen und Pre-Deploy-Backup; bis dahin läuft der alte Container unverändert weiter). Umstellen und
  `docker compose up` laufen in einem einzigen SSH-Aufruf, damit ein Verbindungsabbruch dazwischen nicht
  den alten Container (uid 1000) vor einem data/ stehen lässt, das er nicht mehr lesen darf. Das Skript
  auf der Box wird ganz gelesen, bevor es läuft, ignoriert HUP/PIPE/INT (bricht die Verbindung ab, läuft
  es zu Ende) und schreibt nach `/opt/kiosk/backups/deploy-start.log`. Es hält dabei das Lock von
  `backup.sh` (wartet bis zu 300 s auf ein laufendes Backup) und bricht vorher ohne Änderung ab (Exit 42,
  „Alter Container läuft weiter“), wenn uid oder gid 10100 auf dem Host schon einem Benutzer bzw. einer
  Gruppe gehört (`getent`). Zurückrollen: Weil
  `docker-compose.yml` den Benutzer setzt, läuft auch ein älteres Image (`kiosk:good`, `kiosk:previous`,
  gebaut noch mit uid 1000) als 10100 und kann `data/` schreiben; `data/` also nie auf 1000 zurückstellen.
  Nur wer von Hand ein Image ohne dieses Compose-File startet, muss `--user 10100:10100` angeben.
- KI-Ausgabe: `OPENROUTER_API_KEY` (und bei Bedarf `KIOSK_AI_MODEL`, `KIOSK_AI_MORNING`, `KIOSK_AI_EVENING`,
  siehe `.env.example`) von Hand in `/opt/kiosk/.env` eintragen, dann `docker compose up -d` in `/opt/kiosk`
  oder ein Deploy. deploy.sh trägt den Schlüssel nie ein (an der `.env` ergänzt es nur ein fehlendes
  `KIOSK_FETCH_DENY_IPS` und hält sie bei root, Modus 600). Ohne Schlüssel ist die KI-Ausgabe aus (Hinweis im Log). Prüfen: `/api/status`
  als Admin (`editions.enabled`), sofort eine Ausgabe schreiben: `POST /api/editions/run {"slot": "morning"}`.
- Versand der KI-Ausgabe per Mail: `KIOSK_MAIL_HOST`, `KIOSK_MAIL_USER`, `KIOSK_MAIL_PASSWORD`, `KIOSK_MAIL_TO`
  und `KIOSK_PUBLIC_URL` ebenso von Hand in `/opt/kiosk/.env` (siehe `.env.example`); das Passwort gehört in
  einfache Anführungszeichen. Auf der Box läuft der Mailserver `mailserver` (/opt/mailserver, mail.example.org,
  Konto kiosk@example.org); der Kiosk-Container erreicht ihn über die öffentliche Adresse auf 465. Prüfen:
  `/api/status` als Admin (`editions.mail`), Probeversand der neuesten Ausgabe: `POST /api/editions/mail {}`.
- Container `kiosk` lauscht nur auf `127.0.0.1:8100`. Caddy terminiert HTTPS unter `kiosk.example.org`.
- Deploy: `bash deploy/deploy.sh` aus dem Repo-Root. Beim ersten Lauf wird ein Passwort erzeugt und in
  `/opt/kiosk/.env` abgelegt (nicht im Terminal ausgegeben). Fehlt `KIOSK_FETCH_DENY_IPS` in der `.env`,
  trägt jeder Deploy den Wert von `FETCH_DENY_IPS` oben in `deploy.sh` ein (die Adressen der Box); ein
  vorhandener Wert bleibt unverändert. Hochgeladen wird nur, was Build und Betrieb brauchen (Allowlist:
  Dockerfile, .dockerignore, docker-compose.yml, requirements.txt, app/, static/, sources.yaml, deploy/,
  README.md, .env.example), und zwar der committete Stand (`git archive HEAD`); mit nicht committeten
  Änderungen oder ungetrackten (nicht ignorierten) Dateien unter diesen Pfaden bricht der Deploy ab und
  nennt sie (`KIOSK_ALLOW_DIRTY=1` deployt trotzdem genau HEAD, ohne diese Änderungen und Dateien).
  `.env*` auf dem Server wird nie überschrieben oder gelöscht. Reste früherer Deploys (`tests/` usw.)
  löscht der Rechte-Schritt. Steht in der `.env` noch `KIOSK_PASSWORD`, obwohl es schon Benutzer gibt,
  warnt der Deploy am Ende (die Datei ändert er nicht). Ablauf: Code per rsync, Backup-Skript und Cron
  installieren, Image als `kiosk:candidate` bauen (mit Build-Zeitstempel, sichtbar in `/api/status` als
  `build`), Pre-Deploy-Backup der Datenbank (gibt auch die Zahl der Artikel aus), Container mit
  `kiosk:candidate` starten, bis zu 300 s auf `/healthz` warten
  (überschreibbar: `KIOSK_HEALTH_WAIT=600 bash deploy/deploy.sh`), Caddy-Block aktualisieren, dann eine
  Prüfung über Caddy ohne Zugangsdaten: `GET https://kiosk.example.org/api/session` muss 200 mit
  `{"authenticated": false, ...}` liefern, und `POST /api/login` mit richtigem `Origin`, einem zufälligen, nicht
  existierenden Benutzernamen (`deploy-probe-<zufall>`) und falschem Passwort muss 401 liefern (beweist Caddy,
  Origin-/CSRF-Prüfung, Login-Pfad und Datenbank; 403 = Origin abgelehnt, 429 = Login-Bremse). Kein echtes
  Konto wird dabei belastet; der Fehlversuch zählt aber gegen die IP der Box (8 pro 15 Minuten), mehr als
  acht Deploys in 15 Minuten scheitern also an dieser Prüfung mit 429. Bei 429 beim Login-Check:
  15 Minuten warten, kein Rollback nötig.
- Dauer der Schema-Migration beim Start: Der Umstieg von Schema 1 auf 2 (Volltext neu indizieren) braucht ca.
  2 ms pro Artikel, bei 100 000 Artikeln also gut 3 Minuten; in dieser Zeit antwortet `/healthz` nicht. Der
  Docker-HEALTHCHECK hat deshalb `--start-period=180s`; bei sehr großen Datenbanken `KIOSK_HEALTH_WAIT`
  erhöhen. Schema 2 auf 3 (`seen_guids`) ist ein einzelnes `INSERT ... SELECT` und dauert nur Sekunden.
- Image-Markierungen: `docker-compose.yml` nutzt `image: ${KIOSK_IMAGE:-kiosk:latest}`. deploy.sh baut und
  startet mit `KIOSK_IMAGE=kiosk:candidate`; erst nach der Prüfung über Caddy zeigen `kiosk:latest` und `kiosk:good`
  auf dasselbe Image (der Container läuft einfach weiter, die Image-ID ist dieselbe). Ein gescheiterter
  Deploy lässt `kiosk:latest` also unverändert. Ein späteres `docker compose up -d` ohne Variable legt den
  Container einmal mit dem Namen `kiosk:latest` neu an (gleiches Image). `kiosk:good` ist immer das zuletzt
  erfolgreich geprüfte Image, `kiosk:previous` das geprüfte davor (beim nächsten erfolgreichen Deploy aus dem alten
  `kiosk:good` abgeleitet, nicht aus dem vorher laufenden Container). Beim ersten Deploy mit diesem Schema
  gibt es noch kein `kiosk:good`, ein älteres `kiosk:previous` bleibt dann unverändert.
- Scheitert das Bauen oder das Pre-Deploy-Backup, bricht das Skript mit `Alter Container läuft weiter,
  nichts zu tun` ab: Bis dahin ist am laufenden Container nichts verändert.
- Scheitert das Umstellen von `data/` oder `docker compose up` selbst, gibt das Skript einen eigenen
  Hinweis aus: keine Datenbank-Wiederherstellung nötig (das Schema ist noch nicht migriert),
  `docker compose down`, `docker tag kiosk:good kiosk:latest`, `chown -R 10100:10100 /opt/kiosk/data`,
  `docker compose up -d --no-build` (das ältere Image läuft über `user:` in `docker-compose.yml` als 10100).
- Scheitert ein Schritt danach (`/healthz`, Caddy oder die Prüfung über Caddy), bricht das Skript ab und gibt die Befehle zum Zurückrollen aus (Ziel `kiosk:good`, ohne dieses
  die ID des vorher laufenden Images, dazu der Name des Pre-Deploy-Backups); der neue Container läuft dann weiter.
- Zurückrollen nach einem gescheiterten Deploy: `cd /opt/kiosk && docker compose down && docker tag kiosk:good kiosk:latest`
  (`kiosk:latest` ist dann ohnehin noch der alte Stand; das Umtaggen schadet nicht), dann ggf. die Datenbank
  wie unten zurückspielen und `docker compose up -d --no-build` (ohne `KIOSK_IMAGE`, also `kiosk:latest`).
  Zurückrollen nach einem erfolgreichen Deploy (auf das geprüfte Image davor):
  `cd /opt/kiosk && docker compose down && docker tag kiosk:previous kiosk:latest`. Von Schema 7 zurück auf
  ein Image mit Schema 6 braucht es **keine** Wiederherstellung: 7 fügt nur die Spalte `profiles.in_chips`
  hinzu, das ältere Image überspringt die Migration (Version ist schon höher) und ignoriert die Spalte.
  Ein Zurückspielen würde dann nur Konten, Lesestand und Profile verwerfen, die seit dem Deploy entstanden
  sind. Nur bei einem Ziel-Image mit älterem Schema als 6 (und schon migrierter Datenbank) das
  Pre-Deploy-Backup dieses Deploys zurückspielen: jüngste Datei
  `/opt/kiosk/backups/kiosk-predeploy-<UTC-Zeitstempel>.db.gz` (`ls -1t /opt/kiosk/backups/kiosk-predeploy-*.db.gz | head -1`)
  bei gestopptem Container entpacken:
  `cd / && gunzip -c <Datei> | setpriv --reuid=10100 --regid=10100 --clear-groups sh -c 'cd /opt/kiosk/data && rm -f kiosk.db kiosk.db-wal kiosk.db-shm && umask 077 && cat > kiosk.db'`
  (geschrieben wird nur als 10100 und in eine neu angelegte Datei: root folgt so keinem Symlink, den ein
  kompromittierter Container in `data/` gelegt haben könnte; `cd /`, weil 10100 `/root` nicht betreten darf). Dann `docker compose up -d --no-build`.
  (Ältere Sicherungen ohne `.gz` direkt kopieren.)
- Benutzer verwalten vom Laptop aus: `bash deploy/user.sh <befehl> ...` führt per SSH
  `docker compose exec -T kiosk python -m app.users <befehl> ...` im laufenden Container aus
  (Befehle siehe docs/REFERENZ.md, Abschnitt Benutzer), z. B. `bash deploy/user.sh add anna --name "Anna"`,
  `bash deploy/user.sh passwd anna`, `bash deploy/user.sh list`. Bei `add` und `passwd` fragt das Skript das
  Passwort lokal zweimal verdeckt ab und reicht es über stdin (`--password-stdin`) durch; es steht nie in
  einer Kommandozeile oder Shell-History. `remove` fragt lokal nach und ruft dann mit `--yes` auf.
  Anderer SSH-Alias: `BOX=... bash deploy/user.sh ...`.
  Quellen mit `in_all: false` für ein Konto in ALLE: `bash deploy/user.sh extra-sources reiner focus
  --profile-de` (ohne Quelle: keine mehr).
- Passwörter ändern: `bash deploy/user.sh passwd <name>`. `KIOSK_PASSWORD` in der `.env` legt nur beim
  ersten Start (bzw. beim Umbau auf Schema 6) den Admin `KIOSK_ADMIN_USER` (Standard `admin`) an und wird
  danach ignoriert. Die Prüfung von deploy.sh braucht kein Passwort; nach einem `passwd` muss die `.env`
  nicht angepasst werden.
- Umbau auf Mehrbenutzer (Schema 6): Beim ersten Start migriert die App in einer Transaktion; der bisherige
  Lesestand, die Profile, Favoriten und Einstellungen gehen auf den Admin über, der sich mit
  `KIOSK_ADMIN_USER` (Standard `admin`) und dem bisherigen `KIOSK_PASSWORD` anmeldet. Alle bisherigen
  Cookies sind danach ungültig (neues Format), jeder meldet sich einmal neu an. Zurück geht es nur über das
  Pre-Deploy-Backup (siehe unten).
- Logs: `docker logs -f kiosk`.
- Der Container läuft mit `init: true`: Ein kleiner init-Prozess ist PID 1, sammelt verwaiste Prozesse
  (z. B. beendete Extraktions-Worker) ein und leitet Signale weiter.
- Backup: `deploy/backup.sh` wird nach `/opt/kiosk/bin/backup.sh` installiert und läuft täglich um 03:30 als root
  (`/etc/cron.d/kiosk-backup`, Ausgabe im Journal unter `kiosk-backup`) sowie vor jedem Deploy. Es nutzt die
  Online-Backup-API von SQLite (auch bei laufendem Container konsistent), prüft die Kopie mit `quick_check`
  und komprimiert sie mit gzip. Den SQLite-Teil führt das Skript nicht als root aus, sondern in einem
  eigenen Prozess als Besitzer des Datenverzeichnisses (uid/gid von `data/` selbst, also 10100, während
  der Umstellung noch 1000; überschreibbar mit `KIOSK_BACKUP_UID`/`KIOSK_BACKUP_GID`, gehört `data/` root,
  bricht es ab statt als root zu laufen). Das ist sicher: Der Container kann `data/` weder ersetzen noch
  einem anderen Benutzer übertragen. Die Arbeitskopie liegt kurz als
  `data/.backup-*` im Datenverzeichnis, root schreibt nur die komprimierte Sicherung. So folgt root keinem
  Symlink, den ein kompromittierter Container im Datenverzeichnis anlegt. Ergebnis: täglich `/opt/kiosk/backups/kiosk-<UTC-Zeitstempel>.db.gz` (Modus 600, die
  neuesten 7 bleiben) und vor jedem Deploy `/opt/kiosk/backups/kiosk-predeploy-<UTC-Zeitstempel>.db.gz` (eigene
  Rotation, die neuesten 3 bleiben; tägliche Läufe löschen sie nicht). Alte unkomprimierte `.db`-Sicherungen
  zählen bei der Rotation mit und laufen so aus. Reste abgebrochener Läufe (`*.part`, älter als eine Stunde)
  werden beim nächsten Lauf gelöscht. Nie zwei Läufe gleichzeitig (`flock` auf `/opt/kiosk/backups/.backup.lock`):
  Ein täglicher Lauf wird dann mit Exit 0 und einer Meldung übersprungen, einer vor dem Deploy wartet bis zu
  300 s (`KIOSK_BACKUP_LOCK_WAIT`) und bricht sonst mit Fehler ab. Die Sicherung selbst darf höchstens
  600 s dauern (`KIOSK_BACKUP_TIMEOUT`), sonst wird sie abgebrochen (z. B. wenn die Datenbank dauerhaft
  gesperrt ist). `secret.key` wird einmalig daneben gelegt
  (nur als einfache Datei, nie über einen Symlink). Von Hand:
  `/opt/kiosk/bin/backup.sh` bzw. `/opt/kiosk/bin/backup.sh predeploy`.
- Fehlgeschlagene Backups: Das Skript meldet `FEHLER: Kiosk-Backup (<Modus>) fehlgeschlagen` und endet mit
  Exit-Code ungleich 0; der Cron-Eintrag schreibt dann zusätzlich `backup failed` mit Priorität `user.err`
  ins Journal: `journalctl -t kiosk-backup -p err`.
- Wiederherstellen: Container stoppen (`docker compose down`), dann
  `cd / && gunzip -c <Sicherung>.db.gz | setpriv --reuid=10100 --regid=10100 --clear-groups sh -c 'cd /opt/kiosk/data && rm -f kiosk.db kiosk.db-wal kiosk.db-shm && umask 077 && cat > kiosk.db'`
  (geschrieben wird nur als 10100 und in eine neu angelegte Datei: root folgt so keinem Symlink, den ein
  kompromittierter Container in `data/` gelegt haben könnte; `cd /`, weil 10100 `/root` nicht betreten darf),
  danach `docker compose up -d --no-build`.
- Wachhund: Hängt die Sammler-Schleife (kein Lauf fertig seit 3 Intervallen + 10 min, oder ein Lauf dauert
  länger als 3 h) oder ist sie beendet, beendet sich die App nach spätestens einer Minute selbst (Exit-Code 3)
  und Docker startet den Container neu (`restart: unless-stopped`). Sichtbar in `docker logs kiosk` als
  `Wachhund: ...`.
- Netz: `docker-compose.yml` legt das Netz `kiosk_default` fest auf `172.19.0.0/16` (Gateway `172.19.0.1`).
  Die App vertraut Proxy-Headern nur von `127.0.0.1` und `172.19.0.1`, Caddy kommt über docker-proxy vom
  Gateway herein. Wer das Netz ändert, muss `--forwarded-allow-ips` im `Dockerfile` mitziehen.
- Caddy: Der Kiosk-Block in der gemeinsamen `/etc/caddy/Caddyfile` steht zwischen den Markerzeilen
  `# >>> kiosk (managed by deploy.sh)` und `# <<< kiosk` und wird bei jedem Deploy aus `deploy/Caddyfile.kiosk`
  ersetzt. Änderungen am Block also in `deploy/Caddyfile.kiosk` machen, nicht auf dem Server; alles außerhalb
  der Marker bleibt unberührt.
- Das Update ist atomar: `deploy.sh` baut eine Kandidatendatei in `/etc/caddy/`, prüft sie mit
  `caddy validate --adapter caddyfile` und ersetzt die echte Datei nur bei Erfolg per `mv` (vorher Backup
  `/etc/caddy/Caddyfile.bak-<Zeitstempel>`), dann `systemctl reload caddy`. Scheitert das Reload, wird das
  Backup zurückverschoben und das Skript bricht ab. Ist die Kandidatendatei ungültig,
  bricht das Skript mit dem Fehler von Caddy ab und die laufende Konfiguration bleibt unverändert.
- Einmalige Umstellung: Ein alter, ohne Marker angehängter Block (`kiosk.example.org {` bis zum
  Dateiende) wird erkannt und durch den markierten Block ersetzt. Steht danach noch etwas anderes, bricht das
  Skript ab, statt zu raten.
