// ── Library associations ──────────────────────────────────────────────────────
//
// Which folder holds which title (app/library.py). Shown in two places that
// share one modal: the Associazioni tab in Settings, every association at once,
// and a card on a title's page, just that title's. Both are MANAGE_SETTINGS
// only, like the endpoints behind them.
//
// There is no "reject" button. Changing the id or the folder is the correction,
// and removing is a correction to nothing; the server keeps the old pair set
// aside so the panel does not guess it back.

const LIB_SOURCE_PREFIX = { streamingcommunity: 'SC', animeunity: 'AU' };
const LIB_TYPE_LABEL = { film: 'Film', episode: 'Serie', anime: 'Anime' };
const LIB_ORIGIN = {
  download: { label: 'Scaricata',             cls: 'bg-secondary-lt' },
  matched:  { label: 'Riconosciuta per anno', cls: 'bg-yellow-lt' },
  manual:   { label: 'Impostata a mano',      cls: 'bg-blue-lt' },
};

let _libAll = [];
// What the modal is editing: {mode, association} for a correction, or
// {mode: 'create', key, title} for a new one. onSaved reloads whichever view
// opened it.
let _libModal = null;

function libIdLabel(source, id) {
  return `${LIB_SOURCE_PREFIX[source] || source} #${id}`;
}

// The title page speaks in route types; the registry in source + media type.
function libKeyForRoute(type, id) {
  if (type === 'anime') return { source: 'animeunity', media_type: 'anime', external_id: String(id) };
  if (type === 'tv') return { source: 'streamingcommunity', media_type: 'episode', external_id: String(id) };
  return { source: 'streamingcommunity', media_type: 'film', external_id: String(id) };
}

function _libOffsets(a) {
  const parts = [];
  if (a.season_offset) parts.push(`stagioni ${a.season_offset > 0 ? '+' : ''}${a.season_offset}`);
  if (a.episode_offset) parts.push(`episodi ${a.episode_offset > 0 ? '+' : ''}${a.episode_offset}`);
  return parts.join(' · ');
}

function _libMissingLabel(a) {
  if (a.exists !== false) return '';
  const since = a.missing_since ? new Date(a.missing_since) : null;
  const when = since && !isNaN(since) ? ` dal ${since.toLocaleDateString('it-IT')}` : '';
  return `<span class="badge bg-red-lt" title="La cartella non esiste più con questo nome">
            <i class="ti ti-folder-off me-1"></i>Mancante${when}</span>`;
}

// ── Settings tab ──────────────────────────────────────────────────────────────

async function loadLibraryAssociations() {
  try {
    const data = await api.get('/api/library/associations');
    _libAll = data.associations || [];
  } catch (e) {
    _feedback('lib-feedback', errText(e, 'Impossibile leggere le associazioni.'), 'danger');
    return;
  }
  renderLibraryAssociations();
}

function _libRow(a) {
  const origin = LIB_ORIGIN[a.origin] || { label: a.origin, cls: 'bg-secondary-lt' };
  const offsets = _libOffsets(a);
  return `
    <div class="lib-row">
      <div class="lib-row-main">
        <div class="lib-row-title">
          <span class="lib-row-name">${escapeHtml(a.title || 'Titolo senza nome')}</span>
          <span class="lib-id">${escapeHtml(libIdLabel(a.source, a.external_id))}</span>
          <span class="lib-type">${escapeHtml(LIB_TYPE_LABEL[a.media_type] || a.media_type)}</span>
        </div>
        <div class="lib-row-folder">
          <i class="ti ti-folder"></i><span class="text-truncate">${escapeHtml(a.folder)}</span>
          ${offsets ? `<span class="lib-offsets">${escapeHtml(offsets)}</span>` : ''}
          ${_libMissingLabel(a)}
        </div>
      </div>
      <span class="badge ${origin.cls} lib-origin">${escapeHtml(origin.label)}</span>
      <div class="lib-row-actions">
        <button class="btn btn-sm btn-outline-secondary" data-action="lib:edit" data-id="${a.id}"
                title="Modifica associazione"><i class="ti ti-pencil"></i></button>
        <button class="btn btn-sm btn-outline-danger" data-action="lib:remove" data-id="${a.id}"
                title="Rimuovi associazione"><i class="ti ti-trash"></i></button>
      </div>
    </div>`;
}

