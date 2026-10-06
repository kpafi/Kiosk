"""Versand der KI-Ausgabe per Mail (SMTP mit TLS).

Reine Bausteine: build_message macht aus einer gespeicherten Ausgabe (Zeile aus articles) eine Mail mit Text- und
HTML-Teil, send schickt fertige Mails über den Server aus den Einstellungen. Zeitplan, Wiederholungen und Zustand
hängen in editions.EditionWriter (mail_edition). Das Passwort erscheint nie in Fehlertexten oder im Log."""
from __future__ import annotations

import html
import logging
import re
import smtplib
import ssl
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from html.parser import HTMLParser
from typing import Any

from .config import Settings

log = logging.getLogger("kiosk.mail")

SMTP_TIMEOUT = 30                   # Sekunden je Verbindungsschritt (Aufbau, Anmeldung, Senden)
STARTTLS_PORTS = (587, 25)          # dort STARTTLS; sonst (465) TLS von Anfang an
SENDER_NAME = "Kiosk"
_VOID = {"br", "img", "hr"}
_BLOCK_BREAK = {"p", "h1", "h2", "h3", "h4", "ul", "ol", "blockquote", "figure", "figcaption", "div"}
# Tags, die die Mail übernimmt (alles andere fällt weg, der Text bleibt; script/style samt Inhalt)
_KEEP = _BLOCK_BREAK | {"li", "strong", "em", "b", "i", "br", "hr", "a", "img"}
_DROP_CONTENT = {"script", "style", "template", "noscript", "iframe", "object"}
_ARTICLE_HREF_RE = re.compile(r"#/a/(\d+)")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")


class MailError(Exception):
    """Versand gescheitert (Verbindung, Anmeldung, Server lehnt ab). Text ohne Passwort. `delivered`: Empfänger,
    die vor dem Fehler schon beliefert waren (nicht noch einmal schicken); `permanent`: Wiederholen zwecklos
    (Anmeldung abgelehnt, 5xx), sonst später noch einmal versuchen."""

    def __init__(self, message: str, *, delivered: Sequence[str] = (), failed: Sequence[str] = (),
                 permanent: bool = False):
        super().__init__(message)
        self.delivered = list(delivered)
        self.failed = list(failed)          # vorher im selben Lauf endgültig abgelehnt (nicht wiederholen)
        self.permanent = permanent


@dataclass
class SendResult:
    """Ergebnis von send: wer beliefert ist, wen der Server vorübergehend (4xx, später noch einmal) und wen er
    endgültig (5xx, nicht wiederholen) abgelehnt hat."""
    delivered: list[str] = field(default_factory=list)
    retry: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


_ADDR_RE = re.compile(r"<?[^\s<>@,;:\"]+@[^\s<>@,;:\"]+>?")


def _scrub(text: str, secret: str) -> str:
    """Server-Antworten für Log und Zustand: ohne Passwort und ohne Mailadressen (Postfix zitiert den Empfänger)."""
    text = text.replace(secret, "***") if secret else text
    return _ADDR_RE.sub("<…>", text)


def _header(text: str, limit: int = 300) -> str:
    """Eine Kopfzeile: keine Steuerzeichen und kein Unicode-Zeilenumbruch (U+2028, U+0085: die E-Mail-Bibliothek
    wiese die Kopfzeile sonst zurück), Leerraum auf ein Leerzeichen, begrenzt lang."""
    return " ".join(_CTRL_RE.sub(" ", text).split())[:limit]


def article_url(public_url: str, article_id: int) -> str:
    return f"{public_url}/#/a/{int(article_id)}"


