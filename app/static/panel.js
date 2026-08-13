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

// Same set as the server's OPEN_STATUSES: unresolved, and — not coincidentally
// — exactly the statuses a request can still be withdrawn from. A closed
// request (completed/available/denied/failed/cancelled) has no way back into
// "cancelled" in the state machine, so there is nothing to offer there.
const MINE_ACTIVE_STATUSES = ['pending', 'approved', 'downloading', 'needs_attention'];

// NOTIFICATION_ICONS and NOTIFICATION_LABELS live in app.js, which loads first:
// the settings modal needs them as module constants to build the per-channel
// event picker, and this file already depends on app.js's helpers.

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
  document.querySelectorAll('#queue-filters .queue-filter').forEach(el =>
    el.classList.toggle('active', el.dataset.filter === filter));
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

function renderRequestQueue() {
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
             onclick="event.stopPropagation(); toggleSelected('queue', ${r.id})">
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
            <button class="btn btn-sm btn-success" onclick="approveRequests([${r.id}])">
              <i class="ti ti-check me-1"></i>${r.status === 'needs_attention' ? 'Riprova' : 'Approva'}</button>
            <button class="btn btn-sm btn-outline-danger" onclick="openDenyModal([${r.id}])">
              <i class="ti ti-x"></i></button>` : ''}
          ${r.status === 'needs_attention' ? `
            <button class="btn btn-sm btn-outline-warning" onclick="openFixModal(${r.id})">
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
      <div class="req-group-header" onclick="toggleGroup(this.closest('.req-group').dataset.groupKey)">
        <input type="checkbox" class="req-check" ${allSelected ? 'checked' : ''}
               ${someSelected ? 'data-indeterminate="1"' : ''}
               onclick="event.stopPropagation(); toggleGroupSelected('queue', ${JSON.stringify(ids)})">
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
  const res = await fetch('/api/requests/approve-batch', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids, auto_approve_watch_ids }),
  });
  const data = await safeJson(res);
  if (!res.ok) { showToast(data.detail || 'Errore', 'danger'); return; }
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
  const res = await fetch('/api/requests/deny-batch', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids: _denyIds, reason: reason || null }),
  });
  const data = await safeJson(res);
  if (!res.ok) { showToast(data.detail || 'Errore', 'danger'); return; }
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
  const res = await fetch('/api/requests/cancel-batch', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids }),
  });
  const data = await safeJson(res);
  if (!res.ok) { showToast(data.detail || 'Errore', 'danger'); return; }
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
  document.querySelectorAll('#mine-filters .queue-filter').forEach(el =>
    el.classList.toggle('active', el.dataset.filter === filter));
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
             onclick="event.stopPropagation(); toggleSelected('mine', ${r.id})">`
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
          ${cancellable ? `<button class="btn btn-sm btn-outline-secondary" onclick="withdrawRequest(${r.id})">
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
             onclick="event.stopPropagation(); toggleGroupSelected('mine', ${JSON.stringify(cancellableIds)})">`
    : '<span class="req-check-spacer"></span>';

  return `
    <div class="req-group ${expanded ? 'expanded' : ''}" data-group-key="${escapeHtml(g.key)}">
      <div class="req-group-header" onclick="toggleGroup(this.closest('.req-group').dataset.groupKey)">
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

function renderMyRequests() {
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

// ── Sidebar "Richieste" badge ───────────────────────────────────────────────────
//
// Hidden entirely at zero, shown with the real count from the moment the app
// loads — not just after the user happens to open the queue page.

async function refreshQueueBadge() {
  if (!can('MANAGE_REQUESTS')) return;
  const badge = document.getElementById('queue-pending-count');
  if (!badge) return;
  try {
    const res = await fetch('/api/requests/counts');
    if (!res.ok) return;
    const counts = await res.json();
    const n = counts.action_required || 0;
    badge.textContent = n || '';
    badge.style.display = n ? '' : 'none';
  } catch (e) { /* leave the badge as it was */ }
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
            <div class="notif-body">
              <div class="notif-text">${escapeHtml(n.message)}</div>
              <div class="notif-time">${fmtDate(n.created_at)}</div>
            </div>
            <button class="notif-del" onclick="deleteNotification(${n.id})"
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
  await fetch('/api/notifications/read', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  refreshNotifications();
}

async function _deleteNotifications(body) {
  const res = await fetch('/api/notifications/delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    showToast('Eliminazione fallita', 'danger');
    return null;
  }
  await refreshNotifications();
  return res.json();
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
