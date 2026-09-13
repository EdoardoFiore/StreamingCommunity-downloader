'use strict';

// Login / first-run setup page. Both forms post to /api/auth/*, which are the
// only endpoints reachable without a session.
//
// Runs on base_bare.html: tokens, the shared components and core.js, but no
// shell. It takes safeJson and escapeHtml from core.js rather than keeping its
// own copies — this page had redeclared safeJson, which simply shadowed the
// shared one and would have drifted from it.

const $ = id => document.getElementById(id);

function showAlert(message, kind = 'error') {
  const el = $('auth-alert');
  el.textContent = message;
  el.className = 'auth-alert' + (kind === 'info' ? ' info' : '');
  el.style.display = 'block';
}

function clearAlert() {
  $('auth-alert').style.display = 'none';
}

// The button remembers its own label, so a caller cannot restore the wrong
// one. Each of these was written out three times per button — once to go
// busy, once for each way back — which is three chances to let them drift.
function busy(btn, on, busyLabel) {
  if (btn.dataset.label === undefined) btn.dataset.label = btn.innerHTML;
  btn.disabled = on;
  btn.innerHTML = on
    ? '<span class="spinner-border spinner-border-sm me-1"></span>' + busyLabel
    : btn.dataset.label;
}

// Embedded as a Jellyfin custom tab, the parent frame already holds a valid
// Jellyfin access token (window.ApiClient). Announce readiness — no secret in
// this message, so targetOrigin '*' is fine — and if the parent replies with
// a token, trade it for a panel session instead of showing the login form.
// Standalone visits never get a reply, so the form loaded by init() below is
// the fallback in every case: misconfigured embed, expired token, or no
// embed at all.
function listenForJellyfinToken() {
  if (window.top === window.self) return;
  window.addEventListener('message', async (event) => {
    if (!event.data || event.data.type !== 'sc-panel-jellyfin-token' || !event.data.token) return;
    try {
      const res = await fetch('/api/auth/jellyfin-token', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: event.data.token }),
      });
      if (res.ok) window.location.href = '/';
    } catch { /* fall back to the visible login form */ }
  });
  window.parent.postMessage({ type: 'sc-panel-ready' }, '*');
}

async function init() {
  try {
    const res = await fetch('/api/auth/status');
    const status = await safeJson(res);
    $('auth-loading').style.display = 'none';
    if (status.setup_done) {
      $('login-form').style.display = '';
      if (status.jellyfin_url) $('login-server').textContent = status.jellyfin_url;
      $('login-username').focus();
    } else {
      $('setup-form').style.display = '';
      $('setup-url').focus();
    }
  } catch {
    $('auth-loading').style.display = 'none';
    showAlert('Impossibile contattare il pannello.');
  }
}

$('setup-form').addEventListener('submit', async e => {
  e.preventDefault();
  clearAlert();
  const btn = $('setup-btn');
  busy(btn, true, 'Connessione...');
  try {
    const res = await fetch('/api/auth/setup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url: $('setup-url').value.trim(),
        username: $('setup-username').value.trim(),
        password: $('setup-password').value,
      }),
    });
    const data = await safeJson(res);
    if (!res.ok) {
      showAlert(data.detail || 'Configurazione fallita.');
      busy(btn, false);
      return;
    }
    window.location.href = '/';
  } catch {
    showAlert('Errore di rete.');
    busy(btn, false);
  }
});

$('skip-setup-btn').addEventListener('click', async () => {
  clearAlert();
  const btn = $('skip-setup-btn');
  busy(btn, true, 'Attendere...');
  try {
    const res = await fetch('/api/auth/skip', { method: 'POST' });
    const data = await safeJson(res);
    if (!res.ok) {
      showAlert(data.detail || 'Operazione fallita.');
      busy(btn, false);
      return;
    }
    window.location.href = '/';
  } catch {
    showAlert('Errore di rete.');
    busy(btn, false);
  }
});

$('login-form').addEventListener('submit', async e => {
  e.preventDefault();
  clearAlert();
  const btn = $('login-btn');
  busy(btn, true, 'Accesso...');
  try {
    const res = await fetch('/api/auth/jellyfin', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: $('login-username').value.trim(),
        password: $('login-password').value,
      }),
    });
    const data = await safeJson(res);
    if (!res.ok) {
      showAlert(data.detail || 'Accesso fallito.');
      busy(btn, false);
      return;
    }
    window.location.href = '/';
  } catch {
    showAlert('Errore di rete.');
    busy(btn, false);
  }
});

listenForJellyfinToken();
init();
