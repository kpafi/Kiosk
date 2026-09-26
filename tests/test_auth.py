from __future__ import annotations

from dataclasses import replace

import pytest

import app.auth as auth_mod
from app.auth import (MAX_ATTEMPTS, MAX_USER_ATTEMPTS, PLAIN_COOKIE_NAME, SECURE_COOKIE_NAME, Auth, hash_password,
                      normalize_username, verify_password)


def _user(pw_hash="scrypt$x", **kw):
    return {"id": 7, "username": "anna", "display_name": "Anna", "password_hash": pw_hash,
            "is_admin": 0, "disabled": 0, "session_epoch": 1, **kw}


def _loader(user):
    return lambda uid: user if uid == user["id"] else None


def test_hash_format_and_verify():
    h = hash_password("richtig-langes-passwort")
    scheme, n, r, p, salt, digest = h.split("$")
    assert (scheme, n, r, p) == ("scrypt", "16384", "8", "1")
    import base64
    assert len(base64.b64decode(salt)) == 16 and len(base64.b64decode(digest)) == 32
    assert verify_password("richtig-langes-passwort", h)
    assert not verify_password("falsches-passwort", h)
    assert hash_password("richtig-langes-passwort") != h  # eigenes Salt
    for broken in ("", "scrypt$1$8$1$AA==$AA==", "md5$16384$8$1$AA==$AA==", "scrypt$x$8$1$AA==$AA==",
                   "scrypt$16384$8$1$!!$AA==", "scrypt$16384$8$1$AA==$"):
        assert not verify_password("x", broken), broken


def test_normalize_username():
    assert normalize_username("  Anna.B-1_ ") == "anna.b-1_"
    for bad in ("a", "x" * 33, "anna b", "änna", "", "anna@x"):
        assert normalize_username(bad) is None, bad


def test_token_roundtrip(settings):
    a = Auth(settings)
    user = _user()
    token = a.issue_token(user)
    assert a.parse_token(token) == (7, 1, a.fingerprint("scrypt$x"))
    assert a.user_from_cookies({SECURE_COOKIE_NAME: token}, _loader(user)) is user
    for bad in (None, "", "quatsch", token + "x"):
        assert a.parse_token(bad) is None


def test_token_bound_to_password_hash_epoch_and_state(settings):
    a = Auth(settings)
    token = {SECURE_COOKIE_NAME: a.issue_token(_user())}
    assert a.user_from_cookies(token, _loader(_user()))
    assert a.user_from_cookies(token, _loader(_user(pw_hash="scrypt$neu"))) is None
    assert a.user_from_cookies(token, _loader(_user(session_epoch=2))) is None
    assert a.user_from_cookies(token, _loader(_user(disabled=1))) is None
    assert a.user_from_cookies(token, lambda uid: None) is None


def test_token_bound_to_secret(settings):
    token = Auth(settings).issue_token(_user())
    assert Auth(replace(settings, secret="y" * 48)).parse_token(token) is None


def test_fingerprint_is_keyed(settings):
    import hashlib
    h = hash_password("geheim-und-lang")
    fp = Auth(settings).fingerprint(h)
    assert len(fp) == 16
    assert fp != Auth(replace(settings, secret="z" * 48)).fingerprint(h)
    assert fp != hashlib.sha256(h.encode()).hexdigest()[:16]


def test_cookie_names(settings):
    assert Auth(settings).cookie_name == SECURE_COOKIE_NAME
    assert Auth(replace(settings, cookie_secure=False)).cookie_name == PLAIN_COOKIE_NAME


def test_token_read_only_from_cookie_of_current_mode(settings):
    user = _user()
    a = Auth(settings)
    t = a.issue_token(user)
    assert a.user_from_cookies({SECURE_COOKIE_NAME: t}, _loader(user))
    assert a.user_from_cookies({PLAIN_COOKIE_NAME: t}, _loader(user)) is None
    assert a.user_from_cookies({SECURE_COOKIE_NAME: "kaputt", PLAIN_COOKIE_NAME: t}, _loader(user)) is None
    plain = Auth(replace(settings, cookie_secure=False))
    t = plain.issue_token(user)
    assert plain.user_from_cookies({PLAIN_COOKIE_NAME: t}, _loader(user))
    assert plain.user_from_cookies({SECURE_COOKIE_NAME: t}, _loader(user)) is None


