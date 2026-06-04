"""
app/dashboard.py

Store Intelligence Dashboard
Served at GET /dashboard?store_id=STORE_BLR_002

Polls /stores/{store_id}/metrics, /stores/{store_id}/anomalies, and /health
every 2 seconds. No framework dependency — pure HTML/CSS/JS returned as HTMLResponse.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Store Intelligence · Live Dashboard</title>
<style>
  :root {
    --bg:        #0c0d0f;
    --surface:   #13151a;
    --surface2:  #1c1f27;
    --border:    #252830;
    --accent:    #00e5a0;
    --accent2:   #ff5c5c;
    --accent3:   #f5c542;
    --muted:     #5a6070;
    --text:      #e2e5ec;
    --text-dim:  #8a90a0;
    --mono:      'IBM Plex Mono', monospace;
    --sans:      'Syne', sans-serif;
    --radius:    6px;
    --card-gap:  16px;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    min-height: 100vh;
    padding: 24px;
  }

  /* ── Header ── */
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 28px;
    padding-bottom: 18px;
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
    gap: 12px;
  }
  .header-left { display: flex; flex-direction: column; gap: 4px; }
  .logo {
    font-family: var(--sans);
    font-weight: 800;
    font-size: 1.35rem;
    letter-spacing: -0.03em;
    color: var(--accent);
  }
  .logo span { color: var(--text-dim); font-weight: 400; }
  .subtitle {
    font-family: var(--mono);
    font-size: 0.7rem;
    color: var(--muted);
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  /* ── Controls bar ── */
  .controls {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }
  .store-input-wrap {
    display: flex;
    align-items: center;
    gap: 8px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 6px 12px;
  }
  .store-input-wrap label {
    font-family: var(--mono);
    font-size: 0.68rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    white-space: nowrap;
  }
  #storeInput {
    background: transparent;
    border: none;
    outline: none;
    color: var(--accent);
    font-family: var(--mono);
    font-size: 0.82rem;
    width: 180px;
  }

  .btn {
    background: var(--accent);
    color: #000;
    border: none;
    border-radius: var(--radius);
    padding: 7px 16px;
    font-family: var(--mono);
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    cursor: pointer;
    transition: opacity 0.15s;
  }
  .btn:hover { opacity: 0.85; }
  .btn:active { opacity: 0.7; }

  /* ── Feed pill ── */
  #feedPill {
    display: flex;
    align-items: center;
    gap: 6px;
    font-family: var(--mono);
    font-size: 0.7rem;
    padding: 5px 12px;
    border-radius: 20px;
    border: 1px solid var(--border);
    background: var(--surface2);
    color: var(--muted);
    letter-spacing: 0.05em;
    text-transform: uppercase;
    transition: all 0.3s;
  }
  #feedPill.live   { color: var(--accent);  border-color: var(--accent);  background: rgba(0,229,160,0.08); }
  #feedPill.stale  { color: var(--accent3); border-color: var(--accent3); background: rgba(245,197,66,0.08); }
  #feedPill.error  { color: var(--accent2); border-color: var(--accent2); background: rgba(255,92,92,0.08); }
  .pulse {
    width: 7px; height: 7px; border-radius: 50%;
    background: currentColor;
    animation: pulse 1.5s infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50%       { opacity: 0.4; transform: scale(0.7); }
  }

  /* ── Ticker ── */
  .ticker-bar {
    font-family: var(--mono);
    font-size: 0.67rem;
    color: var(--muted);
    display: flex;
    align-items: center;
    gap: 16px;
    margin-bottom: 22px;
    padding: 8px 14px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    flex-wrap: wrap;
  }
  .ticker-bar .sep { color: var(--border); }
  #countdown { color: var(--accent); font-weight: 600; }

  /* ── Error banner ── */
  #errorBanner {
    display: none;
    background: rgba(255,92,92,0.1);
    border: 1px solid var(--accent2);
    border-radius: var(--radius);
    padding: 10px 16px;
    font-family: var(--mono);
    font-size: 0.75rem;
    color: var(--accent2);
    margin-bottom: 20px;
  }

  /* ── Grid ── */
  .grid {
    display: grid;
    gap: var(--card-gap);
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    margin-bottom: 24px;
  }

  /* ── Card ── */
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 18px 20px 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    position: relative;
    overflow: hidden;
    transition: border-color 0.2s;
  }
  .card:hover { border-color: var(--muted); }
  .card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: var(--accent);
    opacity: 0;
    transition: opacity 0.3s;
  }
  .card.accent-green::before  { background: var(--accent);  opacity: 1; }
  .card.accent-red::before    { background: var(--accent2); opacity: 1; }
  .card.accent-yellow::before { background: var(--accent3); opacity: 1; }
  .card.accent-muted::before  { background: var(--muted);   opacity: 1; }

  .card-label {
    font-family: var(--mono);
    font-size: 0.62rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.09em;
  }
  .card-value {
    font-family: var(--sans);
    font-size: 2rem;
    font-weight: 800;
    color: var(--text);
    line-height: 1;
    letter-spacing: -0.04em;
    min-height: 2rem;
    transition: color 0.3s;
  }
  .card-value.green  { color: var(--accent); }
  .card-value.red    { color: var(--accent2); }
  .card-value.yellow { color: var(--accent3); }
  .card-value.dim    { color: var(--muted); }
  .card-sub {
    font-family: var(--mono);
    font-size: 0.67rem;
    color: var(--text-dim);
  }

  /* ── Wide cards row ── */
  .wide-grid {
    display: grid;
    gap: var(--card-gap);
    grid-template-columns: 1fr 1fr;
    margin-bottom: 24px;
  }

  /* ── Anomaly card ── */
  #anomalyCard {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 18px 20px;
  }
  .anomaly-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 14px;
  }
  .anomaly-header .section-title {
    font-family: var(--mono);
    font-size: 0.7rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
  }
  #anomalyBadge {
    font-family: var(--mono);
    font-size: 0.65rem;
    padding: 3px 10px;
    border-radius: 12px;
    background: var(--surface2);
    color: var(--muted);
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }
  #anomalyBadge.sev-high     { background: rgba(255,92,92,0.15);  color: var(--accent2); }
  #anomalyBadge.sev-medium   { background: rgba(245,197,66,0.15); color: var(--accent3); }
  #anomalyBadge.sev-low      { background: rgba(0,229,160,0.1);   color: var(--accent); }

  .anomaly-row {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 6px 14px;
    font-family: var(--mono);
    font-size: 0.72rem;
  }
  .anomaly-row dt { color: var(--muted); white-space: nowrap; }
  .anomaly-row dd { color: var(--text); }
  #anomalyAction {
    margin-top: 12px;
    padding: 10px 14px;
    background: var(--surface2);
    border-left: 3px solid var(--accent3);
    border-radius: 0 4px 4px 0;
    font-family: var(--mono);
    font-size: 0.7rem;
    color: var(--text-dim);
    line-height: 1.5;
  }
  #anomalyAction span { color: var(--accent3); font-weight: 600; }

  /* ── Footer ── */
  footer {
    margin-top: 28px;
    padding-top: 16px;
    border-top: 1px solid var(--border);
    font-family: var(--mono);
    font-size: 0.65rem;
    color: var(--muted);
    display: flex;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
  }
  footer a { color: var(--muted); text-decoration: none; }
  footer a:hover { color: var(--text); }

  /* ── Shimmer skeleton ── */
  .skeleton {
    display: inline-block;
    background: linear-gradient(90deg, var(--surface2) 25%, var(--border) 50%, var(--surface2) 75%);
    background-size: 200% 100%;
    animation: shimmer 1.4s infinite;
    border-radius: 4px;
    min-width: 60px;
    height: 1.8rem;
    vertical-align: middle;
  }
  @keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }

  @media (max-width: 600px) {
    .wide-grid { grid-template-columns: 1fr; }
    .card-value { font-size: 1.6rem; }
  }
</style>
</head>
<body>

<header>
  <div class="header-left">
    <div class="logo">APEX<span> · </span>STORE INTELLIGENCE</div>
    <div class="subtitle">Offline conversion analytics · live feed</div>
  </div>
  <div class="controls">
    <div class="store-input-wrap">
      <label for="storeInput">Store</label>
      <input id="storeInput" type="text" value="STORE_BLR_002" autocomplete="off" spellcheck="false" />
    </div>
    <button class="btn" onclick="applyStore()">APPLY</button>
    <div id="feedPill" class="unknown">
      <div class="pulse"></div>
      <span id="feedPillText">CONNECTING</span>
    </div>
  </div>
</header>

<div class="ticker-bar">
  <span>API&nbsp;<span id="apiStatusInline" style="color:var(--muted)">—</span></span>
  <span class="sep">|</span>
  <span>DB&nbsp;<span id="dbStatusInline" style="color:var(--muted)">—</span></span>
  <span class="sep">|</span>
  <span>Refresh in&nbsp;<span id="countdown" style="color:var(--accent)">2s</span></span>
  <span class="sep">|</span>
  <span>Updated&nbsp;<span id="lastUpdated">—</span></span>
</div>

<div id="errorBanner"></div>

<!-- Primary metrics grid -->
<div class="grid">
  <div class="card accent-green">
    <div class="card-label">Conversion Rate</div>
    <div class="card-value green" id="v-conversion"><span class="skeleton"></span></div>
    <div class="card-sub">Visitors → Purchase</div>
  </div>
  <div class="card accent-muted">
    <div class="card-label">Unique Visitors</div>
    <div class="card-value" id="v-visitors"><span class="skeleton"></span></div>
    <div class="card-sub">This session window</div>
  </div>
  <div class="card accent-muted">
    <div class="card-label">Purchases</div>
    <div class="card-value" id="v-purchases"><span class="skeleton"></span></div>
    <div class="card-sub">POS-correlated conversions</div>
  </div>
  <div class="card accent-yellow">
    <div class="card-label">Queue Depth</div>
    <div class="card-value yellow" id="v-queue"><span class="skeleton"></span></div>
    <div class="card-sub">Current billing queue</div>
  </div>
  <div class="card accent-red">
    <div class="card-label">Abandonment Rate</div>
    <div class="card-value red" id="v-abandon"><span class="skeleton"></span></div>
    <div class="card-sub">Queue joins abandoned</div>
  </div>
  <div class="card accent-muted">
    <div class="card-label">Total Entries</div>
    <div class="card-value" id="v-entries"><span class="skeleton"></span></div>
    <div class="card-sub">ENTRY events (non-staff)</div>
  </div>
  <div class="card accent-muted">
    <div class="card-label">Total Exits</div>
    <div class="card-value" id="v-exits"><span class="skeleton"></span></div>
    <div class="card-sub">EXIT events (non-staff)</div>
  </div>
  <div class="card accent-muted">
    <div class="card-label">Store ID</div>
    <div class="card-value dim" id="v-storeid" style="font-size:1rem;letter-spacing:0;padding-top:8px"><span class="skeleton"></span></div>
    <div class="card-sub" id="v-storeopen">—</div>
  </div>
</div>

<!-- Wide row: API status + latest anomaly -->
<div class="wide-grid">
  <div class="card accent-muted" id="statusCard">
    <div class="card-label">Event / API Status</div>
    <div style="margin-top:8px;display:flex;flex-direction:column;gap:10px">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="font-family:var(--mono);font-size:0.72rem;color:var(--text-dim)">Ingest endpoint</span>
        <span id="s-ingest" style="font-family:var(--mono);font-size:0.72rem;color:var(--muted)">—</span>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="font-family:var(--mono);font-size:0.72rem;color:var(--text-dim)">Metrics endpoint</span>
        <span id="s-metrics" style="font-family:var(--mono);font-size:0.72rem;color:var(--muted)">—</span>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="font-family:var(--mono);font-size:0.72rem;color:var(--text-dim)">Anomalies endpoint</span>
        <span id="s-anomalies" style="font-family:var(--mono);font-size:0.72rem;color:var(--muted)">—</span>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="font-family:var(--mono);font-size:0.72rem;color:var(--text-dim)">Health endpoint</span>
        <span id="s-health" style="font-family:var(--mono);font-size:0.72rem;color:var(--muted)">—</span>
      </div>
    </div>
  </div>

  <div id="anomalyCard">
    <div class="anomaly-header">
      <div class="section-title">Latest Anomaly</div>
      <div id="anomalyBadge">NONE</div>
    </div>
    <dl class="anomaly-row" id="anomalyDetails">
      <dt>Type</dt>   <dd id="an-type">—</dd>
      <dt>Zone</dt>   <dd id="an-zone">—</dd>
      <dt>Detail</dt> <dd id="an-detail">—</dd>
      <dt>At</dt>     <dd id="an-time">—</dd>
    </dl>
    <div id="anomalyAction"><span>Suggested action:</span> —</div>
  </div>
</div>

<footer>
  <span>Store Intelligence · Apex Retail · Hackathon Build</span>
  <span><a href="/docs" target="_blank">/docs</a> · <a href="/health" target="_blank">/health</a></span>
</footer>

<script>
(function () {
  const BASE = window.location.origin;
  let storeId = new URLSearchParams(window.location.search).get('store_id') || 'STORE_BLR_002';
  let countdownVal = 2;
  let timer = null;
  let tickTimer = null;

  document.getElementById('storeInput').value = storeId;

  function applyStore() {
    storeId = document.getElementById('storeInput').value.trim() || storeId;
    const url = new URL(window.location.href);
    url.searchParams.set('store_id', storeId);
    window.history.replaceState(null, '', url.toString());
    refresh();
  }
  window.applyStore = applyStore;

  /* ── helpers ── */
  function setVal(id, val, cls) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = val;
    if (cls) el.className = 'card-value ' + cls;
  }

  function setStatus(id, ok) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = ok ? '✓ OK' : '✗ ERR';
    el.style.color = ok ? 'var(--accent)' : 'var(--accent2)';
  }

  function setFeed(status) {
    const pill = document.getElementById('feedPill');
    const text = document.getElementById('feedPillText');
    pill.className = '';
    if (status === 'LIVE')         { pill.classList.add('live');  text.textContent = 'LIVE'; }
    else if (status === 'STALE_FEED' || status === 'STALE') { pill.classList.add('stale'); text.textContent = 'STALE'; }
    else if (status === 'ERROR')   { pill.classList.add('error'); text.textContent = 'ERROR'; }
    else                           { text.textContent = status || 'UNKNOWN'; }
  }

  function showError(msg) {
    const b = document.getElementById('errorBanner');
    b.style.display = msg ? 'block' : 'none';
    b.textContent = msg || '';
  }

  function fmt(n, isPercent) {
    if (n === null || n === undefined) return '—';
    if (isPercent) return (parseFloat(n) * 100).toFixed(1) + '%';
    return String(n);
  }

  function severityClass(sev) {
    if (!sev) return '';
    const s = sev.toLowerCase();
    if (s === 'high' || s === 'critical') return 'sev-high';
    if (s === 'medium' || s === 'warn')   return 'sev-medium';
    return 'sev-low';
  }

  function suggestAction(anomaly) {
    if (!anomaly) return '—';
    const t = (anomaly.anomaly_type || anomaly.type || '').toLowerCase();
    if (t.includes('queue') || t.includes('abandon'))
      return 'Open additional billing counter or dispatch floor staff immediately.';
    if (t.includes('crowd') || t.includes('density'))
      return 'Dispatch staff to manage crowd flow and prevent bottleneck.';
    if (t.includes('dwell') || t.includes('loiter'))
      return 'Flag zone for staff check-in or review camera feed.';
    if (t.includes('conversion') || t.includes('drop'))
      return 'Review zone merchandising and signage for friction points.';
    if (t.includes('stale') || t.includes('feed'))
      return 'Check camera connectivity and pipeline health.';
    return 'Review event stream and alert ops team.';
  }

  /* ── fetch & update ── */
  async function refresh() {
    const ts = new Date().toLocaleTimeString();
    document.getElementById('lastUpdated').textContent = ts;

    /* ── health ── */
    let healthOk = false;
    try {
      const h = await fetch(BASE + '/health').then(r => r.json());
      healthOk = true;
      setStatus('s-health', true);
      const storeHealth = h.stores?.[storeId];
      const feedStatus  = storeHealth?.feed_status || 'UNKNOWN';
      setFeed(feedStatus);

      const apiStatus = h.status || 'unknown';
      const dbStatus  = h.db    || 'unknown';
      document.getElementById('apiStatusInline').textContent = apiStatus;
      document.getElementById('apiStatusInline').style.color = apiStatus === 'ok' ? 'var(--accent)' : 'var(--accent2)';
      document.getElementById('dbStatusInline').textContent  = dbStatus;
      document.getElementById('dbStatusInline').style.color  = dbStatus === 'ok'  ? 'var(--accent)' : 'var(--accent2)';
      showError(null);
    } catch (e) {
      setStatus('s-health', false);
      setFeed('ERROR');
      showError('⚠  Cannot reach API at ' + BASE + ' — is the server running?');
    }

    /* ── metrics ── */
    try {
      const m = await fetch(BASE + `/stores/${storeId}/metrics`).then(r => { if (!r.ok) throw r; return r.json(); });
      setStatus('s-metrics', true);

      const conv = m.conversion_rate;
      const convFloat = parseFloat(conv);
      setVal('v-conversion', fmt(conv, true), convFloat >= 0.25 ? 'green' : convFloat >= 0.1 ? 'yellow' : 'red');
      setVal('v-visitors',  fmt(m.unique_visitors));
      setVal('v-purchases', fmt(m.purchases));
      setVal('v-entries',   fmt(m.total_entries));
      setVal('v-exits',     fmt(m.total_exits));

      const q = m.current_queue_depth;
      setVal('v-queue',  fmt(q), parseInt(q) > 5 ? 'red' : parseInt(q) > 2 ? 'yellow' : 'green');

      const ab = m.abandonment_rate;
      const abFloat = parseFloat(ab);
      setVal('v-abandon', fmt(ab, false) + '%', abFloat > 40 ? 'red' : abFloat > 20 ? 'yellow' : 'green');

      const sid = m.store_id || storeId;
      document.getElementById('v-storeid').textContent = sid;
      document.getElementById('v-storeopen').textContent = m.open_hours || '—';
    } catch (e) {
      setStatus('s-metrics', false);
      ['v-conversion','v-visitors','v-purchases','v-entries','v-exits','v-queue','v-abandon'].forEach(id => {
        document.getElementById(id).textContent = '—';
      });
    }

    /* ── anomalies ── */
    try {
      const data = await fetch(BASE + `/stores/${storeId}/anomalies`).then(r => { if (!r.ok) throw r; return r.json(); });
      setStatus('s-anomalies', true);
      const list = Array.isArray(data) ? data : (data.anomalies || []);
      const badge = document.getElementById('anomalyBadge');
      if (list.length === 0) {
        badge.textContent = 'NONE';
        badge.className = '';
        document.getElementById('an-type').textContent   = '—';
        document.getElementById('an-zone').textContent   = '—';
        document.getElementById('an-detail').textContent = '—';
        document.getElementById('an-time').textContent   = '—';
        document.getElementById('anomalyAction').innerHTML = '<span>Suggested action:</span> No anomalies detected — system nominal.';
      } else {
        const a = list[list.length - 1];
        const sev = a.severity || a.level || 'low';
        badge.textContent = sev.toUpperCase();
        badge.className   = severityClass(sev);
        document.getElementById('an-type').textContent   = a.anomaly_type || a.type        || '—';
        document.getElementById('an-zone').textContent   = a.zone_id      || a.zone        || '—';
        document.getElementById('an-detail').textContent = a.detail       || a.description || '—';
        document.getElementById('an-time').textContent   = a.detected_at  || a.timestamp   || '—';
        document.getElementById('anomalyAction').innerHTML = '<span>Suggested action:</span> ' + suggestAction(a);
      }
    } catch (e) {
      setStatus('s-anomalies', false);
    }

    setStatus('s-ingest', healthOk);
  }

  /* ── countdown ticker ── */
  function startTick() {
    clearInterval(tickTimer);
    countdownVal = 2;
    document.getElementById('countdown').textContent = countdownVal + 's';
    tickTimer = setInterval(() => {
      countdownVal--;
      if (countdownVal <= 0) countdownVal = 2;
      document.getElementById('countdown').textContent = countdownVal + 's';
    }, 1000);
  }

  /* ── poll loop ── */
  function startPolling() {
    refresh();
    startTick();
    clearInterval(timer);
    timer = setInterval(() => { refresh(); startTick(); }, 2000);
  }

  document.getElementById('storeInput').addEventListener('keydown', e => {
    if (e.key === 'Enter') applyStore();
  });

  startPolling();
})();
</script>
</body>
</html>
"""


@router.get("/dashboard", response_class=HTMLResponse, tags=["Dashboard"])
async def dashboard(store_id: str = "STORE_BLR_002"):
    """
    Live store intelligence dashboard.

    Serves a self-contained HTML/JS page that polls:
      - GET /stores/{store_id}/metrics     every 2 s
      - GET /stores/{store_id}/anomalies   every 2 s
      - GET /health                         every 2 s

    Query params:
      store_id: target store identifier (default STORE_BLR_002)

    Example:
      http://localhost:8000/dashboard?store_id=STORE_BLR_002
    """
    # Inject default store_id into the page via JS variable substitution.
    html = DASHBOARD_HTML.replace(
        "value=\"STORE_BLR_002\"",
        f'value="{store_id}"'
    ).replace(
        "|| 'STORE_BLR_002'",
        f"|| '{store_id}'"
    )
    return HTMLResponse(content=html)
