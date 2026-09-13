'use strict';

/* StreamingCommunity Web Panel — page-requests.js */
//
// Both request pages: the approvers' queue and "Le mie richieste". They share
// the status vocabulary, the grouping of a show's episodes into one collapsible
// row, and the bulk-selection machinery, so they stay in one file.

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

// Same set as the server's OPEN_STATUSES: unresolved, and — not coincidentally
// — exactly the statuses a request can still be withdrawn from. A closed
// request (completed/available/denied/failed/cancelled) has no way back into
// "cancelled" in the state machine, so there is nothing to offer there.
const MINE_ACTIVE_STATUSES = ['pending', 'approved', 'downloading', 'needs_attention'];

let _queue = [];
let _myRequests = [];

// Selection state, kept separate per page so switching pages doesn't leak one
// selection into the other.
const _selected = { queue: new Set(), mine: new Set() };
// Which series/season groups the user has toggled open, keyed by group key.
// Persists across re-renders (SSE refresh, filter change) within the session.
const _expandedGroups = new Set();

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

function episodeLabel(request) {
  if (request.media_type === 'episode') {
    return `S${String(request.season).padStart(2, '0')}E${String(request.episode_number).padStart(2, '0')}`;
  }
  if (request.media_type === 'anime') return `E${request.episode_number}`;
  return request.title;
}

// ── Grouping: episodes/anime of the same show, collapsible ─────────────────────
//
// Films are never grouped — each is already a single request. TV episodes and
// anime episodes that share a source + external id are the same show, whatever
// season or episode: bundling them is what makes approving 40 episodes at once
// readable instead of a wall of identical-looking rows.

function groupKey(r) {
  if (r.media_type === 'film') return `film-${r.id}`;
  return `${r.source}:${r.media_type}:${r.external_id}`;
}

function groupRequests(list) {
  const groups = new Map();
  for (const r of list) {
    const key = groupKey(r);
    if (!groups.has(key)) {
      groups.set(key, {
        key, grouped: r.media_type !== 'film',
        title: r.title, year: r.year, poster: r.poster, source: r.source,
        items: [],
      });
    }
    groups.get(key).items.push(r);
  }
  const all = [...groups.values()];
  for (const g of all) {
    g.items.sort((a, b) => {
      const seasonDiff = (a.season ?? 0) - (b.season ?? 0);
      if (seasonDiff !== 0) return seasonDiff;
      return parseFloat(a.episode_number ?? 0) - parseFloat(b.episode_number ?? 0);
    });
    g.latest = g.items.reduce((m, r) => (r.created_at > m ? r.created_at : m), '');
  }
  all.sort((a, b) => b.latest.localeCompare(a.latest));
  return all;
}

function groupIsExpanded(g) {
  if (!g.grouped || g.items.length <= 1) return true; // singles render flat, no header to collapse
  return _expandedGroups.has(g.key);
}

function toggleGroup(key) {
  if (_expandedGroups.has(key)) _expandedGroups.delete(key);
  else _expandedGroups.add(key);
  // Re-render whichever page is showing.
  if (document.getElementById('page-requests').style.display !== 'none') renderRequestQueue();
  else renderMyRequests();
}

function groupStatusCounts(items) {
  const counts = {};
  for (const r of items) counts[r.status] = (counts[r.status] || 0) + 1;
  return counts;
}

