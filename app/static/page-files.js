/* StreamingCommunity Web Panel — page-files.js */
//
// The file manager: the library tree, search, drag-and-drop moves, and the
// built-in player.

// ── File Manager ───────────────────────────────────────────────────────────────

let _expandedFolders = new Set();
let _cachedTree = null;
let _selectedPaths = new Set();
let _draggedPaths = [];
let _allVisiblePaths = [];  // flat list of visible paths for shift-click range
let _lastSelectedIndex = -1;
let _fmSearchActive = false;
let _fmSearchTimeout = null;

function setupFileManager() {
  // ── Drag & Drop (supports multi-drag) ──
  document.addEventListener('dragstart', (e) => {
    const row = e.target.closest('[data-drag-path]');
    if (!row) return;
    const path = row.dataset.dragPath;
    // If dragged item is selected, drag all selected; otherwise just the one
    if (_selectedPaths.has(path) && _selectedPaths.size > 1) {
      _draggedPaths = [..._selectedPaths];
    } else {
      _draggedPaths = [path];
    }
    e.dataTransfer.effectAllowed='move';
    e.dataTransfer.setData('text/plain', _draggedPaths.join('\n'));
    // Visual: mark all dragged rows
    _draggedPaths.forEach(p => {
      const el = document.querySelector(`[data-drag-path="${CSS.escape(p)}"]`);
      if (el) el.classList.add('dragging');
    });
  });
  document.addEventListener('dragend', () => {
    document.querySelectorAll('.dragging').forEach(el=>el.classList.remove('dragging'));
    document.querySelectorAll('.drag-over').forEach(el=>el.classList.remove('drag-over'));
    _draggedPaths=[];
  });
  document.addEventListener('dragover', (e) => {
    if (!e.target.closest('.fm-drop-zone')) return;
    e.preventDefault(); e.dataTransfer.dropEffect='move';
  });
  document.addEventListener('dragenter', (e) => {
    const zone = e.target.closest('.fm-drop-zone');
    if (!zone || !_draggedPaths.length) return;
    const dest = zone.dataset.dropPath;
    // Prevent dropping into any of the dragged items
    if (_draggedPaths.some(p => dest===p || dest.startsWith(p+'/'))) return;
    e.preventDefault();
    document.querySelectorAll('.drag-over').forEach(el=>el.classList.remove('drag-over'));
    zone.classList.add('drag-over');
  });
  document.addEventListener('dragleave', (e) => {
    const zone = e.target.closest('.fm-drop-zone');
    if (zone && !zone.contains(e.relatedTarget)) zone.classList.remove('drag-over');
  });
  document.addEventListener('drop', (e) => {
    const zone = e.target.closest('.fm-drop-zone');
    if (!zone) return;
    e.preventDefault(); zone.classList.remove('drag-over');
    const destDirPath = zone.dataset.dropPath;
    if (!_draggedPaths.length||destDirPath===undefined) return;
    if (_draggedPaths.some(p => destDirPath===p||destDirPath.startsWith(p+'/'))) return;
    if (_draggedPaths.length > 1) {
      batchMoveToPath(_draggedPaths, destDirPath);
    } else {
      const name = _draggedPaths[0].split(/[/\\]/).pop();
      moveToPath(_draggedPaths[0], name, destDirPath);
    }
    _draggedPaths=[];
  });

  // ── Click handlers ──
  document.addEventListener('click', (e) => {
    // Checkbox toggle
    const check = e.target.closest('.fm-check');
    if (check) {
      e.stopPropagation();
      const path = check.dataset.selectPath;
      const idx = _allVisiblePaths.indexOf(path);
      if (e.shiftKey && _lastSelectedIndex >= 0 && idx >= 0) {
        // Shift-click: range select
        const start = Math.min(_lastSelectedIndex, idx);
        const end = Math.max(_lastSelectedIndex, idx);
        for (let i = start; i <= end; i++) {
          _selectedPaths.add(_allVisiblePaths[i]);
        }
      } else {
        if (_selectedPaths.has(path)) _selectedPaths.delete(path);
        else _selectedPaths.add(path);
      }
      if (idx >= 0) _lastSelectedIndex = idx;
      syncSelectionUI();
      return;
    }

    // Folder toggle
    const toggle = e.target.closest('.fm-toggle');
    if (toggle) {
      const path = toggle.dataset.folderPath;
      if (_expandedFolders.has(path)) _expandedFolders.delete(path);
      else _expandedFolders.add(path);
      if (_cachedTree) renderFileTree(_cachedTree);
      return;
    }
    const renameBtn = e.target.closest('[data-rename-path]');
    if (renameBtn && renameBtn.closest('#files-left-pane')) {
      renamePath(renameBtn.dataset.renamePath, renameBtn.dataset.renameName); return;
    }
    const delBtn = e.target.closest('[data-delete-path]');
    if (delBtn && delBtn.closest('#files-left-pane')) {
      deletePath(delBtn.dataset.deletePath, delBtn.dataset.deleteName, !!delBtn.dataset.deleteDir); return;
    }
    const playBtn = e.target.closest('[data-play-path]');
    if (playBtn) playFile(playBtn.dataset.playPath, playBtn.dataset.playName);
  });

  // ── Batch toolbar buttons ──
  const batchMoveBtn = document.getElementById('fm-batch-move-btn');
  if (batchMoveBtn) batchMoveBtn.addEventListener('click', async () => {
    if (!_selectedPaths.size) return;
    const dest = await scPrompt('Percorso cartella di destinazione (vuoto = radice):','');
    if (dest === null) return;
    batchMoveToPath([..._selectedPaths], dest);
  });
  const batchDeleteBtn = document.getElementById('fm-batch-delete-btn');
  if (batchDeleteBtn) batchDeleteBtn.addEventListener('click', async () => {
    if (!_selectedPaths.size) return;
    if (!await scConfirm(`Eliminare ${_selectedPaths.size} elementi selezionati?`)) return;
    batchDeletePaths([..._selectedPaths]);
  });
  const deselectBtn = document.getElementById('fm-deselect-btn');
  if (deselectBtn) deselectBtn.addEventListener('click', () => {
    _selectedPaths.clear();
    _lastSelectedIndex = -1;
    syncSelectionUI();
  });
}

