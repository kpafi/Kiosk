"""Prüft das Python-Stück aus deploy/deploy.sh, das den Kiosk-Block in der Caddyfile ersetzt."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOST = "kiosk.example.org"
BLOCK = (ROOT / "deploy" / "Caddyfile.kiosk").read_text(encoding="utf-8")
OTHER = "chat.example.org {\n\treverse_proxy 127.0.0.1:9000\n\theader {\n\t\tX-Foo bar\n\t}\n}\n"
OLD_UNMARKED = f"\n{HOST} {{\n\tencode zstd gzip\n\treverse_proxy 127.0.0.1:8100\n\theader {{\n\t\tX-Frame-Options DENY\n\t}}\n}}\n"


def _snippet() -> str:
    script = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    m = re.search(r"<<'PY'\n(.*?)\nPY\n", script, re.S)
    assert m, "Python-Stück in deploy.sh nicht gefunden"
    return m.group(1)


def run(tmp_path, live_text: str) -> subprocess.CompletedProcess:
    live = tmp_path / "Caddyfile"
    live.write_text(live_text, encoding="utf-8")
    out = tmp_path / "candidate"
    return subprocess.run(
        [sys.executable, "-", str(live), str(ROOT / "deploy" / "Caddyfile.kiosk"), HOST, str(out)],
        input=_snippet(), text=True, capture_output=True,
    )


def candidate(tmp_path) -> str:
    return (tmp_path / "candidate").read_text(encoding="utf-8")


def test_block_file_has_markers_and_headers():
    lines = BLOCK.strip().splitlines()
    assert lines[0] == "# >>> kiosk (managed by deploy.sh)" and lines[-1] == "# <<< kiosk"
    assert 'Strict-Transport-Security "max-age=31536000"' in BLOCK
    assert "script-src 'self'" in BLOCK and "frame-ancestors 'none'" in BLOCK
    assert BLOCK.count("{") == BLOCK.count("}")


def test_append_when_absent(tmp_path):
    r = run(tmp_path, OTHER)
    assert r.returncode == 0, r.stderr
    assert candidate(tmp_path) == OTHER + "\n" + BLOCK.strip("\n") + "\n"


def test_migrates_unmarked_block_at_end(tmp_path):
    r = run(tmp_path, OTHER + OLD_UNMARKED)
    assert r.returncode == 0, r.stderr
    out = candidate(tmp_path)
    assert out.startswith(OTHER)
    assert out.count(HOST + " {") == 1
    assert "# >>> kiosk (managed by deploy.sh)" in out
    # Zweiter Lauf auf dem Ergebnis ändert nichts mehr (idempotent).
    r2 = run(tmp_path, out)
    assert r2.returncode == 0, r2.stderr
    assert candidate(tmp_path) == out


def test_replaces_marked_block_in_middle(tmp_path):
    live = OTHER + "\n# >>> kiosk (managed by deploy.sh)\nalt {\n}\n# <<< kiosk\n\n" + OTHER.replace("chat", "mail")
    r = run(tmp_path, live)
    assert r.returncode == 0, r.stderr
    out = candidate(tmp_path)
    assert "alt {" not in out
    assert out.startswith(OTHER) and out.endswith(OTHER.replace("chat", "mail"))
    assert out.count("# >>> kiosk") == 1


@pytest.mark.parametrize("live", [
    OTHER + OLD_UNMARKED + "\n" + OTHER.replace("chat", "mail"),       # alter Block nicht am Ende
    OTHER + "# >>> kiosk (managed by deploy.sh)\n",                     # Endmarker fehlt
    OTHER + OLD_UNMARKED + OLD_UNMARKED,                                # doppelt
])
def test_refuses_ambiguous_files(tmp_path, live):
    r = run(tmp_path, live)
    assert r.returncode != 0
    assert not (tmp_path / "candidate").exists()


# ---- Prüfung über Caddy: ohne Zugangsdaten -------------------------------------

def _probe_snippet() -> str:
    script = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    part = script.split('echo "== Prüfung: Login über Caddy"')[1]
    m = re.search(r"<<'REMOTE'\n(.*?)\nREMOTE\n", part, re.S)
    assert m, "Prüfung über Caddy in deploy.sh nicht gefunden"
    return m.group(1)


def _run_probe(tmp_path, session_body='{"authenticated":false,"user":null}', session_code="200",
               login_code="401"):
    """Führt das Prüfstück mit einer curl-Attrappe aus, die Argumente und stdin mitschreibt."""
    import os
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{tmp_path}/calls"
case "$*" in
  *api/session*) printf '%s\\n%s' '{session_body}' '{session_code}' ;;
  *api/login*) cat > "{tmp_path}/login_body"; printf '%s' '{login_code}' ;;
esac
""", encoding="utf-8")
    (bin_dir / "curl").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("KIOSK_")}
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return subprocess.run(["bash", "-s", "--", HOST], input=_probe_snippet(), env=env, text=True,
                          capture_output=True)