function groupStatusPills(items) {
  const counts = groupStatusCounts(items);
  return Object.entries(counts)
    .map(([status, n]) => {
      const meta = REQUEST_STATUS_LABELS[status] || { label: status, cls: 'bg-secondary' };
      return `<span class="badge ${meta.cls}">${n} ${meta.label.toLowerCase()}</span>`;
    })
    .join('');
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

// Swaps the broken <img> for the same empty slot the no-poster branch below
// renders. It was a fragment of HTML written into an attribute, entity-escaped
// quotes and all, which is unreadable and one stray quote from breaking.
function reqPosterError(img) {
  const slot = document.createElement('div');
  slot.className = 'req-poster req-poster-empty';
  slot.textContent = '\u{1F3AC}';
  img.replaceWith(slot);
}

function requestPoster(request) {
  if (!request.poster) return '<div class="req-poster req-poster-empty">🎬</div>';
  const url = request.poster.startsWith('http')
    ? request.poster
    : `/api/image/${request.poster}`;
  return `<img class="req-poster" src="${escapeHtml(url)}" alt=""
            onerror="reqPosterError(this)">`;
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
    _queue = await api.get('/api/requests');
    renderRequestQueue();
    refreshQueueBadge();
  } catch (e) {
    container.innerHTML = `<div class="alert alert-danger">${escapeHtml(e.message)}</div>`;
  }
}

function queueFilter() {
  const active = document.querySelector('#queue-filters .queue-filter.active');
  return active ? active.dataset.filter : 'action';
}

function setQueueFilter(filter) {
  setActivePill('queue-filters', filter);
  syncHash();
  renderRequestQueue();
}

function _matchesQueueFilter(status, filter) {
  if (filter === 'all') return true;
  // Default view: only what actually needs a decision. "approved" is a
  // transient claim state (resolution is running), not something to act on.
  if (filter === 'action') return status === 'pending' || status === 'needs_attention';
  if (filter === 'in_progress') return status === 'approved' || status === 'downloading';
  return status === filter;
}

// The figures in the page head, counted over everything loaded rather than
// over what the current filter shows: their job is to say whether switching
// filter would find anything.
function _renderStats(elementId, chips) {
  const el = document.getElementById(elementId);
  if (!el) return;
  el.innerHTML = chips.map(([value, label, cls]) =>
    `<span class="pg-stat ${value ? cls : 'pg-stat-zero'}"><b>${value}</b><span>${label}</span></span>`
  ).join('');
}

function renderQueueStats() {
  const n = status => _queue.filter(r => _matchesQueueFilter(r.status, status)).length;
  _renderStats('queue-stats', [
    [n('action'), 'Da gestire', 'pg-stat-warn'],
    [n('in_progress'), 'In corso', 'pg-stat-live'],
    [_queue.length, 'Totali', 'pg-stat-zero'],
  ]);
}

function renderRequestQueue() {
  renderQueueStats();
  const container = document.getElementById('requests-list');
  const filter = queueFilter();
  const groups = groupRequests(_queue)
    .map(g => ({ ...g, items: g.items.filter(r => _matchesQueueFilter(r.status, filter)) }))
    .filter(g => g.items.length);

  if (!groups.length) {
    container.innerHTML = `<div class="empty-panel">
      <i class="ti ti-inbox"></i><p>Nessuna richiesta da mostrare.</p></div>`;
  } else {
    container.innerHTML = groups.map(g => renderQueueGroup(g)).join('');
  }
  syncSelectionBar('queue');
}

