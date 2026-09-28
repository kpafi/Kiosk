// Zeitungen in der Suche (E4Suche): welche Quellen zur Anfrage passen, Reihenfolge, Monogramm.
// Reine Funktionen ohne DOM (auch in tests/test_frontend_js.py geprüft).

const FTS_WORDS = ['AND', 'OR', 'NOT', 'NEAR'];
const ARTICLES = ['the', 'der', 'die', 'das', 'le', 'la', 'el'];   // vor dem Namen, zählen nicht als erstes Wort

/** Akzente entfernen („süd“ → „sud“). */
function stripMarks(s) {
  return s.normalize('NFD').replace(/\p{M}+/gu, '');
}

/** Vergleichsformen eines Textes: ohne Akzente und mit ae/oe/ue („Süd“ → sud, sued). NFC zuerst,
 *  damit auch ein zerlegtes „u + ¨“ zu ae/oe/ue wird. */
function forms(text) {
  const low = String(text || '').normalize('NFC').toLowerCase().replace(/ß/g, 'ss');
  const plain = stripMarks(low);
  const translit = stripMarks(low.replace(/ä/g, 'ae').replace(/ö/g, 'oe').replace(/ü/g, 'ue'));
  return plain === translit ? [plain] : [plain, translit];
}

const words = (s) => s.split(/[^\p{L}\p{N}]+/u).filter(Boolean);

/** Wörter der Anfrage ohne FTS-Operatoren (wie queryWords in der Suche, aber auch einzelne Zeichen
 *  wie das „n“ in „n-tv“), je Vergleichsform. Ausschlüsse gibt es nicht: die Artikelsuche (_fts_query)
 *  sucht „-bild“ als „bild“, also zählt es hier auch als Wort; AND/OR/NOT/NEAR fallen weg. */