class _Rewriter(HTMLParser):
    """Schreibt das (bereits bereinigte) body_html der Ausgabe für die Mail um und sammelt dabei den reinen Text:
    Links auf Artikel (#/a/<id>) zeigen auf die App (KIOSK_PUBLIC_URL) oder werden ohne Adresse zu Text;
    andere Attribute als href, src und alt fallen weg. Text: Überschriften und Absätze durch Leerzeilen getrennt,
    Listenpunkte mit „–“, Bilder ohne Spur."""

    def __init__(self, public_url: str):
        super().__init__(convert_charrefs=True)
        self.public_url = public_url
        self.out: list[str] = []
        self.text: list[str] = []
        self._open_a = 0                 # offene <a> mit Adresse (am Ende geschlossen)
        self._drop = 0                   # in script/style: Inhalt fällt weg

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_CONTENT:
            self._drop += 1
            return
        if self._drop or tag not in _KEEP:
            return                       # Tag weg, Text bleibt (handle_data)
        a = dict(attrs)
        if tag == "a":
            href = a.get("href") or ""
            m = _ARTICLE_HREF_RE.fullmatch(href)
            target = article_url(self.public_url, int(m.group(1))) if m and self.public_url else ""
            if target:                   # sonst nur der Text
                self._open_a += 1
                self.out.append(f'<a href="{html.escape(target, quote=True)}">')
            return
        if tag == "img":
            src = a.get("src") or ""
            if src.startswith(("http://", "https://")):
                self.out.append(f'<img src="{html.escape(src, quote=True)}" alt="" '
                                'style="max-width:100%;height:auto;display:block;margin:16px 0">')
            return
        if tag in _BLOCK_BREAK:
            self._break()
        if tag == "li":
            self._break(single=True)
            self.text.append("– ")
        if tag == "br":
            self.text.append("\n")
        self.out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if tag in _DROP_CONTENT:
            self._drop = max(0, self._drop - 1)
            return
        if self._drop or tag not in _KEEP or tag in _VOID:
            return
        if tag == "a":
            if self._open_a:
                self._open_a -= 1
                self.out.append("</a>")
            return
        self.out.append(f"</{tag}>")
        if tag in _BLOCK_BREAK or tag == "li":
            self._break(single=tag == "li")

    def handle_data(self, data):
        if self._drop:
            return
        self.out.append(html.escape(data, quote=False))
        self.text.append(data)

    def _break(self, single: bool = False) -> None:
        joined = "".join(self.text)
        if not joined.strip():
            return
        sep = "\n" if single else "\n\n"
        self.text.append(sep if not joined.endswith(sep) else "")

    def result(self) -> tuple[str, str]:
        self.out.extend("</a>" * self._open_a)
        self._open_a = 0
        text = re.sub(r"[ \t\u00a0]+", " ", "".join(self.text))
        text = re.sub(r" *\n *", "\n", text)
        return "".join(self.out), re.sub(r"\n{3,}", "\n\n", text).strip()


def rewrite_body(body_html: str, public_url: str) -> tuple[str, str]:
    """(HTML für die Mail, reiner Text) aus dem body_html einer Ausgabe, siehe _Rewriter."""
    r = _Rewriter(public_url)
    r.feed(body_html or "")
    r.close()
    return r.result()


_STYLE = ("margin:0;padding:24px 16px;background:#ffffff;color:#111111;"
          "font-family:Georgia,'Times New Roman',serif;font-size:17px;line-height:1.5")


