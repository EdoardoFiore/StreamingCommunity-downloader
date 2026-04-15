/* StreamingCommunity Web Panel — app.js */

// ── State ──────────────────────────────────────────────────────────────────────
let currentDomain = '';
let currentVersion = '';
let currentSource = 'streamingcommunity'; // 'streamingcommunity' | 'animeunity'
let _searchResults = [];
let _libraries = [];
let _jobPhases = {};      // job_id → current phase string
const _jobs = new Map();  // job_id → job dict (source of truth)
let _animeCtx = {};       // context for anime episode browser

// ── Utilities ──────────────────────────────────────────────────────────────────

function escapeHtml(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function formatSize(bytes) {
  if (bytes == null) return '—';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes/1024).toFixed(1) + ' KB';
  if (bytes < 1073741824) return (bytes/1048576).toFixed(1) + ' MB';
  return (bytes/1073741824).toFixed(2) + ' GB';
}
function fmtEta(sec) {
  if (sec == null || sec <= 0) return '';
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return `${m}m ${s.toString().padStart(2,'0')}s`;
  const h = Math.floor(m / 60), rm = m % 60;
  return `${h}h ${rm}m`;
}
function itemYear(item) {
  const d = item.release_date || item.last_air_date || '';
  return d ? d.slice(0, 4) : null;
}
async function safeJson(res) {
  const text = await res.text();
  try { return JSON.parse(text); }
  catch { throw new Error(`HTTP ${res.status}: ${text.slice(0,120)}`); }
}

// ── Modal helpers ──────────────────────────────────────────────────────────────

function showModal(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.style.display = 'block';
  el.classList.add('show');
  el.setAttribute('aria-modal', 'true');
  el.removeAttribute('aria-hidden');
  if (!document.querySelector('.modal-backdrop')) {
    const bd = document.createElement('div');
    bd.className = 'modal-backdrop fade show';
    document.body.appendChild(bd);
  }
  document.body.classList.add('modal-open');
}
function hideModal(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.style.display = 'none';
  el.classList.remove('show');
  el.setAttribute('aria-hidden', 'true');
  el.removeAttribute('aria-modal');
  document.querySelector('.modal-backdrop')?.remove();
  document.body.classList.remove('modal-open');
}
document.addEventListener('click', (e) => {
  if (e.target.classList.contains('modal') && e.target.classList.contains('show'))
    hideModal(e.target.id);
  if (e.target.closest('[data-bs-dismiss="modal"]')) {
    const modal = e.target.closest('.modal');
    if (modal) hideModal(modal.id);
  }
});

// ── Toast ──────────────────────────────────────────────────────────────────────

function showToast(message, type = 'info') {
  const colors = { success:'bg-success', danger:'bg-danger', info:'bg-info', warning:'bg-warning' };
  const toast = document.createElement('div');
  toast.style.cssText = 'position:fixed;bottom:1rem;right:1rem;z-index:9999;min-width:220px';
  toast.innerHTML = `<div class="alert ${colors[type]||'bg-info'} alert-dismissible text-white mb-0 shadow" role="alert">
    ${escapeHtml(message)}
    <button type="button" class="btn-close btn-close-white" onclick="this.closest('.alert').parentElement.remove()"></button>
  </div>`;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 4000);
}

// ── Init ───────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
  await loadDomainStatus();
  await loadLibraries();
  connectGlobalStream();
  setupFileManager();
  setupSearchDebounce();
});

// ── Domain ─────────────────────────────────────────────────────────────────────

async function loadDomainStatus() {
  try {
    const res = await fetch('/api/domain');
    const data = await safeJson(res);
    currentDomain = data.domain || '';
    currentVersion = data.version || '';
    const badge = document.getElementById('domain-badge');
    if (data.valid) {
      badge.className = 'badge bg-success';
      badge.textContent = currentDomain;
    } else {
      badge.className = 'badge bg-danger';
      badge.textContent = 'Domain non configurato';
      openSettings();
    }
  } catch(e) { console.error('loadDomainStatus:', e); }
}

// ── Source selector ────────────────────────────────────────────────────────────

function setSource(src) {
  currentSource = src;
  document.getElementById('src-sc').classList.toggle('active', src === 'streamingcommunity');
  document.getElementById('src-au').classList.toggle('active', src === 'animeunity');
  const input = document.getElementById('search-input');
  if (input) input.placeholder = src === 'animeunity' ? 'Cerca anime...' : 'Film, serie TV...';
  document.getElementById('search-results').innerHTML = '';
}

// ── Navigation ─────────────────────────────────────────────────────────────────

function showPage(page) {
  ['search','downloads','files'].forEach(p => {
    document.getElementById(`page-${p}`).style.display = p === page ? '' : 'none';
  });
  document.getElementById('page-title').textContent =
    { search:'Cerca', downloads:'Download', files:'File' }[page];
  document.querySelectorAll('.nav-link[data-page]').forEach(el =>
    el.classList.toggle('active', el.dataset.page === page));
  if (page === 'files') loadFiles();
}

// ── Settings ───────────────────────────────────────────────────────────────────

function openSettings() {
  document.getElementById('domain-input').value = currentDomain;
  document.getElementById('domain-feedback').textContent = '';
  renderLibrariesList();
  showModal('settings-modal');
}
async function saveDomain() {
  const domain = document.getElementById('domain-input').value.trim();
  const feedback = document.getElementById('domain-feedback');
  const btn = document.getElementById('save-domain-btn');
  if (!domain) return;
  btn.disabled = true;
  feedback.textContent = 'Verifica in corso...';
  feedback.className = 'form-text text-muted';
  try {
    const res = await fetch('/api/domain', {
      method:'PUT', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({domain}),
    });
    const data = await safeJson(res);
    if (res.ok) {
      currentDomain = data.domain; currentVersion = data.version;
      feedback.textContent = `OK — versione ${data.version}`;
      feedback.className = 'form-text text-success';
      const badge = document.getElementById('domain-badge');
      badge.className = 'badge bg-success';
      badge.textContent = data.domain;
      setTimeout(() => hideModal('settings-modal'), 800);
    } else {
      feedback.textContent = data.detail || 'Errore';
      feedback.className = 'form-text text-danger';
    }
  } catch(e) {
    feedback.textContent = 'Errore di rete'; feedback.className = 'form-text text-danger';
  } finally { btn.disabled = false; }
}

// ── Libraries ──────────────────────────────────────────────────────────────────

