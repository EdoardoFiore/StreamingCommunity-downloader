/* StreamingCommunity Web Panel — app.js */

let currentDomain = '';
let currentVersion = '';
let activeEventSources = {}; // job_id -> EventSource
let _searchResults = [];

async function safeJson(res) {
  const text = await res.text();
  try {
    return JSON.parse(text);
  } catch {
    console.error('Non-JSON response (HTTP', res.status, '):', text);
    throw new Error(`HTTP ${res.status}: ${text.slice(0, 120)}`);
  }
}

// ─── Modal helpers (no bootstrap global needed) ───────────────────────────────

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
  const bd = document.querySelector('.modal-backdrop');
  if (bd) bd.remove();
  document.body.classList.remove('modal-open');
}

// Close modals on backdrop click
document.addEventListener('click', (e) => {
  if (e.target.classList.contains('modal') && e.target.classList.contains('show')) {
    hideModal(e.target.id);
  }
  if (e.target.closest('[data-bs-dismiss="modal"]')) {
    const modal = e.target.closest('.modal');
    if (modal) hideModal(modal.id);
  }
});

// ─── Init ────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
  await loadDomainStatus();
  pollJobs();
});

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
  } catch (e) {
    console.error('loadDomainStatus:', e);
  }
}

// ─── Navigation ──────────────────────────────────────────────────────────────

function showPage(page) {
  ['search', 'downloads', 'files'].forEach(p => {
    document.getElementById(`page-${p}`).style.display = p === page ? '' : 'none';
  });
  document.getElementById('page-title').textContent =
    { search: 'Cerca', downloads: 'Download', files: 'File' }[page];

  document.querySelectorAll('.nav-link[data-page]').forEach(el => {
    el.classList.toggle('active', el.dataset.page === page);
  });

  if (page === 'downloads') refreshJobs();
  if (page === 'files') loadFiles();
}

// ─── Settings ────────────────────────────────────────────────────────────────

function openSettings() {
  document.getElementById('domain-input').value = currentDomain;
  document.getElementById('domain-feedback').textContent = '';
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
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ domain }),
    });
    const data = await safeJson(res);
    if (res.ok) {
      currentDomain = data.domain;
      currentVersion = data.version;
      feedback.textContent = `OK — versione ${data.version}`;
      feedback.className = 'form-text text-success';
      document.getElementById('domain-badge').className = 'badge bg-success';
      document.getElementById('domain-badge').textContent = `Domain: .${domain}`;
      setTimeout(() => hideModal('settings-modal'), 800);
    } else {
      feedback.textContent = data.detail || 'Errore';
      feedback.className = 'form-text text-danger';
    }
  } catch (e) {
    feedback.textContent = 'Errore di rete';
    feedback.className = 'form-text text-danger';
  } finally {
    btn.disabled = false;
  }
}

// ─── Search ───────────────────────────────────────────────────────────────────

