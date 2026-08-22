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

// ── Session ────────────────────────────────────────────────────────────────────

let _me = null;           // { user, csrf_token, auth_enabled }
let _csrf = '';
let _authEnabled = true;  // false when the panel runs without Jellyfin (AUTH_ENABLED=0)
let _requestStatus = {};  // external_id → { id, status } for the result cards

function can(permission) {
  return !!_me && _me.user.permission_names.includes(permission);
}

// Every state-changing call carries the session's CSRF token, and an expired or
// revoked session lands on the login page instead of failing silently. Wrapping
// fetch once covers every call site, including the ones written before auth
// existed.
const _nativeFetch = window.fetch.bind(window);

function _withCsrfHeader(opts, method) {
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && _csrf) {
    opts.headers = { ...(opts.headers || {}), 'X-CSRF-Token': _csrf };
  }
  return opts;
}

/** Re-fetch identity and CSRF token without the page-load side effects
 * (nav visibility, header). Returns false if the session itself is gone. */
async function _refreshIdentity() {
  const res = await _nativeFetch('/api/auth/me');
  if (!res.ok) return false;
  _me = await res.json();
  _csrf = _me.csrf_token;
  return true;
}

window.fetch = async (input, init) => {
  init = init || {};
  const method = (init.method || 'GET').toUpperCase();
  const isAuthCall = String(input).startsWith('/api/auth/');

  let res = await _nativeFetch(input, _withCsrfHeader(init, method));

  // A stale token — typically a tab left open across a newer login elsewhere,
  // which rotates the (browser-wide) session cookie but leaves this tab's own
  // in-memory token behind — is safe to recover from: refresh it once and
  // retry. A real permission-denied 403 carries no such header and falls
  // through untouched, since refreshing a token would never fix that.
  if (res.status === 403 && res.headers.get('X-CSRF-Retry') && !isAuthCall) {
    if (await _refreshIdentity()) {
      res = await _nativeFetch(input, _withCsrfHeader(init, method));
    }
  }

  if (res.status === 401 && !isAuthCall) {
    window.location.href = '/login';
  }
  return res;
};

async function initAuth() {
  if (!await _refreshIdentity()) { window.location.href = '/login'; return false; }

  _authEnabled = _me.auth_enabled !== false;

  const initials = (_me.user.username || '?').slice(0, 2).toUpperCase();
  document.getElementById('user-initials').textContent = initials;
  document.getElementById('user-name').textContent = _me.user.username;
  document.getElementById('user-role').textContent = _roleLabel();

  const version = document.getElementById('app-version');
  if (version) version.textContent = _me.version ? `v${_me.version}` : '';

  // Menu entries follow the permissions. This is cosmetic only — every one of
  // these endpoints is checked server-side as well.
  document.querySelectorAll('[data-perm]').forEach(el => {
    const needed = el.dataset.perm.split('|');
    el.style.display = needed.some(can) ? '' : 'none';
  });
  // Without Jellyfin there is no identity or request queue to show, even for
  // the one permission (DOWNLOAD) that would otherwise leave them visible.
  if (!_authEnabled) {
    document.querySelectorAll('[data-requires-auth]').forEach(el => { el.style.display = 'none'; });
  }
  return true;
}

function _roleLabel() {
  if (can('MANAGE_USERS') || can('MANAGE_SETTINGS')) return 'Amministratore';
  if (can('MANAGE_REQUESTS')) return 'Approvatore';
  if (can('DOWNLOAD')) return 'Download diretto';
  if (can('REQUEST')) return 'Richieste';
  return 'Sola lettura';
}

async function logout() {
  await fetch('/api/auth/logout', { method: 'POST' });
  window.location.href = '/login';
}

// ── Utilities ──────────────────────────────────────────────────────────────────

function scConfirm(msg) {
  return new Promise(resolve => {
    document.getElementById('sc-confirm-msg').textContent = msg;
    const ok = document.getElementById('sc-confirm-ok');
    const cancel = document.getElementById('sc-confirm-cancel');
    function cleanup() {
      ok.removeEventListener('click', onOk);
      cancel.removeEventListener('click', onCancel);
    }
    function onOk()     { cleanup(); hideModal('sc-confirm-modal'); resolve(true); }
    function onCancel() { cleanup(); hideModal('sc-confirm-modal'); resolve(false); }
    ok.addEventListener('click', onOk, {once:true});
    cancel.addEventListener('click', onCancel, {once:true});
    showModal('sc-confirm-modal');
  });
}

function scPrompt(msg, defaultVal='') {
  return new Promise(resolve => {
    document.getElementById('sc-prompt-msg').textContent = msg;
    const input = document.getElementById('sc-prompt-input');
    input.value = defaultVal;
    const ok = document.getElementById('sc-prompt-ok');
    const cancel = document.getElementById('sc-prompt-cancel');
    function cleanup() {
      ok.removeEventListener('click', onOk);
      cancel.removeEventListener('click', onCancel);
      input.removeEventListener('keydown', onKey);
    }
    function onOk()     { cleanup(); hideModal('sc-prompt-modal'); resolve(input.value); }
    function onCancel() { cleanup(); hideModal('sc-prompt-modal'); resolve(null); }
    function onKey(e)   { if (e.key === 'Enter') onOk(); }
    ok.addEventListener('click', onOk, {once:true});
    cancel.addEventListener('click', onCancel, {once:true});
    input.addEventListener('keydown', onKey);
    showModal('sc-prompt-modal');
    setTimeout(() => input.focus(), 50);
  });
}

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
  toast.style.cssText = 'position:fixed;bottom:1rem;right:1rem;left:auto;z-index:9999;min-width:220px;max-width:calc(100vw - 2rem)';
  toast.innerHTML = `<div class="alert ${colors[type]||'bg-info'} alert-dismissible text-white mb-0 shadow" role="alert">
    ${escapeHtml(message)}
    <button type="button" class="btn-close btn-close-white" onclick="this.closest('.alert').parentElement.remove()"></button>
  </div>`;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 4000);
}

// ── Init ───────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
  if (!await initAuth()) return;
  if (can('REQUEST') || can('DOWNLOAD') || can('MANAGE_SETTINGS')) await loadDomainStatus();
  if (can('MANAGE_SETTINGS')) await Promise.all([loadLibraries(), loadPerfSettings()]);
  if (can('DOWNLOAD') || can('MANAGE_REQUESTS')) {
    connectGlobalStream();
  } else {
    // Without access to the job stream there is nothing to push on, so the bell
    // polls instead. Cheap: one indexed count per minute.
    setInterval(refreshNotifications, 60000);
  }
  if (can('VIEW_LIBRARY')) setupFileManager();
  setupSettingsTabs();
  setupSearchDebounce();
  refreshNotifications();
  refreshQueueBadge();
  showPage(defaultPage());
});

function defaultPage() {
  if (can('REQUEST') || can('DOWNLOAD')) return 'search';
  if (can('MANAGE_REQUESTS')) return 'requests';
  if (can('VIEW_LIBRARY')) return 'files';
  return 'search';
}

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
  // Close mobile menu if open
  const mobileMenu = document.getElementById('sidebar-menu');
  if (mobileMenu && mobileMenu.classList.contains('show')) {
    mobileMenu.classList.remove('show');
  }
  ['search','downloads','files','requests','my-requests','watches','users'].forEach(p => {
    const el = document.getElementById(`page-${p}`);
    if (el) el.style.display = p === page ? '' : 'none';
  });
  document.getElementById('page-title').textContent = {
    search:'Cerca', downloads:'Download', files:'File',
    requests:'Coda richieste', 'my-requests':'Le mie richieste',
    watches:'Serie seguite', users:'Utenti',
  }[page] || 'Cerca';
  document.querySelectorAll('.nav-link[data-page]').forEach(el =>
    el.classList.toggle('active', el.dataset.page === page));
  if (page === 'downloads') refreshJobs();
  if (page === 'files') loadFiles();
  if (page === 'requests') loadRequestQueue();
  if (page === 'my-requests') loadMyRequests();
  if (page === 'watches') loadWatches();
  if (page === 'users') loadUsersPage();
}

// ── Serie seguite ────────────────────────────────────────────────────────────

let _watches = [];

function fmtLastChecked(iso) {
  if (!iso) return 'mai controllata';
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return 'controllata ora';
  if (minutes < 60) return `controllata ${minutes} min fa`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `controllata ${hours} h fa`;
  return `controllata ${Math.round(hours / 24)} g fa`;
}

async function loadWatches() {
  const c = document.getElementById('watches-list');
  if (!c) return;
  try {
    // An approver sees every followed series, not only their own: arming a
    // series is their decision, and they cannot make it on a list that hides
    // the follows waiting for it.
    const res = await fetch(can('MANAGE_REQUESTS') ? '/api/watches' : '/api/watches/mine');
    if (!res.ok) { c.innerHTML = '<p class="text-muted">Impossibile caricare le serie seguite.</p>'; return; }
    const data = await safeJson(res);
    _watches = data.watches || [];
    renderWatchesList();
  } catch (e) {
    c.innerHTML = '<p class="text-muted">Errore di rete.</p>';
  }
}

function renderWatchesList() {
  const c = document.getElementById('watches-list');
  if (!c) return;
  if (!_watches.length) {
    c.innerHTML = `<div class="empty-panel">
      <i class="ti ti-bell-off"></i>
      <p>Nessuna serie seguita.</p>
      <p class="text-muted" style="font-size:12px;margin-top:6px">
        Apri una serie o un anime dalla ricerca e premi «Segui» per scaricare
        i nuovi episodi appena escono.
      </p>
    </div>`;
    return;
  }
  c.innerHTML = _watches.map(w => {
    // The owner's DOWNLOAD permission is what the poller checks, so a follower
    // without it must not be told the episode will just appear. An ownerless
    // watch (no accounts) always downloads: there is no queue to wait in.
    const auto = w.created_by === null || w.auto_approve ||
      (!!_me && w.created_by === _me.user.id && can('DOWNLOAD'));
    const badge = auto
      ? '<span class="badge bg-green-lt">download automatico</span>'
      : '<span class="badge bg-yellow-lt">passa dalla coda</span>';
    const kind = w.media_type === 'anime' ? 'Anime' : 'Serie TV';
    const audio = w.audio_languages.length ? w.audio_languages.join(', ') : 'originale';
    // Arming is the approver's decision, and only worth offering where it would
    // change something: a series that already downloads by itself has nothing
    // to approve.
    const canArm = can('MANAGE_REQUESTS') && w.created_by !== null;
    const armButton = !canArm ? '' : w.auto_approve
      ? `<button class="btn btn-sm btn-outline-secondary" onclick="setWatchAutoApprove(${w.id}, false)"
                 title="I nuovi episodi torneranno a passare dalla coda di approvazione">
           <i class="ti ti-bell-x me-1"></i>Togli automatico
         </button>`
      : `<button class="btn btn-sm btn-outline-success" onclick="setWatchAutoApprove(${w.id}, true)"
                 title="Approva la serie una volta: i nuovi episodi verranno scaricati senza passare dalla coda">
           <i class="ti ti-bell-check me-1"></i>Approva automatico
         </button>`;
    const who = can('MANAGE_REQUESTS') && w.followers && w.followers.length
      ? `<span class="req-dot">·</span><i class="ti ti-user"></i> ${escapeHtml(w.followers.join(', '))}`
      : '';
    return `
      <div class="req-row">
        <div class="req-main">
          <div class="req-title">${escapeHtml(w.title)}${w.year ? ` <span class="text-muted">(${escapeHtml(w.year)})</span>` : ''}</div>
          <div class="req-meta">
            <i class="ti ti-device-tv"></i> ${kind}
            <span class="req-dot">·</span>
            <i class="ti ti-volume"></i> ${escapeHtml(audio)}
            <span class="req-dot">·</span>
            <i class="ti ti-refresh"></i> ${escapeHtml(fmtLastChecked(w.last_checked_at))}
            ${who}
          </div>
        </div>
        <div class="req-side">
          ${badge}
          <div class="req-actions">
            ${armButton}
            <button class="btn btn-sm btn-outline-secondary" id="watch-check-${w.id}"
                    onclick="checkWatchNow(${w.id})"
                    title="Cerca subito nuovi episodi, senza aspettare il controllo automatico">
              <i class="ti ti-refresh me-1"></i>Controlla ora
            </button>
            <button class="btn btn-sm btn-outline-secondary" onclick="unfollowWatch(${w.id})">
              <i class="ti ti-bell-off me-1"></i>Non seguire più
            </button>
          </div>
        </div>
      </div>`;
  }).join('');
}