function renderQueueRow(r, compact = false) {
  const checked = _selected.queue.has(r.id) ? 'checked' : '';
  const title = compact ? episodeLabel(r) : requestTitle(r);
  return `
    <div class="req-row ${r.status === 'needs_attention' ? 'req-row-attention' : ''}">
      <input type="checkbox" class="req-check" ${checked}
             data-action="req:toggle" data-page="queue" data-id="${r.id}">
      ${compact ? '' : requestPoster(r)}
      <div class="req-main">
        <div class="req-title">${escapeHtml(title)}${!compact && r.year ? ` <span class="text-muted">(${escapeHtml(r.year)})</span>` : ''}</div>
        <div class="req-meta">
          <i class="ti ti-user"></i> ${escapeHtml(r.requested_by_username || '?')}
          ${r.subscribers.length > 1 ? `<span class="badge bg-purple-lt ms-1">+${r.subscribers.length - 1} altri</span>` : ''}
          <span class="req-dot">·</span>
          <i class="ti ti-calendar"></i> ${fmtDate(r.created_at)}
          ${compact ? '' : `<span class="req-dot">·</span>
          <span class="text-muted">${escapeHtml(r.source === 'animeunity' ? 'AnimeUnity' : 'StreamingCommunity')}</span>`}
        </div>
        ${trackBadges(r)}
        ${r.problem ? `<div class="req-problem"><i class="ti ti-alert-circle me-1"></i>${escapeHtml(r.problem)}</div>` : ''}
        ${r.denial_reason ? `<div class="req-denied"><i class="ti ti-x me-1"></i>${escapeHtml(r.denial_reason)}</div>` : ''}
      </div>
      <div class="req-side">
        ${statusBadge(r.status)}
        <div class="req-actions">
          ${r.watch_id && r.status === 'pending' ? `
            <label class="form-check form-check-inline mb-0" style="font-size:11px"
                   title="Approva anche i prossimi episodi di questa serie, senza ripassare dalla coda">
              <input type="checkbox" class="form-check-input" id="watch-auto-${r.id}">
              <span class="form-check-label">Auto i prossimi</span>
            </label>` : ''}
          ${['pending', 'needs_attention'].includes(r.status) ? `
            <button class="btn btn-sm btn-success" data-action="queue:approveOne" data-id="${r.id}">
              <i class="ti ti-check me-1"></i>${r.status === 'needs_attention' ? 'Riprova' : 'Approva'}</button>
            <button class="btn btn-sm btn-outline-danger" data-action="queue:denyOne" data-id="${r.id}">
              <i class="ti ti-x"></i></button>` : ''}
          ${r.status === 'needs_attention' ? `
            <button class="btn btn-sm btn-outline-warning" data-action="queue:fix" data-id="${r.id}">
              <i class="ti ti-tool me-1"></i>Correggi</button>` : ''}
        </div>
      </div>
    </div>`;
}

function renderQueueGroup(g) {
  if (!g.grouped) return renderQueueRow(g.items[0]);

  const expanded = groupIsExpanded(g);
  const ids = g.items.map(r => r.id);
  const allSelected = ids.every(id => _selected.queue.has(id));
  const someSelected = !allSelected && ids.some(id => _selected.queue.has(id));

  return `
    <div class="req-group ${expanded ? 'expanded' : ''}" data-group-key="${escapeHtml(g.key)}">
      <div class="req-group-header" data-action="req:toggleGroup" data-key="${escapeHtml(g.key)}">
        <input type="checkbox" class="req-check" ${allSelected ? 'checked' : ''}
               ${someSelected ? 'data-indeterminate="1"' : ''}
               data-action="req:toggleGroupSel" data-page="queue" data-ids="${ids.join(',')}">
        <i class="ti ti-chevron-right req-group-chevron"></i>
        ${requestPoster(g)}
        <div>
          <div class="req-group-title">${escapeHtml(g.title)}${g.year ? ` <span class="text-muted">(${escapeHtml(g.year)})</span>` : ''}</div>
          <div class="req-group-count">${g.items.length} episodi richiesti</div>
        </div>
        <div class="req-group-statuses">${groupStatusPills(g.items)}</div>
      </div>
      <div class="req-group-body">
        ${g.items.map(r => renderQueueRow(r, true)).join('')}
      </div>
    </div>`;
}

// ── Bulk selection ──────────────────────────────────────────────────────────────

function toggleSelected(page, id) {
  const set = _selected[page];
  if (set.has(id)) set.delete(id); else set.add(id);
  syncSelectionBar(page);
}

function toggleGroupSelected(page, ids) {
  const set = _selected[page];
  const allIn = ids.every(id => set.has(id));
  ids.forEach(id => allIn ? set.delete(id) : set.add(id));
  if (page === 'queue') renderRequestQueue(); else renderMyRequests();
}

function clearSelection(page) {
  _selected[page].clear();
  if (page === 'queue') renderRequestQueue(); else renderMyRequests();
}

