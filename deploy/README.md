# Kiosk auf der Hetzner-Box

Gleicher Server wie secure-chat und MailDigest (203.0.113.10, SSH-Alias `kiosk-server`).

- Code liegt unter `/opt/kiosk`, Daten (SQLite, Secret) unter `/opt/kiosk/data`, Passwort in `/opt/kiosk/.env`.
- Container `kiosk` lauscht nur auf `127.0.0.1:8100`. Caddy terminiert HTTPS unter `kiosk.example.org`.
- Deploy: `bash deploy/deploy.sh` aus dem Repo-Root. Beim ersten Lauf wird ein Passwort erzeugt und in
  `/opt/kiosk/.env` abgelegt (nicht im Terminal ausgegeben). Ablauf: Code per rsync, Backup-Skript und Cron
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
- Scheitert ein Schritt ab dem Start des neuen Containers (`docker compose up`, `/healthz`, Caddy oder die
  Prüfung über Caddy), bricht das Skript ab und gibt die Befehle zum Zurückrollen aus (Ziel `kiosk:good`, ohne dieses
  die ID des vorher laufenden Images, dazu der Name des Pre-Deploy-Backups); der neue Container läuft dann weiter.
- Zurückrollen nach einem gescheiterten Deploy: `cd /opt/kiosk && docker compose down && docker tag kiosk:good kiosk:latest`
  (`kiosk:latest` ist dann ohnehin noch der alte Stand; das Umtaggen schadet nicht), dann ggf. die Datenbank
  wie unten zurückspielen und `docker compose up -d --no-build` (ohne `KIOSK_IMAGE`, also `kiosk:latest`).
  Zurückrollen nach einem erfolgreichen Deploy (auf das geprüfte Image davor):
  `cd /opt/kiosk && docker compose down && docker tag kiosk:previous kiosk:latest`. Wurde das Schema schon
  migriert, das Pre-Deploy-Backup dieses Deploys zurückspielen: jüngste Datei
  `/opt/kiosk/backups/kiosk-predeploy-<UTC-Zeitstempel>.db.gz` (`ls -1t /opt/kiosk/backups/kiosk-predeploy-*.db.gz | head -1`)
  entpacken: `gunzip -c <Datei> > /opt/kiosk/data/kiosk.db`, dann `data/kiosk.db-wal` und `data/kiosk.db-shm`
  löschen, `chown 1000:1000 /opt/kiosk/data/kiosk.db`. Dann `docker compose up -d --no-build`.
  (Ältere Sicherungen ohne `.gz` direkt kopieren.)
- Benutzer verwalten vom Laptop aus: `bash deploy/user.sh <befehl> ...` führt per SSH
  `docker compose exec -T kiosk python -m app.users <befehl> ...` im laufenden Container aus
  (Befehle siehe README, Abschnitt Benutzer), z. B. `bash deploy/user.sh add anna --name "Anna"`,
  `bash deploy/user.sh passwd anna`, `bash deploy/user.sh list`. Bei `add` und `passwd` fragt das Skript das
  Passwort lokal zweimal verdeckt ab und reicht es über stdin (`--password-stdin`) durch; es steht nie in
  einer Kommandozeile oder Shell-History. `remove` fragt lokal nach und ruft dann mit `--yes` auf.
  Anderer SSH-Alias: `BOX=... bash deploy/user.sh ...`.
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
  und komprimiert sie mit gzip: täglich `/opt/kiosk/backups/kiosk-<UTC-Zeitstempel>.db.gz` (Modus 600, die
  neuesten 7 bleiben) und vor jedem Deploy `/opt/kiosk/backups/kiosk-predeploy-<UTC-Zeitstempel>.db.gz` (eigene
  Rotation, die neuesten 3 bleiben; tägliche Läufe löschen sie nicht). Alte unkomprimierte `.db`-Sicherungen
  zählen bei der Rotation mit und laufen so aus. Reste abgebrochener Läufe (`*.part`, älter als eine Stunde)
  werden beim nächsten Lauf gelöscht. Nie zwei Läufe gleichzeitig (`flock` auf `/opt/kiosk/backups/.backup.lock`):
  Ein täglicher Lauf wird dann mit Exit 0 und einer Meldung übersprungen, einer vor dem Deploy wartet bis zu
  300 s (`KIOSK_BACKUP_LOCK_WAIT`) und bricht sonst mit Fehler ab. `secret.key` wird einmalig daneben gelegt. Von Hand:
  `/opt/kiosk/bin/backup.sh` bzw. `/opt/kiosk/bin/backup.sh predeploy`.
- Fehlgeschlagene Backups: Das Skript meldet `FEHLER: Kiosk-Backup (<Modus>) fehlgeschlagen` und endet mit
  Exit-Code ungleich 0; der Cron-Eintrag schreibt dann zusätzlich `backup failed` mit Priorität `user.err`
  ins Journal: `journalctl -t kiosk-backup -p err`.
- Wiederherstellen: Container stoppen (`docker compose down`), `gunzip -c <Sicherung>.db.gz > /opt/kiosk/data/kiosk.db`,
  `kiosk.db-wal`/`kiosk.db-shm` löschen, `chown 1000:1000 /opt/kiosk/data/kiosk.db`, `docker compose up -d --no-build`.
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
