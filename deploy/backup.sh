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
# Als root (Cron, deploy.sh) fasst das Skript die Dateien im Datenverzeichnis nie selbst an: Das
# gehört dem Container (uid 10100), der dort z. B. Symlinks auf Systemdateien legen könnte. SQLite
# läuft deshalb in einem eigenen Prozess als Besitzer des Datenverzeichnisses (uid und gid des
# Verzeichnisses selbst; überschreibbar mit KIOSK_BACKUP_UID/KIOSK_BACKUP_GID, nie root) und reicht
# die komprimierte Kopie über stdout durch; root schreibt nur in sein eigenes,
# privates Backup-Verzeichnis. secret.key liest root nur ohne Symlinks (O_NOFOLLOW) und nur als
# einfache Datei.
# Nie zwei Läufe gleichzeitig (flock auf <Backup-Verzeichnis>/.backup.lock): Ein täglicher Lauf
# endet dann mit Exit 0 und einer Meldung; ein Lauf vor dem Deploy wartet bis zu
# KIOSK_BACKUP_LOCK_WAIT (300) Sekunden, weil deploy.sh sonst ohne Sicherung weitermachen würde.
# Die Sicherung selbst darf höchstens KIOSK_BACKUP_TIMEOUT (600) Sekunden dauern.
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
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

# Läuft als Besitzer des Datenverzeichnisses, nie als root: sichert per
# Online-Backup-API in eine Arbeitskopie im Datenverzeichnis, prüft sie, schreibt sie gzip-komprimiert
# nach stdout und meldet die Zahl der Artikel als JSON auf dem Dateideskriptor argv[2].
DUMP = r"""
import gzip, json, os, shutil, sqlite3, sys, tempfile, time
from pathlib import Path

db, meta_fd = Path(sys.argv[1]), int(sys.argv[2])
os.umask(0o077)
# Arbeitskopien abgebrochener Läufe (älter als eine Stunde) entfernen.
for old in db.parent.glob(".backup-*"):
    try:
        if not old.is_symlink() and old.is_dir() and time.time() - old.lstat().st_mtime > 3600:
            shutil.rmtree(old)
    except OSError:
        pass
try:
    work = Path(tempfile.mkdtemp(prefix=".backup-", dir=db.parent))
except OSError as e:
    sys.exit(f"Arbeitskopie in {db.parent} nicht möglich: {e}")
try:
    copy = work / "kopie.db"
    articles = None
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(copy)
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
    with open(copy, "rb") as fin, gzip.GzipFile(filename="", mode="wb", fileobj=sys.stdout.buffer,
                                                compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, 1024 * 1024)
    sys.stdout.buffer.flush()
    with os.fdopen(meta_fd, "w") as meta:
        json.dump({"articles": articles}, meta)
finally:
    shutil.rmtree(work, ignore_errors=True)
"""

db, backup_dir, keep, mode = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
PREDEPLOY = "kiosk-predeploy-"
prefix = PREDEPLOY if mode == "predeploy" else "kiosk-"
try:
    if not stat.S_ISREG(os.lstat(db).st_mode):
        sys.exit(f"{db} ist keine einfache Datei (Symlink?), Sicherung abgebrochen")
except FileNotFoundError:
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

# Als root: SQLite nur ohne root-Rechte (siehe Kopf des Skripts), ohne Zusatzgruppen, ohne
# Umgebung (-I) und mit / als Arbeitsverzeichnis. Standard ist der Besitzer des Datenverzeichnisses:
# Der Container kann es weder ersetzen (Quelle seines Bind-Mounts, das Elternverzeichnis gehört
# root) noch einem anderen Benutzer übertragen (keine Capabilities). So passt es auch während des
# Umzugs von uid 1000 auf 10100 (deploy.sh stellt data/ erst direkt vor dem Start um).
drop = {}
if os.geteuid() == 0:
    dir_st = os.lstat(db.parent)
    if not stat.S_ISDIR(dir_st.st_mode):
        sys.exit(f"{db.parent} ist kein Verzeichnis (Symlink?), Sicherung abgebrochen")
    env_uid, env_gid = os.environ.get("KIOSK_BACKUP_UID"), os.environ.get("KIOSK_BACKUP_GID")
    uid = int(env_uid) if env_uid else dir_st.st_uid
    gid = int(env_gid) if env_gid else (uid if env_uid else dir_st.st_gid)
    if uid == 0 or gid == 0:
        sys.exit(f"{db.parent} gehört root: Sicherung nicht als root. Erst chown -R 10100:10100 "
                 f"{db.parent} (oder KIOSK_BACKUP_UID/KIOSK_BACKUP_GID setzen)")
    drop = {"user": uid, "group": gid, "extra_groups": []}

target = backup_dir / (prefix + time.strftime("%Y%m%dT%H%M%SZ.db.gz", time.gmtime()))
tmp_gz = backup_dir / (target.name + ".part")
try:
    read_meta, write_meta = os.pipe()
    with open(tmp_gz, "wb") as out:
        try:
            proc = subprocess.Popen([sys.executable, "-I", "-c", DUMP, str(db), str(write_meta)],
                                    stdout=out, pass_fds=(write_meta,), cwd="/", **drop)
        finally:
            os.close(write_meta)
        # Höchstens KIOSK_BACKUP_TIMEOUT Sekunden: Hält ein (kompromittierter) Container die
        # Datenbank gesperrt, wartete die Online-Backup-API sonst ewig und mit ihr Cron und Deploy.
        # Die Arbeitskopie des getöteten Prozesses räumt ein späterer Lauf weg (älter als 1 h).
        timeout = int(os.environ.get("KIOSK_BACKUP_TIMEOUT") or 600)
        if timeout <= 0:  # 0 oder negativ hieße: sofort abbrechen
            timeout = 600
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            os.close(read_meta)
            sys.exit(f"Sicherung nach {timeout} s abgebrochen (Datenbank gesperrt?)")
        with os.fdopen(read_meta, "r") as meta_in:
            meta_raw = meta_in.read()   # der Prozess ist beendet: kein Warten mehr
        if rc != 0:
            sys.exit(1)  # Meldung kam schon vom Sicherungsprozess
    articles = json.loads(meta_raw)["articles"]
    os.chmod(tmp_gz, 0o600)
    os.replace(tmp_gz, target)
finally:
    tmp_gz.unlink(missing_ok=True)

# Das Secret einmalig daneben legen: Ohne es sind Sessions nach einer Wiederherstellung ungültig,
# die Daten selbst brauchen es nicht. Nie einem Symlink folgen und nur eine einfache Datei ohne
# weitere harte Links kopieren (das Datenverzeichnis gehört dem Container).
secret = db.parent / "secret.key"
secret_copy = backup_dir / "secret.key"
if not secret_copy.exists():
    try:
        fd = os.open(secret, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY)
    except FileNotFoundError:
        fd = None
    except OSError as e:
        print(f"Warnung: {secret} nicht kopiert ({e.strerror})", file=sys.stderr)
        fd = None
    if fd is not None:
        with os.fdopen(fd, "rb") as fin:
            st = os.fstat(fin.fileno())
            if stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_size <= 4096:
                data = fin.read(4096)
                with os.fdopen(os.open(secret_copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as fout:
                    fout.write(data)
            else:
                print(f"Warnung: {secret} ist keine einfache, kleine Datei, nicht kopiert", file=sys.stderr)

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
