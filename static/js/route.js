// Hash-Routen parsen (reine Funktion, ohne DOM; auch in tests/test_frontend_js.py geprüft).

/** '#/profile/4?x=1#zeitungen' → {name, id, key, params, frag, hash} */
export function parseHash(hash) {
  let raw = (hash || '').replace(/^#/, '') || '/';
  let frag = '';
  const cut = raw.indexOf('#');
  if (cut >= 0) { frag = raw.slice(cut + 1); raw = raw.slice(0, cut); }
  const [path, qs = ''] = raw.split('?');
  const params = new URLSearchParams(qs);
  const parts = path.split('/').filter(Boolean).map((p) => {
    try { return decodeURIComponent(p); } catch { return p; }
  });
  const r = { params, frag, hash: `#${raw}`, id: parts[1] };
  switch (parts[0]) {
    case undefined: return { ...r, name: 'list', key: `list:${params.get('p') || ''}` };
    case 'a': return parts[1] && /^\d+$/.test(parts[1]) ? { ...r, name: 'article' } : { ...r, name: 'list', key: 'list:' };
    case 's': return parts[1] ? { ...r, name: 'source', key: `source:${parts[1]}` } : { ...r, name: 'list', key: 'list:' };
    case 'suche': return { ...r, name: 'search', key: 'search' };
    case 'gemerkt': return { ...r, name: 'saved', key: `saved:${params.get('tab') || ''}` };
    case 'einstellungen': return { ...r, name: 'settings', key: 'settings' };
    case 'quellen': return { ...r, name: 'sources', key: 'sources' };
    case 'profile':
      if (!parts[1]) return { ...r, name: 'profiles' };
      return { ...r, name: 'editor', key: `editor:${parts[1]}` };
    case 'login': return { ...r, name: 'login', key: 'login' };
    default: return { ...r, name: 'list', key: 'list:', hash: '#/' };
  }
}