async function doSearch() {
  const q = document.getElementById('search-input').value.trim();
  if (!q) return;
  if (!currentDomain) { openSettings(); return; }

  const btn = document.getElementById('search-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Cerca';

  const container = document.getElementById('search-results');
  container.innerHTML = '';

  try {
    const res = await fetch(`/api/search?q=${encodeURIComponent(q)}&domain=${currentDomain}`);
    const results = await safeJson(res);

    if (!res.ok) {
      container.innerHTML = `<div class="col-12"><div class="alert alert-danger">${results.detail || 'Errore'}</div></div>`;
      return;
    }
    if (results.length === 0) {
      container.innerHTML = '<div class="col-12"><p class="text-muted">Nessun risultato.</p></div>';
      return;
    }

    results.forEach((item, idx) => {
      const isMovie = item.type === 'movie';
      const year = item.last_air_date ? item.last_air_date.slice(0, 4) : '';
      const score = item.score ? parseFloat(item.score).toFixed(1) : null;
      const posterUrl = item.poster ? `/api/image/${currentDomain}/${item.poster}` : '';

      const card = document.createElement('div');
      card.className = 'col-6 col-sm-4 col-md-3 col-lg-2';
      card.innerHTML = `
        <div class="card result-card h-100" onclick="openDetailModal(${idx})" style="overflow:hidden">
          <div style="aspect-ratio:2/3;overflow:hidden;background:#1a1a2e">
            ${posterUrl
              ? `<img src="${posterUrl}" alt="" style="width:100%;height:100%;object-fit:cover;display:block" onerror="console.warn('poster failed:',this.src);this.parentElement.innerHTML='<div style=\\'width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:#666\\'>\\u{1F3AC}</div>'">`
              : `<div style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:#666;font-size:2rem">&#127916;</div>`}
          </div>
          <div class="card-body p-2">
            <div class="fw-bold small lh-sm mb-1">${escapeHtml(item.name)}</div>
            <div class="d-flex align-items-center gap-1 flex-wrap">
              <span class="badge ${isMovie ? 'bg-blue-lt' : 'bg-green-lt'}" style="font-size:.65em">${isMovie ? 'Film' : 'TV'}</span>
              ${score ? `<span class="badge bg-yellow-lt" style="font-size:.65em">★ ${score}</span>` : ''}
              ${year ? `<span class="text-muted" style="font-size:.7em">${year}</span>` : ''}
            </div>
          </div>
        </div>`;
      card._scItem = item;
      container.appendChild(card);
    });
    _searchResults = results;
  } catch (e) {
    container.innerHTML = `<div class="col-12"><div class="alert alert-danger">Errore di rete: ${e.message}</div></div>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<i class="ti ti-search me-1"></i>Cerca';
  }
}

// ─── Detail Modal ─────────────────────────────────────────────────────────────

function openDetailModal(idx) {
  const item = _searchResults[idx];
  if (!item) return;

  const isMovie = item.type === 'movie';
  const year = item.last_air_date ? item.last_air_date.slice(0, 4) : '';
  const score = item.score ? parseFloat(item.score).toFixed(1) : null;
  const posterUrl = item.poster ? `/api/image/${currentDomain}/${item.poster}` : '';

  const poster = document.getElementById('detail-poster');
  if (posterUrl) {
    poster.onerror = () => { console.warn('detail poster failed:', poster.src); poster.style.display = 'none'; };
    poster.src = posterUrl;
    poster.style.display = '';
  } else {
    poster.style.display = 'none';
  }

  document.getElementById('detail-title').textContent = item.name;

  const typeBadge = document.getElementById('detail-type-badge');
  typeBadge.className = `badge me-1 ${isMovie ? 'bg-blue-lt' : 'bg-green-lt'}`;
  typeBadge.textContent = isMovie ? 'Film' : 'Serie TV';

  const ageBadge = document.getElementById('detail-age-badge');
  if (item.age) {
    ageBadge.textContent = `${item.age}+`;
    ageBadge.style.display = '';
  } else {
    ageBadge.style.display = 'none';
  }

  const metaParts = [];
  if (year) metaParts.push(year);
  if (!isMovie && item.seasons_count) metaParts.push(`${item.seasons_count} stagion${item.seasons_count === 1 ? 'e' : 'i'}`);
  document.getElementById('detail-meta').textContent = metaParts.join(' · ');

  const scoreEl = document.getElementById('detail-score');
  if (score) {
    scoreEl.innerHTML = `<span class="badge bg-yellow-lt fs-5"><i class="ti ti-star-filled me-1"></i>${score}</span>`;
  } else {
    scoreEl.innerHTML = '';
  }

  const btn = document.getElementById('detail-action-btn');
  if (isMovie) {
    btn.className = 'btn btn-primary';
    btn.innerHTML = '<i class="ti ti-download me-1"></i>Scarica';
    const year = item.last_air_date ? item.last_air_date.slice(0, 4) : null;
    btn.onclick = () => { hideModal('detail-modal'); startFilmDownload(item.id, item.name, year); };
  } else {
    btn.className = 'btn btn-success';
    btn.innerHTML = '<i class="ti ti-list me-1"></i>Episodi';
    btn.onclick = () => { hideModal('detail-modal'); openEpisodeBrowser(item.id, item.name, item.slug); };
  }

  showModal('detail-modal');
}

// ─── Film download ────────────────────────────────────────────────────────────

async function startFilmDownload(id, title, year = null) {
  try {
    const res = await fetch('/api/download/film', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, title, year, domain: currentDomain }),
    });
    const data = await safeJson(res);
    if (res.ok) {
      showToast(`Download avviato: ${title}`, 'success');
      showPage('downloads');
      watchJob(data.job_id);
    } else {
      showToast(data.detail || 'Errore', 'danger');
    }
  } catch (e) {
    showToast('Errore di rete', 'danger');
  }
}

// ─── Episode Browser ─────────────────────────────────────────────────────────

let _episodeContext = {};