def test_rate_limit_per_ip_and_per_username(settings):
    a = Auth(settings)
    for i in range(MAX_ATTEMPTS - 1):
        a.note_failure("1.2.3.4", f"name{i}")
    assert not a.rate_limited("1.2.3.4", "anna")
    a.note_failure("1.2.3.4", "name-x")
    assert a.rate_limited("1.2.3.4", "anna")          # IP gesperrt
    assert not a.rate_limited("5.6.7.8", "anna")
    a.clear("1.2.3.4", "anna")
    assert not a.rate_limited("1.2.3.4", "anna")
    # Rotierende IPs gegen einen Namen: die Bremse pro Benutzername greift, aber erst nach
    # MAX_USER_ATTEMPTS (höher als je IP, damit ein Name nicht zu leicht zu sperren ist)
    assert (MAX_ATTEMPTS, MAX_USER_ATTEMPTS, auth_mod.GLOBAL_MAX_ATTEMPTS) == (8, 30, 200)
    for i in range(MAX_USER_ATTEMPTS - 1):
        a.note_failure(f"10.0.{i}.1", "anna")
    assert not a.rate_limited("10.9.9.9", "anna")
    a.note_failure("10.0.99.1", "anna")
    assert a.rate_limited("10.9.9.9", "anna") and not a.rate_limited("10.9.9.9", "bert")


def test_reserve_counts_before_verification(settings):
    a = Auth(settings)
    assert all(a.reserve("1.2.3.4", f"n{i}") for i in range(MAX_ATTEMPTS))
    assert not a.reserve("1.2.3.4", "anna")          # gebremst, zählt nicht mit
    assert len(a._by_ip.attempts["1.2.3.4"]) == MAX_ATTEMPTS and "anna" not in a._by_user.attempts
    a.clear("1.2.3.4", "n0")
    assert a.reserve("1.2.3.4", "anna")


def test_successful_login_releases_only_its_own_reservation(settings):
    """7 Fehlversuche + 1 erfolgreicher Login von derselben IP: Nur die Reservierung des Logins
    wird zurückgenommen, die IP hat danach noch genau 8 - 7 = 1 Versuch."""
    a = Auth(settings)
    for i in range(MAX_USER_ATTEMPTS - 1):
        a.note_failure(f"10.0.{i}.1", "anna")          # Fehlversuche gegen den Namen von anderswo
    for i in range(MAX_ATTEMPTS - 1):
        assert a.reserve("1.2.3.4", f"n{i}")
    assert a.reserve("1.2.3.4", "anna")                # der erfolgreiche Login
    a.clear("1.2.3.4", "anna")
    assert len(a._by_ip.attempts["1.2.3.4"]) == MAX_ATTEMPTS - 1
    assert "anna" not in a._by_user.attempts           # Name: Fehlversuche verfallen
    assert a.reserve("1.2.3.4", "x")                   # noch genau ein Versuch
    assert not a.reserve("1.2.3.4", "y")
    # Eine IP ohne weitere Fehlversuche ist nach dem Login wieder leer
    assert a.reserve("5.6.7.8", "bert")
    a.clear("5.6.7.8", "bert")
    assert "5.6.7.8" not in a._by_ip.attempts


def test_global_brake(settings, monkeypatch):
    monkeypatch.setattr(auth_mod, "GLOBAL_MAX_ATTEMPTS", 5)
    a = Auth(settings)
    for i in range(5):
        a.note_failure(f"10.0.0.{i}", f"name{i}")
    assert a.rate_limited("192.0.2.1", "neu")


