// Zeit-, Zahl- und Länderformate (Deutsch, Versalien übernimmt CSS).

const MONTHS = ['JAN', 'FEB', 'MÄR', 'APR', 'MAI', 'JUN', 'JUL', 'AUG', 'SEP', 'OKT', 'NOV', 'DEZ'];

export function parseDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

function startOfDay(d) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

/** Kalendertage zwischen d und heute (0 = heute, 1 = gestern). */
export function daysAgo(d, now = new Date()) {
  return Math.round((startOfDay(now) - startOfDay(d)) / 86400000);
}

const pad = (n) => String(n).padStart(2, '0');
const hhmm = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
const dayMonth = (d) => `${d.getDate()}. ${MONTHS[d.getMonth()]}`;

/** Zeit in der Listenzeile: „12 MIN“, „3 STD“, „GESTERN 22:10“, „24. SEP“. */
export function listTime(iso, now = new Date()) {
  const d = parseDate(iso);
  if (!d) return '';
  const mins = Math.max(0, Math.floor((now - d) / 60000));
  if (mins < 60) return `${Math.max(1, mins)} MIN`;
  if (mins < 24 * 60) return `${Math.floor(mins / 60)} STD`;
  if (daysAgo(d, now) === 1) return `GESTERN ${hhmm(d)}`;
  return dayMonth(d);
}

/** Überschrift der Tagesgruppe: HEUTE, GESTERN, 24. SEP. */
export function dayLabel(iso, now = new Date()) {
  const d = parseDate(iso);
  if (!d) return 'OHNE DATUM';
  const n = daysAgo(d, now);
  if (n <= 0) return 'HEUTE';
  if (n === 1) return 'GESTERN';
  const label = dayMonth(d);
  return d.getFullYear() === now.getFullYear() ? label : `${label} ${d.getFullYear()}`;
}

/** Zeitpunkt in der Leseansicht: „Heute 18:56“, „Gestern 07:10“, „24. Sep 12:00“. */
export function articleTime(iso, now = new Date()) {
  const d = parseDate(iso);
  if (!d) return '';
  const n = daysAgo(d, now);
  if (n <= 0) return `Heute ${hhmm(d)}`;
  if (n === 1) return `Gestern ${hhmm(d)}`;
  return `${dayMonth(d)} ${hhmm(d)}`;
}

/** „vor 12 Min“, „vor 3 Std“, „vor 2 Tagen“. */
export function ago(iso, now = new Date()) {
  const d = parseDate(iso);
  if (!d) return '';
  const mins = Math.max(0, Math.floor((now - d) / 60000));
  if (mins < 1) return 'gerade eben';
  if (mins < 60) return `vor ${mins} Min`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `vor ${hours} Std`;
  const days = Math.floor(hours / 24);
  return days === 1 ? 'vor 1 Tag' : `vor ${days} Tagen`;
}

/** Lesezeit in Minuten (Backend liefert read_minutes; sonst aus body_len geschätzt). */
export function readMinutes(item) {
  if (typeof item.read_minutes === 'number') return item.read_minutes;
  if (!item.body_len) return 0;
  return Math.max(1, Math.round(item.body_len / 1100));
}

export function formatCount(n) {
  return new Intl.NumberFormat('de-DE').format(n || 0);
}

export function formatMB(bytes) {
  return `${Math.round((bytes || 0) / 1e6)} MB`;
}