def test_probe_needs_no_credentials(tmp_path):
    import json
    r = _run_probe(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "HTTP 401 wie erwartet" in r.stdout
    calls = (tmp_path / "calls").read_text()
    assert f"https://{HOST}/api/session" in calls and f"Origin: https://{HOST}" in calls
    body = json.loads((tmp_path / "login_body").read_text())
    # Zufälliger, gültiger, aber nicht existierender Name: belastet die Bremse keines echten Kontos.
    assert re.fullmatch(r"deploy-probe-[0-9a-f]{12}", body["username"])
    assert body["password"] and body["username"] != "admin"
    second = tmp_path / "zwei"
    second.mkdir()
    _run_probe(second)
    assert json.loads((second / "login_body").read_text())["username"] != body["username"]


@pytest.mark.parametrize("kwargs", [
    {"session_code": "502"},
    {"session_body": '{"authenticated":true,"user":{}}'},
    {"login_code": "204"},
    {"login_code": "403"},
    {"login_code": "429"},
])
def test_probe_fails_on_unexpected_answers(tmp_path, kwargs):
    r = _run_probe(tmp_path, **kwargs)
    assert r.returncode != 0 and "FEHLER" in r.stderr


def test_probe_does_not_read_env_credentials():
    snippet = _probe_snippet()
    assert "KIOSK_PASSWORD" not in snippet and "KIOSK_ADMIN_USER" not in snippet and ".env" not in snippet
    script = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    # KIOSK_PASSWORD kommt nur noch beim ersten Anlegen der .env vor.
    assert script.count("KIOSK_PASSWORD") == 1 and "PYLOGIN" not in script


def test_deploy_does_not_print_password():
    script = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    assert "Passwort steht in $DEST/.env" in script
    assert not re.search(r"echo[^\n]*\$pw", script)


# ---- backup.sh ------------------------------------------------------------------

def _run_backup(tmp_path, keep=7, *args, predeploy_keep=3, lock_wait=300):
    import os
    env = {**os.environ, "KIOSK_DB": str(tmp_path / "data" / "kiosk.db"),
           "KIOSK_BACKUP_DIR": str(tmp_path / "backups"), "KIOSK_BACKUP_KEEP": str(keep),
           "KIOSK_PREDEPLOY_KEEP": str(predeploy_keep), "KIOSK_BACKUP_LOCK_WAIT": str(lock_wait)}
    return subprocess.run(["bash", str(ROOT / "deploy" / "backup.sh"), *args], env=env, text=True,
                          capture_output=True)


def _make_db(tmp_path):
    import sqlite3
    data = tmp_path / "data"
    data.mkdir()
    conn = sqlite3.connect(data / "kiosk.db")
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (7)")
    conn.commit()
    conn.close()


def _restore(path) -> "sqlite3.Connection":
    """Entpackt eine .db.gz-Sicherung wie in der Anleitung (gunzip -c) und öffnet sie."""
    import gzip
    import sqlite3
    out = path.parent.parent / (path.name + ".restored")
    out.write_bytes(gzip.decompress(path.read_bytes()))
    return sqlite3.connect(out)


def test_predeploy_backups_rotate_separately(tmp_path):
    _make_db(tmp_path)
    backups = tmp_path / "backups"
    backups.mkdir()
    daily = [backups / f"kiosk-2020010{i}T000000Z.db.gz" for i in range(1, 6)]
    old_pre = [backups / f"kiosk-predeploy-2020010{i}T000000Z.db" for i in range(1, 3)]   # unkomprimiert, alt
    old_pre += [backups / f"kiosk-predeploy-2020010{i}T000000Z.db.gz" for i in range(3, 5)]
    for f in daily + old_pre:
        f.write_bytes(b"alt")

    r = _run_backup(tmp_path, 7, "predeploy")
    assert r.returncode == 0, r.stderr
    pre = sorted(backups.glob("kiosk-predeploy-*"))
    assert len(pre) == 3 and pre[:2] == old_pre[-2:]  # neueste 3 inkl. der neuen
    assert pre[-1].name.endswith(".db.gz")
    assert _restore(pre[-1]).execute("SELECT x FROM t").fetchall() == [(7,)]
    assert all(f.exists() for f in daily)  # tägliche unberührt
    assert r.stdout.startswith(f"Backup: {pre[-1]} ")  # deploy.sh liest den Namen daraus

    # Ein täglicher Lauf mit kleiner Rotation löscht keine Pre-Deploy-Backups.
    r = _run_backup(tmp_path, 2)
    assert r.returncode == 0, r.stderr
    assert sorted(backups.glob("kiosk-predeploy-*")) == pre
    remaining = sorted(p for p in backups.glob("kiosk-*.db.gz") if "predeploy" not in p.name)
    assert len(remaining) == 2 and remaining[0] == daily[-1]


def test_backup_reports_article_count(tmp_path):
    import sqlite3
    data = tmp_path / "data"
    data.mkdir()
    conn = sqlite3.connect(data / "kiosk.db")
    conn.execute("CREATE TABLE articles (id INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO articles DEFAULT VALUES", [()] * 5)
    conn.commit()
    conn.close()
    r = _run_backup(tmp_path, 7, "predeploy")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[1] == "Artikel: 5"


def test_backup_rejects_unknown_mode(tmp_path):
    _make_db(tmp_path)
    r = _run_backup(tmp_path, 7, "wöchentlich")
    assert r.returncode != 0 and "Unbekannter Modus" in r.stderr


def test_backup_creates_consistent_private_copies_and_rotates(tmp_path):
    import sqlite3
    import stat
    import time

    data = tmp_path / "data"
    data.mkdir()
    (data / "secret.key").write_text("s3cret")
    conn = sqlite3.connect(data / "kiosk.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (42)")
    conn.commit()  # Verbindung bleibt offen: Daten stehen noch in der WAL-Datei

    backups = tmp_path / "backups"
    backups.mkdir()
    for i in range(8):  # ältere Sicherungen, davon sollen nur die neuesten übrig bleiben
        (backups / f"kiosk-2020010{i % 10}T000000Z.db.gz").write_bytes(b"alt")
    # Reste eines abgebrochenen Laufs (älter als eine Stunde) und eine frische .part-Datei
    import os
    old_parts = [backups / "kiosk-20200101T000000Z.db.part", backups / "kiosk-20200101T000000Z.db.gz.part"]
    for part in old_parts:
        part.write_bytes(b"halb")
        os.utime(part, (time.time() - 7200, time.time() - 7200))
    fresh = backups / "kiosk-20200102T000000Z.db.gz.part"
    fresh.write_bytes(b"laeuft")
    r = _run_backup(tmp_path, keep=3)
    assert r.returncode == 0, r.stderr
    conn.close()

    files = sorted(backups.glob("kiosk-*.db.gz"))
    assert len(files) == 3
    newest = files[-1]
    assert newest.name.startswith(f"kiosk-{time.strftime('%Y', time.gmtime())}")
    assert stat.S_IMODE(newest.stat().st_mode) == 0o600
    assert stat.S_IMODE(backups.stat().st_mode) == 0o700
    assert _restore(newest).execute("SELECT x FROM t").fetchall() == [(42,)]
    assert (backups / "secret.key").read_text() == "s3cret"
    assert stat.S_IMODE((backups / "secret.key").stat().st_mode) == 0o600
    assert list(backups.glob("*.part")) == [fresh]  # nur die frische bleibt
    assert not list(backups.glob("*.db"))  # keine unkomprimierte Kopie übrig


def test_backup_without_database_fails_clearly(tmp_path):
    r = _run_backup(tmp_path)
    assert r.returncode != 0 and "Keine Datenbank" in r.stderr
    assert "FEHLER: Kiosk-Backup (daily) fehlgeschlagen" in r.stderr


def test_backup_of_broken_database_fails_and_leaves_nothing(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "kiosk.db").write_bytes(b"das ist keine sqlite-datei" * 100)
    r = _run_backup(tmp_path, 7, "predeploy")
    assert r.returncode != 0
    assert "FEHLER: Kiosk-Backup (predeploy) fehlgeschlagen" in r.stderr
    assert not [p for p in (tmp_path / "backups").iterdir() if p.name not in ("secret.key", ".backup.lock")]


def test_backup_lock_prevents_parallel_runs(tmp_path):
    import fcntl
    _make_db(tmp_path)
    backups = tmp_path / "backups"
    backups.mkdir()
    with open(backups / ".backup.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # ein anderer Lauf hält das Lock
        r = _run_backup(tmp_path)
        assert r.returncode == 0, r.stderr
        assert "übersprungen" in r.stdout
        r = _run_backup(tmp_path, 7, "predeploy", lock_wait=1)
        assert r.returncode != 0 and "Wartezeit abgelaufen" in r.stderr
        assert not list(backups.glob("kiosk-*"))
    r = _run_backup(tmp_path)  # Lock frei: läuft normal
    assert r.returncode == 0 and r.stdout.startswith("Backup: "), r.stderr


def test_cron_logs_backup_failures():
    script = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    assert '|| logger -t kiosk-backup -p user.err \\"backup failed\\"; } 2>&1 | logger -t kiosk-backup' in script


# ---- deploy.sh: Zurückrollen ------------------------------------------------------

def _tag_snippet() -> str:
    script = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    m = re.search(r"<<'TAG'\n(.*?)\nTAG\n", script, re.S)
    assert m, "Markier-Stück in deploy.sh nicht gefunden"
    return m.group(1)


FAKE_DOCKER = r"""#!/usr/bin/env bash
# Minimaler docker-Ersatz: Tags als Dateien in $FAKE_DOCKER_STATE.
st=$FAKE_DOCKER_STATE
echo "$*" >> "$st/log"
case "$1" in
  inspect) cat "$st/running" ;;
  image) f="$st/tag_${5//:/_}"; [ -f "$f" ] && cat "$f" || { echo "No such image" >&2; exit 1; } ;;
  tag) echo "$2" > "$st/tag_${3//:/_}" ;;
  *) exit 99 ;;
esac
"""


def _run_tagging(tmp_path, running: str, good: str | None):
    import os
    import stat
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "running").write_text(running + "\n")
    if good is not None:
        (state / "tag_kiosk_good").write_text(good + "\n")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_DOCKER_STATE": str(state)}
    r = subprocess.run(["bash", "-s"], input=_tag_snippet(), env=env, text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
    tags = {p.name[len("tag_"):]: p.read_text().strip() for p in state.glob("tag_*")}
    return tags


def test_tagging_moves_good_to_previous(tmp_path):
    assert _run_tagging(tmp_path, "sha256:neu", "sha256:alt") == {
        "kiosk_latest": "sha256:neu", "kiosk_good": "sha256:neu", "kiosk_previous": "sha256:alt"}


def test_tagging_first_deploy_without_good(tmp_path):
    assert _run_tagging(tmp_path, "sha256:neu", None) == {"kiosk_latest": "sha256:neu", "kiosk_good": "sha256:neu"}


def test_tagging_unchanged_image_keeps_previous(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "tag_kiosk_previous").write_text("sha256:uralt\n")
    assert _run_tagging(tmp_path, "sha256:gleich", "sha256:gleich") == {
        "kiosk_latest": "sha256:gleich", "kiosk_good": "sha256:gleich", "kiosk_previous": "sha256:uralt"}


def test_deploy_order_and_rollback_guards():
    script = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    trap = script.index("trap rollback_hint ERR")
    assert script.count("trap rollback_hint ERR") == 1
    # Bauen und Pre-Deploy-Backup ändern nichts am laufenden Container: eigener Hinweis, kein Rückweg.
    build = script.index("KIOSK_IMAGE=kiosk:candidate docker compose build")
    backup = script.index("backup.sh predeploy")
    up = script.index("KIOSK_IMAGE=kiosk:candidate docker compose up -d --no-build")
    assert build < backup < trap < up
    assert "Alter Container läuft weiter, nichts zu tun" in script[:trap]
    assert script[:trap].count("old_keeps_running") == 3  # Definition + Bauen + Backup
    assert "docker compose build" not in script.replace("KIOSK_IMAGE=kiosk:candidate docker compose build", "")
    assert script.index("== Caddy") < script.index("trap - ERR")
    # Markieren erst nach dem Login-Test
    assert script.index("Prüfung: Login über Caddy") < script.index("trap - ERR") < script.index("<<'TAG'")
    assert "docker tag kiosk:latest kiosk:previous" not in script
    assert 'docker tag "$prev_image"' not in script and "docker tag $prev_image" not in script
    assert "backup.sh predeploy" in script and "--build-arg KIOSK_BUILD=" in script
    assert "umask 077" in script.split("== .env")[1].split("printf")[0]
    assert 'grep 8100" || true' in script
    assert "--exclude .pytest_cache/" in script
    assert 'HEALTH_WAIT="${KIOSK_HEALTH_WAIT:-300}"' in script


def test_compose_image_tag_and_init():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "image: ${KIOSK_IMAGE:-kiosk:latest}" in compose
    assert "init: true" in compose


def test_dockerfile_healthcheck_start_period():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "--start-period=180s" in dockerfile