// The follow toggle lives in two modals whose contexts are shaped differently,
// so both are flattened to the same shape here rather than in each caller.
function _followTarget(kind) {
  if (kind === 'anime') {
    return {
      btnId: 'follow-anime-btn',
      source: 'animeunity',
      media_type: 'anime',
      external_id: String(_animeCtx.animeId ?? ''),
      title: _animeCtx.animeName,
      year: _animeCtx.animeYear,
      anime_type: _animeCtx.animeType,
      audio_languages: _animeCtx.audioLangs || [],
      subtitle_languages: _animeCtx.subLangs || [],
      // A one-shot anime film has no next episode to wait for.
      followable: (_animeCtx.animeType || 'tv') !== 'movie',
    };
  }
  return {
    btnId: 'follow-tv-btn',
    source: 'streamingcommunity',
    media_type: 'tv',
    external_id: String(_epCtx.tvId ?? ''),
    title: _epCtx.tvName,
    slug: _epCtx.slug,
    year: _epCtx.year,
    poster: _epCtx.poster,
    audio_languages: _epCtx.audioLangs || [],
    subtitle_languages: _epCtx.subLangs || [],
    followable: true,
  };
}

function _renderFollowButton(kind, following, busy = false) {
  const target = _followTarget(kind);
  const btn = document.getElementById(target.btnId);
  if (!btn) return;
  // Available with or without Jellyfin: an ownerless watch downloads directly,
  // the way everything else does when the panel runs without accounts.
  if (!target.followable || !(can('REQUEST') || can('DOWNLOAD'))) {
    btn.style.display = 'none';
    return;
  }
  btn.style.display = '';
  btn.disabled = busy;
  btn.dataset.following = following ? '1' : '';
  // No margin utilities: the header is a flex row with a gap, and the title
  // takes the free space.
  btn.className = 'btn btn-sm flex-shrink-0 ' + (following ? 'btn-success' : 'btn-outline-secondary');
  btn.innerHTML = following
    ? '<i class="ti ti-bell-check me-1"></i>Seguita'
    : '<i class="ti ti-bell-plus me-1"></i>Segui';
  btn.title = following
    ? 'I nuovi episodi vengono cercati automaticamente. Premi per smettere.'
    : 'Cerca automaticamente i nuovi episodi di questa serie';
}

async function checkWatchStatus(kind) {
  const target = _followTarget(kind);
  if (!target.followable || !target.external_id) { _renderFollowButton(kind, false); return; }
  try {
    const params = new URLSearchParams({
      source: target.source, media_type: target.media_type, external_id: target.external_id,
    });
    const res = await fetch(`/api/watches/status?${params}`);
    if (!res.ok) { _renderFollowButton(kind, false); return; }
    const data = await safeJson(res);
    _renderFollowButton(kind, !!data.followed_by_me);
  } catch (e) { _renderFollowButton(kind, false); }
}

async function toggleFollowSeries(kind) {
  const target = _followTarget(kind);
  const btn = document.getElementById(target.btnId);
  const following = !!(btn && btn.dataset.following);
  _renderFollowButton(kind, following, true);

  try {
    if (following) {
      const params = new URLSearchParams({
        source: target.source, media_type: target.media_type, external_id: target.external_id,
      });
      const status = await safeJson(await fetch(`/api/watches/status?${params}`));
      const res = await fetch(`/api/watches/${status.watch_id}`, {method: 'DELETE'});
      if (!res.ok) throw new Error();
      _renderFollowButton(kind, false);
      showToast('Serie non più seguita', 'success');
    } else {
      const res = await fetch('/api/watches', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(target),
      });
      const data = await safeJson(res);
      if (!res.ok) {
        _renderFollowButton(kind, false);
        showToast(data.detail || 'Impossibile seguire la serie', 'danger');
        return;
      }
      _renderFollowButton(kind, true);
      // Only true for someone who can start downloads. Without that permission
      // each new episode becomes a request an approver has to accept, and
      // saying otherwise sets up a wait for something that never arrives.
      showToast(can('DOWNLOAD')
        ? 'Serie seguita: i nuovi episodi arriveranno da soli'
        : 'Serie seguita: i nuovi episodi verranno richiesti a un amministratore',
        'success');
    }
    if (document.getElementById('page-watches').style.display !== 'none') loadWatches();
  } catch (e) {
    _renderFollowButton(kind, following);
    showToast('Errore di rete', 'danger');
  }
}

// The automatic check runs every few hours; this is for when an episode has
// just dropped and waiting for the next cycle makes no sense.
async function checkWatchNow(watchId) {
  const btn = document.getElementById(`watch-check-${watchId}`);
  if (btn) { btn.disabled = true; btn.innerHTML = '<i class="ti ti-loader-2 ti-spin me-1"></i>Controllo...'; }
  try {
    const res = await fetch(`/api/watches/${watchId}/check`, {method: 'POST'});
    const data = await safeJson(res);
    if (!res.ok) {
      showToast(data.detail || 'Controllo fallito', 'danger');
      return;
    }
    if (data.new) {
      showToast(
        data.new === 1 ? '1 nuovo episodio trovato' : `${data.new} nuovi episodi trovati`,
        'success',
      );
    } else {
      showToast('Nessun nuovo episodio', 'info');
    }
  } catch (e) {
    showToast('Errore di rete', 'danger');
  } finally {
    // Redraws the row, which also refreshes "controllata ora".
    await loadWatches();
  }
}

// Arming a series before an episode exists, which is the whole point: waiting
// for the first request means waiting for the source to publish.
async function setWatchAutoApprove(watchId, enabled) {
  const watch = _watches.find(w => w.id === watchId);
  const name = watch ? watch.title : watchId;
  if (!enabled && !await scConfirm(`I nuovi episodi di «${name}» torneranno in coda. Procedere?`)) return;
  try {
    const res = await fetch(`/api/watches/${watchId}/auto-approve`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled}),
    });
    const data = await safeJson(res);
    if (!res.ok) { showToast(data.detail || 'Operazione fallita', 'danger'); return; }
    showToast(enabled
      ? `«${name}»: i nuovi episodi verranno scaricati automaticamente`
      : `«${name}»: i nuovi episodi torneranno in coda`, 'success');
    await loadWatches();
  } catch (e) {
    showToast('Errore di rete', 'danger');
  }
}

async function unfollowWatch(watchId) {
  const watch = _watches.find(w => w.id === watchId);
  if (!await scConfirm(`Smettere di seguire «${watch ? watch.title : watchId}»?`)) return;
  try {
    const res = await fetch(`/api/watches/${watchId}`, {method: 'DELETE'});
    if (!res.ok) { showToast('Errore', 'danger'); return; }
    showToast('Serie non più seguita', 'success');
    await loadWatches();
  } catch (e) { showToast('Errore di rete', 'danger'); }
}

// ── Vocabolario notifiche ────────────────────────────────────────────────────
//
// Mirrors notify.ALL_EVENTS on the server. The bell reads the icons; the
// per-channel picker in Impostazioni reads the labels and the grouping.

const NOTIFICATION_ICONS = {
  request_created: 'ti-inbox',
  request_joined: 'ti-users',
  request_approved: 'ti-circle-check',
  request_denied: 'ti-circle-x',
  request_downloading: 'ti-download',
  request_completed: 'ti-device-tv',
  request_failed: 'ti-alert-triangle',
  request_needs_attention: 'ti-alert-circle',
  request_available: 'ti-library',
  download_completed: 'ti-circle-check',
  download_failed: 'ti-alert-triangle',
  download_batch_completed: 'ti-checkbox',
  download_batch_failed: 'ti-alert-octagon',
  watch_needs_approval: 'ti-bell-question',
  watch_auto_approved: 'ti-bell-check',
};

const NOTIFICATION_LABELS = {
  request_created: 'Nuova richiesta',
  request_joined: 'Richiesta già presente',
  request_approved: 'Richiesta approvata',
  request_denied: 'Richiesta rifiutata',
  request_downloading: 'Richiesta in download',
  request_completed: 'Richiesta completata',
  request_failed: 'Richiesta fallita',
  request_needs_attention: 'Richiesta da verificare',
  request_available: 'Già in libreria',
  download_completed: 'Download completato',
  download_failed: 'Download fallito',
  download_batch_completed: 'Stagione o serie completata',
  download_batch_failed: 'Stagione o serie fallita',
  watch_needs_approval: 'Serie seguita da approvare',
  watch_auto_approved: 'Serie approvata',
};

// Explicit order, so the picker does not depend on object key order.
const NOTIFICATION_EVENT_GROUPS = [
  {
    label: 'Richieste',
    events: ['request_created', 'request_joined', 'request_approved', 'request_denied',
             'request_downloading', 'request_completed', 'request_failed',
             'request_needs_attention', 'request_available'],
  },
  {
    label: 'Download diretti',
    events: ['download_completed', 'download_failed',
             'download_batch_completed', 'download_batch_failed'],
  },
  {
    label: 'Serie seguite',
    events: ['watch_needs_approval', 'watch_auto_approved'],
  },
];

const ALL_NOTIFICATION_EVENTS = NOTIFICATION_EVENT_GROUPS.flatMap(g => g.events);

// ── Settings ───────────────────────────────────────────────────────────────────

// Every section in the settings modal saves itself, so each one reports into its
// own feedback line rather than sharing one status area.
const _SETTINGS_FEEDBACK_IDS = [
  'domain-feedback', 'libraries-feedback', 'perf-settings-feedback',
  'jf-connect-feedback', 'jf-reconnect-feedback', 'notif-channels-feedback',
];

function _feedback(id, message = '', kind = 'muted') {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = message;
  el.className = 'form-text' + (message ? ` text-${kind}` : '');
}

