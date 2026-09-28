#!/usr/bin/env bash
# Deployt Kiosk auf die gemeinsame Hetzner-Box. Aufruf aus dem Repo-Root: bash deploy/deploy.sh
set -euo pipefail
BOX="${BOX:-kiosk-server}"
DEST=/opt/kiosk
HOST=kiosk.example.org
# Zusätzlich gesperrte Abrufziele (KIOSK_FETCH_DENY_IPS): die eigenen Adressen der Box, damit ein
# Feed oder Artikel nicht auf Dienste des Servers selbst zeigen kann. Wird in eine bestehende .env
# nur eingetragen, wenn der Schlüssel dort fehlt; ein vorhandener Wert bleibt. Kein ganzes
# Provider-Netz eintragen (2a01:4f8::/32 sperrte auch dort gehostete Nachrichtenseiten).
FETCH_DENY_IPS="203.0.113.10,2001:db8:1234:5678::/64"
cd "$(dirname "$0")/.."
BUILD=$(date -u +%Y%m%dT%H%M%SZ)
# Wartezeit auf /healthz nach dem Start (Sekunden); die Schema-Migration kann bei großen
# Datenbanken dauern (ca. 2 ms pro Artikel), siehe deploy/README.md.
HEALTH_WAIT="${KIOSK_HEALTH_WAIT:-300}"
case "$HEALTH_WAIT" in
  ''|*[!0-9]*) echo "KIOSK_HEALTH_WAIT muss eine Zahl (Sekunden) sein, ist '$HEALTH_WAIT'" >&2; exit 2 ;;
esac

# Gebaut wird nach kiosk:candidate (KIOSK_IMAGE, siehe docker-compose.yml); kiosk:latest und
# kiosk:good zeigen erst nach der Prüfung über Caddy auf das neue Image (siehe Ende). Bis dahin bleibt
# kiosk:latest der bisherige Stand, ein gescheiterter Deploy verändert es nicht.
# Ziel zum Zurückrollen: kiosk:good ist das zuletzt erfolgreich geprüfte Image. Fehlt es (erster
# Deploy mit diesem Schema), das gerade laufende Image.
good_image=$(ssh "$BOX" "docker image inspect -f '{{.Id}}' kiosk:good 2>/dev/null" || true)
prev_image=$(ssh "$BOX" "docker inspect -f '{{.Image}}' kiosk 2>/dev/null" || true)
predeploy=""

rollback_hint() {
  local target
  if [ -n "$good_image" ]; then target="kiosk:good"; else target="${prev_image:-<kein vorheriges Image, erster Deploy>}"; fi
  cat >&2 <<MSG
Deploy abgebrochen, der neue Container (kiosk:candidate) läuft evtl. schon.
Zurückrollen auf den letzten geprüften Stand:
  cd $DEST && docker compose down
  docker tag $target kiosk:latest
  chown -R 10100:10100 $DEST/data
  Datenbank zurück nur bei einem Ziel-Image mit Schema < 6 (Schema 7 -> 6 braucht das nicht, 7 fügt
  nur eine Spalte hinzu; Zurückspielen verwirft Konten und Lesestand seit dem Deploy): Pre-Deploy-Backup
    ${predeploy:-<keins angelegt>}
  bei gestopptem Container; geschrieben wird nur als 10100, root folgt so keinem Symlink aus dem Container:
  cd / && gunzip -c <Backup> | setpriv --reuid=10100 --regid=10100 --clear-groups \
    sh -c 'cd $DEST/data && rm -f kiosk.db kiosk.db-wal kiosk.db-shm && umask 077 && cat > kiosk.db'
  cd $DEST && docker compose up -d --no-build
  (docker-compose.yml setzt user: 10100:10100, auch das ältere Image läuft damit als 10100;
  data/ gehört 10100, nicht auf 1000 zurückstellen)
MSG
}

