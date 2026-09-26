#!/usr/bin/env bash
# Deployt Kiosk auf die gemeinsame Hetzner-Box. Aufruf aus dem Repo-Root: bash deploy/deploy.sh
set -euo pipefail
BOX="${BOX:-kiosk-server}"
DEST=/opt/kiosk
HOST=kiosk.example.org
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
  Datenbank zurück (Schema evtl. schon migriert): Pre-Deploy-Backup
    ${predeploy:-<keins angelegt>}
  gunzip -c <Backup> > $DEST/data/kiosk.db, $DEST/data/kiosk.db-wal und -shm löschen,
  chown 1000:1000 $DEST/data/kiosk.db
  cd $DEST && docker compose up -d --no-build
MSG
}

echo "== Code nach $BOX:$DEST"
ssh "$BOX" "mkdir -p $DEST/data"
# /bin/ und /backups/ gibt es nur auf dem Server; ohne Exclude würde --delete sie löschen.
rsync -az --delete --itemize-changes \
  --exclude /data/ --exclude /bin/ --exclude /backups/ --exclude .env \
  --exclude venv/ --exclude .venv/ --exclude '__pycache__/' \
  --exclude .git/ --exclude '*.pyc' --exclude .pytest_cache/ \
  ./ "$BOX:$DEST/"

echo "== .env"
ssh "$BOX" "cd $DEST && if [ ! -f .env ]; then
  umask 077
  pw=\$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')
  printf 'KIOSK_PASSWORD=%s\nKIOSK_FETCH_INTERVAL=900\nKIOSK_RETENTION_DAYS=90\n' \"\$pw\" > .env
  chmod 600 .env
  echo 'Neue .env angelegt. Passwort steht in $DEST/.env'
else echo '.env vorhanden, unverändert'; fi
chown -R 1000:1000 $DEST/data"

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

echo "== Container starten"
ssh "$BOX" "cd $DEST && KIOSK_IMAGE=kiosk:candidate docker compose up -d --no-build --remove-orphans"

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
echo "Fertig: https://$HOST (Build $BUILD)"