// Each tab fetches its own data the first time it is opened, so opening the
// modal no longer waits on the slowest section (disk usage stats every library
// path, on an NFS mount that can be asleep).
const _SETTINGS_TAB_LOADERS = {
  download: () => loadPerfSettings(),
  accesso: () => loadJellyfinSettings(),
  notifiche: () => loadNotificationChannels(),
};

// Tabs whose panes only talk to MANAGE_SETTINGS endpoints: without it they would
// render as empty panes fed by 403s.
const _SETTINGS_TABS_NEED_MANAGE = ['sorgente', 'librerie', 'download', 'notifiche'];

let _settingsTab = 'sorgente';
const _settingsLoaded = new Set();

function setupSettingsTabs() {
  const tabs = document.getElementById('settings-tabs');
  if (!tabs) return;
  tabs.addEventListener('click', (e) => {
    const link = e.target.closest('[data-settings-tab]');
    if (!link) return;
    e.preventDefault();
    switchSettingsTab(link.dataset.settingsTab);
  });
}

function _visibleSettingsTabs() {
  return [...document.querySelectorAll('#settings-tabs [data-settings-tab]')]
    .filter(a => a.closest('.nav-item').style.display !== 'none')
    .map(a => a.dataset.settingsTab);
}

async function switchSettingsTab(name) {
  document.querySelectorAll('#settings-tabs [data-settings-tab]').forEach(a =>
    a.classList.toggle('active', a.dataset.settingsTab === name));
  document.querySelectorAll('[data-settings-pane]').forEach(pane => {
    pane.style.display = pane.dataset.settingsPane === name ? '' : 'none';
  });
  _settingsTab = name;
  const body = document.querySelector('#settings-modal .modal-body');
  if (body) body.scrollTop = 0;

  // Marked before awaiting, so a double click cannot fire two fetches.
  if (!_settingsLoaded.has(name)) {
    _settingsLoaded.add(name);
    await _SETTINGS_TAB_LOADERS[name]?.();
  }
}

async function openSettings() {
  document.getElementById('domain-input').value = currentDomain;
  _SETTINGS_FEEDBACK_IDS.forEach(id => _feedback(id));
  renderLibrariesList();

  const manage = can('MANAGE_SETTINGS');
  document.querySelectorAll('#settings-tabs [data-settings-tab]').forEach(a => {
    const restricted = _SETTINGS_TABS_NEED_MANAGE.includes(a.dataset.settingsTab);
    a.closest('.nav-item').style.display = restricted && !manage ? 'none' : '';
  });

  // Cleared on every open so a value changed elsewhere is picked up; within one
  // open, moving between tabs does not refetch.
  _settingsLoaded.clear();
  showModal('settings-modal');
  const tabs = _visibleSettingsTabs();
  await switchSettingsTab(tabs.includes('sorgente') ? 'sorgente' : tabs[0]);
}

// ── Spazio disco ─────────────────────────────────────────────────────────────

// formatSize() stops at GB and is used for file rows; volumes are routinely in
// terabytes, so the disk readout gets its own scale.
function fmtBytes(bytes) {
  if (bytes == null) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let value = bytes, i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
  return `${value.toFixed(value >= 100 || i === 0 ? 0 : 1).replace('.', ',')} ${units[i]}`;
}

// Reported per volume, not per library: the three libraries are almost always
// folders on one mount, and three identical bars said nothing.
async function loadDiskUsage() {
  const el = document.getElementById('fm-disk-usage');
  if (!el) return;
  try {
    const res = await fetch('/api/files/disk-usage');
    if (!res.ok) { el.textContent = ''; return; }
    renderDiskUsage(await safeJson(res));
  } catch (e) { el.textContent = ''; }
}

function renderDiskUsage(data) {
  const el = document.getElementById('fm-disk-usage');
  if (!el) return;
  const volumes = (data && data.volumes) || [];
  if (!volumes.length) {
    el.textContent = data && data.errors && data.errors.length ? 'Spazio non leggibile' : '';
    return;
  }
  // Several distinct mounts is the unusual case; show the fullest, since that
  // is the one that will stop a download.
  const worst = volumes.reduce((a, b) => (a.free <= b.free ? a : b));
  const pct = worst.total ? Math.round((worst.used / worst.total) * 100) : 0;
  // Amber past 80%, red past 92%: at that point a 4K season may not fit.
  const color = pct >= 92 ? 'text-danger' : pct >= 80 ? 'text-warning' : '';
  const suffix = volumes.length > 1 ? ` (volume più pieno di ${volumes.length})` : '';
  el.innerHTML =
    `<i class="ti ti-database me-1"></i><span class="${color}">${fmtBytes(worst.free)} liberi` +
    `</span> su ${fmtBytes(worst.total)}${suffix}`;
  el.title = worst.paths.join('\n');
}

// ── Canali di notifica (Apprise) ─────────────────────────────────────────────

let _notifChannels = [];

async function loadNotificationChannels() {
  try {
    const res = await fetch('/api/notification-channels');
    if (!res.ok) return;
    const data = await safeJson(res);
    _notifChannels = data.channels || [];
    renderNotificationChannelsList();
  } catch (e) { console.error('loadNotificationChannels:', e); }
}

// The URL carries the bot token, so the list shows only enough of it to tell two
// channels apart. The full value stays behind the MANAGE_SETTINGS endpoint.
function _maskAppriseUrl(url) {
  const scheme = url.split('://')[0];
  return `${scheme}://…${url.slice(-4)}`;
}

// Which channels have their event picker open. Kept outside the render so
// rebuilding the list does not collapse what the user was editing.
const _expandedChannels = new Set();

// An empty list means "every event" on the server, so the picker needs a master
// switch: without it, unchecking the last box would silently mean the opposite
// of what it looks like.
function _eventSummary(ch) {
  if (!ch.events.length) return 'Tutti gli eventi';
  return ch.events.length === 1 ? '1 evento' : `${ch.events.length} eventi`;
}

function _renderEventPicker(ch) {
  const all = ch.events.length === 0;
  const groups = NOTIFICATION_EVENT_GROUPS.map(group => {
    const boxes = group.events.map(event => `
      <label class="form-check form-check-inline" style="min-width:200px">
        <input class="form-check-input" type="checkbox" value="${event}"
               data-channel="${ch.id}"
               ${all || ch.events.includes(event) ? 'checked' : ''}
               ${all ? 'disabled' : ''}
               onchange="updateChannelEvents(${ch.id})">
        <span class="form-check-label" style="font-size:12px">
          <i class="ti ${NOTIFICATION_ICONS[event] || 'ti-bell'} me-1"></i>${NOTIFICATION_LABELS[event]}
        </span>
      </label>`).join('');
    return `
      <div class="mb-2">
        <p class="settings-section-label mb-1">${group.label}</p>
        ${boxes}
      </div>`;
  }).join('');

  return `
    <div class="ps-4 pb-2" id="notif-events-${ch.id}">
      <label class="form-check form-switch mb-2">
        <input class="form-check-input" type="checkbox" ${all ? 'checked' : ''}
               id="notif-all-events-${ch.id}"
               onchange="toggleAllChannelEvents(${ch.id}, this.checked)">
        <span class="form-check-label" style="font-size:12px">Tutti gli eventi</span>
      </label>
      ${groups}
    </div>`;
}

function renderNotificationChannelsList() {
  const c = document.getElementById('notif-channels-list');
  if (!c) return;
  if (!_notifChannels.length) {
    c.innerHTML = '<p class="text-muted small mb-0">Nessun canale configurato.</p>';
    return;
  }
  c.innerHTML = _notifChannels.map(ch => {
    const open = _expandedChannels.has(ch.id);
    return `
    <div class="border-bottom pb-1 mb-1">
      <div class="d-flex align-items-center gap-2 py-1">
        <label class="form-check form-switch mb-0">
          <input class="form-check-input" type="checkbox" ${ch.enabled ? 'checked' : ''}
                 onchange="toggleNotificationChannel(${ch.id}, this.checked)">
        </label>
        <div class="flex-fill text-truncate">
          <span style="color:var(--text)">${escapeHtml(ch.name)}</span>
          <span class="text-muted small ms-2">${escapeHtml(_maskAppriseUrl(ch.apprise_url))}</span>
        </div>
        <button type="button" class="btn btn-sm btn-ghost-secondary"
                onclick="toggleChannelEvents(${ch.id})" title="Scegli quali notifiche ricevere">
          <i class="ti ti-${open ? 'chevron-up' : 'chevron-down'} me-1"></i>${_eventSummary(ch)}
        </button>
        <button type="button" class="btn btn-sm btn-outline-secondary" onclick="testNotificationChannel(${ch.id})">
          <i class="ti ti-send me-1"></i>Test
        </button>
        <button type="button" class="btn btn-sm btn-outline-danger" onclick="deleteNotificationChannel(${ch.id})">
          <i class="ti ti-trash"></i>
        </button>
      </div>
      ${open ? _renderEventPicker(ch) : ''}
    </div>`;
  }).join('');
}

function toggleChannelEvents(id) {
  if (_expandedChannels.has(id)) _expandedChannels.delete(id);
  else _expandedChannels.add(id);
  renderNotificationChannelsList();
}

function _checkedEvents(id) {
  return [...document.querySelectorAll(`#notif-events-${id} input[data-channel="${id}"]`)]
    .filter(box => box.checked).map(box => box.value);
}

async function toggleAllChannelEvents(id, all) {
  // Turning "all" off pre-selects everything, so the user removes what they do
  // not want rather than starting from nothing.
  await _saveChannelEvents(id, all ? [] : ALL_NOTIFICATION_EVENTS.slice());
}

async function updateChannelEvents(id) {
  const chosen = _checkedEvents(id);
  if (!chosen.length) {
    // [] would be stored as "every event" — the opposite of an empty selection.
    _feedback('notif-channels-feedback', 'Seleziona almeno un evento, oppure attiva «Tutti gli eventi».', 'danger');
    renderNotificationChannelsList();
    return;
  }
  await _saveChannelEvents(id, chosen);
}

