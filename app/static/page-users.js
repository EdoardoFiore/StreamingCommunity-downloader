'use strict';

/* StreamingCommunity Web Panel — page-users.js */
//
// User management: importing accounts from Jellyfin, and the permission grid.
// Permissions are independent flags — there is no administrator bit that
// implies the others — so every one of them is its own control.

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
