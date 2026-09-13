/* StreamingCommunity Web Panel — page-downloads.js */
//
// The downloads page, and the SSE stream that feeds it. The phase
// vocabulary lives here because this is the only place that renders it;
// tests/test_assets.py checks the tables against what the server emits,
// wherever they are.

// ── Global SSE stream ──────────────────────────────────────────────────────────

function connectGlobalStream() {
  const es = new EventSource('/api/progress/stream');

  es.onopen = () => {
    document.getElementById('stream-label').textContent='Live';
    document.querySelector('.stream-dot').style.background='#2fb344';
  };

  es.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    switch (msg.type) {
      case 'snapshot':
        _jobs.clear();
        msg.jobs.forEach(j => _jobs.set(j.job_id, j));
        renderAllJobCards();
        updateActiveBadge();
        break;
      case 'job_created':
        _jobs.set(msg.job.job_id, msg.job);
        addJobCard(msg.job);
        updateActiveBadge();
        break;
      case 'job_status':
        if (_jobs.has(msg.job_id)) {
          _jobs.get(msg.job_id).status = msg.status;
          refreshCardAppearance(msg.job_id);
          updateActiveBadge();
        }
        break;
      case 'progress':
        handleProgressEvent(msg);
        break;
      case 'status':
        handlePhaseEvent(msg.job_id, msg.phase);
        break;
      case 'done':
        handleDoneEvent(msg.job_id, msg.output_path);
        break;
      case 'error':
        handleErrorEvent(msg.job_id, msg.message);
        break;
      case 'job_dismissed':
        _jobs.delete(msg.job_id);
        renderAllJobCards();
        updateActiveBadge();
        break;
      case 'domain_candidate':
        loadDomainCandidate();
        break;
      case 'notification':
        // A bare signal: the payload lives behind /api/notifications, which is
        // scoped to the caller, so a shared stream leaks nothing.
        refreshNotifications();
        refreshQueueBadge();
        if (can('MANAGE_REQUESTS') &&
            document.getElementById('page-requests').style.display !== 'none') {
          loadRequestQueue();
        }
        break;
    }
  };

  es.onerror = () => {
    es.close();
    document.getElementById('stream-label').textContent='Riconnessione...';
    document.querySelector('.stream-dot').style.background='#d63939';
    setTimeout(connectGlobalStream, 3000);
  };
}

// ── Job cards ──────────────────────────────────────────────────────────────────

// Every phase app/jobs.py::_compute_phases can emit needs an entry in all four
// of these. "video" had none, so the moment a card was rebuilt - switching page
// and back - the running job fell through to the grey fallback and lost its
// colour, its border and its label. tests/test_assets.py now checks the set.
const PHASE_LABELS = {
  scheduled:'Programmato', queued:'In coda', running:'In corso',
  video:'Video', joining:'Finalizzazione',
  audio:'Audio', merging:'Unione', done:'Completato', error:'Errore', cancelled:'Annullato',
};
const PHASE_BADGE = {
  scheduled:'bg-yellow-lt', queued:'bg-secondary-lt', running:'bg-blue-lt',
  video:'bg-blue-lt', joining:'bg-yellow-lt',
  audio:'bg-teal-lt', merging:'bg-purple-lt', done:'bg-success-lt',
  error:'bg-danger-lt', cancelled:'bg-secondary-lt',
};
const PHASE_BAR = {
  running:'bg-blue', video:'bg-blue', joining:'phase-bar-joining bg-warning',
  audio:'phase-bar-audio bg-teal', merging:'phase-bar-merging bg-purple',
  done:'phase-bar-done bg-success', error:'phase-bar-error bg-danger',
};
const PHASE_BORDER_MAP = {
  scheduled:'var(--yellow)', queued:'var(--text-dim)', running:'var(--blue)',
  video:'var(--blue)', joining:'var(--yellow)',
  audio:'var(--teal)', merging:'var(--purple)', done:'var(--green)',
  error:'var(--danger)', cancelled:'var(--text-dim)',
};