function syncSelectionUI() {
  // Update checkboxes and row highlights
  document.querySelectorAll('.fm-check').forEach(cb => {
    const path = cb.dataset.selectPath;
    cb.checked = _selectedPaths.has(path);
    const row = cb.closest('.fm-row');
    if (row) row.classList.toggle('fm-selected', _selectedPaths.has(path));
  });
  // Update toolbar
  const bar = document.getElementById('fm-selection-bar');
  const count = document.getElementById('fm-selection-count');
  if (bar) bar.style.visibility = _selectedPaths.size ? '' : 'hidden';
  if (count) count.textContent = `${_selectedPaths.size} selezionat${_selectedPaths.size===1?'o':'i'}`;
}

function onFmSearchInput(value) {
  const clearBtn = document.getElementById('fm-search-clear');
  if (clearBtn) clearBtn.style.display = value ? '' : 'none';
  syncHash();
  clearTimeout(_fmSearchTimeout);
  if (!value || value.trim().length < 2) {
    if (_fmSearchActive) {
      _fmSearchActive = false;
      if (_cachedTree) renderFileTree(_cachedTree);
      else loadFiles();
    }
    return;
  }
  _fmSearchTimeout = setTimeout(() => searchFiles(value.trim()), 300);
}

function clearFmSearch() {
  const input = document.getElementById('fm-search-input');
  if (input) input.value = '';
  syncHash();
  const clearBtn = document.getElementById('fm-search-clear');
  if (clearBtn) clearBtn.style.display = 'none';
  _fmSearchActive = false;
  if (_cachedTree) renderFileTree(_cachedTree);
  else loadFiles();
}

