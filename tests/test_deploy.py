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
    # (außerdem im Hinweis am Ende, der nur prüft, ob die Zeile noch da ist, und sie nie ausgibt)
    outside = script.replace(_heredoc("PWCHECK"), "")
    assert outside.count("KIOSK_PASSWORD") == 1 and "PYLOGIN" not in script


def test_deploy_does_not_print_password():
    script = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    assert "Passwort steht in $1/.env" in script
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
    assert "git archive --format=tar HEAD" in script
    assert 'HEALTH_WAIT="${KIOSK_HEALTH_WAIT:-300}"' in script


def test_compose_image_tag_and_init():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "image: ${KIOSK_IMAGE:-kiosk:latest}" in compose
    assert "init: true" in compose


def test_dockerfile_healthcheck_start_period():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "--start-period=180s" in dockerfile


# ---- backup.sh: root fasst das Datenverzeichnis des Containers nicht an --------------------

def _backup_snippet() -> str:
    script = (ROOT / "deploy" / "backup.sh").read_text(encoding="utf-8")
    m = re.search(r"<<'PY'\n(.*?)\nPY\n", script, re.S)
    assert m, "Python-Stück in backup.sh nicht gefunden"
    return m.group(1)


def test_backup_root_part_never_opens_sqlite_or_chowns():
    snippet = _backup_snippet()
    dump_start = snippet.index('DUMP = r"""')
    dump_end = snippet.index('"""', dump_start + len('DUMP = r"""'))
    root_part = snippet[:dump_start] + snippet[dump_end + 3:]
    assert "sqlite3" not in root_part and "os.chown" not in snippet and "shutil.chown" not in snippet
    assert "sqlite3.connect" in snippet[dump_start:dump_end]
    assert "O_NOFOLLOW" in root_part and "extra_groups" in root_part and '"-I"' in root_part