/** Ländernamen für Gruppentitel; unbekannte Codes bleiben als Code stehen. */
const COUNTRIES = {
  DE: 'Deutschland', AT: 'Österreich', CH: 'Schweiz', GB: 'Großbritannien', UK: 'Großbritannien',
  IE: 'Irland', US: 'USA', CA: 'Kanada', AU: 'Australien', NZ: 'Neuseeland', FR: 'Frankreich',
  BE: 'Belgien', NL: 'Niederlande', LU: 'Luxemburg', IT: 'Italien', ES: 'Spanien', PT: 'Portugal',
  DK: 'Dänemark', SE: 'Schweden', NO: 'Norwegen', FI: 'Finnland', IS: 'Island', PL: 'Polen',
  CZ: 'Tschechien', SK: 'Slowakei', HU: 'Ungarn', RO: 'Rumänien', BG: 'Bulgarien', GR: 'Griechenland',
  CY: 'Zypern', MT: 'Malta', SI: 'Slowenien', HR: 'Kroatien', RS: 'Serbien', BA: 'Bosnien',
  UA: 'Ukraine', RU: 'Russland', BY: 'Belarus', LT: 'Litauen', LV: 'Lettland', EE: 'Estland',
  TR: 'Türkei', IL: 'Israel', PS: 'Palästina', LB: 'Libanon', QA: 'Katar', AE: 'Emirate',
  SA: 'Saudi-Arabien', IR: 'Iran', IQ: 'Irak', EG: 'Ägypten', MA: 'Marokko', TN: 'Tunesien',
  DZ: 'Algerien', NG: 'Nigeria', KE: 'Kenia', ZA: 'Südafrika', GH: 'Ghana', ET: 'Äthiopien',
  IN: 'Indien', PK: 'Pakistan', BD: 'Bangladesch', LK: 'Sri Lanka', NP: 'Nepal', CN: 'China',
  HK: 'Hongkong', TW: 'Taiwan', JP: 'Japan', KR: 'Südkorea', SG: 'Singapur', MY: 'Malaysia',
  ID: 'Indonesien', PH: 'Philippinen', TH: 'Thailand', VN: 'Vietnam', MX: 'Mexiko', BR: 'Brasilien',
  AR: 'Argentinien', CL: 'Chile', CO: 'Kolumbien', PE: 'Peru', VE: 'Venezuela', CU: 'Kuba',
  INT: 'International', EU: 'Europa',
};
export function countryName(code) {
  return COUNTRIES[(code || '').toUpperCase()] || code || '?';
}

/** Reihenfolge der Ländergruppen: DE, AT, CH, GB, US zuerst, dann alphabetisch nach Name. */
const FIRST = ['DE', 'AT', 'CH', 'GB', 'US'];
export function compareCountries(a, b) {
  const ia = FIRST.indexOf(a), ib = FIRST.indexOf(b);
  if (ia !== -1 || ib !== -1) return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
  return countryName(a).localeCompare(countryName(b), 'de');
}

/** Quellen nach Land gruppieren: [[code, [quellen…]], …] in fester Reihenfolge. */
export function groupByCountry(sources) {
  const groups = new Map();
  for (const s of sources) {
    const cc = (s.country || '?').toUpperCase();
    if (!groups.has(cc)) groups.set(cc, []);
    groups.get(cc).push(s);
  }
  for (const list of groups.values()) list.sort((a, b) => a.name.localeCompare(b.name, 'de'));
  return [...groups.entries()].sort((a, b) => compareCountries(a[0], b[0]));
}

/** Platzhalter aus Feeds („None“, „null“, „undefined“) oder reine Titel-Wiederholung → ''. */
const PLACEHOLDERS = new Set(['none', 'null', 'undefined']);
export function cleanText(text, title) {
  const t = String(text ?? '').trim();
  const low = t.toLowerCase();
  if (!t || PLACEHOLDERS.has(low)) return '';
  if (title && low === String(title).trim().toLowerCase()) return '';
  return t;
}