async function openEpisodeBrowser(tvId, tvName, slug) {
  _episodeContext = { tvId, tvName, slug, token: null, version: currentVersion, episodes: [] };

  document.getElementById('episode-modal-title').textContent = tvName;
  document.getElementById('episode-modal-body').innerHTML =
    '<div class="text-center py-4"><div class="spinner-border text-primary" role="status"></div></div>';
  showModal('episode-modal');

  try {
    // Get token
    const tokenRes = await fetch(`/api/tv/${tvId}/token?domain=${currentDomain}`);
    const tokenData = await tokenRes.json();
    _episodeContext.token = tokenData.token;

    // Get seasons count
    const seasonsRes = await fetch(
      `/api/tv/${tvId}/seasons?slug=${encodeURIComponent(slug)}&domain=${currentDomain}&version=${encodeURIComponent(currentVersion)}`
    );
    const seasonsData = await seasonsRes.json();
    const count = seasonsData.seasons_count;

    renderSeasonSelector(count);
  } catch (e) {
    document.getElementById('episode-modal-body').innerHTML =
      `<div class="alert alert-danger">Errore: ${e.message}</div>`;
  }
}

function renderSeasonSelector(count) {
  const body = document.getElementById('episode-modal-body');
  let btns = '';
  for (let s = 1; s <= count; s++) {
    btns += `<button class="btn btn-outline-primary m-1" onclick="loadSeason(${s})">Stagione ${s}</button>`;
  }
  body.innerHTML = `
    <div class="mb-3">
      <strong>Seleziona stagione:</strong><br>
      <div class="mt-2">${btns}</div>
    </div>
    <div id="season-episodes"></div>`;
}