def test_backup_as_root_runs_sqlite_as_container_user(tmp_path):
    """Als root startet das Skript den SQLite-Teil mit uid/gid des Containers und ohne Zusatzgruppen
    (hier simuliert: geteuid() == 0, Popen mitgeschnitten, der Wechsel selbst entfällt)."""
    import json
    import os
    _make_db(tmp_path)
    snippet = tmp_path / "backup.py"
    snippet.write_text(_backup_snippet(), encoding="utf-8")
    wrapper = f"""
import json, os, subprocess, sys
calls = []
real = subprocess.Popen
class Spy(real):
    def __init__(self, args, **kw):
        calls.append({{k: kw.pop(k, None) for k in ("user", "group", "extra_groups")}})
        super().__init__(args, **kw)
subprocess.Popen = Spy
os.geteuid = lambda: 0
sys.argv = ["-", {str(tmp_path / "data" / "kiosk.db")!r}, {str(tmp_path / "backups")!r}, "7", "daily"]
try:
    exec(compile(open({str(snippet)!r}).read(), "backup", "exec"), {{"__name__": "__main__"}})
finally:
    print("CALLS=" + json.dumps(calls))
"""
    env = {**os.environ, "KIOSK_BACKUP_UID": "4242"}
    env.pop("KIOSK_BACKUP_GID", None)
    r = subprocess.run([sys.executable, "-c", wrapper], env=env, text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
    calls = json.loads(r.stdout.split("CALLS=")[1])
    assert calls == [{"user": 4242, "group": 4242, "extra_groups": []}]
    assert list((tmp_path / "backups").glob("kiosk-*.db.gz"))
    # Ohne Angabe: Besitzer des Datenverzeichnisses (auf der Box 10100, während des Umzugs noch 1000).
    env.pop("KIOSK_BACKUP_UID")
    r = subprocess.run([sys.executable, "-c", wrapper], env=env, text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
    data = (tmp_path / "data").stat()
    assert json.loads(r.stdout.split("CALLS=")[1]) == [
        {"user": data.st_uid, "group": data.st_gid, "extra_groups": []}]
    # Gehört das Datenverzeichnis root, läuft nichts als root (simuliert: lstat meldet uid 0).
    root_owned = wrapper.replace("os.geteuid = lambda: 0", f"""os.geteuid = lambda: 0
_lstat = os.lstat
def _fake_lstat(p, *a, **kw):
    st = _lstat(p, *a, **kw)
    if str(p) == {str(tmp_path / "data")!r}:
        return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 0, 0, st.st_size,
                               int(st.st_atime), int(st.st_mtime), int(st.st_ctime)))
    return st
os.lstat = _fake_lstat""")
    r = subprocess.run([sys.executable, "-c", root_owned], env=env, text=True, capture_output=True)
    assert r.returncode != 0 and "gehört root" in r.stderr
    assert json.loads(r.stdout.split("CALLS=")[1]) == []


def test_backup_does_not_follow_secret_symlink(tmp_path):
    _make_db(tmp_path)
    outside = tmp_path / "fremd.txt"
    outside.write_text("geheim-von-root")
    (tmp_path / "data" / "secret.key").symlink_to(outside)
    r = _run_backup(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "nicht kopiert" in r.stderr
    assert not (tmp_path / "backups" / "secret.key").exists()


def test_backup_does_not_copy_hardlinked_secret(tmp_path):
    import os
    _make_db(tmp_path)
    outside = tmp_path / "fremd.txt"
    outside.write_text("geheim-von-root")
    os.link(outside, tmp_path / "data" / "secret.key")
    r = _run_backup(tmp_path)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "backups" / "secret.key").exists()


def test_backup_refuses_symlinked_database(tmp_path):
    _make_db(tmp_path)
    data = tmp_path / "data"
    (data / "kiosk.db").rename(tmp_path / "echt.db")
    (data / "kiosk.db").symlink_to(tmp_path / "echt.db")
    r = _run_backup(tmp_path)
    assert r.returncode != 0 and "keine einfache Datei" in r.stderr


def test_backup_work_copy_is_removed(tmp_path):
    import os
    import time
    _make_db(tmp_path)
    data = tmp_path / "data"
    stale = data / ".backup-alt"
    stale.mkdir()
    (stale / "kopie.db").write_bytes(b"halb")
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    fresh = data / ".backup-laeuft"
    fresh.mkdir()
    r = _run_backup(tmp_path)
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in data.iterdir() if p.name.startswith(".backup-")) == [".backup-laeuft"]


# ---- deploy.sh: Besitzer und Rechte ------------------------------------------------------

def test_rsync_sets_root_owner_and_modes():
    script = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    assert "rsync -az --delete --itemize-changes --chown=root:root --chmod=D755,F644" in script
    # Wird auf dem Server nichts direkt ausgeführt, reicht F644: backup.sh installiert install -m 700.
    assert "install -m 700 $DEST/deploy/backup.sh $DEST/bin/backup.sh" in script
    assert script.index("== Besitzer und Rechte (Code)") < script.index("backup.sh predeploy")


def _heredoc(tag: str) -> str:
    script = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    m = re.search(rf"<<'{tag}'[^\n]*\n(.*?)\n{tag}\n", script, re.S)
    assert m, f"Stück {tag} in deploy.sh nicht gefunden"
    return m.group(1)


def _fake_env(tmp_path, *, getent: str | None = None, docker_extra: str = ""):
    """PATH mit chown-, docker-, getent- und setpriv-Attrappen (find ist das echte /usr/bin/find,
    nicht ein Alias der eigenen Shell); Aufrufe landen in tmp_path/log."""
    import os
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "log"
    log.write_text("")
    (bin_dir / "chown").write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "chown $*" >> "{log}"\n')
    (bin_dir / "docker").write_text(
        f'#!/usr/bin/env bash\n{docker_extra}\nprintf "%s\\n" "docker $* (in $PWD)" >> "{log}"\n')
    (bin_dir / "getent").write_text(
        "#!/usr/bin/env bash\n" + (f'echo "{getent}:x:10100:"\n' if getent else "exit 2\n"))
    if not (bin_dir / "find").exists():
        (bin_dir / "find").symlink_to("/usr/bin/find")
    # setpriv: Aufruf mitschreiben, dann den Befehl nach den Optionen ausführen (ohne Wechsel).
    (bin_dir / "setpriv").write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "setpriv $*" >> "{log}"\n'
        'while [ "${1#--}" != "$1" ]; do shift; done\nexec "$@"\n')
    for f in bin_dir.iterdir():
        if not f.is_symlink():
            f.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}, log