async function loadLibraries() {
  try {
    const res = await fetch('/api/domain/libraries');
    const data = await safeJson(res);
    _libraries = data.libraries || [];
    const excl = (data.excluded_folders || []).join(', ');
    const inp = document.getElementById('excluded-input');
    if (inp) inp.value = excl;
  } catch(e) { console.error('loadLibraries:', e); }
}
function renderLibrariesList() {
  const c = document.getElementById('libraries-list');
  if (!c) return;
  if (!_libraries.length) { c.innerHTML = '<p class="text-muted small mb-0">Nessuna libreria.</p>'; return; }
  c.innerHTML = _libraries.map((lib, i) => `
    <div class="row g-2 mb-2 align-items-center">
      <div class="col-4"><input type="text" class="form-control form-control-sm" id="lib-name-${i}" value="${escapeHtml(lib.name)}" placeholder="Nome"></div>
      <div class="col"><input type="text" class="form-control form-control-sm" id="lib-path-${i}" value="${escapeHtml(lib.path)}" placeholder="/srv/nfs/films"></div>
      <div class="col-auto"><button class="btn btn-sm btn-outline-danger" onclick="removeLibrary(${i})"><i class="ti ti-trash"></i></button></div>
    </div>`).join('');
}
function _syncLibs() {
  _libraries = _libraries.map((_,i) => ({
    name: document.getElementById(`lib-name-${i}`)?.value||'',
    path: document.getElementById(`lib-path-${i}`)?.value||'',
  }));
}
function addLibrary() {
  _syncLibs(); _libraries.push({name:'',path:''}); renderLibrariesList();
  document.getElementById(`lib-name-${_libraries.length-1}`)?.focus();
}
function removeLibrary(idx) { _syncLibs(); _libraries.splice(idx,1); renderLibrariesList(); }
async function saveLibraries() {
  const updated = _libraries.map((_,i) => ({
    name:(document.getElementById(`lib-name-${i}`)?.value||'').trim(),
    path:(document.getElementById(`lib-path-${i}`)?.value||'').trim(),
  })).filter(l => l.name && l.path);
  const excluded = (document.getElementById('excluded-input')?.value||'').split(',').map(s=>s.trim()).filter(Boolean);
  const btn = document.getElementById('save-libraries-btn');
  btn.disabled = true;
  try {
    const res = await fetch('/api/domain/libraries', {
      method:'PUT', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({libraries:updated, excluded_folders:excluded}),
    });
    if (res.ok) { _libraries=updated; showToast('Librerie salvate','success'); hideModal('settings-modal'); }
    else { const d=await safeJson(res); showToast(d.detail||'Errore','danger'); }
  } catch(e) { showToast('Errore di rete','danger'); }
  finally { btn.disabled = false; }
}

// ── Search ─────────────────────────────────────────────────────────────────────

let _searchAbort = null;
let _searchDebounceTimer = null;

function setupSearchDebounce() {
  const input = document.getElementById('search-input');
  if (!input) return;
  input.addEventListener('input', () => {
    clearTimeout(_searchDebounceTimer);
    const q = input.value.trim();
    if (q.length >= 3) {
      _searchDebounceTimer = setTimeout(() => doSearch(), 400);
    }
  });
}

function _showSearchSkeletons() {
  const container = document.getElementById('search-results');
  container.innerHTML = '';
  for (let i = 0; i < 6; i++) {
    const col = document.createElement('div');
    col.className = 'col-6 col-sm-4 col-md-3 col-lg-2';
    col.innerHTML = '<div class="skeleton skeleton-card"></div>';
    container.appendChild(col);
  }
}

async function doSearch() {
  const q = document.getElementById('search-input').value.trim();
  if (!q) return;
  if (!currentDomain && currentSource !== 'animeunity') { openSettings(); return; }
  // Cancel previous in-flight request
  if (_searchAbort) _searchAbort.abort();
  _searchAbort = new AbortController();
  const btn = document.getElementById('search-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Cerca';
  _showSearchSkeletons();
  try {
    const searchParams = new URLSearchParams({ q, source: currentSource });
    if (currentSource !== 'animeunity') searchParams.set('domain', currentDomain);
    const res = await fetch(`/api/search?${searchParams}`, {signal: _searchAbort.signal});
    const container = document.getElementById('search-results');
    const results = await safeJson(res);
    if (!res.ok) { container.innerHTML=`<div class="col-12"><div class="alert alert-danger">${results.detail||'Errore'}</div></div>`; return; }
    if (!results.length) { container.innerHTML='<div class="col-12"><p class="text-muted">Nessun risultato.</p></div>'; return; }
    container.innerHTML = '';
    results.forEach((item, idx) => {
      const isMovie = item.type==='movie';
      const year = itemYear(item);
      const score = item.score ? parseFloat(item.score).toFixed(1) : null;
      const posterUrl = item.poster
        ? (item.poster.startsWith('http') ? item.poster : `/api/image/${currentDomain}/${item.poster}`)
        : '';
      const card = document.createElement('div');
      card.className = 'col-6 col-sm-4 col-md-3 col-lg-2';
      const posterHtml = posterUrl
        ? `<img src="${posterUrl}" alt="" onerror="this.closest('.poster-wrap').querySelector('.poster-noimg').style.display='flex';this.style.display='none'">`
        : '';
      card.innerHTML = `
        <div class="result-card" onclick="openDetailModal(${idx})">
          <div class="poster-wrap">
            ${posterHtml}
            <div class="poster-noimg" style="${posterUrl?'display:none':''}">&#127916;</div>
            <div class="poster-overlay"></div>
            <div class="poster-play"><i class="ti ti-player-play-filled" style="font-size:16px"></i></div>
          </div>
          <div class="card-meta">
            <div class="card-title-text" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</div>
            <div class="card-badges">
              <span class="badge ${isMovie?'bg-blue-lt':'bg-green-lt'}">${isMovie?'Film':'TV'}</span>
              ${score?`<span class="badge bg-yellow-lt">★ ${score}</span>`:''}
              ${year?`<span style="font-size:10px;color:var(--text-muted)">${year}</span>`:''}
            </div>
          </div>
        </div>`;
      container.appendChild(card);
    });
    _searchResults = results;
  } catch(e) {
    if (e.name === 'AbortError') return; // cancelled by new search
    const container = document.getElementById('search-results');
    container.innerHTML=`<div class="col-12"><div class="alert alert-danger">Errore: ${escapeHtml(e.message)}</div></div>`;
  } finally {
    btn.disabled=false; btn.innerHTML='<i class="ti ti-search me-1"></i>Cerca';
  }
}

// ── Detail Modal ───────────────────────────────────────────────────────────────

const LANG_NAMES = {
  ita:'Italiano', eng:'English', fra:'Français', spa:'Español',
  deu:'Deutsch', por:'Português', jpn:'日本語', zho:'中文',
  ara:'العربية', rus:'Русский', kor:'한국어',
};
const langName = c => LANG_NAMES[c] || c;

function openDetailModal(idx) {
  const item = _searchResults[idx];
  if (!item) return;
  const isAnime = item.type === 'anime';
  const isMovie = item.type === 'movie';
  const year = itemYear(item);
  const score = item.score ? parseFloat(item.score).toFixed(1) : null;
  const posterUrl = item.poster
    ? (item.poster.startsWith('http') ? item.poster : `/api/image/${currentDomain}/${item.poster}`)
    : '';

  const poster = document.getElementById('detail-poster');
  if (posterUrl) { poster.src=posterUrl; poster.style.display=''; poster.onerror=()=>poster.style.display='none'; }
  else poster.style.display='none';

  document.getElementById('detail-title').textContent = item.name;
  const tb = document.getElementById('detail-type-badge');
  if (isAnime) { tb.className='badge me-1 bg-purple-lt'; tb.textContent='Anime'; }
  else if (isMovie) { tb.className='badge me-1 bg-blue-lt'; tb.textContent='Film'; }
  else { tb.className='badge me-1 bg-green-lt'; tb.textContent='Serie TV'; }
  const ab = document.getElementById('detail-age-badge');
  if (item.age) { ab.textContent=`${item.age}+`; ab.style.display=''; } else ab.style.display='none';

  const meta = [];
  if (year) meta.push(year);
  if (isAnime && item.episodes_count) meta.push(`${item.episodes_count} episodi`);
  else if (!isMovie && item.seasons_count) meta.push(`${item.seasons_count} stagion${item.seasons_count===1?'e':'i'}`);
  document.getElementById('detail-meta').textContent = meta.join(' · ');
  document.getElementById('detail-score').innerHTML = score
    ? `<span class="badge bg-yellow-lt fs-5"><i class="ti ti-star-filled me-1"></i>${score}</span>` : '';

  const btn = document.getElementById('detail-action-btn');
  if (isAnime) {
    btn.className='btn btn-success'; btn.innerHTML='<i class="ti ti-list me-1"></i>Episodi';
    btn.onclick = () => { hideModal('detail-modal'); openAnimeBrowser(item.id, item.name, item.type, year); };
  } else if (isMovie) {
    btn.className='btn btn-primary'; btn.innerHTML='<i class="ti ti-download me-1"></i>Scarica';
    btn.onclick = () => { hideModal('detail-modal'); startFilmDownload(item.id, item.name, year); };
  } else {
    btn.className='btn btn-success'; btn.innerHTML='<i class="ti ti-list me-1"></i>Episodi';
    btn.onclick = () => { hideModal('detail-modal'); openEpisodeBrowser(item.id, item.name, item.slug, year); };
  }

  const langsEl = document.getElementById('detail-langs');
  if (isAnime) {
    langsEl.innerHTML = '';
    showModal('detail-modal');
    return;
  }

  langsEl.innerHTML='<span class="spinner-border spinner-border-sm me-1"></span>Caricamento lingue...';
  showModal('detail-modal');

  const p = new URLSearchParams({ type:isMovie?'movie':'tv', domain:currentDomain, slug:item.slug||'', version:currentVersion||'' });
  fetch(`/api/search/languages/${item.id}?${p}`)
    .then(r => r.ok ? r.json() : null)
    .then(info => {
      if (!info) { langsEl.innerHTML=''; return; }
      let html='';
      const audioHtml = (info.audio?.length)
        ? info.audio.map(c=>`<span class="badge bg-blue-lt me-1">${langName(c)}</span>`).join('')
        : `<span class="text-muted fst-italic">originale</span>`;
      html+=`<div class="mb-1"><span class="text-muted me-1"><i class="ti ti-volume ti-sm"></i> Audio:</span>${audioHtml}</div>`;
      if (info.subtitles?.length) {
        html+=`<div><span class="text-muted me-1"><i class="ti ti-subtitles ti-sm"></i> Sub:</span>${info.subtitles.map(c=>`<span class="badge bg-teal-lt me-1">${langName(c)}</span>`).join('')}</div>`;
      }
      langsEl.innerHTML=html;
    })
    .catch(()=>{ langsEl.innerHTML=''; });
}

// ── Film download ──────────────────────────────────────────────────────────────

async function startFilmDownload(id, title, year=null) {
  try {
    const res = await fetch('/api/download/film', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id, title, year, domain:currentDomain}),
    });
    const data = await safeJson(res);
    if (res.ok) { showToast(`Download avviato: ${title}`,'success'); showPage('downloads'); }
    else showToast(data.detail||'Errore','danger');
  } catch(e) { showToast('Errore di rete','danger'); }
}