async function _saveChannelEvents(id, events) {
  try {
    const res = await fetch(`/api/notification-channels/${id}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({events}),
    });
    const data = await safeJson(res);
    if (!res.ok) {
      _feedback('notif-channels-feedback', data.detail || 'Errore aggiornamento eventi.', 'danger');
      await loadNotificationChannels();
      return;
    }
    // Patched locally instead of refetching: a full reload would rebuild the
    // open picker under the cursor while the user is still clicking.
    const channel = _notifChannels.find(c => c.id === id);
    if (channel) channel.events = data.events || [];
    _feedback('notif-channels-feedback', 'Eventi aggiornati.', 'success');
    renderNotificationChannelsList();
  } catch (e) {
    _feedback('notif-channels-feedback', 'Errore di rete.', 'danger');
  }
}

function toggleNotificationChannelForm() {
  const form = document.getElementById('notif-channel-form');
  form.style.display = form.style.display === 'none' ? '' : 'none';
}

async function saveNotificationChannel() {
  const btn = document.getElementById('notif-channel-save-btn');
  const nameEl = document.getElementById('notif-channel-name');
  const urlEl = document.getElementById('notif-channel-url');
  const name = nameEl.value.trim();
  const apprise_url = urlEl.value.trim();
  if (!name || !apprise_url) {
    _feedback('notif-channels-feedback', 'Compila nome e URL.', 'danger');
    return;
  }
  btn.disabled = true;
  _feedback('notif-channels-feedback', 'Salvataggio...');
  try {
    const res = await fetch('/api/notification-channels', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, apprise_url}),
    });
    if (res.ok) {
      nameEl.value = '';
      urlEl.value = '';
      document.getElementById('notif-channel-form').style.display = 'none';
      _feedback('notif-channels-feedback', 'Canale aggiunto.', 'success');
      await loadNotificationChannels();
    } else {
      const d = await safeJson(res);
      _feedback('notif-channels-feedback', d.detail || 'Errore salvataggio.', 'danger');
    }
  } catch (e) { _feedback('notif-channels-feedback', 'Errore di rete.', 'danger'); }
  finally { btn.disabled = false; }
}

async function toggleNotificationChannel(id, enabled) {
  try {
    const res = await fetch(`/api/notification-channels/${id}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled}),
    });
    if (!res.ok) throw new Error();
    _feedback('notif-channels-feedback', enabled ? 'Canale attivo.' : 'Canale disattivato.', 'success');
    await loadNotificationChannels();
  } catch (e) {
    _feedback('notif-channels-feedback', 'Errore aggiornamento.', 'danger');
    await loadNotificationChannels();
  }
}

async function deleteNotificationChannel(id) {
  const channel = _notifChannels.find(c => c.id === id);
  if (!await scConfirm(`Eliminare il canale «${channel ? channel.name : id}»?`)) return;
  try {
    const res = await fetch(`/api/notification-channels/${id}`, {method: 'DELETE'});
    if (!res.ok) throw new Error();
    _feedback('notif-channels-feedback', 'Canale eliminato.', 'success');
    await loadNotificationChannels();
  } catch (e) { _feedback('notif-channels-feedback', 'Errore eliminazione.', 'danger'); }
}

async function testNotificationChannel(id) {
  _feedback('notif-channels-feedback', 'Invio notifica di test...');
  try {
    const res = await fetch(`/api/notification-channels/${id}/test`, {method: 'POST'});
    const data = await safeJson(res);
    if (res.ok && data.ok) {
      _feedback('notif-channels-feedback', 'Notifica di test inviata.', 'success');
      showToast('Notifica di test inviata', 'success');
    } else {
      _feedback('notif-channels-feedback', data.detail || 'Invio fallito: controlla la URL.', 'danger');
    }
  } catch (e) { _feedback('notif-channels-feedback', 'Errore di rete.', 'danger'); }
}

// ── Jellyfin connection ──────────────────────────────────────────────────────

async function loadJellyfinSettings() {
  try {
    const res = await fetch('/api/auth/status');
    const data = await safeJson(res);
    const connected = !!data.jellyfin_url;
    document.getElementById('jf-not-connected').style.display = connected ? 'none' : '';
    document.getElementById('jf-connected').style.display = connected ? '' : 'none';
    if (connected) {
      document.getElementById('jf-connected-url').textContent = data.jellyfin_url;
      document.getElementById('jf-reconfigure-wrap').style.display = can('MANAGE_USERS') ? '' : 'none';
    }
  } catch (e) { console.error('loadJellyfinSettings:', e); }
}

function toggleJellyfinReconfigure() {
  const form = document.getElementById('jf-reconfigure-form');
  form.style.display = form.style.display === 'none' ? '' : 'none';
}

async function connectJellyfin(reconfigure) {
  const prefix = reconfigure ? 'jf-reconf-' : 'jf-';
  const btn = document.getElementById(reconfigure ? 'jf-reconnect-btn' : 'jf-connect-btn');
  const fbId = reconfigure ? 'jf-reconnect-feedback' : 'jf-connect-feedback';
  const url = document.getElementById(prefix + 'url').value.trim();
  const username = document.getElementById(prefix + 'username').value.trim();
  const password = document.getElementById(prefix + 'password').value;
  if (!url || !username) {
    _feedback(fbId, 'Compila URL e utente amministratore.', 'danger');
    return;
  }

  btn.disabled = true;
  _feedback(fbId, 'Connessione...');
  try {
    const res = await fetch('/api/auth/jellyfin-connect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, username, password }),
    });
    const data = await safeJson(res);
    if (!res.ok) {
      _feedback(fbId, data.detail || 'Collegamento fallito.', 'danger');
      btn.disabled = false;
      return;
    }
    // A full reload re-runs initAuth() against the now-real permission set,
    // which is simpler than patching _me and the nav in place.
    _feedback(fbId, 'Collegato. Ricaricamento...', 'success');
    window.location.reload();
  } catch (e) {
    _feedback(fbId, 'Errore di rete.', 'danger');
    btn.disabled = false;
  }
}

async function loadPerfSettings() {
  try {
    const res = await fetch('/api/domain/settings');
    if (!res.ok) return;
    const data = await safeJson(res);
    document.getElementById('setting-max-concurrent').value = data.max_concurrent_downloads ?? 3;
    document.getElementById('setting-max-workers').value = data.max_segment_workers ?? 16;
    document.getElementById('setting-watch-interval').value =
      data.series_watch_interval_minutes ?? 240;
  } catch (e) { /* ignore */ }
}

async function savePerfSettings() {
  const btn = document.getElementById('save-perf-btn');
  const concurrent = parseInt(document.getElementById('setting-max-concurrent').value, 10);
  const workers = parseInt(document.getElementById('setting-max-workers').value, 10);
  const watchInterval = parseInt(document.getElementById('setting-watch-interval').value, 10);
  if (!concurrent || !workers || !watchInterval) {
    _feedback('perf-settings-feedback', 'Valori non validi.', 'danger'); return;
  }
  btn.disabled = true;
  _feedback('perf-settings-feedback', 'Salvataggio...');
  try {
    const res = await fetch('/api/domain/settings', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        max_concurrent_downloads: concurrent,
        max_segment_workers: workers,
        series_watch_interval_minutes: watchInterval,
      }),
    });
    if (res.ok) {
      _feedback('perf-settings-feedback', 'Salvato.', 'success');
      showToast('Performance salvate', 'success');
    } else {
      const d = await safeJson(res);
      _feedback('perf-settings-feedback', d.detail || 'Errore salvataggio.', 'danger');
    }
  } catch (e) { _feedback('perf-settings-feedback', 'Errore di rete.', 'danger'); }
  finally { btn.disabled = false; }
}

