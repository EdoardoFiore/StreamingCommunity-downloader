/* StreamingCommunity Web Panel — page-settings.js */
//
// The settings page: source, libraries, naming, performance, access,
// notification channels and post-download hooks. Each section saves itself
// and reports into its own feedback line.

// ── Settings ───────────────────────────────────────────────────────────────────

// Every section in the settings modal saves itself, so each one reports into its
// own feedback line rather than sharing one status area.
const _SETTINGS_FEEDBACK_IDS = [
  'domain-feedback', 'libraries-feedback', 'perf-settings-feedback',
  'jf-connect-feedback', 'jf-reconnect-feedback', 'notif-channels-feedback',
  'domain-recovery-feedback',
  'jf-refresh-feedback', 'hooks-feedback', 'naming-feedback',
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
  sorgente: () => loadDomainRecoverySettings(),
  nomi: () => loadNamingTemplates(),
  download: () => loadPerfSettings(),
  accesso: () => loadJellyfinSettings(),
  notifiche: () => loadNotificationChannels(),
  hook: () => Promise.all([loadJellyfinRefresh(), loadHooks()]),
};

// Two panes read the same endpoint. Shared per modal-open so switching between
// them does not fetch it twice; cleared alongside _settingsLoaded.
let _appSettingsPromise = null;

function _loadAppSettings() {
  if (!_appSettingsPromise) {
    _appSettingsPromise = fetch('/api/domain/settings')
      .then(res => (res.ok ? safeJson(res) : null))
      .catch(() => null);
  }
  return _appSettingsPromise;
}

// Tabs whose panes only talk to MANAGE_SETTINGS endpoints: without it they would
// render as empty panes fed by 403s.
const _SETTINGS_TABS_NEED_MANAGE = ['sorgente', 'librerie', 'nomi', 'download', 'notifiche', 'hook'];

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
  // The tab belongs in the address, but replaceState rather than assigning to
  // location.hash: that would fire hashchange, and the router would route
  // back into here.
  if (document.getElementById('page-settings')?.style.display !== 'none') {
    history.replaceState(null, '', `#/settings/${name}`);
  }
  window.scrollTo({ top: 0 });

  // Marked before awaiting, so a double click cannot fire two fetches.
  if (!_settingsLoaded.has(name)) {
    _settingsLoaded.add(name);
    await _SETTINGS_TAB_LOADERS[name]?.();
  }
}

// The sidebar's entry point. Settings is a page with an address now, so this
// navigates; the router calls openSettingsPage() back.
function openSettings() {
  location.hash = '#/settings';
}

// Reached through the router, either from openSettings() or from a pasted
// link naming a tab.
async function openSettingsPage(tab) {
  document.getElementById('domain-input').value = currentDomain;
  _SETTINGS_FEEDBACK_IDS.forEach(id => _feedback(id));
  renderLibrariesList();

  const manage = can('MANAGE_SETTINGS');
  document.querySelectorAll('#settings-tabs [data-settings-tab]').forEach(a => {
    const restricted = _SETTINGS_TABS_NEED_MANAGE.includes(a.dataset.settingsTab);
    a.closest('.nav-item').style.display = restricted && !manage ? 'none' : '';
  });

  // Cleared on every arrival so a value changed elsewhere is picked up; moving
  // between tabs while here does not refetch.
  _settingsLoaded.clear();
  _appSettingsPromise = null;
  showPage('settings');

  const tabs = _visibleSettingsTabs();
  // A tab named in the URL wins, unless the visitor cannot see it - linking
  // someone to a tab their permissions hide must not leave them on a blank
  // pane.
  const wanted = tab && tabs.includes(tab) ? tab
    : (tabs.includes('sorgente') ? 'sorgente' : tabs[0]);
  if (wanted) await switchSettingsTab(wanted);
}

// ── Canali di notifica (Apprise) ─────────────────────────────────────────────

let _notifChannels = [];