function _libRejectedRow(a) {
  return `
    <div class="lib-row lib-row-rejected">
      <div class="lib-row-main">
        <div class="lib-row-title">
          <span class="lib-row-name">${escapeHtml(a.title || 'Titolo senza nome')}</span>
          <span class="lib-id">${escapeHtml(libIdLabel(a.source, a.external_id))}</span>
        </div>
        <div class="lib-row-folder">
          <i class="ti ti-folder-x"></i><span class="text-truncate">${escapeHtml(a.folder)}</span>
        </div>
      </div>
      <div class="lib-row-actions">
        <button class="btn btn-sm btn-outline-secondary" data-action="lib:restore" data-id="${a.id}">
          <i class="ti ti-arrow-back-up me-1"></i>Ripristina
        </button>
      </div>
    </div>`;
}

function renderLibraryAssociations() {
  const list = document.getElementById('lib-list');
  if (!list) return;
  const filter = document.getElementById('lib-filter')?.value || 'all';
  const q = (document.getElementById('lib-search')?.value || '').trim().toLowerCase();
  const active = _libAll.filter(a => a.origin !== 'rejected');
  const shown = active.filter(a => {
    if (filter === 'matched' && a.origin !== 'matched') return false;
    if (filter === 'manual' && a.origin !== 'manual') return false;
    if (filter === 'missing' && a.exists !== false) return false;
    if (q && !`${a.title || ''} ${a.folder} ${a.external_id}`.toLowerCase().includes(q)) return false;
    return true;
  });

  if (!active.length) {
    list.innerHTML = `<p class="text-muted small mb-0">
      Nessuna associazione ancora: il pannello le crea a fine download.</p>`;
  } else if (!shown.length) {
    list.innerHTML = '<p class="text-muted small mb-0">Nessuna associazione corrisponde al filtro.</p>';
  } else {
    list.innerHTML = shown.map(_libRow).join('');
  }

  const rejected = _libAll.filter(a => a.origin === 'rejected');
  const wrap = document.getElementById('lib-rejected-wrap');
  wrap.hidden = !rejected.length;
  document.getElementById('lib-rejected-count').textContent = rejected.length;
  document.getElementById('lib-rejected-list').innerHTML = rejected.map(_libRejectedRow).join('');
}

async function libRemove(id) {
  const a = _libAll.find(x => x.id === id) || _libTitleRows.find(x => x.id === id);
  if (!a) return;
  const ok = await scConfirm(
    `Rimuovere l'associazione tra «${a.title || libIdLabel(a.source, a.external_id)}» e la cartella «${a.folder}»? ` +
    'Il pannello smette di cercarci questo titolo e non la riproporrà da solo. I file restano dove sono.');
  if (!ok) return;
  try {
    await api.del(`/api/library/associations/${id}`);
    showToast('Associazione rimossa', 'success');
    _libReloadViews();
  } catch (e) { showToast(errText(e), 'danger'); }
}

async function libRestore(id) {
  try {
    await api.post(`/api/library/associations/${id}/restore`);
    showToast('Associazione ripristinata', 'success');
    _libReloadViews();
  } catch (e) { showToast(errText(e), 'danger'); }
}

async function libReconcile() {
  const btn = document.getElementById('lib-reconcile-btn');
  btn.disabled = true;
  _feedback('lib-feedback', 'Verifica in corso...');
  try {
    const s = await api.post('/api/library/reconcile');
    const parts = [];
    if (s.renamed) parts.push(`${s.renamed} ritrovate con un altro nome`);
    if (s.missing) parts.push(`${s.missing} mancanti`);
    if (s.forgotten) parts.push(`${s.forgotten} dimenticate dopo un mese`);
    _feedback('lib-feedback', parts.length ? `Fatto: ${parts.join(', ')}.` : 'Tutte le cartelle sono al loro posto.', 'success');
    await loadLibraryAssociations();
  } catch (e) {
    _feedback('lib-feedback', errText(e), 'danger');
  } finally { btn.disabled = false; }
}