async function searchFiles(query) {
  const pane = document.getElementById('files-left-pane');
  if (!pane) return;
  _fmSearchActive = true;
  pane.innerHTML = '<div class="text-center py-4 text-muted" style="font-size:13px"><div class="spinner-border spinner-border-sm me-2"></div>Ricerca...</div>';
  try {
    renderSearchResults(await api.get('/api/files/search', {q: query}), query);
  } catch(e) {
    pane.innerHTML = `<div class="text-danger text-center py-4 px-3">${escapeHtml(errText(e, 'Errore ricerca'))}</div>`;
  }
}

function renderSearchResults(results, query) {
  const pane = document.getElementById('files-left-pane');
  if (!pane) return;
  _allVisiblePaths = [];

  if (!results || !results.length) {
    pane.innerHTML = `<div class="text-muted text-center py-5" style="font-size:13px">
      <i class="ti ti-search-off" style="font-size:2em;display:block;margin-bottom:8px;opacity:.4"></i>
      Nessun risultato per <strong>${escapeHtml(query)}</strong>
    </div>`;
    return;
  }

  const frag = document.createDocumentFragment();
  results.forEach(item => {
    _allVisiblePaths.push(item.path);
    const row = document.createElement('div');
    row.className = 'fm-row';
    if (_selectedPaths.has(item.path)) row.classList.add('fm-selected');
    row.style.paddingLeft = '10px';
    row.setAttribute('draggable', 'true');
    row.dataset.dragPath = item.path;
    const checked = _selectedPaths.has(item.path) ? 'checked' : '';
    const parentPath = item.path.includes('/') ? item.path.substring(0, item.path.lastIndexOf('/')) : '';
    const pathMeta = parentPath
      ? `<span class="fm-meta" style="font-size:11px;opacity:.55;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(parentPath)}">${escapeHtml(parentPath)}</span>`
      : '';
    if (item.type === 'directory') {
      row.classList.add('fm-drop-zone');
      row.dataset.dropPath = item.path;
      row.innerHTML = `
        <input type="checkbox" class="fm-check" data-select-path="${escapeHtml(item.path)}" ${checked}>
        <i class="ti ti-folder-filled text-yellow" style="flex-shrink:0"></i>
        <span class="fm-name">${escapeHtml(item.name)}</span>
        ${pathMeta}
        <div class="fm-actions">
          <button class="btn btn-sm btn-outline-secondary" data-rename-path="${escapeHtml(item.path)}" data-rename-name="${escapeHtml(item.name)}"><i class="ti ti-pencil"></i></button>
          <button class="btn btn-sm btn-outline-danger" data-delete-path="${escapeHtml(item.path)}" data-delete-name="${escapeHtml(item.name)}" data-delete-dir="1"><i class="ti ti-trash"></i></button>
        </div>`;
    } else {
      const size = formatSize(item.size);
      const isMp4 = item.name.toLowerCase().endsWith('.mp4');
      row.innerHTML = `
        <input type="checkbox" class="fm-check" data-select-path="${escapeHtml(item.path)}" ${checked}>
        <i class="ti ${isMp4 ? 'ti-file-type-mp4 text-red' : 'ti-file text-muted'}" style="flex-shrink:0"></i>
        <span class="fm-name">${escapeHtml(item.name)}</span>
        ${pathMeta}
        <span class="fm-meta">${size}</span>
        <div class="fm-actions">
          ${isMp4 ? `<button class="btn btn-sm btn-outline-primary" data-play-path="${escapeHtml(item.path)}" data-play-name="${escapeHtml(item.name)}"><i class="ti ti-player-play"></i></button>` : ''}
          <button class="btn btn-sm btn-outline-secondary" data-rename-path="${escapeHtml(item.path)}" data-rename-name="${escapeHtml(item.name)}"><i class="ti ti-pencil"></i></button>
          <a class="btn btn-sm btn-outline-secondary" href="/api/files/download/${encodeURI(item.path)}"><i class="ti ti-download"></i></a>
          <button class="btn btn-sm btn-outline-danger" data-delete-path="${escapeHtml(item.path)}" data-delete-name="${escapeHtml(item.name)}"><i class="ti ti-trash"></i></button>
        </div>`;
    }
    frag.appendChild(row);
  });

  const header = document.createElement('div');
  header.style.cssText = 'padding:6px 12px 5px;font-size:11px;color:var(--text-dim);border-bottom:1px solid var(--border)';
  header.textContent = `${results.length} risultat${results.length === 1 ? 'o' : 'i'} per "${query}"`;
  pane.innerHTML = '';
  pane.appendChild(header);
  pane.appendChild(frag);

  for (const p of _selectedPaths) {
    if (!_allVisiblePaths.includes(p)) _selectedPaths.delete(p);
  }
  syncSelectionUI();
}