// Short by design: these are cells on a rail, not sentences. An audio phase
// is named by its language alone - "ITA" reads faster than "Audio ITA" when
// there are three of them in a row.
function _stepLabel(phase) {
  const map = { video:'Video', joining:'Join', merging:'Merge', done:'Fine', audio:'Audio' };
  if (map[phase]) return map[phase];
  if (phase && phase.startsWith('audio_')) return 'Audio ' + phase.slice(6).toUpperCase();
  return phase;
}

function _buildStepsHtml(jobId, phases, currentPhase, status) {
  if (!phases || phases.length < 2) return '';
  let activeIdx;
  if (status === 'done') {
    activeIdx = phases.length;
  } else if (status === 'queued' || status === 'scheduled') {
    activeIdx = -1;
  } else {
    const lookup = currentPhase || 'video';
    activeIdx = phases.indexOf(lookup);
    if (activeIdx < 0) activeIdx = phases.indexOf('video') >= 0 ? 0 : -1;
  }
  const items = phases.map((p, i) => {
    let cls = 'jp';
    if (activeIdx === phases.length || i < activeIdx) cls += ' complete';
    else if (i === activeIdx) cls += ' active';
    return `<span class="${cls}" data-phase="${p}">${_stepLabel(p)}</span>`;
  }).join('');
  return `<div class="job-phases" id="job-steps-${jobId}">${items}</div>`;
}

function _updateSteps(jobId, phase) {
  const job = _jobs.get(jobId);
  if (!job || !job.phases || job.phases.length < 2) return;
  const container = document.getElementById(`job-steps-${jobId}`);
  if (!container) return;
  const phases = job.phases;
  const isDone = job.status === 'done';
  const activeIdx = isDone ? phases.length : phases.indexOf(phase || 'video');
  if (activeIdx < 0) return;
  container.querySelectorAll('.jp').forEach((el, i) => {
    el.className = 'jp';
    if (activeIdx === phases.length || i < activeIdx) el.className += ' complete';
    else if (i === activeIdx) el.className += ' active';
  });
}

function _phaseLabel(phase) {
  if (!phase) return '';
  if (PHASE_LABELS[phase]) return PHASE_LABELS[phase];
  if (phase.startsWith('audio_')) return 'Audio ' + phase.slice(6).toUpperCase();
  return phase;
}
function _phaseBadge(phase) {
  if (PHASE_BADGE[phase]) return PHASE_BADGE[phase];
  if (phase && phase.startsWith('audio_')) return 'bg-teal-lt';
  return 'bg-secondary-lt';
}
function _phaseBar(phase) {
  if (PHASE_BAR[phase]) return PHASE_BAR[phase];
  if (phase && phase.startsWith('audio_')) return 'phase-bar-audio bg-teal';
  return 'bg-secondary';
}
function _phaseBorder(phase) {
  if (PHASE_BORDER_MAP[phase]) return PHASE_BORDER_MAP[phase];
  if (phase && phase.startsWith('audio_')) return 'var(--teal)';
  return 'transparent';
}

// The line under the progress bar, built in one place.
//
// It used to be composed twice - once when the card was drawn and again on
// every progress frame - and the two disagreed: the rebuild showed the size,
// the live update replaced it with a percentage. So the size appeared for a
// moment and then vanished for the rest of the download.
//
// The total is extrapolated from the segments already fetched, so it wears a
// tilde for as long as it is a guess; below the server's sample threshold it
// is null and only what has actually arrived is claimed.
function _jobInfoText(progress, status) {
  if (!progress) return '';
  const active = status === 'running';
  const done = progress.bytes_done || 0;
  const expected = progress.bytes_total;

  let size = '';
  if (done > 0) {
    size = (status === 'done' || !expected)
      ? formatSize(done)
      : `${formatSize(done)} di ~${formatSize(expected)}`;
  }
  const speed = active
    ? (progress.bytes_speed > 0 ? formatSize(progress.bytes_speed) + '/s'
       : (progress.speed > 0 ? `${progress.speed} seg/s` : ''))
    : '';
  const eta = active && progress.eta ? fmtEta(progress.eta) : '';
  const pct = active && progress.pct ? `${progress.pct}%` : '';

  return [size, speed, eta, pct].filter(Boolean).join(' · ');
}

