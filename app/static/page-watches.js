/* StreamingCommunity Web Panel — page-watches.js */
//
// Followed series and anime. The follow button lives on the title page and
// calls in here; the list is its own page.

// ── Serie seguite ────────────────────────────────────────────────────────────

let _watches = [];

function fmtLastChecked(iso) {
  if (!iso) return 'mai controllata';
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return 'controllata ora';
  if (minutes < 60) return `controllata ${minutes} min fa`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `controllata ${hours} h fa`;
  return `controllata ${Math.round(hours / 24)} g fa`;
}

async function loadWatches() {
  const c = document.getElementById('watches-list');
  if (!c) return;
  try {
    // An approver sees every followed series, not only their own: arming a
    // series is their decision, and they cannot make it on a list that hides
    // the follows waiting for it.
    const data = await api.get(can('MANAGE_REQUESTS') ? '/api/watches' : '/api/watches/mine');
    _watches = data.watches || [];
    renderWatchesList();
  } catch (e) {
    c.innerHTML = `<p class="text-muted">${escapeHtml(errText(e, 'Impossibile caricare le serie seguite.'))}</p>`;
  }
}

// Whether a new episode lands by itself or waits for an approver is the whole
// question this page answers, so it is what the head counts.
function renderWatchesStats() {
  const el = document.getElementById('watches-stats');
  if (!el) return;
  const auto = _watches.filter(w => _watchIsAutomatic(w)).length;
  const chips = [
    [_watches.length, 'Seguite', 'pg-stat-live'],
    [auto, 'Automatiche', 'pg-stat-ok'],
    [_watches.length - auto, 'Dalla coda', 'pg-stat-warn'],
  ];
  el.innerHTML = chips.map(([value, label, cls]) =>
    `<span class="pg-stat ${value ? cls : 'pg-stat-zero'}"><b>${value}</b><span>${label}</span></span>`
  ).join('');
}

// The owner's DOWNLOAD permission is what the poller checks, so a follower
// without it must not be told the episode will just appear. An ownerless watch
// (no accounts at all) always downloads: there is no queue to wait in.
function _watchIsAutomatic(w) {
  return w.created_by === null || w.auto_approve ||
    (!!_me && w.created_by === _me.user.id && can('DOWNLOAD'));
}

function renderWatchesList() {
  const c = document.getElementById('watches-list');
  if (!c) return;
  renderWatchesStats();
  if (!_watches.length) {
    c.innerHTML = `<div class="empty-panel">
      <i class="ti ti-bell-off"></i>
      <p>Nessuna serie seguita.</p>
      <p class="text-muted" style="font-size:12px;margin-top:6px">
        Apri una serie o un anime dalla ricerca e premi «Segui» per scaricare
        i nuovi episodi appena escono.
      </p>
    </div>`;
    return;
  }
  c.innerHTML = _watches.map(w => {
    const auto = _watchIsAutomatic(w);
    const badge = auto
      ? '<span class="badge bg-green-lt">download automatico</span>'
      : '<span class="badge bg-yellow-lt">passa dalla coda</span>';
    const kind = w.media_type === 'anime' ? 'Anime' : 'Serie TV';
    const audio = w.audio_languages.length ? w.audio_languages.join(', ') : 'originale';
    // Arming is the approver's decision, and only worth offering where it would
    // change something: a series that already downloads by itself has nothing
    // to approve.
    const canArm = can('MANAGE_REQUESTS') && w.created_by !== null;
    const armButton = !canArm ? '' : w.auto_approve
      ? `<button class="btn btn-sm btn-outline-secondary" data-action="watch:arm" data-id="${w.id}" data-on="0"
                 title="I nuovi episodi torneranno a passare dalla coda di approvazione">
           <i class="ti ti-bell-x me-1"></i>Togli automatico
         </button>`
      : `<button class="btn btn-sm btn-outline-success" data-action="watch:arm" data-id="${w.id}" data-on="1"
                 title="Approva la serie una volta: i nuovi episodi verranno scaricati senza passare dalla coda">
           <i class="ti ti-bell-check me-1"></i>Approva automatico
         </button>`;
    const who = can('MANAGE_REQUESTS') && w.followers && w.followers.length
      ? `<span class="req-dot">·</span><i class="ti ti-user"></i> ${escapeHtml(w.followers.join(', '))}`
      : '';
    return `
      <div class="req-row">
        <div class="req-main">
          <div class="req-title">${escapeHtml(w.title)}${w.year ? ` <span class="text-muted">(${escapeHtml(w.year)})</span>` : ''}</div>
          <div class="req-meta">
            <i class="ti ti-device-tv"></i> ${kind}
            <span class="req-dot">·</span>
            <i class="ti ti-volume"></i> ${escapeHtml(audio)}
            <span class="req-dot">·</span>
            <i class="ti ti-refresh"></i> ${escapeHtml(fmtLastChecked(w.last_checked_at))}
            ${who}
          </div>
        </div>
        <div class="req-side">
          ${badge}
          <div class="req-actions">
            ${armButton}
            <button class="btn btn-sm btn-outline-secondary" id="watch-check-${w.id}"
                    data-action="watch:check" data-id="${w.id}"
                    title="Cerca subito nuovi episodi, senza aspettare il controllo automatico">
              <i class="ti ti-refresh me-1"></i>Controlla ora
            </button>
            <button class="btn btn-sm btn-outline-secondary" data-action="watch:unfollow" data-id="${w.id}">
              <i class="ti ti-bell-off me-1"></i>Non seguire più
            </button>
          </div>
        </div>
      </div>`;
  }).join('');
}