def _run_with_fake_tools(tmp_path, snippet: str, dest, cwd=None, *, expect_rc=0, **fake):
    """Führt ein Stück mit den Attrappen aus (_fake_env); gibt die mitgeschriebenen Aufrufe zurück.
    `cwd`: Arbeitsverzeichnis beim Start, wird vorher unzugänglich gemacht (wie /root für uid 10100)."""
    env, log = _fake_env(tmp_path, **fake)
    cmd = ["bash", "-s", "--", str(dest)]
    if cwd is not None:
        cwd.mkdir()
        cmd = ["bash", "-c", 'cd "$1" && chmod 000 . && exec bash -s -- "$2"', "-", str(cwd), str(dest)]
    try:
        r = subprocess.run(cmd, input=snippet, env=env, text=True, capture_output=True)
    finally:
        if cwd is not None:
            cwd.chmod(0o700)
    assert r.returncode == expect_rc, r.stdout + r.stderr
    _run_with_fake_tools.last = r
    return log.read_text().splitlines()


def _make_dest(tmp_path):
    dest = tmp_path / "kiosk"
    for rel in ("app/main.py", "deploy/backup.sh", "bin/backup.sh", "data/kiosk.db", "data/kiosk.db-wal",
                "data/secret.key", "data/sub/x"):
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_text("x")
    (dest / "app" / "main.py").chmod(0o664)
    (dest / "data").chmod(0o775)
    (dest / "data" / "kiosk.db").chmod(0o644)
    return dest


def test_code_permissions_step_leaves_data_alone(tmp_path):
    dest = _make_dest(tmp_path)
    calls = _run_with_fake_tools(tmp_path, _heredoc("PERMS"), dest)
    chowned = [c for c in calls if c.startswith("chown -h root:root")]
    assert chowned and all(f"{dest}/data" not in c for c in chowned)          # data/ bleibt außen vor
    assert f"{dest}/app/main.py" in " ".join(chowned)
    assert not [c for c in calls if "data" in c and not c.startswith("chown -h")]
    assert oct((dest / "app" / "main.py").stat().st_mode & 0o777) == "0o644"   # go-w
    # Der alte Container läuft noch: data/ unverändert
    assert oct((dest / "data").stat().st_mode & 0o777) == "0o775"
    assert oct((dest / "data" / "kiosk.db").stat().st_mode & 0o777) == "0o644"


def test_start_step_moves_data_to_uid_10100_then_starts(tmp_path):
    dest = _make_dest(tmp_path)
    # Startet wie die SSH-Sitzung in einem Verzeichnis, das uid 10100 nicht betreten darf (/root):
    # ohne "cd /" bräche find dort ab und nichts würde gestartet.
    calls = _run_with_fake_tools(tmp_path, _heredoc("START"), dest, cwd=tmp_path / "root-home")
    assert calls[0] == f"chown -R 10100:10100 {dest}/data"
    # Start im selben Aufruf, erst nach dem Umstellen
    assert calls[-1] == f"docker compose up -d --no-build --remove-orphans (in {dest})"
    assert any(c.startswith("setpriv --reuid=10100 --regid=10100 --clear-groups find") for c in calls)
    assert not any("1000:1000" in c or "=1000 " in c for c in calls)
    assert oct((dest / "data").stat().st_mode & 0o777) == "0o700"
    for name in ("kiosk.db", "kiosk.db-wal", "secret.key"):
        assert oct((dest / "data" / name).stat().st_mode & 0o777) == "0o600", name
    assert oct((dest / "data" / "sub" / "x").stat().st_mode & 0o777) != "0o600"   # nur oberste Ebene