// Built in one place because the card renders them twice: once when it is
// drawn and again whenever its status changes under it.
function _fireBtnHtml(jobId) {
  return `<button class="btn btn-sm btn-outline-success ms-1" data-action="jobs:fire"
                  data-job="${jobId}" title="Lancia subito"><i class="ti ti-player-play"></i></button>`;
}
function _stopBtnHtml(jobId) {
  return `<button class="btn btn-sm btn-outline-danger ms-1" data-action="jobs:cancel"
                  data-job="${jobId}" title="Interrompi"><i class="ti ti-player-stop"></i></button>`;
}

function _buildJobCard(j) {
  const phase = _jobPhases[j.job_id] || j.status;
  const isActive = j.status==='running' || j.status==='queued' || j.status==='scheduled';
  const isMovie = j.type==='film';
  const isAnimeJob = j.type==='anime';
  const pct = j.progress?.pct||0;
  const barClass = _phaseBar(phase);
  const animated = isActive && j.status!=='queued' ? ' progress-bar-striped progress-bar-animated' : '';
  const barWidth = j.status==='queued' ? 0 : (j.status==='done' ? 100 : pct);
  const badgeClass = _phaseBadge(phase);
  const label = _phaseLabel(phase);
  const borderColor = _phaseBorder(phase);


  const infoStr = _jobInfoText(j.progress, j.status);
  const infoTitle = (j.progress?.bytes_total && j.status !== 'done')
    ? ' title="La playlist non dichiara una dimensione: il totale è stimato sui segmenti già scaricati."'
    : '';

  const fireBtn = j.status === 'scheduled' ? _fireBtnHtml(j.job_id) : '';
  const stopBtn = isActive ? _stopBtnHtml(j.job_id) : '';

  const rawTs = j.scheduled_at || j.created_at;
  const dateStr = rawTs
    ? new Date(/[Z+]/.test(rawTs)?rawTs:rawTs+'Z').toLocaleString('it-IT',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})
    : '';
  const dateLabel = j.scheduled_at ? `⏰ ${dateStr}` : dateStr;

  const stepsHtml = _buildStepsHtml(j.job_id, j.phases, phase, j.status);
  return `<div class="card mb-2 job-card${j.status==='done'?' is-done':''}${j.status==='error'?' is-error':''}" id="job-card-${j.job_id}" style="border-left:3px solid ${borderColor} !important">
    <div class="card-body py-2 px-3">
      <div class="d-flex align-items-center gap-2">
        <span class="badge ${isMovie?'bg-blue-lt':isAnimeJob?'bg-purple-lt':'bg-green-lt'} flex-shrink-0">${isMovie?'Film':isAnimeJob?'Anime':'TV'}</span>
        <span class="fw-medium text-truncate flex-1" style="min-width:0" title="${escapeHtml(j.title)}">${escapeHtml(j.title)}</span>
        <span class="badge ${badgeClass} flex-shrink-0" id="job-badge-${j.job_id}">${label}</span>
        <span id="job-fire-${j.job_id}">${fireBtn}</span>
        ${stopBtn ? `<span id="job-stop-${j.job_id}">${stopBtn}</span>` : `<span id="job-stop-${j.job_id}"></span>`}
      </div>
      ${stepsHtml}
      <div class="progress my-1" style="height:5px">
        <div class="progress-bar ${barClass}${animated} job-progress-bar" id="job-bar-${j.job_id}" style="width:${barWidth}%"></div>
      </div>
      <div class="d-flex justify-content-between align-items-center">
        <small class="text-muted" id="job-info-${j.job_id}"${infoTitle}>${infoStr || (j.status==='error' ? escapeHtml(j.error||'Errore') : (j.status==='done'?'Completato':''))}</small>
        <small class="text-muted">${dateLabel}</small>
      </div>
    </div>
  </div>`;
}