// The follow toggle lives in two modals whose contexts are shaped differently,
// so both are flattened to the same shape here rather than in each caller.
function _followTarget(kind) {
  if (kind === 'page') {
    // The title page, which serves both sources from one screen.
    const anime = _tp.type === 'anime';
    return {
      btnId: 'th-follow-btn',
      source: anime ? 'animeunity' : 'streamingcommunity',
      media_type: anime ? 'anime' : 'tv',
      external_id: String(_tp.id ?? ''),
      title: _tp.name,
      slug: _tp.slug,
      year: _tp.year,
      poster: _tp.poster,
      anime_type: _tp.animeType,
      audio_languages: _tpPicked('audio'),
      subtitle_languages: _tpPicked('subs'),
      // A film has no next episode to wait for.
      followable: _tp.type !== 'movie' && (!anime || (_tp.animeType || 'tv') !== 'movie'),
    };
  }
  if (kind === 'anime') {
    return {
      btnId: 'follow-anime-btn',
      source: 'animeunity',
      media_type: 'anime',
      external_id: String(_animeCtx.animeId ?? ''),
      title: _animeCtx.animeName,
      year: _animeCtx.animeYear,
      anime_type: _animeCtx.animeType,
      audio_languages: _animeCtx.audioLangs || [],
      subtitle_languages: _animeCtx.subLangs || [],
      // A one-shot anime film has no next episode to wait for.
      followable: (_animeCtx.animeType || 'tv') !== 'movie',
    };
  }
  return {
    btnId: 'follow-tv-btn',
    source: 'streamingcommunity',
    media_type: 'tv',
    external_id: String(_epCtx.tvId ?? ''),
    title: _epCtx.tvName,
    slug: _epCtx.slug,
    year: _epCtx.year,
    poster: _epCtx.poster,
    audio_languages: _epCtx.audioLangs || [],
    subtitle_languages: _epCtx.subLangs || [],
    followable: true,
  };
}

function _renderFollowButton(kind, following, busy = false) {
  const target = _followTarget(kind);
  const btn = document.getElementById(target.btnId);
  if (!btn) return;
  // Available with or without Jellyfin: an ownerless watch downloads directly,
  // the way everything else does when the panel runs without accounts.
  if (!target.followable || !(can('REQUEST') || can('DOWNLOAD'))) {
    btn.style.display = 'none';
    return;
  }
  btn.style.display = '';
  btn.disabled = busy;
  btn.dataset.following = following ? '1' : '';
  // No margin utilities: the header is a flex row with a gap, and the title
  // takes the free space.
  btn.className = 'btn btn-sm flex-shrink-0 ' + (following ? 'btn-success' : 'btn-outline-secondary');
  btn.innerHTML = following
    ? '<i class="ti ti-bell-check me-1"></i>Seguita'
    : '<i class="ti ti-bell-plus me-1"></i>Segui';
  btn.title = following
    ? 'I nuovi episodi vengono cercati automaticamente. Premi per smettere.'
    : 'Cerca automaticamente i nuovi episodi di questa serie';
}

