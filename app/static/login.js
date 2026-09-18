'use strict';

// Login / first-run setup page. Both forms post to /api/auth/*, which are the
// only endpoints reachable without a session.
//
// Runs on base_bare.html: tokens, the shared components and core.js, but no
// shell. It takes its helpers from core.js and its calls from api.js rather
// than keeping its own copies — this page had redeclared safeJson, which
// simply shadowed the shared one and would have drifted from it, and
// hand-rolled the same ok/detail dance at all five call sites.

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
      await api.post('/api/auth/jellyfin-token', { token: event.data.token });
      window.location.href = '/';
    } catch { /* fall back to the visible login form */ }
  });
  window.parent.postMessage({ type: 'sc-panel-ready' }, '*');
}

async function init() {
  try {
    const status = await api.get('/api/auth/status');
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
    await api.post('/api/auth/setup', {
      url: $('setup-url').value.trim(),
      username: $('setup-username').value.trim(),
      password: $('setup-password').value,
    });
    window.location.href = '/';
  } catch (e) {
    showAlert(errText(e, 'Configurazione fallita.'));
    busy(btn, false);
  }
});

$('skip-setup-btn').addEventListener('click', async () => {
  clearAlert();
  const btn = $('skip-setup-btn');
  busy(btn, true, 'Attendere...');
  try {
    await api.post('/api/auth/skip');
    window.location.href = '/';
  } catch (e) {
    showAlert(errText(e, 'Operazione fallita.'));
    busy(btn, false);
  }
});

$('login-form').addEventListener('submit', async e => {
  e.preventDefault();
  clearAlert();
  const btn = $('login-btn');
  busy(btn, true, 'Accesso...');
  try {
    await api.post('/api/auth/jellyfin', {
      username: $('login-username').value.trim(),
      password: $('login-password').value,
    });
    window.location.href = '/';
  } catch (e) {
    showAlert(errText(e, 'Accesso fallito.'));
    busy(btn, false);
  }
});

listenForJellyfinToken();
init();
