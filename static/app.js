const $ = s => document.querySelector(s);
async function refresh() {
  const r = await fetch('/api/status');
  if (r.status === 401) { $('#login').hidden = false; $('#links').hidden = true; $('#status').hidden = true; return; }
  $('#login').hidden = true; $('#links').hidden = false; $('#status').hidden = false;
  $('#status').textContent = JSON.stringify(await r.json(), null, 2);
}
$('#login').addEventListener('submit', async e => {
  e.preventDefault(); $('#msg').textContent = '';
  const r = await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password: $('#pw').value }) });
  if (r.ok) { $('#pw').value = ''; refresh(); } else { $('#msg').textContent = (await r.json()).detail; $('#msg').className = 'err'; }
});
$('#logout').addEventListener('click', async e => { e.preventDefault(); await fetch('/api/logout', { method: 'POST' }); refresh(); });
refresh();
