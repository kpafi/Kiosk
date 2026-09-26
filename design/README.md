# Design-Referenz

Quelle der Wahrheit ist Claude Design:
- Canvas „Kiosk Screens“: https://claude.ai/artifact/Eg7m3dvX8HUphgp1c8ByGg (Reihe „Entwurf 2/2b/2c“ ist der vereinbarte Stand; Reihe „Entwurf 1“ ist verworfen)
- Design System: https://claude.ai/artifact/Xp8jnktSbTpbnbGHH3ro3N

Hier liegen Kopien für Agenten ohne Artefakt-Zugriff:
- `mockups/E2*.dc.html`: die Screens als eigenständige HTML-Dateien (Markup mit Inline-Styles; die `<script src="./support.js">`-Zeile und `<x-dc>`-Hülle stammen vom Canvas-Format und sind beim Bauen zu ignorieren). Die Farben stehen dort als Hexwerte der Dunkel- bzw. Hell-Palette.
- `tokens.json`: Farben (Themes dark/light), Schriften, Größen, Abstände, Radien.
- `BRANDBOOK.md`: die Gestaltungsregeln in Prosa.

Zuordnung Screen → Datei: Liste `E2Liste` (hell: `E2Hell`), Menü `E2Menu`, Leseansicht `E2Lese`, Gemerkt `E2Gemerkt`, Einstellungen `E2Einstellungen`, Zeitungen verwalten `E2Quellen`, Profile `E2Profile`, Profil-Editor `E2ProfilEditor`, Suche `E2Suche`, Zeitungsansicht `E2Zeitung`, Leer/Laden `E2Leer`, Anmelden `E2Login`, Desktop `E2Desktop`.
