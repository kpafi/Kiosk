Kiosk ist ein privater Zeitungskiosk auf einem schwarzen Display: eine ruhige, chronologische Liste aus über neunzig Zeitungen, gebaut mit der Ruhe eines Nothing-Geräts, aber mit eigenem Blau statt Rot. Schwarz, Weiß, Grau, ein einziges Blau als Signal, Punktmatrix-Schrift für das, was Kiosk selbst sagt, und klare Groteske für das, was die Zeitungen sagen. Nichts ist dekoriert, alles ist Werkzeug.

## Grundsätze

- **Dunkel ist die Wahrheit.** Das dunkle Theme ist der Normalfall und wird zuerst entworfen: echtes Schwarz (`bg`), Weiß (`ink`), zwei Graustufen (`raised`, `raised-2`). Das helle Theme ist eine Einstellung, keine zweite Marke, und muss jeden Screen genauso tragen.
- **Ein Blau, ein Zweck.** `signal` markiert nur, was Aufmerksamkeit verlangt: ungelesen, Zähler, Fehler, Aufnahme. Klickbares ist nicht blau, sondern weiß mit Kontur oder als weiße Fläche mit `on-ink`.
- **Punktmatrix spricht Kiosk, Groteske spricht die Zeitung.** `dot` (Doto) nur für Wortmarke, Abschnittstitel und Zähler. Artikeltitel und Lesetext immer in `sans`. `mono` für technische Zeilen: Quelle, Land, Zeit.
- **Pillen und Kreise.** Chips, Leiste, Schaltflächen, Schalter und Punkte sind rund (`radius-pill`). Karten und Bilder haben `radius-md`. Keine scharfen Ecken außer am Bildschirmrand.
- **Linien statt Schatten.** Trennung durch `hairline`, Bedeutung durch `rule`-Konturen. Keine Schatten, keine Verläufe, keine Transparenz-Effekte.
- **Lesbarkeit zuerst.** Lesetext in `article` innerhalb `measure`. Jedes Textpaar hält 4,5:1 in beiden Themes.

## Inhalt und Ton

- Deutsch, knapp, Du-Form, technisch nüchtern: „Profil anlegen“, „Quellen wählen“, „Alles gelesen“.
- Abschnittstitel und Wortmarke in Versalien (`wordmark`, `section`): KIOSK, HEUTE, GESTERN, PROFILE, FAVORITEN.
- Die Meta-Zeile in `meta`, Versalien, Schrägstrich als Trenner: „SPIEGEL / DE / 12 MIN“. Zeiten kurz: „12 MIN“, „3 STD“, „GESTERN 22:10“, „24. SEP“.
- Artikel bleiben in Originalsprache und werden nicht gekürzt, außer dem Teaser nach zwei Zeilen.
- Keine Emojis, keine Flaggen, keine Logos. Eine Zeitung wird durch ihren Namen und ihren Ländercode erkannt.

## Farbe

- Text in `ink` auf `bg`, `raised` und `raised-2`. Nebeninformationen in `muted`.
- Aktive Chips und die primäre Schaltfläche sind eine `ink`-Fläche mit `on-ink`-Text. Inaktive Chips haben eine `rule`-Kontur und `ink`-Text.
- Der Ungelesen-Punkt (`dot` groß) ist `signal`. Ein Zähler in der Leiste ist `signal-text` oder eine kleine `signal`-Pille mit `on-signal`.
- Der gefüllte Stern ist `star`, der leere Stern eine `rule`-Kontur.
- Fehler stehen in `signal-text` mit einem Wort, nie nur als Farbe.
- Paywall-Artikel erscheinen nicht. Es gibt keine Paywall-Farbe.

## Typografie

- Drei Familien: `dot` (Doto, Punktmatrix), `sans` (Space Grotesk), `mono` (Space Mono). Alle von Google Fonts.
- `wordmark` nur für KIOSK im Kopf. `section` für Tages- und Menütitel. `counter` für Zahlen an Chips und in der Leiste.
- Artikeltitel in `headline`, Lesetext in `article`, Zwischenüberschriften in `article-h`, Teaser in `teaser`.
- Alles Bedienbare in `ui`. Nie Punktmatrix auf Schaltflächen, nie Serifen irgendwo.

## Die Schnellzugriffsleiste

- Eine Pille am unteren Rand (`bar` hoch, `raised`, `radius-pill`, `space-4` vom Rand), die immer sichtbar bleibt.
- Darin runde Chips: ganz links „ALLE“, dann die vom Nutzer gewählten Lieblingszeitungen und Profile in seiner Reihenfolge, ganz rechts ein Kreis mit Plus (`rule`-Kontur) zum Hinzufügen.
- Ein Chip zeigt den Kurznamen der Zeitung (SPIEGEL, GUARDIAN, ZEIT) oder des Profils (DEUTSCHLAND, WIRTSCHAFT) in `ui`. Der aktive Chip ist eine `ink`-Fläche. Ein Zähler ungelesener Artikel steht als `counter` rechts im Chip.
- Zu viele Chips scrollen horizontal innerhalb der Pille, „ALLE“ und Plus bleiben stehen.
- Lange drücken auf einen Chip öffnet das Menü zum Entfernen oder Verschieben.

## Profile und Filter