async function loadNotificationChannels() {
  try {
    const data = await api.get('/api/notification-channels');
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
    const data = await api.patch(`/api/notification-channels/${id}`, {events});
    // Patched locally instead of refetching: a full reload would rebuild the
    // open picker under the cursor while the user is still clicking.
    const channel = _notifChannels.find(c => c.id === id);
    if (channel) channel.events = data.events || [];
    _feedback('notif-channels-feedback', 'Eventi aggiornati.', 'success');
    renderNotificationChannelsList();
  } catch (e) {
    _feedback('notif-channels-feedback', e instanceof ApiError ? (e.message || 'Errore aggiornamento eventi.') : 'Errore di rete.', 'danger');
    // The list is refetched only on failure: local state may now disagree
    // with the server.
    if (e instanceof ApiError) await loadNotificationChannels();
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
    await api.post('/api/notification-channels', {name, apprise_url});
    nameEl.value = '';
    urlEl.value = '';
    document.getElementById('notif-channel-form').style.display = 'none';
    _feedback('notif-channels-feedback', 'Canale aggiunto.', 'success');
    await loadNotificationChannels();
  } catch (e) {
    _feedback('notif-channels-feedback', e instanceof ApiError ? (e.message || 'Errore salvataggio.') : 'Errore di rete.', 'danger');
  }
  finally { btn.disabled = false; }
}

async function toggleNotificationChannel(id, enabled) {
  try {
    await api.patch(`/api/notification-channels/${id}`, {enabled});
    _feedback('notif-channels-feedback', enabled ? 'Canale attivo.' : 'Canale disattivato.', 'success');
    await loadNotificationChannels();
  } catch (e) {
    _feedback('notif-channels-feedback', e instanceof ApiError ? (e.message || 'Errore aggiornamento.') : 'Errore di rete.', 'danger');
    await loadNotificationChannels();
  }
}

async function deleteNotificationChannel(id) {
  const channel = _notifChannels.find(c => c.id === id);
  if (!await scConfirm(`Eliminare il canale «${channel ? channel.name : id}»?`)) return;
  try {
    await api.del(`/api/notification-channels/${id}`);
    _feedback('notif-channels-feedback', 'Canale eliminato.', 'success');
    await loadNotificationChannels();
  } catch (e) {
    _feedback('notif-channels-feedback', e instanceof ApiError ? (e.message || 'Errore eliminazione.') : 'Errore di rete.', 'danger');
  }
}

async function testNotificationChannel(id) {
  _feedback('notif-channels-feedback', 'Invio notifica di test...');
  try {
    const data = await api.post(`/api/notification-channels/${id}/test`);
    if (data.ok) {
      _feedback('notif-channels-feedback', 'Notifica di test inviata.', 'success');
      showToast('Notifica di test inviata', 'success');
    } else {
      _feedback('notif-channels-feedback', 'Invio fallito: controlla la URL.', 'danger');
    }
  } catch (e) {
    _feedback('notif-channels-feedback', e instanceof ApiError ? (e.message || 'Invio fallito: controlla la URL.') : 'Errore di rete.', 'danger');
  }
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
  const data = await _loadAppSettings();
  if (!data) return;
  document.getElementById('setting-max-concurrent').value = data.max_concurrent_downloads ?? 3;
  document.getElementById('setting-max-workers').value = data.max_segment_workers ?? 16;
  document.getElementById('setting-watch-interval').value =
    data.series_watch_interval_minutes ?? 240;
}

async function loadDomainRecoverySettings() {
  const data = await _loadAppSettings();
  if (!data) return;
  document.getElementById('domain-auto-check').checked =
    data.domain_auto_check_enabled !== false;
  document.getElementById('domain-auto-apply').checked = !!data.domain_auto_apply;
  document.getElementById('domain-check-interval').value =
    data.domain_check_interval_minutes ?? 360;
}


// ── Naming templates ───────────────────────────────────────────────────────────
//
// The preview is rendered by the server, using the same engine the downloader
// uses. Reimplementing it here would give two renderers that drift, and this way
// an invalid template shows its real validation error while it is being typed.

let _namingDefaults = null;
let _namingPreviewTimer = null;

function _namingInputs() {
  return [...document.querySelectorAll('[data-naming-slot]')];
}