// ── The list: filter, then group ─────────────────────────────────────────────
//
// A season download submits one job per episode. Twenty-four identical-looking
// cards, one per episode, is the state the page spent most of its time in, and
// nothing in it said they were one thing the user had asked for. batch_label is
// composed by the server at submit time and carried on every job in the batch,
// which is what makes a heading possible at all: ``title`` is already a
// composed string ("Nome Serie S02E05") and reparsing a heading back out of it
// misreads the first title that contains something like S01 itself.

let _dlFilter = 'all';
const _dlCollapsed = new Set();   // batch_id of the groups the user folded away

function _jobBucket(j) {
  if (j.status === 'running' || j.status === 'queued' || j.status === 'scheduled') return 'active';
  if (j.status === 'done') return 'done';
  if (j.status === 'error') return 'error';
  return 'other';   // cancelled
}

function setDlFilter(filter) {
  _dlFilter = filter;
  document.querySelectorAll('#dl-filters .queue-filter').forEach(el =>
    el.classList.toggle('active', el.dataset.filter === filter));
  renderAllJobCards();
}

function _dlSorted(jobs) {
  return jobs.sort((a, b) => {
    const rank = j => _jobBucket(j) === 'active' ? 1 : 0;
    if (rank(a) !== rank(b)) return rank(b) - rank(a);
    return new Date(b.created_at) - new Date(a.created_at);
  });
}

function renderDlStats() {
  const el = document.getElementById('dl-stats');
  if (!el) return;
  const counts = {active: 0, done: 0, error: 0, other: 0};
  _jobs.forEach(j => { counts[_jobBucket(j)] += 1; });
  const chips = [
    ['active', 'In corso', 'pg-stat-live'],
    ['done', 'Completati', 'pg-stat-ok'],
    ['error', 'Errori', 'pg-stat-error'],
  ];
  el.innerHTML = chips.map(([key, label, cls]) =>
    `<span class="pg-stat ${counts[key] ? cls : 'pg-stat-zero'}">
       <b>${counts[key]}</b><span>${label}</span>
     </span>`).join('');
}

// One group heading for a batch, with what the batch is doing as a whole —
// counted over every job in it, not over the ones the current filter lets
// through. Collapsed groups still report it, which is the point of collapsing
// them. When a filter hides some of the batch the count says so ("1 di 24"),
// because a heading reading 24 above a single card is a heading that lies.
function _buildJobGroup(batchId, label, jobs) {
  const all = [..._jobs.values()].filter(j => j.batch_id === batchId);
  const counts = {active: 0, done: 0, error: 0, other: 0};
  all.forEach(j => { counts[_jobBucket(j)] += 1; });
  const count = jobs.length === all.length
    ? String(all.length) : `${jobs.length} di ${all.length}`;
  const collapsed = _dlCollapsed.has(batchId);
  const summary = [
    counts.active ? `<span class="jg-n jg-live">${counts.active} in corso</span>` : '',
    counts.done ? `<span class="jg-n jg-ok">${counts.done} completati</span>` : '',
    counts.error ? `<span class="jg-n jg-err">${counts.error} falliti</span>` : '',
  ].filter(Boolean).join('');
  const body = collapsed ? '' : jobs.map(_buildJobCard).join('');
  return `<div class="job-group${collapsed ? ' is-collapsed' : ''}">
    <button class="job-group-head" data-action="jobs:toggleGroup" data-batch="${escapeHtml(batchId)}"
            aria-expanded="${collapsed ? 'false' : 'true'}">
      <i class="ti ti-chevron-down jg-chev"></i>
      <span class="jg-title">${escapeHtml(label)}</span>
      <span class="jg-count">${count}</span>
      <span class="jg-summary">${summary}</span>
    </button>
    <div class="job-group-body">${body}</div>
  </div>`;
}