async function loadFiles() {
  const pane = document.getElementById('files-left-pane');
  if (!pane) return;
  // Not awaited: the free-space readout is an aside, and stat'ing a sleeping
  // NFS mount must not hold up the file list.
  loadDiskUsage();
  // If search is active, refresh search results instead of reloading the tree
  const searchInput = document.getElementById('fm-search-input');
  if (_fmSearchActive && searchInput && searchInput.value.trim().length >= 2) {
    return searchFiles(searchInput.value.trim());
  }
  // Show skeleton while loading
  if (!_cachedTree) {
    let skeletonHtml = '';
    for (let i = 0; i < 5; i++) skeletonHtml += `<div class="skeleton skeleton-row"></div>`;
    pane.innerHTML = skeletonHtml;
  }
  try {
    const tree = await api.get('/api/files');
    _cachedTree = tree;
    if (!tree||!tree.length) { pane.innerHTML='<div class="text-muted text-center py-4">Nessun file trovato</div>'; return; }
    renderFileTree(tree);
  } catch(e) {
    pane.innerHTML=`<div class="text-danger text-center py-4">${escapeHtml(errText(e))}</div>`;
  }
}

function renderFileTree(tree) {
  const pane = document.getElementById('files-left-pane');
  if (!pane) return;
  _allVisiblePaths = [];
  const frag = document.createDocumentFragment();
  const rootZone = document.createElement('div');
  rootZone.className='fm-row fm-drop-zone fm-root-zone';
  rootZone.dataset.dropPath='';
  rootZone.innerHTML=`<span style="min-width:14px;flex-shrink:0"></span>
    <i class="ti ti-home text-muted" style="flex-shrink:0"></i>
    <span class="fm-meta ms-1">radice</span>`;
  frag.appendChild(rootZone);
  renderTreeItems(tree, frag, 0);
  pane.innerHTML='';
  pane.appendChild(frag);
  // Clean stale selections (paths no longer visible)
  for (const p of _selectedPaths) {
    if (!_allVisiblePaths.includes(p)) _selectedPaths.delete(p);
  }
  syncSelectionUI();
}