function syncSelectionBar(page) {
  const set = _selected[page];
  const bar = document.getElementById(page === 'queue' ? 'queue-selection-bar' : 'mine-selection-bar');
  const count = document.getElementById(page === 'queue' ? 'queue-selection-count' : 'mine-selection-count');
  bar.style.visibility = set.size ? 'visible' : 'hidden';
  count.textContent = `${set.size} selezionat${set.size === 1 ? 'a' : 'e'}`;
  // Reflect selection on checkboxes already in the DOM without a full re-render
  // (checkboxes are re-synced on every render anyway, this just keeps clicks snappy).
  document.querySelectorAll('.req-check[data-indeterminate]').forEach(cb => {
    cb.indeterminate = true;
  });
}

async function approveRequests(ids) {
  // Ticked only on requests a followed series produced: saying yes here means
  // the rest of that series stops asking.
  const auto_approve_watch_ids = ids.filter(
    id => document.getElementById(`watch-auto-${id}`)?.checked
  );
  let data;
  try {
    data = await api.post('/api/requests/approve-batch', { ids, auto_approve_watch_ids });
  } catch (e) { showToast(errText(e), 'danger'); return; }
  const failed = (data.results || []).filter(r => !r.ok);
  showToast(
    failed.length
      ? `${ids.length - failed.length}/${ids.length} approvate, ${failed.length} fallite`
      : ids.length > 1 ? `${ids.length} richieste approvate` : 'Richiesta approvata',
    failed.length ? 'warning' : 'success',
  );
  ids.forEach(id => _selected.queue.delete(id));
  await loadRequestQueue();
  refreshNotifications();
}

function approveSelected() {
  const ids = [..._selected.queue];
  if (ids.length) approveRequests(ids);
}

let _denyIds = [];

function openDenyModal(ids) {
  _denyIds = ids;
  const single = ids.length === 1 ? _queue.find(r => r.id === ids[0]) : null;
  document.getElementById('deny-title').textContent = single
    ? requestTitle(single)
    : `${ids.length} richieste selezionate`;
  document.getElementById('deny-reason').value = '';
  showModal('deny-modal');
  setTimeout(() => document.getElementById('deny-reason').focus(), 150);
}

function denySelected() {
  const ids = [..._selected.queue];
  if (ids.length) openDenyModal(ids);
}

async function confirmDeny() {
  const reason = document.getElementById('deny-reason').value.trim();
  let data;
  try {
    data = await api.post('/api/requests/deny-batch', { ids: _denyIds, reason: reason || null });
  } catch (e) { showToast(errText(e), 'danger'); return; }
  hideModal('deny-modal');
  const failed = (data.results || []).filter(r => !r.ok);
  showToast(
    failed.length ? `${failed.length} richieste non rifiutabili` : 'Richieste rifiutate',
    failed.length ? 'warning' : 'info',
  );
  _denyIds.forEach(id => _selected.queue.delete(id));
  loadRequestQueue();
}

async function _cancelIds(ids, page) {
  if (!ids.length) return;
  if (!await scConfirm(`Annullare ${ids.length} richiest${ids.length === 1 ? 'a' : 'e'}?`)) return;
  let data;
  try {
    data = await api.post('/api/requests/cancel-batch', { ids });
  } catch (e) { showToast(errText(e), 'danger'); return; }
  const failed = (data.results || []).filter(r => !r.ok);
  showToast(
    failed.length ? `${failed.length} richieste non annullabili` : 'Richieste annullate',
    failed.length ? 'warning' : 'info',
  );
  ids.forEach(id => _selected[page].delete(id));
  if (page === 'queue') { await loadRequestQueue(); refreshNotifications(); }
  else await loadMyRequests();
}

function cancelSelected(page) {
  return _cancelIds([..._selected[page]], page);
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

  try {
    await api.patch(`/api/requests/${_fixId}`, payload);
  } catch (e) { showToast(errText(e), 'danger'); return; }
  hideModal('fix-modal');
  showToast('Richiesta corretta — approvala per riprovare', 'success');
  loadRequestQueue();
}

// ── My requests ────────────────────────────────────────────────────────────────