async function loadNamingTemplates() {
  // The defaults come first: they are what every placeholder shows, and the
  // markup's hardcoded ones are only a fallback for when this fetch fails.
  // Without this the two copies drift the day a default changes server-side.
  if (!_namingDefaults) {
    try {
      const res = await fetch('/api/domain/settings/naming-defaults');
      if (res.ok) _namingDefaults = (await safeJson(res)).templates;
    } catch (e) { /* the markup's placeholders stand in */ }
  }

  const data = await _loadAppSettings();
  const templates = (data && data.naming_templates) || {};
  _namingInputs().forEach(input => {
    const slot = input.dataset.namingSlot;
    if (_namingDefaults && _namingDefaults[slot]) input.placeholder = _namingDefaults[slot];
    // Left blank when it matches the default, so the placeholder — which *is*
    // the default — stays visible, and the field reads as "nothing changed
    // here" rather than as a value somebody chose.
    const stored = templates[slot] || '';
    input.value = stored === input.placeholder ? '' : stored;
    if (!input.dataset.wired) {
      input.addEventListener('input', scheduleNamingPreview);
      input.dataset.wired = '1';
    }
  });
  refreshNamingPreview();
}

function scheduleNamingPreview() {
  clearTimeout(_namingPreviewTimer);
  _namingPreviewTimer = setTimeout(refreshNamingPreview, 200);
}

function _collectNamingTemplates() {
  // An empty field means the default, which is what its placeholder shows. The
  // server rejects an empty template outright, so the substitution happens here
  // rather than turning a blank box into a validation error.
  const templates = {};
  _namingInputs().forEach(i => {
    templates[i.dataset.namingSlot] = i.value.trim() || i.placeholder;
  });
  return templates;
}

async function refreshNamingPreview() {
  try {
    const res = await fetch('/api/domain/settings/naming-preview', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({templates: _collectNamingTemplates()}),
    });
    if (!res.ok) return;
    const data = await safeJson(res);
    _namingInputs().forEach(input => {
      const slot = data.slots[input.dataset.namingSlot];
      const line = document.getElementById(`naming-preview-${input.dataset.namingSlot}`);
      if (!line || !slot) return;
      if (slot.error) {
        line.className = 'form-text text-danger';
        line.textContent = slot.error;
      } else {
        line.className = 'form-text';
        line.textContent = `Esempio: ${slot.preview}`;
      }
    });
  } catch (e) { /* previews are a convenience; saving still validates */ }
}

async function saveNamingTemplates() {
  const btn = document.getElementById('save-naming-btn');
  btn.disabled = true;
  _feedback('naming-feedback', 'Salvataggio...');
  try {
    const res = await fetch('/api/domain/settings', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({naming_templates: _collectNamingTemplates()}),
    });
    if (res.ok) {
      _feedback('naming-feedback', 'Salvato.', 'success');
      showToast('Schema dei nomi salvato', 'success');
    } else {
      const d = await safeJson(res);
      _feedback('naming-feedback', _detailText(d) || 'Errore salvataggio.', 'danger');
    }
  } catch (e) { _feedback('naming-feedback', 'Errore di rete.', 'danger'); }
  finally { btn.disabled = false; }
}

async function resetNamingTemplates() {
  if (!await scConfirm('Ripristinare lo schema dei nomi predefinito?')) return;
  // Blank means default, so restoring is emptying every field.
  _namingInputs().forEach(input => { input.value = ''; });
  refreshNamingPreview();
  _feedback('naming-feedback', 'Predefiniti ripristinati: premi Salva per applicarli.');
}


// ── Post-download hooks ────────────────────────────────────────────────────────
//
// Webhooks only: open mode grants MANAGE_SETTINGS to every anonymous visitor, so
// a shell hook would be remote code execution for whoever can reach the panel.
// See app/downloads_hooks.py.

let _hooks = [];

const HOOK_EVENT_LABELS = {
  done: 'Completato',
  error: 'Fallito',
  cancelled: 'Annullato',
};

async function loadJellyfinRefresh() {
  const data = await _loadAppSettings();
  if (!data) return;
  document.getElementById('jf-refresh-on-download').checked =
    !!data.jellyfin_refresh_on_download;

}

// Set from the hooks payload, which reports whether the refresh has credentials
// to use. Inferring it from the auth status would report an installation that
// skipped the wizard and connected Jellyfin later as unconnected.
function renderJellyfinRefreshAvailability(connected) {
  const toggle = document.getElementById('jf-refresh-on-download');
  if (!toggle) return;
  toggle.disabled = !connected;
  document.getElementById('jf-refresh-status').textContent = connected
    ? ''
    : 'Jellyfin non è collegato: collegalo da Accesso e utenti perché questa opzione abbia effetto.';
}

