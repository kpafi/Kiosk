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