async function loadMyRequests() {
  const container = document.getElementById('my-requests-list');
  container.innerHTML = '<div class="text-center py-4"><span class="spinner-border"></span></div>';
  try {
    _myRequests = await api.get('/api/requests/mine');
    renderMyRequests();
  } catch (e) {
    container.innerHTML = `<div class="alert alert-danger">${escapeHtml(e.message)}</div>`;
  }
}

function mineFilter() {
  const active = document.querySelector('#mine-filters .queue-filter.active');
  return active ? active.dataset.filter : 'active';
}

function setMineFilter(filter) {
  setActivePill('mine-filters', filter);
  syncHash();
  renderMyRequests();
}

function _matchesMineFilter(status, filter) {
  if (filter === 'all') return true;
  const isActive = MINE_ACTIVE_STATUSES.includes(status);
  return filter === 'active' ? isActive : !isActive;
}

function renderMineRow(r, compact = false) {
  const cancellable = MINE_ACTIVE_STATUSES.includes(r.status);
  const checkboxHtml = cancellable
    ? `<input type="checkbox" class="req-check" ${_selected.mine.has(r.id) ? 'checked' : ''}
             data-action="req:toggle" data-page="mine" data-id="${r.id}">`
    : '<span class="req-check-spacer"></span>';
  const title = compact ? episodeLabel(r) : requestTitle(r);
  return `
    <div class="req-row">
      ${checkboxHtml}
      ${compact ? '' : requestPoster(r)}
      <div class="req-main">
        <div class="req-title">${escapeHtml(title)}${!compact && r.year ? ` <span class="text-muted">(${escapeHtml(r.year)})</span>` : ''}</div>
        <div class="req-meta"><i class="ti ti-calendar"></i> ${fmtDate(r.created_at)}</div>
        ${trackBadges(r)}
        ${r.denial_reason ? `<div class="req-denied"><i class="ti ti-x me-1"></i>Motivo: ${escapeHtml(r.denial_reason)}</div>` : ''}
        ${r.status === 'needs_attention' ? '<div class="req-problem"><i class="ti ti-clock-pause me-1"></i>In attesa di una verifica da parte di un amministratore.</div>' : ''}
      </div>
      <div class="req-side">
        ${statusBadge(r.status)}
        <div class="req-actions">
          ${cancellable ? `<button class="btn btn-sm btn-outline-secondary" data-action="mine:withdraw" data-id="${r.id}">
            <i class="ti ti-trash me-1"></i>Annulla</button>` : ''}
        </div>
      </div>
    </div>`;
}

function renderMineGroup(g) {
  if (!g.grouped) return renderMineRow(g.items[0]);

  const expanded = groupIsExpanded(g);
  const cancellableIds = g.items.filter(r => MINE_ACTIVE_STATUSES.includes(r.status)).map(r => r.id);
  const allSelected = cancellableIds.length > 0 && cancellableIds.every(id => _selected.mine.has(id));
  const someSelected = !allSelected && cancellableIds.some(id => _selected.mine.has(id));
  const headerCheckbox = cancellableIds.length
    ? `<input type="checkbox" class="req-check" ${allSelected ? 'checked' : ''}
             ${someSelected ? 'data-indeterminate="1"' : ''}
             data-action="req:toggleGroupSel" data-page="mine" data-ids="${cancellableIds.join(',')}">`
    : '<span class="req-check-spacer"></span>';

  return `
    <div class="req-group ${expanded ? 'expanded' : ''}" data-group-key="${escapeHtml(g.key)}">
      <div class="req-group-header" data-action="req:toggleGroup" data-key="${escapeHtml(g.key)}">
        ${headerCheckbox}
        <i class="ti ti-chevron-right req-group-chevron"></i>
        ${requestPoster(g)}
        <div>
          <div class="req-group-title">${escapeHtml(g.title)}${g.year ? ` <span class="text-muted">(${escapeHtml(g.year)})</span>` : ''}</div>
          <div class="req-group-count">${g.items.length} episodi richiesti</div>
        </div>
        <div class="req-group-statuses">${groupStatusPills(g.items)}</div>
      </div>
      <div class="req-group-body">
        ${g.items.map(r => renderMineRow(r, true)).join('')}
      </div>
    </div>`;
}

