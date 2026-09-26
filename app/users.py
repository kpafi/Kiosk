"""Konten verwalten (Kommandozeile), im Container: python -m app.users <befehl> ...

    list
    add <name> [--admin] [--name "Anzeigename"] [--password-stdin]
                                          ohne aktiven Admin wird der neue Benutzer Admin
    passwd <name> [--password-stdin]      beendet alle Sitzungen des Benutzers
    disable <name> | enable <name>        disable beendet ebenfalls alle Sitzungen
    remove <name> [--yes]                 löscht Lesestand, Profile, Favoriten, Einstellungen
    admin <name> --on|--off

Ohne --password-stdin wird das Passwort zweimal verdeckt abgefragt (getpass). Mit
--password-stdin kommt es aus der ersten Zeile der Standardeingabe (deploy/user.sh reicht es so
durch, damit es nie in einer Kommandozeile steht). Der letzte aktive Admin lässt sich weder
löschen noch sperren noch herabstufen. Kein Sammler, das Werkzeug beendet sich sofort.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys
import unicodedata
from pathlib import Path

from .auth import hash_password, normalize_username
from .config import EXAMPLE_PASSWORD, MIN_PASSWORD_LEN, admin_username, bootstrap_password
from .db import Database
from .sources import Catalog

ROOT = Path(__file__).resolve().parent.parent


class UserError(Exception):
    """Fehler mit einer Meldung für die Kommandozeile (Exit-Code 1)."""


def seed_builtin_profiles(db: Database, catalog: Catalog, user_id: int) -> bool:
    """Die vier vorgegebenen Profile eines Benutzers anlegen, falls er noch keine hat."""
    enabled = set(db.enabled_source_ids())
    # Deutschland meint „alle deutschen Quellen“: ohne in_all: false (wie ALLE, api_ui.implicit_sources).
    de = [s.id for s in catalog.all() if s.country == "DE" and s.in_all]
    business = [s.id for s in catalog.all() if s.category == "business"]
    # Keine Wirtschaftsquelle aktiv: alle Quellen, die Begriffe filtern.
    return db.seed_profiles(user_id, de, business if enabled & set(business) else None)


def _open_db(create: bool = False) -> Database:
    """Dieselbe Datenbank wie die App (KIOSK_DATA_DIR, im Container /data/kiosk.db). Ist sie noch
    nicht auf Schema 6, migriert sie wie beim Start der App (Admin aus KIOSK_PASSWORD). Mit
    `create` (Befehl add) wird eine fehlende Datenbank angelegt: So lässt sich der erste Admin
    auch ohne KIOSK_PASSWORD anlegen, bevor die App je gestartet ist."""
    data_dir = Path(os.environ.get("KIOSK_DATA_DIR", ROOT / "data")).resolve()
    path = data_dir / "kiosk.db"
    if not path.exists() and not create:
        raise UserError(f"Keine Datenbank unter {path}; zuerst die App starten")
    # Dieselben Regeln wie beim Start der App (RuntimeError), geprüft bevor etwas angelegt wird.
    password = bootstrap_password()
    admin = (admin_username(), password) if password else None
    data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(path, admin=admin)
    if db.admin_created:
        print(f"Admin '{admin[0]}' aus KIOSK_ADMIN_USER/KIOSK_PASSWORD angelegt")
    return db


def _catalog() -> Catalog:
    return Catalog.load(Path(os.environ.get("KIOSK_SOURCES", ROOT / "sources.yaml")))


def _check_password(password: str, username: str) -> str:
    # Leerzeichen am Rand zählen nicht mit; der Benutzername selbst ist kein Passwort.
    if len(password.strip()) < MIN_PASSWORD_LEN:
        raise UserError(f"Das Passwort muss mindestens {MIN_PASSWORD_LEN} Zeichen lang sein "
                        "(ohne Leerzeichen am Anfang und Ende)")
    if password == EXAMPLE_PASSWORD:
        raise UserError("Das Beispielpasswort aus .env.example ist nicht erlaubt")
    if password.strip().lower() == username.lower():
        raise UserError("Das Passwort darf nicht der Benutzername sein")
    return password


def _check_display_name(raw: str | None, username: str) -> str:
    """Anzeigename in NFC, höchstens 64 Zeichen, nur druckbare Zeichen; leer = Benutzername."""
    display = unicodedata.normalize("NFC", (raw or "").strip()) or username
    if len(display) > 64:
        raise UserError("Anzeigename: höchstens 64 Zeichen")
    if not display.isprintable():
        raise UserError("Anzeigename: keine Steuerzeichen oder Zeilenumbrüche")
    return display


def _read_password(from_stdin: bool, username: str) -> str:
    if from_stdin:
        line = sys.stdin.readline()
        return _check_password(line.rstrip("\r\n"), username)
    first = getpass.getpass("Neues Passwort: ")
    _check_password(first, username)
    if getpass.getpass("Passwort wiederholen: ") != first:
        raise UserError("Die Passwörter stimmen nicht überein")
    return first


def _user(db: Database, raw: str) -> dict:
    name = normalize_username(raw)
    user = db.get_user_by_name(name) if name else None
    if user is None:
        raise UserError(f"Unbekannter Benutzer: {raw}")
    return user


def _is_last_admin(db: Database, user: dict) -> bool:
    """True, wenn `user` der einzige aktive (nicht gesperrte) Admin ist."""
    return bool(user["is_admin"]) and not user["disabled"] and db.active_admin_count() <= 1


def cmd_list(db: Database, args: argparse.Namespace) -> None:
    users = db.list_users()
    if not users:
        print("Keine Benutzer")
        return
    print(f"{'ID':>4}  {'Benutzer':<32}  {'Anzeigename':<24}  Rolle  Status    Letzte Anmeldung")
    for u in users:
        print(f"{u['id']:>4}  {u['username']:<32}  {u['display_name'][:24]:<24}  "
              f"{'Admin' if u['is_admin'] else '-':<5}  {'gesperrt' if u['disabled'] else 'aktiv':<8}  "
              f"{u['last_login_at'] or '-'}")


def cmd_add(db: Database, args: argparse.Namespace) -> None:
    name = normalize_username(args.username)
    if name is None:
        raise UserError("Benutzername: 2 bis 32 Zeichen aus a-z, 0-9, Punkt, Unterstrich, Bindestrich")
    if db.get_user_by_name(name):
        raise UserError(f"Benutzer {name} gibt es schon")
    display = _check_display_name(args.display_name, name)
    catalog = _catalog()  # vor dem Anlegen: ein kaputter Katalog soll kein Konto ohne Profile hinterlassen
    is_admin = args.admin
    if not is_admin and db.active_admin_count() == 0:
        # Eine Instanz ohne aktiven Admin könnte niemand mehr verwalten.
        is_admin = True
        print(f"Es gibt noch keinen aktiven Admin: {name} wird Admin")
    password = _read_password(args.password_stdin, name)
    try:
        user_id = db.create_user(name, hash_password(password), display_name=display, is_admin=is_admin)
    except sqlite3.IntegrityError:
        raise UserError(f"Benutzer {name} gibt es schon") from None
    # Frische Datenbank (App noch nie gestartet): erst die Quellen wie beim Start der App eintragen,
    # sonst fehlen die Wirtschaftsquellen und "Wirtschaft" filtert über alle Quellen.
    db.ensure_sources([(s.id, s.default_enabled) for s in catalog.all()])
    seed_builtin_profiles(db, catalog, user_id)
    print(f"Benutzer {name} angelegt{' (Admin)' if is_admin else ''}")


def cmd_passwd(db: Database, args: argparse.Namespace) -> None:
    user = _user(db, args.username)
    password = _read_password(args.password_stdin, user["username"])
    db.update_user(user["id"], password_hash=hash_password(password), end_sessions=True)
    print(f"Passwort von {user['username']} geändert, alle Sitzungen beendet")


def cmd_disable(db: Database, args: argparse.Namespace) -> None:
    user = _user(db, args.username)
    if _is_last_admin(db, user):
        raise UserError("Der letzte Admin kann nicht gesperrt werden")
    db.update_user(user["id"], disabled=1, end_sessions=True)
    print(f"Benutzer {user['username']} gesperrt, alle Sitzungen beendet")


def cmd_enable(db: Database, args: argparse.Namespace) -> None:
    user = _user(db, args.username)
    db.update_user(user["id"], disabled=0)
    print(f"Benutzer {user['username']} entsperrt")


def cmd_remove(db: Database, args: argparse.Namespace) -> None:
    user = _user(db, args.username)
    if _is_last_admin(db, user):
        raise UserError("Der letzte Admin kann nicht gelöscht werden")
    if not args.yes:
        if not sys.stdin.isatty():
            raise UserError("Löschen ohne Terminal nur mit --yes")
        answer = input(f"Benutzer {user['username']} mit Lesestand, Profilen, Favoriten und "
                       "Einstellungen löschen? [j/N] ")
        if answer.strip().lower() not in ("j", "ja", "y", "yes"):
            raise UserError("Abgebrochen")
    db.delete_user(user["id"])
    print(f"Benutzer {user['username']} gelöscht")


def cmd_admin(db: Database, args: argparse.Namespace) -> None:
    user = _user(db, args.username)
    if not args.on and _is_last_admin(db, user):
        raise UserError("Der letzte Admin kann nicht herabgestuft werden")
    db.update_user(user["id"], is_admin=1 if args.on else 0)
    print(f"{user['username']} ist {'jetzt' if args.on else 'nicht mehr'} Admin")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.users", description="Konten der Kiosk-Instanz verwalten")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="Benutzer auflisten").set_defaults(func=cmd_list)

    add = sub.add_parser("add", help="Benutzer anlegen")
    add.add_argument("username")
    add.add_argument("--admin", action="store_true",
                     help="als Admin anlegen (automatisch, solange es keinen aktiven Admin gibt)")
    add.add_argument("--name", dest="display_name", help="Anzeigename (Standard: Benutzername)")
    add.add_argument("--password-stdin", action="store_true", help="Passwort aus der Standardeingabe")
    add.set_defaults(func=cmd_add)

    pw = sub.add_parser("passwd", help="Passwort setzen, beendet alle Sitzungen")
    pw.add_argument("username")
    pw.add_argument("--password-stdin", action="store_true", help="Passwort aus der Standardeingabe")
    pw.set_defaults(func=cmd_passwd)

    for name, func, text in (("disable", cmd_disable, "Benutzer sperren"),
                             ("enable", cmd_enable, "Benutzer entsperren")):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("username")
        cmd.set_defaults(func=func)

    rm = sub.add_parser("remove", help="Benutzer mit Lesestand, Profilen, Favoriten löschen")
    rm.add_argument("username")
    rm.add_argument("--yes", action="store_true", help="ohne Rückfrage")
    rm.set_defaults(func=cmd_remove)

    adm = sub.add_parser("admin", help="Admin-Recht setzen oder entziehen")
    adm.add_argument("username")
    group = adm.add_mutually_exclusive_group(required=True)
    group.add_argument("--on", dest="on", action="store_true")
    group.add_argument("--off", dest="on", action="store_false")
    adm.set_defaults(func=cmd_admin)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        db = _open_db(create=args.command == "add")
    except (UserError, RuntimeError) as e:
        print(f"Fehler: {e}", file=sys.stderr)
        return 1
    try:
        args.func(db, args)
    except UserError as e:
        print(f"Fehler: {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nAbgebrochen", file=sys.stderr)
        return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    os.umask(0o077)  # wie die App: angelegte Datenbankdateien nur für den eigenen Benutzer
    sys.exit(main())
