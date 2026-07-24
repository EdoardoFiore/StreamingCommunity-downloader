'use strict';

// Request queue, my requests, user management and in-app notifications.
// Loaded after app.js and relies on its helpers (escapeHtml, showToast, can,
// showModal/hideModal, safeJson) and on its fetch wrapper for the CSRF token.

const REQUEST_STATUS_LABELS = {
  pending:         { label: 'In attesa',   cls: 'bg-yellow-lt' },
  approved:        { label: 'Approvata',   cls: 'bg-blue-lt' },
  downloading:     { label: 'In download', cls: 'bg-azure-lt' },
  completed:       { label: 'Completata',  cls: 'bg-green-lt' },
  available:       { label: 'Già in libreria', cls: 'bg-green-lt' },
  denied:          { label: 'Rifiutata',   cls: 'bg-red-lt' },
  failed:          { label: 'Fallita',     cls: 'bg-red-lt' },
  cancelled:       { label: 'Annullata',   cls: 'bg-secondary' },
  needs_attention: { label: 'Da verificare', cls: 'bg-orange-lt' },
};

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
};

let _queue = [];
let _myRequests = [];

function statusBadge(status) {
  const meta = REQUEST_STATUS_LABELS[status] || { label: status, cls: 'bg-secondary' };
  return `<span class="badge ${meta.cls}">${meta.label}</span>`;
}

function requestTitle(request) {
  if (request.media_type === 'episode') {
    const season = String(request.season).padStart(2, '0');
    return `${request.title} S${season}E${request.episode_number}`;
  }
  if (request.media_type === 'anime') return `${request.title} E${request.episode_number}`;
  return request.title;
}

function trackBadges(request) {
  const audio = (request.audio_languages || [])
    .map(c => `<span class="badge bg-blue-lt">${escapeHtml(langName(c))}</span>`).join(' ');
  const subs = (request.subtitle_languages || [])
    .map(c => `<span class="badge bg-teal-lt">${escapeHtml(langName(c))}</span>`).join(' ');
  return `<div class="track-badges">
    <span class="track-label"><i class="ti ti-volume"></i></span>${audio || '<span class="text-muted">—</span>'}
    ${subs ? `<span class="track-label ms-2"><i class="ti ti-subtitles"></i></span>${subs}` : ''}
  </div>`;
}

function requestPoster(request) {
  if (!request.poster) return '<div class="req-poster req-poster-empty">🎬</div>';
  const url = request.poster.startsWith('http')
    ? request.poster
    : `/api/image/${request.poster}`;
  return `<img class="req-poster" src="${escapeHtml(url)}" alt=""
            onerror="this.outerHTML='<div class=&quot;req-poster req-poster-empty&quot;>🎬</div>'">`;
}

function fmtDate(iso) {
  if (!iso) return '';
  try { return new Date(iso).toLocaleString('it-IT', { dateStyle: 'short', timeStyle: 'short' }); }
  catch { return iso; }
}

// ── Request queue (approvers) ──────────────────────────────────────────────────

async function loadRequestQueue() {
  const container = document.getElementById('requests-list');
  container.innerHTML = '<div class="text-center py-4"><span class="spinner-border"></span></div>';
  try {
    const res = await fetch('/api/requests');
    if (!res.ok) throw new Error((await safeJson(res)).detail || 'Errore');
    _queue = await res.json();
    renderRequestQueue();
  } catch (e) {
    container.innerHTML = `<div class="alert alert-danger">${escapeHtml(e.message)}</div>`;
  }
}

function queueFilter() {
  const active = document.querySelector('#queue-filters .queue-filter.active');
  return active ? active.dataset.filter : 'open';
}

function setQueueFilter(filter) {
  document.querySelectorAll('#queue-filters .queue-filter').forEach(el =>
    el.classList.toggle('active', el.dataset.filter === filter));
  renderRequestQueue();
}