def build_message(row: Mapping[str, Any], *, subject: str, sender: str, recipient: str,
                  public_url: str = "") -> EmailMessage:
    """Mail für eine Ausgabe (Zeile aus articles: id, title, summary, author, body_html, image_url) an einen
    Empfänger: Textteil und HTML-Teil, Titelbild oben, Links auf Artikel zur App (public_url) oder als Text.
    Der Betreff kommt vom Aufrufer (kennt Slot und Datum)."""
    msg = EmailMessage()
    msg["Subject"] = _header(subject)
    msg["From"] = formataddr((SENDER_NAME, sender))
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    msg["Auto-Submitted"] = "auto-generated"
    title = _header(str(row.get("title") or "Ausgabe"), 500)
    author = _header(str(row.get("author") or ""), 500)
    body_html, body_text = rewrite_body(str(row.get("body_html") or ""), public_url)
    link = article_url(public_url, int(row["id"])) if public_url and row.get("id") is not None else ""

    text = [title, author, ""]
    if link:
        text += [f"Im Kiosk lesen: {link}", ""]
    text.append(body_text)
    msg.set_content("\n".join(text).strip() + "\n")

    hero = str(row.get("image_url") or "")
    parts = [f"<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>{html.escape(title)}</title></head>",
             f'<body style="{_STYLE}"><div style="max-width:640px;margin:0 auto">',
             f'<p style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#666">{SENDER_NAME}</p>',
             f'<h1 style="font-size:28px;line-height:1.2;margin:0 0 8px">{html.escape(title)}</h1>',
             f'<p style="font-size:14px;color:#666;margin:0 0 16px">{html.escape(author)}</p>']
    if link:
        parts.append(f'<p style="font-size:14px;margin:0 0 16px"><a href="{html.escape(link, quote=True)}" '
                     'style="color:#1d4ed8">Im Kiosk lesen</a></p>')
    if hero.startswith(("http://", "https://")):
        parts.append(f'<img src="{html.escape(hero, quote=True)}" alt="" '
                     'style="max-width:100%;height:auto;display:block;margin:0 0 16px">')
    parts.append(body_html)
    parts.append("</div></body></html>")
    msg.add_alternative("".join(parts), subtype="html")
    return msg


def send(settings: Settings, messages: Sequence[EmailMessage], *, timeout: float = SMTP_TIMEOUT) -> SendResult:
    """Schickt die Mails über den Server aus den Einstellungen (eine Verbindung, TLS, Anmeldung). Ergebnis je
    Empfänger (SendResult: beliefert, vorübergehend abgelehnt, endgültig abgelehnt). MailError, wenn Verbindung,
    Anmeldung oder der Versand als Ganzes scheitern; darin `delivered` (wer vorher schon beliefert war) und
    `permanent` (Anmeldung abgelehnt oder 5xx: nicht wiederholen). Ein Fehler erst beim Verabschieden (QUIT) nach
    der letzten Mail zählt als Erfolg. Im Log stehen keine Adressen: nur die Nummer des Empfängers, und aus
    zitierten Server-Antworten sind Adressen entfernt (_scrub).
    Blockiert: aus der Event-Loop per asyncio.to_thread aufrufen."""
    secret = settings.mail_password
    context = ssl.create_default_context()
    res = SendResult()
    try:
        if settings.mail_port in STARTTLS_PORTS:
            client = smtplib.SMTP(settings.mail_host, settings.mail_port, timeout=timeout)
        else:
            client = smtplib.SMTP_SSL(settings.mail_host, settings.mail_port, timeout=timeout, context=context)
        try:
            if settings.mail_port in STARTTLS_PORTS:
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
            client.login(settings.mail_user, secret)
            for i, msg in enumerate(messages, 1):
                to = str(msg["To"])
                try:
                    client.send_message(msg)
                except smtplib.SMTPRecipientsRefused as e:
                    code, text = next(iter(e.recipients.values()), (0, b""))
                    (res.failed if 500 <= int(code) < 600 else res.retry).append(to)
                    log.warning("Mail: Empfänger %d von %d abgelehnt (%s): %s", i, len(messages), code,
                                _scrub(_bytes(text), secret)[:200])
                except smtplib.SMTPResponseException as e:
                    raise MailError(f"Server lehnt ab ({e.smtp_code}): {_scrub(_bytes(e.smtp_error), secret)[:300]}",
                                    delivered=res.delivered, failed=res.failed,
                                    permanent=500 <= int(e.smtp_code) < 600) from None
                else:
                    res.delivered.append(to)
        finally:
            try:
                client.quit()
            except (smtplib.SMTPException, OSError, ssl.SSLError):
                client.close()              # alles schon raus (oder der Fehler davor zählt): QUIT ist egal
    except MailError:
        raise
    except smtplib.SMTPAuthenticationError as e:
        raise MailError(f"Anmeldung abgelehnt ({e.smtp_code}): {_scrub(_bytes(e.smtp_error), secret)[:200]}",
                        permanent=True) from None
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        raise MailError(f"{type(e).__name__}: {_scrub(str(e), secret)[:300]}", delivered=res.delivered,
                        failed=res.failed) from None
    return res


def _bytes(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
