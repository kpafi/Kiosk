from __future__ import annotations

from dataclasses import replace

import app.auth as auth_mod
from app.auth import MAX_ATTEMPTS, PLAIN_COOKIE_NAME, SECURE_COOKIE_NAME, Auth, password_fingerprint


def test_token_roundtrip(settings):
    a = Auth(settings)
    assert a.verify_token(a.issue_token())
    assert not a.verify_token(None)
    assert not a.verify_token("quatsch")
    assert not a.verify_token(a.issue_token() + "x")


def test_token_bound_to_password(settings):
    token = Auth(settings).issue_token()
    assert Auth(replace(settings, password="geheim")).verify_token(token)
    assert not Auth(replace(settings, password="neues-passwort")).verify_token(token)


def test_token_bound_to_secret(settings):
    token = Auth(settings).issue_token()
    assert not Auth(replace(settings, secret="y" * 48)).verify_token(token)


def test_cookie_names(settings):
    assert Auth(settings).cookie_name == SECURE_COOKIE_NAME
    assert Auth(replace(settings, cookie_secure=False)).cookie_name == PLAIN_COOKIE_NAME


def test_token_read_only_from_cookie_of_current_mode(settings):
    a = Auth(settings)
    t = a.issue_token()
    assert a.token_from_cookies({SECURE_COOKIE_NAME: t}) == t
    assert a.token_from_cookies({PLAIN_COOKIE_NAME: t}) is None
    assert a.token_from_cookies({SECURE_COOKIE_NAME: "kaputt", PLAIN_COOKIE_NAME: t}) is None
    assert a.token_from_cookies({}) is None
    plain = Auth(replace(settings, cookie_secure=False))
    t = plain.issue_token()
    assert plain.token_from_cookies({PLAIN_COOKIE_NAME: t}) == t
    assert plain.token_from_cookies({SECURE_COOKIE_NAME: t}) is None


def test_token_bound_to_epoch(settings):
    a = Auth(settings, epoch=1)
    t = a.issue_token()
    a.epoch = 2
    assert not a.verify_token(t)
    assert a.verify_token(a.issue_token())
    assert not Auth(settings, epoch=1).verify_token(a.issue_token())


def test_fingerprint_is_keyed(settings):
    import hashlib
    fp = password_fingerprint(settings.secret, "geheim")
    assert len(fp) == 16
    assert fp != password_fingerprint("anderes-secret", "geheim")
    assert fp not in (hashlib.sha256(b"geheim").hexdigest()[:16], hashlib.sha256(b"kiosk-pw:geheim").hexdigest()[:16])


def test_rate_limit_and_clear(settings):
    a = Auth(settings)
    for _ in range(MAX_ATTEMPTS - 1):
        a.note_failure("1.2.3.4")
    assert not a.rate_limited("1.2.3.4")
    a.note_failure("1.2.3.4")
    assert a.rate_limited("1.2.3.4")
    assert not a.rate_limited("5.6.7.8")
    a.clear("1.2.3.4")
    assert not a.rate_limited("1.2.3.4")


def test_rate_limiter_prunes_old_entries(settings, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(auth_mod.time, "monotonic", lambda: clock[0])
    a = Auth(settings)
    for _ in range(MAX_ATTEMPTS):
        a.note_failure("1.2.3.4")
    a.note_failure("9.9.9.9")
    assert a.rate_limited("1.2.3.4")
    clock[0] += auth_mod.WINDOW_SECONDS + 1
    assert not a.rate_limited("1.2.3.4")
    assert len(a._attempts) == 0


def test_rate_limiter_caps_size(settings, monkeypatch):
    monkeypatch.setattr(auth_mod, "MAX_TRACKED_IPS", 10)
    a = Auth(settings)
    for i in range(25):
        a.note_failure(f"10.0.0.{i}")
    assert len(a._attempts) == 10
    assert "10.0.0.0" not in a._attempts and "10.0.0.24" in a._attempts


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