function renderRequestQueue() {
  const container = document.getElementById('requests-list');
  const filter = queueFilter();
  const open = ['pending', 'approved', 'downloading', 'needs_attention'];
  const rows = _queue.filter(r =>
    filter === 'all' ? true :
    filter === 'open' ? open.includes(r.status) :
    r.status === filter);

  document.getElementById('queue-pending-count').textContent =
    _queue.filter(r => r.status === 'pending').length || '';

  if (!rows.length) {
    container.innerHTML = `<div class="empty-panel">
      <i class="ti ti-inbox"></i><p>Nessuna richiesta da mostrare.</p></div>`;
    return;
  }

  container.innerHTML = rows.map(r => `
    <div class="req-row ${r.status === 'needs_attention' ? 'req-row-attention' : ''}">
      ${requestPoster(r)}
      <div class="req-main">
        <div class="req-title">${escapeHtml(requestTitle(r))}${r.year ? ` <span class="text-muted">(${escapeHtml(r.year)})</span>` : ''}</div>
        <div class="req-meta">
          <i class="ti ti-user"></i> ${escapeHtml(r.requested_by_username || '?')}
          ${r.subscribers.length > 1 ? `<span class="badge bg-purple-lt ms-1">+${r.subscribers.length - 1} altri</span>` : ''}
          <span class="req-dot">·</span>
          <i class="ti ti-calendar"></i> ${fmtDate(r.created_at)}
          <span class="req-dot">·</span>
          <span class="text-muted">${escapeHtml(r.source === 'animeunity' ? 'AnimeUnity' : 'StreamingCommunity')}</span>
        </div>
        ${trackBadges(r)}
        ${r.problem ? `<div class="req-problem"><i class="ti ti-alert-circle me-1"></i>${escapeHtml(r.problem)}</div>` : ''}
        ${r.denial_reason ? `<div class="req-denied"><i class="ti ti-x me-1"></i>${escapeHtml(r.denial_reason)}</div>` : ''}
      </div>
      <div class="req-side">
        ${statusBadge(r.status)}
        <div class="req-actions">
          ${['pending', 'needs_attention'].includes(r.status) ? `
            <button class="btn btn-sm btn-success" onclick="approveRequest(${r.id})">
              <i class="ti ti-check me-1"></i>${r.status === 'needs_attention' ? 'Riprova' : 'Approva'}</button>
            <button class="btn btn-sm btn-outline-danger" onclick="openDenyModal(${r.id})">
              <i class="ti ti-x"></i></button>` : ''}
          ${r.status === 'needs_attention' ? `
            <button class="btn btn-sm btn-outline-warning" onclick="openFixModal(${r.id})">
              <i class="ti ti-tool me-1"></i>Correggi</button>` : ''}
        </div>
      </div>
    </div>`).join('');
}

async function approveRequest(id) {
  const res = await fetch(`/api/requests/${id}/approve`, { method: 'POST' });
  const data = await safeJson(res);
  if (!res.ok) { showToast(data.detail || 'Errore', 'danger'); return; }
  showToast('Richiesta approvata', 'success');
  await loadRequestQueue();
  refreshNotifications();
}

let _denyId = null;

function openDenyModal(id) {
  _denyId = id;
  const request = _queue.find(r => r.id === id);
  document.getElementById('deny-title').textContent = request ? requestTitle(request) : '';
  document.getElementById('deny-reason').value = '';
  showModal('deny-modal');
  setTimeout(() => document.getElementById('deny-reason').focus(), 150);
}

async function confirmDeny() {
  const reason = document.getElementById('deny-reason').value.trim();
  if (!reason) { showToast('Indica un motivo', 'warning'); return; }
  const res = await fetch(`/api/requests/${_denyId}/deny`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  });
  const data = await safeJson(res);
  if (!res.ok) { showToast(data.detail || 'Errore', 'danger'); return; }
  hideModal('deny-modal');
  showToast('Richiesta rifiutata', 'info');
  loadRequestQueue();
}

// ── Fixing a parked request ────────────────────────────────────────────────────

let _fixId = null;

