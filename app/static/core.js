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
  // The inverse: hidden *because* the user holds a permission. Someone who
  // downloads directly never files a request, so the queue of their own
  // requests is an empty page with a name.
  //
  // A followed series does file requests in their name, but that is
  // machinery, not something they asked for: the request exists so dedup, the
  // library check and the notifications keep working, it is auto-approved
  // against their own DOWNLOAD permission, and what they actually want to see
  // - the episode arriving - shows up under Download like any other job. One
  // that parks on a missing track still reaches them through the bell, which
  // carries no permission gate.
  //
  // Applied second so it can override a data-perm that just showed the
  // element.
  document.querySelectorAll('[data-hide-perm]').forEach(el => {
    if (el.dataset.hidePerm.split('|').some(can)) el.style.display = 'none';
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

// ── Artwork ───────────────────────────────────────────────────────────────────
//
// Stills and posters travel through /api/image/, which reaches the source's
// CDN on the server's behalf. A single failure there is usually transient, so
// a missing image should not collapse the row it belongs to: the slot stays,
// a placeholder holds it, and the fetch is retried a couple of times before
// giving up quietly.

const _THUMB_RETRIES = 2;

function thumbHtml(src, cls, alt = '') {
  if (!src) return `<span class="${cls} thumb is-failed" aria-hidden="true"></span>`;
  // No onload: the image is visible from the start and the placeholder simply
  // sits behind it. Gating visibility on a handler meant a missed load event
  // hid the picture permanently.
  return `<span class="${cls} thumb">
    <img src="${escapeHtml(src)}" alt="${escapeHtml(alt)}" loading="lazy"
         data-src="${escapeHtml(src)}" onerror="thumbError(this)">
  </span>`;
}

function thumbError(img) {
  const tries = Number(img.dataset.tries || 0);
  if (tries >= _THUMB_RETRIES) { img.parentElement?.classList.add('is-failed'); return; }
  img.dataset.tries = String(tries + 1);
  // A cache-busting query, not a fragment: a fragment change does not make
  // the browser ask again.
  const base = img.dataset.src;
  const sep = base.includes('?') ? '&' : '?';
  img.parentElement?.classList.remove('is-failed');
  setTimeout(() => { img.src = `${base}${sep}_retry=${tries + 1}`; }, 500 * (tries + 1));
}

// A title's poster. AnimeUnity sends an absolute URL; StreamingCommunity sends
// a bare filename that /api/image/ resolves against the current source host -
// which is why it must not be hardcoded anywhere.
function posterUrl(item) {
  const p = item && item.poster;
  if (!p) return '';
  return p.startsWith('http') ? p : `/api/image/${p}`;
}


// ── Delegated actions ────────────────────────────────────────────────────────
//
// One listener on the document, instead of an onclick= per button. Two reasons
// beyond tidiness: an inline handler interpolates its arguments into an HTML
// attribute with no escaping, so a title containing an apostrophe used to break
// the button it was rendered into; and a row that is re-rendered mid-interaction
// keeps working, because nothing is bound to the node that went away.
//
// Additive on purpose: it coexists with the inline handlers that have not been
// converted yet. A page converts its own markup when it is reworked, never in a
// sweep across all of them at once.
//
// Arguments travel as data-* attributes and arrive as the element's dataset, so
// they are strings: a handler that wants a number converts it.
const _ACTIONS = Object.create(null);

function registerActions(map) { Object.assign(_ACTIONS, map); }

// A checkbox's click event carries its own activation: cancelling it un-checks
// the box the user just checked. So the default is only suppressed for things
// that have no useful default of their own.
const _KEEPS_ITS_DEFAULT = new Set(['INPUT', 'SELECT', 'TEXTAREA', 'OPTION', 'LABEL']);

function _dispatchAction(event, attribute) {
  const el = event.target.closest(`[${attribute}]`);
  if (!el || el.disabled) return;
  const handler = _ACTIONS[el.getAttribute(attribute)];
  if (!handler) return;
  // Only swallow the event once something is actually going to handle it: an
  // unregistered name must look broken, not silently eat the click.
  if (event.type === 'click' && !_KEEPS_ITS_DEFAULT.has(el.tagName)) event.preventDefault();
  handler(el.dataset, el, event);
}

document.addEventListener('click', e => _dispatchAction(e, 'data-action'));
document.addEventListener('change', e => _dispatchAction(e, 'data-change'));


// ── Language names ───────────────────────────────────────────────────────────
//
// Shared, not owned by a page: the title page names the tracks you are picking
// and the request pages name the tracks a request was made with. This lived in
// the detail view until splitting the scripts showed the request rows reaching
// across for it.
//
// Anything not listed is shown as the source spelled it, rather than dropped.

const LANG_NAMES = {
  ita:'Italiano', eng:'English', fra:'Français', spa:'Español',
  deu:'Deutsch', por:'Português', jpn:'日本語', zho:'中文',
  ara:'العربية', rus:'Русский', kor:'한국어',
};
const langName = c => LANG_NAMES[c] || c;