# Scheitert das Umstellen von data/ oder der Start selbst: Das Schema ist dann noch nicht migriert
# (der neue Container lief nicht an), eine Datenbank muss nicht zurück.
start_hint() {
  local target
  if [ -n "$good_image" ]; then target="kiosk:good"; else target="${prev_image:-<kein vorheriges Image, erster Deploy>}"; fi
  cat >&2 <<MSG
Deploy abgebrochen beim Umstellen von data/ auf uid 10100 oder beim Start des neuen Containers.
Keine Datenbank-Wiederherstellung nötig. Weiter mit dem letzten geprüften Stand:
  cd $DEST && docker compose down
  docker tag $target kiosk:latest
  chown -R 10100:10100 $DEST/data && chmod 700 $DEST/data
  cd $DEST && docker compose up -d --no-build
  (docker-compose.yml setzt user: 10100:10100, das ältere Image läuft damit als 10100 und kann
  data/ schreiben; data/ nicht auf 1000 zurückstellen. Lief der neue Container doch schon an,
  gilt der Hinweis mit dem Pre-Deploy-Backup: ${predeploy:-<keins angelegt>})
Brach nur die SSH-Verbindung ab, läuft der Schritt auf der Box zu Ende; erst nachsehen:
  cat $DEST/backups/deploy-start.log; docker ps --filter name=kiosk
MSG
}

echo "== Code nach $BOX:$DEST"
# Allowlist: Auf die Box kommt nur, was Build und Betrieb brauchen: Dockerfile, .dockerignore,
# docker-compose.yml, requirements.txt, app/, static/, sources.yaml, deploy/ (backup.sh,
# Caddyfile.kiosk), dazu README.md und .env.example als Doku. Und zwar der committete Stand
# (git archive HEAD), nie das Arbeitsverzeichnis: keine halbfertigen Änderungen, keine
# ungetrackten Dateien (.env*, Testdatenbanken, venv). Ältere Deploys luden auch
# Entwicklungsdateien hoch; die entfernt der Schritt "Besitzer und Rechte".
DEPLOY_PATHS=(Dockerfile .dockerignore docker-compose.yml requirements.txt app static sources.yaml deploy
              README.md .env.example)
dirty=$(git status --porcelain --untracked-files=no -- "${DEPLOY_PATHS[@]}")
if [ -n "$dirty" ] && [ "${KIOSK_ALLOW_DIRTY:-}" != 1 ]; then
  echo "Nicht committete Änderungen an deploybaren Dateien; deployt würde nur HEAD:" >&2
  printf '%s\n' "$dirty" >&2
  echo "Erst committen (oder KIOSK_ALLOW_DIRTY=1 setzen, dann geht HEAD ohne diese Änderungen raus)." >&2
  exit 2
fi
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
git archive --format=tar HEAD -- "${DEPLOY_PATHS[@]}" | tar -x -C "$stage"
echo "Stand: $(git rev-parse --short HEAD)"
ssh "$BOX" "mkdir -p $DEST/data"
# /data/, /bin/ und /backups/ gibt es nur auf dem Server, ebenso .env: Ausgeschlossenes löscht
# --delete nicht. .env* als Muster, damit nie eine Umgebungsdatei hochgeladen oder gelöscht wird
# (außer der Doku .env.example).
# Auf dem Server gehört der Code root und ist nur für root schreibbar (sonst kämen uid und
# Modi 775/664 vom Laptop mit). Nichts davon wird direkt ausgeführt: backup.sh installiert der
# nächste Schritt mit eigenem Modus, deploy.sh und user.sh laufen auf dem Laptop.
rsync -az --delete --itemize-changes --chown=root:root --chmod=D755,F644 \
  --exclude /data/ --exclude /bin/ --exclude /backups/ --include /.env.example --exclude '.env*' \
  "$stage/" "$BOX:$DEST/"

echo "== .env"
# Neue .env mit Zufallspasswort (nie ausgegeben); KIOSK_FETCH_DENY_IPS wird ergänzt, wenn der
# Schlüssel fehlt, ein vorhandener Wert bleibt. Immer root, Modus 600.
ssh "$BOX" bash -s -- "$DEST" "$FETCH_DENY_IPS" <<'ENV'
set -euo pipefail
cd "$1"
deny=$2
umask 077
if [ ! -f .env ]; then
  pw=$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')
  printf 'KIOSK_PASSWORD=%s\nKIOSK_FETCH_INTERVAL=900\nKIOSK_RETENTION_DAYS=90\n' "$pw" > .env
  echo "Neue .env angelegt. Passwort steht in $1/.env"
else
  echo ".env vorhanden"
fi
if ! grep -q '^[[:space:]]*KIOSK_FETCH_DENY_IPS=' .env; then
  if [ -n "$(tail -c1 .env)" ]; then echo >> .env; fi   # letzte Zeile ohne Zeilenende
  printf 'KIOSK_FETCH_DENY_IPS=%s\n' "$deny" >> .env
  echo "KIOSK_FETCH_DENY_IPS=$deny in .env ergänzt"