// ── Episode Browser ────────────────────────────────────────────────────────────

let _epCtx = {};

async function openEpisodeBrowser(tvId, tvName, slug, year=null) {
  _epCtx = { tvId, tvName, slug, year, token:null, episodes:[], currentSeason:null };
  document.getElementById('episode-modal-title').textContent = tvName;
  document.getElementById('season-tabs').style.display='none';
  document.getElementById('episode-modal-body').innerHTML =
    '<div class="text-center py-4"><div class="spinner-border text-primary" role="status"></div></div>';
  showModal('episode-modal');

  try {
    const [tokenData, seasonsData] = await Promise.all([
      fetch(`/api/tv/${tvId}/token?domain=${currentDomain}`).then(r=>r.json()),
      fetch(`/api/tv/${tvId}/seasons?slug=${encodeURIComponent(slug)}&domain=${currentDomain}&version=${encodeURIComponent(currentVersion)}`).then(r=>r.json()),
    ]);
    _epCtx.token = tokenData.token;
    renderSeasonTabs(seasonsData.seasons_count);
    loadSeason(1);
  } catch(e) {
    document.getElementById('episode-modal-body').innerHTML=`<div class="alert alert-danger">Errore: ${escapeHtml(e.message)}</div>`;
  }
}

function renderSeasonTabs(count) {
  const tabs = document.getElementById('season-tabs');
  tabs.innerHTML='';
  for (let s=1; s<=count; s++) {
    const li=document.createElement('li');
    li.className='nav-item';
    li.innerHTML=`<a class="nav-link${s===1?' active':''}" href="#" data-season="${s}">S${s}</a>`;
    li.querySelector('a').addEventListener('click', (e)=>{
      e.preventDefault();
      tabs.querySelectorAll('.nav-link').forEach(a=>a.classList.remove('active'));
      e.target.classList.add('active');
      loadSeason(s);
    });
    tabs.appendChild(li);
  }
  tabs.style.display='flex';
}