- Ein Profil ist eine benannte Auswahl von Zeitungen, optional mit Suchbegriffen. Vorgegeben: DEUTSCHLAND, WELTWEIT, WIRTSCHAFT, MILITÄR. Eigene Profile beliebig.
- Das Profil-Menü ist ein Blatt (Sheet) von unten in `raised` mit `radius-md` oben: Liste der Profile, jedes mit Schalter „In der Leiste“ und Pfeil zum Bearbeiten, unten „Profil anlegen“. Steht ein Profil nicht oben bei den Filtern, endet sein Untertitel mit „Nicht bei den Filtern“; umgestellt wird das im Editor.
- Der Profil-Editor: Name in einem Eingabefeld (`raised-2`, `radius-pill`), Suchbegriffe als Chips, dann die Gruppe „Sichtbar“ mit zwei Schaltern: „Oben bei den Filtern“ (Filterchip der Liste, neu an) und „Unten in der Leiste“ (neu aus). Darunter „Zeitungen“ mit dem Schalter „Alle aktiven Zeitungen“, ist er aus, alle Zeitungen nach Land gruppiert mit einem runden Schalter je Zeitung. Ein Land lässt sich mit einem Chip komplett wählen. Gruppentitel im Editor sind Beschriftungen in `caption` (wie „Name“, „Suchbegriffe“), keine `section`-Titel.
- Das gerade gewählte Profil steht immer als aktiver Chip in der Filterzeile, auch wenn es sonst nicht oben erscheint, damit man sieht, was filtert, und es abwählen kann.
- Favoriten (Lieblingszeitungen) sind kein Profil, sondern eine Liste einzelner Zeitungen mit demselben Menü: Schalter „In der Leiste“, Stern zum Entfernen.

## Abstände und Formen

- `space-4` Seitenrand am Handy. Artikel durch `hairline` getrennt, `space-4` Innenabstand oben und unten.
- Vorschaubild quadratisch (`thumb`) mit `radius-sm` rechts vom Text. Fehlt ein Bild, entfällt die Fläche.
- Die Leiste hat `space-3` Innenabstand, Chips sind `tap` hoch.
- Zwischen Tagesgruppen `space-8` und ein `section`-Titel.

## Zustände

- Ungelesen: `signal`-Punkt vor der Meta-Zeile, Titel in `ink`. Gelesen: kein Punkt, Titel in `muted`.
- Fokus: 2px-Ring in `ink`, 2px Abstand, auf jedem Grund sichtbar.
- Laden: eine Zeile in `meta` und `muted` („HOLE NEUE ARTIKEL“) mit drei pulsierenden `signal`-Punkten. Keine Skelett-Kästen.
- Leer: eine Zeile in `section` („NICHTS NEUES“) und ein Satz in `teaser` darunter.
- Schalter: Pille aus `rule`-Kontur mit `ink`-Kreis; an: `ink`-Fläche mit `on-ink`-Kreis. Kein Blau am Schalter.

## Ikonografie

- Strichsymbole, 1,5px, 20px Raster, aus Lucide: `search`, `sliders-horizontal`, `star`, `check-check`, `arrow-left`, `plus`, `chevron-right`, `x`, `external-link`, `circle-alert`, `sun`, `moon`.
- Das Matrix-Muster (Punkte im `dot`-Maß auf `space-3`-Raster) ist das einzige Ornament, sparsam als Hintergrund hinter der Wortmarke oder im Leerzustand.
- Kein Logo, keine Wort-Bild-Marke: KIOSK in `wordmark` ist die Marke.

## Menü, Einstellungen, Gemerktes

- Der Knopf oben rechts (drei Punkte in einer Pille) öffnet ein Blatt von unten mit vier Einträgen: Profile, Lieblingszeitungen, Gemerkt, Einstellungen. Die Startseite bleibt dadurch frei von weiteren Knöpfen.
- Gemerkt hat zwei Reiter als Chips: „Später lesen“ und „Favoriten“. Der Stern in der Leseansicht merkt einen Artikel als Favorit, das Lesezeichen legt ihn auf „Später lesen“.
- Einstellungen: Erscheinungsbild als drei Chips (System, Dunkel, Hell), Abrufintervall und Aufbewahrung als Zeilen mit Wert und Pfeil, Textgröße als Chips, Abmelden als Pille mit `rule`-Kontur am Ende. Jede Zeile ist `tap` hoch und durch `hairline` getrennt.

## Zeitungen in der Suche

- Passt die Suche auf Zeitungen, stehen sie über den Artikeln unter dem `section`-Titel ZEITUNGEN (rechts die Zahl in `caption`), die Artikel folgen unter ARTIKEL.
- Die Zeitungen liegen als ein Block auf `raised` mit `radius-md`, ohne Kontur und Schatten, Zeilen durch `hairline` getrennt. Höchstens drei Zeilen, darunter „N weitere Zeitungen“ in `meta` und `muted` mit Chevron.
- Eine Zeile: links ein Kreis mit `rule`-Kontur und Monogramm in `dot` (LDE, SZ, SP), daneben der Name in `sans` 18px, darunter in `meta` Land und Ungelesen („DE / 12 UNGELESEN HEUTE“, gefüllter `star` davor bei Lieblingszeitungen), rechts ein Pfeil in einem kleinen `rule`-Kreis. Die ganze Zeile führt zur Zeitung.
- Ausgeschaltete Zeitungen stehen gedämpft da: Name und Monogramm in `muted`, der Kreis gestrichelt, die Meta-Zeile „CH / AUSGESCHALTET“. Sie bleiben antippbar.

## Layout

- Eine Spalte bis `measure`, zentriert. Kopf: KIOSK links, Suche und Menü rechts als runde Schaltflächen mit `rule`-Kontur.
- Unter dem Kopf die Filterchips des aktiven Profils, unten die Schnellzugriffsleiste.
- Leseansicht auf derselben Spalte, darüber der Kopf mit Zurück, Stern und Gelesen. Am Ende „Original öffnen“ als Pille mit `rule`-Kontur.
- Einstellungen: Theme (System, Dunkel, Hell), Abrufintervall, Aufbewahrung, Abmelden.