async function checkWatchStatus(kind) {
  const target = _followTarget(kind);
  if (!target.followable || !target.external_id) { _renderFollowButton(kind, false); return; }
  try {
    const data = await api.get('/api/watches/status', {
      source: target.source, media_type: target.media_type, external_id: target.external_id,
    });
    _renderFollowButton(kind, !!data.followed_by_me);
  } catch (e) { _renderFollowButton(kind, false); }
}

async function toggleFollowSeries(kind) {
  const target = _followTarget(kind);
  const btn = document.getElementById(target.btnId);
  const following = !!(btn && btn.dataset.following);
  _renderFollowButton(kind, following, true);

  try {
    if (following) {
      const status = await api.get('/api/watches/status', {
        source: target.source, media_type: target.media_type, external_id: target.external_id,
      });
      await api.del(`/api/watches/${status.watch_id}`);
      _renderFollowButton(kind, false);
      showToast('Serie non più seguita', 'success');
    } else {
      let data;
      try {
        data = await api.post('/api/watches', target);
      } catch (e) {
        _renderFollowButton(kind, false);
        showToast(errText(e, 'Impossibile seguire la serie'), 'danger');
        return;
      }
      _renderFollowButton(kind, true);
      // Only true for someone who can start downloads. Without that permission
      // each new episode becomes a request an approver has to accept, and
      // saying otherwise sets up a wait for something that never arrives.
      showToast(can('DOWNLOAD')
        ? 'Serie seguita: i nuovi episodi arriveranno da soli'
        : 'Serie seguita: i nuovi episodi verranno richiesti a un amministratore',
        'success');
    }
    if (document.getElementById('page-watches').style.display !== 'none') loadWatches();
  } catch (e) {
    _renderFollowButton(kind, following);
    showToast('Errore di rete', 'danger');
  }
}

// The automatic check runs every few hours; this is for when an episode has
// just dropped and waiting for the next cycle makes no sense.
async function checkWatchNow(watchId) {
  const btn = document.getElementById(`watch-check-${watchId}`);
  if (btn) { btn.disabled = true; btn.innerHTML = '<i class="ti ti-loader-2 ti-spin me-1"></i>Controllo...'; }
  try {
    let data;
    try {
      data = await api.post(`/api/watches/${watchId}/check`);
    } catch (e) {
      showToast(errText(e, 'Controllo fallito'), 'danger');
      return;
    }
    if (data.new) {
      showToast(
        data.new === 1 ? '1 nuovo episodio trovato' : `${data.new} nuovi episodi trovati`,
        'success',
      );
    } else {
      showToast('Nessun nuovo episodio', 'info');
    }
  } catch (e) {
    showToast('Errore di rete', 'danger');
  } finally {
    // Redraws the row, which also refreshes "controllata ora".
    await loadWatches();
  }
}

// Arming a series before an episode exists, which is the whole point: waiting
// for the first request means waiting for the source to publish.
async function setWatchAutoApprove(watchId, enabled) {
  const watch = _watches.find(w => w.id === watchId);
  const name = watch ? watch.title : watchId;
  if (!enabled && !await scConfirm(`I nuovi episodi di «${name}» torneranno in coda. Procedere?`)) return;
  try {
    try {
      await api.post(`/api/watches/${watchId}/auto-approve`, {enabled});
    } catch (e) { showToast(errText(e, 'Operazione fallita'), 'danger'); return; }
    showToast(enabled
      ? `«${name}»: i nuovi episodi verranno scaricati automaticamente`
      : `«${name}»: i nuovi episodi torneranno in coda`, 'success');
    await loadWatches();
  } catch (e) {
    showToast('Errore di rete', 'danger');
  }
}

async function unfollowWatch(watchId) {
  const watch = _watches.find(w => w.id === watchId);
  if (!await scConfirm(`Smettere di seguire «${watch ? watch.title : watchId}»?`)) return;
  try {
    await api.del(`/api/watches/${watchId}`);
    showToast('Serie non più seguita', 'success');
    await loadWatches();
  } catch (e) { showToast(errText(e), 'danger'); }
}


// ── Delegated handlers ───────────────────────────────────────────────────────

registerActions({
  'watch:reload':   () => loadWatches(),
  'watch:arm':      d => setWatchAutoApprove(Number(d.id), d.on === '1'),
  'watch:check':    d => checkWatchNow(Number(d.id)),
  'watch:unfollow': d => unfollowWatch(Number(d.id)),
});
