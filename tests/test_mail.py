"""Versand der KI-Ausgabe per Mail: Einstellungen, Aufbau der Mail, SMTP (gefälscht), Zeitplan und Endpunkt."""
from __future__ import annotations

import json
import logging
import smtplib
import time as _time
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from email import message_from_bytes
from email.message import EmailMessage

import httpx
import pytest
from fastapi.testclient import TestClient

import app.editions as ed
import app.mail as mail
from app.config import load_settings, mail_addresses
from app.editions import EditionBusy, EditionWriter, MailError
from app.mail import SendResult
from app.main import app, state
from conftest import SOURCES_YAML, ago, make_row
from test_editions import (BERLIN, CATALOG, KEY, KIOSK_YAML, ORIGIN, Model, add, answer, edition_json,
                           good_answer_for, second_user)
from test_editions import edb  # noqa: F401  (Fixture)

PW = "ganz-geheim-'quote'-123"
TO = ("a@example.org", "b@example.org")


@pytest.fixture
def mail_settings(settings):
    return replace(settings, ai_api_key=KEY, ai_model="test/model", ai_base_url="https://openrouter.example/api/v1",
                   mail_host="mail.example.org", mail_port=465, mail_user="kiosk@example.org", mail_password=PW,
                   mail_from="kiosk@example.org", mail_to=TO, public_url="https://kiosk.example.org")


# ---- Einstellungen ----------------------------------------------------------------------------

def test_mail_addresses():
    assert mail_addresses(" a@x.org, B@y.de ;a@x.org,, ") == ("a@x.org", "B@y.de")
    assert mail_addresses("") == ()
    for bad in ("a@x", "a b@x.org", "Name <a@x.org>", "a@x.org,b"):
        with pytest.raises(RuntimeError, match="Mailadresse"):
            mail_addresses(bad)
    with pytest.raises(RuntimeError, match="höchstens"):
        mail_addresses(",".join(f"u{i}@x.org" for i in range(51)))


def test_app_settings_for_mail(app_env, monkeypatch):
    for name in ("KIOSK_MAIL_HOST", "KIOSK_MAIL_PORT", "KIOSK_MAIL_USER", "KIOSK_MAIL_PASSWORD", "KIOSK_MAIL_FROM",
                 "KIOSK_MAIL_TO", "KIOSK_PUBLIC_URL"):
        monkeypatch.delenv(name, raising=False)
    s = load_settings()
    assert (s.mail_host, s.mail_port, s.mail_to, s.public_url, s.mail_enabled) == ("", 465, (), "", False)
    monkeypatch.setenv("KIOSK_MAIL_HOST", "mail.example.org")
    with pytest.raises(RuntimeError, match="KIOSK_MAIL_USER oder KIOSK_MAIL_PASSWORD"):
        load_settings()
    monkeypatch.setenv("KIOSK_MAIL_USER", "kiosk@example.org")
    monkeypatch.setenv("KIOSK_MAIL_PASSWORD", PW)
    s = load_settings()      # ohne Empfänger: gültig, aber aus
    assert (s.mail_from, s.mail_to, s.mail_enabled) == ("kiosk@example.org", (), False)
    monkeypatch.setenv("KIOSK_MAIL_TO", "a@example.org, b@example.org")
    monkeypatch.setenv("KIOSK_MAIL_PORT", "587")
    monkeypatch.setenv("KIOSK_PUBLIC_URL", "https://kiosk.example.org/")
    s = load_settings()
    assert (s.mail_port, s.mail_to, s.public_url, s.mail_enabled) == (587, TO, "https://kiosk.example.org", True)
    assert PW not in repr(s)
    for name, bad in (("KIOSK_MAIL_PORT", "0"), ("KIOSK_MAIL_HOST", "mail host"), ("KIOSK_MAIL_TO", "kaputt"),
                      ("KIOSK_MAIL_FROM", "Kiosk <k@x.org>"), ("KIOSK_PUBLIC_URL", "http://kiosk.example.org"),
                      ("KIOSK_MAIL_PASSWORD", "zeile\numbruch"), ("KIOSK_MAIL_USER", "a b")):
        with monkeypatch.context() as m:
            m.setenv(name, bad)
            with pytest.raises(RuntimeError) as e:
                load_settings()
            assert PW not in str(e.value)


