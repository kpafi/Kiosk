#!/usr/bin/env bash
# Sichert die Kiosk-Datenbank. Läuft AUF der Box (per Cron täglich und vor jedem Deploy).
# Nutzt die Online-Backup-API von SQLite, funktioniert also auch bei laufendem Container (WAL).
# Die Kopie wird mit PRAGMA quick_check geprüft und dann mit gzip komprimiert.
# Aufruf: backup.sh [daily|predeploy]
#   daily (Standard, Cron): kiosk-<UTC>.db.gz, die neuesten KIOSK_BACKUP_KEEP (7) bleiben.
#   predeploy (deploy.sh):  kiosk-predeploy-<UTC>.db.gz, eigene Rotation, die neuesten
#                           KIOSK_PREDEPLOY_KEEP (3) bleiben; zählt nicht zu den täglichen.
# Ausgabe: "Backup: <Datei> (<Bytes> Bytes), <n> vorhanden" und, falls die Tabelle existiert,
# "Artikel: <Anzahl>" (deploy.sh liest beides). Bei einem Fehler: Meldung
# "FEHLER: Kiosk-Backup (<Modus>) fehlgeschlagen" auf stderr und Exit-Code ungleich 0.
# Pfade per Umgebung überschreibbar (für Tests): KIOSK_DB, KIOSK_BACKUP_DIR.
# Nie zwei Läufe gleichzeitig (flock auf <Backup-Verzeichnis>/.backup.lock): Ein täglicher Lauf
# endet dann mit Exit 0 und einer Meldung; ein Lauf vor dem Deploy wartet bis zu
# KIOSK_BACKUP_LOCK_WAIT (300) Sekunden, weil deploy.sh sonst ohne Sicherung weitermachen würde.
set -euo pipefail
mode=${1:-daily}
db=${KIOSK_DB:-/opt/kiosk/data/kiosk.db}
dir=${KIOSK_BACKUP_DIR:-/opt/kiosk/backups}
case "$mode" in
  daily) keep=${KIOSK_BACKUP_KEEP:-7} ;;
  predeploy) keep=${KIOSK_PREDEPLOY_KEEP:-3} ;;
  *) echo "Unbekannter Modus: $mode (daily oder predeploy)" >&2; exit 2 ;;
esac

umask 077
mkdir -p "$dir"
chmod 700 "$dir"
exec 9>"$dir/.backup.lock"
if [ "$mode" = daily ]; then
  if ! flock -n 9; then
    echo "Ein anderes Kiosk-Backup läuft gerade, dieser Lauf wird übersprungen"
    exit 0
  fi
elif ! flock -w "${KIOSK_BACKUP_LOCK_WAIT:-300}" 9; then
  echo "FEHLER: Kiosk-Backup ($mode): anderes Backup läuft noch, Wartezeit abgelaufen" >&2
  exit 1
fi

if ! python3 - "$db" "$dir" "$keep" "$mode" <<'PY'
import gzip
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

db, backup_dir, keep, mode = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
PREDEPLOY = "kiosk-predeploy-"
prefix = PREDEPLOY if mode == "predeploy" else "kiosk-"
if not db.exists():
    sys.exit(f"Keine Datenbank unter {db}, nichts zu sichern")
os.umask(0o077)
backup_dir.mkdir(parents=True, exist_ok=True)
os.chmod(backup_dir, 0o700)
# Reste abgebrochener Läufe (z. B. voller Datenträger, Neustart) entfernen. Nur ältere als eine
# Stunde: Das Lock schützt vor parallelen Läufen dieses Skripts, aber nicht vor einer älteren
# Version ohne Lock oder einer Kopie von Hand.
for part in backup_dir.glob("*.part"):
    try:
        if time.time() - part.stat().st_mtime > 3600:
            part.unlink()
    except FileNotFoundError:
        pass

target = backup_dir / (prefix + time.strftime("%Y%m%dT%H%M%SZ.db.gz", time.gmtime()))
tmp_db = backup_dir / (target.name[:-len(".gz")] + ".part")
tmp_gz = backup_dir / (target.name + ".part")
articles = None
try:
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(tmp_db)
        try:
            src.backup(dst)
            ok = dst.execute("PRAGMA quick_check").fetchone()[0]
            try:
                articles = dst.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            except sqlite3.Error:
                pass  # (noch) keine Artikeltabelle
        finally:
            dst.close()
    finally:
        src.close()
    if ok != "ok":
        sys.exit(f"Backup fehlerhaft (quick_check: {ok})")
    with open(tmp_db, "rb") as fin, gzip.open(tmp_gz, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, 1024 * 1024)
    os.chmod(tmp_gz, 0o600)
    os.replace(tmp_gz, target)
finally:
    tmp_db.unlink(missing_ok=True)
    tmp_gz.unlink(missing_ok=True)

# Läuft das Skript als root, dürfen von SQLite angelegte -wal/-shm-Dateien nicht root gehören,
# sonst kann der Container (uid 1000) die Datenbank nicht mehr öffnen.
st = db.stat()
for suffix in ("-wal", "-shm"):
    side = Path(str(db) + suffix)
    try:
        if side.stat().st_uid != st.st_uid:
            os.chown(side, st.st_uid, st.st_gid)
    except FileNotFoundError:
        pass

# Das Secret einmalig daneben legen: Ohne es sind Sessions nach einer Wiederherstellung ungültig,
# die Daten selbst brauchen es nicht.
secret = db.parent / "secret.key"
secret_copy = backup_dir / "secret.key"
if secret.exists() and not secret_copy.exists():
    shutil.copyfile(secret, secret_copy)
    os.chmod(secret_copy, 0o600)

# Beide Arten getrennt rotieren: Tägliche Läufe dürfen die Sicherungen vor Deploys nicht verdrängen.
# Unkomprimierte .db aus der Zeit vor gzip zählen mit und laufen so nach und nach aus. Sortiert
# wird nach dem Zeitstempel im Namen.
def stamp(p: Path) -> str:
    return p.name[len(prefix):].split(".", 1)[0]

backups = sorted(
    (p for p in backup_dir.iterdir()
     if p.name.startswith(prefix) and p.name.endswith((".db", ".db.gz"))
     and (mode == "predeploy") == p.name.startswith(PREDEPLOY)),
    key=stamp,
)
for old in backups[:-keep] if keep > 0 else []:
    old.unlink()
print(f"Backup: {target} ({target.stat().st_size} Bytes), {min(len(backups), keep)} vorhanden")
if articles is not None:
    print(f"Artikel: {articles}")
PY
then
  echo "FEHLER: Kiosk-Backup ($mode) fehlgeschlagen" >&2
  exit 1
fi