async function loadSeason(season) {
  const { tvId, slug, token } = _epCtx;
  const container = document.getElementById('episode-modal-body');
  container.innerHTML='<div class="text-center py-3"><div class="spinner-border text-primary" role="status"></div></div>';
  try {
    const res = await fetch(`/api/tv/${tvId}/seasons/${season}/episodes?slug=${encodeURIComponent(slug)}&domain=${currentDomain}&version=${encodeURIComponent(currentVersion)}&token=${encodeURIComponent(token)}`);
    const eps = await safeJson(res);
    _epCtx.episodes=eps; _epCtx.currentSeason=season;

    const rows = eps.map((ep, idx) => `
      <tr>
        <td class="text-muted w-1 text-nowrap">${ep.n}</td>
        <td>${escapeHtml(ep.name)}</td>
        <td class="w-1">
          <button class="btn btn-sm btn-primary" onclick="startEpisodeDownload(${idx})" title="Scarica">
            <i class="ti ti-download"></i>
          </button>
        </td>
      </tr>`).join('');

    container.innerHTML=`
      <div class="d-flex align-items-center justify-content-between mb-2">
        <span class="text-muted small">${eps.length} episodi</span>
        <button class="btn btn-sm btn-outline-success" onclick="downloadWholeSeason(${season})">
          <i class="ti ti-download me-1"></i>Tutta la stagione
        </button>
      </div>
      <div class="table-responsive" style="max-height:380px;overflow-y:auto">
        <table class="table table-sm table-hover">
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  } catch(e) {
    container.innerHTML=`<div class="alert alert-danger">Errore: ${escapeHtml(e.message)}</div>`;
  }
}

async function startEpisodeDownload(epIndex) {
  const { tvId, tvName, year, token, episodes, currentSeason } = _epCtx;
  const ep = episodes[epIndex];
  try {
    const res = await fetch('/api/download/episode', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ tv_id:tvId, eps:episodes, ep_index:epIndex, domain:currentDomain, token, tv_name:tvName, season:currentSeason, year }),
    });
    const data = await safeJson(res);
    if (res.ok) showToast(`In coda: ${tvName} S${String(currentSeason).padStart(2,'0')}E${String(ep.n).padStart(2,'0')}`,'success');
    else showToast(data.detail||'Errore','danger');
  } catch(e) { showToast('Errore di rete','danger'); }
}

async function downloadWholeSeason(season) {
  const { episodes } = _epCtx;
  if (!confirm(`Aggiungere tutti i ${episodes.length} episodi della stagione ${season} alla coda?`)) return;
  for (let i=0; i<episodes.length; i++) {
    await startEpisodeDownload(i);
    await new Promise(r=>setTimeout(r,150));
  }
  showPage('downloads'); hideModal('episode-modal');
}

// ── Anime Browser (AnimeUnity) ─────────────────────────────────────────────────

async function openAnimeBrowser(animeId, animeName, animeType, animeYear = null) {
  // Auto-detect if film (1 episode) but allow user override
  const isAutoFilm = _searchResults.find(r => r.id === animeId)?.episodes_count === 1;
  const effectiveType = (isAutoFilm && animeType === 'anime') ? 'movie' : animeType;
  
  _animeCtx = { animeId, animeName, animeType: effectiveType, animeYear, episodes: [], isAutoFilm };
  document.getElementById('anime-modal-title').textContent = animeName;
  document.getElementById('anime-modal-body').innerHTML =
    '<div class="text-center py-4"><div class="spinner-border text-primary" role="status"></div></div>';
  showModal('anime-modal');

  try {
    const res = await fetch(`/api/anime/${encodeURIComponent(animeId)}/episodes`);
    const episodes = await safeJson(res);
    if (!res.ok) throw new Error(episodes.detail || 'Errore');
    _animeCtx.episodes = episodes;

    if (!episodes.length) {
      document.getElementById('anime-modal-body').innerHTML =
        '<p class="text-muted">Nessun episodio trovato.</p>';
      return;
    }

    const rows = episodes.map((ep, idx) => {
      let epNum = ep.number;
      try { epNum = String(parseFloat(ep.number)); } catch(e) {}
      // If only one episode and it's auto-detected as film, don't show as series
      if (_animeCtx.isAutoFilm && episodes.length === 1) {
        return `
          <tr>
            <td class="text-muted w-1 text-nowrap">Film</td>
            <td class="text-muted" style="font-size:12px">1 episodio</td>
            <td class="w-1">
              <button class="btn btn-sm btn-primary" onclick="startAnimeDownload(${idx})" title="Scarica">
                <i class="ti ti-download"></i>
              </button>
            </td>
          </tr>`;
      }
      return `
        <tr>
          <td class="text-muted w-1 text-nowrap">E${epNum}</td>
          <td class="text-muted" style="font-size:12px">ep. ${epNum}</td>
          <td class="w-1">
            <button class="btn btn-sm btn-primary" onclick="startAnimeDownload(${idx})" title="Scarica">
              <i class="ti ti-download"></i>
            </button>
          </td>
        </tr>`;
    }).join('');

    let typeToggle = '';
    if (_animeCtx.isAutoFilm) {
      const currentType = _animeCtx.animeType === 'movie' ? 'Film' : 'Serie';
      typeToggle = `
        <div class="mb-2 d-flex align-items-center gap-2">
          <span class="text-muted small">Tipo:</span>
          <button class="btn btn-sm ${_animeCtx.animeType === 'movie' ? 'btn-primary' : 'btn-outline-secondary'}"
                  onclick="toggleAnimeType('movie')" title="Film">
            <i class="ti ti-ticket me-1"></i>Film
          </button>
          <button class="btn btn-sm ${_animeCtx.animeType === 'tv' ? 'btn-primary' : 'btn-outline-secondary'}"
                  onclick="toggleAnimeType('tv')" title="Serie">
            <i class="ti ti-list me-1"></i>Serie
          </button>
        </div>`;
    }

    document.getElementById('anime-modal-body').innerHTML = `
      ${typeToggle}
      <div class="d-flex align-items-center justify-content-between mb-2">
        <span class="text-muted small">${episodes.length} episodi</span>
        <button class="btn btn-sm btn-outline-success" onclick="downloadAllAnime()">
          <i class="ti ti-download me-1"></i>Scarica tutti
        </button>
      </div>
      <div class="table-responsive" style="max-height:380px;overflow-y:auto">
        <table class="table table-sm table-hover">
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  } catch(e) {
    document.getElementById('anime-modal-body').innerHTML =
      `<div class="alert alert-danger">Errore: ${escapeHtml(e.message)}</div>`;
  }
}

async function startAnimeDownload(epIndex) {
  const { animeId, animeName, animeType, animeYear, episodes } = _animeCtx;
  const episode = episodes[epIndex];
  try {
    const res = await fetch('/api/download/anime', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ 
        anime_id: animeId, 
        episode, 
        anime_name: animeName, 
        anime_type: animeType,
        year: animeYear
      }),
    });
    const data = await safeJson(res);
    if (res.ok) showToast(`In coda: ${animeName} E${episode.number}`, 'success');
    else showToast(data.detail || 'Errore', 'danger');
  } catch(e) { showToast('Errore di rete', 'danger'); }
}

function toggleAnimeType(newType) {
  _animeCtx.animeType = newType;
  // Ricarica il modal per mostrare il toggle aggiornato
  const { animeId, animeName, animeYear } = _animeCtx;
  openAnimeBrowser(animeId, animeName, newType, animeYear);
}

async function downloadAllAnime() {
  const { episodes } = _animeCtx;
  if (!confirm(`Aggiungere tutti i ${episodes.length} episodi alla coda?`)) return;
  for (let i = 0; i < episodes.length; i++) {
    await startAnimeDownload(i);
    await new Promise(r => setTimeout(r, 150));
  }
  showPage('downloads'); hideModal('anime-modal');
}

// ── Global SSE stream ──────────────────────────────────────────────────────────

