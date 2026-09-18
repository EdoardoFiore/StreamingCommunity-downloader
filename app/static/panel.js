'use strict';

// The shell's live parts: the in-app notification bell and the sidebar's
// pending-requests badge. Both run on every page, which is why they are not
// in any page-*.js. Relies on core.js for escapeHtml/showToast/can and on
// api.js for the calls themselves.


// ── Sidebar "Richieste" badge ───────────────────────────────────────────────────
//
// Hidden entirely at zero, shown with the real count from the moment the app
// loads — not just after the user happens to open the queue page.

async function refreshQueueBadge() {
  if (!can('MANAGE_REQUESTS')) return;
  const badge = document.getElementById('queue-pending-count');
  if (!badge) return;
  try {
    const counts = await api.get('/api/requests/counts');
    const n = counts.action_required || 0;
    badge.textContent = n || '';
    badge.style.display = n ? '' : 'none';
  } catch (e) { /* leave the badge as it was */ }
}

// ── Notifications ──────────────────────────────────────────────────────────────

async function refreshNotifications() {
  try {
    const payload = await api.get('/api/notifications');
    const badge = document.getElementById('notif-badge');
    badge.textContent = payload.unread || '';
    badge.style.display = payload.unread ? '' : 'none';

    const list = document.getElementById('notif-list');
    list.innerHTML = payload.items.length
      ? payload.items.map(n => `
          <div class="notif-item ${n.read_at ? '' : 'notif-unread'}">
            <i class="ti ${NOTIFICATION_ICONS[n.event] || 'ti-bell'}"></i>
            <div class="notif-body">
              <div class="notif-text">${escapeHtml(n.message)}</div>
              <div class="notif-time">${fmtDate(n.created_at)}</div>
            </div>
            <button class="notif-del" data-action="notif:delete" data-id="${n.id}"
                    title="Elimina questa notifica" aria-label="Elimina">
              <i class="ti ti-x"></i>
            </button>
          </div>`).join('')
      : '<div class="notif-empty">Nessuna notifica.</div>';

    // Nothing to act on means nothing to offer.
    const actions = document.getElementById('notif-actions');
    if (actions) actions.style.display = payload.items.length ? '' : 'none';
  } catch (e) { /* the bell just stays as it was */ }
}

function toggleNotifications() {
  const panel = document.getElementById('notif-panel');
  const opening = panel.style.display === 'none' || !panel.style.display;
  panel.style.display = opening ? 'block' : 'none';
  if (opening) refreshNotifications();
}

async function markAllNotificationsRead() {
  try { await api.post('/api/notifications/read', {}); }
  catch (e) { /* the badge will correct itself on the next poll */ }
  refreshNotifications();
}

async function _deleteNotifications(body) {
  let result;
  try {
    // Always {deleted, unread}, so the object itself is the success signal
    // the caller checks - as res.json() was before.
    result = await api.post('/api/notifications/delete', body);
  } catch (e) {
    showToast(errText(e, 'Eliminazione fallita'), 'danger');
    return null;
  }
  await refreshNotifications();
  return result;
}

// One row: no confirmation. It is a single line of history, and asking every
// time would cost more than the mistake.
async function deleteNotification(id) {
  await _deleteNotifications({ ids: [id] });
}

async function clearAllNotifications() {
  if (!await scConfirm('Eliminare tutte le notifiche?')) return;
  const result = await _deleteNotifications({});
  if (result) showToast('Notifiche eliminate', 'info');
}

document.addEventListener('click', event => {
  const panel = document.getElementById('notif-panel');
  if (!panel || panel.style.display === 'none') return;
  if (!event.target.closest('#notif-panel') && !event.target.closest('#notif-button')) {
    panel.style.display = 'none';
  }
});


// ── Delegated handlers ───────────────────────────────────────────────────────

registerActions({
  'notif:toggle':   () => toggleNotifications(),
  'notif:readAll':  () => markAllNotificationsRead(),
  'notif:clearAll': () => clearAllNotifications(),
  'notif:delete':   d => deleteNotification(Number(d.id)),
});