function renderTreeItems(items, container, depth) {
  items.forEach(item => {
    _allVisiblePaths.push(item.path);
    const row = document.createElement('div');
    row.className='fm-row';
    if (_selectedPaths.has(item.path)) row.classList.add('fm-selected');
    row.style.paddingLeft=`${8+depth*16}px`;
    row.setAttribute('draggable','true');
    row.dataset.dragPath=item.path;
    const checked = _selectedPaths.has(item.path) ? 'checked' : '';
    if (item.type==='directory') {
      const expanded = _expandedFolders.has(item.path);
      row.classList.add('fm-drop-zone');
      row.dataset.dropPath=item.path;
      const actions = `
        <div class="fm-actions">
          <button class="btn btn-sm btn-outline-secondary" data-rename-path="${escapeHtml(item.path)}" data-rename-name="${escapeHtml(item.name)}"><i class="ti ti-pencil"></i></button>
          <button class="btn btn-sm btn-outline-danger"
                  data-delete-path="${escapeHtml(item.path)}"
                  data-delete-name="${escapeHtml(item.name)}"
                  data-delete-dir="1"><i class="ti ti-trash"></i></button>
        </div>`;
      if (item.empty) {
        row.innerHTML=`
          <input type="checkbox" class="fm-check" data-select-path="${escapeHtml(item.path)}" ${checked}>
          <span style="min-width:22px;flex-shrink:0"></span>
          <i class="ti ti-folder text-muted" style="flex-shrink:0;opacity:0.45"></i>
          <span class="fm-name text-muted">${escapeHtml(item.name)}</span>
          ${actions}`;
        container.appendChild(row);
      } else {
        row.innerHTML=`
          <input type="checkbox" class="fm-check" data-select-path="${escapeHtml(item.path)}" ${checked}>
          <i class="ti ${expanded?'ti-chevron-down':'ti-chevron-right'} text-muted fm-toggle"
             data-folder-path="${escapeHtml(item.path)}"
             style="font-size:1em;cursor:pointer;min-width:22px;flex-shrink:0;padding:4px 3px;margin:-4px -3px"></i>
          <i class="ti ti-folder-filled text-yellow" style="flex-shrink:0"></i>
          <span class="fm-name">${escapeHtml(item.name)}</span>
          ${actions}`;
        container.appendChild(row);
        if (expanded && item.children) renderTreeItems(item.children, container, depth+1);
      }
    } else {
      const size = formatSize(item.size);
      const isMp4 = item.name.toLowerCase().endsWith('.mp4');
      row.innerHTML=`
        <input type="checkbox" class="fm-check" data-select-path="${escapeHtml(item.path)}" ${checked}>
        <span style="min-width:14px;flex-shrink:0"></span>
        <i class="ti ${isMp4?'ti-file-type-mp4 text-red':'ti-file text-muted'}" style="flex-shrink:0"></i>
        <span class="fm-name">${escapeHtml(item.name)}</span>
        <span class="fm-meta">${size}</span>
        <div class="fm-actions">
          ${isMp4?`<button class="btn btn-sm btn-outline-primary" data-play-path="${escapeHtml(item.path)}" data-play-name="${escapeHtml(item.name)}"><i class="ti ti-player-play"></i></button>`:''}
          <button class="btn btn-sm btn-outline-secondary" data-rename-path="${escapeHtml(item.path)}" data-rename-name="${escapeHtml(item.name)}"><i class="ti ti-pencil"></i></button>
          <a class="btn btn-sm btn-outline-secondary" href="/api/files/download/${encodeURI(item.path)}"><i class="ti ti-download"></i></a>
          <button class="btn btn-sm btn-outline-danger" data-delete-path="${escapeHtml(item.path)}" data-delete-name="${escapeHtml(item.name)}"><i class="ti ti-trash"></i></button>
        </div>`;
      container.appendChild(row);
    }
  });
}

async function moveToPath(sourcePath, name, destDirPath) {
  try {
    await api.post('/api/files/move', {path:sourcePath, dest_dir_path:destDirPath});
    showToast(`Spostato: ${name}`,'success');
    loadFiles();
  } catch(e) { showToast(errText(e, 'Errore spostamento'),'danger'); }
}

async function batchMoveToPath(paths, destDirPath) {
  try {
    const data = await api.post('/api/files/move-batch', {paths, dest_dir_path:destDirPath});
    const ok = data.results.filter(r=>r.ok).length;
    const fail = data.results.filter(r=>!r.ok).length;
    if (ok) showToast(`${ok} file spostati`,'success');
    if (fail) showToast(`${fail} file non spostati`,'danger');
    _selectedPaths.clear();
    loadFiles();
  } catch(e) { showToast('Errore di rete','danger'); }
}

async function batchDeletePaths(paths) {
  try {
    const data = await api.post('/api/files/delete-batch', {paths});
    const ok = data.results.filter(r=>r.ok).length;
    const fail = data.results.filter(r=>!r.ok).length;
    if (ok) showToast(`${ok} file eliminati`,'success');
    if (fail) showToast(`${fail} file non eliminati`,'danger');
    _selectedPaths.clear();
    loadFiles();
  } catch(e) { showToast('Errore di rete','danger'); }
}