function connectGlobalStream() {
  const es = new EventSource('/api/progress/stream');

  es.onopen = () => {
    document.getElementById('stream-label').textContent='Live';
    document.querySelector('.stream-dot').style.background='#2fb344';
  };

  es.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    switch (msg.type) {
      case 'snapshot':
        _jobs.clear();
        msg.jobs.forEach(j => _jobs.set(j.job_id, j));
        renderAllJobCards();
        updateActiveBadge();
        break;
      case 'job_created':
        _jobs.set(msg.job.job_id, msg.job);
        addJobCard(msg.job);
        updateActiveBadge();
        break;
      case 'job_status':
        if (_jobs.has(msg.job_id)) {
          _jobs.get(msg.job_id).status = msg.status;
          refreshCardAppearance(msg.job_id);
          updateActiveBadge();
        }
        break;
      case 'progress':
        handleProgressEvent(msg);
        break;
      case 'status':
        handlePhaseEvent(msg.job_id, msg.phase);
        break;
      case 'done':
        handleDoneEvent(msg.job_id, msg.output_path);
        break;
      case 'error':
        handleErrorEvent(msg.job_id, msg.message);
        break;
    }
  };

  es.onerror = () => {
    es.close();
    document.getElementById('stream-label').textContent='Riconnessione...';
    document.querySelector('.stream-dot').style.background='#d63939';
    setTimeout(connectGlobalStream, 3000);
  };
}

// ── Job cards ──────────────────────────────────────────────────────────────────

const PHASE_LABELS = {
  queued:'In coda', running:'In corso', joining:'Finalizzazione',
  audio:'Audio', merging:'Unione', done:'Completato', error:'Errore', cancelled:'Annullato',
};
const PHASE_BADGE = {
  queued:'bg-secondary-lt', running:'bg-blue-lt', joining:'bg-yellow-lt',
  audio:'bg-teal-lt', merging:'bg-purple-lt', done:'bg-success-lt',
  error:'bg-danger-lt', cancelled:'bg-secondary-lt',
};
const PHASE_BAR = {
  running:'bg-blue', joining:'phase-bar-joining bg-warning',
  audio:'phase-bar-audio bg-teal', merging:'phase-bar-merging bg-purple',
  done:'phase-bar-done bg-success', error:'phase-bar-error bg-danger',
};

function _buildJobCard(j) {
  const phase = _jobPhases[j.job_id] || j.status;
  const isActive = j.status==='running' || j.status==='queued';
  const isMovie = j.type==='film';
  const isAnimeJob = j.type==='anime';
  const pct = j.progress?.pct||0;
  const barClass = PHASE_BAR[phase] || 'bg-secondary';
  const animated = isActive && j.status!=='queued' ? ' progress-bar-striped progress-bar-animated' : '';
  const barWidth = j.status==='queued' ? 0 : (j.status==='done' ? 100 : pct);
  const badgeClass = PHASE_BADGE[phase]||'bg-secondary-lt';
  const label = PHASE_LABELS[phase] || phase;
  const PHASE_BORDER = {
    queued:'var(--text-dim)', running:'var(--blue)', joining:'var(--yellow)',
    audio:'var(--teal)', merging:'var(--purple)', done:'var(--green)',
    error:'var(--accent)', cancelled:'var(--text-dim)',
  };
  const borderColor = PHASE_BORDER[phase] || 'transparent';

  const speed = j.progress?.speed;
  const eta = j.progress?.eta;
  const speedStr = (speed && speed>0 && isActive && j.status!=='queued')
    ? `${speed} seg/s` : '';
  const etaStr = eta ? fmtEta(eta) : '';
  const infoStr = [speedStr, etaStr].filter(Boolean).join(' · ');

  const stopBtn = isActive
    ? `<button class="btn btn-sm btn-outline-danger ms-2" onclick="cancelJob('${j.job_id}')" title="Interrompi">
         <i class="ti ti-player-stop"></i>
       </button>` : '';

  const rawTs = j.created_at;
  const dateStr = rawTs
    ? new Date(/[Z+]/.test(rawTs)?rawTs:rawTs+'Z').toLocaleString('it-IT',{hour:'2-digit',minute:'2-digit'})
    : '';

  return `<div class="card mb-2 job-card${j.status==='done'?' is-done':''}${j.status==='error'?' is-error':''}" id="job-card-${j.job_id}" style="border-left:3px solid ${borderColor} !important">
    <div class="card-body py-2 px-3">
      <div class="d-flex align-items-center gap-2">
        <span class="badge ${isMovie?'bg-blue-lt':isAnimeJob?'bg-purple-lt':'bg-green-lt'} flex-shrink-0">${isMovie?'Film':isAnimeJob?'Anime':'TV'}</span>
        <span class="fw-medium text-truncate flex-1" style="min-width:0" title="${escapeHtml(j.title)}">${escapeHtml(j.title)}</span>
        <span class="badge ${badgeClass} flex-shrink-0" id="job-badge-${j.job_id}">${label}</span>
        ${stopBtn ? `<span id="job-stop-${j.job_id}">${stopBtn}</span>` : `<span id="job-stop-${j.job_id}"></span>`}
      </div>
      <div class="progress my-1" style="height:5px">
        <div class="progress-bar ${barClass}${animated} job-progress-bar" id="job-bar-${j.job_id}" style="width:${barWidth}%"></div>
      </div>
      <div class="d-flex justify-content-between align-items-center">
        <small class="text-muted" id="job-info-${j.job_id}">${infoStr || (j.status==='error' ? escapeHtml(j.error||'Errore') : (j.status==='done'?'Completato':''))}</small>
        <small class="text-muted">${dateStr}</small>
      </div>
    </div>
  </div>`;
}

function renderAllJobCards() {
  const container = document.getElementById('jobs-container');
  const empty = document.getElementById('jobs-empty');
  if (!_jobs.size) {
    empty.style.display=''; container.innerHTML=''; container.appendChild(empty);
    return;
  }
  // Sort: active first, then by created_at desc
  const sorted = [..._jobs.values()].sort((a,b) => {
    const aActive = (a.status==='running'||a.status==='queued')?1:0;
    const bActive = (b.status==='running'||b.status==='queued')?1:0;
    if (aActive!==bActive) return bActive-aActive;
    return new Date(b.created_at)-new Date(a.created_at);
  });
  empty.style.display='none';
  const frag = document.createDocumentFragment();
  sorted.forEach(j => {
    const tmp = document.createElement('div');
    tmp.innerHTML = _buildJobCard(j);
    frag.appendChild(tmp.firstElementChild);
  });
  container.innerHTML = '';
  container.appendChild(frag);
  updateActiveSection();
}

function addJobCard(job) {
  const container = document.getElementById('jobs-container');
  const empty = document.getElementById('jobs-empty');
  empty.style.display='none';
  // Insert at top of container
  const tmp = document.createElement('div');
  tmp.innerHTML = _buildJobCard(job);
  container.insertBefore(tmp.firstElementChild, container.firstChild);
  updateActiveSection();
}

