#!/usr/bin/env bash
# Konten der Kiosk-Instanz vom Laptop aus verwalten (per SSH im laufenden Container):
#   bash deploy/user.sh list
#   bash deploy/user.sh add anna [--admin] [--name "Anna"]
#   bash deploy/user.sh passwd anna
#   bash deploy/user.sh disable anna | enable anna
#   bash deploy/user.sh remove anna
#   bash deploy/user.sh admin anna --on|--off
# Passwörter werden hier lokal verdeckt abgefragt (getpass) und über stdin an
# `python -m app.users ... --password-stdin` durchgereicht: Sie stehen nie in einer
# Kommandozeile, Prozessliste oder Shell-History.
set -euo pipefail
BOX="${BOX:-kiosk-server}"
DEST=/opt/kiosk

usage() {
  sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

[ $# -ge 1 ] || usage
cmd=$1
case "$cmd" in
  list|add|passwd|disable|enable|remove|admin) ;;
  -h|--help|help) usage ;;
  *) echo "Unbekannter Befehl: $cmd" >&2; usage ;;
esac

args=("$@")
password=""
# Benutzername schon hier prüfen (wie im Container: 2 bis 32 Zeichen aus a-z, 0-9, Punkt,
# Unterstrich, Bindestrich; Groß-/Kleinschreibung egal), bevor etwas per SSH läuft.
if [ "$cmd" != list ]; then
  [ $# -ge 2 ] || usage
  # Zeichen ausgeschrieben: [a-z] träfe je nach Locale auch Umlaute.
  if ! [[ "${2,,}" =~ ^[abcdefghijklmnopqrstuvwxyz0123456789._-]{2,32}$ ]]; then
    echo "Ungültiger Benutzername: $2 (2 bis 32 Zeichen aus a-z, 0-9, Punkt, Unterstrich, Bindestrich)" >&2
    exit 2
  fi
fi
case "$cmd" in
  add|passwd)
    # Passwort zweimal verdeckt abfragen, Regeln wie im Container (12 Zeichen ohne Rand-Leerzeichen,
    # nicht der Benutzername).
    password=$(python3 -c '
import getpass, sys
first = getpass.getpass("Neues Passwort: ")
if len(first.strip()) < 12:
    sys.exit("Das Passwort muss mindestens 12 Zeichen lang sein (ohne Leerzeichen am Anfang und Ende)")
if first.strip().lower() == sys.argv[1].lower():
    sys.exit("Das Passwort darf nicht der Benutzername sein")
if getpass.getpass("Passwort wiederholen: ") != first:
    sys.exit("Die Passwörter stimmen nicht überein")
if "\n" in first or "\r" in first:
    sys.exit("Das Passwort darf keinen Zeilenumbruch enthalten")
sys.stdout.write(first)
' "$2")
    args+=(--password-stdin)
    ;;
  remove)
    # Ohne Terminal im Container: hier nachfragen, dann mit --yes weiter.
    yes=0
    for a in "${args[@]}"; do [ "$a" = "--yes" ] && yes=1; done
    if [ "$yes" = 0 ]; then
      read -r -p "Benutzer $2 mit Lesestand, Profilen, Favoriten und Einstellungen löschen? [j/N] " answer
      case "$answer" in
        j|J|ja|Ja|y|Y|yes) args+=(--yes) ;;
        *) echo "Abgebrochen" >&2; exit 1 ;;
      esac
    fi
    ;;
esac

# Argumente quoten (Anzeigenamen mit Leerzeichen, Sonderzeichen). printf %q erzeugt Bash-Quoting,
# deshalb läuft der Befehl auf der Box ausdrücklich per bash -c; für die Login-Shell der Box (evtl.
# kein Bash) wird er als Ganzes in einfache Anführungszeichen gesetzt, was jede POSIX-Shell versteht.
inner="cd $DEST && docker compose exec -T kiosk python -m app.users $(printf '%q ' "${args[@]}")"
remote="bash -c '${inner//\'/\'\\\'\'}'"
if [ -n "$password" ]; then
  # printf ist ein Builtin: Das Passwort erscheint in keiner Prozessliste.
  printf '%s\n' "$password" | ssh "$BOX" "$remote"
else
  ssh "$BOX" "$remote" </dev/null
fi