async function saveJellyfinRefresh() {
  const btn = document.getElementById('save-jf-refresh-btn');
  btn.disabled = true;
  _feedback('jf-refresh-feedback', 'Salvataggio...');
  try {
    const res = await fetch('/api/domain/settings', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        jellyfin_refresh_on_download:
          document.getElementById('jf-refresh-on-download').checked,
      }),
    });
    if (res.ok) {
      _feedback('jf-refresh-feedback', 'Salvato.', 'success');
      showToast('Impostazione salvata', 'success');
    } else {
      const d = await safeJson(res);
      _feedback('jf-refresh-feedback', d.detail || 'Errore salvataggio.', 'danger');
    }
  } catch (e) { _feedback('jf-refresh-feedback', 'Errore di rete.', 'danger'); }
  finally { btn.disabled = false; }
}

async function loadHooks() {
  try {
    const res = await fetch('/api/download-hooks');
    if (!res.ok) return;
    const data = await safeJson(res);
    _hooks = data.hooks || [];
    renderJellyfinRefreshAvailability(!!data.jellyfin_connected);
    renderHooksList();
  } catch (e) { /* the list simply stays as it was */ }
}

function _hookEventSummary(hook) {
  // An empty list means every event — the same convention the notification
  // channels use, and the one thing here that is easy to read backwards.
  if (!hook.events || !hook.events.length) return 'Tutti gli esiti';
  return hook.events.map(e => HOOK_EVENT_LABELS[e] || e).join(', ');
}

function renderHooksList() {
  const container = document.getElementById('hooks-list');
  if (!_hooks.length) {
    container.innerHTML = '<p class="text-muted small mb-0">Nessun webhook configurato.</p>';
    return;
  }
  container.innerHTML = _hooks.map(hook => `
    <div class="border-bottom pb-1 mb-1">
      <div class="d-flex align-items-center gap-2 py-1">
        <label class="form-check form-switch mb-0">
          <input class="form-check-input" type="checkbox" ${hook.enabled ? 'checked' : ''}
                 onchange="toggleHook(${hook.id}, this.checked)">
        </label>
        <div class="flex-fill text-truncate">
          <span style="color:var(--text)">${escapeHtml(hook.name)}</span>
          <span class="text-muted small ms-2">${escapeHtml(hook.method)} ${escapeHtml(hook.url_masked || '')}</span>
        </div>
        <span class="badge bg-secondary-lt">${escapeHtml(_hookEventSummary(hook))}</span>
        <button class="btn btn-sm btn-outline-secondary" onclick="testHook(${hook.id})">
          <i class="ti ti-send me-1"></i>Test
        </button>
        <button class="btn btn-sm btn-outline-danger" onclick="deleteHook(${hook.id})">
          <i class="ti ti-trash"></i>
        </button>
      </div>
    </div>`).join('');
}

function toggleHookForm() {
  const form = document.getElementById('hook-form');
  form.style.display = form.style.display === 'none' ? '' : 'none';
}

async function saveHook() {
  const btn = document.getElementById('hook-save-btn');
  const name = document.getElementById('hook-name').value.trim();
  const url = document.getElementById('hook-url').value.trim();
  if (!name || !url) {
    _feedback('hooks-feedback', 'Nome e URL sono obbligatori.', 'danger'); return;
  }
  btn.disabled = true;
  _feedback('hooks-feedback', 'Salvataggio...');
  try {
    const res = await fetch('/api/download-hooks', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        name, url,
        method: document.getElementById('hook-method').value,
        body_template: document.getElementById('hook-body').value,
      }),
    });
    if (res.ok) {
      document.getElementById('hook-name').value = '';
      document.getElementById('hook-url').value = '';
      document.getElementById('hook-body').value = '';
      document.getElementById('hook-form').style.display = 'none';
      _feedback('hooks-feedback', 'Aggiunto.', 'success');
      await loadHooks();
    } else {
      const d = await safeJson(res);
      _feedback('hooks-feedback', _detailText(d) || 'Errore salvataggio.', 'danger');
    }
  } catch (e) { _feedback('hooks-feedback', 'Errore di rete.', 'danger'); }
  finally { btn.disabled = false; }
}

