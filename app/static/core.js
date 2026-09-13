// Shared primitives: session, the fetch wrapper, the small formatters, the
// modal and toast plumbing. Everything here is used by more than one page.
//
// Loaded before app.js and panel.js, and a classic script on purpose, not a
// module: the markup still carries inline onclick handlers, which resolve
// against global scope. See the rework plan.


// ── Session ────────────────────────────────────────────────────────────────────

let _me = null;           // { user, csrf_token, auth_enabled }
let _csrf = '';
let _authEnabled = true;  // false when the panel runs without Jellyfin (AUTH_ENABLED=0)

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