function toggleJobGroup(batchId) {
  if (_dlCollapsed.has(batchId)) _dlCollapsed.delete(batchId);
  else _dlCollapsed.add(batchId);
  renderAllJobCards();
}

function renderAllJobCards() {
  const container = document.getElementById('jobs-container');
  const empty = document.getElementById('jobs-empty');
  if (!container) return;
  renderDlStats();

  const visible = [..._jobs.values()].filter(
    j => _dlFilter === 'all' || _jobBucket(j) === _dlFilter);

  if (!visible.length) {
    // Two different emptinesses, and telling them apart is the difference
    // between "nothing is happening" and "your filter hides everything".
    empty.style.display = '';
    empty.querySelector('span:last-child').textContent = _jobs.size
      ? 'Nessun download in questo filtro'
      : 'Nessun download in corso';
    container.innerHTML = '';
    updateActiveSection();
    return;
  }
  empty.style.display = 'none';

  // Batches keep their jobs together; everything else stays a loose card, in
  // the same list, ordered by the newest thing each entry holds.
  const groups = new Map();
  const loose = [];
  visible.forEach(j => {
    if (!j.batch_id) { loose.push(j); return; }
    if (!groups.has(j.batch_id)) groups.set(j.batch_id, []);
    groups.get(j.batch_id).push(j);
  });

  const entries = [
    ...loose.map(j => ({at: j.created_at, active: _jobBucket(j) === 'active',
                        html: () => _buildJobCard(j)})),
    ...[...groups].map(([batchId, jobs]) => {
      _dlSorted(jobs);
      const label = jobs.find(j => j.batch_label)?.batch_label || 'Gruppo';
      return {
        at: jobs.reduce((newest, j) => j.created_at > newest ? j.created_at : newest, ''),
        active: jobs.some(j => _jobBucket(j) === 'active'),
        html: () => _buildJobGroup(batchId, label, jobs),
      };
    }),
  ].sort((a, b) => (b.active - a.active) || (a.at < b.at ? 1 : -1));

  container.innerHTML = entries.map(e => e.html()).join('');
  updateActiveSection();
}

// A new job can belong to a group, can be filtered out, and can change where
// every other entry sorts. Inserting a node at the top guessed at all three;
// re-rendering answers them.
function addJobCard(job) {
  renderAllJobCards();
}

function refreshCardAppearance(jobId) {
  const j = _jobs.get(jobId);
  if (!j) return;
  const card = document.getElementById(`job-card-${jobId}`);
  if (!card) return;

  const phase = _jobPhases[j.job_id] || j.status;
  const isActive = j.status==='running' || j.status==='queued' || j.status==='scheduled';

  // Update card classes and border
  card.classList.toggle('is-done', j.status==='done');
  card.classList.toggle('is-error', j.status==='error');
  card.style.borderLeftColor = _phaseBorder(phase);

  // Update badge
  const badge = document.getElementById(`job-badge-${jobId}`);
  if (badge) {
    badge.className = `badge ${_phaseBadge(phase)} flex-shrink-0`;
    badge.textContent = _phaseLabel(phase);
  }

  // Update progress bar
  const bar = document.getElementById(`job-bar-${jobId}`);
  if (bar) {
    const barClass = _phaseBar(phase);
    const animated = isActive && j.status!=='queued' ? ' progress-bar-striped progress-bar-animated' : '';
    bar.className = `progress-bar ${barClass}${animated} job-progress-bar`;
    bar.style.width = (j.status==='queued' ? 0 : (j.status==='done' ? 100 : (j.progress?.pct||0))) + '%';
  }

  // Update fire/stop buttons
  const fire = document.getElementById(`job-fire-${jobId}`);
  if (fire) fire.innerHTML = j.status === 'scheduled' ? _fireBtnHtml(jobId) : '';
  const stop = document.getElementById(`job-stop-${jobId}`);
  if (stop) {
    stop.innerHTML = isActive && j.status !== 'scheduled' ? _stopBtnHtml(jobId) : '';
  }

  // Update info text
  const info = document.getElementById(`job-info-${jobId}`);
  if (info) {
    if (j.status==='error') info.textContent = j.error||'Errore';
    else if (j.status==='done') info.textContent = 'Completato';
    else if (j.status==='cancelled') info.textContent = 'Annullato';
  }

  updateActiveSection();
}