fi
chown root:root .env
chmod 600 .env
ENV

echo "== Besitzer und Rechte (Code)"
# Idempotent bei jedem Deploy. Code, Konfiguration, bin/ und backups/: root, für andere nicht
# schreibbar (frühere Deploys brachten uid 1000 und 775/664 vom Laptop mit). data/ bleibt hier
# unberührt: Der alte Container läuft bis zum Start des neuen weiter und braucht es unverändert
# (siehe "Datenverzeichnis" direkt vor dem Start).
ssh "$BOX" bash -s -- "$DEST" <<'PERMS'
set -euo pipefail
dest=$1
# Schutz für die rm -rf weiter unten: nie ein leerer oder relativer Pfad
case $dest in /?*) ;; *) echo "Ungültiges Ziel: '$dest'" >&2; exit 1 ;; esac
chown root:root "$dest"
chmod go-w "$dest"
find "$dest" -mindepth 1 -path "$dest/data" -prune -o \( ! -user root -o ! -group root \) \
  -exec chown -h root:root {} +
find "$dest" -mindepth 1 -path "$dest/data" -prune -o ! -type l -perm /022 -exec chmod go-w {} +
# Entwicklungsdateien früherer Deploys (rsync lädt sie nicht mehr hoch, --delete räumt
# ausgeschlossene Pfade nicht weg).
rm -rf -- "$dest/.claude" "$dest/design" "$dest/docs" "$dest/tests" "$dest/pytest.ini" \
  "$dest/requirements-dev.txt" "$dest/.gitignore"
echo "Rechte gesetzt: Code root, für andere nicht schreibbar"
PERMS

echo "== Backup-Skript und Cron"
# Wird bei jedem Deploy überschrieben (idempotent).
ssh "$BOX" "install -d -m 700 $DEST/bin $DEST/backups
install -m 700 $DEST/deploy/backup.sh $DEST/bin/backup.sh
printf '%s\n' '# Verwaltet von kiosk deploy/deploy.sh, wird bei jedem Deploy überschrieben' \
  '30 3 * * * root { $DEST/bin/backup.sh || logger -t kiosk-backup -p user.err \"backup failed\"; } 2>&1 | logger -t kiosk-backup' \
  > /etc/cron.d/kiosk-backup
chmod 644 /etc/cron.d/kiosk-backup"

# Bis zum Start des neuen Containers läuft der alte unverändert weiter: Fehler hier brauchen
# keinen Rückweg.
old_keeps_running() {
  echo "Deploy abgebrochen vor dem Start des neuen Containers. Alter Container läuft weiter, nichts zu tun." >&2
  # Das neue docker-compose.yml (user: 10100) liegt schon auf der Box. Gehört data/ noch einer
  # anderen uid (erster Deploy nach dem Wechsel), startete ein händisches `docker compose up`
  # den Container als 10100 ohne Zugriff auf data/.
  cat >&2 <<EOF
Achtung: Gehört $DEST/data noch nicht uid 10100 (ls -ln $DEST), vor dem nächsten erfolgreichen
Deploy kein 'docker compose up' von Hand ausführen. Sonst zuerst:
  chown -R 10100:10100 $DEST/data
EOF
  exit 1
}

echo "== Container bauen ($BUILD)"
if ! ssh "$BOX" "cd $DEST && KIOSK_IMAGE=kiosk:candidate docker compose build --pull -q --build-arg KIOSK_BUILD=$BUILD"; then
  old_keeps_running
fi

echo "== Backup vor dem Start"
# Eigene Dateien kiosk-predeploy-<UTC>.db.gz, getrennt von der täglichen Rotation. backup.sh
# nennt auch die Zahl der Artikel (gezählt in der Sicherungskopie, die Live-Datenbank wird nur
# lesend geöffnet), damit die Dauer einer Schema-Migration abschätzbar ist.
if ! out=$(ssh "$BOX" "if [ -f $DEST/data/kiosk.db ]; then $DEST/bin/backup.sh predeploy; else echo 'Noch keine Datenbank, kein Backup'; fi"); then
  echo "$out"
  old_keeps_running