def test_data_moves_to_10100_only_right_before_the_new_container():
    """Bauen und Pre-Deploy-Backup laufen noch mit dem alten Container (uid 1000) und dessen data/;
    umgestellt wird erst nach dem Backup, im Bereich mit Rückweg, direkt vor dem Start."""
    script = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    start = script.index("<<'START'")
    assert script.index("backup.sh predeploy") < script.index("trap rollback_hint ERR") < start
    block = _heredoc("START")
    assert block.index("cd /\n") < block.index("setpriv") < block.index("docker compose up")
    assert script.count("docker compose up -d --no-build --remove-orphans") == 1   # nur hier
    # Eigener Hinweis bei Fehlern in diesem Schritt: keine Datenbank zurückspielen.
    after = script[start:]
    assert after.index("start_hint") < after.index("== Warten auf /healthz")
    hint = script.split("start_hint() {")[1].split("MSG\n}")[0]
    assert "Keine Datenbank-Wiederherstellung nötig" in hint and "docker compose up -d --no-build" in hint
    rollback = script.split("rollback_hint() {")[1].split("MSG\n}")[0]
    assert "chown -R 10100:10100 $DEST/data" in rollback
    # Wiederherstellen schreibt nur als 10100 und legt die Datei neu an (kein Symlink wird verfolgt).
    assert "setpriv --reuid=10100 --regid=10100 --clear-groups" in rollback and "rm -f kiosk.db" in rollback
    assert "> $DEST/data/kiosk.db" not in rollback
    assert "1000:1000" not in script


def test_container_runs_as_dedicated_uid():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "groupadd -g 10100 kiosk && useradd -u 10100 -g kiosk" in dockerfile and "USER kiosk" in dockerfile
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    # Auch ein älteres Image (uid 1000) läuft nach dem Zurückrollen als 10100.
    assert 'user: "10100:10100"' in compose


# ---- START: robust, mit Backup-Lock, ohne fremde uid 10100 -------------------------------

def test_start_block_is_parsed_whole_and_ignores_hangup():
    block = _heredoc("START")
    assert block.startswith("{\n") and block.rstrip().endswith("}")
    assert "trap '' HUP PIPE INT" in block and 'exec 3>&1 >"$log" 2>&1' in block
    assert block.index("flock -w 300 9") < block.index("chown -R 10100") < block.index("docker compose up")
    assert block.index("getent") < block.index("chown -R 10100")


def test_start_holds_backup_lock_until_compose_up(tmp_path):
    dest = _make_dest(tmp_path)
    lock = dest / "backups" / ".backup.lock"
    calls = _run_with_fake_tools(
        tmp_path, _heredoc("START"), dest,
        docker_extra=f'flock -n "{lock}" true && echo "LOCK FREI" >> "{tmp_path}/log" || echo "LOCK GEHALTEN" >> "{tmp_path}/log"')
    assert "LOCK GEHALTEN" in calls and "LOCK FREI" not in calls
    assert "data/ gehört 10100:10100" in _run_with_fake_tools.last.stdout   # Log am Ende an SSH


def test_start_waits_for_running_backup_and_changes_nothing(tmp_path):
    import fcntl
    dest = _make_dest(tmp_path)
    (dest / "backups").mkdir()
    with open(dest / "backups" / ".backup.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        calls = _run_with_fake_tools(tmp_path, _heredoc("START").replace("flock -w 300", "flock -w 1"), dest,
                                     expect_rc=42)
    assert calls == [] and "Lock" in _run_with_fake_tools.last.stdout
    assert oct((dest / "data").stat().st_mode & 0o777) == "0o775"


def test_start_refuses_if_10100_belongs_to_someone_else(tmp_path):
    dest = _make_dest(tmp_path)
    calls = _run_with_fake_tools(tmp_path, _heredoc("START"), dest, expect_rc=42, getent="fremd")
    assert calls == [] and "fremd" in _run_with_fake_tools.last.stdout
    script = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    after = script.split("<<'START' || rc=$?")[1].split("\nSTART\n", 1)[1]
    assert after.index('if [ "$rc" = 42 ]; then\n  old_keeps_running') < after.index("start_hint")