function updateActiveSection() {
  renderDlStats();
}

function updateActiveBadge() {
  const count = [..._jobs.values()].filter(j=>j.status==='running'||j.status==='queued'||j.status==='scheduled').length;
  const badge = document.getElementById('active-jobs-badge');
  if (count>0) { badge.style.display=''; badge.textContent=count; }
  else badge.style.display='none';
  updateActiveSection();
}

function handleProgressEvent(msg) {
  const job = _jobs.get(msg.job_id);
  if (job) {
    job.progress = { current:msg.current, total:msg.total, pct:msg.pct, speed:msg.speed||0,
                     bytes_speed:msg.bytes_speed||0, eta:msg.eta||null,
                     bytes_done:msg.bytes_done||0, bytes_total:msg.bytes_total ?? null,
                     bytes_total_estimated:msg.bytes_total_estimated !== false };
    const phase = msg.phase || _jobPhases[msg.job_id] || 'running';
    const prevPhase = _jobPhases[msg.job_id];
    _jobPhases[msg.job_id] = phase;
    if (phase !== prevPhase) _updateSteps(msg.job_id, phase);
  }
  // Update bar and info without full card rebuild
  const bar = document.getElementById(`job-bar-${msg.job_id}`);
  if (bar) bar.style.width = msg.pct + '%';
  const info = document.getElementById(`job-info-${msg.job_id}`);
  // Same builder as the initial render, so a live frame cannot quietly drop
  // the size the card was showing a moment ago.
  if (info) info.textContent = _jobInfoText(job ? job.progress : msg, 'running');
}

function handlePhaseEvent(jobId, phase) {
  _jobPhases[jobId] = phase;
  const job = _jobs.get(jobId);
  if (job) job.status = 'running';

  const badge = document.getElementById(`job-badge-${jobId}`);
  if (badge) {
    badge.className = `badge ${_phaseBadge(phase)} flex-shrink-0`;
    badge.textContent = _phaseLabel(phase);
  }
  const card = document.getElementById(`job-card-${jobId}`);
  if (card) card.style.borderLeftColor = _phaseBorder(phase);
  const bar = document.getElementById(`job-bar-${jobId}`);
  if (bar) {
    bar.className = `progress-bar ${_phaseBar(phase)} progress-bar-striped progress-bar-animated job-progress-bar`;
    const isIndeterminate = phase === 'joining' || phase === 'merging' || phase.startsWith('audio_');
    if (isIndeterminate) bar.style.width = '100%';
  }
  const info = document.getElementById(`job-info-${jobId}`);
  if (info) {
    const isIndeterminate = phase === 'joining' || phase === 'merging';
    if (isIndeterminate) info.textContent = _phaseLabel(phase) + '...';
  }
  _updateSteps(jobId, phase);
}