async function loadSeason(season) {
  const { tvId, slug, token } = _episodeContext;
  const container = document.getElementById('season-episodes');
  container.innerHTML = '<div class="text-center py-3"><div class="spinner-border text-primary" role="status"></div></div>';

  try {
    const res = await fetch(
      `/api/tv/${tvId}/seasons/${season}/episodes?slug=${encodeURIComponent(slug)}&domain=${currentDomain}&version=${encodeURIComponent(currentVersion)}&token=${encodeURIComponent(token)}`
    );
    const eps = await safeJson(res);
    _episodeContext.episodes = eps;
    _episodeContext.currentSeason = season;

    let rows = eps.map((ep, idx) => `
      <tr>
        <td class="text-muted">${ep.n}</td>
        <td>${escapeHtml(ep.name)}</td>
        <td>
          <button class="btn btn-sm btn-primary" onclick="startEpisodeDownload(${idx})">
            <i class="ti ti-download"></i>
          </button>
        </td>
      </tr>`).join('');

    container.innerHTML = `
      <div class="d-flex align-items-center justify-content-between mb-2">
        <strong>Stagione ${season} — ${eps.length} episodi</strong>
        <button class="btn btn-sm btn-outline-success" onclick="downloadWholeSeason(${season})">
          <i class="ti ti-download me-1"></i>Tutta la stagione
        </button>
      </div>
      <div class="table-responsive">
        <table class="table table-sm">
          <thead><tr><th>#</th><th>Titolo</th><th></th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  } catch (e) {
    container.innerHTML = `<div class="alert alert-danger">Errore: ${e.message}</div>`;
  }
}

async function startEpisodeDownload(epIndex) {
  const { tvId, tvName, token, episodes, currentSeason } = _episodeContext;
  const ep = episodes[epIndex];

  try {
    const res = await fetch('/api/download/episode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tv_id: tvId, eps: episodes, ep_index: epIndex,
        domain: currentDomain, token, tv_name: tvName, season: currentSeason,
      }),
    });
    const data = await safeJson(res);
    if (res.ok) {
      showToast(`Download avviato: ${tvName} S${String(currentSeason).padStart(2,'0')}E${String(ep.n).padStart(2,'0')}`, 'success');
      watchJob(data.job_id);
    } else {
      showToast(data.detail || 'Errore', 'danger');
    }
  } catch (e) {
    showToast('Errore di rete', 'danger');
  }
}

async function downloadWholeSeason(season) {
  const { episodes } = _episodeContext;
  if (!confirm(`Scaricare tutti i ${episodes.length} episodi della stagione ${season}?`)) return;

  for (let i = 0; i < episodes.length; i++) {
    await startEpisodeDownload(i);
    await new Promise(r => setTimeout(r, 300));
  }
  showPage('downloads');
  hideModal('episode-modal');
}

// ─── Jobs / Progress ──────────────────────────────────────────────────────────

function watchJob(jobId) {
  if (activeEventSources[jobId]) return;
  const es = new EventSource(`/api/progress/${jobId}`);
  activeEventSources[jobId] = es;

  es.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'progress') {
      updateJobRow(jobId, 'running', msg.pct);
    } else if (msg.type === 'done') {
      updateJobRow(jobId, 'done', 100);
      es.close();
      delete activeEventSources[jobId];
      updateActiveBadge();
    } else if (msg.type === 'error') {
      updateJobRow(jobId, 'error', 0, msg.message);
      es.close();
      delete activeEventSources[jobId];
      updateActiveBadge();
    }
  };
  es.onerror = () => { es.close(); delete activeEventSources[jobId]; };
  updateActiveBadge();
}

async function refreshJobs() {
  try {
    const res = await fetch('/api/jobs');
    const jobs = await safeJson(res);
    renderJobsTable(jobs);
    jobs.filter(j => j.status === 'running' || j.status === 'queued').forEach(j => watchJob(j.job_id));
    updateActiveBadge();
  } catch (e) { console.error('refreshJobs:', e); }
}

function renderJobsTable(jobs) {
  const tbody = document.getElementById('jobs-table-body');
  if (jobs.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" class="text-muted text-center py-4">Nessun download</td></tr>';
    return;
  }
  tbody.innerHTML = jobs.map(j => {
    const statusBadge = {
      queued: '<span class="badge bg-secondary-lt">In coda</span>',
      running: '<span class="badge bg-blue-lt">In corso</span>',
      done: '<span class="badge bg-success-lt">Completato</span>',
      error: '<span class="badge bg-danger-lt">Errore</span>',
    }[j.status] || j.status;

    const progress = j.status === 'running'
      ? `<div class="progress"><div class="progress-bar progress-bar-animated bg-blue" id="prog-${j.job_id}" style="width:${j.progress.pct}%"></div></div><small class="text-muted">${j.progress.pct}%</small>`
      : j.status === 'done'
      ? `<div class="progress"><div class="progress-bar bg-success" style="width:100%"></div></div>`
      : j.status === 'error'
      ? `<small class="text-danger">${escapeHtml(j.error || 'Errore')}</small>`
      : '—';

    const date = new Date(j.created_at + 'Z').toLocaleString('it-IT');
    const typeBadge = j.type === 'film'
      ? '<span class="badge bg-blue-lt">Film</span>'
      : '<span class="badge bg-green-lt">Serie</span>';

    return `<tr id="job-row-${j.job_id}">
      <td>${escapeHtml(j.title)}</td>
      <td>${typeBadge}</td>
      <td>${statusBadge}</td>
      <td style="min-width:160px">${progress}</td>
      <td class="text-muted">${date}</td>
    </tr>`;
  }).join('');
}

function updateJobRow(jobId, status, pct, errorMsg = '') {
  const row = document.getElementById(`job-row-${jobId}`);
  if (!row) { refreshJobs(); return; }

  const progEl = document.getElementById(`prog-${jobId}`);
  if (progEl) {
    progEl.style.width = pct + '%';
  }

  const cells = row.querySelectorAll('td');
  if (status === 'done') {
    cells[2].innerHTML = '<span class="badge bg-success-lt">Completato</span>';
    cells[3].innerHTML = '<div class="progress"><div class="progress-bar bg-success" style="width:100%"></div></div>';
    if (document.getElementById('page-files').style.display !== 'none') loadFiles();
  } else if (status === 'error') {
    cells[2].innerHTML = '<span class="badge bg-danger-lt">Errore</span>';
    cells[3].innerHTML = `<small class="text-danger">${escapeHtml(errorMsg)}</small>`;
  } else if (status === 'running') {
    cells[2].innerHTML = '<span class="badge bg-blue-lt">In corso</span>';
    cells[3].innerHTML = `<div class="progress"><div class="progress-bar progress-bar-animated bg-blue" id="prog-${jobId}" style="width:${pct}%"></div></div><small class="text-muted">${pct}%</small>`;
  }
}

function updateActiveBadge() {
  const count = Object.keys(activeEventSources).length;
  const badge = document.getElementById('active-jobs-badge');
  if (count > 0) {
    badge.style.display = '';
    badge.textContent = count;
  } else {
    badge.style.display = 'none';
  }
}

function pollJobs() {
  setInterval(async () => {
    if (document.getElementById('page-downloads').style.display !== 'none') {
      await refreshJobs();
    }
  }, 5000);
}

// ─── File Browser ─────────────────────────────────────────────────────────────

async function loadFiles() {
  try {
    const res = await fetch('/api/files');
    const tree = await safeJson(res);
    renderFilesTable(tree);
  } catch (e) {
    document.getElementById('files-table-body').innerHTML =
      `<tr><td colspan="4" class="text-danger text-center">Errore: ${e.message}</td></tr>`;
  }
}

function renderFilesTable(tree) {
  const tbody = document.getElementById('files-table-body');
  const rows = [];
  flattenTree(tree, rows, 0);

  if (rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" class="text-muted text-center py-4">Nessun file trovato</td></tr>';
    return;
  }
  tbody.innerHTML = rows.join('');
}

function flattenTree(items, rows, depth) {
  items.forEach(item => {
    const indent = '&nbsp;'.repeat(depth * 4);
    if (item.type === 'directory') {
      rows.push(`<tr class="table-active">
        <td colspan="3"><strong>${indent}<i class="ti ti-folder-filled text-yellow me-1"></i>${escapeHtml(item.name)}</strong></td>
        <td class="text-end">
          <button class="btn btn-sm btn-outline-danger" onclick="deletePath('${escapeStr(item.path)}', '${escapeStr(item.name)}', true)">
            <i class="ti ti-trash"></i>
          </button>
        </td>
      </tr>`);
      if (item.children) flattenTree(item.children, rows, depth + 1);
    } else {
      const size = formatSize(item.size);
      const date = new Date(item.mtime * 1000).toLocaleDateString('it-IT');
      rows.push(`<tr>
        <td>${indent}<i class="ti ti-file-type-mp4 text-red me-1"></i>${escapeHtml(item.name)}</td>
        <td class="text-muted">${size}</td>
        <td class="text-muted">${date}</td>
        <td class="text-end">
          <button class="btn btn-sm btn-outline-primary me-1" onclick="playFile('${escapeStr(item.path)}', '${escapeStr(item.name)}')">
            <i class="ti ti-player-play"></i>
          </button>
          <a class="btn btn-sm btn-outline-secondary me-1" href="/api/files/download/${encodeURI(item.path)}">
            <i class="ti ti-download"></i>
          </a>
          <button class="btn btn-sm btn-outline-danger" onclick="deletePath('${escapeStr(item.path)}', '${escapeStr(item.name)}', false)">
            <i class="ti ti-trash"></i>
          </button>
        </td>
      </tr>`);
    }
  });
}

function playFile(path, name) {
  document.getElementById('player-modal-title').textContent = name;
  const video = document.getElementById('video-player');
  video.src = `/api/files/stream/${encodeURI(path)}`;
  video.load();

  showModal('player-modal');

  document.getElementById('player-modal').addEventListener('click', (e) => {
    if (e.target.closest('[data-bs-dismiss="modal"]')) {
      video.pause();
      video.src = '';
    }
  }, { once: true });
}

async function deletePath(path, name, isDir) {
  const msg = isDir
    ? `Eliminare la cartella "${name}" e tutto il suo contenuto?`
    : `Eliminare il file "${name}"?`;
  if (!confirm(msg)) return;

  try {
    const res = await fetch(`/api/files/delete/${encodeURI(path)}`, { method: 'DELETE' });
    if (res.ok || res.status === 204) {
      showToast(`Eliminato: ${name}`, 'success');
      loadFiles();
    } else {
      const data = await safeJson(res);
      showToast(data.detail || 'Errore eliminazione', 'danger');
    }
  } catch (e) {
    showToast('Errore di rete', 'danger');
  }
}

// ─── Toast ────────────────────────────────────────────────────────────────────

function showToast(message, type = 'info') {
  const colors = { success: 'bg-success', danger: 'bg-danger', info: 'bg-info', warning: 'bg-warning' };
  const toast = document.createElement('div');
  toast.style.cssText = 'position:fixed;bottom:1rem;right:1rem;z-index:9999';
  toast.innerHTML = `
    <div class="alert ${colors[type] || 'bg-info'} alert-dismissible text-white mb-0 shadow" role="alert">
      ${escapeHtml(message)}
      <button type="button" class="btn-close btn-close-white" data-bs-dismiss="alert"></button>
    </div>`;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 5000);
}

// ─── Utilities ────────────────────────────────────────────────────────────────

function formatSize(bytes) {
  if (bytes === undefined || bytes === null) return '—';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  return (bytes / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
}

function escapeHtml(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function escapeStr(s) {
  if (!s) return '';
  return String(s).replace(/'/g, "\\'").replace(/"/g, '\\"');
}
