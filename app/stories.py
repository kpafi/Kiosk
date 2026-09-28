"""Geschichten: erkennt, welche Artikel verschiedener Zeitungen über dasselbe Ereignis berichten.

Reines Python ohne Modelle. Ablauf je Lauf (nach jedem Abruf der Feeds und einmal beim Start):

1. Alle Artikel der letzten WINDOW Stunden lesen (Titel, Anreißer, bei fehlendem oder sehr kurzem
   Anreißer der bereinigte Anfang des Volltexts). Wörtlich gleiche Meldungen verschiedener Quellen
   zählen beim Rechnen als ein Artikel, jede Zeitung zählt aber mit.
2. Text normalisieren, Feed-Floskeln und Stoppwörter weg, leicht stemmen (de/en), deutsche Wörter über
   ein allgemeines Lexikon auf englische Grundformen abbilden (Länder, Städte, Namen samt
   Transliteration, häufige Nachrichtenwörter), lange Komposita in bekannte Teile zerlegen.
3. TF-IDF mit IDF aus dem Fenster (Titel doppelt, Vor- und Nachname als ein Wort). Ähnlichkeit ist
   der Kosinus, das stärkste gemeinsame Wort zählt nur mit TOP_FACTOR (ein Name allein verbindet
   nicht); de/en-Paare vergleichen nur Wörter, die es in beiden Sprachen gibt.
4. Ereignis statt Thema (Model.linkable): Ein Paar braucht besondere gemeinsame Wörter (Namen, Orte,
   Komposita, nicht in einem großen Teil des Fensters; allgemeine Wörter, Zahlen, klein geschriebene
   und häufige deutsche Substantive zählen nicht), bei englischen Artikeln einen erkennbaren Namen
   plus Inhaltswörter, deutsch/englisch zusätzlich gemeinsame Inhaltswörter in Titel und erstem Satz.
   Ereignisarten (Beben, Brand, Streik, Sportart, Medaille; nur Substantive aus Titel und erstem
   Satz) müssen übereinstimmen, zwei Ereignismeldungen zudem einen Ort oder Namen teilen. Teilen
   gleichsprachige Titel nur ein Wort, braucht es mehr Ähnlichkeit; fast gleicher Text mit einem
   gemeinsamen Namen reicht immer. Kommentare, Analysen, Liveblogs, Newsletter (is_soft) nur bei sehr
   hoher Ähnlichkeit. Paare liegen höchstens MAX_GAP auseinander; Kandidaten kommen aus einem
   invertierten Index mit absoluter Obergrenze je Wort.
5. Average-Link-Clustering, stärkste Kanten zuerst: verschmelzen nur, wenn die Gruppe in MAX_GAP passt,
   die mittlere Ähnlichkeit reicht, mindestens die Hälfte der Paare ähnlich ist und die Ereignisarten
   je Mitglied passen; Kommentare tragen keine anderen Artikel. Zweiter Durchgang für gespaltene
   Geschichten über die Schwerpunkte, dritter für Nachzügler, zum Schluss fallen Mitglieder heraus,
   die nur an wenigen anderen hängen. Abgelehnte Verschmelzungen merkt sich der Lauf, bis sich eine
   der Gruppen ändert.
6. Eine Geschichte braucht zwei Zeitungen (Verlage, siehe publisher); story_id siehe assign (bleibt,
   solange die Artikel einer Geschichte zusammenbleiben; nie die ID einer anderen alten Geschichte).
   Je Mitglied die Mittigkeit (centrality) für die Wahl des Vertreters.

„Verwandte Themen“ (topic_ids) nutzen die Vektoren des letzten Laufs mit niedrigerer Schwelle; für
ältere Artikel (oder wenn das nicht reicht) sucht der Volltextindex nach Paaren ihrer
informativsten Wörter. Ein Thema ist ein anderes Ereignis (nie die eigene Geschichte oder Zeitung).
Messung (Tuning-, Held-out- und ein ungesehenes Fenster): tests/test_stories.py.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .db import _iso   # Zeitpunkte im Format von published_at (UTC, ohne Mikrosekunden)

log = logging.getLogger("kiosk.stories")

# ---- Parameter ------------------------------------------------------------------

WINDOW = timedelta(hours=48)        # so weit zurück wird je Lauf gruppiert
MAX_GAP = timedelta(hours=36)       # Artikel einer Geschichte liegen höchstens so weit auseinander
RELATED_DAYS = 7                    # „verwandte Themen“ aus den letzten so vielen Tagen
TITLE_WEIGHT = 2.0                  # Gewicht eines Worts im Titel (Anreißer 1, Volltext-Anfang BODY_WEIGHT)
BODY_WEIGHT = 0.5
SUMMARY_CHARS = 400                 # so viel vom Anreißer zählt
BODY_CHARS = 600                    # so viel vom Volltext zählt (nur bei kurzem Anreißer)
SUMMARY_TRIM = " \t\n\r\xa0"         # Rand eines Anreißers wie db.SUMMARY_TRIM (gleiche Länge in SQL)
BODY_IF_SUMMARY_BELOW = 80          # Volltext-Anfang nur, wenn der Anreißer fehlt oder sehr kurz ist
PART_FACTOR = 0.5                   # Gewicht der Teile eines zerlegten Kompositums
ALIAS_MIN_DF = 3                    # Wörter, die fast immer nebeneinander stehen, zählen als eines ...
NAME_NEIGHBOUR_SHARE = 0.5          # ... neben einem Namen in so einem Anteil seiner Artikel: ein Name
ALIAS_SHARE = 0.8                   # ... wenn sie in so einem Anteil ihrer Artikel so stehen
NAME_CAPS_SHARE = 0.6               # englisches Wort ist ein Name, wenn es mitten im Satz so oft groß steht
XNORM_FLOOR = 0.5                   # de/en-Vergleich: Vektor über gemeinsame Wörter höchstens so stark anheben
# Informativ ist ein Wort, das in höchstens so vielen Artikeln des Fensters steht: Anteil am
# Fenster, aber mindestens INFORMATIVE_MIN_DF (kleine Fenster, Tests).
INFORMATIVE_FRAC = 0.025
INFORMATIVE_MIN_DF = 12
INFORMATIVE_MAX_DF = 100            # absolute Obergrenzen (Kandidatenpaare wachsen mit Postings²)
TOPIC_MAX_DF = 300
MIN_SHARED = 2                      # gemeinsame informative Wörter für eine Kante
EDGE_MIN = 0.30                     # Mindestähnlichkeit einer Kante
AVG_MIN = 0.21                      # mittlere Ähnlichkeit beim Verschmelzen zweier Gruppen
SAME_SOURCE_MIN = 0.45              # Kante zwischen zwei Artikeln derselben Quelle
COHERENCE_FLOOR = 0.2               # beim Verschmelzen: mindestens COHERENCE_SHARE der Paare so ähnlich
COHERENCE_SHARE = 0.5
WEAK_EDGE_MIN = 0.2                 # zweiter Durchgang: Kanten ab hier, wenn die Schwerpunkte ...
CENTROID_MIN = 0.45                 # ... mindestens so ähnlich sind
UMBRELLA_SPORTS = 3                 # Namen bei so vielen Sportarten sind Dach-Namen (siehe Model.umbrella)
PRUNE_MIN_SIZE = 5                  # Gruppen ab dieser Größe: Mitglieder mit zu wenigen ähnlichen ...
PRUNE_SHARE = 0.3                   # ... Paaren (Anteil ab COHERENCE_FLOOR) fallen heraus
LATE_EDGES = 2                      # Nachzügler: so viele Kanten zu einer Gruppe (oder Schwerpunkt nah)
SOFT_SIM_MIN = 0.5                  # Kommentar, Analyse, Liveblog: Kante erst ab dieser Ähnlichkeit
STRONG_SIM = 0.45                   # gleichsprachiges Paar so ähnlich, mit einem besonderen Wort ...
STRONG_PLAIN = 3                    # ... und so vielen gewöhnlichen Wörtern (bis TOPIC_FRAC): verbindbar
SAME_TEXT_MIN = 0.6                 # gleichsprachiges Paar, fast derselbe Text: verbindbar mit jedem Namen
CROSS_SIM_MIN = 0.5                 # de/en-Paar ohne gemeinsame Inhaltswörter nur ab dieser Ähnlichkeit
CONTENT_MIN = 0.09                  # Anteil gemeinsamer Nicht-Namen an der Ähnlichkeit (englische Paare)
TITLE_APART_MIN = 0.36              # gleichsprachige Kante, deren Titel nur ein Wort teilen
SHORT_TOKENS = 8                    # Artikel mit so wenigen Wörtern (nur Titel) ...
SHORT_SIM_MIN = 0.55                # ... verbinden sich über häufige Wörter nur ab dieser Ähnlichkeit
# Das stärkste gemeinsame Wort eines Paars zählt nur mit diesem Faktor: Paare, die nur ein Name
# verbindet (zwei verschiedene Geschichten über Klopp), fallen so unter die Schwellen.
TOP_FACTOR = 0.4
# Verwandte Themen: niedrigere Schwelle, aber echte Überschneidung (nicht nur „Trump“).
TOPIC_MIN = 0.18
TOPIC_DUPLICATE = 0.6               # ähnlicher Artikel derselben Quelle: eine Neufassung, kein anderes Thema
TOPIC_FRAC = 0.08                   # „informativ“ für Themen: höchstens so großer Anteil am Fenster
TOPIC_MIN_SHARED = 2
TOPIC_LIMIT = 5
SAME_EVENT_PLAIN_SIM = 0.40         # ohne Geschichte, ohne gemeinsamen Namen: dasselbe Ereignis ab hier
TOPIC_PER_PAPER = 2                 # verwandte Themen: höchstens so viele aus einer Zeitung
TOPIC_FTS_MIN = 0.25                # ... und aus der Volltextsuche erst ab dieser Ähnlichkeit
TOKEN_CACHE_MAX = 200_000           # Einträge im Wort-Cache eines Tokenizers
KEY_SHARE = 0.4                     # Stichwort einer Geschichte: in so einem Anteil ihrer Artikel
FTS_WORDS = 4                       # verwandte Themen ohne Vektor: so viele Wörter für die Volltextsuche

# ---- Normalisierung ---------------------------------------------------------------

_WORD_RE = re.compile(r"[^\W_]+")
_ORDINAL_RE = re.compile(r"\d+(?:st|nd|rd|th)")


def normalize(text: str) -> str:
    """Klein, Unicode NFKD ohne Akzente/Umlautpunkte (ä -> a, ß -> ss)."""
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


# Floskeln aus Feeds, die nichts über den Inhalt sagen (vor dem Tokenisieren entfernt).
_BOILERPLATE = [re.compile(p, re.S) for p in (
    r"The post .{0,400}? appeared first on .*$",
    r"La entrada .{0,400}? se publicó primero en .*$",
    r"Continue reading\.*\s*$",
    r"Leia mais.*$",
    r"\[\s*mehr\s*\]",
    r"\bmehr\s*(?:\.\.\.|…)\s*$",
    r"\(ANSA\)\s*-\s*[A-ZÀ-Ü' .,/-]+,?\s*\d{1,2}\s+[A-Z]{3}\s*-",      # (ANSA) - ROME, 27 SET -
    r"^[A-ZÀ-Ü' .,/-]+,\s*[A-Z][a-z]{2,4}\.?\s*\d{1,2}\s*\((?:Yonhap|Kyodo|AFP|AP|Reuters)\)\s*-+",
    r"^[A-ZÀ-Ü' .,/-]{3,}\s*\((?:Yonhap|Kyodo|AFP|AP|Reuters|dpa)\)\s*-+",   # TOKYO (Kyodo) --
    r"^[A-Z]{3,}(?: [A-Z]{3,})?\s*[—–]+",                                    # TEHRAN—
    r"^\((?:LEAD|\d+(?:st|nd|rd|th) LD|URGENT|News Focus|Asiad|Yonhap Interview)[^)]*\)",
    r"\+\+\+",
)]
# Abkürzungen in Großbuchstaben, die klein geschrieben mit Wörtern kollidieren („US“/„us“).
_ACRONYMS = {
    "US": "usa", "USA": "usa", "UK": "britain", "EU": "eu", "UN": "un", "UNO": "un", "KI": "ai",
    "AI": "ai", "IS": "isis", "WHO": "who_org", "NATO": "nato", "EZB": "ecb", "ECB": "ecb",
    "IWF": "imf", "IMF": "imf", "IAEA": "iaea", "IAEO": "iaea", "G7": "g7", "G20": "g20",
    "RBA": "rba", "RBI": "rbi", "RBNZ": "rbnz", "BoE": "boe", "BOE": "boe", "BoJ": "boj", "BOJ": "boj",
}
_DOTTED = [(re.compile(r"\bU\.S\.(?:A\.)?"), "USA"), (re.compile(r"\bU\.K\."), "UK"),
           (re.compile(r"\bU\.N\."), "UN")]
# Mehrwortausdrücke (normalisiert), die als ein Token zählen.
_PHRASES = {
    "vereinte nationen": "un", "vereinten nationen": "un", "united nations": "un",
    "weisses haus": "white_house", "weissen haus": "white_house", "white house": "white_house",
    "strasse von hormus": "hormuz", "strait of hormuz": "hormuz", "strasse von hormuz": "hormuz",
    "new york": "new_york", "hong kong": "hong_kong", "europaische union": "eu", "european union": "eu",
    "kunstliche intelligenz": "ai", "artificial intelligence": "ai", "sri lanka": "sri_lanka",
    "saudi arabia": "saudi", "saudi arabien": "saudi", "west bank": "westbank",
    "prime minister": "prime_minister", "foreign minister": "foreign_minister",
    "general assembly": "general_assembly", "security council": "security_council",
    "asian games": "asian_games", "world cup": "world_cup", "champions league": "champions_league",
    "premier league": "premier_league", "oktoberfest": "oktoberfest", "gaza city": "gaza",
    "man city": "manchester_city", "manchester city": "manchester_city",
    "manchester united": "manchester_united", "von der leyen": "vonderleyen",
    "al jazeera": "aljazeera", "el nino": "elnino", "la nina": "lanina", "orange order": "orange_order",
    "far right": "far_right", "far-right": "far_right",
    # Notenbanken als eigene Namen: „Reserve“ allein verbände Fed und die australische Notenbank.
    "federal reserve": "fed", "us federal reserve": "fed", "reserve bank of australia": "rba",
    "reserve bank of india": "rbi", "reserve bank of new zealand": "rbnz", "reserve bank": "reserve_bank",
    "european central bank": "ecb", "europaische zentralbank": "ecb", "bank of england": "boe",
    "bank of japan": "boj", "bank of canada": "boc", "swiss national bank": "snb",
    "schweizerische nationalbank": "snb",
}
_PHRASE_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, _PHRASES), key=len, reverse=True)) + r")\b")

# ---- Stoppwörter ----------------------------------------------------------------

_MONTHS_DAYS = """
januar februar marz april mai juni juli august september oktober november dezember jan feb mar apr jun
jul aug sep sept okt oct nov dez dec set montag dienstag mittwoch donnerstag freitag samstag sonntag
sonnabend montags sonntags wochenende montagabend sonntagabend samstagabend freitagabend
january february june july october december monday tuesday wednesday thursday friday saturday sunday
weekend tonight morgen abend nacht morning evening night
"""
_STOP_DE = set(normalize("""
aber alle allem allen aller alles als also am an ander andere anderen anderer anderes auch auf aus
bei beim beide beiden bereits besonders bin bis bisher bist bitte bleibt da dabei dadurch dafur
dagegen daher damit danach dann daran darauf daraus darf darum darunter das dass dazu dem den denen
denn der deren des deshalb dessen die dies diese diesem diesen dieser dieses doch dort drei du durch
durfen ein eine einem einen einer eines einige einmal er erst erste ersten es etwa etwas euch euer
fast fur gab ganz gar gegen geht gibt gleich hat hatte hatten hier hin hinter ich ihm ihn ihnen ihr
ihre ihrem ihren ihrer ihres im immer in ins ist ja jede jedem jeden jeder jedes jedoch jetzt kann
kaum kein keine keinem keinen keiner konnen konnte konnten lasst man manche mehr mein meine mit
muss mussen musste nach nachdem neben nein nicht nichts noch nun nur ob oder ohne schon sehr sei
seid sein seine seinem seinen seiner seit selbst sich sie sind so soll sollen sollte sollten somit
sondern sowie statt uber um und uns unser unsere unter viel viele vom von vor wahrend war waren
warum was weil weiter weitere welche welchem welchen welcher wenig wenn wer werde werden wie wieder
will wir wird wo wohl wollen wollte worden wurde wurden zu zum zur zwar zwei zwischen eins funf vier
sechs sieben acht neun zehn hundert tausend mal heute gestern jahr jahre jahren jahres tag tage tagen
woche wochen monat monaten stunde stunden minuten uhr prozent neu neue neuer neues neuen erneut
sagt sagte sagen gesagt erklart erklarte laut meldet melden berichtet berichten bericht zufolge
liveblog newsblog ticker liveticker live update updates eilmeldung video videos podcast kolumne
kommentar interview analyse meinung uberblick ueberblick news blog newsletter bilder fotos foto
galerie audio anzeige hintergrund faktencheck exklusiv zdf ard dpa afp reuters spiegel faz sz welt
tagesschau zeit taz ntv standard orf srf nzz bild focus stern heise golem handelsblatt fr
gut gute guten grosse grossen grosser kleine kleinen lange langer macht machen gemacht wirklich
eigentlich offenbar moglich moglicherweise zeigt zeigen bringt bringen geben gehen kommt kommen
lassen stehen steht sieht sehen weiss wissen bekommt nimmt nehmen halt halten fuhrt fuhren liegt
bleiben gilt setzt stellt spricht sprechen trifft ruft zieht tun uhr ca bzw etc usw dr prof hr
herr menschen leute milliarden milliarde millionen berichte haben habe hast jahrige jahriger jahrigen jahriges ende anfang mitte teil seite fall grund frage antwort
ab bald rund knapp mindestens hoch mehrere weniger weitem erste zweite dritte letzte letzten
nachsten nachste inzwischen zudem zunachst wegen trotz trotzdem dennoch sowohl weder allerdings
etwa wann wieso weshalb wem wen welches einfach schnell richtig falsch klar neben sogar
fc sv vfl tsv vfb tsg ssv spvgg sc sg rb bsc fsv sge
sorgt sorgte sorgten wirbel aufsehen furore aufregung emporung entsetzen kopfschutteln unverstandnis
schlagzeilen
"""
    + _MONTHS_DAYS).split())
_STOP_EN = set(normalize("""
a about above after again against ago all almost along already also although always am among an and
another any anyone anything are around as at back be became because become been before being below
between both but by came can cannot could did do does doing done down during each either else even
ever every few for from further get gets getting give given gives go goes going gone got had has have
having he her here hers herself him himself his how however i if in into is it its itself just last
least less like made make makes many may me might more most much must my near nearly need never new
next no nor not now of off often on once one only or other others our ours out over own per put
rather really said same say says see seen she should since so some something still such take takes
taken than that the their theirs them themselves then there these they this those though three
through thus to too two under until up upon us very via was way we well were what whatever when
where whether which while who whom whose why will with within without would yet you your yours
four five six seven eight nine ten hundred thousand million billion first second third fourth
latest news live update updates report reports reported reporting according breaking watch video
videos photo photos podcast opinion analysis exclusive explainer editorial column comment interview
review newsletter today yesterday tomorrow week weeks year years day days month months time times
hour hours minute minutes percent here there new big small top best worst full part amid set sets
per cent want wants told tell tells ask asks asked call calls called look looks looking come comes
coming know knows think thinks thought people thing things lot lots way ways yes mr mrs ms dr st
sir lady says saying claim show shows showing appears appeared continue reading read more click
also just like get got getting via know many much re ll ve don doesn didn isn aren wasn weren won
ahead behind inside outside across around toward towards onto amongst yet despite plus old
"""
    + _MONTHS_DAYS).split())
_STOP = {"de": _STOP_DE, "en": _STOP_EN}

# ---- Lexikon ----------------------------------------------------------------------
# Zeile "kanonisch: Form Form ...". Kanonisch darf aus mehreren Tokens bestehen ("+"). Formen werden
# normalisiert; gestemmte Formen (de/en) kommen automatisch dazu. Englische Formen sind nur nötig,
# wo sie sich nicht schon per Stemming auf die kanonische Form bringen lassen.
# Länder, Städte, Personen, Organisationen: Namen, die Artikel verankern (siehe Model.linkable).
_LEXICON_NAMES = """
germany: deutschland deutsch deutsche deutscher deutschen deutsches deutschlands german germans bundesrepublik brd
russia: russland russlands russisch russische russischen russischer russisches russen russe russian russians
ukraine: ukrainisch ukrainische ukrainischen ukrainischer ukrainer ukrainerin ukrainian ukrainians ukraines
usa: amerika amerikanisch amerikanische amerikanischen amerikanischer amerikaner american americans vereinigte staaten us-amerikanisch
china: chinesisch chinesische chinesischen chinesischer chinas chinese volksrepublik
israel: israelisch israelische israelischen israelischer israelis israeli israelis israels
iran: iranisch iranische iranischen iranischer iraner iranian iranians irans
france: frankreich frankreichs franzosisch franzosische franzosischen franzosischer franzosen french
britain: grossbritannien grossbritanniens britisch britische britischen britischer briten british brits vereinigtes konigreich
italy: italien italiens italienisch italienische italienischen italienischer italiener italian italians
spain: spanien spaniens spanisch spanische spanischen spanischer spanier spanish
switzerland: schweiz schweizer schweizerisch schweizerische schweizerischen eidgenossenschaft swiss
austria: osterreich osterreichs osterreichisch osterreichische osterreichischen osterreicher austrian austrians
poland: polen polens polnisch polnische polnischen polnischer polish poles
turkey: turkei turkisch turkische turkischen turkischer turken turkish turkiye turkiyes
japan: japanisch japanische japanischen japanischer japaner japanese japans
korea: koreanisch koreanische koreanischen koreaner korean koreans
south+korea: sudkorea sudkoreas sudkoreanisch sudkoreanische sudkoreanischen
north+korea: nordkorea nordkoreas nordkoreanisch nordkoreanische nordkoreanischen
india: indien indiens indisch indische indischen indischer inder indian indians
pakistan: pakistanisch pakistanische pakistanischen pakistani pakistanis
afghanistan: afghanisch afghanische afghanischen afghanen afghan afghans
syria: syrien syriens syrisch syrische syrischen syrer syrian syrians
lebanon: libanon libanons libanesisch libanesische libanesischen lebanese
egypt: agypten agyptens agyptisch agyptische agyptischen egyptian egyptians
saudi: saudi-arabien saudiarabien saudisch saudische saudischen saudi saudis
iraq: irak iraks irakisch irakische irakischen iraqi iraqis
yemen: jemen jemens jemenitisch jemenitische jemenitischen yemeni
qatar: katar katars katarisch katarische qatari
greece: griechenland griechenlands griechisch griechische griechischen griechen greek greeks
netherlands: niederlande niederlanden niederlandisch niederlandische niederlandischen holland hollandisch dutch
belgium: belgien belgiens belgisch belgische belgischen belgier belgian
denmark: danemark danemarks danisch danische danischen danish danes
sweden: schweden schwedens schwedisch schwedische schwedischen swedish swedes
norway: norwegen norwegens norwegisch norwegische norwegischen norwegian
finland: finnland finnlands finnisch finnische finnischen finnish finns
hungary: ungarn ungarns ungarisch ungarische ungarischen hungarian
czech: tschechien tschechiens tschechisch tschechische tschechischen czechia
romania: rumanien rumaniens rumanisch rumanische rumanischen romanian
bulgaria: bulgarien bulgariens bulgarisch bulgarische bulgarian
serbia: serbien serbiens serbisch serbische serbischen serbian serbs
croatia: kroatien kroatiens kroatisch kroatische croatian
bosnia: bosnien bosnisch bosnische bosnian
kosovo: kosovarisch kosovarische kosovar
belarus: weissrussland belarussisch belarussische belarusian
georgia: georgien georgiens georgisch georgische georgian
armenia: armenien armenisch armenische armenian
azerbaijan: aserbaidschan aserbaidschanisch azerbaijani azeri azeris
canada: kanada kanadas kanadisch kanadische kanadischen canadian canadians
mexico: mexiko mexikos mexikanisch mexikanische mexikanischen mexican mexicans
brazil: brasilien brasiliens brasilianisch brasilianische brasilianischen brazilian brazilians
argentina: argentinien argentiniens argentinisch argentinische argentinian argentine
colombia: kolumbien kolumbiens kolumbianisch kolumbianische colombian
venezuela: venezolanisch venezolanische venezuelan
cuba: kuba kubas kubanisch kubanische kubanischen cuban cubans
australia: australien australiens australisch australische australischen australian australians
new_zealand: neuseeland neuseelands neuseelandisch zealand
south+africa: sudafrika sudafrikas sudafrikanisch sudafrikanische sudafrikanischen
nigeria: nigerianisch nigerianische nigerian nigerians
kenya: kenia kenias kenianisch kenianische kenyan
ethiopia: athiopien athiopiens athiopisch athiopische athiopier athiopierin ethiopian ethiopians
sudan: sudanesisch sudanesische sudanese
palestine: palastina palastinenser palastinensisch palastinensische palastinensischen palestinian palestinians
gaza: gazastreifen gazastreifens gaza-streifen
westbank: westjordanland westjordanlands
taiwan: taiwanisch taiwanische taiwanese
vietnam: vietnamesisch vietnamesische vietnamese
thailand: thailandisch thailandische thai
philippines: philippinen philippinisch philippinische philippine filipino filipinos
indonesia: indonesien indonesisch indonesische indonesian
nepal: nepalesisch nepalesische nepali nepalese
europe: europa europas europaisch europaische europaischen europaischer european europeans
africa: afrika afrikas afrikanisch afrikanische afrikanischen african
asia: asien asiens asiatisch asiatische asiatischen asian
moscow: moskau moskaus
kyiv: kiew kiews kyjiw kyjiws kiev
beijing: peking pekings
tehran: teheran teherans
washington: washingtons
vienna: wien wiens
warsaw: warschau
rome: rom roms
brussels: brussel brussels
london: londons
paris: pariser
munich: munchen munchens munchner
bayern: bayerns bavaria
bayer: bayers
cologne: koln kolns kolner
lisbon: lissabon
athens: athen
copenhagen: kopenhagen
prague: prag
jerusalem: jerusalems
damascus: damaskus
cairo: kairo
new_york: newyork
kharkiv: charkiw charkow kharkov
zaporizhzhia: saporischschja saporischja zaporizhia zaporozhye
odesa: odessa
donetsk: donezk
luhansk: lugansk
kherson: cherson
crimea: krim
donbas: donbass
kremlin: kreml kremls
northern+ireland: nordirland nordirlands nordirisch nordirische nordirischen
orange_order: oranier oranierorden
iceland: islandisch islandische islandischen reykjavik
zelensky: selenskyj selenskyjs selenski zelenskyy zelenskiy zelenskyi zelenskyj
putin: putins
lavrov: lawrow lawrows
netanyahu: netanjahu netanjahus
erdogan: erdogans
khamenei: chamenei
araghchi: araghtschi araktschi
pezeshkian: peseschkian pesechkian
xi: jinping
shoigu: schoigu
medvedev: medwedew
navalny: nawalny
lukashenko: lukaschenko
kadyrov: kadyrow
zverev: swerew
hezbollah: hisbollah
houthi: huthi huthis houthis
hamas: hamas-terroristen
taliban: taliban-regierung
isis: islamischer islamic daesh
"""
# Allgemeine Nachrichtenwörter.
_LEXICON_WORDS = """
germany+government: bundesregierung bundesregierungen
government: regierung regierungen regierungs kabinett cabinet
parliament: parlament parlaments parlamente
chancellor: kanzler kanzlerin bundeskanzler bundeskanzlerin kanzleramt
president: prasident prasidenten prasidentin staatsprasident staatschef
prime_minister: premierminister premierministerin premier regierungschef regierungschefin ministerprasident
foreign_minister: aussenminister aussenministerin aussenministers
defence+minister: verteidigungsminister verteidigungsministerin
interior+minister: innenminister innenministerin
finance+minister: finanzminister finanzministerin
health+minister: gesundheitsminister gesundheitsministerin bundesgesundheitsminister
economy+minister: wirtschaftsminister wirtschaftsministerin
minister: ministerin ministers ministerium ministry
foreign+ministry: aussenministerium auswartiges amt
defence: verteidigung defense
general_assembly: generalversammlung vollversammlung
security_council: sicherheitsrat
pope: papst papstes pontifex
church: kirche kirchen katholisch katholische katholischen katholiken catholic catholics
police: polizei polizisten polizist polizeilich
military: militar militars militarisch militarische militarischen streitkrafte armee army
soldier: soldaten soldat soldiers
troops: truppen
air+force: luftwaffe
navy: marine
base: basis stutzpunkt militarbasis militarstutzpunkt luftwaffenbasis luftwaffenstutzpunkt airbase
court: gericht gerichts gerichte gerichtshof
constitutional+court: verfassungsgericht bundesverfassungsgericht
prosecutor: staatsanwaltschaft staatsanwalt staatsanwaltin prosecutors prosecution ankläger
election: wahl wahlen wahlkampf wahlkampfs elections
vote: abstimmung abstimmungen abgestimmt stimmen voted votes voters voter voting
referendum: volksabstimmung volksentscheid referenden
candidate: kandidat kandidaten kandidatin candidacy
party: partei parteien parties
coalition: koalition koalitions
opposition: oppositions
resign: rucktritt zurucktreten zuruckgetreten resignation resigns resigned
war: krieg krieges kriegs kriege wars
ceasefire: waffenruhe waffenstillstand feuerpause truce
peace: frieden friedens
talks: gesprache verhandlungen verhandeln verhandelt verhandlungsbereit negotiations negotiate negotiating
summit: gipfel gipfeltreffen
meeting: treffen meet meets met
attack: angriff angriffe angriffen attacke attacken anschlag anschlage anschlags angegriffen attacks attacked
air+attack: luftangriff luftangriffe luftangriffen airstrike airstrikes
drone: drohne drohnen drones
drone+attack: drohnenangriff drohnenangriffe
missile: rakete raketen marschflugkorper missiles
bomb: bombe bomben bombs bombing
explosion: explosionen explodiert explosions exploded
terror: terrorismus terroristisch terroristische terroristen terrorist terrorists terrorism
terror+suspect: terrorverdacht
terror+attack: terroranschlag terroranschlage
arrest: festnahme festnahmen festgenommen verhaftet verhaftung verhaftungen arrests arrested
suspect: verdachtige verdachtigen verdachtiger tatverdachtige mutmasslich suspects suspected
death: tote toten todesopfer getotet gestorben tot tod todes starb sterben kill kills killed killing dead deaths died dies
injured: verletzte verletzten verletzt schwerverletzt injuries injury wounded
earthquake: erdbeben beben quake
flood: uberschwemmung uberschwemmungen hochwasser flut fluten uberflutung uberflutungen floods flooding
storm: sturm sturme unwetter storms
typhoon: taifun
hurricane: hurrikan
wildfire: waldbrand waldbrande buschfeuer wildfires
fire: brand brande feuer
avalanche: lawine lawinen
accident: unfall unfalle unglucks ungluck accidents
crash: absturz abgesturzt crashed crashes
bus: reisebus reisebusse busse busunglück busunfall
plane: flugzeug flugzeuge maschine aircraft jet
airport: flughafen flughafens airports
train: zug zuge bahn
ship: schiff schiffe
tariff: zoll zolle zollen strafzolle strafzoll tariffs
trade: handel handels
sanctions: sanktionen sanktion sanction
economy: wirtschaft konjunktur economic
inflation: teuerung
interest+rate: zinsen zins leitzins zinssatz
central+bank: zentralbank notenbank
stock: borse aktien aktie stocks shares
budget: haushalt haushalts etat
tax: steuer steuern taxes
pension: rente renten pensions
unemployment: arbeitslosigkeit arbeitslose
strike: strikes
strike+labour_strike: streik streiks warnstreik warnstreiks ausstand walkout walkouts
union: gewerkschaft gewerkschaften unions
migration: migranten migrant migrants einwanderung zuwanderung zugewanderte zugewanderten immigration immigrants
refugee: fluchtlinge fluchtling gefluchtete refugees
asylum: asyl asylbewerber
border: grenze grenzen borders
deportation: abschiebung abschiebungen abgeschoben deportations deported
climate: klima klimawandel
heat: hitze hitzewelle heatwave
drought: durre
energy: energie
nuclear: atom atomar atomare atomaren nuklear nukleare atomkraft kernkraft
oil: ol olpreis olpreise
electricity: strom
ai: ki kunstliche
hacker: hackerangriff hackerangriffe cyberangriff cyberangriffe cyberattack hackers hacked
vulnerability: sicherheitslucke sicherheitslucken vulnerabilities
data+leak: datenleck
spy: spion spionage spionin spies espionage
sabotage: sabotiert
school: schule schulen schools
university: universitat universitaten hochschule universities
hospital: krankenhaus krankenhauser klinik hospitals
health: gesundheit
vaccine: impfstoff impfung impfungen vaccines vaccination
abuse: missbrauch missbrauchs
protest: demonstration demonstrationen demonstranten protestierende proteste protests protesters demonstrators
rally: kundgebung kundgebungen rallies
agreement: abkommen vereinbarung einigung deal deals
treaty: vertrag vertrage
visit: besuch besuche besucht visits visited
speech: rede
investigation: ermittlungen ermittelt ermitteln ermittelte ermittlungsverfahren ermittler untersuchung investigators investigate investigating probe
trial: prozess prozesses
verdict: urteil urteile ruling
lawsuit: klage klagen sued
murder: mord morde mordes ermordet murdered
shooting: schusse schiesserei schusswaffe shootings
knife+attack: messerangriff messerangriffe messerattacke messerattacken
knife: messer messern knives stabbing stabbed
hostage: geisel geiseln hostages
kidnap: entfuhrung entfuhrt kidnapped kidnapping abducted
prison: gefangnis haft jail
housing: wohnungsnot wohnungen wohnung wohnraum
neutrality: neutralitat
initiative: initiativen
steel: stahl
car: auto autos autoindustrie cars
record: rekord weltrekord records
award: auszeichnung auszeichnungen awards prize prizes preistrager preistragerin laureate
world+record: weltrekorde
football: fussball soccer
chess: schach schachspieler schachspielerin
fencing: fechten fechter fechterin
swimming: schwimmen schwimmer schwimmerin
athletics: leichtathletik
equestrian: reiten dressur
boxing: boxen boxer
national+team: nationalmannschaft nationalelf
coach: trainer bundestrainer
win: sieg siege gewinnt gewinnen gewonnen siegt wins won victory
defeat: niederlage verliert verloren loses lost
final: finale endspiel finals
olympic: olympia olympische olympischen olympics
championship: meisterschaft weltmeisterschaft championships
medal: medaille medaillen medals
gold: goldmedaille
green: grune grunen greens
social+democrat: sozialdemokraten sozialdemokratisch
conservative: konservative konservativen konservativ conservatives tory tories
far_right: rechtsextrem rechtsextreme rechtsextremen rechtsextremist rechtsextremisten rechtsextremer rechtspopulistisch rechtspopulistische rechtspopulisten
left+party: linkspartei
care: pflege pflegeheim pflegeheime pflegeversicherung pflegekassen
reform: reformen reformplan reformplane
pressure: druck
criticise: kritik kritisiert kritisieren kritisierte kritiker criticism criticize criticizes criticized critics critic
demand: fordert fordern forderte forderung forderungen gefordert demands demanded
reject: lehnt ablehnen abgelehnt ablehnung absage rejects rejected rejection
approve: billigt gebilligt genehmigt zustimmung approves approved approval
warn: warnt warnen warnte warnung warnungen gewarnt warns warned warning warnings
threaten: droht drohen drohte drohung drohungen threatens threatened threat threats
announce: kundigt angekundigt ankundigung announces announced announcement
plan: plant geplant geplante plans planned
propose: vorschlag vorschlage vorgeschlagen proposal proposals proposes proposed
decide: entscheidung entscheidet entschieden beschluss beschlossen decision decides decided
confirm: bestatigt bestatigte bestatigung confirms confirmed confirmation
deny: bestreitet dementiert zuruckgewiesen denies denied
accuse: vorwurf vorwurfe wirft beschuldigt accuses accused accusation accusations
condemn: verurteilt verurteilen verurteilung condemns condemned
praise: lob lobt loben gelobt praises praised
support: unterstutzung unterstutzt unterstutzen supports supported
agree: einigen einigt geeinigt agrees agreed
disagree: uneins uneinig disagree disagreement
escalation: eskalation deeskalation deeskalieren eskalieren escalate escalating de-escalation deescalation
diplomat: chefdiplomat chefdiplomaten diplomaten diplomatisch diplomatie diplomacy diplomatic diplomats
adversary: gegner gegnern opponent opponents enemy enemies
counterpart: amtskollege amtskollegen amtskollegin counterparts
relations: beziehungen beziehung
foreign_minister: bundesaussenminister bundesaussenministerin
invasion: invasion einmarsch uberfall angriffskrieg
occupied: besetzt besetzte besetzten besatzung besatzungsgebiet occupation
front: front frontstadt frontlinie
annex: annektieren annexion annexation annexing
settlement: siedlung siedlungen siedler settlements settlers
commander: kommandeur kommandant commanders
attack+plan: anschlagsplan anschlagsplane
explosives: sprengstoff sprengsatz sprengstoffe explosive
evacuate: evakuiert evakuierung evakuieren evacuated evacuation evacuations
rescue: rettung retter rettungskrafte gerettet rescuers rescued rescues
missing: vermisst vermisste vermissten
victim: opfer opfern victims
survivor: uberlebende uberlebenden survivors
perpetrator: tater taterin tatern perpetrators
gunman: schutze schutzen gunmen
police+officer: polizistin beamte beamten officers officer
detain: festgehalten inhaftiert detained detention detains
charge: angeklagt anklage charges charged indictment indicted
sentence: haftstrafe sentenced
eviction: zwangsraumung zwangsraumungen raumung evicted evictions evict
tenant: mieter mieterin mieterinnen tenants
rent: miete mieten mietpreise rents rental
crowd: massen menschenmenge crowds
thousands: tausende tausenden zehntausende zehntausenden
bishop: bischof bischofe bishops
priest: priester priests clergy klerus
law: gesetz gesetze gesetzes laws legislation
constitution: verfassung verfassungs constitutional
ban: verbot verboten verbietet bans banned
rule: regeln regel regelung rules
result: ergebnis ergebnisse resultat resultate results
majority: mehrheit mehrheiten majorities
vote: stimmvolk stimmbevolkerung stimmberechtigte wahler wahlerinnen
cooperation: zusammenarbeit kooperation cooperate
verdict: entscheid
appeal: berufung berufungsgericht appeals
march: marsch aufmarsch
parade: paraden parades
bid: bewerbung bewerber bewerbungen bewerbungsverfahren
host: gastgeber ausrichter ausrichten austragen hosting
game: spiel spiele games match matches
goal: tore treffer goals
player: spieler spielerin players
season: saison seasons
title: titel titles
tournament: turnier turniers tournaments
race: rennen races
runner: laufer lauferin runners
bronze: bronzemedaille
silver: silbermedaille silber
women: frauen damen
men: manner herren
flood: uberflutet uberschwemmt sturzflut sturzfluten
rain: regen starkregen regenfalle rainfall rains downpour
landslide: erdrutsch erdrutsche erdrutschen erdrutschs landslides mudslide mudslides
disaster: katastrophe katastrophen katastrophengebiet disasters
emergency: notstand notfall emergencies
shelter: notunterkunft notunterkunfte unterkunfte shelters
power+outage: stromausfall stromausfalle blackout outages outage
wind: winde windboen boen winds gusts
coast: kuste kusten coastal coasts
mountain: berg berge gebirge mountains
collapse: einsturz eingesturzt einsturzen collapsed collapses
building: gebaude hochhaus hauser haus buildings
apartment: wohnhaus apartments
highway: autobahn autobahnen motorway
driver: fahrer fahrerin busfahrer drivers
passenger: passagier passagiere passagieren fahrgaste passengers
tourist: touristen tourist tourists urlauber reisegruppe
flight: flug fluge flights
airline: fluggesellschaft fluglinie airlines
data+center: rechenzentrum rechenzentren datacentre datacenter
mobile+operator: mobilfunkanbieter mobilfunkbetreiber
telecom: telekommunikation
network: netz netze netzwerk networks
digital: digitale digitalen
model: modell modelle models
training: trainiert
incident: zwischenfall zwischenfalle vorfall vorfalle incidents
security: sicherheit sicherheits
breach: sicherheitsverstoss verstosse verstoss breaches
chip: chips halbleiter semiconductor semiconductors
agent: agenten agents
user: nutzer nutzerin nutzern users
leak: leck geleakt leaked leaks
drone: unterwasserdrohne
submarine: u-boot uboot uboote submarines
seize: beschlagnahmt sichergestellt erbeutet seized seizes seizure capture captured captures
blockade: blockade blockaden blockiert blockieren
reopen: wiedereroffnung wiedereroffnen offnung reopening reopens
propose: angebot friedensvorschlag
peace+plan: friedensplan
condition: bedingungen bedingung conditions
talks: gesprach gesprachen unterredung
midterm: zwischenwahlen midterms
ambassador: botschafter botschafterin ambassadors
envoy: gesandter sondergesandter envoys
spy: spionagevorwurf
sabotage: sabotageakt
debt: schulden verschuldung debts
price: preis preise prices
cost: kosten costs
spending: ausgaben spending
cut: kurzungen kurzung gekurzt cuts
funds: gelder funding
investor: investor investoren investors
company: unternehmen konzern firma firmen companies
worker: arbeiter beschaftigte arbeitnehmer workers employees
job: arbeitsplatze jobs
wage: lohn lohne gehalt wages salaries
consumer: verbraucher verbraucherinnen konsumenten consumers
fuel: treibstoff kraftstoff kraftstoffe sprit benzin diesel
tax: steuererhohung
social: sozial soziale sozialen
welfare: sozialleistungen burgergeld sozialhilfe benefits
pension: rentner pensioners
crime: kriminalitat verbrechen crimes criminal
violence: gewalt gewalttat violent
gang: bande banden gangs
weapon: waffe waffen weapons arms
leader: chefin fuhrung anfuhrer leaders leadership
chair: vorsitz vorsitzende vorsitzender parteivorsitz parteichef parteichefin chairman chairwoman
term: amtszeit amtszeiten
candidate: kandidieren kandidiert kandidatur
re-election: wiederwahl wiedergewahlt
convention: parteitag parteitage conference
poll: umfrage umfragen polls survey
defeat: wahlniederlage wahlniederlagen
youth: jugend jugendliche jugendlichen
children: kinder kindern kids
student: schuler studenten schulerinnen students pupils
teacher: lehrer lehrerin teachers
doctor: arzt arzte arztin doctors physicians
patient: patienten patients
disease: krankheit krankheiten diseases
virus: viren viruses
museum: museen museums
film: filme films movie movies
festival: festivals festspiele
book: buch bucher books novel
author: autorin schriftsteller schriftstellerin writer
artist: kunstler kunstlerin artists
music: musik
concert: konzert konzerte concerts
beer: bier biere
tradition: brauch traditionen traditions
anniversary: jahrestag jubilaum anniversaries
history: geschichte historisch historic historical
monument: denkmal denkmaler monuments
statue: statuen statues
island: insel inseln islands
city: stadte cities town
capital: hauptstadt hauptstadte capitals
region: regionen regions
village: dorf dorfer villages
residents: bewohner anwohner einwohner inhabitants
neighbour: nachbarn nachbar neighbors neighbours
family: familie familien families
mother: mutter mothers
father: vater fathers
son: sohn sohne sons
daughter: tochter daughters
wife: ehefrau
husband: ehemann
body: leiche leichen bodies
funeral: beerdigung trauerfeier funerals
court+ruling: gerichtsurteil
judge: richter richterin judges
lawyer: anwalt anwaltin anwalte lawyers attorney
inquiry: untersuchungsausschuss inquiries
study: studie studien studies
researcher: forscher forscherin forschende researchers scientists
scientist: wissenschaftler wissenschaftlerin
space: weltraum weltall
telescope: teleskop weltraumteleskop
satellite: satelliten satellites
rocket: raketenstart
launch: gestartet launches launched
"""


def _parse_lexicon(text: str) -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for line in text.strip().splitlines():
        canon, _, forms = line.partition(":")
        target = tuple(canon.strip().split("+"))
        for form in normalize(forms).split():
            out.setdefault(form, target)
        if len(target) == 1:
            out.setdefault(target[0], target)
    return out


def _stemmed(lexicon: dict[str, tuple[str, ...]], stem) -> dict[str, tuple[str, ...]]:
    """Gestemmte Formen je Sprache (z. B. "sanktion" aus "sanktionen"), nachgeschlagen nur mit einem
    Wort derselben Sprache, das kein Eintrag selbst ist: Sonst machte der deutsche Stemmer aus
    "worker" ein "work" und aus „kürzen“ (kurzen) ein „kurz“."""
    out: dict[str, tuple[str, ...]] = {}
    for form, target in lexicon.items():
        st = stem(form)
        if st != form and len(st) >= 3 and st not in lexicon:
            out.setdefault(st, target)
    return out


# ---- Stemming ---------------------------------------------------------------------

_DE_SUFFIXES = ("innen", "ern", "em", "en", "er", "es", "e", "s", "n")


def _stem_de(w: str) -> str:
    """Leichtes Stemming für Deutsch: Flexions- und Pluralendungen (nur eine), Stamm mindestens 4."""
    if len(w) <= 4 or w.isdigit():
        return w
    for suf in _DE_SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            if suf == "innen" and len(w) < 9:
                continue
            if suf == "s" and w.endswith(("ss", "nis", "us", "is", "os", "as")):
                continue
            return w[:-len(suf)]
    return w


def _undouble(w: str) -> str:
    if len(w) >= 4 and w[-1] == w[-2] and w[-1] not in "lsz":
        return w[:-1]
    return w


def _stem_en(w: str) -> str:
    """Leichtes Stemming für Englisch: Plural, -ed, -ing, stummes e."""
    if len(w) <= 3 or w.isdigit():
        return w
    if w.endswith(("ies", "ied")) and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith(("sses", "shes", "ches", "xes", "zes")):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is", "os", "as")):
        w = w[:-1]
    if w.endswith("ing") and len(w) >= 7:
        w = _undouble(w[:-3])
    elif w.endswith("ed") and len(w) >= 6:
        w = _undouble(w[:-2])
    if w.endswith("e") and len(w) >= 5:
        w = w[:-1]
    return w


LEXICON = _parse_lexicon(_LEXICON_NAMES + _LEXICON_WORDS)
_LEXICON_STEMS = {"de": _stemmed(LEXICON, _stem_de), "en": _stemmed(LEXICON, _stem_en)}
# Tokens aus den allgemeinen Nachrichtenwörtern (Regierung, Angriff, Wahl ...): nie ein Name und nie
# das „besondere“ Wort, das zwei deutsche Artikel verbinden muss (siehe Model.linkable).
GENERIC = frozenset(t for ts in _parse_lexicon(_LEXICON_WORDS).values() for t in ts) - frozenset(
    t for ts in _parse_lexicon(_LEXICON_NAMES).values() for t in ts)
# Häufige deutsche Substantive ohne Eintrag im Lexikon: Auch sie machen kein Ereignis fest (deutsche
# Substantive sind immer groß geschrieben, die Kleinschreibung verrät sie also nicht; siehe specific).
_PLAIN_DE_WORDS = """
abend absicht abstand alltag alter amt anfang angaben angebot angst anlass anschluss ansicht anspruch
antwort anzahl arbeit art aufgabe aufsicht auftrag auftritt ausblick ausgang aussage ausstellung auswahl
auto bau bedeutung bedingung beginn begriff behorde beispiel beitrag bereich bericht beruf bestand besuch
betrieb bevolkerung bewegung beziehung bild bilanz blick boden branche brief bruder buch bund bundnis
burger chance chef dach debatte details dienst ding druck duell dutzend ecke ehe einfluss einsatz eltern
ende entscheidung entwicklung erfahrung erfolg ergebnis erinnerung erkenntnis erklarung erwartung essen
fach fahrer fahrzeug fall farbe feld fenster ferien fest figur film firma fläche folge form forderung
forschung frage freiheit freude freund fuhrung fuss gast gebiet gebaude gefahr gefuhl gegenteil gegner
geld gelegenheit gemeinde gerechtigkeit geschichte gesellschaft gesetz gesicht gesprach gewalt glaube
glück grad grenze grund gruppe hand haus heim herbst herz hilfe himmel hinweis hoffnung hohe hund idee
infrastruktur inhalt interesse jahrzehnt jugend kampf kanton karriere kasse kauf kind kirche klasse kopf
kosten kraft kreis krimi krise kritik kultur kunde kunst kurs lage land last leben lehre leistung leute
licht liebe liga linie liste luft macht mann mannschaft markt mauer medien meinung menge mensch miete
minute mitglied mittel moglichkeit monat moment motiv mut mutter nachbar nachfolger nachfrage nachricht
nahe name natur netz norden nord osten ort paar partner person platz plan politik posten preis problem
produkt projekt prozent publikum punkt quelle rahmen rand rat raum realitat recht rede regel reihe reise
rennen richtung risiko rolle ruf ruhe runde saison sache schaden schluss schmerz schock schritt schuld
schule schutz schwester see seite sekunde show sicht sieg sinn sitz sommer sorge spass spiel spieler
spitze sprache stadion stadt stand start stelle stimme stimmung strasse strecke streit stuck stunde
suche summe szene tag tat technik teil tempo thema tier tipp titel tod ton tor tradition traum trend
uberraschung umfeld umgang umsetzung umwelt unmut ursache urteil vater veranderung verband verbindung
verdacht verein verfahren vergleich verhalten verlust version vertrauen verwaltung vorbereitung vorlage
vorschlag vorstoss vorteil vorwurf wahrheit wald wand wasser weg welt wende werk wert wettbewerb
widerstand wirkung wissen woche wort wunsch wut zahl zeichen zeit zentrum ziel zimmer zukunft zusammenhang
zustand zutat zweifel regisseur autor autorin dorf insel kilo kilometer meter tonne liter
feuerwehr feuerwehrleute polizei polizist polizisten polizeisprecher insasse insassen anwohner anwohnerin
passant passanten zeuge zeugin zeugen ermittler ermittlung ermittlungen staatsanwalt staatsanwaltschaft
gericht richter richterin prozess anklage klage tater opfer leiche verletzung einsatzkrafte rettungskrafte
rettungsdienst notarzt notaufnahme krankenhaus klinik hubschrauber rettungshubschrauber flugzeug
kleinflugzeug flughafen pilot pilotin passagier passagiere besatzung maschine zug bahnhof gleis bus
lastwagen lkw pkw autobahn bundesstrasse kreuzung unfallstelle brand feuer flammen rauch explosion
lagerhalle halle wohnhaus wohnung gebaude keller dachstuhl fabrik werk lager schule kita bergsteiger
bergsteigerin wanderer bergrettung bergwacht hochwasser starkregen regen gewitter hagel sturm hitze
kalte schnee eis wetter unternehmen konzern mitarbeiter mitarbeitende beschaftigte stellen arbeitsplatze
umsatz gewinn verlust aktie aktien anleger investor investoren kunde kunden verbraucher preise inflation
zinsen steuer steuern haushalt schulden rente renten reform regierung minister ministerin ministerium
kanzler kanzlerin behorden sprecher sprecherin partei parteien fraktion koalition opposition wahl wahlen
wahler abstimmung umfrage parlament gesetzentwurf vorschlag verhandlung verhandlungen gesprache treffen
gipfel besucher zuschauer fans trainer mannschaft spiel spielerin torwart stuermer sieg niederlage
tabelle meisterschaft turnier finale halbfinale saisonauftakt einwohner burgermeister landrat
bundestag bundesrat landtag landtage parlament kabinett abgeordnete abgeordneten ausschuss plenum
"""
# Deutsche Wörter, die im Lexikon als englische Wörter stehen, aber im Deutschen etwas anderes heißen.
_DE_ONLY = {"island": ("iceland",), "islands": ("iceland",)}
# Tokens, die immer als Name zählen (Länder, Städte, Personen, Organisationen; Mehrwortnamen).
NAME_TOKENS = frozenset(t for ts in _parse_lexicon(_LEXICON_NAMES).values() for t in ts) | frozenset((
    "usa", "britain", "eu", "un", "nato", "isis", "ecb", "imf", "iaea", "who_org", "g7", "g20",
    "white_house", "hormuz", "new_york", "hong_kong", "sri_lanka", "saudi", "westbank", "asian_games",
    "world_cup", "champions_league", "premier_league", "oktoberfest", "manchester_city",
    "manchester_united", "vonderleyen", "aljazeera", "fed", "rba", "rbi", "rbnz", "reserve_bank", "boe",
    "boj", "boc", "snb"))


# ---- Tokenisierung ------------------------------------------------------------------

def clean(text: str) -> str:
    """Feed-Floskeln entfernen, Abkürzungen mit Punkten zusammenziehen."""
    for rx in _BOILERPLATE:
        text = rx.sub(" ", text)
    for rx, repl in _DOTTED:
        text = rx.sub(repl, text)
    return text


def words(text: str) -> list[str]:
    """Normalisierte Wörter eines Texts; bekannte Abkürzungen (US, EU, KI) schon kanonisch,
    Mehrwortausdrücke (Vereinte Nationen) als ein Wort."""
    out: list[str] = []
    for raw in _WORD_RE.findall(clean(text)):
        acro = _ACRONYMS.get(raw)
        out.append(acro if acro else normalize(raw))
    joined = " ".join(out)
    if _PHRASE_RE.search(joined):
        joined = _PHRASE_RE.sub(lambda m: _PHRASES[m.group(1)], joined)
        return joined.split()
    return out


_CASE_RE = re.compile(r"[^\W\d_][^\W_]*")
_SENTENCE_END = frozenset(".!?:;\"'“”„»«‘’–—-([")


def _case_evidence(text: str) -> Iterable[tuple[str, bool]]:
    """(normalisiertes Wort, groß geschrieben?) für Wörter mitten im Satz eines englischen Texts;
    Überschriften in Title Case („Five Men Arrested Near ...“) zählen nicht."""
    text = clean(text)
    found = list(_CASE_RE.finditer(text))
    long_words = [m.group() for m in found if len(m.group()) >= 4]
    if long_words and sum(w[0].isupper() for w in long_words) >= 0.7 * len(long_words):
        return
    prev_end = None                         # Ende des vorigen Worts; None: erstes Wort (Satzanfang)
    for m in found:
        gap = text[prev_end:m.start()].rstrip() if prev_end is not None else None
        prev_end = m.end()
        if gap is None or (gap and gap[-1] in _SENTENCE_END):
            continue                        # Satzanfang: Großschreibung sagt nichts
        w = m.group()
        if w.isupper() and len(w) > 5:
            continue                        # Versalien-Überschrift („OWN GOAL“), keine Aussage
        if len(w) >= 2:
            yield normalize(w), w[0].isupper()


def _keep_number(w: str) -> bool:
    """Zahlen zählen, wenn sie etwas unterscheiden können (87-Jährige, 27 Tote, Boeing 737), nicht
    einstellige, Jahre und 000; häufige (Tagesdaten) drückt die IDF."""
    if len(w) < 2 or w == "000":
        return False
    if len(w) == 4 and w[:2] in ("19", "20"):
        return False
    return True


class Tokenizer:
    """Wörter -> kanonische Tokens für eine Sprache. `shared` sind Wörter, die im Fenster in beiden
    Sprachen vorkommen (vor allem Namen): Die bleiben ungestemmt, damit de und en sie gleich
    abbilden. `known` sind deutsche Wörter, in die lange Komposita zerlegt werden dürfen."""

    def __init__(self, shared: set[str] | None = None, known: set[str] | None = None):
        self.shared = shared or set()
        self.known = known or set()
        self.compounds: dict[str, tuple[str, ...]] = {}     # Kompositum -> Tokens seiner Teile
        self._cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}

    def canon(self, w: str, lang: str) -> tuple[tuple[str, float], ...]:
        """Kanonische Tokens eines Worts mit Gewichtsfaktor (Teile eines Kompositums geringer)."""
        key = (w, lang)
        hit = self._cache.get(key)
        if hit is None:
            hit = self._canon(w, lang)
            if len(self._cache) >= TOKEN_CACHE_MAX:
                self._cache.clear()         # begrenzt (related() tokenisiert laufend neue Artikel)
            self._cache[key] = hit
        return hit

    def _canon(self, w: str, lang: str) -> tuple[tuple[str, float], ...]:
        lang = "de" if lang == "de" else "en"
        if w in _STOP[lang] or len(w) < 2:
            return ()
        if w.isdigit():
            return ((w, 1.0),) if _keep_number(w) else ()
        if _ORDINAL_RE.fullmatch(w):
            return ()
        if w in _ACRONYM_TOKENS:
            return ((w, 1.0),)
        target = _DE_ONLY.get(w) if lang == "de" else None
        target = target or LEXICON.get(w)
        if target is None and w in self.shared:
            return ((w, 1.0),)
        if target is None:
            stem = _stem_de(w) if lang == "de" else _stem_en(w)
            if stem in _STOP[lang]:
                return ()
            target = LEXICON.get(stem) or _LEXICON_STEMS[lang].get(stem)
            if target is None:
                parts = self._split(w) if lang == "de" else ()
                if parts:
                    self.compounds[stem] = tuple(t for t, _ in parts)
                return ((stem, 1.0), *parts)
        return tuple((t, 1.0) for t in target)

    def _split(self, w: str) -> tuple[tuple[str, float], ...]:
        """Langes deutsches Kompositum in zwei bekannte Teile zerlegen (Fugen-s erlaubt):
        „Neutralitätsinitiative“ -> neutrality, initiative. Längster rechter Teil gewinnt."""
        if len(w) < 9:
            return ()
        for i in range(4, len(w) - 3):
            left, right = w[:i], w[i:]
            left_ok = (left in self.known or (left.endswith("s") and left[:-1] in self.known)
                       or left + "e" in self.known or left + "en" in self.known)
            if left_ok and right in self.known:
                left = left if left in self.known else left[:-1]
                out: list[tuple[str, float]] = []
                for part in (left, right):
                    stem = _stem_de(part)
                    target = LEXICON.get(part) or LEXICON.get(stem) or _LEXICON_STEMS["de"].get(stem) or (stem,)
                    out += [(t, PART_FACTOR) for t in target]
                return tuple(out)
        return ()

    def tokens(self, text: str, lang: str) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        for w in words(text):
            out += self.canon(w, lang)
        return out


_ACRONYM_TOKENS = frozenset(_ACRONYMS.values()) | frozenset(_PHRASES.values())

# Ereignisarten: Zwei Artikel, die beide eine Art nennen, aber keine gemeinsame, berichten über
# verschiedene Ereignisse (Streik bei der Lufthansa und brennende Powerbank im Lufthansa-Flug,
# Basketball- und Fechtgold bei denselben Asienspielen, Lawine und Sturzflut in Nepal). Erkannt nur an
# Substantiven in Titel und erstem Satz (keine Verben: „fires“, „derailed“, „bewaffnete“ sind oft
# übertragen gemeint), je Dimension (event, sport, comp = Wettbewerb, medal = Medaille im Titel).
_KIND_WORDS = {
    "event:quake": "earthquake earthquakes quake quakes tsunami erdbeben seebeben",
    "event:flood": "flood floods flooding hochwasser uberschwemmung uberschwemmungen flut sturzflut sturzfluten",
    "event:storm": ("storm storms hurricane hurricanes typhoon typhoons cyclone tornado sturm sturme unwetter "
                    "orkan hurrikan taifun zyklon"),
    "event:landslide": "landslide landslides mudslide mudslides erdrutsch erdrutsche mure muren murenabgang",
    "event:avalanche": "avalanche avalanches lawine lawinen",
    "event:collapse": "einsturz",
    "event:capsize": "shipwreck schiffsungluck bootsungluck fahrungluck",
    "event:fire": "fire blaze wildfire wildfires inferno brand feuer waldbrand grossbrand explosion explosionen",
    "event:crash": ("accident crash derailment collision unfall absturz entgleisung zusammenstoss zugungluck "
                    "busungluck flugzeugabsturz"),
    "event:shooting": "shooting shootings gunman gunmen gunfire schusse schiesserei amoklauf",
    "event:knife": "knife stabbing messer messerangriff messerattacke",
    "event:labour_strike": "streik streiks warnstreik ausstand walkout",
    "event:volcano": "volcano eruption vulkan vulkanausbruch",
    "sport:basketball": "basketball", "sport:baseball": "baseball", "sport:handball": "handball",
    "sport:volleyball": "volleyball", "sport:football": "football fussball soccer",
    "sport:fencing": "fencing fechten", "sport:swimming": "swimming schwimmen freestyle",
    "sport:tennis": "tennis", "sport:golf": "golf", "sport:cricket": "cricket", "sport:rugby": "rugby",
    "sport:cycling": "cycling radrennen radsport", "sport:boxing": "boxing boxen", "sport:judo": "judo",
    "sport:skateboarding": "skateboarding", "sport:esports": "esports", "sport:hockey": "hockey eishockey",
    "sport:badminton": "badminton", "sport:diving": "diving", "sport:equestrian": "equestrian dressage dressur",
    "sport:athletics": "athletics leichtathletik relay", "sport:marathon": "marathon", "sport:chess": "chess schach",
    "sport:kayak": "kayak kanu canoe",
    "comp:bundesliga": "bundesliga", "comp:champions_league": "champions_league",
    "comp:premier_league": "premier_league", "comp:world_cup": "world_cup", "comp:cup": "pokal",
    "medal:gold": "gold goldmedaille", "medal:silver": "silver silber silbermedaille",
    "medal:bronze": "bronze bronzemedaille",
}
_KINDS = {w: kind for kind, text in _KIND_WORDS.items() for w in normalize(text).split()}
# Deutsche Komposita auf -brand, -beben, -unfall ... (Kellerbrand, Seebeben, Busunfall).
_KIND_SUFFIXES = (("brand", "event:fire"), ("beben", "event:quake"), ("unfall", "event:crash"),
                  ("absturz", "event:crash"), ("einsturz", "event:collapse"), ("lawine", "event:avalanche"),
                  ("hochwasser", "event:flood"), ("streik", "event:labour_strike"), ("messerangriff", "event:knife"))


def _kinds(title: Iterable[str], lead: Iterable[str] = ()) -> frozenset:
    """Ereignisarten eines Artikels aus den normalisierten Wörtern von Titel und erstem Satz. Medaillen
    zählen nur im Titel und nur bei Sportartikeln."""
    title = list(title)
    out: set[str] = set()
    for w in (*title, *lead):
        kind = _KINDS.get(w)
        if kind is None and len(w) >= 8:
            kind = next((k for suffix, k in _KIND_SUFFIXES if w.endswith(suffix)), None)
        if kind and not kind.startswith("medal:"):
            out.add(kind)
    if any(k.startswith("sport:") for k in out):
        out.update(_KINDS[w] for w in title if _KINDS.get(w, "").startswith("medal:"))
    return frozenset(out)


_SCORE_RE = re.compile(r"(?<![\d.,:/])(\d{1,2})\s?[-:–]\s?(\d{1,2})(?![\d.,:/%])(?!\s*(?:uhr|h\b|am\b|pm\b))",
                       re.I)


def _scores(*texts: str) -> frozenset:
    """Spielstände aus Titel und erstem Satz als Art „score:3-1“ (größere Zahl zuerst): Zwei Berichte mit
    verschiedenen Ergebnissen sind verschiedene Spiele (Chelsea gegen Arsenal bei den Männern 2:1, bei den
    Frauen 1:0), siehe kinds_conflict. Keine Uhrzeiten (20:00) oder Zahlen über 20."""
    out = set()
    for text in texts:
        for a, b in _SCORE_RE.findall(text or ""):
            if (len(b) == 2 and b.startswith("0")) or int(a) > 20 or int(b) > 20:
                continue
            hi, lo = sorted((int(a), int(b)), reverse=True)
            out.add(f"score:{hi}-{lo}")
    return frozenset(out)


def kinds_conflict(a: frozenset, b: frozenset) -> bool:
    """True, wenn beide Artikel in einer Dimension Ereignisarten nennen, aber keine gemeinsame."""
    if not a or not b:
        return False
    for dim in {k.split(":", 1)[0] for k in a} & {k.split(":", 1)[0] for k in b}:
        va = {k for k in a if k.startswith(dim + ":")}
        vb = {k for k in b if k.startswith(dim + ":")}
        if not va & vb:
            return True
    return False


# Kanonische Formen und die Wörter selbst (ein Wort, das es im Fenster in beiden Sprachen gibt, bleibt
# unverändert, siehe Tokenizer.shared).
_PLAIN_DE = frozenset(t for w in normalize(_PLAIN_DE_WORDS).split()
                      for t in (w, *(t for t, _ in Tokenizer().canon(w, "de"))))


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ„\"'])")


def _first_sentence(text: str) -> str:
    return _SENTENCE_RE.split(clean(text).strip(), maxsplit=1)[0][:300]


# Meinung, Analyse, Porträt, Liveblog, Newsletter: Solche Stücke streifen oft mehrere Ereignisse
# (Kommentar zur Koalition, Morgen-Newsletter, Liveblog). Sie gehören nur bei sehr hoher Ähnlichkeit zu
# einer Geschichte und verbinden nie andere Artikel miteinander.
_SOFT_TITLE = re.compile(
    r"\b(?:kommentar|meinung|leitartikel|kolumne|analyse|essay|porträt|portrait|interview|gastbeitrag|"
    r"liveblog|newsblog|live-?ticker|ticker|newsletter|presseschau|der morgen|am morgen|briefing|"
    r"opinion|analysis|explainer|editorial|comment|column|podcast|what we know|live:|as it happened|"
    r"latest updates|live updates|evening brief|morning brief|week in review|in pictures)\b"
    r"|^\s*\+\+\+|\bhas lost (?:about |around |approximately )?[\d,. ]+ troops", re.I)
_SOFT_URL = re.compile(r"/(?:meinung|kommentar|kommentare|opinion|commentisfree|analyse|analysis|kolumne|"
                       r"columns?|liveblog|live-?blog|newsletter|podcast|live)/|liveblog|newsblog|newsletter", re.I)


# Wiederkehrende Sammelstücke (Liveblog, Newsticker, Newsletter, Sendung, tägliche Verlustzahlen): nie
# ein „anderes Thema“ zu einem Artikel, sie streifen jeden Tag alles.
_RECURRING = re.compile(
    r"\b(?:liveblog|newsblog|live-?ticker|ticker|newsletter|der morgen|am morgen|presseschau|briefing|"
    r"live updates|latest updates|as it happened|tagesthemen|in 100 sekunden|podcast|live:)|^\s*\+\+\+|"
    r"^\s*(?:tagesschau|tagesthemen|heute journal)(?:\s+(?:\d|in\b|vor\b)|\s*$)|\bhas lost (?:about |around |approximately )?[\d,. ]+ troops", re.I)


def is_recurring(row: Any) -> bool:
    """Wiederkehrendes Sammelstück (Titel oder URL)?"""
    return bool(_RECURRING.search(row.get("title") or "") or
                re.search(r"liveblog|newsblog|newsletter|/live/", row.get("url") or "", re.I))


def is_soft(row: Any) -> bool:
    """Kommentar, Analyse, Porträt, Liveblog oder Newsletter (Titel oder URL)?"""
    title = row.get("title") or ""
    return bool(_SOFT_TITLE.search(title) or _SOFT_URL.search(row.get("url") or ""))


# Verlage mit mehreren Quellen im Katalog: Ihre Ausgaben zählen als eine Zeitung.
_PUBLISHER_PREFIXES = (("thelocal", "thelocal"),)
_PUBLISHERS = {"heisesec": "heise"}


def publisher(source_id: str) -> str:
    """Verlag einer Quelle: The Local Deutschland/Österreich/... sind eine Zeitung, heise Security
    gehört zu heise; sonst die Quelle selbst."""
    for prefix, name in _PUBLISHER_PREFIXES:
        if source_id.startswith(prefix):
            return name
    return _PUBLISHERS.get(source_id, source_id)


COPY_MIN_SUMMARY = 40               # kürzere Anreißer: keine Kopie („Der Morgen“, „Presseschau“)


def copy_key(row: Any) -> str | None:
    """Schlüssel wörtlich gleicher Meldungen (Titel und Anfang des Anreißers); None bei zu kurzem
    Anreißer, dann ist nichts eine Kopie."""
    summary = normalize(row["summary"] or "").strip()
    if len(summary) < COPY_MIN_SUMMARY:
        return None
    return normalize(row["title"] or "").strip() + "|" + summary[:160]


# ---- Dokumente und Vektoren -----------------------------------------------------------

@dataclass(slots=True)
class Doc:
    id: int
    source: str
    lang: str
    ts: float                           # published_at als Unix-Zeit
    vec: dict[str, float] = field(default_factory=dict)   # normiert (Länge 1)
    # Nur Wörter, die im Fenster in beiden Sprachen vorkommen, neu normiert: für de/en-Vergleiche
    # (unübersetzte Wörter können nie passen und würden die Ähnlichkeit nur verdünnen).
    xvec: dict[str, float] = field(default_factory=dict)
    story_id: int | None = None         # bisherige story_id aus der Datenbank
    kinds: frozenset = frozenset()      # Ereignisarten (siehe _kinds), z. B. {"event:quake"}
    title: frozenset = frozenset()      # Tokens des Titels (im Vektor)
    lead: frozenset = frozenset()       # Tokens von Titel und erstem Satz (im Vektor)
    soft: bool = False                  # Kommentar, Analyse, Liveblog, Newsletter (siehe is_soft)
    recurring: bool = False             # Liveblog, Ticker, Newsletter, Sendung (siehe is_recurring)

    @property
    def event(self) -> bool:
        """Meldet ein Ereignis (Brand, Absturz, Beben ...), nicht nur eine Sportart."""
        return any(k.startswith("event:") for k in self.kinds)


def _ts(published_at: str) -> float:
    try:
        dt = datetime.fromisoformat(published_at)
    except (TypeError, ValueError):
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# Floskeln am Anfang extrahierter Volltexte: Bildnachweise, Lesezeit, Datumszeilen, Rubriken.
_BODY_BOILERPLATE = [re.compile(p, re.I) for p in (
    r"\((?:Bild|Foto|Fotos|Image|Photo|Supplied|Unsplash|Getty)[^)]{0,160}\)",
    r"\b(?:Foto|Fotos|Bild|Photo|Image)s?\s*:\s*[^.:\n]{0,80}?/\s*(?:dpa|AFP|Reuters|AP|picture alliance|imago)\w*",
    r"Artikeltyp:\s*\w+", r"\d+-MIN READ\S*(?:\s*Listen)?", r"Prefer Editor'?s note:?", r"Note from the editor:?",
    r"^\s*(?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4}",
    r"©[^.\n]{0,80}", r"\b(?:Lesezeit|Hörzeit)\s*:?\s*\d+\s*Min\.?", r"Artikel anhören", r"Hören Sie diesen Artikel",
    r"^\s*Stand:\s*[\d.]+\s*[\d:]*\s*Uhr",
)]


def clean_body(text: str) -> str:
    """Anfang des Volltexts ohne Bildnachweise und ähnliche Floskeln."""
    text = text[:BODY_CHARS + 200]
    for rx in _BODY_BOILERPLATE:
        text = rx.sub(" ", text)
    return text[:BODY_CHARS]


def _fields(row: dict[str, Any]) -> list[tuple[str, float]]:
    """(Text, Gewicht) der Felder eines Artikels: Titel, Anreißer, bei fehlendem oder sehr kurzem
    Anreißer der bereinigte Anfang des Volltexts."""
    summary = (row.get("summary") or "")[:SUMMARY_CHARS]
    out = [(row.get("title") or "", TITLE_WEIGHT), (summary, 1.0)]
    body = row.get("body_text") or ""
    if body and len(summary.strip(SUMMARY_TRIM)) < BODY_IF_SUMMARY_BELOW:
        out.append((clean_body(body), BODY_WEIGHT))
    return out


def _lang(row: dict[str, Any]) -> str:
    return "de" if row.get("lang") == "de" else "en"


class Model:
    """Vektoren, IDF und invertierter Index eines Laufs; bleibt für related() im Speicher."""

    def __init__(self, rows: list[dict[str, Any]]):
        self.docs: list[Doc] = []
        self.by_id: dict[int, int] = {}
        self.langs: dict[str, str] = {}             # Quelle -> Sprache (setzt refresh)
        self.story_of: dict[int, int | None] = {}   # Ergebnis des Laufs (setzt refresh)
        self.centrality: dict[int, float] = {}       # Mittigkeit je Artikel einer Geschichte (setzt refresh)
        self.retired: set[int] = set()               # nicht fortgesetzte, zu lange Geschichten (setzt assign)
        # Wörtlich gleiche Meldungen mehrerer Quellen (The Local Deutschland/Österreich/..., heise und
        # heise Security) zählen als ein Artikel: Sonst sähen ihre Wörter wie eine große Geschichte aus.
        # Die Kopien bekommen später die Geschichte ihres Originals (copies: Original-ID -> Kopien).
        self.copies: dict[int, list[int]] = defaultdict(list)
        self.original_of: dict[int, int] = {}
        self.copy_sources: dict[int, set[str]] = {}  # Original-ID -> Quellen samt Kopien
        originals: dict[str, dict[str, Any]] = {}
        kept = []
        for r in sorted((r for r in rows if r.get("title")), key=lambda r: int(r["id"])):
            key = copy_key(r)
            orig = originals.get(key) if key else None
            if orig is not None and orig["source_id"] != r["source_id"]:
                self.copies[int(orig["id"])].append(int(r["id"]))
                self.original_of[int(r["id"])] = int(orig["id"])
                self.copy_sources.setdefault(int(orig["id"]), {orig["source_id"]}).add(r["source_id"])
                continue
            if key and orig is None:
                originals[key] = r
            kept.append(r)
        rows = kept
        # Pass 1: Wortschatz je Sprache (für gemeinsame Wörter und Komposita), Großschreibung im
        # Englischen (Namen).
        raw: list[list[tuple[list[str], float]]] = []
        vocab = {"de": Counter(), "en": Counter()}
        caps: dict[str, list[int]] = defaultdict(lambda: [0, 0])     # Wort -> [groß, klein]
        lower_de: set[str] = set()          # deutsche Wörter, die mitten im Satz klein stehen
        leads: list[list[str]] = []         # Wörter des ersten Satzes (Titel + Lead für Arten, de/en-Paare)
        doc_kinds: list[frozenset] = []
        for r in rows:
            lang = _lang(r)
            fields = [(words(text), weight) for text, weight in _fields(r)]
            raw.append(fields)
            lead = words(_first_sentence(r.get("summary") or ""))
            leads.append(lead)
            doc_kinds.append(_kinds(fields[0][0], lead)
                             | _scores(r.get("title") or "", _first_sentence(r.get("summary") or "")))
            seen = {w for ws, _ in fields for w in ws}
            vocab[lang].update(seen)
            for text, _ in _fields(r):
                for w, upper in _case_evidence(text):
                    if lang == "en":
                        caps[w][0 if upper else 1] += 1
                    elif not upper:
                        lower_de.add(w)
        de_words = {w for w in vocab["de"] if w not in _STOP_DE}
        en_words = {w for w in vocab["en"] if w not in _STOP_EN}
        shared = {w for w in de_words & en_words if len(w) >= 4 and not w.isdigit()}
        known = {w for w, n in vocab["de"].items() if n >= 2 and len(w) >= 4 and w not in _STOP_DE}
        known |= {w for w in LEXICON if len(w) >= 4}
        self.tokenizer = Tokenizer(shared, known)
        # Namen: kanonische Tokens englischer Wörter, die mitten im Satz meist groß geschrieben
        # stehen, dazu Namen aus dem Lexikon (siehe linkable).
        self.names = set(NAME_TOKENS)
        for w, (up, low) in caps.items():
            if up >= 2 and up >= NAME_CAPS_SHARE * (up + low):
                self.names.update(t for t, f in self.tokenizer.canon(w, "en") if f == 1.0)
        self.names -= GENERIC                 # „President“, „Pope“, „Conservatives“: Ämter, keine Namen
        # Englische Wörter, die mitten im Satz meist klein stehen: gewöhnliche Wörter (siehe specific).
        self.plain_en = set()
        for w, (up, low) in caps.items():
            if low > up:
                self.plain_en.update(t for t, _ in self.tokenizer.canon(w, "en"))
        self.plain_en -= self.names
        # Deutsche Tokens von Wörtern, die klein geschrieben vorkommen (Verben, Adjektive): nie
        # „besonders“ im Sinn von specific(), auch wenn das Lexikon sie nicht kennt.
        self.plain_de = {t for w in lower_de for t, _ in self.tokenizer.canon(w, "de")} - self.names
        # Pass 2: Tokens je Artikel mit Feldgewicht (je Feld einmal, Felder addiert).
        tfs: list[dict[str, float]] = []
        df: Counter = Counter()
        langs: dict[str, set[str]] = defaultdict(set)
        bigrams: Counter = Counter()        # Artikel, in denen zwei Tokens direkt nacheinander stehen
        titles: list[frozenset] = []        # Tokens des Titels je Artikel
        for r, fields in zip(rows, raw):
            lang = _lang(r)
            tf: dict[str, float] = {}
            adjacent: set[tuple[str, str]] = set()
            for k, (ws, weight) in enumerate(fields):
                field_tokens: dict[str, float] = {}
                prev = None
                for w in ws:
                    canon = self.tokenizer.canon(w, lang)
                    for tok, factor in canon:
                        if factor > field_tokens.get(tok, 0.0):
                            field_tokens[tok] = factor
                    cur = canon[0][0] if len(canon) == 1 and canon[0][1] == 1.0 else None
                    if prev and cur and prev != cur:
                        adjacent.add((prev, cur) if prev < cur else (cur, prev))
                    prev = cur
                for tok, factor in field_tokens.items():
                    tf[tok] = tf.get(tok, 0.0) + weight * factor
                if k == 0:
                    titles.append(frozenset(field_tokens))
            tfs.append(tf)
            df.update(tf.keys())
            bigrams.update(adjacent)
            for tok in tf:
                langs[tok].add(lang)
        self.n = len(rows)
        self.df = df
        n = max(self.n, 1)
        # Wörter, die nur ein Artikel hat, können nichts verbinden: Sie zählen nicht mit (sonst
        # drückten unübersetzte Wörter die Ähnlichkeit deutsch-englischer Paare).
        self.idf = {t: math.log((n + 1) / (d + 0.5)) for t, d in df.items() if d >= 2}
        # Absolute Obergrenzen halten die Kandidatenpaare (Postings²) auch in großen Fenstern klein.
        self.informative_df = min(INFORMATIVE_MAX_DF, max(INFORMATIVE_MIN_DF, int(INFORMATIVE_FRAC * n)))
        self.topic_df = min(TOPIC_MAX_DF, max(INFORMATIVE_MIN_DF, int(TOPIC_FRAC * n)))
        self.bilingual = {t for t, ls in langs.items() if len(ls) > 1}
        # Dach-Namen: Wörter, die bei mindestens UMBRELLA_SPORTS verschiedenen Sportarten stehen
        # (Asienspiele, Austragungsort Aichi-Nagoya): Sie machen kein einzelnes Ereignis fest.
        sports: dict[str, set[str]] = defaultdict(set)
        for tf, kinds in zip(tfs, doc_kinds):
            kinds = {k for k in kinds if k.startswith("sport:")}
            if kinds:
                for tok in tf:
                    sports[tok].update(kinds)
        self.umbrella = {t for t, ks in sports.items() if len(ks) >= UMBRELLA_SPORTS}
        # Wörter, die meist direkt neben einem Namen stehen, sind selbst Namen (Vornamen: „Friedrich“
        # neben merz, auch wenn es sie nur deutsch gibt und die Großschreibung nichts verrät).
        for (x, y), n in sorted((k, n) for k, n in bigrams.items() if n >= ALIAS_MIN_DF):
            for name, other in ((x, y), (y, x)):
                if (name in self.names and other not in self.names and not other.isdigit()
                        and other not in GENERIC and not self.plain(other) and n >= NAME_NEIGHBOUR_SHARE * df[other]):
                    self.names.add(other)
        self.alias = self._aliases(bigrams)
        if self.alias:
            tfs = [self._fold(tf) for tf in tfs]
        for r, tf, title, lead, kinds in zip(rows, tfs, titles, leads, doc_kinds):
            vec = self._vector(tf)
            lang = _lang(r)
            lead_tokens = set(title) | {t for w in lead for t, _ in self.tokenizer.canon(w, lang)}
            doc = Doc(id=int(r["id"]), source=r["source_id"], lang=lang, ts=_ts(r["published_at"]),
                      vec=vec, xvec=self._restrict(vec), story_id=r.get("story_id"), kinds=kinds,
                      title=self._in_vec(title, vec), lead=self._in_vec(lead_tokens, vec), soft=is_soft(r),
                      recurring=is_recurring(r))
            self.by_id[doc.id] = len(self.docs)
            self.docs.append(doc)
        # Invertierter Index über Wörter, die für Themen informativ sind (enthält die für Geschichten).
        self.postings: dict[str, list[int]] = defaultdict(list)
        for i, doc in enumerate(self.docs):
            for tok in doc.vec:
                if df[tok] <= self.topic_df:
                    self.postings[tok].append(i)

    def index_of(self, article_id: int) -> int | None:
        """Index in docs; eine wörtliche Kopie (siehe copies) zählt als ihr Original."""
        return self.by_id.get(self.original_of.get(article_id, article_id))

    def _vector(self, tf: dict[str, float], unseen: bool = False) -> dict[str, float]:
        """TF-IDF, normiert. Mit `unseen` (Artikel außerhalb des Fensters) zählen Wörter, die das
        Fenster nicht oder nur einmal kennt, wie seltene Wörter statt wegzufallen."""
        if unseen:
            rare = math.log((max(self.n, 1) + 1) / 1.5)
            vec = {t: w * self.idf.get(t, rare) for t, w in tf.items()}
        else:
            vec = {t: w * self.idf[t] for t, w in tf.items() if t in self.idf}
        norm = math.sqrt(sum(v * v for v in vec.values()))
        return {t: v / norm for t, v in vec.items()} if norm else {}

    def _aliases(self, bigrams: Counter) -> dict[str, str]:
        """Wörter, die im Fenster fast immer direkt nebeneinander stehen (Vor- und Nachname: „Jürgen
        Klopp“, „Tigst Assefa“), zählen als eines: Sonst verbände ein Name zwei Artikel doppelt so
        stark. Liefert Wort -> Stellvertreter (das häufigere, bei Gleichstand das alphabetisch erste)."""
        df = self.df
        pairs = {k: n for k, n in bigrams.items() if n >= ALIAS_MIN_DF}
        alias: dict[str, str] = {}
        targets: set[str] = set()
        fixed = NAME_TOKENS | GENERIC       # Lexikon-Namen und Ämter („President“) bleiben eigenständig
        # Feste Reihenfolge (häufigste Paare zuerst): Die Paare kommen aus Mengen, deren Reihenfolge
        # von der Hash-Saat abhängt. Jedes Wort geht höchstens einmal und direkt in einem anderen auf
        # (keine Ketten: „Vice President“ und „Delcy Rodríguez“ machen aus „President“ kein „Rodríguez“).
        for (a, b), n in sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0])):
            keep, drop = sorted((a, b), key=lambda t: (-df[t], t))
            # Das seltenere Wort (Vorname) geht im häufigeren auf, wenn es meist daneben steht:
            # „Donald“ -> trump, „Harry“ -> kane.
            # Das seltenere Wort darf in einem Lexikon-Namen aufgehen („Friedrich“ -> merz), selbst aber
            # kein Lexikon-Name, kein Amt und kein gewöhnliches Wort sein („Strait“ bleibt, „Vice“ auch).
            if (n < ALIAS_SHARE * df[drop] or drop in fixed or keep in GENERIC or drop.isdigit() or keep.isdigit()
                    or self.plain(drop)):
                continue
            if drop in alias or drop in targets:
                continue
            keep = alias.get(keep, keep)
            if keep == drop:
                continue
            alias[drop] = keep
            targets.add(keep)
        return alias

    def _in_vec(self, tokens: Iterable[str], vec: dict[str, float]) -> frozenset:
        return frozenset(t for t in (self.alias.get(t, t) for t in tokens) if t in vec)

    def _fold(self, tf: dict[str, float]) -> dict[str, float]:
        """Gewichte zusammengehöriger Wörter (siehe _aliases) auf den Stellvertreter, das stärkere zählt."""
        out: dict[str, float] = {}
        for t, w in tf.items():
            t = self.alias.get(t, t)
            if w > out.get(t, 0.0):
                out[t] = w
        return out

    def _restrict(self, vec: dict[str, float]) -> dict[str, float]:
        """Vektor nur über Wörter beider Sprachen, neu normiert (höchstens bis XNORM_FLOOR hoch, damit
        zwei zufällig gemeinsame Namen einen sonst unübersetzten Artikel nicht allein tragen)."""
        part = {t: v for t, v in vec.items() if t in self.bilingual}
        norm = math.sqrt(sum(v * v for v in part.values()))
        scale = 1.0 / max(norm, XNORM_FLOOR) if part else 0.0
        return {t: v * scale for t, v in part.items()}

    def vector_for(self, row: dict[str, Any]) -> dict[str, float]:
        """Vektor eines Artikels außerhalb des Fensters (IDF des Fensters, unbekannte Wörter fallen weg)."""
        lang = _lang(row)
        tf: dict[str, float] = {}
        for text, weight in _fields(row):
            field_tokens: dict[str, float] = {}
            for tok, factor in self.tokenizer.tokens(text, lang):
                if factor > field_tokens.get(tok, 0.0):
                    field_tokens[tok] = factor
            for tok, factor in field_tokens.items():
                tf[tok] = tf.get(tok, 0.0) + weight * factor
        return self._vector(self._fold(tf), unseen=True)

    def sim(self, a: Doc, b: Doc) -> float:
        """Ähnlichkeit für das Clustering: Kosinus, das stärkste gemeinsame Wort nur mit TOP_FACTOR."""
        s, rest = self.sim_detail(a, b)
        return rest + (s - rest) * TOP_FACTOR

    def sim_detail(self, a: Doc, b: Doc) -> tuple[float, float]:
        """(Kosinus, Kosinus ohne das stärkste gemeinsame Wort). Der zweite Wert fängt Paare ab,
        die nur ein Name verbindet (zwei verschiedene Geschichten über Klopp oder Trump)."""
        if a.lang == b.lang:
            va, vb = a.vec, b.vec
        else:
            va, vb = a.xvec, b.xvec
        if len(va) > len(vb):
            va, vb = vb, va
        s = top = 0.0
        for t, w in va.items():
            x = vb.get(t)
            if x is not None:
                c = w * x
                s += c
                if c > top:
                    top = c
        return s, s - top

    def plain(self, t: str) -> bool:
        """Ein gewöhnliches Wort: allgemeines Nachrichtenwort, Zahl, ein Wort, das im Fenster klein
        geschrieben vorkommt (Verb, Adjektiv, englisches Substantiv), ein häufiges deutsches Substantiv
        oder ein Kompositum aus gewöhnlichen Wörtern (Großbrand, Kleinflugzeug, Lagerhalle)."""
        if (t in GENERIC or t.isdigit() or t in self.plain_de or t in self.plain_en or t in _PLAIN_DE):
            return True
        parts = self.tokenizer.compounds.get(t)
        return bool(parts) and all(self.plain(p) for p in parts if p != t)

    def is_name(self, t: str) -> bool:
        """Name im engeren Sinn: im Englischen groß geschrieben oder im Lexikon, oder ein Wort, das es im
        Fenster in beiden Sprachen gibt (meist Namen); kein Dach-Name, keine Zahl."""
        return ((t in self.names or t in self.bilingual) and not t.isdigit() and t not in self.umbrella
                and not self.plain(t))

    def specific(self, t: str) -> bool:
        """Ein Wort, das ein Ereignis festmacht: Name, Ort oder Kompositum, nicht in einem großen Teil
        des Fensters, kein gewöhnliches Wort (plain), kein Dach-Name."""
        return (self.df[t] <= self.informative_df and len(t) >= 3 and not self.plain(t)
                and t not in self.umbrella)

    def linkable(self, a: Doc, b: Doc) -> bool:
        """Dürfen zwei Artikel überhaupt zu einer Geschichte gehören? Beide müssen dasselbe Ereignis
        festmachen können (alle Wörter nicht in einem großen Teil des Fensters):

        - Nennen beide eine Ereignisart (Erdbeben, Streik, Lawine, Basketball, Bundesliga ...), muss
          eine davon gemeinsam sein, und sie müssen einen Namen oder Ort teilen: zwei Großbrände in
          Berlin und Hamburg, zwei Beben, Basketball- und Fechtgold sind verschiedene Geschichten.
        - Deutsche Paare: zwei gemeinsame besondere Wörter (specific: Namen, Orte, Komposita) oder ein
          Name plus zwei gewöhnliche informative Wörter (plain).
        - Ist ein englischer Artikel beteiligt: ein erkennbarer Name (Großschreibung im Englischen,
          Lexikon), ein besonderes und ein gewöhnliches Wort und genug Inhalt neben den Namen
          (content_sim): Drei Autounfälle an drei Orten teilen nur „crash“ und „dies“; die Tötung eines
          Hamas-Kommandeurs und eine Rede eines Hamas-Führers nur Hamas, Israel, Gaza.
        - Deutsch/englisch zusätzlich: zwei gemeinsame Inhaltswörter (keine Namen) in Titel und erstem
          Satz, davon eines im Titel oder zwei besondere Nicht-Namen, oder sehr hohe Ähnlichkeit
          (CROSS_SIM_MIN): Xi, Washington, Besuch und „einigen sich“ machen aus dem KI-Kanal und den
          Pandas keine Geschichte."""
        if kinds_conflict(a.kinds, b.kinds):
            return False
        special, plain, named, place = self._shared(a, b)
        if a.event and b.event and not place:
            return False
        english = a.lang == "en" or b.lang == "en"
        if a.lang == b.lang and self.sim(a, b) >= SAME_TEXT_MIN and self._any_name(a, b):
            # Fast derselbe Text, nur mit häufigen Namen („5 arrested near UK air base used by US on
            # suspicion of preparing terrorist act“): dieselbe Meldung, auch ohne seltenes Wort.
            return True
        if (a.lang == b.lang and named and special >= 1 and self.sim(a, b) >= STRONG_SIM
                and sum(1 for t in a.vec if t in b.vec and self.df[t] <= self.topic_df and self.plain(t))
                >= STRONG_PLAIN):
            # Sehr ähnlich, ein seltener Name („Fairford“) und viele gemeinsame gewöhnliche Wörter, die im
            # kleinen Fenster nur knapp nicht mehr als selten gelten (Festnahme, Terror, Stützpunkt).
            return True
        if english:
            ok = (named and special >= 1 and plain >= 1 and special + plain >= 2 + (special < 2)
                  and self.content_sim(a, b) >= CONTENT_MIN)
            return ok and (a.lang == b.lang or self.cross_ok(a, b))
        if not (special >= 2 or (named and plain >= 2)):
            return False
        # Namen allein reichen nicht (Vor- und Nachname, Verein und Stadt: „Bayern“, „Musiala“ im
        # Spielbericht und in der Vertragsmeldung): mindestens ein gemeinsames seltenes Wort, das kein
        # Name ist, oder zwei gemeinsame Titelwörter, die keine Namen sind.
        # Nur gewöhnliche Wörter zählen dafür sicher als Nicht-Name: Deutsche Namen, die es im Fenster
        # nur deutsch gibt („Jamal Musiala“), sehen aus wie seltene Substantive.
        # Ein gewöhnliches Wort neben zwei Namen reicht nicht („Jemen“, „Huthi“, „Gewalt“: Flucht nach
        # Ostafrika und ein saudischer Luftangriff); zwei, oder eines neben einem besonderen Nicht-Namen.
        if plain >= 2 or len({t for t in a.title & b.title if self._content(t)}) >= 2:
            return True
        # Das besondere Wort muss ein zerlegtes Kompositum sein (Pflegedeckel, Zwangsräumung): Ein Wort,
        # das es nur deutsch gibt und das sich nicht zerlegen lässt, ist oft ein Name („Friedrich Merz“).
        return plain >= 1 and any(self.df[t] <= self.informative_df and self._content(t) and self.specific(t)
                                  and t in self.tokenizer.compounds for t in a.vec if t in b.vec)

    def _any_name(self, a: Doc, b: Doc) -> bool:
        """Irgendein gemeinsamer Name oder Ort, auch ein häufiger (Großbritannien, USA)."""
        va, vb = (a.vec, b.vec) if len(a.vec) <= len(b.vec) else (b.vec, a.vec)
        return any(t in vb and (t in self.names or t in NAME_TOKENS) and not t.isdigit() for t in va)

    def _shared(self, a: Doc, b: Doc) -> tuple[int, int, bool, bool]:
        """Gemeinsame Wörter zweier Artikel: (besondere, gewöhnliche, Name?, Ort/Name, der das
        Ereignis festmacht?)."""
        va, vb = (a.vec, b.vec) if len(a.vec) <= len(b.vec) else (b.vec, a.vec)
        english = a.lang == "en" or b.lang == "en"
        special = plain = 0
        named = place = False
        for t in va:
            if t not in vb:
                continue
            d = self.df[t]
            if d <= self.topic_df and self.is_name(t):
                named = True
                place = place or d <= self.informative_df
            if d <= self.informative_df:
                if self.plain(t) or t in self.umbrella:
                    plain += 1
                elif len(t) >= 3:
                    special += 1
                    place = place or not english
        return special, plain, named, place

    def events_apart(self, a: Doc, b: Doc) -> bool:
        """Zwei Ereignismeldungen (Brand, Absturz, Beben ...) ohne gemeinsamen Ort oder Namen, oder
        mit verschiedenen Ereignisarten: nie eine Geschichte (auch nicht über andere Mitglieder)."""
        if kinds_conflict(a.kinds, b.kinds):
            return True
        return a.event and b.event and not self._shared(a, b)[3]

    def cross_ok(self, a: Doc, b: Doc) -> bool:
        """Deutsch/englisch: gemeinsamer Inhalt, nicht nur Namen (siehe linkable)."""
        if self.sim(a, b) >= CROSS_SIM_MIN:
            return True
        lead = {t for t in a.lead & b.lead if self._content(t)}
        title = {t for t in a.title & b.title if self._content(t)}
        spec = sum(1 for t in a.vec if t in b.vec and self.specific(t) and self._content(t))
        return len(lead) >= 2 and (bool(title) or spec >= 2)

    def _content(self, t: str) -> bool:
        """Inhaltswort: kein Name, kein Land, keine Zahl, nicht in einem großen Teil des Fensters."""
        return (t not in self.names and t not in NAME_TOKENS and not self.is_name(t) and not t.isdigit()
                and t not in self.umbrella and self.df[t] <= self.topic_df)

    def content_sim(self, a: Doc, b: Doc) -> float:
        """Kosinus-Anteil der gemeinsamen Wörter, die keine Namen sind: Zwei Artikel über Anthropic
        (Gerichtsurteil zum Pentagon, Wasserzeichen für KI-Texte) teilen fast nur „Anthropic“ und
        „Claude“; derselbe Vorgang teilt auch, was passiert ist (Gericht, Festnahme, Rückruf)."""
        va, vb = (a.vec, b.vec) if a.lang == b.lang else (a.xvec, b.xvec)
        if len(va) > len(vb):
            va, vb = vb, va
        return sum(w * vb[t] for t, w in va.items() if t in vb and t not in self.names)

    def named(self, a: Doc, b: Doc) -> bool:
        """Lockerer als linkable, für verwandte Themen: Ist ein englischer Artikel beteiligt, ein
        gemeinsamer Name (Pflegereform und britische „social care reform“ teilen nur Wörter)."""
        if a.lang != "en" and b.lang != "en":
            return True
        va, vb = (a.vec, b.vec) if len(a.vec) <= len(b.vec) else (b.vec, a.vec)
        return any(t in vb and not t.isdigit() and self.df[t] <= self.topic_df
                   and (t in self.names or (self.df[t] == 0 and t not in GENERIC)) for t in va)

    def shared_informative(self, a: dict[str, float], b: dict[str, float], limit: int) -> int:
        va, vb = (a, b) if len(a) <= len(b) else (b, a)
        return sum(1 for t in va if t in vb and self.df[t] <= limit)


# ---- Clustering ----------------------------------------------------------------------

def _candidate_pairs(model: Model) -> dict[tuple[int, int], int]:
    """Paare (i, j) mit i < j, die mindestens ein informatives Wort teilen und höchstens MAX_GAP
    auseinanderliegen, mit der Zahl gemeinsamer informativer Wörter."""
    gap = MAX_GAP.total_seconds()
    docs = model.docs
    counts: dict[tuple[int, int], int] = defaultdict(int)
    for tok, posting in model.postings.items():
        if model.df[tok] > model.informative_df or len(posting) < 2:
            continue
        ordered = sorted(posting, key=lambda i: docs[i].ts)
        for x, i in enumerate(ordered):
            ti = docs[i].ts
            for j in ordered[x + 1:]:
                if docs[j].ts - ti > gap:
                    break
                counts[(i, j) if i < j else (j, i)] += 1
    return counts


def _short_pairs(model: Model, known: dict[tuple[int, int], int]) -> set[tuple[int, int]]:
    """Paare mit einem kurzen Artikel (nur Titel, SHORT_TOKENS Wörter oder weniger), die ein
    Themenwort teilen, aber kein informatives: ORF „Trump rechnet mit neuen Gesprächen mit dem Iran“
    und „Trump expects US to hold talks with Iran this week“ teilen nur häufige Wörter. Solche Paare
    zählen nur bei sehr hoher Ähnlichkeit (SHORT_SIM_MIN)."""
    gap = MAX_GAP.total_seconds()
    docs = model.docs
    out: set[tuple[int, int]] = set()
    for i, d in enumerate(docs):
        if len(d.vec) > SHORT_TOKENS:
            continue
        for tok in d.vec:
            if model.df[tok] <= model.informative_df:
                continue
            for j in model.postings.get(tok, ()):
                if j != i and abs(docs[j].ts - d.ts) <= gap:
                    key = (i, j) if i < j else (j, i)
                    if key not in known:
                        out.add(key)
    return out


def cluster(model: Model) -> list[list[int]]:
    """Gruppen (Listen von Indizes in model.docs) aus mindestens zwei Zeitungen (Verlagen, siehe
    publisher; Kopien zählen mit), jede Gruppe nach ID sortiert.

    Erster Durchgang: Average-Link über die Kanten, stärkste zuerst. Zwei Gruppen verschmelzen nur,
    wenn die ganze Gruppe in MAX_GAP passt, die mittlere Ähnlichkeit aller Paare AVG_MIN erreicht und
    mindestens COHERENCE_SHARE der Paare COHERENCE_FLOOR (kein Klumpen, der nur an wenigen Paaren
    hängt). Zweiter Durchgang: Gruppen und Einzelartikel, die eine Kante unter EDGE_MIN verbindet,
    verschmelzen, wenn ihre Schwerpunkte sich sehr ähnlich sind (CENTROID_MIN) und die Paare
    dieselben Regeln erfüllen (gespaltene Geschichten, z. B. deutsche und englische Hälfte)."""
    docs = model.docs
    edges: list[tuple[float, int, int]] = []
    weak: list[tuple[float, int, int]] = []     # verbindbar, aber unter EDGE_MIN (zweiter Durchgang)
    sims: dict[tuple[int, int], float] = {}
    linked: set[tuple[int, int]] = set()        # Paare, die linkable bestanden haben
    strong: set[tuple[int, int]] = set()        # ... und als Kante zählen (erster Durchgang)
    candidates = _candidate_pairs(model)
    for (i, j), shared in candidates.items():
        # Liveblogs, Ticker, Newsletter, Sendungen streifen mehrere Geschichten: nie ein Mitglied.
        if shared < MIN_SHARED or docs[i].recurring or docs[j].recurring:
            continue
        if not model.linkable(docs[i], docs[j]):
            continue
        s = model.sim(docs[i], docs[j])
        sims[(i, j)] = s
        linked.add((i, j))
        need = SAME_SOURCE_MIN if docs[i].source == docs[j].source else EDGE_MIN
        if docs[i].lang == docs[j].lang and len(docs[i].title & docs[j].title) < 2:
            need = max(need, TITLE_APART_MIN)   # Titel teilen höchstens ein Wort (meist nur den Namen)
        if docs[i].soft or docs[j].soft:
            need = max(need, SOFT_SIM_MIN)      # Kommentar, Analyse: nur sehr nah an der Meldung
        if s >= need:
            edges.append((s, i, j))
            strong.add((i, j))
        elif s >= WEAK_EDGE_MIN and docs[i].source != docs[j].source and not (docs[i].soft or docs[j].soft):
            weak.append((s, i, j))
    for i, j in _short_pairs(model, candidates):
        if docs[i].source == docs[j].source or docs[i].recurring or docs[j].recurring:
            continue
        if not model.named(docs[i], docs[j]):
            continue
        if kinds_conflict(docs[i].kinds, docs[j].kinds):
            continue
        s = model.sim(docs[i], docs[j])
        sims[(i, j)] = s
        if s >= max(SHORT_SIM_MIN, SOFT_SIM_MIN if docs[i].soft or docs[j].soft else 0.0):
            linked.add((i, j))
            strong.add((i, j))
            edges.append((s, i, j))
    # Stärkste Kanten zuerst; bei Gleichstand deterministisch nach Index.
    edges.sort(key=lambda e: (-e[0], e[1], e[2]))
    weak.sort(key=lambda e: (-e[0], e[1], e[2]))
    gap = MAX_GAP.total_seconds()
    root = list(range(len(docs)))
    members: dict[int, list[int]] = {i: [i] for i in range(len(docs))}
    span: dict[int, tuple[float, float]] = {i: (d.ts, d.ts) for i, d in enumerate(docs)}
    version = [0] * len(docs)                    # je Gruppe: wie oft sie gewachsen ist
    kinded: dict[int, list[int]] = {i: [i] for i, d in enumerate(docs) if d.kinds}  # Mitglieder mit Ereignisart
    failed: set[tuple[int, ...]] = set()   # abgelehnte Verschmelzungen je Test (bis sich eine Gruppe ändert)

    def find(i: int) -> int:
        while root[i] != i:
            root[i] = root[root[i]]
            i = root[i]
        return i

    def pair_sim(i: int, j: int) -> float:
        key = (i, j) if i < j else (j, i)
        s = sims.get(key)
        if s is None:
            s = model.sim(docs[i], docs[j])
            sims[key] = s
        return s

    def firm(group: list[int]) -> list[int]:
        """Mitglieder, die für den Zusammenhalt zählen: Kommentare und Analysen tragen keine anderen
        Artikel (kein Knoten, über den sich Fremdes anhängt), außer die Gruppe hat nur solche."""
        out = [x for x in group if not docs[x].soft]
        return out or group

    def coherent(a: list[int], b: list[int], avg_min: float) -> bool:
        a, b = firm(a), firm(b)
        vals = [pair_sim(x, y) for x in a for y in b]
        good = sum(1 for v in vals if v >= COHERENCE_FLOOR)
        return sum(vals) / len(vals) >= avg_min and good >= COHERENCE_SHARE * len(vals)

    def merge(ri: int, rj: int, lo: float, hi: float) -> None:
        a, b = members[ri], members[rj]
        keep, drop = (ri, rj) if len(a) >= len(b) else (rj, ri)
        root[drop] = keep
        members[keep] = a + b if keep == ri else b + a
        del members[drop]
        span[keep] = (lo, hi)
        del span[drop]
        version[keep] += 1
        if drop in kinded:
            kinded.setdefault(keep, []).extend(kinded.pop(drop))

    def apart(ri: int, rj: int) -> bool:
        """Ereignisarten je Mitglied, nicht als Vereinigung der Gruppe (siehe Model.events_apart)."""
        return any(model.events_apart(docs[x], docs[y])
                   for x in kinded.get(ri, ()) for y in kinded.get(rj, ()))

    def attempt(i: int, j: int, test, tag: int = 0) -> bool:
        ri, rj = find(i), find(j)
        if ri == rj:
            return False
        # Ein Kommentar hängt sich an, trägt aber nichts Fremdes: Kante nur, solange er allein ist.
        if (docs[i].soft and len(members[ri]) > 1) or (docs[j].soft and len(members[rj]) > 1):
            return False
        key = (tag, ri, version[ri], rj, version[rj]) if ri < rj else (tag, rj, version[rj], ri, version[ri])
        if key in failed:
            return False
        lo = min(span[ri][0], span[rj][0])
        hi = max(span[ri][1], span[rj][1])
        if hi - lo > gap or apart(ri, rj) or not test(members[ri], members[rj]):
            failed.add(key)
            return False
        merge(ri, rj, lo, hi)
        return True

    for _, i, j in edges:
        attempt(i, j, lambda a, b: (len(a) == 1 and len(b) == 1) or coherent(a, b, AVG_MIN))

    def centroid_close(a: list[int], b: list[int]) -> bool:
        if len(a) == 1 and len(b) == 1:
            return False                            # zwei Einzelartikel: nur über eine echte Kante
        cross = {docs[x].lang for x in a} != {docs[y].lang for y in b} or len({docs[x].lang for x in a + b}) > 1
        ca, cb = _centroid(docs, a, cross), _centroid(docs, b, cross)
        if len(ca) > len(cb):
            ca, cb = cb, ca
        return (sum(v * cb.get(t, 0.0) for t, v in ca.items()) >= CENTROID_MIN
                and coherent(a, b, AVG_MIN))

    for _, i, j in weak:
        attempt(i, j, centroid_close)

    # Dritter Durchgang: Nachzügler. Eine Kante wird im ersten Durchgang nur einmal versucht; war die
    # Gruppe am anderen Ende damals noch klein (oder ein anderer Einzelartikel), bleibt ein später
    # passender Artikel sonst allein. Ein Einzelartikel kommt zu einer Gruppe, wenn er ihrem
    # Schwerpunkt nahe ist (CENTROID_MIN) oder LATE_EDGES Kanten des ersten Durchgangs zu ihr hat und
    # der Zusammenhalt stimmt (alle Regeln wie oben).
    near: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for (i, j) in linked:
        s = sims[(i, j)]
        if s >= WEAK_EDGE_MIN:
            near[i].append((s, j))
            near[j].append((s, i))

    def late(a: list[int], b: list[int]) -> bool:
        single, group = (a, b) if len(a) == 1 else (b, a)
        x = single[0]
        if docs[x].soft and max(pair_sim(x, y) for y in group) < SOFT_SIM_MIN:
            return False
        n_strong = sum(1 for y in group if ((x, y) if x < y else (y, x)) in strong)
        cross = len({docs[y].lang for y in a + b}) > 1
        cg = _centroid(docs, firm(group), cross)
        cx = docs[x].xvec if cross else docs[x].vec
        close = sum(v * cg.get(t, 0.0) for t, v in cx.items()) >= CENTROID_MIN
        return (close or n_strong >= LATE_EDGES) and coherent(single, group, AVG_MIN)

    for x in sorted(near, key=lambda x: docs[x].id):
        if len(members[find(x)]) != 1:
            continue
        for _, y in sorted(near[x], key=lambda e: (-e[0], e[1])):
            if len(members[find(y)]) > 1 and attempt(x, y, late, tag=1):
                break
    # Zum Schluss: Mitglieder, die nur an wenigen anderen hängen (früh über ein Paar gekommen, zu dem
    # Rest der gewachsenen Geschichte kaum ähnlich), fallen heraus: Treffen des türkischen
    # Außenministers am Rand der UN-Vollversammlung in der Geschichte über Wadephul und Lawrow.
    groups = []
    for group in members.values():
        if len(group) >= PRUNE_MIN_SIZE:
            keep = []
            for x in group:
                vals = [pair_sim(x, y) for y in group if y != x]
                if sum(1 for v in vals if v >= COHERENCE_FLOOR) >= PRUNE_SHARE * len(vals):
                    keep.append(x)
            groups.append(keep)
            groups.extend([x] for x in group if x not in keep)
        else:
            groups.append(group)
    out = []
    for group in groups:
        papers = {publisher(docs[i].source) for i in group}
        for i in group:
            papers.update(publisher(sid) for sid in model.copy_sources.get(docs[i].id, ()))
        if len(papers) >= 2:
            out.append(sorted(group, key=lambda i: docs[i].id))
    out.sort(key=lambda g: docs[g[0]].id)
    return out


def _centroid(docs: list[Doc], group: list[int], cross: bool) -> dict[str, float]:
    """Normierter Schwerpunkt der Vektoren einer Gruppe (bei gemischten Sprachen der Teilvektoren
    über Wörter beider Sprachen)."""
    out: dict[str, float] = defaultdict(float)
    for i in group:
        for t, v in (docs[i].xvec if cross else docs[i].vec).items():
            out[t] += v
    norm = math.sqrt(sum(v * v for v in out.values()))
    return {t: v / norm for t, v in out.items()} if norm else {}


def _with_copies(model: Model, result: dict[int, int | None]) -> dict[int, int | None]:
    """Wörtliche Kopien bekommen die Geschichte ihres Originals (siehe Model.copies)."""
    for original, copies in model.copies.items():
        for cid in copies:
            result[cid] = result.get(original)
    return result


def assign(model: Model, reserved: set[int] | frozenset = frozenset(),
           outside_oldest: dict[int, float] | None = None) -> dict[int, int | None]:
    """Neue story_id je Artikel des Fensters (None: keine Geschichte). Gruppen in fester Reihenfolge
    (kleinste Artikel-ID zuerst), jede ID an höchstens eine Gruppe:

    1. Trugen Mitglieder eine story_id, deren Artikel nicht mehr im Fenster liegt (eine ältere
       Geschichte läuft weiter), nimmt die Gruppe die kleinste solche ID: Die Geschichte zerfällt nicht,
       wenn ihr erster Artikel herauswandert.
    2. Sonst eine bisherige ID, deren Artikel im Fenster alle in dieser Gruppe gelandet sind (die
       Geschichte hat nur ihren ersten Artikel verloren oder ist gewachsen); bei mehreren die mit den
       meisten Mitgliedern, dann die kleinste. So bleibt eine offene Geschichte dieselbe.
    3. Sonst die kleinste Artikel-ID der Gruppe. `reserved`: IDs, die Artikel außerhalb des Fensters
       noch als story_id tragen; so eine ID nimmt eine Gruppe nur, wenn eines ihrer Mitglieder sie schon
       trug (sonst hinge sie sich an eine alte, andere Geschichte). Ist jede Mitglieds-ID so belegt,
       bleibt die Gruppe diesmal ohne Geschichte.

    `outside_oldest`: bisherige story_id -> Zeit (Unix) ihres ältesten Artikels vor dem Fenster. Eine
    Gruppe setzt so eine Geschichte (Schritte 1 und 2) nur fort, wenn ihr neuester Artikel höchstens
    MAX_GAP danach erschien: Eine Geschichte umfasst nie mehr als MAX_GAP, sonst kettete sich Tag für Tag
    ein neues Ereignis an eine alte ID (Preisdeckel von Eni am 25.9., von SOCAR am 27.9.). Die so nicht
    fortgesetzten IDs stehen danach in model.retired (die älteren Artikel behalten sie)."""
    docs = model.docs
    result: dict[int, int | None] = {d.id: None for d in docs}
    window = set(model.by_id) | {c for cs in model.copies.values() for c in cs}
    groups = [[docs[i] for i in g] for g in cluster(model)]
    holders: Counter = Counter()            # bisherige story_id -> Mitglieder im Fenster (samt Kopien)
    for d in docs:
        n = 1 + len(model.copies.get(d.id, ()))
        if d.story_id is not None:
            holders[d.story_id] += n
    own: dict[int, dict[int, int]] = {}     # bisherige ID -> {Gruppe: Mitglieder}
    for k, g in enumerate(groups):
        for d in g:
            if d.story_id is not None:
                own.setdefault(d.story_id, {}).setdefault(k, 0)
                own[d.story_id][k] += 1 + len(model.copies.get(d.id, ()))
    gap = MAX_GAP.total_seconds()
    oldest = outside_oldest or {}
    model.retired = set()

    def continues(sid: int, g: list[Doc]) -> bool:
        if sid not in oldest or max(d.ts for d in g) - oldest[sid] <= gap:
            return True
        model.retired.add(sid)
        return False

    taken: set[int] = set()
    chosen: dict[int, int] = {}
    for k, g in enumerate(groups):          # 1. ältere Geschichte läuft weiter
        outside = sorted({d.story_id for d in g if d.story_id is not None and d.story_id not in window
                          and d.story_id not in taken and continues(d.story_id, g)})
        if outside:
            chosen[k] = outside[0]
            taken.add(outside[0])
    members_ids = [{d.id for d in g} | {c for d in g for c in model.copies.get(d.id, ())} for g in groups]
    for k, g in enumerate(groups):          # 2. bisherige ID, deren Artikel alle hier gelandet sind
        if k in chosen:
            continue
        olds = sorted({d.story_id for d in g if d.story_id is not None and d.story_id in window},
                      key=lambda sid: (-holders[sid], sid))
        for sid in olds:
            if sid in taken or own[sid].get(k, 0) != holders[sid] or not continues(sid, g):
                continue
            # Die ID ist eine Artikel-ID: Steht dieser Artikel in einer anderen Gruppe, gehört sie dorthin.
            if sid in window and sid not in members_ids[k] and any(sid in m for m in members_ids):
                continue
            chosen[k] = sid
            taken.add(sid)
            break
    for k, g in enumerate(groups):          # 3. kleinste freie Artikel-ID
        if k in chosen:
            continue
        had = {d.story_id for d in g}
        free = sorted(i for i in members_ids[k] if i not in taken and i not in model.retired
                      and (i not in reserved or i in had))
        if free:
            chosen[k] = free[0]
            taken.add(free[0])
    for k, g in enumerate(groups):
        for d in g:
            result[d.id] = chosen.get(k)
    return _with_copies(model, result)


def centrality(model: Model, result: dict[int, int | None]) -> dict[int, float]:
    """Wie mittig ein Artikel in seiner Geschichte steht: Ähnlichkeit zum Schwerpunkt der Gruppe
    (ohne Kommentare, siehe cluster; bei gemischten Sprachen über Wörter beider Sprachen). Der
    Vertreter einer Geschichte soll sie erzählen, nicht einen Nebenaspekt. Kopien wie ihr Original."""
    groups: dict[int, list[int]] = defaultdict(list)
    for aid, sid in result.items():
        k = model.by_id.get(aid)
        if sid is not None and k is not None:
            groups[sid].append(k)
    out: dict[int, float] = {}
    for group in groups.values():
        docs = [model.docs[i] for i in group]
        cross = len({d.lang for d in docs}) > 1
        firm = [i for i, d in zip(group, docs) if not d.soft] or group
        c = _centroid(model.docs, firm, cross)
        for d in docs:
            v = d.xvec if cross else d.vec
            out[d.id] = round(sum(w * c.get(t, 0.0) for t, w in v.items()), 4)
            for cid in model.copies.get(d.id, ()):
                out[cid] = out[d.id]
    return out


def cluster_rows(rows: list[dict[str, Any]]) -> dict[int, int | None]:
    """Gruppiert Zeilen (id, source_id, lang, title, summary, body_text, published_at, story_id)
    und liefert story_id je Artikel. Für Tests und Auswertungen ohne Datenbank."""
    return assign(Model(rows))


# ---- Lauf mit Datenbank -----------------------------------------------------------------

# Modell des letzten Laufs (für related); None bis zum ersten Lauf.
_model: Model | None = None
_busy = False
_dirty = False          # während eines Laufs angefordert: gleich danach noch einmal


def current_model() -> Model | None:
    return _model


def _compute(rows: list[dict[str, Any]], reserved: set[int],
             outside_oldest: dict[int, float] | None = None) -> tuple[Model, dict[int, int | None]]:
    """Reine Rechenarbeit eines Laufs (läuft in einem Thread, fasst die Datenbank nicht an)."""
    model = Model(rows)
    result = assign(model, reserved, outside_oldest)
    model.centrality = centrality(model, result)
    return model, result


def central_scores(article_ids: list[int]) -> dict[int, float]:
    """Mittigkeit (siehe centrality) der Artikel aus dem letzten Lauf; fehlende fehlen."""
    model = _model
    if model is None:
        return {}
    return {i: model.centrality[i] for i in article_ids if i in model.centrality}


def shown_with(article_id: int, other_id: int) -> bool:
    """Darf `other` neben dem offenen Artikel als „dieselbe Geschichte“ stehen? Gleiche Sprache immer;
    deutsch/englisch nur mit zwei gemeinsamen Inhaltswörtern (keine Namen) in Titel und erstem Satz
    oder sehr hoher Ähnlichkeit (CROSS_SIM_MIN): Die Geschichte hängt über andere Mitglieder zusammen,
    das Paar selbst muss aber erkennbar dasselbe erzählen. Ohne Modell oder außerhalb: ja."""
    model = _model
    if model is None:
        return True
    a, b = model.index_of(article_id), model.index_of(other_id)
    if a is None or b is None:
        return True
    da, db = model.docs[a], model.docs[b]
    if da.lang == db.lang or a == b:
        return True
    if model.sim(da, db) >= CROSS_SIM_MIN:
        return True
    return len({t for t in da.lead & db.lead if model._content(t)}) >= 2


async def refresh(db: Any, catalog: Any, now: datetime | None = None) -> dict[str, Any] | None:
    """Gruppiert die Artikel der letzten WINDOW Stunden neu und schreibt geänderte story_id in einer
    Transaktion. Lesen und Schreiben auf der Event-Loop (kurz), das Rechnen in einem Thread. Läuft
    schon ein Lauf, passiert nichts (None). Gibt Kennzahlen zurück; Fehler gehen an den Aufrufer."""
    global _busy, _dirty
    if _busy:
        # Nicht verloren: Der laufende Lauf rechnet gleich danach noch einmal (die neuen Artikel kamen
        # womöglich nach seinem Lesen).
        _dirty = True
        log.info("Geschichten: Lauf übersprungen, der vorige läuft noch (folgt gleich)")
        return None
    _busy = True
    try:
        stats = await _refresh(db, catalog, now)
        while _dirty:
            _dirty = False
            stats = await _refresh(db, catalog, None)
        return stats
    finally:
        _busy = False
        _dirty = False


async def _refresh(db: Any, catalog: Any, now: datetime | None) -> dict[str, Any]:
    """Ein Lauf von refresh (ohne die Sperre)."""
    global _model
    started = time.monotonic()
    since = _iso((now or datetime.now(timezone.utc)) - WINDOW)
    rows = [dict(r) for r in db.story_window(since, BODY_CHARS, BODY_IF_SUMMARY_BELOW)]
    langs = {s.id: s.lang for s in catalog.all()}
    for r in rows:
        r["lang"] = langs.get(r["source_id"], "en")
    reserved = db.outside_story_ids([int(r["id"]) for r in rows], since)
    carried = sorted({r["story_id"] for r in rows if r["story_id"] is not None})
    outside_oldest = {sid: _ts(at) for sid, at in db.story_oldest_before(carried, since).items()}
    model, result = await asyncio.to_thread(_compute, rows, reserved, outside_oldest)
    model.langs = langs
    model.story_of = result
    before = {int(r["id"]): r["story_id"] for r in rows}
    for aid in before:
        result.setdefault(aid, None)       # ohne Titel: nie Teil einer Geschichte
    changes = [(sid, aid) for aid, sid in result.items() if before.get(aid) != sid]
    db.set_story_ids(changes)
    # Geschichten, die es im Fenster nicht mehr gibt: auch ältere Mitglieder lösen (sonst zählten sie
    # später zu einer Gruppe, die dieselbe ID zufällig wieder nimmt). Nur wenn mindestens zwei Artikel im
    # Fenster sie trugen: Ein einzelner Nachzügler, der jetzt allein steht, löst keine ältere Geschichte
    # auf (ihre übrigen Artikel liegen schon vor dem Fenster).
    holders = Counter(sid for sid in before.values() if sid is not None)
    alive = set(result.values())
    gone = [sid for sid in holders if sid not in alive]
    # Bleibt vor dem Fenster nur eine Zeitung übrig, ist es auch dort keine Geschichte mehr (sonst hielten
    # alte Artikel eine story_id aus einer Zeitung). Nicht fortgesetzte, weil zu lange Geschichten
    # (model.retired) bleiben sonst bei ihren älteren Artikeln.
    outside = db.story_sources_before(sorted(gone), since)
    dead = sorted(sid for sid in gone
                  if (holders[sid] >= 2 and sid not in model.retired)
                  or len({publisher(s) for s in outside.get(sid, ())}) < 2)
    db.clear_story_ids(dead, since)
    _model = model
    groups: dict[int, set[str]] = defaultdict(set)
    by_id = {int(r["id"]): r for r in rows}
    for aid, sid in result.items():
        if sid is not None:
            groups[sid].add(by_id[aid]["source_id"])
    stats = {
        "articles": len(rows), "stories": len(groups),
        "in_stories": sum(1 for sid in result.values() if sid is not None),
        "biggest": max((len(v) for v in groups.values()), default=0),
        "changed": len(changes), "seconds": round(time.monotonic() - started, 2),
    }
    log.info("Geschichten: %(articles)d Artikel, %(stories)d Geschichten mit %(in_stories)d Artikeln "
             "(größte %(biggest)d Quellen), %(changed)d geändert, %(seconds).2f s", stats)
    return stats


def _topic_score(model: Model, doc: Doc, other: Doc) -> float | None:
    """Ähnlichkeit für „verwandte Themen“ oder None, wenn die Überschneidung zu dünn ist, der andere
    Artikel aus derselben Zeitung kommt oder schon für sich wie dasselbe Ereignis aussieht
    (verbindbar und ähnlich genug für eine Kante: gehört zur Geschichte, nicht zum Thema)."""
    if publisher(doc.source) == publisher(other.source):
        return None
    if model.shared_informative(doc.vec, other.vec, model.topic_df) < TOPIC_MIN_SHARED:
        return None
    if not model.named(doc, other):
        return None
    # Ein besonderes gemeinsames Wort (Name, Ort, Kompositum), nicht nur „schwer verletzt“ ...
    shared = [t for t in doc.vec if t in other.vec and model.df[t] <= model.topic_df]
    if not any(model.specific(t) for t in shared):
        return None
    # ... und Inhalt neben den Namen: zwei gemeinsame Wörter, die keine Namen sind, oder eines neben zwei
    # Namen. Ein Land, eine Stadt, ein Kontinent allein („Europa“ und „Gespräche“, „Liverpool“ und
    # „Festnahme“, „Südkorea“) macht kein gemeinsames Thema. Teile eines gemeinsamen Kompositums zählen
    # nicht extra („Superstar“, „Super“, „Star“).
    parts = {p for t in shared for p in model.tokenizer.compounds.get(t, ()) if p != t}
    names = sum(1 for t in shared if t in model.names or t in NAME_TOKENS or model.is_name(t))
    content = sum(1 for t in shared if t not in parts and not t.isdigit() and not (
        t in model.names or t in NAME_TOKENS or model.is_name(t)))
    if content < 2 and not (content >= 1 and names >= 2):
        return None
    s = model.sim(doc, other)
    if s < TOPIC_MIN or (s >= EDGE_MIN and model.linkable(doc, other)):
        return None
    return s


def _same_event(model: Model, other: Doc, members: list[Doc]) -> bool:
    """Erzählt `other` das Ereignis der Geschichte des offenen Artikels? Ähnlich genug für eine Kante
    zu LATE_EDGES Mitgliedern (wie ein Nachzügler, siehe cluster), auch wenn ihm für die Geschichte
    selbst ein seltener Name fehlt („Men arrested at UK air base used by US“). Solche Artikel sind
    kein anderes Thema."""
    if len(members) < 2:                    # keine Geschichte: der offene Artikel allein
        return bool(members) and _near_same_event(model, members[0], other)
    return sum(1 for m in members if model.sim(other, m) >= EDGE_MIN) >= LATE_EDGES


def _near_same_event(model: Model, doc: Doc, other: Doc, strict: bool = False) -> bool:
    """Zwei Artikel ohne gemeinsame Geschichte, die trotzdem dasselbe Ereignis erzählen: ähnlich genug
    für eine Kante, höchstens MAX_GAP auseinander, keine verschiedenen Ereignisarten. Mit `strict`
    (um ihn als „dieselbe Geschichte“ zu zeigen) deutsch/englisch zusätzlich wie shown_with
    (gemeinsame Inhaltswörter oder sehr hohe Ähnlichkeit); ohne (um ihn als Thema wegzulassen) nicht."""
    if abs(doc.ts - other.ts) > MAX_GAP.total_seconds() or model.events_apart(doc, other):
        return False
    s = model.sim(doc, other)
    if s < EDGE_MIN:
        return False
    if strict and doc.lang != other.lang:
        return s >= CROSS_SIM_MIN or len({t for t in doc.lead & other.lead if model._content(t)}) >= 2
    return True


def same_event_ids(row: dict[str, Any], exclude: set[int]) -> list[int]:
    """Für einen Artikel ohne Geschichte: Artikel anderer Zeitungen, die dasselbe Ereignis erzählen
    (siehe _near_same_event), ähnlichste zuerst. Die rechte Seite zeigt sie als „dieselbe Geschichte“
    statt als Thema (die Gruppierung hat sie nicht verbunden, etwa weil ein seltener Name fehlt)."""
    model = _model
    if model is None:
        return []
    idx = model.index_of(int(row["id"]))
    if idx is None:
        return []
    doc = model.docs[idx]
    if model.story_of.get(doc.id) is not None or doc.recurring:
        return []                           # Liveblog, Sendung: streift viele Ereignisse
    own = publisher(doc.source)
    exclude = set(exclude) | {doc.id} | set(model.copies.get(doc.id, ()))
    cand: set[int] = set()
    for tok in doc.vec:
        if model.df[tok] <= model.informative_df:
            cand.update(model.postings.get(tok, ()))
    found = [(model.sim(doc, model.docs[j]), model.docs[j].id) for j in cand
             if model.docs[j].id not in exclude and _same_event_pair(model, doc, model.docs[j])]
    return [aid for _, aid in sorted(found, key=lambda x: (-x[0], -x[1]))]


def _same_event_pair(model: Model, a: Doc, b: Doc) -> bool:
    """Erzählen zwei Artikel ohne gemeinsame Geschichte dasselbe Ereignis (für „dieselbe Geschichte“
    ohne Geschichte, von beiden Seiten gleich: nie auf der einen Seite Geschichte, auf der anderen Thema)?
    Verschiedene Zeitungen, kein Liveblog und keine Sendung, _near_same_event (streng) und, weil die
    Prüfung der Gruppierung (linkable) fehlt: ein gemeinsamer Name oder Ort, sonst gleiche Sprache und
    sehr ähnlich. „Polizei“ und „Festnahme“ im ersten Satz machen aus einem Auffahrunfall in Reutlingen
    und russischen Angriffen auf Sumy nicht dasselbe Ereignis."""
    if a.recurring or b.recurring or publisher(a.source) == publisher(b.source):
        return False
    if not _near_same_event(model, a, b, strict=True):
        return False
    if a.lang == b.lang and model.sim(a, b) >= SAME_EVENT_PLAIN_SIM:
        return True
    return model._any_name(a, b)


def _doc_for(model: Model, row: dict[str, Any]) -> Doc:
    """Doc eines Artikels außerhalb des Modells (Vektor mit der IDF des letzten Laufs)."""
    row = {**row, "lang": model.langs.get(row["source_id"], "en")}
    vec = model.vector_for(row)
    lang = _lang(row)
    title = {t for w in words(row.get("title") or "") for t, _ in model.tokenizer.canon(w, lang)}
    lead = title | {t for w in words(_first_sentence(row.get("summary") or ""))
                    for t, _ in model.tokenizer.canon(w, lang)}
    return Doc(id=int(row["id"]), source=row["source_id"], lang=lang, ts=_ts(row["published_at"]),
               vec=vec, xvec=model._restrict(vec), story_id=row.get("story_id"),
               kinds=_kinds(words(row.get("title") or ""), words(_first_sentence(row.get("summary") or "")))
               | _scores(row.get("title") or "", _first_sentence(row.get("summary") or "")),
               title=model._in_vec(title, vec), lead=model._in_vec(lead, vec), soft=is_soft(row),
               recurring=is_recurring(row))


def _best_per_story(model: Model, scored: list[tuple[float, int, int | None]], limit: int) -> list[int]:
    """Höchstens ein Artikel je Geschichte, die ähnlichsten zuerst (bei Gleichstand der neuere)."""
    out: list[int] = []
    seen: set[int] = set()
    for _, aid, sid in sorted(scored, key=lambda x: (-x[0], -x[1])):
        key = sid if sid is not None else -aid
        if key in seen:
            continue
        seen.add(key)
        out.append(aid)
        if len(out) >= limit:
            break
    return out


def topic_ids(db: Any, row: dict[str, Any], exclude: set[int], limit: int = TOPIC_LIMIT,
              time_limit: float | None = None) -> list[int]:
    """Artikel-IDs zum selben Thema, aber nicht derselben Geschichte, beste zuerst (mehr als `limit`,
    damit nach Filtern der Aufrufer noch genug übrig sind; höchstens 3 * limit).

    Liegt der Artikel im Fenster des letzten Laufs, kommen Kandidaten aus dem invertierten Index;
    finden sich so weniger als `limit` (oder liegt er davor), sucht der Volltextindex (Titel und
    Anreißer) der letzten RELATED_DAYS Tage nach Paaren seiner informativsten Wörter. Alle Kandidaten werden mit den Vektoren bewertet.
    `exclude` (Artikel selbst, seine Geschichte) kommt nie zurück; `time_limit` für die Volltextsuche."""
    model = _model
    if model is None:
        return []
    want = 3 * limit
    exclude = set(exclude) | {int(row["id"])}
    scored: list[tuple[float, int, int | None]] = []
    fts_docs: dict[int, Doc] = {}           # Kandidaten aus der Volltextsuche (nicht im Modell)
    idx = model.index_of(int(row["id"]))
    doc = model.docs[idx] if idx is not None else _doc_for(model, row)
    if doc.recurring:
        return []                           # Sendung, Liveblog, Newsletter: streift alles
    if idx is not None:
        exclude.update(model.copies.get(doc.id, ()))
        cand: set[int] = set()
        for tok in doc.vec:
            cand.update(model.postings.get(tok, ()))
        for j in cand:
            other = model.docs[j]
            if other.id in exclude:
                continue
            s = _topic_score(model, doc, other)
            if s is not None:
                scored.append((s, other.id, model.story_of.get(other.id)))
    if len(_best_per_story(model, scored, want)) < limit:
        query = _fts_topic_query(model, row)
        if query:
            since = _iso(datetime.now(timezone.utc) - timedelta(days=RELATED_DAYS))
            known = {aid for _, aid, _ in scored}
            own = copy_key(row)
            fts = (db.topic_candidates(query, since, limit=4 * want) if time_limit is None
                   else db.topic_candidates(query, since, limit=4 * want, time_limit=time_limit))
            for r in fts:
                r = dict(r)
                if r["id"] in exclude or r["id"] in known or (own and copy_key(r) == own):
                    continue
                other = _doc_for(model, r)
                s = _topic_score(model, doc, other)
                if s is not None and s >= TOPIC_FTS_MIN:        # ohne Vorauswahl über den Index: strenger
                    scored.append((s, r["id"], r["story_id"]))
                    fts_docs[r["id"]] = other
    # Kandidaten, die das Ereignis der eigenen Geschichte erzählen, fallen weg (siehe _same_event).
    sid = model.story_of.get(doc.id) if idx is not None else row.get("story_id")
    member_idx = {k for k in (model.index_of(a) for a, s in model.story_of.items()
                              if sid is not None and s == sid) if k is not None}   # Kopien nur einmal
    members = [model.docs[k] for k in sorted(member_idx) if model.docs[k].id != doc.id] + [doc]
    keys = _story_keys(model, members)
    gap = MAX_GAP.total_seconds()
    keep: list[tuple[float, int, int | None, Doc]] = []
    for s, aid, osid in scored:
        k = model.index_of(aid)
        other = model.docs[k] if k is not None else fts_docs.get(aid)
        if other is None or other.recurring:
            continue
        # Das Thema muss im Titel stehen (keine Sendungen und Übersichten, die es nur im Text streifen).
        if not any(t in doc.vec and model.df[t] <= model.topic_df for t in other.title):
            continue
        if _same_event(model, other, members):
            continue
        # Von der anderen Seite gesehen dasselbe Ereignis (steht dort unter „dieselbe Geschichte“)?
        if (osid is None and k is not None and model.story_of.get(other.id) is None
                and _same_event_pair(model, other, doc)):
            continue
        # Nennt der Titel das Stichwort der Geschichte (Fairford) zur selben Zeit: dasselbe Ereignis.
        if other.title & keys and abs(other.ts - doc.ts) <= gap:
            continue
        keep.append((s, aid, osid, other))
    # Höchstens einer je Geschichte, TOPIC_PER_PAPER je Zeitung, und keine zwei Artikel, die dasselbe
    # Ereignis erzählen (auch wenn die Gruppierung sie nicht verbunden hat).
    out: list[int] = []
    chosen: list[Doc] = []
    seen: set[int] = set()
    papers: Counter = Counter()
    for s, aid, osid, other in sorted(keep, key=lambda x: (-x[0], -x[1])):
        key = osid if osid is not None else -aid
        paper = publisher(other.source)
        if key in seen or papers[paper] >= TOPIC_PER_PAPER:
            continue
        if any(_near_same_event(model, c, other) for c in chosen):
            continue
        seen.add(key)
        papers[paper] += 1
        chosen.append(other)
        out.append(aid)
        if len(out) >= want:
            break
    return out


def _story_keys(model: Model, members: list[Doc]) -> set[str]:
    """Stichwörter einer Geschichte: seltene, besondere Wörter (Name, Ort; keine Länder und Namen aus
    dem Lexikon), die in mindestens KEY_SHARE ihrer Mitglieder einer Sprache stehen („fairford“). Nur
    bei Geschichten ab 3 Artikeln."""
    if len(members) < 3:
        return set()
    out: set[str] = set()
    for lang in {m.lang for m in members}:
        same = [m for m in members if m.lang == lang]
        if len(same) < 2:
            continue
        counts = Counter(t for m in same for t in m.vec if model.df[t] <= model.informative_df
                         and model.specific(t) and t not in NAME_TOKENS)
        out.update(t for t, n in counts.items() if n >= KEY_SHARE * len(same))
    return out


def _fts_topic_query(model: Model, row: dict[str, Any]) -> str | None:
    """FTS5-Abfrage aus den informativsten Wörtern eines Artikels: je zwei davon müssen vorkommen
    (ODER über alle Paare der besten FTS_WORDS), damit ein einzelnes Wort wie „Trump“ nicht reicht."""
    from itertools import combinations

    from .db import _phrase

    lang = "de" if model.langs.get(row["source_id"]) == "de" else "en"
    n = max(model.n, 1)
    best: dict[str, float] = {}
    for text in (row.get("title") or "", (row.get("summary") or "")[:SUMMARY_CHARS]):
        for raw in _WORD_RE.findall(clean(text)):
            w = normalize(raw)
            if len(w) < 4 or w.isdigit() or w in _STOP[lang]:
                continue
            # Abgefragt wird das Wort wie geschrieben (klein): FTS faltet Akzente selbst, aber kein
            # „ß“ zu „ss“ („Straße“ stünde sonst als „strasse“ in der Abfrage und fände nichts).
            score = 0.0
            for tok, _ in model.tokenizer.canon(w, lang):
                d = model.df.get(tok, 0)
                if d > model.topic_df:
                    score = 0.0
                    break
                score = max(score, math.log((n + 1) / (d + 0.5)))
            if score > best.get(raw.lower(), (0.0, ""))[0]:
                best[raw.lower()] = (score, "|".join(t for t, _ in model.tokenizer.canon(w, lang)))
    top: list[str] = []
    seen: set[str] = set()                  # je kanonischem Wort nur eine Schreibweise
    for word, (_, key) in sorted(best.items(), key=lambda x: (-x[1][0], x[0])):
        if key not in seen:
            seen.add(key)
            top.append(word)
        if len(top) >= FTS_WORDS:
            break
    if len(top) < 2:
        return None
    pairs = " OR ".join(f"({_phrase(a)} AND {_phrase(b)})" for a, b in combinations(top, 2))
    return "{title summary} : (" + pairs + ")"      # Volltexte streifen zu viele Themen