async function toggleHook(id, enabled) {
  try {
    await fetch(`/api/download-hooks/${id}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled}),
    });
  } catch (e) { _feedback('hooks-feedback', 'Errore di rete.', 'danger'); }
  await loadHooks();
}

async function deleteHook(id) {
  if (!await scConfirm('Eliminare questo webhook?')) return;
  try {
    await fetch(`/api/download-hooks/${id}`, {method: 'DELETE'});
    await loadHooks();
  } catch (e) { _feedback('hooks-feedback', 'Errore di rete.', 'danger'); }
}

async function testHook(id) {
  _feedback('hooks-feedback', 'Invio chiamata di prova...');
  try {
    const res = await fetch(`/api/download-hooks/${id}/test`, {method: 'POST'});
    const data = await safeJson(res);
    // Only an outcome and a status code come back: the panel never relays what
    // the other end said.
    if (res.ok && data.ok) {
      _feedback('hooks-feedback', `Riuscito (HTTP ${data.status}).`, 'success');
      showToast('Webhook raggiunto', 'success');
    } else {
      _feedback('hooks-feedback',
        data.status ? `Fallito (HTTP ${data.status}).` : 'Nessuna risposta.', 'danger');
    }
  } catch (e) { _feedback('hooks-feedback', 'Errore di rete.', 'danger'); }
}

// ── Post-download hooks end ────────────────────────────────────────────────────

async function saveDomainRecovery() {
  const btn = document.getElementById('save-domain-recovery-btn');
  const interval = parseInt(document.getElementById('domain-check-interval').value, 10);
  if (!(interval >= 30 && interval <= 1440)) {
    _feedback('domain-recovery-feedback', 'Intervallo tra 30 e 1440 minuti.', 'danger');
    return;
  }
  const autoApply = document.getElementById('domain-auto-apply').checked;
  // Turning this on hands a page we do not control the ability to move the
  // panel's source. Worth one deliberate click.
  if (autoApply && !await scConfirm(
      'Con l\'applicazione automatica il pannello adotta il dominio trovato senza chiedere. ' +
      'Verranno accettati solo domini verificati e con un nome riconosciuto. Continuare?')) {
    return;
  }
  btn.disabled = true;
  _feedback('domain-recovery-feedback', 'Salvataggio...');
  try {
    const res = await fetch('/api/domain/settings', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        domain_auto_check_enabled: document.getElementById('domain-auto-check').checked,
        domain_auto_apply: autoApply,
        domain_check_interval_minutes: interval,
      }),
    });
    if (res.ok) {
      _feedback('domain-recovery-feedback', 'Salvato.', 'success');
      showToast('Impostazioni salvate', 'success');
    } else {
      const d = await safeJson(res);
      _feedback('domain-recovery-feedback', d.detail || 'Errore salvataggio.', 'danger');
    }
  } catch (e) { _feedback('domain-recovery-feedback', 'Errore di rete.', 'danger'); }
  finally { btn.disabled = false; }
}

async function checkDomainNow() {
  const btn = document.getElementById('domain-check-btn');
  btn.disabled = true;
  _feedback('domain-recovery-feedback', 'Controllo in corso...');
  try {
    const res = await fetch('/api/domain/check', {method: 'POST'});
    if (!res.ok) {
      const d = await safeJson(res);
      _feedback('domain-recovery-feedback', d.detail || 'Controllo fallito.', 'danger');
      return;
    }
    const data = await safeJson(res);
    if (data.applied) {
      _feedback('domain-recovery-feedback', `Applicato ${data.candidate}.`, 'success');
      await loadDomainStatus();
    } else if (data.candidate) {
      _feedback('domain-recovery-feedback', `Trovato ${data.candidate}: da applicare.`, 'success');
    } else if (data.current_ok) {
      _feedback('domain-recovery-feedback', 'Il dominio attuale risponde.', 'success');
    } else {
      // Rejections are shown rather than swallowed: a rebranded source and an
      // edited page look identical from here, and only a person can tell them
      // apart.
      const why = (data.rejected || []).map(r => `${r.host} (${r.reason})`).join(', ');
      _feedback('domain-recovery-feedback',
        why ? `Nessun dominio adottabile. Scartati: ${why}` : 'Nessun dominio trovato.',
        'danger');
    }
    await loadDomainCandidate();
  } catch (e) { _feedback('domain-recovery-feedback', 'Errore di rete.', 'danger'); }
  finally { btn.disabled = false; }
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