# ---- Aufbau der Mail --------------------------------------------------------------------------

BODY = ('<p><strong>Der Vorspann.</strong></p><h2>Lage</h2><img src="https://img.example.org/a.jpg" alt="">'
        '<p>Es geschah etwas. <a href="#/a/11">SPIEGEL</a> · <a href="#/a/12">FAZ</a></p>'
        '<p>Zweiter Absatz.</p><h2>Agenda</h2><ul><li>10 Uhr: Kabinett <a href="#/a/13">BBC</a></li>'
        '<li>Später: <b>Gipfel</b></li></ul><h2>Quellen</h2><p>SPIEGEL · FAZ</p>')


def test_rewrite_body_links_images_and_text():
    html, text = mail.rewrite_body(BODY, "https://kiosk.example.org")
    assert '<a href="https://kiosk.example.org/#/a/11">SPIEGEL</a>' in html
    assert 'src="https://img.example.org/a.jpg"' in html and "max-width:100%" in html
    assert text == ("Der Vorspann.\n\nLage\n\nEs geschah etwas. SPIEGEL · FAZ\n\nZweiter Absatz.\n\nAgenda\n\n"
                    "– 10 Uhr: Kabinett BBC\n– Später: Gipfel\n\nQuellen\n\nSPIEGEL · FAZ")
    # ohne öffentliche Adresse: Links werden zu Text, kein href mehr
    html2, text2 = mail.rewrite_body(BODY, "")
    assert "<a " not in html2 and "href" not in html2 and ">SPIEGEL</a>" not in html2 and "SPIEGEL" in html2
    assert text2 == text
    # fremde Links und Attribute verschwinden, Text bleibt escaped
    html3, _ = mail.rewrite_body('<p onclick="x" class="y">a &lt;b&gt; <a href="https://evil.example/x">evil</a>'
                                 '<img src="data:image/png;base64,AAAA"></p>', "https://kiosk.example.org")
    assert html3 == "<p>a &lt;b&gt; evil</p>"
    # fremde Tags fallen weg (Text bleibt), script/style samt Inhalt; <br> bricht den Text um; offenes <a> wird
    # geschlossen; Entities bleiben Entities
    html4, text4 = mail.rewrite_body('<script>alert(1)</script><style>p{}</style><div><span>x<br>y</span></div>'
                                     '<p>&amp; &lt; <a href="#/a/5">offen</p>', "https://kiosk.example.org")
    assert html4 == '<div>x<br>y</div><p>&amp; &lt; <a href="https://kiosk.example.org/#/a/5">offen</p></a>'
    assert "alert" not in html4 and "<span" not in html4 and "<script" not in html4
    assert text4 == "x\ny\n\n& < offen"
    assert mail.rewrite_body("", "") == ("", "")


def test_mail_subject():
    assert ed.mail_subject("ausgabe-2026-10-06-morning", "Haushalt steht") == \
        "Kiosk · Morgenausgabe, 6. Oktober 2026 · Haushalt steht"
    assert ed.mail_subject("ausgabe-2026-10-06-evening", "") == "Kiosk · Abendausgabe, 6. Oktober 2026"
    assert ed.mail_subject("irgendwas", "Titel") == "Kiosk · Titel"
    assert ed.mail_subject("ausgabe-2026-13-99-morning", "Titel") == "Kiosk · Titel"