// ---- Bild-Schlüssel: dasselbe Bild in anderer Größe erkennen -------------------------------
// Gleiche Regeln wie image_key() in app/extract.py (beide Stellen gemeinsam ändern!):
//  1. Fragment (#…) und Schema (http:// bzw. https://, //) weg; nur der Host wird klein geschrieben,
//     Pfad und Query bleiben, wie sie sind (Bild-IDs können Groß-/Kleinschreibung unterscheiden).
//  2. Query (?…): bleibt, nur ohne Größen- und Signatur-Parameter (w, width, h, height, q, quality,
//     dpr, fit, resize, func, org_if_sml, auto, format, fm, s, sig, itok, smart; Name ohne Beachtung
//     der Schreibung). Zuschnitt-Parameter (crop, rect, im) entfernt nur der Browser (Standard,
//     strict = false: für die Anzeige ist ein anderer Ausschnitt dasselbe Bild); mit strict = true
//     bleiben sie wie beim Server (image_key, Standard strict=True), der dadurch kein Bild verliert.
//     Steht bei einem Pfad ohne Bild-Endung in url= oder src= eine URL (absolut oder mit / beginnend,
//     dann auf dem Host der äußeren URL), ist deren Schlüssel der Schlüssel (Bild-Proxys wie
//     /_next/image?url=…&w=640), höchstens 5 Ebenen tief; die übrigen Parameter der äußeren URL
//     (ohne Größen-Parameter) werden angehängt, außer die innere URL hat eine Bild-Endung
//     (/thumb?src=/v.mp4&t=10 und &t=50 sind verschiedene Bilder).
//  3. Pfadsegmente, die nur eine Größe angeben, weg: /w960/, /h540/ (mind. zwei Ziffern), /1024x683/.
//  4. Im Dateinamen die Endung(en) weg (.jpg, .JPG, .jpg.webp …), dann Größen-Token weg:
//     _w960 bzw. -w960, _1024x683 bzw. -1024x683, @2x, -scaled; die Spiegel-Zuschnitte _r1.5,
//     _fpx43.2, _fpy41 nur, wenn der Dateiname auch ein _w960-Token hat (Token enden an _ - . @
//     oder am Namensende).
//  Schlüssel = { full: Host+Pfad[+?Query], base: bereinigter Dateiname falls ≥ 8 Zeichen, sonst '' }.
//  Der Server wertet nur gleiche Schlüssel als gleiches Bild; die unscharfe Erkennung unten
//  (sameImage) gibt es nur hier im Browser.
const SIZE_DIR_RE = /\/(?:[wh]\d{2,}|\d+x\d+)(?=\/)/g;
const SIZE_TOKEN_RE = /(?:[_-]w\d+|[_-]\d+x\d+|@\d+x|-scaled)(?=[_.@-]|$)/g;
const WIDTH_TOKEN_RE = /_w\d+(?=[_.@-]|$)/;
const CROP_TOKEN_RE = /(?:_r\d+(?:\.\d+)?|_fp[xy]\d+(?:\.\d+)?)(?=[_.@-]|$)/g;
const IMAGE_EXT_RE = /\.(?:jpe?g|png|webp|gif|avif)$/i;
const EXT_RE = /(?:\.(?:jpe?g|png|webp|gif|avif))+$|\.[a-z][a-z0-9]{1,4}$/i;
const SIZE_PARAMS = new Set(['w', 'width', 'h', 'height', 'q', 'quality', 'dpr', 'fit', 'resize',
  'func', 'org_if_sml', 'auto', 'format', 'fm', 's', 'sig', 'itok', 'smart']);
const CROP_PARAMS = new Set(['crop', 'rect', 'im']);   // nur ohne strict entfernt (siehe oben)
const URL_PARAM_RE = /^(?:(?:https?:)?\/\/|\/)/i;
const IMAGE_BASE_MIN = 8;
const URL_PARAM_DEPTH = 5;

/** Nur ASCII-Leerraum abschneiden (JS trim() und Python strip() unterscheiden sich). */
const trimSpace = (v) => v.replace(/^[ \t\n\r\f\v]+|[ \t\n\r\f\v]+$/g, '');

/** %XX-Folgen dekodieren wie Pythons unquote(); kaputte Folgen bleiben stehen. */
function unquote(v) {
  return v.replace(/(?:%[0-9a-f]{2})+/gi, (m) => { try { return decodeURIComponent(m); } catch { return m; } });
}

/** Schlüssel einer Bild-URL: { full: Host+Pfad[+?Query] bereinigt, base: bereinigter Dateiname oder '' }.
 *  strict = true behält Zuschnitt-Parameter (wie der Server). */