async function saveDomain() {
  const domain = document.getElementById('domain-input').value.trim();
  const btn = document.getElementById('save-domain-btn');
  if (!domain) { _feedback('domain-feedback', 'Inserisci un domain.', 'danger'); return; }
  btn.disabled = true;
  _feedback('domain-feedback', 'Verifica in corso...');
  try {
    const res = await fetch('/api/domain', {
      method:'PUT', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({domain}),
    });
    const data = await safeJson(res);
    if (res.ok) {
      currentDomain = data.domain; currentVersion = data.version;
      _feedback('domain-feedback', `OK — versione ${data.version}`, 'success');
      const badge = document.getElementById('domain-badge');
      badge.className = 'badge bg-success';
      badge.textContent = data.domain;
      showToast('Domain salvato', 'success');
    } else {
      _feedback('domain-feedback', data.detail || 'Errore', 'danger');
    }
  } catch(e) {
    _feedback('domain-feedback', 'Errore di rete', 'danger');
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
const _LIB_TYPE_OPTIONS = [{value:'film',label:'Film'},{value:'tv',label:'Serie TV'},{value:'anime',label:'Anime'}];
function renderLibrariesList() {
  const c = document.getElementById('libraries-list');
  if (!c) return;
  if (!_libraries.length) { c.innerHTML = '<p class="text-muted small mb-0">Nessuna libreria.</p>'; return; }
  const usedTypes = _libraries.map(l => l.type);
  c.innerHTML = _libraries.map((lib, i) => {
    const opts = _LIB_TYPE_OPTIONS.map(o => {
      const disabled = o.value !== lib.type && usedTypes.some((t,j) => j !== i && t === o.value) ? 'disabled' : '';
      const selected = o.value === lib.type ? 'selected' : '';
      return `<option value="${o.value}" ${selected} ${disabled}>${o.label}</option>`;
    }).join('');
    return `
    <div class="row g-2 mb-2 align-items-center">
      <div class="col-4"><select class="form-select form-select-sm" id="lib-type-${i}"><option value="">Tipo...</option>${opts}</select></div>
      <div class="col"><input type="text" class="form-control form-control-sm" id="lib-path-${i}" value="${escapeHtml(lib.path)}" placeholder="/srv/nfs/films"></div>
      <div class="col-auto"><button class="btn btn-sm btn-outline-danger" onclick="removeLibrary(${i})"><i class="ti ti-trash"></i></button></div>
    </div>`;
  }).join('');
}
function _syncLibs() {
  _libraries = _libraries.map((_,i) => ({
    type: document.getElementById(`lib-type-${i}`)?.value||'',
    path: document.getElementById(`lib-path-${i}`)?.value||'',
  }));
}
function addLibrary() {
  _syncLibs(); _libraries.push({type:'',path:''}); renderLibrariesList();
  document.getElementById(`lib-path-${_libraries.length-1}`)?.focus();
}
function removeLibrary(idx) { _syncLibs(); _libraries.splice(idx,1); renderLibrariesList(); }
async function saveLibraries() {
  const updated = _libraries.map((_,i) => ({
    type:(document.getElementById(`lib-type-${i}`)?.value||'').trim(),
    path:(document.getElementById(`lib-path-${i}`)?.value||'').trim(),
  })).filter(l => l.type && l.path);
  const excluded = (document.getElementById('excluded-input')?.value||'').split(',').map(s=>s.trim()).filter(Boolean);
  const btn = document.getElementById('save-libraries-btn');
  btn.disabled = true;
  _feedback('libraries-feedback', 'Salvataggio...');
  try {
    const res = await fetch('/api/domain/libraries', {
      method:'PUT', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({libraries:updated, excluded_folders:excluded}),
    });
    if (res.ok) {
      _libraries = updated;
      renderLibrariesList();
      _feedback('libraries-feedback', 'Salvato.', 'success');
      showToast('Librerie salvate','success');
    } else {
      const d = await safeJson(res);
      _feedback('libraries-feedback', d.detail || 'Errore', 'danger');
    }
  } catch(e) { _feedback('libraries-feedback', 'Errore di rete', 'danger'); }
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
        ? (item.poster.startsWith('http') ? item.poster : `/api/image/${item.poster}`)
        : '';
      const card = document.createElement('div');
      card.className = 'col-6 col-sm-4 col-md-3 col-lg-2';
      const posterHtml = posterUrl
        ? `<img src="${posterUrl}" alt="" onerror="this.closest('.poster-wrap').querySelector('.poster-noimg').style.display='flex';this.style.display='none'">`
        : '';
      // Movie cards carry no status ribbon: a movie can be requested again
      // freely (denied/failed/cancelled never block it), so a "richiesto" chip
      // would just read as blocked when it is not. TV and anime keep it — their
      // status is read from the grouped request rows anyway, not per-title.
      const ribbonHtml = isMovie
        ? ''
        : `<div class="status-ribbon" data-ribbon-for="${escapeHtml(String(item.id))}"></div>`;
      card.innerHTML = `
        <div class="result-card" onclick="openDetailModal(${idx})">
          <div class="poster-wrap">
            ${posterHtml}
            <div class="poster-noimg" style="${posterUrl?'display:none':''}">&#127916;</div>
            <div class="poster-overlay"></div>
            ${ribbonHtml}
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
    loadRequestStatuses(results.filter(r => r.type !== 'movie').map(r => String(r.id)));
  } catch(e) {
    if (e.name === 'AbortError') return; // cancelled by new search
    const container = document.getElementById('search-results');
    container.innerHTML=`<div class="col-12"><div class="alert alert-danger">Errore: ${escapeHtml(e.message)}</div></div>`;
  } finally {
    btn.disabled=false; btn.innerHTML='<i class="ti ti-search me-1"></i>Cerca';
  }
}

// ── Request status on the result cards ─────────────────────────────────────────
//
// The one thing worth taking from Seerr: the state of a title is readable on the
// card itself, without opening anything.

const STATUS_RIBBONS = {
  pending:         { label: 'Richiesto',    cls: 'ribbon-pending',   icon: 'ti-clock' },
  approved:        { label: 'Approvato',    cls: 'ribbon-approved',  icon: 'ti-check' },
  downloading:     { label: 'In download',  cls: 'ribbon-download',  icon: 'ti-download' },
  completed:       { label: 'Disponibile',  cls: 'ribbon-available', icon: 'ti-circle-check' },
  available:       { label: 'Disponibile',  cls: 'ribbon-available', icon: 'ti-circle-check' },
  denied:          { label: 'Rifiutato',    cls: 'ribbon-denied',    icon: 'ti-x' },
  failed:          { label: 'Fallito',      cls: 'ribbon-denied',    icon: 'ti-alert-triangle' },
  needs_attention: { label: 'Attenzione',   cls: 'ribbon-attention', icon: 'ti-alert-circle' },
  cancelled:       { label: 'Annullato',    cls: 'ribbon-denied',    icon: 'ti-ban' },
};

async function loadRequestStatuses(externalIds) {
  if (!externalIds.length || !(can('REQUEST') || can('DOWNLOAD'))) return;
  try {
    const res = await fetch('/api/requests/status', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: currentSource, external_ids: externalIds }),
    });
    if (!res.ok) return;
    _requestStatus = await res.json();
    renderRequestRibbons();
  } catch (e) { /* the cards simply stay plain */ }
}

function renderRequestRibbons() {
  document.querySelectorAll('[data-ribbon-for]').forEach(el => {
    const info = _requestStatus[el.dataset.ribbonFor];
    const style = info && STATUS_RIBBONS[info.status];
    if (!style) { el.innerHTML = ''; el.className = 'status-ribbon'; return; }
    el.className = `status-ribbon ${style.cls}`;
    el.innerHTML = `<i class="ti ${style.icon}"></i>${style.label}`;
  });
}

// ── Detail Modal ───────────────────────────────────────────────────────────────

const LANG_NAMES = {
  ita:'Italiano', eng:'English', fra:'Français', spa:'Español',
  deu:'Deutsch', por:'Português', jpn:'日本語', zho:'中文',
  ara:'العربية', rus:'Русский', kor:'한국어',
};
const langName = c => LANG_NAMES[c] || c;

function _getLangSelections() {
  const audio = [...document.querySelectorAll('.lang-audio-check:checked')].map(cb => cb.value);
  const subs  = [...document.querySelectorAll('.lang-sub-check:checked')].map(cb => cb.value);
  return {
    audio: audio.length ? audio : ['ita'],
    subs:  subs,
  };
}

function openDetailModal(idx) {
  const item = _searchResults[idx];
  if (!item) return;
  const isAnime = item.type === 'anime';
  const isMovie = item.type === 'movie';
  const year = itemYear(item);
  const score = item.score ? parseFloat(item.score).toFixed(1) : null;
  const posterUrl = item.poster
    ? (item.poster.startsWith('http') ? item.poster : `/api/image/${item.poster}`)
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

  const scheduleWrap = document.getElementById('detail-schedule-wrap');
  const scheduledAtInput = document.getElementById('detail-scheduled-at');
  scheduledAtInput.value = '';
  // Scheduling a download is part of the download privilege; a requester picks
  // tracks and the approver decides when it runs.
  scheduleWrap.style.display = can('DOWNLOAD') ? '' : 'none';

  const requestOnly = !can('DOWNLOAD');
  const btn = document.getElementById('detail-action-btn');
  const readAt = () => (can('DOWNLOAD') && scheduledAtInput.value)
    ? new Date(scheduledAtInput.value).toISOString() : null;

  if (isAnime) {
    btn.className='btn btn-success'; btn.innerHTML='<i class="ti ti-list me-1"></i>Episodi';
    btn.onclick = () => {
      const { audio, subs } = _getLangSelections();
      hideModal('detail-modal');
      openAnimeBrowser(item.id, item.name, item.type, year, readAt(), audio, subs);
    };
  } else if (isMovie) {
    btn.className = requestOnly ? 'btn btn-warning' : 'btn btn-primary';
    btn.innerHTML = requestOnly
      ? '<i class="ti ti-send me-1"></i>Richiedi'
      : '<i class="ti ti-download me-1"></i>Scarica';
    btn.onclick = () => {
      const { audio, subs } = _getLangSelections();
      hideModal('detail-modal');
      startFilmDownload(item.id, item.name, year, readAt(), audio, subs, item.poster);
    };
  } else {
    btn.className='btn btn-success'; btn.innerHTML='<i class="ti ti-list me-1"></i>Episodi';
    btn.onclick = () => {
      const { audio, subs } = _getLangSelections();
      hideModal('detail-modal');
      openEpisodeBrowser(item.id, item.name, item.slug, year, readAt(), audio, subs, item.poster);
    };
  }

  const langsEl = document.getElementById('detail-langs');
  if (isAnime) {
    langsEl.innerHTML = '';
    showModal('detail-modal');
    return;
  }

  langsEl.innerHTML='<span class="spinner-border spinner-border-sm me-1"></span>Caricamento lingue...';
  showModal('detail-modal');

  const p = new URLSearchParams({ type:isMovie?'movie':'tv', slug:item.slug||'', version:currentVersion||'' });
  fetch(`/api/search/languages/${item.id}?${p}`)
    .then(r => r.ok ? r.json() : null)
    .then(info => {
      if (!info) { langsEl.innerHTML=''; return; }
      let html='';
      if (info.audio?.length) {
        const audioHtml = info.audio.map(c => {
          const checked = (c === 'ita' || (info.audio.length === 1)) ? 'checked' : '';
          return `<label class="me-2 mb-1" style="cursor:pointer"><input type="checkbox" class="lang-audio-check me-1" value="${escapeHtml(c)}" ${checked}><span class="badge bg-blue-lt">${langName(c)}</span></label>`;
        }).join('');
        html+=`<div class="mb-1"><span class="text-muted me-1"><i class="ti ti-volume ti-sm"></i> Audio:</span>${audioHtml}</div>`;
      } else {
        html+=`<div class="mb-1"><span class="text-muted me-1"><i class="ti ti-volume ti-sm"></i> Audio:</span><span class="text-muted fst-italic">originale</span></div>`;
      }
      if (info.subtitles?.length) {
        const subHtml = info.subtitles.map(c => {
          const checked = (c === 'ita' || c === 'eng') ? 'checked' : '';
          return `<label class="me-2 mb-1" style="cursor:pointer"><input type="checkbox" class="lang-sub-check me-1" value="${escapeHtml(c)}" ${checked}><span class="badge bg-teal-lt">${langName(c)}</span></label>`;
        }).join('');
        html+=`<div><span class="text-muted me-1"><i class="ti ti-subtitles ti-sm"></i> Sub:</span>${subHtml}</div>`;
      }
      langsEl.innerHTML=html;
    })
    .catch(()=>{ langsEl.innerHTML=''; });
}

// ── Requesting ─────────────────────────────────────────────────────────────────

// Same form, different action: without the download permission the choice of
// audio and subtitles becomes a request instead of a job.
async function submitRequest(payload, label) {
  try {
    const res = await fetch('/api/requests', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await safeJson(res);
    if (!res.ok) { showToast(data.detail || 'Errore', 'danger'); return false; }

    const status = data.request.status;
    if (status === 'available') showToast(`${label} è già in libreria.`, 'info');
    else if (!data.created) showToast(`${label} era già stato richiesto: sarai avvisato.`, 'info');
    else showToast(`Richiesta inviata: ${label}`, 'success');

    _requestStatus[String(payload.external_id)] = { id: data.request.id, status };
    renderRequestRibbons();
    refreshNotifications();
    return true;
  } catch (e) { showToast('Errore di rete', 'danger'); return false; }
}

// ── Film download ──────────────────────────────────────────────────────────────

async function startFilmDownload(id, title, year=null, scheduledAt=null, audioLangs=null, subLangs=null, poster=null) {
  if (!can('DOWNLOAD')) {
    const ok = await submitRequest({
      source: currentSource, media_type: 'film', external_id: String(id),
      title, year, poster,
      audio_languages: audioLangs || ['ita'],
      subtitle_languages: subLangs || [],
    }, title);
    if (ok) showPage('my-requests');
    return;
  }
  try {
    const endpoint = scheduledAt ? '/api/download/schedule/film' : '/api/download/film';
    const body = {
      id, title, year,
      audio_languages: audioLangs || ['ita'],
      subtitle_languages: subLangs || ['ita', 'eng'],
    };
    if (scheduledAt) body.scheduled_at = scheduledAt;
    const res = await fetch(endpoint, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body),
    });
    const data = await safeJson(res);
    if (res.ok) {
      const msg = scheduledAt
        ? `Programmato: ${title} — ${new Date(scheduledAt).toLocaleString('it-IT')}`
        : `Download avviato: ${title}`;
      showToast(msg, 'success');
      showPage('downloads');
    } else showToast(data.detail||'Errore','danger');
  } catch(e) { showToast('Errore di rete','danger'); }
}

// ── Episode Browser ────────────────────────────────────────────────────────────

let _epCtx = {};

async function openEpisodeBrowser(tvId, tvName, slug, year=null, scheduledAt=null, audioLangs=null, subLangs=null, poster=null) {
  _epCtx = { tvId, tvName, slug, year, scheduledAt, token:null, episodes:[], currentSeason:null, poster,
    audioLangs: audioLangs || ['ita'], subLangs: subLangs || ['ita', 'eng'] };
  document.getElementById('episode-modal-title').textContent = tvName;
  document.getElementById('season-tabs-wrap').style.display='none';
  document.getElementById('dl-whole-series-btn').style.display='none';
  document.getElementById('episode-modal-body').innerHTML =
    '<div class="text-center py-4"><div class="spinner-border text-primary" role="status"></div></div>';
  showModal('episode-modal');
  checkWatchStatus('tv');

  try {
    const [tokenData, seasonsData] = await Promise.all([
      fetch(`/api/tv/${tvId}/token`).then(r=>r.json()),
      fetch(`/api/tv/${tvId}/seasons?slug=${encodeURIComponent(slug)}&version=${encodeURIComponent(currentVersion)}`).then(r=>r.json()),
    ]);
    _epCtx.token = tokenData.token;
    _epCtx.seasonsCount = seasonsData.seasons_count;
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
  const wrap = document.getElementById('season-tabs-wrap');
  wrap.style.display = 'flex';
  const dlAllBtn = document.getElementById('dl-whole-series-btn');
  dlAllBtn.style.display = count > 1 ? '' : 'none';
}

async function loadSeason(season) {
  const { tvId, slug, token } = _epCtx;
  const container = document.getElementById('episode-modal-body');
  container.innerHTML='<div class="text-center py-3"><div class="spinner-border text-primary" role="status"></div></div>';
  try {
    const res = await fetch(`/api/tv/${tvId}/seasons/${season}/episodes?slug=${encodeURIComponent(slug)}&version=${encodeURIComponent(currentVersion)}&token=${encodeURIComponent(token)}`);
    const eps = await safeJson(res);
    if (!res.ok) { container.innerHTML=`<div class="alert alert-danger">${escapeHtml(eps.detail||'Errore caricamento episodi')}</div>`; return; }
    if (!Array.isArray(eps)) { container.innerHTML=`<div class="alert alert-danger">Risposta non valida dal server</div>`; return; }
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
  const { tvId, tvName, slug, year, scheduledAt, token, episodes, currentSeason, audioLangs, subLangs, poster } = _epCtx;
  const ep = episodes[epIndex];
  const label = `${tvName} S${String(currentSeason).padStart(2,'0')}E${String(ep.n).padStart(2,'0')}`;

  if (!can('DOWNLOAD')) {
    await submitRequest({
      source: 'streamingcommunity', media_type: 'episode', external_id: String(tvId),
      slug, title: tvName, year, poster,
      season: currentSeason, episode_number: String(ep.n),
      audio_languages: audioLangs || ['ita'],
      subtitle_languages: subLangs || [],
    }, label);
    return;
  }

  const endpoint = scheduledAt ? '/api/download/schedule/episode' : '/api/download/episode';
  const body = {
    tv_id: tvId, eps: episodes, ep_index: epIndex, token,
    tv_name: tvName, season: currentSeason, year,
    audio_languages: audioLangs || ['ita'],
    subtitle_languages: subLangs || ['ita', 'eng'],
  };
  if (scheduledAt) body.scheduled_at = scheduledAt;
  try {
    const res = await fetch(endpoint, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body),
    });
    const data = await safeJson(res);
    if (res.ok) showToast(scheduledAt ? `Programmato: ${label}` : `In coda: ${label}`, 'success');
    else showToast(data.detail||'Errore','danger');
  } catch(e) { showToast('Errore di rete','danger'); }
}

// Whole seasons and whole series are one call: the server lists the episodes
// itself and queues them as a batch. That is also what lets it report the season
// once at the end instead of pinging for every episode.
async function _startBatch(path, body, modalId) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await safeJson(res);
  if (!res.ok) {
    showToast(data.detail || 'Errore avviando i download', 'danger');
    return false;
  }
  showToast(
    body.scheduled_at ? `${data.count} episodi programmati` : `${data.count} episodi in coda`,
    'success',
  );
  hideModal(modalId);
  showPage('downloads');
  return true;
}

async function downloadWholeSeason(season) {
  const { tvId, slug, tvName, year, episodes, scheduledAt, audioLangs, subLangs } = _epCtx;
  const label = scheduledAt ? 'Programmare' : 'Aggiungere alla coda';
  if (!await scConfirm(`${label} tutti i ${episodes.length} episodi della stagione ${season}?`)) return;
  await _startBatch('/api/download/season', {
    tv_id: tvId, slug, tv_name: tvName, season, year,
    audio_languages: audioLangs, subtitle_languages: subLangs,
    scheduled_at: scheduledAt || null,
  }, 'episode-modal');
}

async function downloadWholeSeries() {
  const { tvId, slug, tvName, year, scheduledAt, seasonsCount, audioLangs, subLangs } = _epCtx;
  const label = scheduledAt ? 'Programmare' : 'Aggiungere alla coda';
  if (!await scConfirm(`${label} tutte le ${seasonsCount} stagioni?`)) return;
  await _startBatch('/api/download/series', {
    tv_id: tvId, slug, tv_name: tvName, year,
    audio_languages: audioLangs, subtitle_languages: subLangs,
    scheduled_at: scheduledAt || null,
  }, 'episode-modal');
}

// ── Anime Browser (AnimeUnity) ─────────────────────────────────────────────────

async function openAnimeBrowser(animeId, animeName, animeType, animeYear = null, scheduledAt = null, audioLangs = null, subLangs = null) {
  // Auto-detect if film (1 episode) but allow user override
  const isAutoFilm = _searchResults.find(r => r.id === animeId)?.episodes_count === 1;
  const effectiveType = (isAutoFilm && animeType === 'anime') ? 'movie' : animeType;

  _animeCtx = { animeId, animeName, animeType: effectiveType, animeYear, scheduledAt, episodes: [], isAutoFilm,
    audioLangs: audioLangs || ['ita'], subLangs: subLangs || ['ita', 'eng'] };
  document.getElementById('anime-modal-title').textContent = animeName;
  document.getElementById('anime-modal-body').innerHTML =
    '<div class="text-center py-4"><div class="spinner-border text-primary" role="status"></div></div>';
  showModal('anime-modal');
  checkWatchStatus('anime');

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
  const { animeId, animeName, animeType, animeYear, scheduledAt, episodes, audioLangs, subLangs } = _animeCtx;
  const episode = episodes[epIndex];
  const label = `${animeName} E${episode.number}`;

  if (!can('DOWNLOAD')) {
    await submitRequest({
      source: 'animeunity', media_type: 'anime', external_id: String(animeId),
      title: animeName, year: animeYear, anime_type: animeType,
      episode_number: String(episode.number),
      audio_languages: audioLangs || ['ita'],
      subtitle_languages: subLangs || [],
    }, label);
    return;
  }

  const endpoint = scheduledAt ? '/api/download/schedule/anime' : '/api/download/anime';
  const body = {
    anime_id: animeId, episode, anime_name: animeName, anime_type: animeType, year: animeYear,
    audio_languages: audioLangs || ['ita'],
    subtitle_languages: subLangs || ['ita', 'eng'],
  };
  if (scheduledAt) body.scheduled_at = scheduledAt;
  try {
    const res = await fetch(endpoint, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await safeJson(res);
    if (res.ok) showToast(scheduledAt ? `Programmato: ${label}` : `In coda: ${label}`, 'success');
    else showToast(data.detail || 'Errore', 'danger');
  } catch(e) { showToast('Errore di rete', 'danger'); }
}

function toggleAnimeType(newType) {
  _animeCtx.animeType = newType;
  const { animeId, animeName, animeYear, scheduledAt, audioLangs, subLangs } = _animeCtx;
  openAnimeBrowser(animeId, animeName, newType, animeYear, scheduledAt, audioLangs, subLangs);
}

async function downloadAllAnime() {
  const { animeId, animeName, animeType, animeYear, episodes, scheduledAt,
          audioLangs, subLangs } = _animeCtx;
  const label = scheduledAt ? 'Programmare' : 'Aggiungere alla coda';
  if (!await scConfirm(`${label} tutti i ${episodes.length} episodi?`)) return;
  await _startBatch('/api/download/anime-all', {
    anime_id: String(animeId), anime_name: animeName, anime_type: animeType,
    year: animeYear,
    audio_languages: audioLangs, subtitle_languages: subLangs,
    scheduled_at: scheduledAt || null,
  }, 'anime-modal');
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
      case 'job_dismissed':
        _jobs.delete(msg.job_id);
        document.getElementById(`job-card-${msg.job_id}`)?.remove();
        updateActiveBadge();
        break;
      case 'notification':
        // A bare signal: the payload lives behind /api/notifications, which is
        // scoped to the caller, so a shared stream leaks nothing.
        refreshNotifications();
        refreshQueueBadge();
        if (can('MANAGE_REQUESTS') &&
            document.getElementById('page-requests').style.display !== 'none') {
          loadRequestQueue();
        }
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
  scheduled:'Programmato', queued:'In coda', running:'In corso', joining:'Finalizzazione',
  audio:'Audio', merging:'Unione', done:'Completato', error:'Errore', cancelled:'Annullato',
};
const PHASE_BADGE = {
  scheduled:'bg-yellow-lt', queued:'bg-secondary-lt', running:'bg-blue-lt', joining:'bg-yellow-lt',
  audio:'bg-teal-lt', merging:'bg-purple-lt', done:'bg-success-lt',
  error:'bg-danger-lt', cancelled:'bg-secondary-lt',
};
const PHASE_BAR = {
  running:'bg-blue', joining:'phase-bar-joining bg-warning',
  audio:'phase-bar-audio bg-teal', merging:'phase-bar-merging bg-purple',
  done:'phase-bar-done bg-success', error:'phase-bar-error bg-danger',
};
const PHASE_BORDER_MAP = {
  scheduled:'var(--yellow)', queued:'var(--text-dim)', running:'var(--blue)', joining:'var(--yellow)',
  audio:'var(--teal)', merging:'var(--purple)', done:'var(--green)',
  error:'var(--accent)', cancelled:'var(--text-dim)',
};

function _stepLabel(phase) {
  const map = { video:'Video', joining:'Join', merging:'Merge', done:'Fine', audio:'Audio' };
  if (map[phase]) return map[phase];
  if (phase && phase.startsWith('audio_')) return 'Audio ' + phase.slice(6).toUpperCase();
  return phase;
}

function _buildStepsHtml(jobId, phases, currentPhase, status) {
  if (!phases || phases.length < 2) return '';
  let activeIdx;
  if (status === 'done') {
    activeIdx = phases.length;
  } else if (status === 'queued' || status === 'scheduled') {
    activeIdx = -1;
  } else {
    const lookup = currentPhase || 'video';
    activeIdx = phases.indexOf(lookup);
    if (activeIdx < 0) activeIdx = phases.indexOf('video') >= 0 ? 0 : -1;
  }
  const items = phases.map((p, i) => {
    let cls = 'jp';
    if (activeIdx === phases.length || i < activeIdx) cls += ' complete';
    else if (i === activeIdx) cls += ' active';
    return `<span class="${cls}" data-phase="${p}">${_stepLabel(p)}</span>`;
  }).join('');
  return `<div class="job-phases" id="job-steps-${jobId}">${items}</div>`;
}

function _updateSteps(jobId, phase) {
  const job = _jobs.get(jobId);
  if (!job || !job.phases || job.phases.length < 2) return;
  const container = document.getElementById(`job-steps-${jobId}`);
  if (!container) return;
  const phases = job.phases;
  const isDone = job.status === 'done';
  const activeIdx = isDone ? phases.length : phases.indexOf(phase || 'video');
  if (activeIdx < 0) return;
  container.querySelectorAll('.jp').forEach((el, i) => {
    el.className = 'jp';
    if (activeIdx === phases.length || i < activeIdx) el.className += ' complete';
    else if (i === activeIdx) el.className += ' active';
  });
}

function _phaseLabel(phase) {
  if (!phase) return '';
  if (PHASE_LABELS[phase]) return PHASE_LABELS[phase];
  if (phase.startsWith('audio_')) return 'Audio ' + phase.slice(6).toUpperCase();
  return phase;
}
function _phaseBadge(phase) {
  if (PHASE_BADGE[phase]) return PHASE_BADGE[phase];
  if (phase && phase.startsWith('audio_')) return 'bg-teal-lt';
  return 'bg-secondary-lt';
}
function _phaseBar(phase) {
  if (PHASE_BAR[phase]) return PHASE_BAR[phase];
  if (phase && phase.startsWith('audio_')) return 'phase-bar-audio bg-teal';
  return 'bg-secondary';
}
function _phaseBorder(phase) {
  if (PHASE_BORDER_MAP[phase]) return PHASE_BORDER_MAP[phase];
  if (phase && phase.startsWith('audio_')) return 'var(--teal)';
  return 'transparent';
}

function _buildJobCard(j) {
  const phase = _jobPhases[j.job_id] || j.status;
  const isActive = j.status==='running' || j.status==='queued' || j.status==='scheduled';
  const isMovie = j.type==='film';
  const isAnimeJob = j.type==='anime';
  const pct = j.progress?.pct||0;
  const barClass = _phaseBar(phase);
  const animated = isActive && j.status!=='queued' ? ' progress-bar-striped progress-bar-animated' : '';
  const barWidth = j.status==='queued' ? 0 : (j.status==='done' ? 100 : pct);
  const badgeClass = _phaseBadge(phase);
  const label = _phaseLabel(phase);
  const borderColor = _phaseBorder(phase);

  const speed = j.progress?.speed;
  const bytesSpeed = j.progress?.bytes_speed;
  const eta = j.progress?.eta;
  const speedStr = (isActive && j.status!=='queued')
    ? (bytesSpeed > 0 ? formatSize(bytesSpeed) + '/s' : (speed > 0 ? `${speed} seg/s` : ''))
    : '';
  const etaStr = eta ? fmtEta(eta) : '';
  const infoStr = [speedStr, etaStr].filter(Boolean).join(' · ');

  const fireBtn = j.status === 'scheduled'
    ? `<button class="btn btn-sm btn-outline-success ms-1" onclick="fireNow('${j.job_id}')" title="Lancia subito">
         <i class="ti ti-player-play"></i>
       </button>` : '';
  const stopBtn = isActive
    ? `<button class="btn btn-sm btn-outline-danger ms-1" onclick="cancelJob('${j.job_id}')" title="Interrompi">
         <i class="ti ti-player-stop"></i>
       </button>` : '';

  const rawTs = j.scheduled_at || j.created_at;
  const dateStr = rawTs
    ? new Date(/[Z+]/.test(rawTs)?rawTs:rawTs+'Z').toLocaleString('it-IT',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})
    : '';
  const dateLabel = j.scheduled_at ? `⏰ ${dateStr}` : dateStr;

  const stepsHtml = _buildStepsHtml(j.job_id, j.phases, phase, j.status);
  return `<div class="card mb-2 job-card${j.status==='done'?' is-done':''}${j.status==='error'?' is-error':''}" id="job-card-${j.job_id}" style="border-left:3px solid ${borderColor} !important">
    <div class="card-body py-2 px-3">
      <div class="d-flex align-items-center gap-2">
        <span class="badge ${isMovie?'bg-blue-lt':isAnimeJob?'bg-purple-lt':'bg-green-lt'} flex-shrink-0">${isMovie?'Film':isAnimeJob?'Anime':'TV'}</span>
        <span class="fw-medium text-truncate flex-1" style="min-width:0" title="${escapeHtml(j.title)}">${escapeHtml(j.title)}</span>
        <span class="badge ${badgeClass} flex-shrink-0" id="job-badge-${j.job_id}">${label}</span>
        <span id="job-fire-${j.job_id}">${fireBtn}</span>
        ${stopBtn ? `<span id="job-stop-${j.job_id}">${stopBtn}</span>` : `<span id="job-stop-${j.job_id}"></span>`}
      </div>
      ${stepsHtml}
      <div class="progress my-1" style="height:5px">
        <div class="progress-bar ${barClass}${animated} job-progress-bar" id="job-bar-${j.job_id}" style="width:${barWidth}%"></div>
      </div>
      <div class="d-flex justify-content-between align-items-center">
        <small class="text-muted" id="job-info-${j.job_id}">${infoStr || (j.status==='error' ? escapeHtml(j.error||'Errore') : (j.status==='done'?'Completato':''))}</small>
        <small class="text-muted">${dateLabel}</small>
      </div>
    </div>
  </div>`;
}

function renderAllJobCards() {
  const container = document.getElementById('jobs-container');
  const empty = document.getElementById('jobs-empty');
  if (!_jobs.size) {
    empty.style.display=''; container.innerHTML='';
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
  const isActive = j.status==='running' || j.status==='queued' || j.status==='scheduled';

  // Update card classes and border
  card.classList.toggle('is-done', j.status==='done');
  card.classList.toggle('is-error', j.status==='error');
  card.style.borderLeftColor = _phaseBorder(phase);

  // Update badge
  const badge = document.getElementById(`job-badge-${jobId}`);
  if (badge) {
    badge.className = `badge ${_phaseBadge(phase)} flex-shrink-0`;
    badge.textContent = _phaseLabel(phase);
  }

  // Update progress bar
  const bar = document.getElementById(`job-bar-${jobId}`);
  if (bar) {
    const barClass = _phaseBar(phase);
    const animated = isActive && j.status!=='queued' ? ' progress-bar-striped progress-bar-animated' : '';
    bar.className = `progress-bar ${barClass}${animated} job-progress-bar`;
    bar.style.width = (j.status==='queued' ? 0 : (j.status==='done' ? 100 : (j.progress?.pct||0))) + '%';
  }

  // Update fire/stop buttons
  const fire = document.getElementById(`job-fire-${jobId}`);
  if (fire) fire.innerHTML = j.status === 'scheduled'
    ? `<button class="btn btn-sm btn-outline-success ms-1" onclick="fireNow('${j.job_id}')" title="Lancia subito"><i class="ti ti-player-play"></i></button>`
    : '';
  const stop = document.getElementById(`job-stop-${jobId}`);
  if (stop) {
    stop.innerHTML = isActive && j.status !== 'scheduled'
      ? `<button class="btn btn-sm btn-outline-danger ms-1" onclick="cancelJob('${j.job_id}')" title="Interrompi"><i class="ti ti-player-stop"></i></button>`
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
  const active = [..._jobs.values()].filter(j=>j.status==='running'||j.status==='queued'||j.status==='scheduled');
  const pill = document.getElementById('dl-active-pill');
  const countEl = document.getElementById('dl-active-count');
  if (active.length) {
    pill.style.display=''; countEl.textContent=active.length;
  } else {
    pill.style.display='none';
  }
}

function updateActiveBadge() {
  const count = [..._jobs.values()].filter(j=>j.status==='running'||j.status==='queued'||j.status==='scheduled').length;
  const badge = document.getElementById('active-jobs-badge');
  if (count>0) { badge.style.display=''; badge.textContent=count; }
  else badge.style.display='none';
  updateActiveSection();
}

function handleProgressEvent(msg) {
  const job = _jobs.get(msg.job_id);
  if (job) {
    job.progress = { current:msg.current, total:msg.total, pct:msg.pct, speed:msg.speed||0, bytes_speed:msg.bytes_speed||0, eta:msg.eta||null };
    const phase = msg.phase || _jobPhases[msg.job_id] || 'running';
    const prevPhase = _jobPhases[msg.job_id];
    _jobPhases[msg.job_id] = phase;
    if (phase !== prevPhase) _updateSteps(msg.job_id, phase);
  }
  // Update bar and info without full card rebuild
  const bar = document.getElementById(`job-bar-${msg.job_id}`);
  if (bar) bar.style.width = msg.pct + '%';
  const info = document.getElementById(`job-info-${msg.job_id}`);
  if (info) {
    const speedStr = msg.bytes_speed > 0 ? formatSize(msg.bytes_speed) + '/s' : (msg.speed > 0 ? `${msg.speed} seg/s` : '');
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
    badge.className = `badge ${_phaseBadge(phase)} flex-shrink-0`;
    badge.textContent = _phaseLabel(phase);
  }
  const card = document.getElementById(`job-card-${jobId}`);
  if (card) card.style.borderLeftColor = _phaseBorder(phase);
  const bar = document.getElementById(`job-bar-${jobId}`);
  if (bar) {
    bar.className = `progress-bar ${_phaseBar(phase)} progress-bar-striped progress-bar-animated job-progress-bar`;
    const isIndeterminate = phase === 'joining' || phase === 'merging' || phase.startsWith('audio_');
    if (isIndeterminate) bar.style.width = '100%';
  }
  const info = document.getElementById(`job-info-${jobId}`);
  if (info) {
    const isIndeterminate = phase === 'joining' || phase === 'merging';
    if (isIndeterminate) info.textContent = _phaseLabel(phase) + '...';
  }
  _updateSteps(jobId, phase);
}

function handleDoneEvent(jobId, outputPath) {
  delete _jobPhases[jobId];
  const job = _jobs.get(jobId);
  if (job) { job.status='done'; job.output_path=outputPath; }
  _updateSteps(jobId, 'done');

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

async function fireNow(jobId) {
  try {
    const res = await fetch(`/api/download/${jobId}/fire`, {method:'POST'});
    if (!res.ok) { const d=await safeJson(res); showToast(d.detail||'Errore','danger'); }
  } catch(e) { showToast('Errore di rete','danger'); }
}

async function cancelJob(jobId) {
  if (!await scConfirm('Interrompere il download?')) return;
  try {
    const res = await fetch(`/api/download/${jobId}`, {method:'DELETE'});
    if (!res.ok) { const d=await safeJson(res); showToast(d.detail||'Errore','danger'); }
  } catch(e) { showToast('Errore di rete','danger'); }
}

// The list is normally kept current by the event stream. This re-reads it from
// the server for the times that is not enough — a laptop coming back from
// sleep, a proxy that closed the connection, a tab that fell far behind.
async function refreshJobs() {
  const btn = document.getElementById('dl-refresh-btn');
  if (btn) btn.disabled = true;
  try {
    const res = await fetch('/api/jobs');
    if (!res.ok) { showToast('Impossibile aggiornare i download', 'danger'); return; }
    const jobs = await safeJson(res);
    _jobs.clear();
    jobs.forEach(j => _jobs.set(j.job_id, j));
    renderAllJobCards();
    updateActiveBadge();
  } catch (e) {
    showToast('Errore di rete', 'danger');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function clearFinished() {
  const finished = [..._jobs.entries()]
    .filter(([,j]) => j.status==='done'||j.status==='error'||j.status==='cancelled')
    .map(([id]) => id);
  await Promise.allSettled(finished.map(id =>
    fetch(`/api/download/${id}`, {method:'DELETE'})
  ));
  // UI cleanup handled by job_dismissed SSE; also clean locally in case SSE lags
  for (const id of finished) {
    _jobs.delete(id);
    document.getElementById(`job-card-${id}`)?.remove();
  }
  if (!_jobs.size) {
    const container = document.getElementById('jobs-container');
    const empty = document.getElementById('jobs-empty');
    empty.style.display='';
    container.innerHTML='';
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
let _fmSearchActive = false;
let _fmSearchTimeout = null;

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
    const renameBtn = e.target.closest('[data-rename-path]');
    if (renameBtn && renameBtn.closest('#files-left-pane')) {
      renamePath(renameBtn.dataset.renamePath, renameBtn.dataset.renameName); return;
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
  if (batchMoveBtn) batchMoveBtn.addEventListener('click', async () => {
    if (!_selectedPaths.size) return;
    const dest = await scPrompt('Percorso cartella di destinazione (vuoto = radice):','');
    if (dest === null) return;
    batchMoveToPath([..._selectedPaths], dest);
  });
  const batchDeleteBtn = document.getElementById('fm-batch-delete-btn');
  if (batchDeleteBtn) batchDeleteBtn.addEventListener('click', async () => {
    if (!_selectedPaths.size) return;
    if (!await scConfirm(`Eliminare ${_selectedPaths.size} elementi selezionati?`)) return;
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

function onFmSearchInput(value) {
  const clearBtn = document.getElementById('fm-search-clear');
  if (clearBtn) clearBtn.style.display = value ? '' : 'none';
  clearTimeout(_fmSearchTimeout);
  if (!value || value.trim().length < 2) {
    if (_fmSearchActive) {
      _fmSearchActive = false;
      if (_cachedTree) renderFileTree(_cachedTree);
      else loadFiles();
    }
    return;
  }
  _fmSearchTimeout = setTimeout(() => searchFiles(value.trim()), 300);
}

function clearFmSearch() {
  const input = document.getElementById('fm-search-input');
  if (input) input.value = '';
  const clearBtn = document.getElementById('fm-search-clear');
  if (clearBtn) clearBtn.style.display = 'none';
  _fmSearchActive = false;
  if (_cachedTree) renderFileTree(_cachedTree);
  else loadFiles();
}

async function searchFiles(query) {
  const pane = document.getElementById('files-left-pane');
  if (!pane) return;
  _fmSearchActive = true;
  pane.innerHTML = '<div class="text-center py-4 text-muted" style="font-size:13px"><div class="spinner-border spinner-border-sm me-2"></div>Ricerca...</div>';
  try {
    const res = await fetch(`/api/files/search?q=${encodeURIComponent(query)}`);
    const results = await safeJson(res);
    if (!res.ok) {
      pane.innerHTML = `<div class="text-danger text-center py-4 px-3">${escapeHtml(results.detail || 'Errore ricerca')}</div>`;
      return;
    }
    renderSearchResults(results, query);
  } catch(e) {
    pane.innerHTML = `<div class="text-danger text-center py-4">Errore: ${escapeHtml(e.message)}</div>`;
  }
}

function renderSearchResults(results, query) {
  const pane = document.getElementById('files-left-pane');
  if (!pane) return;
  _allVisiblePaths = [];

  if (!results || !results.length) {
    pane.innerHTML = `<div class="text-muted text-center py-5" style="font-size:13px">
      <i class="ti ti-search-off" style="font-size:2em;display:block;margin-bottom:8px;opacity:.4"></i>
      Nessun risultato per <strong>${escapeHtml(query)}</strong>
    </div>`;
    return;
  }

  const frag = document.createDocumentFragment();
  results.forEach(item => {
    _allVisiblePaths.push(item.path);
    const row = document.createElement('div');
    row.className = 'fm-row';
    if (_selectedPaths.has(item.path)) row.classList.add('fm-selected');
    row.style.paddingLeft = '10px';
    row.setAttribute('draggable', 'true');
    row.dataset.dragPath = item.path;
    const checked = _selectedPaths.has(item.path) ? 'checked' : '';
    const parentPath = item.path.includes('/') ? item.path.substring(0, item.path.lastIndexOf('/')) : '';
    const pathMeta = parentPath
      ? `<span class="fm-meta" style="font-size:11px;opacity:.55;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(parentPath)}">${escapeHtml(parentPath)}</span>`
      : '';
    if (item.type === 'directory') {
      row.classList.add('fm-drop-zone');
      row.dataset.dropPath = item.path;
      row.innerHTML = `
        <input type="checkbox" class="fm-check" data-select-path="${escapeHtml(item.path)}" ${checked}>
        <i class="ti ti-folder-filled text-yellow" style="flex-shrink:0"></i>
        <span class="fm-name">${escapeHtml(item.name)}</span>
        ${pathMeta}
        <div class="fm-actions">
          <button class="btn btn-sm btn-outline-secondary" data-rename-path="${escapeHtml(item.path)}" data-rename-name="${escapeHtml(item.name)}"><i class="ti ti-pencil"></i></button>
          <button class="btn btn-sm btn-outline-danger" data-delete-path="${escapeHtml(item.path)}" data-delete-name="${escapeHtml(item.name)}" data-delete-dir="1"><i class="ti ti-trash"></i></button>
        </div>`;
    } else {
      const size = formatSize(item.size);
      const isMp4 = item.name.toLowerCase().endsWith('.mp4');
      row.innerHTML = `
        <input type="checkbox" class="fm-check" data-select-path="${escapeHtml(item.path)}" ${checked}>
        <i class="ti ${isMp4 ? 'ti-file-type-mp4 text-red' : 'ti-file text-muted'}" style="flex-shrink:0"></i>
        <span class="fm-name">${escapeHtml(item.name)}</span>
        ${pathMeta}
        <span class="fm-meta">${size}</span>
        <div class="fm-actions">
          ${isMp4 ? `<button class="btn btn-sm btn-outline-primary" data-play-path="${escapeHtml(item.path)}" data-play-name="${escapeHtml(item.name)}"><i class="ti ti-player-play"></i></button>` : ''}
          <button class="btn btn-sm btn-outline-secondary" data-rename-path="${escapeHtml(item.path)}" data-rename-name="${escapeHtml(item.name)}"><i class="ti ti-pencil"></i></button>
          <a class="btn btn-sm btn-outline-secondary" href="/api/files/download/${encodeURI(item.path)}"><i class="ti ti-download"></i></a>
          <button class="btn btn-sm btn-outline-danger" data-delete-path="${escapeHtml(item.path)}" data-delete-name="${escapeHtml(item.name)}"><i class="ti ti-trash"></i></button>
        </div>`;
    }
    frag.appendChild(row);
  });

  const header = document.createElement('div');
  header.style.cssText = 'padding:6px 12px 5px;font-size:11px;color:var(--text-dim);border-bottom:1px solid var(--border)';
  header.textContent = `${results.length} risultat${results.length === 1 ? 'o' : 'i'} per "${query}"`;
  pane.innerHTML = '';
  pane.appendChild(header);
  pane.appendChild(frag);

  for (const p of _selectedPaths) {
    if (!_allVisiblePaths.includes(p)) _selectedPaths.delete(p);
  }
  syncSelectionUI();
}

async function loadFiles() {
  const pane = document.getElementById('files-left-pane');
  if (!pane) return;
  // Not awaited: the free-space readout is an aside, and stat'ing a sleeping
  // NFS mount must not hold up the file list.
  loadDiskUsage();
  // If search is active, refresh search results instead of reloading the tree
  const searchInput = document.getElementById('fm-search-input');
  if (_fmSearchActive && searchInput && searchInput.value.trim().length >= 2) {
    return searchFiles(searchInput.value.trim());
  }
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
      const actions = `
        <div class="fm-actions">
          <button class="btn btn-sm btn-outline-secondary" data-rename-path="${escapeHtml(item.path)}" data-rename-name="${escapeHtml(item.name)}"><i class="ti ti-pencil"></i></button>
          <button class="btn btn-sm btn-outline-danger"
                  data-delete-path="${escapeHtml(item.path)}"
                  data-delete-name="${escapeHtml(item.name)}"
                  data-delete-dir="1"><i class="ti ti-trash"></i></button>
        </div>`;
      if (item.empty) {
        row.innerHTML=`
          <input type="checkbox" class="fm-check" data-select-path="${escapeHtml(item.path)}" ${checked}>
          <span style="min-width:22px;flex-shrink:0"></span>
          <i class="ti ti-folder text-muted" style="flex-shrink:0;opacity:0.45"></i>
          <span class="fm-name text-muted">${escapeHtml(item.name)}</span>
          ${actions}`;
        container.appendChild(row);
      } else {
        row.innerHTML=`
          <input type="checkbox" class="fm-check" data-select-path="${escapeHtml(item.path)}" ${checked}>
          <i class="ti ${expanded?'ti-chevron-down':'ti-chevron-right'} text-muted fm-toggle"
             data-folder-path="${escapeHtml(item.path)}"
             style="font-size:1em;cursor:pointer;min-width:22px;flex-shrink:0;padding:4px 3px;margin:-4px -3px"></i>
          <i class="ti ti-folder-filled text-yellow" style="flex-shrink:0"></i>
          <span class="fm-name">${escapeHtml(item.name)}</span>
          ${actions}`;
        container.appendChild(row);
        if (expanded && item.children) renderTreeItems(item.children, container, depth+1);
      }
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
          <button class="btn btn-sm btn-outline-secondary" data-rename-path="${escapeHtml(item.path)}" data-rename-name="${escapeHtml(item.name)}"><i class="ti ti-pencil"></i></button>
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

async function renamePath(path, name) {
  const newName = await scPrompt(`Nuovo nome:`, name);
  if (!newName || newName === name) return;
  try {
    const res = await fetch('/api/files/rename', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ path, new_name: newName }),
    });
    if (res.ok) { showToast(`Rinominato in: ${newName}`, 'success'); loadFiles(); }
    else { const d = await safeJson(res); showToast(d.detail || 'Errore rinomina', 'danger'); }
  } catch(e) { showToast('Errore di rete', 'danger'); }
}

async function deletePath(path, name, isDir) {
  const msg = isDir ? `Eliminare la cartella "${name}" e tutto il suo contenuto?` : `Eliminare il file "${name}"?`;
  if (!await scConfirm(msg)) return;
  try {
    const res = await fetch(`/api/files/delete/${encodeURI(path)}`, {method:'DELETE'});
    if (res.ok||res.status===204) { showToast(`Eliminato: ${name}`,'success'); loadFiles(); }
    else { const d=await safeJson(res); showToast(d.detail||'Errore eliminazione','danger'); }
  } catch(e) { showToast('Errore di rete','danger'); }
}