fi
echo "$out"
predeploy=$(printf '%s\n' "$out" | sed -n 's/^Backup: \([^ ]*\) .*/\1/p')
articles=$(printf '%s\n' "$out" | sed -n 's/^Artikel: \([0-9]*\)$/\1/p')
if [ -n "$articles" ]; then
  echo "Bestehende Datenbank: $articles Artikel; eine Migration von Schema 1 dauert ca. $(( articles * 2 / 1000 + 1 )) s (2 ms pro Artikel), /healthz-Wartezeit ${HEALTH_WAIT} s"
fi

# Ab hier läuft der Umbau: Jeder Fehler (auch im Caddy-Schritt) zeigt den Weg zurück.
# Explizite Prüfungen (if ! ...) rufen rollback_hint selbst auf, ERR greift dort nicht.
trap rollback_hint ERR

echo "== Datenverzeichnis für uid 10100 und Container starten"
# Kiosk läuft als eigene uid/gid 10100 (Dockerfile, docker-compose.yml user:); frühere Versionen
# liefen als 1000, die auf der Box maildigest gehört. Umstellen und Start in EINEM Aufruf auf der
# Box: Bricht die SSH-Verbindung dazwischen ab, liefe sonst der alte Container (uid 1000) gegen ein
# data/, das er nicht mehr lesen darf. Bis dahin arbeitet er mit seinen offenen Dateien weiter.
# data/ gehört danach 10100 und ist nur für ihn lesbar: Verzeichnis 700, Datenbank und Secret 600.
# chown -R folgt keinen Symlinks (-P ist Standard); das chmod der Dateien läuft als 10100, damit
# root keinem Symlink folgt, den der Container dort anlegen könnte. cd /: Die SSH-Sitzung beginnt
# in /root (700); find als 10100 bräche dort ab ("Konnte nicht ... zurückkehren").
# Robust gegen einen Abbruch der SSH-Verbindung (oder Strg-C) zwischen chown und compose up: Das
# Skript wird als Ganzes gelesen ({ ... }), ignoriert HUP, PIPE und INT und schreibt nach
# backups/deploy-start.log (am Ende an SSH weitergereicht). Es hält das Lock von backup.sh, damit
# der tägliche Lauf um 03:30 nicht dazwischen kommt. Exit 42: nichts verändert.
rc=0
ssh "$BOX" bash -s -- "$DEST" <<'START' || rc=$?
{
set -euo pipefail
trap '' HUP PIPE INT
cd /
dest=$1
mkdir -p "$dest/backups"
log="$dest/backups/deploy-start.log"
exec 3>&1 >"$log" 2>&1
trap 'rc=$?; cat "$log" >&3 2>/dev/null || true; exit $rc' EXIT
# 10100 darf auf dem Host niemandem sonst gehören (sonst bekäme dieser Benutzer data/).
for db in passwd group; do
  if owner=$(getent "$db" 10100); then
    echo "FEHLER: 10100 gehört auf dem Host schon ${owner%%:*} ($db); data/ bleibt unverändert."
    exit 42
  fi
done
exec 9>"$dest/backups/.backup.lock"
if ! flock -w 300 9; then
  echo "FEHLER: Ein Kiosk-Backup hält das Lock seit über 300 s; data/ bleibt unverändert."
  exit 42
fi
mkdir -p "$dest/data"
chown -R 10100:10100 "$dest/data"
chmod 700 "$dest/data"
setpriv --reuid=10100 --regid=10100 --clear-groups \
  find "$dest/data" -maxdepth 1 -type f \( -name 'kiosk.db*' -o -name secret.key \) ! -perm 600 \
  -exec chmod 600 {} +
echo "data/ gehört 10100:10100 (700, Datenbank und Secret 600)"
cd "$dest"
KIOSK_IMAGE=kiosk:candidate docker compose up -d --no-build --remove-orphans
exit 0
}
START
if [ "$rc" = 42 ]; then
  old_keeps_running
elif [ "$rc" != 0 ]; then
  start_hint
  exit 1
fi

echo "== Warten auf /healthz (bis ${HEALTH_WAIT} s)"
# Nach einem Update kann die Schema-Migration beim Start dauern (KIOSK_HEALTH_WAIT, Standard 300 s).
if ! ssh "$BOX" bash -s -- "$HEALTH_WAIT" <<'REMOTE'
wait=$1
end=$(( $(date +%s) + wait ))
while [ "$(date +%s)" -lt "$end" ]; do
  if curl -sf --max-time 3 http://127.0.0.1:8100/healthz; then echo; exit 0; fi
  sleep 2
done
echo "Kiosk antwortet nach $wait s nicht auf /healthz. Logs: docker logs --tail 100 kiosk" >&2
exit 1
REMOTE
then
  rollback_hint
  exit 1