// ── Title page card ───────────────────────────────────────────────────────────

let _libTitleRows = [];
let _libTitleKey = null;

async function libRenderTitleCard(route, title) {
  const card = document.getElementById('th-library-card');
  if (!card) return;
  if (!can('MANAGE_SETTINGS') || !route?.id) { card.hidden = true; return; }
  const key = libKeyForRoute(route.type, route.id);
  const stamp = `${key.source}/${key.media_type}/${key.external_id}`;
  _libTitleKey = { ...key, title, stamp };
  let rows;
  try {
    rows = (await api.get('/api/library/associations/title', key)).associations || [];
  } catch (e) { card.hidden = true; return; }
  // The page may have moved on to another title while this was in flight.
  if (_libTitleKey?.stamp !== stamp) return;
  _libTitleRows = rows;
  card.hidden = false;

  const list = rows.map(a => {
    const origin = LIB_ORIGIN[a.origin] || { label: a.origin, cls: 'bg-secondary-lt' };
    const offsets = _libOffsets(a);
    return `
      <div class="lib-card-row">
        <i class="ti ti-folder"></i>
        <div class="lib-card-main">
          <div class="text-truncate" title="${escapeHtml(a.folder)}">${escapeHtml(a.folder)}</div>
          <div class="lib-card-meta">
            <span class="badge ${origin.cls}">${escapeHtml(origin.label)}</span>
            ${offsets ? `<span class="lib-offsets">${escapeHtml(offsets)}</span>` : ''}
            ${_libMissingLabel(a)}
          </div>
        </div>
        <button class="btn btn-sm btn-ghost-secondary" data-action="lib:edit" data-id="${a.id}"
                title="Modifica associazione"><i class="ti ti-pencil"></i></button>
      </div>`;
  }).join('');

  document.getElementById('th-library').innerHTML = `
    ${list || '<p class="th-empty mb-2">Nessuna cartella associata.</p>'}
    <button class="btn btn-sm btn-outline-secondary w-100 mt-1" data-action="lib:associate">
      <i class="ti ti-folder-plus me-1"></i>Associa cartella…
    </button>`;
}

// ── The modal ─────────────────────────────────────────────────────────────────

async function _libOpenModal(state) {
  _libModal = state;
  const a = state.association;
  const key = a || state.key;
  const isFilm = key.media_type === 'film';

  document.getElementById('lib-assoc-heading').textContent =
    state.mode === 'edit' ? 'Modifica associazione' : 'Associa cartella';
  document.getElementById('lib-assoc-subject').textContent =
    (a ? a.title : state.title) || libIdLabel(key.source, key.external_id);
  document.getElementById('lib-assoc-source').textContent = LIB_SOURCE_PREFIX[key.source] || key.source;
  document.getElementById('lib-assoc-id').value = key.external_id;
  document.getElementById('lib-assoc-season').value = a ? a.season_offset : 0;
  document.getElementById('lib-assoc-episode').value = a ? a.episode_offset : 0;
  // A film has no seasons or episodes to place.
  document.getElementById('lib-assoc-offsets').hidden = isFilm;
  document.getElementById('lib-assoc-feedback').textContent = '';

  const select = document.getElementById('lib-assoc-folder');
  select.innerHTML = '<option value="">Caricamento...</option>';
  select.disabled = true;
  showModal('lib-assoc-modal');

  let folders = [];
  try {
    folders = (await api.get('/api/library/folders', { media_type: key.media_type })).folders || [];
  } catch (e) {
    document.getElementById('lib-assoc-feedback').textContent = errText(e, 'Impossibile leggere la libreria.');
  }
  if (_libModal !== state) return;
  const current = a?.folder || '';
  const options = folders.map(f =>
    `<option value="${escapeHtml(f)}"${f === current ? ' selected' : ''}>${escapeHtml(f)}</option>`);
  // A folder that went missing stays visible as what it was, so saving only
  // new offsets does not also move the association somewhere else.
  if (current && !folders.includes(current)) {
    options.unshift(`<option value="${escapeHtml(current)}" selected>${escapeHtml(current)} (mancante)</option>`);
  }
  if (!current) options.unshift('<option value="" selected>Scegli una cartella…</option>');
  select.innerHTML = options.join('');
  select.disabled = false;
  libPreview();
}