def test_build_message_parts_and_headers():
    row = {"id": 7, "title": "Haushalt\r\nsteht", "author": "Morgenausgabe · von einer KI", "body_html": BODY,
           "image_url": "https://img.example.org/hero.jpg", "summary": "Der Vorspann."}
    msg = mail.build_message(row, subject="Kiosk · Morgen\nausgabe", sender="kiosk@example.org",
                             recipient="a@example.org", public_url="https://kiosk.example.org")
    assert msg["Subject"] == "Kiosk · Morgen ausgabe" and msg["To"] == "a@example.org"
    assert mail._header("a\u2028b\x85c\r\n  d") == "a b c d"     # Unicode-Zeilenumbrüche: kein ValueError
    assert msg["From"] == "Kiosk <kiosk@example.org>" and msg["Message-ID"].endswith("@example.org>")
    assert msg["Auto-Submitted"] == "auto-generated" and msg["Date"]
    text = msg.get_body(("plain",)).get_content()
    assert text.startswith("Haushalt steht\nMorgenausgabe · von einer KI\n\nIm Kiosk lesen: "
                           "https://kiosk.example.org/#/a/7\n\nDer Vorspann.")
    html = msg.get_body(("html",)).get_content()
    assert "<h1" in html and "Haushalt steht" in html and 'src="https://img.example.org/hero.jpg"' in html
    assert 'href="https://kiosk.example.org/#/a/7"' in html and 'href="https://kiosk.example.org/#/a/11"' in html
    # die Mail lässt sich als Bytes ausgeben und wieder lesen (Umlaute, 8-Bit)
    again = message_from_bytes(bytes(msg))
    assert again["Subject"].encode("utf-8") and again.is_multipart()
    # ohne öffentliche Adresse und Bild: kein Link, kein img
    msg2 = mail.build_message({"id": 7, "title": "T", "body_html": "<p>x</p>"}, subject="S", sender="k@x.org",
                              recipient="a@example.org")
    assert "Im Kiosk lesen" not in msg2.get_body(("plain",)).get_content()
    assert "<img" not in msg2.get_body(("html",)).get_content() and "<p>x</p>" in msg2.get_body(("html",)).get_content()


# ---- SMTP (gefälscht) -------------------------------------------------------------------------

class FakeSMTP:
    """Gefälschtes smtplib.SMTP/SMTP_SSL: protokolliert Aufrufe; `fail` steuert Fehler je Methode."""
    instances: list = []
    fail: dict = {}

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls: list = []
        self.sent: list[EmailMessage] = []
        FakeSMTP.instances.append(self)
        if "connect" in FakeSMTP.fail:
            raise FakeSMTP.fail["connect"]

    def quit(self):
        self.calls.append("quit")
        if "quit" in FakeSMTP.fail:
            raise FakeSMTP.fail["quit"]

    def close(self):
        self.calls.append("close")

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")
        self.context = context

    def login(self, user, password):
        self.calls.append(("login", user, password))
        if "login" in FakeSMTP.fail:
            raise FakeSMTP.fail["login"]

    def send_message(self, msg):
        to = str(msg["To"])
        if to in FakeSMTP.fail.get("refuse", ()):
            raise smtplib.SMTPRecipientsRefused({to: (550, b"no such user")})
        if to in FakeSMTP.fail.get("defer", ()):
            raise smtplib.SMTPRecipientsRefused({to: (450, b"greylisted")})
        if "send" in FakeSMTP.fail and len(self.sent) >= FakeSMTP.fail.get("send_after", 0):
            raise FakeSMTP.fail["send"]
        self.sent.append(msg)


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.fail = {}
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def two_messages():
    row = {"id": 1, "title": "T", "body_html": "<p>x</p>"}
    return [mail.build_message(row, subject="S", sender="kiosk@example.org", recipient=to) for to in TO]


def test_send_over_tls_and_starttls(mail_settings, fake_smtp):
    assert mail.send(mail_settings, two_messages()) == SendResult(delivered=list(TO))
    (c,) = fake_smtp.instances
    assert (c.host, c.port, c.timeout) == ("mail.example.org", 465, mail.SMTP_TIMEOUT) and c.context is not None
    assert c.calls == [("login", "kiosk@example.org", PW), "quit"] and [str(m["To"]) for m in c.sent] == list(TO)
    # 587: erst EHLO, STARTTLS, EHLO, dann Anmeldung
    fake_smtp.instances.clear()
    assert mail.send(replace(mail_settings, mail_port=587), two_messages()).delivered == list(TO)
    (c,) = fake_smtp.instances
    assert c.calls == ["ehlo", "starttls", "ehlo", ("login", "kiosk@example.org", PW), "quit"]
    # Fehler erst beim QUIT nach der letzten Mail: zählt als Erfolg
    fake_smtp.instances.clear()
    fake_smtp.fail = {"quit": smtplib.SMTPResponseException(421, b"bye")}
    assert mail.send(mail_settings, two_messages()).delivered == list(TO)
    assert fake_smtp.instances[0].calls[-2:] == ["quit", "close"]