function openFixModal(id) {
  const request = _queue.find(r => r.id === id);
  if (!request) return;
  _fixId = id;
  document.getElementById('fix-title').textContent = requestTitle(request);
  document.getElementById('fix-problem').textContent = request.problem || '';
  document.getElementById('fix-external-id').value = request.external_id;
  document.getElementById('fix-slug').value = request.slug || '';
  document.getElementById('fix-season').value = request.season ?? '';
  document.getElementById('fix-episode').value = request.episode_number ?? '';

  const offered = (request.available_snapshot && request.available_snapshot.audio) || [];
  const audioBox = document.getElementById('fix-audio');
  audioBox.innerHTML = offered.length
    ? offered.map(c => `<label class="me-2 mb-1" style="cursor:pointer">
        <input type="checkbox" class="fix-audio-check me-1" value="${escapeHtml(c)}"
          ${request.audio_languages.includes(c) ? 'checked' : ''}>
        <span class="badge bg-blue-lt">${escapeHtml(langName(c))}</span></label>`).join('')
    : '<span class="text-muted">Nessuna traccia rilevata all\'ultimo tentativo.</span>';

  const seasonRow = document.getElementById('fix-season-row');
  seasonRow.style.display = request.media_type === 'film' ? 'none' : '';
  showModal('fix-modal');
}