def test_rate_limiter_prunes_old_entries(settings, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(auth_mod.time, "monotonic", lambda: clock[0])
    a = Auth(settings)
    for _ in range(MAX_ATTEMPTS):
        a.note_failure("1.2.3.4", "anna")
    a.note_failure("9.9.9.9", "bert")
    assert a.rate_limited("1.2.3.4", "x")
    clock[0] += auth_mod.WINDOW_SECONDS + 1
    assert not a.rate_limited("1.2.3.4", "anna")
    assert len(a._by_ip.attempts) == 0 and len(a._by_user.attempts) == 0


def test_rate_limiter_caps_size(settings, monkeypatch):
    monkeypatch.setattr(auth_mod, "MAX_TRACKED_KEYS", 10)
    a = Auth(settings)
    for i in range(25):
        a.note_failure(f"10.0.0.{i}", f"n{i}")
    assert len(a._by_ip.attempts) == 10 and len(a._by_user.attempts) == 10
    assert "10.0.0.0" not in a._by_ip.attempts and "10.0.0.24" in a._by_ip.attempts


def test_secret_file_created_private_and_reused(tmp_path, monkeypatch):
    import os
    import stat

    from app.config import load_settings
    monkeypatch.setenv("KIOSK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KIOSK_PASSWORD", "geheim-und-lang")
    monkeypatch.delenv("KIOSK_SECRET", raising=False)
    old = os.umask(0)  # auch eine offene umask darf keine lesbare Datei erzeugen
    try:
        first = load_settings().secret
    finally:
        os.umask(old)
    path = tmp_path / "secret.key"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(first) > 40 and load_settings().secret == first


def test_docs_flag(monkeypatch):
    from app.config import docs_enabled
    monkeypatch.delenv("KIOSK_DOCS", raising=False)
    assert docs_enabled() is False
    monkeypatch.setenv("KIOSK_DOCS", "1")
    assert docs_enabled() is True


@pytest.mark.parametrize("ip,key", [
    ("203.0.113.7", "203.0.113.7"),
    ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::/64"),
    ("2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::/64"),
    ("::ffff:203.0.113.7", "203.0.113.7"),
    ("testclient", "testclient"),
])
def test_ip_key_groups_ipv6_by_64(ip, key):
    from app.auth import ip_key
    assert ip_key(ip) == key


@pytest.mark.parametrize("secret", ["kurz", "x" * 31])
def test_short_kiosk_secret_is_rejected(tmp_path, monkeypatch, secret):
    from app.config import load_settings
    monkeypatch.setenv("KIOSK_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("KIOSK_PASSWORD", raising=False)
    monkeypatch.setenv("KIOSK_SECRET", secret)
    with pytest.raises(RuntimeError, match="KIOSK_SECRET muss mindestens 32 Zeichen"):
        load_settings()
    monkeypatch.setenv("KIOSK_SECRET", "x" * 32)
    assert load_settings().secret == "x" * 32


@pytest.mark.parametrize("content", ["", "\n", "zu-kurz"])
def test_empty_or_short_secret_file_is_rejected(tmp_path, monkeypatch, content):
    from app.config import load_settings
    monkeypatch.setenv("KIOSK_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("KIOSK_PASSWORD", raising=False)
    monkeypatch.delenv("KIOSK_SECRET", raising=False)
    (tmp_path / "secret.key").write_text(content)
    with pytest.raises(RuntimeError, match="secret.key ist leer oder kürzer als 32"):
        load_settings()


def test_secret_file_is_written_atomically(tmp_path, monkeypatch):
    """Scheitert das Schreiben, bleibt keine (leere) secret.key und keine Hilfsdatei zurück."""
    import os

    from app.config import load_settings
    monkeypatch.setenv("KIOSK_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("KIOSK_PASSWORD", raising=False)
    monkeypatch.delenv("KIOSK_SECRET", raising=False)

    def full_disk(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", full_disk)
    with pytest.raises(OSError):
        load_settings()
    assert list(tmp_path.iterdir()) == []
    monkeypatch.undo()
    monkeypatch.setenv("KIOSK_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("KIOSK_PASSWORD", raising=False)
    monkeypatch.delenv("KIOSK_SECRET", raising=False)
    secret = load_settings().secret
    assert [p.name for p in tmp_path.iterdir()] == ["secret.key"] and len(secret) >= 32


def test_stale_secret_temp_link_is_removed(tmp_path, monkeypatch):
    """Absturz zwischen link() und unlink(): Beim nächsten Start verschwindet die Hilfsdatei, damit
    secret.key wieder nur einen harten Link hat (backup.sh kopiert sonst nicht)."""
    import os

    from app.config import load_settings
    monkeypatch.setenv("KIOSK_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("KIOSK_PASSWORD", raising=False)
    monkeypatch.delenv("KIOSK_SECRET", raising=False)
    (tmp_path / "secret.key").write_text("s" * 64)
    own = tmp_path / f".secret.key.{os.getpid()}.abcd1234"      # eigene PID aus einem früheren Leben
    os.link(tmp_path / "secret.key", own)
    dead = tmp_path / ".secret.key.99999999.deadbeef"            # Prozess gibt es nicht
    dead.write_text("x")
    alive = tmp_path / f".secret.key.{os.getppid()}.cafe0000"     # läuft noch: bleibt
    alive.write_text("x")
    assert load_settings().secret == "s" * 64
    assert not own.exists() and not dead.exists() and alive.exists()
    assert os.stat(tmp_path / "secret.key").st_nlink == 1