def test_start_survives_dropped_connection(tmp_path):
    """SSH bricht ab (stdout weg, SIGHUP, Strg-C): chown und compose up laufen trotzdem zu Ende."""
    import os
    import signal
    import time
    dest = _make_dest(tmp_path)
    env, log = _fake_env(tmp_path, docker_extra="sleep 1")
    p = subprocess.Popen(["bash", "-s", "--", str(dest)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env, start_new_session=True)
    p.stdin.write(_heredoc("START").encode())
    p.stdin.close()
    p.stdout.close()
    p.stderr.close()
    time.sleep(0.5)
    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGPIPE):
        os.killpg(p.pid, sig)
    assert p.wait(timeout=20) == 0
    calls = log.read_text().splitlines()
    assert calls[0].startswith("chown -R 10100:10100") and calls[-1].startswith("docker compose up")
    assert "data/ gehört 10100:10100" in (dest / "backups" / "deploy-start.log").read_text()
    assert oct((dest / "data" / "kiosk.db").stat().st_mode & 0o777) == "0o600"


# ---- .env: KIOSK_FETCH_DENY_IPS ergänzen --------------------------------------------------

DENY = "203.0.113.10,2001:db8:1234:5678::/64"


def _run_env(tmp_path, dest):
    env, log = _fake_env(tmp_path)
    return subprocess.run(["bash", "-s", "--", str(dest), DENY], input=_heredoc("ENV"), env=env, text=True,
                          capture_output=True)


def test_env_step_creates_env_with_deny_list(tmp_path):
    import stat
    dest = tmp_path / "kiosk"
    dest.mkdir()
    r = _run_env(tmp_path, dest)
    assert r.returncode == 0, r.stderr
    text = (dest / ".env").read_text()
    pw = text.split("KIOSK_PASSWORD=")[1].split("\n")[0]
    assert len(pw) >= 20 and pw not in r.stdout + r.stderr      # Passwort nie ausgegeben
    assert text.endswith(f"KIOSK_FETCH_DENY_IPS={DENY}\n")
    assert stat.S_IMODE((dest / ".env").stat().st_mode) == 0o600
    assert f"chown root:root .env" in (tmp_path / "log").read_text()


def test_env_step_adds_deny_list_once_and_keeps_existing_value(tmp_path):
    import stat
    dest = tmp_path / "kiosk"
    dest.mkdir()
    (dest / ".env").write_text("KIOSK_PASSWORD=geheim-geheim-geheim")   # ohne Zeilenende
    (dest / ".env").chmod(0o644)
    for _ in range(2):   # idempotent
        assert _run_env(tmp_path, dest).returncode == 0
    assert (dest / ".env").read_text() == f"KIOSK_PASSWORD=geheim-geheim-geheim\nKIOSK_FETCH_DENY_IPS={DENY}\n"
    assert stat.S_IMODE((dest / ".env").stat().st_mode) == 0o600
    (dest / ".env").write_text("KIOSK_PASSWORD=x\nKIOSK_FETCH_DENY_IPS=203.0.113.1\n")
    r = _run_env(tmp_path, dest)
    assert r.returncode == 0 and (dest / ".env").read_text() == "KIOSK_PASSWORD=x\nKIOSK_FETCH_DENY_IPS=203.0.113.1\n"
    assert "geheim" not in r.stdout


def test_deny_list_value_defined_once_at_top():
    script = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    assert script.count(DENY) == 1 and script.index(f'FETCH_DENY_IPS="{DENY}"') < script.index("== Code nach")
    assert 'bash -s -- "$DEST" "$FETCH_DENY_IPS" <<\'ENV\'' in script