function playFile(path, name) {
  document.getElementById('player-modal-title').textContent=name;
  const video = document.getElementById('video-player');
  video.src=`/api/files/stream/${encodeURI(path)}`; video.load();
  showModal('player-modal');
  document.getElementById('player-modal').addEventListener('click', (e) => {
    if (e.target.closest('[data-bs-dismiss="modal"]')) { video.pause(); video.src=''; }
  }, {once:true});
}

async function renamePath(path, name) {
  const newName = await scPrompt(`Nuovo nome:`, name);
  if (!newName || newName === name) return;
  try {
    await api.post('/api/files/rename', { path, new_name: newName });
    showToast(`Rinominato in: ${newName}`, 'success');
    loadFiles();
  } catch(e) { showToast(errText(e, 'Errore rinomina'), 'danger'); }
}

async function deletePath(path, name, isDir) {
  const msg = isDir ? `Eliminare la cartella "${name}" e tutto il suo contenuto?` : `Eliminare il file "${name}"?`;
  if (!await scConfirm(msg)) return;
  try {
    await api.del(`/api/files/delete/${encodeURI(path)}`);
    showToast(`Eliminato: ${name}`,'success');
    loadFiles();
  } catch(e) { showToast(errText(e, 'Errore eliminazione'),'danger'); }
}


// ── Spazio sul volume ────────────────────────────────────────────────────────
//
// Reported per volume, not per library: the three libraries are almost always
// folders on one mount, and three identical bars said nothing. Where they
// genuinely differ, the fullest is the one shown — it is the one that will
// stop a download.
//
// This was a line of text in a card header. It is the single fact on this page
// that can stop the panel working, so it gets a bar, and the bar only takes a
// colour once the number means something.

async function loadDiskUsage() {
  const box = document.getElementById('fm-disk-usage');
  if (!box) return;
  try {
    renderDiskUsage(await api.get('/api/files/disk-usage'));
  } catch (e) { box.hidden = true; }
}

function renderDiskUsage(data) {
  const box = document.getElementById('fm-disk-usage');
  if (!box) return;
  const volumes = ((data && data.volumes) || []).filter(v => v.total > 0);
  if (!volumes.length) { box.hidden = true; return; }

  const worst = volumes.reduce((a, b) => (a.used / a.total >= b.used / b.total ? a : b));
  const pct = Math.min(100, Math.round(worst.used / worst.total * 100));
  box.hidden = false;
  const fill = document.getElementById('fm-disk-fill');
  fill.className = diskLevel(pct);
  fill.style.width = `${pct}%`;
  document.getElementById('fm-disk-free').textContent = fmtBytes(worst.free);
  document.getElementById('fm-disk-total').textContent =
    `di ${fmtBytes(worst.total)}${volumes.length > 1 ? ` · volume più pieno di ${volumes.length}` : ''}`;
  box.title = `${pct}% occupato — ${(worst.paths || []).join(', ')}`;
}


// ── Delegated handlers ───────────────────────────────────────────────────────

registerActions({
  'files:reload':      () => loadFiles(),
  'files:search':      (d, el) => onFmSearchInput(el.value),
  'files:clearSearch': () => clearFmSearch(),
});


// The search term is in the address; which folders are unfolded is not. The
// tree is rebuilt from the server on every entry, and a link carrying a dozen
// directory paths would be longer than the page.
registerPageHash('files', {
  read: () => {
    const value = document.getElementById('fm-search-input')?.value.trim() || '';
    return { params: { q: value.length >= 2 ? value : null } };
  },
  apply: params => {
    const input = document.getElementById('fm-search-input');
    if (input) input.value = params.q || '';
    const clearBtn = document.getElementById('fm-search-clear');
    if (clearBtn) clearBtn.style.display = params.q ? '' : 'none';
    // loadFiles() checks this flag and runs the search instead of the tree.
    _fmSearchActive = !!params.q;
  },
});