function refreshCardAppearance(jobId) {
  const j = _jobs.get(jobId);
  if (!j) return;
  const card = document.getElementById(`job-card-${jobId}`);
  if (!card) return;

  const phase = _jobPhases[j.job_id] || j.status;
  const isActive = j.status==='running' || j.status==='queued';

  // Update card classes and border
  card.classList.toggle('is-done', j.status==='done');
  card.classList.toggle('is-error', j.status==='error');
  const PHASE_BORDER = {
    queued:'var(--text-dim)', running:'var(--blue)', joining:'var(--yellow)',
    audio:'var(--teal)', merging:'var(--purple)', done:'var(--green)',
    error:'var(--accent)', cancelled:'var(--text-dim)',
  };
  card.style.borderLeftColor = PHASE_BORDER[phase] || 'transparent';

  // Update badge
  const badge = document.getElementById(`job-badge-${jobId}`);
  if (badge) {
    badge.className = `badge ${PHASE_BADGE[phase]||'bg-secondary-lt'} flex-shrink-0`;
    badge.textContent = PHASE_LABELS[phase] || phase;
  }

  // Update progress bar
  const bar = document.getElementById(`job-bar-${jobId}`);
  if (bar) {
    const barClass = PHASE_BAR[phase] || 'bg-secondary';
    const animated = isActive && j.status!=='queued' ? ' progress-bar-striped progress-bar-animated' : '';
    bar.className = `progress-bar ${barClass}${animated} job-progress-bar`;
    bar.style.width = (j.status==='queued' ? 0 : (j.status==='done' ? 100 : (j.progress?.pct||0))) + '%';
  }

  // Update stop button
  const stop = document.getElementById(`job-stop-${jobId}`);
  if (stop) {
    stop.innerHTML = isActive
      ? `<button class="btn btn-sm btn-outline-danger ms-2" onclick="cancelJob('${j.job_id}')" title="Interrompi"><i class="ti ti-player-stop"></i></button>`
      : '';
  }

  // Update info text
  const info = document.getElementById(`job-info-${jobId}`);
  if (info) {
    if (j.status==='error') info.textContent = j.error||'Errore';
    else if (j.status==='done') info.textContent = 'Completato';
    else if (j.status==='cancelled') info.textContent = 'Annullato';
  }

  updateActiveSection();
}

function updateActiveSection() {
  const active = [..._jobs.values()].filter(j=>j.status==='running'||j.status==='queued');
  const pill = document.getElementById('dl-active-pill');
  const countEl = document.getElementById('dl-active-count');
  if (active.length) {
    pill.style.display=''; countEl.textContent=active.length;
  } else {
    pill.style.display='none';
  }
}

function updateActiveBadge() {
  const count = [..._jobs.values()].filter(j=>j.status==='running'||j.status==='queued').length;
  const badge = document.getElementById('active-jobs-badge');
  if (count>0) { badge.style.display=''; badge.textContent=count; }
  else badge.style.display='none';
  updateActiveSection();
}

function handleProgressEvent(msg) {
  const job = _jobs.get(msg.job_id);
  if (job) {
    job.progress = { current:msg.current, total:msg.total, pct:msg.pct, speed:msg.speed||0, eta:msg.eta||null };
    const phase = msg.phase || _jobPhases[msg.job_id] || 'running';
    _jobPhases[msg.job_id] = phase;
  }
  // Update bar and info without full card rebuild
  const bar = document.getElementById(`job-bar-${msg.job_id}`);
  if (bar) bar.style.width = msg.pct + '%';
  const info = document.getElementById(`job-info-${msg.job_id}`);
  if (info) {
    const speedStr = msg.speed>0 ? `${msg.speed} seg/s` : '';
    const etaStr = msg.eta ? fmtEta(msg.eta) : '';
    info.textContent = [speedStr, etaStr, `${msg.pct}%`].filter(Boolean).join(' · ');
  }
}

function handlePhaseEvent(jobId, phase) {
  _jobPhases[jobId] = phase;
  const job = _jobs.get(jobId);
  if (job) job.status = 'running';

  const badge = document.getElementById(`job-badge-${jobId}`);
  if (badge) {
    badge.className = `badge ${PHASE_BADGE[phase]||'bg-secondary-lt'} flex-shrink-0`;
    badge.textContent = PHASE_LABELS[phase] || phase;
  }
  const bar = document.getElementById(`job-bar-${jobId}`);
  if (bar) {
    bar.className = `progress-bar ${PHASE_BAR[phase]||'bg-secondary'} progress-bar-striped progress-bar-animated job-progress-bar`;
    if (phase==='joining'||phase==='merging') bar.style.width='100%';
  }
  const info = document.getElementById(`job-info-${jobId}`);
  if (info && (phase==='joining'||phase==='merging')) {
    info.textContent = PHASE_LABELS[phase]+'...';
  }
}

function handleDoneEvent(jobId, outputPath) {
  delete _jobPhases[jobId];
  const job = _jobs.get(jobId);
  if (job) { job.status='done'; job.output_path=outputPath; }

  const card = document.getElementById(`job-card-${jobId}`);
  if (card) card.classList.add('is-done');
  const badge = document.getElementById(`job-badge-${jobId}`);
  if (badge) { badge.className='badge bg-success-lt flex-shrink-0'; badge.textContent='Completato'; }
  const bar = document.getElementById(`job-bar-${jobId}`);
  if (bar) {
    bar.style.width='100%';
    bar.className='progress-bar phase-bar-done bg-success job-progress-bar';
  }
  const info = document.getElementById(`job-info-${jobId}`);
  if (info) info.textContent='Completato';
  const stop = document.getElementById(`job-stop-${jobId}`);
  if (stop) stop.innerHTML='';

  updateActiveBadge();
  // Refresh file manager if open
  if (document.getElementById('page-files')?.style.display!=='none') loadFiles();
}

function handleErrorEvent(jobId, message) {
  delete _jobPhases[jobId];
  const job = _jobs.get(jobId);
  if (job) { job.status='error'; job.error=message; }

  const card = document.getElementById(`job-card-${jobId}`);
  if (card) card.classList.add('is-error');
  const badge = document.getElementById(`job-badge-${jobId}`);
  if (badge) { badge.className='badge bg-danger-lt flex-shrink-0'; badge.textContent='Errore'; }
  const bar = document.getElementById(`job-bar-${jobId}`);
  if (bar) { bar.className='progress-bar phase-bar-error bg-danger job-progress-bar'; bar.style.width='100%'; }
  const info = document.getElementById(`job-info-${jobId}`);
  if (info) info.textContent = message==='Annullato' ? 'Annullato' : escapeHtml(message||'Errore');
  const stop = document.getElementById(`job-stop-${jobId}`);
  if (stop) stop.innerHTML='';

  updateActiveBadge();
}

async function cancelJob(jobId) {
  if (!confirm('Interrompere il download?')) return;
  try {
    const res = await fetch(`/api/download/${jobId}`, {method:'DELETE'});
    if (!res.ok) { const d=await safeJson(res); showToast(d.detail||'Errore','danger'); }
  } catch(e) { showToast('Errore di rete','danger'); }
}