function handleDoneEvent(jobId, outputPath) {
  delete _jobPhases[jobId];
  const job = _jobs.get(jobId);
  if (job) { job.status='done'; job.output_path=outputPath; }
  _updateSteps(jobId, 'done');

  const card = document.getElementById(`job-card-${jobId}`);
  if (card) card.classList.add('is-done');
  const badge = document.getElementById(`job-badge-${jobId}`);
  if (badge) { badge.className='badge bg-success-lt flex-shrink-0'; badge.textContent='Completato'; }
  const bar = document.getElementById(`job-bar-${jobId}`);
  if (bar) {
    bar.style.width='100%';
    bar.className='progress-bar phase-bar-done bg-success job-progress-bar';
  }
  const info = document.getElementById(`job-info-${jobId}`);
  if (info) info.textContent='Completato';
  const stop = document.getElementById(`job-stop-${jobId}`);
  if (stop) stop.innerHTML='';

  // The edits above keep this one card right immediately. A finished job has
  // also left the "in corso" bucket, though, which moves it in the order,
  // changes its batch's summary, and under a filter may mean it no longer
  // belongs on screen at all - none of which a card can do to itself.
  renderAllJobCards();
  updateActiveBadge();
  // Refresh file manager if open
  if (document.getElementById('page-files')?.style.display!=='none') loadFiles();
}

function handleErrorEvent(jobId, message) {
  delete _jobPhases[jobId];
  const job = _jobs.get(jobId);
  if (job) { job.status='error'; job.error=message; }

  const card = document.getElementById(`job-card-${jobId}`);
  if (card) card.classList.add('is-error');
  const badge = document.getElementById(`job-badge-${jobId}`);
  if (badge) { badge.className='badge bg-danger-lt flex-shrink-0'; badge.textContent='Errore'; }
  const bar = document.getElementById(`job-bar-${jobId}`);
  if (bar) { bar.className='progress-bar phase-bar-error bg-danger job-progress-bar'; bar.style.width='100%'; }
  const info = document.getElementById(`job-info-${jobId}`);
  if (info) info.textContent = message==='Annullato' ? 'Annullato' : escapeHtml(message||'Errore');
  const stop = document.getElementById(`job-stop-${jobId}`);
  if (stop) stop.innerHTML='';

  // Same reason as handleDoneEvent: a failed job has changed bucket.
  renderAllJobCards();
  updateActiveBadge();
}

async function fireNow(jobId) {
  try {
    await api.post(`/api/download/${jobId}/fire`);
  } catch(e) { showToast(errText(e), 'danger'); }
}

async function cancelJob(jobId) {
  if (!await scConfirm('Interrompere il download?')) return;
  try {
    await api.del(`/api/download/${jobId}`);
  } catch(e) { showToast(errText(e), 'danger'); }
}

// The list is normally kept current by the event stream. This re-reads it from
// the server for the times that is not enough — a laptop coming back from
// sleep, a proxy that closed the connection, a tab that fell far behind.
async function refreshJobs() {
  const btn = document.getElementById('dl-refresh-btn');
  if (btn) btn.disabled = true;
  try {
    const jobs = await api.get('/api/jobs');
    _jobs.clear();
    jobs.forEach(j => _jobs.set(j.job_id, j));
    renderAllJobCards();
    updateActiveBadge();
  } catch (e) {
    showToast(errText(e, 'Impossibile aggiornare i download'), 'danger');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function clearFinished() {
  const finished = [..._jobs.entries()]
    .filter(([,j]) => j.status==='done'||j.status==='error'||j.status==='cancelled')
    .map(([id]) => id);
  // allSettled, so one refusal does not abandon the rest: every job that
  // can go, goes.
  await Promise.allSettled(finished.map(id => api.del(`/api/download/${id}`)));
  // UI cleanup handled by job_dismissed SSE; also clean locally in case SSE lags.
  // Re-rendered rather than each node removed: a batch whose jobs have all
  // gone would otherwise keep its heading, sitting above nothing.
  for (const id of finished) _jobs.delete(id);
  renderAllJobCards();
  updateActiveBadge();
}


// ── Delegated handlers ───────────────────────────────────────────────────────

registerActions({
  'jobs:refresh':     () => refreshJobs(),
  'jobs:clear':       () => clearFinished(),
  'jobs:filter':      d => setDlFilter(d.filter),
  'jobs:fire':        d => fireNow(d.job),
  'jobs:cancel':      d => cancelJob(d.job),
  'jobs:toggleGroup': d => toggleJobGroup(d.batch),
});
