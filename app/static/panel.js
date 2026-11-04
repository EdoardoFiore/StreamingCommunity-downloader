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

// ── New version badge ───────────────────────────────────────────────────────────
//
// Beside the version number in the sidebar, hidden while there is nothing newer.

let _updateInfo = null;

function renderUpdateBadge(info) {
  _updateInfo = info;
  const badge = document.getElementById('update-badge');
  if (!badge) return;
  badge.hidden = !info.update_available;
  if (!info.update_available) return;
  document.getElementById('update-badge-version').textContent = `v${info.latest}`;
  badge.title = `Disponibile la v${info.latest}: apri le note di rilascio`;
}

async function refreshUpdateBadge() {
  if (!can('MANAGE_SETTINGS')) return;
  try {
    renderUpdateBadge(await api.get('/api/updates'));
  } catch (e) { /* leave the badge as it was */ }
}

function openUpdateModal() {
  if (!_updateInfo?.update_available) return;
  // notes_html is rendered and escaped by app/updates.py; everything else on
  // the release is escaped here.
  document.getElementById('update-modal-body').innerHTML =
    `<p class="release-current">Versione installata: v${escapeHtml(_updateInfo.current)}</p>` +
    _updateInfo.releases.map(r => `
      <section class="release-entry">
        <h3 class="release-title">
          v${escapeHtml(r.version)}
          ${r.published_at ? `<span class="release-date">${escapeHtml(fmtDate(r.published_at))}</span>` : ''}
        </h3>
        <div class="release-notes">
          ${r.notes_html || '<p class="release-empty">Nessuna nota di rilascio.</p>'}
        </div>
      </section>`).join('');
  document.getElementById('update-modal-link').href = _updateInfo.releases[0].url;
  _renderUpdateAction();
  showModal('update-modal');
}

// The button when the updater service is there, otherwise the commands: the
// panel cannot replace its own container, and most installs run without it.
function _renderUpdateAction() {
  const hint = document.getElementById('update-modal-how');
  const btn = document.getElementById('update-install-btn');
  const updater = _updateInfo.updater;
  const canInstall = !!updater?.ready;
  btn.hidden = !canInstall;
  btn.disabled = _updateWatch !== null;
  document.getElementById('update-modal-progress').textContent = '';
  if (canInstall) { hint.hidden = true; return; }
  const commands = _updateInfo.install === 'docker'
    ? 'docker compose pull\ndocker compose up -d'
    : 'git pull\npip install -r requirements.txt\n# poi riavvia il pannello';
  const problem = updater?.problem
    ? `<p class="settings-warn">L'aggiornamento dal pannello non è disponibile: ${escapeHtml(updater.problem)}</p>`
    : '';
  hint.innerHTML = `${problem}<p>Per aggiornare:</p><pre class="settings-snippet"><code>${escapeHtml(commands)}</code></pre>`;
  hint.hidden = false;
}

const _UPDATE_WATCH_INTERVAL = 3000;
const _UPDATE_WATCH_LIMIT = 10 * 60 * 1000;
let _updateWatch = null;

function _updateProgress(text, kind = 'muted') {
  const el = document.getElementById('update-modal-progress');
  el.textContent = text;
  el.className = `form-text text-${kind}`;
}

function _stopUpdateWatch(text, kind) {
  clearInterval(_updateWatch);
  _updateWatch = null;
  document.getElementById('update-install-btn').disabled = false;
  _updateProgress(text, kind);
}

// The panel goes away halfway through this, so a failed poll is expected and
// says nothing: only the version it comes back with means the update landed.
function _watchUpdate(from) {
  const since = Date.now();
  _updateWatch = setInterval(async () => {
    if (Date.now() - since > _UPDATE_WATCH_LIMIT) {
      _stopUpdateWatch('Il pannello non è tornato con la nuova versione: controlla `docker compose logs web`.', 'danger');
      return;
    }
    let info;
    try { info = await api.get('/api/updates'); } catch (e) { _updateProgress('Riavvio del pannello...'); return; }
    if (info.current !== from) {
      clearInterval(_updateWatch);
      window.location.reload();
      return;
    }
    const state = info.updater?.state;
    if (state === 'failed') _stopUpdateWatch(`Aggiornamento non riuscito: ${info.updater.message}`, 'danger');
    else if (state === 'unchanged') {
      _stopUpdateWatch('L\'immagine è già l\'ultima pubblicata per questo tag. La nuova versione può essere ancora in pubblicazione, oppure il compose fissa una versione precisa.', 'warning');
    }
    else if (state === 'pulling') _updateProgress('Scaricamento della nuova immagine...');
    else _updateProgress('Riavvio del pannello...');
  }, _UPDATE_WATCH_INTERVAL);
}

async function installUpdate() {
  const target = _updateInfo.latest;
  if (!await scConfirm(`Aggiornare il pannello alla v${target}? Verrà riavviato: qualche istante senza interfaccia.`)) return;
  const btn = document.getElementById('update-install-btn');
  btn.disabled = true;
  _updateProgress('Avvio dell\'aggiornamento...');
  try {
    await api.post('/api/updates/install');
    _watchUpdate(_updateInfo.current);
  } catch (e) {
    btn.disabled = false;
    _updateProgress(errText(e, 'Aggiornamento non riuscito.'), 'danger');
  }
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
  'updates:open':   () => openUpdateModal(),
  'updates:install': () => installUpdate(),
  'notif:toggle':   () => toggleNotifications(),
  'notif:readAll':  () => markAllNotificationsRead(),
  'notif:clearAll': () => clearAllNotifications(),
  'notif:delete':   d => deleteNotification(Number(d.id)),
});