function clearFinished() {
  for (const [id, j] of _jobs) {
    if (j.status==='done'||j.status==='error'||j.status==='cancelled') {
      _jobs.delete(id);
      document.getElementById(`job-card-${id}`)?.remove();
    }
  }
  if (!_jobs.size) {
    const container = document.getElementById('jobs-container');
    const empty = document.getElementById('jobs-empty');
    empty.style.display='';
    container.innerHTML='';
    container.appendChild(empty);
  }
  updateActiveBadge();
}

// ── File Manager ───────────────────────────────────────────────────────────────

let _expandedFolders = new Set();
let _cachedTree = null;
let _selectedPaths = new Set();
let _draggedPaths = [];
let _allVisiblePaths = [];  // flat list of visible paths for shift-click range
let _lastSelectedIndex = -1;

function setupFileManager() {
  // ── Drag & Drop (supports multi-drag) ──
  document.addEventListener('dragstart', (e) => {
    const row = e.target.closest('[data-drag-path]');
    if (!row) return;
    const path = row.dataset.dragPath;
    // If dragged item is selected, drag all selected; otherwise just the one
    if (_selectedPaths.has(path) && _selectedPaths.size > 1) {
      _draggedPaths = [..._selectedPaths];
    } else {
      _draggedPaths = [path];
    }
    e.dataTransfer.effectAllowed='move';
    e.dataTransfer.setData('text/plain', _draggedPaths.join('\n'));
    // Visual: mark all dragged rows
    _draggedPaths.forEach(p => {
      const el = document.querySelector(`[data-drag-path="${CSS.escape(p)}"]`);
      if (el) el.classList.add('dragging');
    });
  });
  document.addEventListener('dragend', () => {
    document.querySelectorAll('.dragging').forEach(el=>el.classList.remove('dragging'));
    document.querySelectorAll('.drag-over').forEach(el=>el.classList.remove('drag-over'));
    _draggedPaths=[];
  });
  document.addEventListener('dragover', (e) => {
    if (!e.target.closest('.fm-drop-zone')) return;
    e.preventDefault(); e.dataTransfer.dropEffect='move';
  });
  document.addEventListener('dragenter', (e) => {
    const zone = e.target.closest('.fm-drop-zone');
    if (!zone || !_draggedPaths.length) return;
    const dest = zone.dataset.dropPath;
    // Prevent dropping into any of the dragged items
    if (_draggedPaths.some(p => dest===p || dest.startsWith(p+'/'))) return;
    e.preventDefault();
    document.querySelectorAll('.drag-over').forEach(el=>el.classList.remove('drag-over'));
    zone.classList.add('drag-over');
  });
  document.addEventListener('dragleave', (e) => {
    const zone = e.target.closest('.fm-drop-zone');
    if (zone && !zone.contains(e.relatedTarget)) zone.classList.remove('drag-over');
  });
  document.addEventListener('drop', (e) => {
    const zone = e.target.closest('.fm-drop-zone');
    if (!zone) return;
    e.preventDefault(); zone.classList.remove('drag-over');
    const destDirPath = zone.dataset.dropPath;
    if (!_draggedPaths.length||destDirPath===undefined) return;
    if (_draggedPaths.some(p => destDirPath===p||destDirPath.startsWith(p+'/'))) return;
    if (_draggedPaths.length > 1) {
      batchMoveToPath(_draggedPaths, destDirPath);
    } else {
      const name = _draggedPaths[0].split(/[/\\]/).pop();
      moveToPath(_draggedPaths[0], name, destDirPath);
    }
    _draggedPaths=[];
  });

  // ── Click handlers ──
  document.addEventListener('click', (e) => {
    // Checkbox toggle
    const check = e.target.closest('.fm-check');
    if (check) {
      e.stopPropagation();
      const path = check.dataset.selectPath;
      const idx = _allVisiblePaths.indexOf(path);
      if (e.shiftKey && _lastSelectedIndex >= 0 && idx >= 0) {
        // Shift-click: range select
        const start = Math.min(_lastSelectedIndex, idx);
        const end = Math.max(_lastSelectedIndex, idx);
        for (let i = start; i <= end; i++) {
          _selectedPaths.add(_allVisiblePaths[i]);
        }
      } else {
        if (_selectedPaths.has(path)) _selectedPaths.delete(path);
        else _selectedPaths.add(path);
      }
      if (idx >= 0) _lastSelectedIndex = idx;
      syncSelectionUI();
      return;
    }

    // Folder toggle
    const toggle = e.target.closest('.fm-toggle');
    if (toggle) {
      const path = toggle.dataset.folderPath;
      if (_expandedFolders.has(path)) _expandedFolders.delete(path);
      else _expandedFolders.add(path);
      if (_cachedTree) renderFileTree(_cachedTree);
      return;
    }
    const delBtn = e.target.closest('[data-delete-path]');
    if (delBtn && delBtn.closest('#files-left-pane')) {
      deletePath(delBtn.dataset.deletePath, delBtn.dataset.deleteName, !!delBtn.dataset.deleteDir); return;
    }
    const playBtn = e.target.closest('[data-play-path]');
    if (playBtn) playFile(playBtn.dataset.playPath, playBtn.dataset.playName);
  });

  // ── Batch toolbar buttons ──
  const batchMoveBtn = document.getElementById('fm-batch-move-btn');
  if (batchMoveBtn) batchMoveBtn.addEventListener('click', () => {
    if (!_selectedPaths.size) return;
    const dest = prompt('Percorso cartella di destinazione (vuoto = radice):','');
    if (dest === null) return; // cancelled
    batchMoveToPath([..._selectedPaths], dest);
  });
  const batchDeleteBtn = document.getElementById('fm-batch-delete-btn');
  if (batchDeleteBtn) batchDeleteBtn.addEventListener('click', () => {
    if (!_selectedPaths.size) return;
    if (!confirm(`Eliminare ${_selectedPaths.size} elementi selezionati?`)) return;
    batchDeletePaths([..._selectedPaths]);
  });
  const deselectBtn = document.getElementById('fm-deselect-btn');
  if (deselectBtn) deselectBtn.addEventListener('click', () => {
    _selectedPaths.clear();
    _lastSelectedIndex = -1;
    syncSelectionUI();
  });
}

function syncSelectionUI() {
  // Update checkboxes and row highlights
  document.querySelectorAll('.fm-check').forEach(cb => {
    const path = cb.dataset.selectPath;
    cb.checked = _selectedPaths.has(path);
    const row = cb.closest('.fm-row');
    if (row) row.classList.toggle('fm-selected', _selectedPaths.has(path));
  });
  // Update toolbar
  const bar = document.getElementById('fm-selection-bar');
  const count = document.getElementById('fm-selection-count');
  if (bar) bar.style.visibility = _selectedPaths.size ? '' : 'hidden';
  if (count) count.textContent = `${_selectedPaths.size} selezionat${_selectedPaths.size===1?'o':'i'}`;
}