def test_send_reports_refused_and_hides_password(mail_settings, fake_smtp, caplog):
    caplog.set_level(logging.WARNING)
    fake_smtp.fail = {"refuse": ("a@example.org",), "defer": ("b@example.org",)}
    res = mail.send(mail_settings, two_messages())
    assert res == SendResult(delivered=[], retry=["b@example.org"], failed=["a@example.org"])
    assert "Empfänger 1 von 2 abgelehnt (550)" in caplog.text and "a@example.org" not in caplog.text
    # zitierte Server-Antworten ohne Adressen
    fake_smtp.fail = {"send": smtplib.SMTPResponseException(550, b"5.1.1 <geheim.person@x.org>: User unknown")}
    with pytest.raises(MailError) as e:
        mail.send(mail_settings, two_messages())
    assert "geheim" not in str(e.value) and "<\u2026>: User unknown" in str(e.value)
    assert "geheim" not in caplog.text
    for fail, pattern, permanent in (
            ({"login": smtplib.SMTPAuthenticationError(535, f"bad {PW}".encode())}, "Anmeldung", True),
            ({"connect": OSError(f"no route {PW}")}, "OSError", False),
            ({"send": smtplib.SMTPResponseException(452, b"too many")}, "452", False),
            ({"send": smtplib.SMTPResponseException(554, b"spam")}, "554", True),
            ({"connect": smtplib.SMTPServerDisconnected("weg")}, "SMTPServerDisconnected", False)):
        fake_smtp.fail = fail
        with pytest.raises(MailError) as e:
            mail.send(mail_settings, two_messages())
        assert pattern in str(e.value) and PW not in str(e.value) and e.value.permanent is permanent
        assert e.value.delivered == []


def test_send_failure_midway_keeps_delivered(mail_settings, fake_smtp):
    """Abbruch nach der ersten Mail: Die Ausnahme nennt, wer schon beliefert ist (keine Doppelmail)."""
    for fail in ({"send": smtplib.SMTPServerDisconnected("weg"), "send_after": 1},
                 {"send": smtplib.SMTPResponseException(451, b"rate"), "send_after": 1}):
        fake_smtp.fail = fail
        with pytest.raises(MailError) as e:
            mail.send(mail_settings, two_messages())
        assert e.value.delivered == ["a@example.org"] and not e.value.permanent
    # vorher endgültig abgelehnte Empfänger stehen in der Ausnahme (werden nicht wiederholt)
    fake_smtp.fail = {"refuse": ("a@example.org",), "send": smtplib.SMTPServerDisconnected("weg"), "send_after": 0}
    with pytest.raises(MailError) as e:
        mail.send(mail_settings, two_messages())
    assert e.value.failed == ["a@example.org"] and e.value.delivered == []


# ---- Schreiber: Versand nach dem Speichern ---------------------------------------------------