def test_rsync_uploads_allowlist_from_head(tmp_path):
    """Auf die Box kommt nur die Allowlist aus dem committeten Stand (git archive HEAD); .env* (außer
    .env.example), data/, bin/ und backups/ auf dem Server bleiben unberührt."""
    script = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    paths = re.search(r"DEPLOY_PATHS=\((.*?)\)", script, re.S).group(1).split()
    assert set(paths) == {"Dockerfile", ".dockerignore", "docker-compose.yml", "requirements.txt", "app", "static",
                          "sources.yaml", "deploy", "README.md", ".env.example"}
    for dev in ("tests", "docs", "design", ".claude", "pytest.ini", "requirements-dev.txt", ".env"):
        assert dev not in paths
    rsync = re.search(r"rsync -az .*?\n(?:.*\\\n)*.*\n", script).group(0)
    assert '"$stage/" "$BOX:$DEST/"' in rsync and "./ " not in rsync
    # Filterregeln an einer lokalen Kopie ausprobieren (Ziel = "Server").
    stage, dest = tmp_path / "stage", tmp_path / "dest"
    for rel in ("app/main.py", ".env.example", "Dockerfile"):
        (stage / rel).parent.mkdir(parents=True, exist_ok=True)
        (stage / rel).write_text("neu")
    for rel in (".env", ".env.bak", "data/kiosk.db", "bin/backup.sh", "backups/x.gz", "tests/alt.py"):
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_text("server")
    filters = re.findall(r"--(?:exclude|include) (\S+)", rsync)
    args = ["rsync", "-a", "--delete"]
    for flag, pat in re.findall(r"--(exclude|include) (\S+)", rsync):
        args.append(f"--{flag}={pat.strip(chr(39))}")
    assert filters
    r = subprocess.run([*args, f"{stage}/", f"{dest}/"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    kept = {str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file()}
    assert kept == {"app/main.py", ".env.example", "Dockerfile", ".env", ".env.bak", "data/kiosk.db",
                    "bin/backup.sh", "backups/x.gz"}
    assert (dest / ".env").read_text() == "server"


def test_deploy_refuses_dirty_tree_and_uses_head():
    script = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    code = script.split('echo "== Code nach $BOX:$DEST"')[1].split('echo "== .env"')[0]
    assert 'git status --porcelain --untracked-files=all -- "${DEPLOY_PATHS[@]}"' in code
    assert "exit 2" in code and "KIOSK_ALLOW_DIRTY" in code
    assert code.index("git archive") < code.index("rsync")
    perms = _heredoc("PERMS")
    assert '"$dest/tests"' in perms and '"$dest/docs"' in perms


def test_password_hint_never_prints_the_value():
    hint = _heredoc("PWCHECK")
    assert "grep -q" in hint and "KIOSK_PASSWORD=" in hint
    assert "$pw" not in hint and "source" not in hint and ". $dest/.env" not in hint


def test_backup_gives_up_when_database_stays_locked(tmp_path):
    import os
    import sqlite3
    import time
    _make_db(tmp_path)
    holder = sqlite3.connect(tmp_path / "data" / "kiosk.db", isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")   # wie ein Container, der die Datenbank nicht mehr freigibt
    try:
        env = {**os.environ, "KIOSK_DB": str(tmp_path / "data" / "kiosk.db"),
               "KIOSK_BACKUP_DIR": str(tmp_path / "backups"), "KIOSK_BACKUP_TIMEOUT": "1"}
        t0 = time.monotonic()
        r = subprocess.run(["bash", str(ROOT / "deploy" / "backup.sh"), "predeploy"], env=env, text=True,
                           capture_output=True, timeout=30)
        assert time.monotonic() - t0 < 10
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert r.returncode != 0 and "nach 1 s abgebrochen" in r.stderr
    assert "FEHLER: Kiosk-Backup (predeploy) fehlgeschlagen" in r.stderr
    assert not list((tmp_path / "backups").glob("kiosk-*"))


@pytest.mark.parametrize("env_text, users, warns", [
    ("KIOSK_PASSWORD=geheimes-start-passwort\nKIOSK_FETCH_INTERVAL=900\n", "2", True),
    ("KIOSK_PASSWORD=geheimes-start-passwort\n", "0", False),
    ("KIOSK_FETCH_INTERVAL=900\n", "2", False),
])
def test_password_hint_after_deploy(tmp_path, env_text, users, warns):
    import os
    dest = tmp_path / "kiosk"
    dest.mkdir()
    (dest / ".env").write_text(env_text)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(f"#!/usr/bin/env bash\necho {users}\n")
    (bin_dir / "docker").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    r = subprocess.run(["bash", "-s", "--", str(dest)], input=_heredoc("PWCHECK"), env=env, text=True,
                       capture_output=True)
    assert r.returncode == 0
    assert ("WARNUNG" in r.stderr) is warns
    assert "geheimes-start-passwort" not in r.stdout + r.stderr
    assert (dest / ".env").read_text() == env_text          # nie automatisch geändert


def test_caddy_strips_range_and_server_and_sets_isolation_headers():
    for needed in ("request_header -Range", "request_header -If-Range", "-Server", "Permissions-Policy",
                   "Cross-Origin-Opener-Policy same-origin", "Cross-Origin-Resource-Policy same-origin"):
        assert needed in BLOCK, needed


def test_uvicorn_without_server_header_and_access_log():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "\"--no-server-header\"" in dockerfile and "\"--no-access-log\"" in dockerfile
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "starlette>=0.49.1" in req


def _dirty_check(repo, allow=False):
    import os
    script = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    start = script.index("DEPLOY_PATHS=(")
    snippet = script[start:script.index("stage=$(mktemp -d)", start)] + "\necho SAUBER\n"
    env = {**os.environ, "KIOSK_ALLOW_DIRTY": "1" if allow else ""}
    return subprocess.run(["bash", "-c", snippet], cwd=repo, env=env, text=True, capture_output=True)


def test_deploy_refuses_untracked_files_under_deploy_paths(tmp_path):
    """Ein committetes app.js, das ein nie hinzugefügtes Modul importiert: nicht deployen."""
    repo = tmp_path / "repo"
    (repo / "static" / "js").mkdir(parents=True)
    (repo / "app").mkdir()
    (repo / "static" / "js" / "app.js").write_text("import './neu.js';\n")
    (repo / "app" / "main.py").write_text("x = 1\n")
    (repo / ".gitignore").write_text("*.pyc\n")
    git = ["git", "-c", "user.email=t@example.org", "-c", "user.name=t"]
    subprocess.run(git + ["init", "-q"], cwd=repo, check=True)
    subprocess.run(git + ["add", "-A"], cwd=repo, check=True)
    subprocess.run(git + ["commit", "-qm", "x"], cwd=repo, check=True)
    assert "SAUBER" in _dirty_check(repo).stdout
    (repo / "app" / "cache.pyc").write_text("ignoriert")          # ignorierte Dateien stören nicht
    (repo / "notizen.txt").write_text("außerhalb der Pfade")      # nicht deploybar, stört nicht
    assert "SAUBER" in _dirty_check(repo).stdout
    (repo / "static" / "js" / "neu.js").write_text("export {};\n")
    r = _dirty_check(repo)
    assert r.returncode == 2 and "?? static/js/neu.js" in r.stderr and "KIOSK_ALLOW_DIRTY" in r.stderr
    assert "SAUBER" in _dirty_check(repo, allow=True).stdout


def test_caddy_csp_replaces_the_app_fallback():
    csp = BLOCK.split("header @app {")[1].split("}")[0]
    assert "Content-Security-Policy" in csp and "defer" in csp
    assert "interest-cohort" not in BLOCK


def test_worker_memory_limits_fit_the_container():
    import app.fetcher as f
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    limit_mb = int(re.search(r"mem_limit: (\d+)m", compose).group(1))
    workers = (f.EXTRACT_WORKERS + f.FEED_PARSE_WORKERS) * f.JOB_MEMORY_LIMIT // 2**20
    assert workers + 320 <= limit_mb     # Reserve für die App selbst