function libPreview() {
  const el = document.getElementById('lib-assoc-preview');
  if (!_libModal || !el) return;
  const key = _libModal.association || _libModal.key;
  if (key.media_type === 'film') { el.textContent = ''; return; }
  const so = parseInt(document.getElementById('lib-assoc-season').value, 10) || 0;
  const eo = parseInt(document.getElementById('lib-assoc-episode').value, 10) || 0;
  const pad = n => String(n).padStart(2, '0');
  const folder = document.getElementById('lib-assoc-folder').value;
  const where = folder ? ` in «${folder}»` : '';
  if (1 + so < 0 || 1 + eo < 1) {
    el.textContent = 'Con questi valori il primo episodio non avrebbe un numero valido.';
    return;
  }
  el.textContent = key.media_type === 'anime'
    ? `L'episodio 1 diventa S${pad(1 + so)}E${pad(1 + eo)}${where}.`
    : `S01E01 diventa S${pad(1 + so)}E${pad(1 + eo)}${where}; le altre stagioni si spostano allo stesso modo.`;
}

async function libSave() {
  const state = _libModal;
  if (!state) return;
  const fb = document.getElementById('lib-assoc-feedback');
  const externalId = document.getElementById('lib-assoc-id').value.trim();
  const folder = document.getElementById('lib-assoc-folder').value;
  const so = parseInt(document.getElementById('lib-assoc-season').value, 10) || 0;
  const eo = parseInt(document.getElementById('lib-assoc-episode').value, 10) || 0;
  if (!externalId) { fb.textContent = "Serve l'ID del titolo."; return; }
  if (!folder) { fb.textContent = 'Scegli una cartella.'; return; }

  const btn = document.getElementById('lib-assoc-save');
  btn.disabled = true;
  fb.textContent = '';
  try {
    if (state.mode === 'edit') {
      const a = state.association;
      const body = {};
      if (externalId !== a.external_id) body.external_id = externalId;
      if (folder !== a.folder) body.folder = folder;
      if (so !== a.season_offset) body.season_offset = so;
      if (eo !== a.episode_offset) body.episode_offset = eo;
      if (!Object.keys(body).length) { hideModal('lib-assoc-modal'); return; }
      await api.patch(`/api/library/associations/${a.id}`, body);
    } else {
      await api.post('/api/library/associations', {
        ...state.key, external_id: externalId, folder, title: state.title || null,
        season_offset: so, episode_offset: eo,
      });
    }
    hideModal('lib-assoc-modal');
    showToast('Associazione salvata', 'success');
    _libReloadViews();
  } catch (e) {
    fb.textContent = errText(e);
  } finally { btn.disabled = false; }
}

function libEdit(id) {
  const a = _libAll.find(x => x.id === id) || _libTitleRows.find(x => x.id === id);
  if (a) _libOpenModal({ mode: 'edit', association: a });
}

function libAssociate() {
  if (!_libTitleKey) return;
  const { stamp, title, ...key } = _libTitleKey;
  // The name may have arrived with the metadata after the card was drawn.
  _libOpenModal({ mode: 'create', key, title: (typeof _tp !== 'undefined' && _tp?.name) || title });
}

// Whichever view is on screen reloads; the other refetches when next opened.
function _libReloadViews() {
  const visible = id => document.getElementById(id)?.style.display !== 'none';
  if (visible('page-settings')) loadLibraryAssociations();
  else _settingsLoaded.delete('associazioni');
  if (visible('page-detail') && _libTitleKey && _tp) libRenderTitleCard(_tp, _libTitleKey.title);
}

registerActions({
  'lib:filter':    () => renderLibraryAssociations(),
  'lib:reconcile': () => libReconcile(),
  'lib:edit':      d => libEdit(Number(d.id)),
  'lib:remove':    d => libRemove(Number(d.id)),
  'lib:restore':   d => libRestore(Number(d.id)),
  'lib:associate': () => libAssociate(),
  'lib:preview':   () => libPreview(),
  'lib:save':      () => libSave(),
});