async function confirmFix() {
  const payload = {};
  const externalId = document.getElementById('fix-external-id').value.trim();
  const slug = document.getElementById('fix-slug').value.trim();
  const season = document.getElementById('fix-season').value.trim();
  const episode = document.getElementById('fix-episode').value.trim();
  const audio = [...document.querySelectorAll('.fix-audio-check:checked')].map(c => c.value);

  if (externalId) payload.external_id = externalId;
  if (slug) payload.slug = slug;
  if (season) payload.season = parseInt(season, 10);
  if (episode) payload.episode_number = episode;
  if (audio.length) payload.audio_languages = audio;

  const res = await fetch(`/api/requests/${_fixId}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await safeJson(res);
  if (!res.ok) { showToast(data.detail || 'Errore', 'danger'); return; }
  hideModal('fix-modal');
  showToast('Richiesta corretta — approvala per riprovare', 'success');
  loadRequestQueue();
}

// ── My requests ────────────────────────────────────────────────────────────────

async function loadMyRequests() {
  const container = document.getElementById('my-requests-list');
  container.innerHTML = '<div class="text-center py-4"><span class="spinner-border"></span></div>';
  try {
    const res = await fetch('/api/requests/mine');
    if (!res.ok) throw new Error((await safeJson(res)).detail || 'Errore');
    _myRequests = await res.json();
  } catch (e) {
    container.innerHTML = `<div class="alert alert-danger">${escapeHtml(e.message)}</div>`;
    return;
  }

  if (!_myRequests.length) {
    container.innerHTML = `<div class="empty-panel">
      <i class="ti ti-send"></i><p>Non hai ancora richiesto niente.</p></div>`;
    return;
  }

  container.innerHTML = _myRequests.map(r => `
    <div class="req-row">
      ${requestPoster(r)}
      <div class="req-main">
        <div class="req-title">${escapeHtml(requestTitle(r))}${r.year ? ` <span class="text-muted">(${escapeHtml(r.year)})</span>` : ''}</div>
        <div class="req-meta"><i class="ti ti-calendar"></i> ${fmtDate(r.created_at)}</div>
        ${trackBadges(r)}
        ${r.denial_reason ? `<div class="req-denied"><i class="ti ti-x me-1"></i>Motivo: ${escapeHtml(r.denial_reason)}</div>` : ''}
        ${r.status === 'needs_attention' ? '<div class="req-problem"><i class="ti ti-clock-pause me-1"></i>In attesa di una verifica da parte di un amministratore.</div>' : ''}
      </div>
      <div class="req-side">
        ${statusBadge(r.status)}
        <div class="req-actions">
          ${r.status === 'pending' ? `<button class="btn btn-sm btn-outline-secondary" onclick="withdrawRequest(${r.id})">
            <i class="ti ti-trash me-1"></i>Annulla</button>` : ''}
        </div>
      </div>
    </div>`).join('');
}

async function withdrawRequest(id) {
  if (!await scConfirm('Annullare questa richiesta?')) return;
  const res = await fetch(`/api/requests/${id}`, { method: 'DELETE' });
  const data = await safeJson(res);
  if (!res.ok) { showToast(data.detail || 'Errore', 'danger'); return; }
  showToast('Richiesta annullata', 'info');
  loadMyRequests();
}

// ── Users ──────────────────────────────────────────────────────────────────────

let _permissionCatalogue = [];
let _jellyfinUsers = [];

const PERMISSION_LABELS = {
  DOWNLOAD: 'Scarica direttamente',
  REQUEST: 'Può richiedere',
  MANAGE_REQUESTS: 'Approva richieste',
  MANAGE_USERS: 'Gestisce utenti',
  MANAGE_SETTINGS: 'Gestisce impostazioni',
  MANAGE_FILES: 'Gestisce file',
  VIEW_LIBRARY: 'Sfoglia libreria',
};

async function loadUsersPage() {
  const container = document.getElementById('users-list');
  container.innerHTML = '<div class="text-center py-4"><span class="spinner-border"></span></div>';
  try {
    const [permissions, jellyfinUsers, settings] = await Promise.all([
      fetch('/api/users/permissions').then(r => r.json()),
      fetch('/api/users/jellyfin').then(async r => {
        if (!r.ok) throw new Error((await safeJson(r)).detail || 'Errore Jellyfin');
        return r.json();
      }),
      fetch('/api/users/settings').then(r => r.json()),
    ]);
    _permissionCatalogue = permissions.permissions;
    _jellyfinUsers = jellyfinUsers;
    document.getElementById('open-signin').checked = settings.allow_new_jellyfin_login;
    renderUsers();
  } catch (e) {
    container.innerHTML = `<div class="alert alert-danger">${escapeHtml(e.message)}</div>`;
  }
}

function _permissionCheckboxes(rowId, permissions) {
  return _permissionCatalogue.map(p => `
    <label class="perm-chip">
      <input type="checkbox" data-user-row="${rowId}" value="${p.value}"
        ${(permissions & p.value) ? 'checked' : ''}>
      <span>${escapeHtml(PERMISSION_LABELS[p.name] || p.name)}</span>
    </label>`).join('');
}

function renderUsers() {
  const container = document.getElementById('users-list');
  container.innerHTML = _jellyfinUsers.map(u => {
    const panel = u.panel_user;
    const rowId = panel ? `u${panel.id}` : `j${u.jellyfin_user_id}`;
    return `
    <div class="user-row ${panel && !panel.enabled ? 'user-row-disabled' : ''}">
      <div class="user-avatar">${escapeHtml((u.username || '?').slice(0, 2).toUpperCase())}</div>
      <div class="user-main">
        <div class="user-name">
          ${escapeHtml(u.username)}
          ${u.is_jellyfin_admin ? '<span class="badge bg-purple-lt ms-1">Admin Jellyfin</span>' : ''}
          ${panel ? (panel.enabled
              ? '<span class="badge bg-green-lt ms-1">Attivo</span>'
              : '<span class="badge bg-red-lt ms-1">Disabilitato</span>')
            : '<span class="badge bg-secondary ms-1">Non importato</span>'}
        </div>
        <div class="perm-chips">${_permissionCheckboxes(rowId, panel ? panel.permissions : 0)}</div>
      </div>
      <div class="user-actions">
        ${panel ? `
          <button class="btn btn-sm btn-primary" onclick="saveUserPermissions(${panel.id}, '${rowId}')">
            <i class="ti ti-device-floppy me-1"></i>Salva</button>
          <button class="btn btn-sm ${panel.enabled ? 'btn-outline-danger' : 'btn-outline-success'}"
            onclick="toggleUserEnabled(${panel.id}, ${!panel.enabled})">
            ${panel.enabled ? '<i class="ti ti-user-off"></i>' : '<i class="ti ti-user-check"></i>'}</button>`
        : `<button class="btn btn-sm btn-success"
             onclick="importUser('${escapeHtml(u.jellyfin_user_id)}', '${rowId}')">
             <i class="ti ti-download me-1"></i>Importa</button>`}
      </div>
    </div>`;
  }).join('');
}

function _selectedPermissions(rowId) {
  return [...document.querySelectorAll(`input[data-user-row="${rowId}"]:checked`)]
    .reduce((total, cb) => total | parseInt(cb.value, 10), 0);
}

async function importUser(jellyfinUserId, rowId) {
  const res = await fetch('/api/users/import', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      jellyfin_user_ids: [jellyfinUserId],
      permissions: _selectedPermissions(rowId),
    }),
  });
  const data = await safeJson(res);
  if (!res.ok) { showToast(data.detail || 'Errore', 'danger'); return; }
  showToast('Utente importato', 'success');
  loadUsersPage();
}

async function saveUserPermissions(userId, rowId) {
  const res = await fetch(`/api/users/${userId}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ permissions: _selectedPermissions(rowId) }),
  });
  const data = await safeJson(res);
  if (!res.ok) { showToast(data.detail || 'Errore', 'danger'); return; }
  showToast('Permessi aggiornati', 'success');
  loadUsersPage();
}