fi

echo "== Caddy"
# Der Kiosk-Block steht zwischen Markern in der gemeinsamen /etc/caddy/Caddyfile. Wir bauen eine
# Kandidatendatei, validieren sie und ersetzen die echte Datei nur bei Erfolg (atomar per mv).
ssh "$BOX" bash -s -- "$HOST" "$DEST/deploy/Caddyfile.kiosk" <<'REMOTE'
set -euo pipefail
host=$1
block=$2
live=/etc/caddy/Caddyfile
# Gleiches Verzeichnis: relative imports funktionieren beim Validieren, und mv ist atomar.
tmp=$(mktemp /etc/caddy/.Caddyfile.kiosk-XXXXXX)
trap 'rm -f "$tmp" "$tmp.log"' EXIT

python3 - "$live" "$block" "$host" "$tmp" <<'PY'
import sys

live, block_path, host, out = sys.argv[1:]
BEGIN = "# >>> kiosk (managed by deploy.sh)"
END = "# <<< kiosk"
lines = open(live, encoding="utf-8").read().splitlines(keepends=True)
block = open(block_path, encoding="utf-8").read().strip("\n") + "\n"

begins = [i for i, l in enumerate(lines) if l.strip() == BEGIN]
ends = [i for i, l in enumerate(lines) if l.strip() == END]
if begins or ends:
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        sys.exit("Kiosk-Marker in der Caddyfile sind unvollständig oder doppelt, bitte von Hand prüfen")
    result = lines[:begins[0]] + [block] + lines[ends[0] + 1:]
else:
    # Einmalige Migration: alter Block ohne Marker, als letzter Block der Datei angehängt.
    starts = [i for i, l in enumerate(lines) if l.strip() == host + " {"]
    if len(starts) > 1:
        sys.exit("Kiosk-Host mehrfach in der Caddyfile, bitte von Hand prüfen")
    if starts:
        depth, close = 0, None
        for i in range(starts[0], len(lines)):
            depth += lines[i].count("{") - lines[i].count("}")
            if depth == 0:
                close = i
                break
        if close is None or any(l.strip() for l in lines[close + 1:]):
            sys.exit("Alter Kiosk-Block ist nicht der letzte Block der Caddyfile, bitte von Hand prüfen")
        lines = lines[:starts[0]]
        print("Alter Kiosk-Block ohne Marker wird ersetzt")
    head = "".join(lines).rstrip("\n")
    result = [head + "\n\n" if head else "", block]
with open(out, "w", encoding="utf-8") as f:
    f.write("".join(result))
PY

if ! caddy validate --config "$tmp" --adapter caddyfile >"$tmp.log" 2>&1; then
  echo "Caddy-Konfiguration ungültig, $live bleibt unverändert:" >&2
  cat "$tmp.log" >&2
  exit 1
fi
if cmp -s "$tmp" "$live"; then
  echo "Caddy-Block unverändert"
  exit 0
fi
bak="$live.bak-$(date +%Y%m%d%H%M%S)"
cp -a "$live" "$bak"
chown --reference="$live" "$tmp"
chmod --reference="$live" "$tmp"
mv "$tmp" "$live"
if ! systemctl reload caddy; then
  # Ein gescheitertes Reload lässt Caddy mit der alten Konfiguration weiterlaufen; die Datei
  # muss wieder dazu passen, sonst greift die neue beim nächsten Neustart von Caddy.
  mv "$bak" "$live"
  echo "systemctl reload caddy fehlgeschlagen, vorherige $live wiederhergestellt." >&2
  systemctl status caddy --no-pager -n 20 >&2 || true
  exit 1
fi
echo "Caddy-Block aktualisiert und neu geladen"
REMOTE