class Sender:
    """Gefälschtes mail.send: `script` je Aufruf ein SendResult, eine Liste vorübergehend abgelehnter Empfänger
    (der Rest gilt als beliefert) oder eine Ausnahme; ohne Skript beliefert es alle."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[list[EmailMessage]] = []

    def __call__(self, settings, messages):
        self.calls.append(list(messages))
        item = self.script[min(len(self.calls), len(self.script)) - 1] if self.script else []
        if isinstance(item, Exception):
            raise item
        if isinstance(item, SendResult):
            return item
        tos = [str(m["To"]) for m in messages]
        return SendResult(delivered=[t for t in tos if t not in item], retry=[t for t in tos if t in item])


async def wait_tasks(w: EditionWriter) -> None:
    while w._tasks:
        await asyncio.gather(*list(w._tasks), return_exceptions=True)


async def make_writer(db, settings, model, sender) -> EditionWriter:
    w = EditionWriter(db, CATALOG, settings, retry_pauses=(0, 0), transport=httpx.MockTransport(model), sender=sender)
    await w.start()
    return w


async def test_mail_edition_sends_one_mail_per_recipient(edb, mail_settings, caplog):
    caplog.set_level(logging.INFO)
    add(edb, "alpha", 1, ago(minutes=30), title="Ereignis A")
    sender = Sender()
    w = await make_writer(edb, mail_settings, Model(answer(good_answer_for(edb))), sender)
    try:
        res = await w.run("morning")
        assert sender.calls == []                        # run allein verschickt nichts
        out = await w.mail_edition(res["id"])
    finally:
        await w.close()
    assert out == {"sent": 2, "unreached": 0} and len(sender.calls) == 1
    msgs = sender.calls[0]
    assert [str(m["To"]) for m in msgs] == list(TO) and all(m["From"] == "Kiosk <kiosk@example.org>" for m in msgs)
    today = datetime.now(timezone.utc).astimezone(BERLIN).date()
    assert msgs[0]["Subject"] == f"Kiosk · Morgenausgabe, {ed.german_date(today)} · Haushalt steht, Bahn streikt"
    html = msgs[0].get_body(("html",)).get_content()
    assert f'href="https://kiosk.example.org/#/a/{res["id"]}"' in html and "Es geschah etwas." in html
    st = w.state["mail"]
    assert st["enabled"] and st["recipients"] == 2 and st["last_sent"] and st["last_error"] is None
    assert not st["sending"] and "a@example.org" not in json.dumps(w.state) and PW not in json.dumps(w.state)
    assert "an 2 Empfänger verschickt" in caplog.text and "a@example.org" not in caplog.text
    # keine Ausgabe: ValueError; Versand aus: MailError ohne Aufruf
    other = add(edb, "beta", 2, ago(minutes=5))
    with pytest.raises(ValueError):
        await w.mail_edition(other)
    w.settings = replace(mail_settings, mail_to=())
    with pytest.raises(MailError, match="Versand aus"):
        await w.mail_edition(res["id"])
    assert len(sender.calls) == 1


async def test_mail_edition_retries_only_unreached_recipients(edb, mail_settings, caplog):
    caplog.set_level(logging.INFO)
    add(edb, "alpha", 1, ago(minutes=30))
    sender = Sender(MailError("Verbindung weg"), ["b@example.org"], [])
    w = await make_writer(edb, mail_settings, Model(answer(good_answer_for(edb))), sender)
    pauses = []

    async def no_wait(seconds):
        pauses.append(seconds)

    w.sleep = no_wait
    try:
        aid = (await w.run("morning"))["id"]
        out = await w.mail_edition(aid)
        assert out == {"sent": 2, "unreached": 0} and pauses == [60, 300]
        assert [[str(m["To"]) for m in call] for call in sender.calls] == [list(TO), list(TO), ["b@example.org"]]
        assert w.state["mail"]["last_error"] is None and "Verbindung weg" in caplog.text
        # Abbruch mitten in der Liste: Wer schon beliefert ist, bekommt keine zweite Mail
        pauses.clear()
        w.send = sender2 = Sender(MailError("weg", delivered=["a@example.org"]), [])
        assert await w.mail_edition(aid) == {"sent": 2, "unreached": 0} and pauses == [60]
        assert [[str(m["To"]) for m in call] for call in sender2.calls] == [list(TO), ["b@example.org"]]
        # endgültig abgelehnt vor dem Abbruch: nicht wiederholt, nur der Rest
        pauses.clear()
        w.send = sender5 = Sender(MailError("weg", failed=["a@example.org"]), [])
        assert await w.mail_edition(aid) == {"sent": 1, "unreached": 1} and pauses == [60]
        assert [[str(m["To"]) for m in call] for call in sender5.calls] == [list(TO), ["b@example.org"]]
        # Anmeldung abgelehnt: keine Wiederholung (fail2ban), MailError nach außen
        pauses.clear()
        w.send = sender3 = Sender(MailError("Anmeldung abgelehnt (535)", permanent=True))
        with pytest.raises(MailError, match="535"):
            await w.mail_edition(aid)
        assert len(sender3.calls) == 1 and pauses == [] and "zwecklos" in caplog.text
        assert w.state["mail"]["last_error"] == (f"Ausgabe {aid}: 2 von 2 Empfängern nicht erreicht: "
                                                  "Anmeldung abgelehnt (535)")
        # vom Server endgültig abgelehnt (5xx): nicht wiederholt, aber kein Fehler nach außen, wenn jemand beliefert ist
        w.send = sender4 = Sender(SendResult(delivered=["a@example.org"], failed=["b@example.org"]))
        assert await w.mail_edition(aid) == {"sent": 1, "unreached": 1} and len(sender4.calls) == 1
        assert w.state["mail"]["last_error"] == (f"Ausgabe {aid}: 1 von 2 Empfängern nicht erreicht: 1 endgültig "
                                                  "abgelehnt; 1 von 2 Empfängern abgelehnt")
        # endgültig abgelehnt, der Rest im zweiten Versuch beliefert: Grund bleibt benannt
        w.send = Sender(SendResult(delivered=[], failed=["a@example.org"], retry=["b@example.org"]), [])
        assert await w.mail_edition(aid) == {"sent": 1, "unreached": 1}
        assert w.state["mail"]["last_error"].endswith("nicht erreicht: 1 endgültig abgelehnt; "
                                                       "2 von 2 Empfängern abgelehnt")
    finally:
        await w.close()


async def test_mail_edition_gives_up_after_three_tries(edb, mail_settings):
    add(edb, "alpha", 1, ago(minutes=30))
    sender = Sender(MailError("Server lehnt ab (452)"))
    w = await make_writer(edb, mail_settings, Model(answer(good_answer_for(edb))), sender)

    async def no_wait(seconds):
        pass

    w.sleep = no_wait
    try:
        aid = (await w.run("morning"))["id"]
        with pytest.raises(MailError, match="452"):
            await w.mail_edition(aid)
        # teilweise erreicht: kein Fehler nach außen, aber im Zustand
        sender2 = Sender(["b@example.org"])
        w.send = sender2
        assert await w.mail_edition(aid) == {"sent": 1, "unreached": 1}
    finally:
        await w.close()
    assert len(sender.calls) == 3 and len(sender2.calls) == 3
    assert w.state["mail"]["last_error"] == (f"Ausgabe {aid}: 1 von 2 Empfängern nicht erreicht: "
                                              "1 von 1 Empfängern abgelehnt")
    assert w.state["mail"]["last_sent"] and not w.mail_busy


async def test_schedule_mails_new_editions_but_not_existing(edb, mail_settings):
    """Der Zeitplan verschickt jede Ausgabe, die er schreibt; schon vorhandene nicht noch einmal."""
    add(edb, "alpha", 1, "2026-10-06T03:30:00+00:00")          # 5:30 Uhr Berlin, vor der festen Uhr unten
    sender = Sender()
    w = await make_writer(edb, mail_settings, Model(answer(good_answer_for(edb))), sender)
    w.clock = lambda: datetime(2026, 10, 6, 6, 5, tzinfo=BERLIN).astimezone(timezone.utc)
    try:
        await w._schedule(ed.parse_times(mail_settings))
        assert sender.calls == [] and w.mail_busy                 # Versand läuft im Hintergrund
        await wait_tasks(w)
        assert len(sender.calls) == 1 and [str(m["To"]) for m in sender.calls[0]] == list(TO) and not w.mail_busy
        await w._schedule(ed.parse_times(mail_settings))     # schon da: kein Lauf, keine Mail
        await wait_tasks(w)
        assert len(sender.calls) == 1
        # Versand aus: der Zeitplan schreibt trotzdem (Abend)
        w.settings = replace(mail_settings, mail_to=())
        add(edb, "alpha", 2, "2026-10-06T15:00:00+00:00")
        w.clock = lambda: datetime(2026, 10, 6, 20, 5, tzinfo=BERLIN).astimezone(timezone.utc)
        await w._schedule(ed.parse_times(mail_settings))
        await wait_tasks(w)
        assert len(sender.calls) == 1 and edb.edition_exists("ausgabe-2026-10-06-evening")
    finally:
        await w.close()


async def test_schedule_survives_mail_failure(edb, mail_settings, caplog):
    """Scheitert der Versand ganz, bleibt die Ausgabe gespeichert und der Zeitplan läuft weiter."""
    caplog.set_level(logging.INFO)
    add(edb, "alpha", 1, "2026-10-06T03:30:00+00:00")
    w = await make_writer(edb, mail_settings, Model(answer(good_answer_for(edb))), Sender(RuntimeError("Bug")))
    w.clock = lambda: datetime(2026, 10, 6, 6, 5, tzinfo=BERLIN).astimezone(timezone.utc)
    try:
        await w._schedule(ed.parse_times(mail_settings))
        await wait_tasks(w)
    finally:
        await w.close()
    assert edb.edition_exists("ausgabe-2026-10-06-morning") and w._attempts["ausgabe-2026-10-06-morning"][1] is None
    assert "Versand der Ausgabe" in caplog.text and "Bug" in caplog.text
    assert w.state["mail"]["last_error"].endswith("Interner Fehler (RuntimeError)") and not w.mail_busy


async def test_launch_mail_refuses_a_second_send(edb, mail_settings):
    """Zwei Anfragen hintereinander (Doppelklick): nur ein Versand, der zweite Aufruf bekommt EditionBusy sofort."""
    add(edb, "alpha", 1, ago(minutes=30))
    sender = Sender()
    w = await make_writer(edb, mail_settings, Model(answer(good_answer_for(edb))), sender)
    try:
        aid = (await w.run("morning"))["id"]
        w.launch_mail(aid)
        with pytest.raises(EditionBusy):
            w.launch_mail(aid)
        assert w.mail_busy and sender.calls == []
        await wait_tasks(w)
        assert len(sender.calls) == 1 and not w.mail_busy
        # Zeitplan und Hand nacheinander: beide kommen dran, nie gleichzeitig
        w._spawn_mail(aid, "a")
        w._spawn_mail(aid, "b")
        assert w.mail_busy
        await wait_tasks(w)
        assert len(sender.calls) == 3 and w.state["mail"]["sending"] is False
    finally:
        await w.close()


# ---- Endpunkte ---------------------------------------------------------------------------------

@pytest.fixture
def mail_client(app_env, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setenv("KIOSK_AI_BASE_URL", "https://openrouter.example/api/v1")
    monkeypatch.setenv("KIOSK_MAIL_HOST", "mail.example.org")
    monkeypatch.setenv("KIOSK_MAIL_USER", "kiosk@example.org")
    monkeypatch.setenv("KIOSK_MAIL_PASSWORD", PW)
    monkeypatch.setenv("KIOSK_MAIL_TO", ", ".join(TO))
    monkeypatch.setenv("KIOSK_PUBLIC_URL", "https://kiosk.example.org")
    (app_env / "sources.yaml").write_text(SOURCES_YAML + KIOSK_YAML, encoding="utf-8")
    with TestClient(app, base_url=ORIGIN) as c:
        assert c.post("/api/login", json={"username": "admin", "password": "geheim-und-lang"}).status_code == 204
        yield c


def wait_mail(c, timeout: float = 5.0) -> dict:
    for _ in range(int(timeout / 0.05)):
        st = c.get("/api/status").json()["editions"]
        if not st["running"] and not st["mail"]["sending"]:
            return st
        _time.sleep(0.05)
    raise AssertionError("Versand hängt")


def test_mail_endpoint_sends_latest_or_given_edition(mail_client):
    c = mail_client
    sender = Sender()
    state.editions.send = sender
    assert c.post("/api/editions/mail", json={}).status_code == 404       # noch keine Ausgabe
    old = state.db.store_edition({"source_id": "kiosk", "guid": "ausgabe-2026-10-05-evening",
                                  "url": "kiosk:ausgabe/2026-10-05/evening", "title": "Alt", "body_html": "<p>a</p>",
                                  "published_at": "2026-10-05T18:00:00+00:00"})
    new = state.db.store_edition({"source_id": "kiosk", "guid": "ausgabe-2026-10-06-morning",
                                  "url": "kiosk:ausgabe/2026-10-06/morning", "title": "Neu", "body_html": "<p>b</p>",
                                  "published_at": "2026-10-06T05:00:00+00:00"})
    r = c.post("/api/editions/mail", json={})
    assert r.status_code == 202 and r.json() == {"started": True, "id": new}
    st = wait_mail(c)
    assert st["mail"] == {"enabled": True, "recipients": 2, "sending": False, "last_sent": st["mail"]["last_sent"],
                          "last_error": None} and st["mail"]["last_sent"]
    assert len(sender.calls) == 1 and sender.calls[0][0]["Subject"] == "Kiosk · Morgenausgabe, 6. Oktober 2026 · Neu"
    r = c.post("/api/editions/mail", json={"id": old})
    assert r.status_code == 202 and r.json()["id"] == old
    wait_mail(c)
    assert len(sender.calls) == 2 and sender.calls[1][0]["Subject"] == "Kiosk · Abendausgabe, 5. Oktober 2026 · Alt"
    # kein Artikel / keine Ausgabe / Unsinn / fremde Benutzer
    state.db.upsert_articles([make_row("alpha", 1, ago(minutes=30))])
    other_article = state.db.one("SELECT id FROM articles WHERE source_id='alpha'")["id"]
    assert c.post("/api/editions/mail", json={"id": other_article}).status_code == 404
    assert c.post("/api/editions/mail", json={"id": 999999}).status_code == 404
    assert c.post("/api/editions/mail", json={"id": 0}).status_code == 422
    assert c.post("/api/editions/mail", json={"id": 2**63}).status_code == 422
    assert c.post("/api/editions/mail", json={"id": "x"}).status_code == 422
    other = second_user()
    assert other.post("/api/editions/mail", json={}).status_code == 403
    assert TestClient(app, base_url=ORIGIN).post("/api/editions/mail", json={}).status_code == 401
    # Nicht-Admins sehen den Versand nicht im Status
    assert set(other.get("/api/status").json()["editions"]) == {"enabled", "running"}


def test_mail_endpoint_without_mail_settings(client_without_mail):
    c = client_without_mail
    r = c.post("/api/editions/mail", json={})
    assert r.status_code == 503 and "KIOSK_MAIL_HOST" in r.json()["detail"]
    r = c.post("/api/editions/run", json={"slot": "morning", "mail": True})
    assert r.status_code == 503
    assert c.get("/api/status").json()["editions"]["mail"]["enabled"] is False


@pytest.fixture
def client_without_mail(app_env, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    for name in ("KIOSK_MAIL_HOST", "KIOSK_MAIL_USER", "KIOSK_MAIL_PASSWORD", "KIOSK_MAIL_TO"):
        monkeypatch.delenv(name, raising=False)
    (app_env / "sources.yaml").write_text(SOURCES_YAML + KIOSK_YAML, encoding="utf-8")
    with TestClient(app, base_url=ORIGIN) as c:
        assert c.post("/api/login", json={"username": "admin", "password": "geheim-und-lang"}).status_code == 204
        yield c


def test_run_endpoint_with_mail_sends_after_creation(mail_client):
    c = mail_client
    sender = Sender()
    state.editions.send = sender
    state.db.upsert_articles([make_row("alpha", 1, ago(minutes=30), title="Ereignis")])
    aid = state.db.one("SELECT id FROM articles")["id"]
    content = json.dumps(edition_json(sections=[{"heading": "Lage", "paragraphs": ["Text."], "refs": [aid]}]))
    state.editions._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: answer(content)))
    assert c.post("/api/editions/run", json={"slot": "morning", "mail": "ja"}).status_code == 422
    assert c.post("/api/editions/run", json={"slot": "morning"}).status_code == 202
    wait_mail(c)
    assert sender.calls == []                                   # ohne mail: kein Versand
    assert c.post("/api/editions/run", json={"slot": "morning", "force": True, "mail": True}).status_code == 202
    st = wait_mail(c)
    assert len(sender.calls) == 1 and st["mail"]["last_sent"] and st["last_error"] is None
    assert "Haushalt steht, Bahn streikt" in sender.calls[0][0]["Subject"]