function renderMineStats() {
  const active = _myRequests.filter(r => MINE_ACTIVE_STATUSES.includes(r.status)).length;
  _renderStats('mine-stats', [
    [active, 'In corso', 'pg-stat-live'],
    [_myRequests.length - active, 'Concluse', 'pg-stat-ok'],
  ]);
}

function renderMyRequests() {
  renderMineStats();
  const container = document.getElementById('my-requests-list');
  if (!_myRequests.length) {
    container.innerHTML = `<div class="empty-panel">
      <i class="ti ti-send"></i><p>Non hai ancora richiesto niente.</p></div>`;
    syncSelectionBar('mine');
    return;
  }

  const filter = mineFilter();
  const groups = groupRequests(_myRequests)
    .map(g => ({ ...g, items: g.items.filter(r => _matchesMineFilter(r.status, filter)) }))
    .filter(g => g.items.length);

  if (!groups.length) {
    container.innerHTML = `<div class="empty-panel">
      <i class="ti ti-filter-off"></i><p>Nessuna richiesta in questa categoria.</p></div>`;
    syncSelectionBar('mine');
    return;
  }

  container.innerHTML = groups.map(g => renderMineGroup(g)).join('');
  syncSelectionBar('mine');
}

/** Withdraw a single request via the batch endpoint, independent of any
 * bulk selection the user might currently have (must not sweep up other
 * checked rows just because one row's own button was clicked). */
async function withdrawRequest(id) {
  await _cancelIds([id], 'mine');
}


// ── Delegated handlers ───────────────────────────────────────────────────────
//
// dataset values are strings; the selection sets hold the numeric ids the API
// returns, so every id crossing this boundary is converted back.

const _ids = d => (d.ids || '').split(',').filter(Boolean).map(Number);

registerActions({
  'queue:reload':        () => loadRequestQueue(),
  'queue:filter':        d => setQueueFilter(d.filter),
  'queue:approve':       () => approveSelected(),
  'queue:deny':          () => denySelected(),
  'queue:approveOne':    d => approveRequests([Number(d.id)]),
  'queue:denyOne':       d => openDenyModal([Number(d.id)]),
  'queue:fix':           d => openFixModal(Number(d.id)),
  'mine:reload':         () => loadMyRequests(),
  'mine:filter':         d => setMineFilter(d.filter),
  'mine:withdraw':       d => withdrawRequest(Number(d.id)),
  'req:toggle':          d => toggleSelected(d.page, Number(d.id)),
  'req:toggleGroupSel':  d => toggleGroupSelected(d.page, _ids(d)),
  'req:toggleGroup':     d => toggleGroup(d.key),
  'req:cancelSelected':  d => cancelSelected(d.page),
  'req:clearSelection':  d => clearSelection(d.page),
  // The two request modals. They stay modals on purpose: an interruption
  // asking for one answer is not a place.
  'queue:confirmDeny':   () => confirmDeny(),
  'queue:confirmFix':    () => confirmFix(),
});


// ── The address ──────────────────────────────────────────────────────────────
//
// Both filters live in the DOM rather than in a variable — queueFilter() and
// mineFilter() read the active pill — so applying one is moving the pill, and
// reading one is asking which pill is lit. The default is left out of the
// address, so the everyday link stays #/requests.

registerPageHash('requests', {
  read: () => ({ params: { f: queueFilter() === 'action' ? null : queueFilter() } }),
  apply: params => setActivePill('queue-filters', params.f || 'action'),
});

registerPageHash('my-requests', {
  read: () => ({ params: { f: mineFilter() === 'active' ? null : mineFilter() } }),
  apply: params => setActivePill('mine-filters', params.f || 'active'),
});