echo "== Prüfung: Login über Caddy"
# Ende-zu-Ende über HTTPS, Caddy und die Proxy-Header, ohne Zugangsdaten:
#  1. GET /api/session antwortet 200 mit {"authenticated": false, ...} (Caddy und App laufen).
#  2. POST /api/login mit richtigem Origin, einem zufälligen, nicht existierenden Benutzernamen und
#     falschem Passwort antwortet 401 (Origin-/CSRF-Prüfung, Login-Pfad, Datenbank). 403 hieße: Origin
#     abgelehnt, 429: Login-Bremse.
# Kein echtes Konto wird belastet (die Bremse je Benutzername trifft nur den Zufallsnamen); der
# Fehlversuch zählt aber gegen die IP der Box (8 pro 15 Minuten, siehe README).
if ! ssh "$BOX" bash -s -- "$HOST" <<'REMOTE'
set -euo pipefail
host=$1
session=$(curl -sS --max-time 20 -w '\n%{http_code}' "https://$host/api/session") || session=$'\n000'
code=${session##*$'\n'}
if [ "$code" != "200" ] || ! printf '%s' "${session%$'\n'*}" | grep -Eq '"authenticated": ?false'; then
  echo "FEHLER: https://$host/api/session antwortet nicht wie erwartet (HTTP $code, erwartet 200 mit authenticated false)." >&2
  echo "Der neue Container läuft weiter. Diagnose: docker logs --tail 100 kiosk; journalctl -u caddy -n 50" >&2
  exit 1
fi
echo "Sitzung über Caddy: HTTP 200, nicht angemeldet"
probe="deploy-probe-$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
code=$(printf '{"username":"%s","password":"kein-echtes-passwort"}' "$probe" \
  | curl -sS -o /dev/null -w '%{http_code}' --max-time 20 \
    -H "Origin: https://$host" -H 'Content-Type: application/json' \
    -X POST "https://$host/api/login" --data-binary @-) || true
if [ "$code" != "401" ]; then
  echo "FEHLER: Login-Probe über https://$host/api/login lieferte HTTP ${code:-000}, erwartet 401 (falsche Zugangsdaten)." >&2
  echo "Der neue Container läuft weiter. Diagnose: docker logs --tail 100 kiosk; journalctl -u caddy -n 50" >&2
  exit 1
fi
echo "Login-Probe über Caddy: HTTP 401 wie erwartet"
REMOTE
then
  rollback_hint
  exit 1
fi
trap - ERR

# Erst jetzt ist der Deploy erfolgreich. kiosk:latest und kiosk:good zeigen auf das geprüfte
# Image (das der laufende Container nutzt, gebaut als kiosk:candidate); kiosk:previous auf das
# geprüfte davor (abgeleitet aus dem alten kiosk:good, nicht aus dem vorher laufenden Container,
# der ein ungeprüfter Fehlversuch sein kann). Unverändertes Image: previous bleibt.
# Der Container bleibt stehen: Alle Tags zeigen auf dieselbe Image-ID, ein Neustart ist unnötig.
echo "== Images markieren"
ssh "$BOX" bash -s <<'TAG'
set -euo pipefail
new=$(docker inspect -f '{{.Image}}' kiosk)
old=$(docker image inspect -f '{{.Id}}' kiosk:good 2>/dev/null || true)
if [ -n "$old" ] && [ "$old" != "$new" ]; then
  docker tag "$old" kiosk:previous
  echo "kiosk:previous = bisheriges kiosk:good ($old)"
fi
docker tag "$new" kiosk:latest
docker tag "$new" kiosk:good
echo "kiosk:latest = kiosk:good = $new"
TAG
ssh "$BOX" "ss -ltnp | grep 8100" || true

# Das Startpasswort aus .env legt nur beim allerersten Start den Admin an; danach liegt es nutzlos im Klartext in
# .env. Nur ein Hinweis, die Datei auf dem Server wird nicht automatisch geändert.
ssh "$BOX" bash -s -- "$DEST" <<'PWCHECK' || true
dest=$1
if grep -q '^[[:space:]]*KIOSK_PASSWORD=' "$dest/.env" 2>/dev/null; then
  users=$(docker exec kiosk python -c "import sqlite3; print(sqlite3.connect('file:/data/kiosk.db?mode=ro', uri=True).execute('SELECT COUNT(*) FROM users').fetchone()[0])" 2>/dev/null || echo "?")
  if [ "$users" != 0 ]; then
    cat >&2 <<MSG
WARNUNG: $dest/.env enthält noch KIOSK_PASSWORD, es gibt aber schon Benutzer ($users).
Das Startpasswort wird nicht mehr gebraucht (Passwörter: python -m app.users passwd <name>),
liegt aber im Klartext auf dem Server. Zeile von Hand entfernen und neu starten:
  sed -i '/^[[:space:]]*KIOSK_PASSWORD=/d' $dest/.env && cd $dest && docker compose up -d --no-build
MSG
  fi
fi
PWCHECK
echo "Fertig: https://$HOST (Build $BUILD)"