export function imageKey(url, strict = false, depth = 0) {
  const raw = trimSpace(String(url || ''))
    .replace(/#[^]*$/, '').replace(/^[a-z][a-z0-9+.-]*:\/\//i, '').replace(/^\/\//, '');
  const q = raw.indexOf('?');
  let path = q === -1 ? raw : raw.slice(0, q);
  const query = q === -1 ? '' : raw.slice(q + 1);
  const slash = path.indexOf('/');
  const host = (slash === -1 ? path : path.slice(0, slash)).toLowerCase();
  path = host + (slash === -1 ? '' : path.slice(slash));
  const drop = (name) => SIZE_PARAMS.has(name) || (!strict && CROP_PARAMS.has(name));
  const kept = [];
  let inner = null;
  for (const part of query ? query.split('&') : []) {
    const eq = part.indexOf('=');
    const name = (eq === -1 ? part : part.slice(0, eq)).toLowerCase();
    if (inner === null && (name === 'url' || name === 'src') && !IMAGE_EXT_RE.test(path) && depth < URL_PARAM_DEPTH) {
      let candidate = trimSpace(unquote(eq === -1 ? '' : part.slice(eq + 1)));
      if (URL_PARAM_RE.test(candidate)) {
        if (candidate.startsWith('/') && !candidate.startsWith('//')) candidate = host + candidate;   // relativ: Host der äußeren URL
        inner = candidate;
        continue;
      }
    }
    if (part && !drop(name)) kept.push(part);
  }
  if (inner !== null) {
    const key = imageKey(inner, strict, depth + 1);
    if (kept.length && !IMAGE_EXT_RE.test(inner.replace(/[?#][^]*$/, ''))) {
      key.full += (key.full.includes('?') ? '&' : '?') + kept.join('&');
    }
    return key;
  }
  const cut = path.lastIndexOf('/');
  const dir = path.slice(0, cut + 1).replace(SIZE_DIR_RE, '');
  let stem = path.slice(cut + 1).replace(EXT_RE, '');
  if (WIDTH_TOKEN_RE.test(stem)) stem = stem.replace(CROP_TOKEN_RE, '');
  stem = stem.replace(SIZE_TOKEN_RE, '');
  const full = dir + stem + (kept.length ? `?${kept.join('&')}` : '');
  return { full, base: stem.length >= IMAGE_BASE_MIN ? stem : '' };
}

// Unscharf (nur im Browser): gleiche Website (registrierbare Domain), gleich viele Pfadsegmente,
// gleiche Query, gleicher bereinigter Dateiname (≥ 8 Zeichen, Varianten-Endungen wie
// -superJumbo/-articleLarge abgeschnitten) und jedes abweichende Segment ist ein Größen-/
// Zuschnitt-Token (/1024/ statt /480/, /Img_16_9/ statt /Img_4_3/, /master/ …).
const SIZE_SEG_RE = /^(?:\d+x\d+(?:-\w+)?|[wh]\d+|\d{1,4}|img_\d+_\d+|master|thumb|small|medium|large|xlarge|articlelarge|superjumbo)$/i;
const VARIANT_RE = /-(?:master|thumb|small|medium|large|xlarge|articlelarge|superjumbo|jumbo)(?:\d+)?$/i;
const NUM_SEG_RE = /^\d{1,4}$/;
const SECOND_LEVEL_RE = /^(?:co|com|org|net|gov|ac)$/;

/** Registrierbare Domain: letzte zwei Labels, drei bei co.uk, com.au, org.uk … */
function siteOf(host) {
  const labels = host.split('.');
  const n = labels.length >= 3 && SECOND_LEVEL_RE.test(labels[labels.length - 2]) ? 3 : 2;
  return labels.slice(-n).join('.');
}

// Hash-Endung im Dateinamen (heise: Daimler_E-lkw-86ffab972e33a670 und -a55055d4fc8ac0e5 sind zwei
// Größen desselben Bilds): 16–32 Hex-Zeichen mit Ziffer und Buchstabe (kein reines Datum/keine Zahl).
// Kürzere Endungen (WordPress screenshot-a1b2c3d4e5f6) unterscheiden eher verschiedene Bilder.
const HASH_SUFFIX_RE = /-(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{16,32}$/i;
// Bild-Proxy cloudimg.io: /<Bearbeitung…>/_<alias>_/<Pfad am Ursprung>, Alias = Host mit „-“ statt „.“
// (heise: …cloudimg.io/width/610/q85…/_www-heise-de_/imgs/18/… zeigt www.heise.de/imgs/18/…).
const CLOUDIMG_HOST_RE = /\.cloudimg\.io$/;
const CLOUDIMG_ALIAS_RE = /^_[a-z0-9-]+_$/i;

/** Pfadsegmente am Ursprung hinter einem Bild-Proxy (Host am Anfang), oder null (kein Proxy). */
function unproxied(segs) {
  if (!CLOUDIMG_HOST_RE.test(segs[0])) return null;
  const at = segs.findIndex((seg, i) => i > 0 && CLOUDIMG_ALIAS_RE.test(seg));
  return at === -1 ? null : [segs[at].slice(1, -1).replace(/-/g, '.').toLowerCase(), ...segs.slice(at + 1)];
}

/** Teile eines Schlüssels für den unscharfen Vergleich, oder null (kann nie unscharf gleich sein).
 *  bucket fasst zusammen, was gleich sein muss (Website, Segmentzahl, Dateiname, Query): Nur
 *  Schlüssel mit gleichem bucket müssen Segment für Segment verglichen werden. */
function fuzzyParts(key) {
  const [path, rawQuery = ''] = key.full.split('?');
  let query = rawQuery;
  let segs = path.split('/');
  const origin = unproxied(segs);
  if (origin) { segs = origin; query = ''; }   // Query des Proxys sind nur Bearbeitungsschritte
  if (segs.length < 2) return null;
  const named = segs.pop().replace(VARIANT_RE, '');
  // Ohne Hash-Endung; bleibt davon zu wenig (pic-<hash>), zählt der ganze Name (dann nur gleiche Namen).
  const stripped = named.replace(HASH_SUFFIX_RE, '');
  const stem = stripped.length >= IMAGE_BASE_MIN ? stripped : named;
  if (stem.length < IMAGE_BASE_MIN) return null;
  // named: Dateiname mit Hash-Endung; gleicht er erst ohne sie, muss der Pfad ganz gleich sein.
  return { segs, stem, named, bucket: JSON.stringify([siteOf(segs[0]), segs.length, stem, query]) };   // [0] = Host
}

/** Unscharfer Vergleich zweier fuzzyParts mit gleichem bucket. */
function fuzzyMatch(a, b) {
  const sa = a.segs, sb = b.segs;
  // Gleich erst ohne Hash-Endung (heise -86ffab…/-a550…): Größen desselben Bilds liegen im selben
  // Ordner (heise: Bild-ID als Ordnerkette imgs/18/4/9/…); in anderen Ordnern ist es ein anderes Bild
  // gleichen Namens (Tesla-Model-Y, screenshot).
  // Nur mit Bild-ID als Ordnerkette (mindestens drei Zahlen-Ordner wie heise imgs/18/4/9/…); flache
  // CMS-Ordner (/uploads/article-image-<hash>.jpg) trennen verschiedene Bilder allein über den Hash.
  if (a.named !== b.named) {
    return a.named.length === b.named.length && sa.filter((seg) => NUM_SEG_RE.test(seg)).length >= 3
      && sa.every((seg, i) => i === 0 || seg === sb[i]);
  }
  let differs = false, onlyNumbers = true;
  for (let i = 1; i < sa.length; i++) {
    if (sa[i] === sb[i]) continue;
    if (!SIZE_SEG_RE.test(sa[i]) || !SIZE_SEG_RE.test(sb[i])) return false;
    differs = true;
    if (!NUM_SEG_RE.test(sa[i]) || !NUM_SEG_RE.test(sb[i])) onlyNumbers = false;
  }
  // Nur Zahlen-Ordner verschieden (2025/10 statt 2026/09), Dateiname ohne Ziffern („screenshot“) und
  // kein gemeinsames Pfadsegment mit Ziffern (Bild-ID wie taz /picture/8763231/948/): eher ein
  // anderes Bild gleichen Namens als eine andere Größe.
  if (differs && onlyNumbers && !/\d/.test(a.named)) return sa.some((seg, i) => i > 0 && seg === sb[i] && /\d/.test(seg));
  return true;
}

function fuzzySame(a, b) {
  const pa = fuzzyParts(a), pb = fuzzyParts(b);
  return !!pa && !!pb && pa.bucket === pb.bucket && fuzzyMatch(pa, pb);
}

// Breite laut URL (Spiegel _w520/_w960, ?width=, ?w=, cloudimg /width/610/, taz /picture/<id>/<breite>/
// mit Bild-ID ab 5 Ziffern, damit Datumsordner nicht zählen), 0 = unbekannt.
const URL_WIDTH_RE = /_w(?!(?:19|20)\d\d(?![\d]))(\d{2,4})(?=[_.@-]|$)|[?&](?:width|w)=(\d{2,4})(?=&|$)|\/width\/(\d{2,4})(?=\/)|\/picture\/\d{5,}\/(\d{1,4})(?=\/)/;

/** Bildbreite aus der URL (für „größere Fassung im Text statt kleinem Feed-Bild“), 0 = unbekannt. */
export function urlWidth(url) {
  const m = URL_WIDTH_RE.exec(String(url || '').toLowerCase());
  return m ? Number(m[1] || m[2] || m[3] || m[4]) : 0;
}

/** true, wenn beide URLs dasselbe Bild (ggf. in anderer Größe) zeigen: gleicher Schlüssel oder unscharf gleich. */
export function sameImage(a, b) {
  const ka = imageKey(a), kb = imageKey(b);
  if (!ka.full || !kb.full) return false;
  return ka.full === kb.full || fuzzySame(ka, kb);
}

// Unscharf nur mit den ersten 50 Bildern desselben buckets vergleichen. Bewusster Tausch: Erst ab dem
// 51. verschiedenen Bild mit gleicher Website, Segmentzahl und gleichem Dateinamen (nur bei feindlichem
// HTML) kann eine Größen-Variante stehen bleiben; dafür bleibt die Laufzeit linear.
const SEEN_BUCKET_MAX = 50;

/** Merkliste für „schon gezeigt?“: seen(url) ist true, wenn ein früher gemerktes Bild laut sameImage
 *  dasselbe zeigt; sonst wird url gemerkt. Wie ein Vergleich mit allen früheren, aber jeder Schlüssel
 *  wird nur einmal berechnet, gleiche Schlüssel über ein Set, unscharf nur innerhalb des buckets
 *  (höchstens SEEN_BUCKET_MAX Einträge): lineare Laufzeit auch bei Tausenden Bildern. */
export function imageSeen() {
  const exact = new Set();
  const buckets = new Map();
  return (url) => {
    const key = imageKey(url);
    if (!key.full) return false;
    if (exact.has(key.full)) return true;
    const parts = fuzzyParts(key);
    const list = parts ? buckets.get(parts.bucket) : null;
    if (list && list.some((other) => fuzzyMatch(other, parts))) return true;
    exact.add(key.full);
    if (parts) {
      if (!list) buckets.set(parts.bucket, [parts]);
      else if (list.length < SEEN_BUCKET_MAX) list.push(parts);
    }
    return false;
  };
}

// Kleines Bild laut URL (Breite < 200 px; _w120, w_64 in Bild-Diensten, /80x80/, width=, dimensions=): Autorenfoto, Wappen, Symbol. Wird nie als Duplikat
// entfernt und nie zum Titelbild. Gleiche Regel wie is_small_image_url() in app/extract.py.
const SMALL_URL_RE = /_w(\d+)(?=[_.@-]|$)|(?:,|%2c|\/)w_(\d+)|\/(\d{2,3})x\d{2,3}\/|[?&](?:width|dimensions)=(\d+)/g;
const SMALL_MAX = 200;

export function isSmallImageUrl(url) {
  for (const m of String(url || '').toLowerCase().matchAll(SMALL_URL_RE)) {
    if (Number(m[1] || m[2] || m[3] || m[4]) < SMALL_MAX) return true;
  }
  return false;
}

// ---- Rechte Seite am Desktop (Tagesübersicht, Titelseite) ------------------------------------

const WEEKDAYS = ['Sonntag', 'Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag'];
const MONTH_NAMES = ['Januar', 'Februar', 'März', 'April', 'Mai', 'Juni', 'Juli', 'August', 'September',
  'Oktober', 'November', 'Dezember'];

/** Tag als Date (Ortszeit): 'YYYY-MM-DD' (Kalendertag vom Server) oder Date; sonst null. */
function asDay(value) {
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ''));
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null;
}

/** „Samstag 27. September“ (Versalien übernimmt CSS). */
export function longDate(value) {
  const d = asDay(value);
  return d ? `${WEEKDAYS[d.getDay()]} ${d.getDate()}. ${MONTH_NAMES[d.getMonth()]}` : '';
}

/** „Sa 27.09.“ */
export function shortDate(value) {
  const d = asDay(value);
  return d ? `${WEEKDAYS[d.getDay()].slice(0, 2)} ${pad(d.getDate())}.${pad(d.getMonth() + 1)}.` : '';
}

/** Uhrzeit „18:40“. */
export function clockTime(d = new Date()) {
  return hhmm(d);
}

/**
 * Punkt-Histogramm: Füllhöhe je Stunde in 0..rows Punkten, ceil(n / max * rows); 0 bleibt 0, jede
 * Stunde mit Artikeln hat mindestens einen Punkt. Fehlende oder kaputte Werte zählen als 0.
 */
export function histogramLevels(counts, rows = 6) {
  const values = (Array.isArray(counts) ? counts : []).map((n) => (Number.isFinite(n) && n > 0 ? n : 0));
  const max = Math.max(0, ...values);
  return values.map((n) => (max && n ? Math.min(rows, Math.ceil((n / max) * rows)) : 0));
}

/** Quellenzeile einer Geschichte: „SPIEGEL, GUARDIAN, NZZ +7“ (gezeigte Kurznamen, Rest als +N). */
export function sourcesLine(names, total) {
  const list = (names || []).filter(Boolean);
  const rest = (Number(total) || 0) - list.length;
  if (rest <= 0) return list.join(', ');
  return list.length ? `${list.join(', ')} +${rest}` : `+${rest}`;
}