async function toggleUserEnabled(userId, enabled) {
  if (!enabled && !await scConfirm('Disabilitare questo utente? Le sue sessioni verranno chiuse subito.')) return;
  const res = await fetch(`/api/users/${userId}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  });
  const data = await safeJson(res);
  if (!res.ok) { showToast(data.detail || 'Errore', 'danger'); return; }
  showToast(enabled ? 'Utente abilitato' : 'Utente disabilitato', 'info');
  loadUsersPage();
}

async function saveOpenSignin() {
  const allow = document.getElementById('open-signin').checked;
  const settings = await fetch('/api/users/settings').then(r => r.json());
  const res = await fetch('/api/users/settings', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      allow_new_jellyfin_login: allow,
      default_permissions: settings.default_permissions,
    }),
  });
  if (!res.ok) { showToast('Errore', 'danger'); return; }
  showToast(allow
    ? 'Chiunque abbia un account Jellyfin può ora accedere'
    : 'Accesso limitato agli utenti importati', 'info');
}

// ── Notifications ──────────────────────────────────────────────────────────────

async function refreshNotifications() {
  try {
    const res = await fetch('/api/notifications');
    if (!res.ok) return;
    const payload = await res.json();
    const badge = document.getElementById('notif-badge');
    badge.textContent = payload.unread || '';
    badge.style.display = payload.unread ? '' : 'none';

    const list = document.getElementById('notif-list');
    list.innerHTML = payload.items.length
      ? payload.items.map(n => `
          <div class="notif-item ${n.read_at ? '' : 'notif-unread'}">
            <i class="ti ${NOTIFICATION_ICONS[n.event] || 'ti-bell'}"></i>
            <div>
              <div class="notif-text">${escapeHtml(n.message)}</div>
              <div class="notif-time">${fmtDate(n.created_at)}</div>
            </div>
          </div>`).join('')
      : '<div class="notif-empty">Nessuna notifica.</div>';
  } catch (e) { /* the bell just stays as it was */ }
}

function toggleNotifications() {
  const panel = document.getElementById('notif-panel');
  const opening = panel.style.display === 'none' || !panel.style.display;
  panel.style.display = opening ? 'block' : 'none';
  if (opening) refreshNotifications();
}

async function markAllNotificationsRead() {
  await fetch('/api/notifications/read', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  refreshNotifications();
}

document.addEventListener('click', event => {
  const panel = document.getElementById('notif-panel');
  if (!panel || panel.style.display === 'none') return;
  if (!event.target.closest('#notif-panel') && !event.target.closest('#notif-button')) {
    panel.style.display = 'none';
  }
});