async function loadFiles() {
  const pane = document.getElementById('files-left-pane');
  if (!pane) return;
  // Show skeleton while loading
  if (!_cachedTree) {
    let skeletonHtml = '';
    for (let i = 0; i < 5; i++) skeletonHtml += `<div class="skeleton skeleton-row"></div>`;
    pane.innerHTML = skeletonHtml;
  }
  try {
    const res = await fetch('/api/files');
    const tree = await safeJson(res);
    _cachedTree = tree;
    if (!tree||!tree.length) { pane.innerHTML='<div class="text-muted text-center py-4">Nessun file trovato</div>'; return; }
    renderFileTree(tree);
  } catch(e) {
    pane.innerHTML=`<div class="text-danger text-center py-4">Errore: ${escapeHtml(e.message)}</div>`;
  }
}

function renderFileTree(tree) {
  const pane = document.getElementById('files-left-pane');
  if (!pane) return;
  _allVisiblePaths = [];
  const frag = document.createDocumentFragment();
  const rootZone = document.createElement('div');
  rootZone.className='fm-row fm-drop-zone fm-root-zone';
  rootZone.dataset.dropPath='';
  rootZone.innerHTML=`<span style="min-width:14px;flex-shrink:0"></span>
    <i class="ti ti-home text-muted" style="flex-shrink:0"></i>
    <span class="fm-meta ms-1">radice</span>`;
  frag.appendChild(rootZone);
  renderTreeItems(tree, frag, 0);
  pane.innerHTML='';
  pane.appendChild(frag);
  // Clean stale selections (paths no longer visible)
  for (const p of _selectedPaths) {
    if (!_allVisiblePaths.includes(p)) _selectedPaths.delete(p);
  }
  syncSelectionUI();
}

function renderTreeItems(items, container, depth) {
  items.forEach(item => {
    _allVisiblePaths.push(item.path);
    const row = document.createElement('div');
    row.className='fm-row';
    if (_selectedPaths.has(item.path)) row.classList.add('fm-selected');
    row.style.paddingLeft=`${8+depth*16}px`;
    row.setAttribute('draggable','true');
    row.dataset.dragPath=item.path;
    const checked = _selectedPaths.has(item.path) ? 'checked' : '';
    if (item.type==='directory') {
      const expanded = _expandedFolders.has(item.path);
      row.classList.add('fm-drop-zone');
      row.dataset.dropPath=item.path;
      row.innerHTML=`
        <input type="checkbox" class="fm-check" data-select-path="${escapeHtml(item.path)}" ${checked}>
        <i class="ti ${expanded?'ti-chevron-down':'ti-chevron-right'} text-muted fm-toggle"
           data-folder-path="${escapeHtml(item.path)}"
           style="font-size:1em;cursor:pointer;min-width:22px;flex-shrink:0;padding:4px 3px;margin:-4px -3px"></i>
        <i class="ti ti-folder-filled text-yellow" style="flex-shrink:0"></i>
        <span class="fm-name">${escapeHtml(item.name)}</span>
        <div class="fm-actions">
          <button class="btn btn-sm btn-outline-danger"
                  data-delete-path="${escapeHtml(item.path)}"
                  data-delete-name="${escapeHtml(item.name)}"
                  data-delete-dir="1"><i class="ti ti-trash"></i></button>
        </div>`;
      container.appendChild(row);
      if (expanded && item.children) renderTreeItems(item.children, container, depth+1);
    } else {
      const size = formatSize(item.size);
      const isMp4 = item.name.toLowerCase().endsWith('.mp4');
      row.innerHTML=`
        <input type="checkbox" class="fm-check" data-select-path="${escapeHtml(item.path)}" ${checked}>
        <span style="min-width:14px;flex-shrink:0"></span>
        <i class="ti ${isMp4?'ti-file-type-mp4 text-red':'ti-file text-muted'}" style="flex-shrink:0"></i>
        <span class="fm-name">${escapeHtml(item.name)}</span>
        <span class="fm-meta">${size}</span>
        <div class="fm-actions">
          ${isMp4?`<button class="btn btn-sm btn-outline-primary" data-play-path="${escapeHtml(item.path)}" data-play-name="${escapeHtml(item.name)}"><i class="ti ti-player-play"></i></button>`:''}
          <a class="btn btn-sm btn-outline-secondary" href="/api/files/download/${encodeURI(item.path)}"><i class="ti ti-download"></i></a>
          <button class="btn btn-sm btn-outline-danger" data-delete-path="${escapeHtml(item.path)}" data-delete-name="${escapeHtml(item.name)}"><i class="ti ti-trash"></i></button>
        </div>`;
      container.appendChild(row);
    }
  });
}

async function moveToPath(sourcePath, name, destDirPath) {
  try {
    const res = await fetch('/api/files/move', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path:sourcePath, dest_dir_path:destDirPath}),
    });
    const data = await safeJson(res);
    if (res.ok) { showToast(`Spostato: ${name}`,'success'); loadFiles(); }
    else showToast(data.detail||'Errore spostamento','danger');
  } catch(e) { showToast('Errore di rete','danger'); }
}

async function batchMoveToPath(paths, destDirPath) {
  try {
    const res = await fetch('/api/files/move-batch', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({paths, dest_dir_path:destDirPath}),
    });
    const data = await safeJson(res);
    if (res.ok) {
      const ok = data.results.filter(r=>r.ok).length;
      const fail = data.results.filter(r=>!r.ok).length;
      if (ok) showToast(`${ok} file spostati`,'success');
      if (fail) showToast(`${fail} file non spostati`,'danger');
      _selectedPaths.clear();
      loadFiles();
    } else showToast(data.detail||'Errore spostamento','danger');
  } catch(e) { showToast('Errore di rete','danger'); }
}

async function batchDeletePaths(paths) {
  try {
    const res = await fetch('/api/files/delete-batch', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({paths}),
    });
    const data = await safeJson(res);
    if (res.ok) {
      const ok = data.results.filter(r=>r.ok).length;
      const fail = data.results.filter(r=>!r.ok).length;
      if (ok) showToast(`${ok} file eliminati`,'success');
      if (fail) showToast(`${fail} file non eliminati`,'danger');
      _selectedPaths.clear();
      loadFiles();
    } else showToast(data.detail||'Errore eliminazione','danger');
  } catch(e) { showToast('Errore di rete','danger'); }
}

function playFile(path, name) {
  document.getElementById('player-modal-title').textContent=name;
  const video = document.getElementById('video-player');
  video.src=`/api/files/stream/${encodeURI(path)}`; video.load();
  showModal('player-modal');
  document.getElementById('player-modal').addEventListener('click', (e) => {
    if (e.target.closest('[data-bs-dismiss="modal"]')) { video.pause(); video.src=''; }
  }, {once:true});
}

async function deletePath(path, name, isDir) {
  const msg = isDir ? `Eliminare la cartella "${name}" e tutto il suo contenuto?` : `Eliminare il file "${name}"?`;
  if (!confirm(msg)) return;
  try {
    const res = await fetch(`/api/files/delete/${encodeURI(path)}`, {method:'DELETE'});
    if (res.ok||res.status===204) { showToast(`Eliminato: ${name}`,'success'); loadFiles(); }
    else { const d=await safeJson(res); showToast(d.detail||'Errore eliminazione','danger'); }
  } catch(e) { showToast('Errore di rete','danger'); }
}