function queryForms(q) {
  const raw = q.replace(/["()*^:]/g, ' ').split(/\s+/).filter(Boolean);
  const text = raw.filter((w) => !FTS_WORDS.includes(w)).join(' ');
  return forms(text).map((f) => ({ tokens: words(f), compact: words(f).join('') }))
    .filter((f) => f.compact.length >= 2);
}

/** Vergleichsformen einer Zeitung: Wörter aus Name und Kurzname, alles ohne Leerzeichen, erstes Wort. */
function sourceForms(s) {
  const id = String(s.id || '').toLowerCase();
  const names = forms(s.name);
  const shorts = forms(s.short || '');
  return names.flatMap((name) => shorts.map((short) => {
    const nameWords = words(name);
    const shortWords = words(short);
    return {
      id,
      short: shortWords.join(''),
      pool: [...nameWords, ...shortWords],
      firsts: [nameWords[0], nameWords[ARTICLES.includes(nameWords[0]) ? 1 : 0], shortWords[0]].filter(Boolean),
      compacts: [nameWords.join(''), shortWords.join('')].filter(Boolean),
    };
  }));
}

/** Passt die Zeitung? null oder {exact, first}. */
function matchOne(s, qforms) {
  let hit = null;
  for (const src of sourceForms(s)) {
    for (const { tokens, compact } of qforms) {
      if (compact.length < 3) {
        // Kurze Anfragen („sz“, „dw“, „fr“) nur als genauer Kurzname oder ID, sonst brächte
        // „de“ oder „ai“ jede zweite Zeitung.
        if (src.short === compact || src.id === compact) hit = { exact: true, first: true };
        continue;
      }
      const byWords = tokens.every((t) => src.pool.some((w) => w.startsWith(t)));
      const byCompact = src.compacts.some((c) => c.startsWith(compact)) || (!!src.id && src.id.startsWith(compact));
      if (!byWords && !byCompact) continue;
      const exact = src.compacts.includes(compact) || src.id === compact;
      const first = src.firsts.some((w) => w.startsWith(tokens[0])) || (!!src.id && src.id.startsWith(tokens[0]));
      hit = { exact: exact || !!hit?.exact, first: first || !!hit?.first };
    }
  }
  return hit;
}

/** Name zum Sortieren ohne Artikel vorn („The Irish Times“ unter I). */
const sortName = (s) => String(s.name || '').replace(new RegExp(`^(${ARTICLES.join('|')})\\s+`, 'i'), '');

/**
 * Zeitungen, die zur Suchanfrage passen, sortiert: genaue Treffer (Name, Kurzname oder ID), Lieblings-
 * zeitungen, eingeschaltete, Treffer am ersten Wort, dann nach Name.
 * @param {Array} sources  aus /api/sources ({id, name, short, enabled, is_favorite, …})
 * @param {string} q       Suchanfrage wie eingegeben
 */
export function matchSources(sources, q) {
  const query = String(q || '').trim();
  if (query.length < 2 || !Array.isArray(sources)) return [];
  const qforms = queryForms(query);
  if (!qforms.length) return [];
  const hits = [];
  for (const s of sources) {
    const m = s && matchOne(s, qforms);
    if (m) hits.push({ s, ...m });
  }
  const rank = (x) => [x.exact ? 0 : 1, x.s.is_favorite ? 0 : 1, x.s.enabled === false ? 1 : 0, x.first ? 0 : 1];
  hits.sort((a, b) => {
    const ra = rank(a), rb = rank(b);
    for (let i = 0; i < ra.length; i++) if (ra[i] !== rb[i]) return ra[i] - rb[i];
    return sortName(a.s).localeCompare(sortName(b.s), 'de');
  });
  return hits.map((x) => x.s);
}

/** Wörter des Kurznamens (sonst Name) in Großbuchstaben, ohne Akzente (Doto hat nicht jedes
 *  Zeichen), je Wort nur Buchstaben und Ziffern. */
function monoWords(s) {
  const text = stripMarks(String((s && (s.short || s.name)) || '').normalize('NFC')).toUpperCase();
  return text.split(/\s+/).map((w) => w.replace(/[^\p{L}\p{N}]/gu, '')).filter(Boolean);
}

/** Monogramm im Kreis: „GLOBE & MAIL“ → GM, „LOCAL DE“ → LDE (Ländercode ganz), „ABC AU“ → ABC
 *  (kurzes erstes Wort ganz), SZ, „N-TV“ → NTV, „SPIEGEL“ → SP, „HÜRRIYET“ → HU. */
export function monogram(s) {
  const w = monoWords(s);
  if (w.length >= 2 && /^\p{L}{2}$/u.test(w[1])) return w[0].length <= 3 ? w[0] : w[0][0] + w[1];
  if (w.length >= 2) return w[0][0] + w[1][0];
  const chars = w[0] || '';
  return chars.length <= 3 ? chars : chars.slice(0, 2);
}

/** Dreistellige Form bei Gleichstand: „TEHRAN TIMES“ → TET, „FOCUS“ → FOC. */
function longMonogram(s) {
  const base = monogram(s);
  if (base.length >= 3) return base;
  const w = monoWords(s);
  if (w.length >= 2) return w[0].slice(0, 2) + w[1][0];
  return (w[0] || '').slice(0, 3);
}

/** Wie oft kommt jeder Wert der Map vor? */
function tally(map) {
  const n = new Map();
  for (const m of map.values()) n.set(m, (n.get(m) || 0) + 1);
  return n;
}

/** Letzter Ausweg bei Gleichstand eines Worts: erster Buchstabe, dann die Stelle, an der sich die
 *  gleichen trennen, mit dem Zeichen davor („TAGESSCHAU“/„TAGESSPIEGEL“ → TSC/TSP). */
function splitMonogram(word, others) {
  let at = 1;
  for (const o of others) {
    let i = 0;
    while (i < word.length && word[i] === o[i]) i += 1;
    at = Math.max(at, i);
  }
  return at < word.length ? word[0] + word[at - 1] + word[at] : null;
}

/** Monogramme einer angezeigten Liste (Map id → Text): Gleiche werden dreistellig; sind einwortige
 *  dann immer noch gleich, zählt die Stelle, an der sie sich trennen; sonst bleibt es dabei. */
export function monograms(list) {
  const items = (list || []).filter(Boolean);
  const base = new Map(items.map((s) => [s.id, monogram(s)]));
  const n1 = tally(base);
  const long = new Map(items.map((s) => [s.id, n1.get(base.get(s.id)) > 1 ? longMonogram(s) : base.get(s.id)]));
  const n2 = tally(long);
  const out = new Map();
  for (const s of items) {
    const m = long.get(s.id);
    const w = monoWords(s);
    if (n2.get(m) > 1 && w.length === 1) {
      const others = items.filter((o) => o.id !== s.id && long.get(o.id) === m)
        .map((o) => monoWords(o)).filter((ow) => ow.length === 1).map((ow) => ow[0]);
      const split = others.length ? splitMonogram(w[0], others) : null;
      out.set(s.id, split || m);
    } else out.set(s.id, m);
  }
  return out;
}
