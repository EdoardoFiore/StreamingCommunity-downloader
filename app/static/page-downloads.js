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
        document.getElementById(`job-card-${msg.job_id}`)?.remove();
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

  const fireBtn = j.status === 'scheduled'
    ? `<button class="btn btn-sm btn-outline-success ms-1" onclick="fireNow('${j.job_id}')" title="Lancia subito">
         <i class="ti ti-player-play"></i>
       </button>` : '';
  const stopBtn = isActive
    ? `<button class="btn btn-sm btn-outline-danger ms-1" onclick="cancelJob('${j.job_id}')" title="Interrompi">
         <i class="ti ti-player-stop"></i>
       </button>` : '';

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

function renderAllJobCards() {
  const container = document.getElementById('jobs-container');
  const empty = document.getElementById('jobs-empty');
  if (!_jobs.size) {
    empty.style.display=''; container.innerHTML='';
    return;
  }
  // Sort: active first, then by created_at desc
  const sorted = [..._jobs.values()].sort((a,b) => {
    const aActive = (a.status==='running'||a.status==='queued')?1:0;
    const bActive = (b.status==='running'||b.status==='queued')?1:0;
    if (aActive!==bActive) return bActive-aActive;
    return new Date(b.created_at)-new Date(a.created_at);
  });
  empty.style.display='none';
  const frag = document.createDocumentFragment();
  sorted.forEach(j => {
    const tmp = document.createElement('div');
    tmp.innerHTML = _buildJobCard(j);
    frag.appendChild(tmp.firstElementChild);
  });
  container.innerHTML = '';
  container.appendChild(frag);
  updateActiveSection();
}

function addJobCard(job) {
  const container = document.getElementById('jobs-container');
  const empty = document.getElementById('jobs-empty');
  empty.style.display='none';
  // Insert at top of container
  const tmp = document.createElement('div');
  tmp.innerHTML = _buildJobCard(job);
  container.insertBefore(tmp.firstElementChild, container.firstChild);
  updateActiveSection();
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
  if (fire) fire.innerHTML = j.status === 'scheduled'
    ? `<button class="btn btn-sm btn-outline-success ms-1" onclick="fireNow('${j.job_id}')" title="Lancia subito"><i class="ti ti-player-play"></i></button>`
    : '';
  const stop = document.getElementById(`job-stop-${jobId}`);
  if (stop) {
    stop.innerHTML = isActive && j.status !== 'scheduled'
      ? `<button class="btn btn-sm btn-outline-danger ms-1" onclick="cancelJob('${j.job_id}')" title="Interrompi"><i class="ti ti-player-stop"></i></button>`
      : '';
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
  const active = [..._jobs.values()].filter(j=>j.status==='running'||j.status==='queued'||j.status==='scheduled');
  const pill = document.getElementById('dl-active-pill');
  const countEl = document.getElementById('dl-active-count');
  if (active.length) {
    pill.style.display=''; countEl.textContent=active.length;
  } else {
    pill.style.display='none';
  }
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

  updateActiveBadge();
}

async function fireNow(jobId) {
  try {
    const res = await fetch(`/api/download/${jobId}/fire`, {method:'POST'});
    if (!res.ok) { const d=await safeJson(res); showToast(d.detail||'Errore','danger'); }
  } catch(e) { showToast('Errore di rete','danger'); }
}

async function cancelJob(jobId) {
  if (!await scConfirm('Interrompere il download?')) return;
  try {
    const res = await fetch(`/api/download/${jobId}`, {method:'DELETE'});
    if (!res.ok) { const d=await safeJson(res); showToast(d.detail||'Errore','danger'); }
  } catch(e) { showToast('Errore di rete','danger'); }
}

// The list is normally kept current by the event stream. This re-reads it from
// the server for the times that is not enough — a laptop coming back from
// sleep, a proxy that closed the connection, a tab that fell far behind.
async function refreshJobs() {
  const btn = document.getElementById('dl-refresh-btn');
  if (btn) btn.disabled = true;
  try {
    const res = await fetch('/api/jobs');
    if (!res.ok) { showToast('Impossibile aggiornare i download', 'danger'); return; }
    const jobs = await safeJson(res);
    _jobs.clear();
    jobs.forEach(j => _jobs.set(j.job_id, j));
    renderAllJobCards();
    updateActiveBadge();
  } catch (e) {
    showToast('Errore di rete', 'danger');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function clearFinished() {
  const finished = [..._jobs.entries()]
    .filter(([,j]) => j.status==='done'||j.status==='error'||j.status==='cancelled')
    .map(([id]) => id);
  await Promise.allSettled(finished.map(id =>
    fetch(`/api/download/${id}`, {method:'DELETE'})
  ));
  // UI cleanup handled by job_dismissed SSE; also clean locally in case SSE lags
  for (const id of finished) {
    _jobs.delete(id);
    document.getElementById(`job-card-${id}`)?.remove();
  }
  if (!_jobs.size) {
    const container = document.getElementById('jobs-container');
    const empty = document.getElementById('jobs-empty');
    empty.style.display='';
    container.innerHTML='';
  }
  updateActiveBadge();
}
