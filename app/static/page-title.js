// The title page.
//
// Replaces the detail / episode / anime modals: one address, one screen,
// everything a download decision needs. It rebuilds itself from the URL, so a
// reload or a pasted link works and there is nothing to "reopen".
//
// Downloads are not reimplemented here. The page fills _epCtx / _animeCtx and
// calls the existing startEpisodeDownload / downloadWholeSeason /
// startFilmDownload / startAnimeDownload, which already know how to branch
// between a download and a request.

let _tp = null;      // current title state
let _tpToken = 0;    // guards against a slow response painting over a newer title

function titleHash(item) {
  const kind = item.type === 'anime' ? 'anime' : (item.type === 'movie' ? 'movie' : 'tv');
  return hashFor('detail', [kind, item.id, item.slug || '']);
}

// From a result card or a shelf.
function openTitle(idx) {
  const item = _searchResults[idx];
  if (!item) return;
  _tpSeed = item;                     // spares the first render a round trip
  location.hash = titleHash(item);
}
let _tpSeed = null;

async function loadTitlePage(route) {
  const token = ++_tpToken;
  // Every call below carries the site version, and a pasted link can arrive
  // before the boot knows it — it used to be sent empty and the endpoint
  // answered "field required". ensureDomain() is now shared with the search,
  // which needs the same guarantee for the same reason.
  await ensureDomain();
  if (token !== _tpToken) return;
  const seed = _tpSeed; _tpSeed = null;

  _tp = {
    ...route,
    name: seed?.name || '',
    // Not for a series: its search record has no release_date, and the
    // last_air_date itemYear() falls back to is the latest season's - the
    // folder was named after it (issue #21). The title page's own year
    // arrives with the metadata.
    year: seed && route.type !== 'tv' ? itemYear(seed) : null,
    poster: seed?.poster || null,
    score: seed?.score ? parseFloat(seed.score).toFixed(1) : null,
    age: seed?.age || null,
    seasonsCount: seed?.seasons_count || 0,
    episodesCount: seed?.episodes_count || 0,
    animeType: seed?.media_type || null,
    plot: seed?.plot || null,
    genres: seed?.genres || [],
    // AnimeUnity has no metadata endpoint - it never needed one, because its
    // search record already carries the plot, the genres, the banner and the
    // studio. So the page builds its own "meta" from what the card was
    // holding rather than asking for anything.
    meta: seed && seed.type === 'anime' ? {
      backdrop: seed.backdrop || null,
      plot: seed.plot || null,
      genres: seed.genres || [],
      rating: seed.score ? parseFloat(seed.score).toFixed(1) : null,
      status: seed.status || null,
      original_name: seed.original_name || null,
      studio: seed.studio || null,
      season: seed.season || null,
      cast: [], directors: [],
    } : null,
    // From the address when a link named one, so a reload of S03 is S03.
    season: route.season || 1,
    episodes: [],
    audio: [], subs: [], tracksLoaded: false, tracksError: null,
    // Raw value of the datetime input, kept so a re-render does not clear
    // what the user typed; scheduledAt is its ISO form, or null for "now".
    scheduledRaw: '', scheduledAt: null,
    tab: null,
  };

  // Reached by pasted link: there is no card to borrow from, and AnimeUnity
  // has nothing to ask. The slug carried in the id is the only name available.
  if (!_tp.name && route.type === 'anime') {
    const slug = String(route.id).split('-').slice(1).join('-') || route.slug;
    _tp.name = slug ? slug.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase()) : 'Anime';
  }

  showPage('detail');
  _tpRender();

  let metaReady = Promise.resolve();
  if (route.type !== 'anime') {
    const q = { slug: route.slug, version: currentVersion || '' };
    // Metadata and the track list are independent: one reads the title page,
    // the other reaches the stream host. Neither waits on the other.
    metaReady = api.get(`/api/metadata/${route.type}/${route.id}`, q)
      .then(meta => { if (token === _tpToken) { _tpApplyMeta(meta); _tpRender(); } })
      .catch(() => {});

    api.get(`/api/search/languages/${route.id}`,
            { type: route.type, slug: route.slug, version: currentVersion || '' })
      .then(info => {
        if (token !== _tpToken) return;
        const audio = dedupeLangs(info.audio);
        const subs = dedupeLangs(info.subtitles);
        const preferred = preferredSubSelection(subs);
        _tp.audio = audio.map(c => ({ code: c, on: c === 'ita' || audio.length === 1 }));
        _tp.subs  = subs.map(c => ({ code: c, on: preferred.has(c) }));
        _tp.tracksLoaded = true;
        _tpRender();
      })
      .catch(e => {
        if (token !== _tpToken) return;
        // Swallowing this used to leave no chips, no reason, and a download
        // button that looked perfectly fine.
        _tp.tracksError = e.message || 'Sorgente non raggiungibile';
        _tp.tracksLoaded = true;
        _tpRender();
      });
  }

  if (route.type === 'tv') {
    // The episode list waits for the year: the in-library marks and every
    // download built from _epCtx are named after it.
    await metaReady;
    if (token !== _tpToken) return;
    await tpLoadSeason(1, token);
  }
  else if (route.type === 'anime') await _tpLoadAnime(token);
}

function _tpApplyMeta(meta) {
  if (!meta) return;
  _tp.meta = meta;
  _tp.name = _tp.name || meta.name || '';
  _tp.plot = meta.plot || _tp.plot;
  _tp.genres = meta.genres?.length ? meta.genres : _tp.genres;
  _tp.score = _tp.score || (meta.rating ? String(meta.rating) : null);
  _tp.poster = _tp.poster || meta.poster;
  _tp.age = _tp.age ?? meta.age;
  // Authoritative over the search card's guess. A series with no year keeps
  // none rather than the card's, which is the wrong one (issue #21).
  if (meta.year || _tp.type === 'tv') _tp.year = meta.year || null;
  if (!_tp.seasonsCount && meta.seasons_count) _tp.seasonsCount = meta.seasons_count;
}

// ── Episodes ──────────────────────────────────────────────────────────────────

async function tpLoadSeason(n, token = _tpToken) {
  _tp.season = n;
  syncHash('detail');
  _tp.episodes = null;                       // renders the loading state
  _tpRenderEpisodes();
  try {
    const tk = await api.get(`/api/tv/${_tp.id}/token`);
    const eps = await api.get(`/api/tv/${_tp.id}/seasons/${n}/episodes`, {
      slug: _tp.slug, version: currentVersion || '', token: tk.token,
      title: _tp.name, year: _tp.year || '',
    });
    if (token !== _tpToken) return;
    _tp.episodes = eps;
    _tpSyncEpCtx(tk.token);
  } catch (e) {
    if (token !== _tpToken) return;
    _tp.episodes = [];
    _tp.episodesError = e.message || 'Errore';
  }
  _tpRenderEpisodes();
  _tpRenderFacts();      // the episode count is per season
  _tpRenderHero();       // and so is the chip
}

async function _tpLoadAnime(token) {
  _tp.episodes = null;
  _tpRenderEpisodes();
  try {
    const eps = await api.get(`/api/anime/${_tp.id}/episodes`);
    if (token !== _tpToken) return;
    // Spread, not a projection: startAnimeDownload reads episode.number and
    // posts the whole record back. Narrowing it to {id, n} here sent the API
    // a stripped object and labelled every job "E undefined". `n` is added
    // only so the shared row renderer has the field it expects.
    _tp.episodes = eps.map(e => ({ ...e, n: e.number ?? e.n }));
    _animeCtx = {
      animeId: _tp.id, animeName: _tp.name, animeType: _tp.animeType || 'tv',
      animeYear: _tp.year, episodes: _tp.episodes,
      audioLangs: _tpPicked('audio'), subLangs: _tpPicked('subs'), scheduledAt: _tp.scheduledAt,
    };
  } catch (e) {
    if (token !== _tpToken) return;
    _tp.episodes = [];
    _tp.episodesError = e.message || 'Errore';
  }
  _tpRenderEpisodes();
}

// The download functions read these globals. Keeping them in sync here means
// the page does not duplicate the download-vs-request branching.
function _tpSyncEpCtx(token) {
  _epCtx = {
    tvId: _tp.id, tvName: _tp.name, slug: _tp.slug, year: _tp.year,
    scheduledAt: _tp.scheduledAt, token, episodes: _tp.episodes || [],
    currentSeason: _tp.season, poster: _tp.poster,
    // downloadWholeSeries reads this off the context, and said "tutte le
    // undefined stagioni" without it.
    seasonsCount: _tp.seasonsCount,
    audioLangs: _tpPicked('audio'), subLangs: _tpPicked('subs'),
  };
}

function _tpPicked(which) {
  const picked = (_tp[which] || []).filter(t => t.on).map(t => t.code);
  return which === 'audio' ? (picked.length ? picked : ['ita']) : picked;
}

// ── Tracks ────────────────────────────────────────────────────────────────────

// Empty means "now", which is what the download endpoints already expect.
function tpSetSchedule(value) {
  _tp.scheduledRaw = value || '';
  _tp.scheduledAt = value ? new Date(value).toISOString() : null;
  if (_tp.type === 'anime') { if (_animeCtx) _animeCtx.scheduledAt = _tp.scheduledAt; }
  else if (_epCtx) _epCtx.scheduledAt = _tp.scheduledAt;
  _tpRenderEpisodes();     // the batch buttons say "Programma" once a time is set
}

function tpToggleTrack(which, code) {
  const t = (_tp[which] || []).find(x => x.code === code);
  if (!t) return;
  t.on = !t.on;
  if (_tp.type === 'anime') { if (_animeCtx) { _animeCtx.audioLangs = _tpPicked('audio'); _animeCtx.subLangs = _tpPicked('subs'); } }
  else if (_epCtx) { _epCtx.audioLangs = _tpPicked('audio'); _epCtx.subLangs = _tpPicked('subs'); }
  _tpRenderTracks();
}

// ── Tabs ──────────────────────────────────────────────────────────────────────

function _tpTabs() {
  const tabs = [];
  if (_tp.type !== 'movie') tabs.push(['episodes', 'Episodi']);
  tabs.push(['about', 'Descrizione']);
  if (_tp.meta?.cast?.length || _tp.meta?.directors?.length) tabs.push(['cast', 'Cast']);
  // Anime tracks are chosen by the source, not here.
  if (_tp.type !== 'anime') tabs.push(['tracks', 'Tracce']);
  return tabs;
}

function tpSetTab(name) {
  _tp.tab = name;
  for (const [key] of [['episodes'], ['about'], ['cast'], ['tracks']]) {
    const pane = document.getElementById(`th-pane-${key}`);
    if (pane) pane.hidden = key !== name;
  }
  document.querySelectorAll('.th-tab').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === name));
}

// ── Render ────────────────────────────────────────────────────────────────────

function _tpRender() {
  if (!_tp) return;
  _tpRenderHero();
  _tpRenderTabsBar();
  _tpRenderAbout();
  _tpRenderCast();
  _tpRenderTracks();
  _tpRenderFacts();
  _tpRenderTrailer();
  _tpRenderEpisodes();
  if (!_tp.tab || !_tpTabs().some(([k]) => k === _tp.tab)) {
    tpSetTab(_tpTabs()[0][0]);
  }
}

function _tpRenderHero() {
  // Set straight away rather than behind a preload that flips a class on
  // load: the same pattern on the episode stills left them invisible whenever
  // the load event was missed. A backdrop that 404s simply paints nothing
  // behind the gradient, which is what "no backdrop" looks like anyway.
  const art = document.getElementById('th-art');
  const src = _tp.meta?.backdrop;
  if (src) {
    art.style.backgroundImage = `url("${encodeURI(src)}")`;
    art.classList.add('is-loaded');
  } else {
    art.style.backgroundImage = '';
    art.classList.remove('is-loaded');
  }

  const kindLabel = _tp.type === 'movie' ? 'Film' : (_tp.type === 'anime' ? 'Anime' : 'Serie TV');
  // A back control, not just a trail. The trail alone was a row of grey text
  // against artwork and read as a caption rather than as the way out.
  document.getElementById('th-crumbs').innerHTML =
    `<button class="th-back" data-action="tp:back" title="Torna alla ricerca">
       <i class="ti ti-arrow-left"></i>Cerca
     </button>` +
    `<span class="sep">›</span><span>${escapeHtml(kindLabel)}</span>` +
    `<span class="sep">›</span><span class="th-crumb-here">${escapeHtml(_tp.name || '—')}</span>`;

  document.getElementById('th-name').textContent = _tp.name || '—';

  const chips = [['ti-movie', kindLabel]];
  if (_tp.year) chips.push(['ti-calendar', _tp.year]);
  if (_tp.type === 'tv' && _tp.seasonsCount)
    chips.push(['ti-layout-grid', `${_tp.seasonsCount} stagion${_tp.seasonsCount === 1 ? 'e' : 'i'}`]);
  if (_tp.episodes?.length) {
    chips.push(['ti-list', _tp.type === 'tv'
      ? `${_tp.episodes.length} episodi in S${String(_tp.season).padStart(2, '0')}`
      : `${_tp.episodes.length} episodi`]);
  }
  if (_tp.meta?.runtime) chips.push(['ti-clock', `${_tp.meta.runtime} min`]);
  if (_tp.age) chips.push(['ti-shield', `${_tp.age}+`]);
  if (_tp.meta?.quality) chips.push(['ti-badge-hd', _tp.meta.quality]);
  document.getElementById('th-chips').innerHTML = chips
    .map(([i, t]) => `<span class="th-chip"><i class="ti ${i}"></i>${escapeHtml(String(t))}</span>`).join('');

  const plotEl = document.getElementById('th-plot');
  plotEl.textContent = _tp.plot || '';
  plotEl.style.display = _tp.plot ? '' : 'none';

  document.getElementById('th-score').innerHTML = _tp.score
    ? `<span class="th-score"><i class="ti ti-star-filled"></i>${escapeHtml(_tp.score)}/10</span>` : '';
  document.getElementById('th-genres').innerHTML = (_tp.genres || []).slice(0, 4)
    .map(g => `<span class="th-genre">${escapeHtml(g)}</span>`).join('');

  _tpRenderCta();
}

function _tpRenderCta() {
  const wants = !can('DOWNLOAD');
  // The hero takes the broadest action available - the whole series where
  // there is one - and the season bar keeps the season. Two buttons that both
  // said "season" left nowhere to ask for the lot.
  const verb = wants ? 'Richiedi' : (_tp.scheduledAt ? 'Programma' : 'Scarica');
  // A requester can ask for a season but not for a series or a whole anime:
  // those two go through the batch download endpoints, which require
  // DOWNLOAD. Offering them would be a button that 403s.
  const scope = _tp.type === 'movie' ? ''
    : (_tp.type === 'anime' ? (wants ? '' : ' tutti gli episodi')
    : (!wants && _tp.seasonsCount > 1 ? ' la serie' : ' la stagione'));
  const label = `${verb}${scope}`;
  const icon = wants ? 'ti-send' : (_tp.seasonsCount > 1 && _tp.type === 'tv' ? 'ti-stack-2' : 'ti-download');
  const followable = _tp.type !== 'movie';

  // Scheduling is part of the download privilege: a requester picks tracks and
  // an approver decides when it runs.
  const sched = can('DOWNLOAD') ? `
    <label class="th-sched" title="Lascia vuoto per scaricare subito">
      <i class="ti ti-clock"></i>
      <input type="datetime-local" id="th-sched-at" value="${escapeHtml(_tp.scheduledRaw)}"
             data-change="tp:schedule">
    </label>` : '';

  // An anime requester has nothing batchable to press: every episode is its
  // own request, from the rows below.
  const primary = (wants && _tp.type === 'anime') ? '' : `
    <button class="btn btn-primary" data-action="tp:primary">
      <i class="ti ${icon} me-1"></i>${label}
    </button>`;

  document.getElementById('th-cta').innerHTML = `
    ${sched}
    ${primary}
    <!-- Left bare: _renderFollowButton owns this button's class and label,
         and rewriting them here would fight it. -->
    ${followable ? `<button id="th-follow-btn" data-action="tp:follow"></button>` : ''}
    <button class="th-icon-btn" data-action="tp:tab" data-tab="tracks" title="Scegli audio e sottotitoli">
      <i class="ti ti-adjustments"></i>
    </button>`;
  if (followable) tpRefreshFollow();
}

function _tpRenderTabsBar() {
  document.getElementById('th-tabs').innerHTML = _tpTabs()
    .map(([k, label]) =>
      `<button class="th-tab${_tp.tab === k ? ' active' : ''}" data-tab="${k}" role="tab"
        data-action="tp:tab" data-tab="${k}">${escapeHtml(label)}</button>`).join('');
}

function _tpRenderAbout() {
  const el = document.getElementById('th-pane-about');
  el.innerHTML = _tp.plot
    ? `<p style="color:var(--text-sub);font-size:13.5px;line-height:1.7;max-width:70ch">${escapeHtml(_tp.plot)}</p>`
    : `<div class="th-empty">Nessuna descrizione disponibile per questo titolo.</div>`;
}

function _tpRenderCast() {
  const el = document.getElementById('th-pane-cast');
  const m = _tp.meta;
  if (!m?.cast?.length && !m?.directors?.length) { el.innerHTML = ''; return; }
  const row = (k, v) => `<div class="th-track-group">
      <div class="th-track-h">${k}</div>
      <div class="th-track-chips">${v.map(n => `<span class="th-track">${escapeHtml(n)}</span>`).join('')}</div>
    </div>`;
  el.innerHTML = (m.directors?.length ? row('Regia', m.directors) : '')
               + (m.cast?.length ? row('Interpreti', m.cast) : '');
}

function _tpRenderTracks() {
  const el = document.getElementById('th-pane-tracks');
  if (_tp.type === 'anime') { el.innerHTML = ''; return; }
  if (!_tp.tracksLoaded) {
    el.innerHTML = `<div class="th-empty"><span class="spinner-border spinner-border-sm me-2"></span>Lettura delle tracce…</div>`;
    return;
  }
  if (_tp.tracksError) {
    el.innerHTML = `<div class="alert alert-danger">${escapeHtml(_tp.tracksError)}</div>`;
    return;
  }
  const group = (which, title, icon, empty) => {
    const list = _tp[which] || [];
    const chips = list.length
      ? list.map(t => `<button class="th-track${t.on ? ' on' : ''}"
          data-action="tp:track" data-which="${which}" data-code="${escapeHtml(t.code)}">${escapeHtml(langName(t.code))}</button>`).join('')
      : `<span class="th-empty" style="padding:0">${empty}</span>`;
    return `<div class="th-track-group">
        <div class="th-track-h"><i class="ti ${icon}"></i>${title}</div>
        <div class="th-track-chips">${chips}</div>
      </div>`;
  };
  el.innerHTML =
    group('audio', 'Audio', 'ti-volume', 'solo la traccia originale')
  + group('subs', 'Sottotitoli', 'ti-badge-cc', 'nessun sottotitolo disponibile')
  + `<p style="color:var(--text-muted);font-size:12px;margin:0">
       Vale per ogni download avviato da questa pagina. Una lingua audio mai
       presente nel file non viene sostituita con un'altra: la richiesta si
       ferma e la guarda una persona.
     </p>`;
}

function _tpRenderFacts() {
  const m = _tp.meta || {};
  const rows = [];
  if (m.original_name && m.original_name !== _tp.name) rows.push(['Titolo originale', m.original_name]);
  if (_tp.year) rows.push(['Anno', _tp.year]);
  if (_tp.type === 'tv' && _tp.seasonsCount) rows.push(['Stagioni', _tp.seasonsCount]);
  // Per season, not for the series: the source gives no series-wide total, and
  // labelling one season's count "Episodi" read as if it were that total.
  if (_tp.episodes?.length) {
    rows.push([_tp.type === 'tv' ? `Episodi (S${String(_tp.season).padStart(2, '0')})` : 'Episodi',
               _tp.episodes.length]);
  }
  if (m.runtime) rows.push(['Durata', `${m.runtime} min`]);
  if (m.status) rows.push(['Stato', m.status]);
  if (m.studio) rows.push(['Studio', m.studio]);
  if (m.season) rows.push(['Stagione di uscita', m.season]);
  if (m.quality) rows.push(['Qualità', m.quality]);
  if (_tp.genres?.length) rows.push(['Genere', _tp.genres.join(', ')]);
  document.getElementById('th-facts').innerHTML = rows
    .map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(v))}</dd>`).join('')
    || `<dd style="grid-column:1/-1;text-align:left" class="th-empty">—</dd>`;
}

function _tpRenderTrailer() {
  const card = document.getElementById('th-trailer-card');
  const url = _tp.meta?.trailer_url;
  if (!url) { card.hidden = true; return; }
  card.hidden = false;
  const still = _tp.meta?.backdrop ? `style="background-image:url('${encodeURI(_tp.meta.backdrop)}')"` : '';
  document.getElementById('th-trailer').innerHTML =
    `<button class="th-trailer-facade" ${still} data-action="tp:trailer" aria-label="Riproduci il trailer"></button>`;
}

function tpPlayTrailer() {
  const url = _tp.meta?.trailer_url || '';
  const id = (url.match(/[?&]v=([\w-]+)/) || [])[1];
  if (!id) { window.open(url, '_blank', 'noopener'); return; }
  // Only now does anything reach YouTube, and through the no-cookie host.
  document.getElementById('th-trailer').innerHTML =
    `<iframe class="th-trailer-frame" src="https://www.youtube-nocookie.com/embed/${encodeURIComponent(id)}?autoplay=1"
       allow="accelerometer; autoplay; encrypted-media; picture-in-picture" allowfullscreen
       referrerpolicy="no-referrer"></iframe>`;
}

function _tpRenderEpisodes() {
  const el = document.getElementById('th-pane-episodes');
  if (!el || _tp.type === 'movie') { if (el) el.innerHTML = ''; return; }

  const pills = _tp.type === 'tv' && _tp.seasonsCount > 1
    ? `<div class="th-season-pills">${Array.from({ length: _tp.seasonsCount }, (_, i) => i + 1)
        .map(n => `<button class="th-season-pill${n === _tp.season ? ' active' : ''}"
          data-action="tp:season" data-n="${n}">S${String(n).padStart(2, '0')}</button>`).join('')}</div>` : '';

  // Batching is a download privilege; a requester asks episode by episode.
  // The verb follows the schedule field, so the button says what will happen.
  const verb = _tp.scheduledAt ? 'Programma' : 'Scarica';
  let batch = '';
  if (_tp.episodes?.length) {
    if (can('DOWNLOAD')) {
      batch = _tp.type === 'anime'
        ? `<button class="btn btn-sm btn-outline-primary" data-action="tp:batchAnime">
             <i class="ti ti-download me-1"></i>${verb} tutti gli episodi</button>`
        : `<button class="btn btn-sm btn-outline-primary" data-action="tp:batchSeason" data-season="${_tp.season}">
             <i class="ti ti-download me-1"></i>${verb} la stagione</button>`;
    } else if (_tp.type === 'tv' && can('REQUEST')) {
      batch = `<button class="btn btn-sm btn-outline-primary" data-action="tp:requestSeason">
                 <i class="ti ti-send me-1"></i>Richiedi la stagione</button>`;
    }
  }

  const head = `<div class="th-season-bar">
      <span class="th-season-label">${_tp.type === 'anime' ? 'Episodi' : `Stagione ${_tp.season}`}</span>
      ${pills}
      <span class="th-season-batch">${batch}</span>
    </div>`;

  if (_tp.episodes === null) {
    el.innerHTML = head + `<div class="th-empty"><span class="spinner-border spinner-border-sm me-2"></span>Caricamento episodi…</div>`;
    return;
  }
  if (_tp.episodesError) {
    el.innerHTML = head + `<div class="alert alert-danger">${escapeHtml(_tp.episodesError)}</div>`;
    return;
  }
  if (!_tp.episodes.length) {
    el.innerHTML = head + `<div class="th-empty">Nessun episodio in questa stagione.</div>`;
    return;
  }

  const wants = !can('DOWNLOAD');
  const rows = _tp.episodes.map((ep, i) => {
    // Only when the source has one. The placeholder means "an image was
    // expected and did not arrive", which is true of a StreamingCommunity
    // still that failed and false of AnimeUnity, whose episode records carry
    // no image field at all - there, reserving the slot just draws the same
    // empty box down the whole list.
    const still = ep.still ? thumbHtml(ep.still, 'th-ep-still') : '';
    const plot = ep.plot ? `<div class="th-ep-plot">${escapeHtml(ep.plot)}</div>` : '';
    const dur = ep.duration ? `<span class="th-ep-dur">${escapeHtml(String(ep.duration))}m</span>` : '';
    const owned = ep.in_library
      ? `<i class="ti ti-circle-check-filled th-ep-owned" title="Già in libreria"></i>` : '';
    return `<div class="th-ep">
        <span class="th-ep-n">${escapeHtml(String(ep.n))}</span>
        ${still}
        <div class="th-ep-body">
          <div class="th-ep-name">${escapeHtml(ep.name || `Episodio ${ep.n}`)}</div>
          ${plot}
        </div>
        ${dur}${owned}
        <button class="btn btn-sm ${wants ? 'btn-outline-primary' : 'btn-primary'}"
                data-action="tp:episode" data-index="${i}">
          <i class="ti ${wants ? 'ti-send' : 'ti-download'} me-1"></i>${wants ? 'Richiedi' : 'Scarica'}
        </button>
      </div>`;
  }).join('');

  el.innerHTML = head + `<div class="th-eps">${rows}</div>`;
}

// ── Actions ───────────────────────────────────────────────────────────────────

function tpPrimary() {
  // Without DOWNLOAD a season becomes a season's worth of requests, made
  // server-side so dedup and the library check apply to each of them.
  if (!can('DOWNLOAD') && _tp.type === 'tv') { tpRequestSeason(); return; }
  if (_tp.type === 'movie') {
    startFilmDownload(_tp.id, _tp.name, _tp.year, _tp.scheduledAt, _tpPicked('audio'), _tpPicked('subs'), _tp.poster);
  } else if (_tp.type === 'anime') {
    downloadAllAnime();
  } else if (_tp.seasonsCount > 1) {
    downloadWholeSeries();
  } else {
    downloadWholeSeason(_tp.season);
  }
}

async function tpToggleFollow() { await toggleFollowSeries('page'); }
async function tpRefreshFollow() { await checkWatchStatus('page'); }

// Back to wherever the visitor came from, and to the search page when they
// arrived by pasted link and there is no history to go back to.
function tpBack() {
  // Back where possible, because it returns to the exact search that was
  // there — query, filters and all — rather than to an empty one.
  if (history.length > 1 && document.referrer !== '') { history.back(); return; }
  navigate('search');
}

// Every episode of the season, as requests. The server enumerates and creates
// them, so each one is an ordinary request: already-asked episodes are joined
// rather than duplicated, and the reply says how many of each.
async function tpRequestSeason() {
  const n = _tp.episodes?.length || 0;
  if (!await scConfirm(`Richiedere tutti i ${n} episodi della stagione ${_tp.season}?`)) return;
  try {
    const r = await api.post('/api/requests/season', {
      source: 'streamingcommunity',
      external_id: String(_tp.id), title: _tp.name, slug: _tp.slug,
      season: _tp.season, year: _tp.year || null, poster: _tp.poster || null,
      audio_languages: _tpPicked('audio'),
      subtitle_languages: _tpPicked('subs'),
    });
    const parts = [];
    if (r.created) parts.push(`${r.created} richiest${r.created === 1 ? 'a' : 'e'}`);
    if (r.joined) parts.push(`${r.joined} già in coda`);
    showToast(parts.join(', ') || 'Nessun episodio da richiedere', 'success');
    refreshQueueBadge();
  } catch (e) {
    showToast(e.message || 'Richiesta non riuscita', 'danger');
  }
}


// ── Avvio di un download ─────────────────────────────────────────────────────
//
// Moved here from app.js: after the detail page replaced the three modals,
// this page is the only caller of any of it.

// ── Tracce audio e sottotitoli ───────────────────────────────────────────────

// Which subtitle tracks start selected.
//
// A forced Italian track subtitles only what the Italian audio does not cover
// - signs, and lines spoken in another language - while a full Italian track
// repeats dialogue you can already hear. Alongside Italian audio the forced
// one is almost always what is wanted, so it wins whenever the source has it,
// and the full track is the fallback rather than the default.
function preferredSubSelection(codes) {
  const forcedIta = codes.find(c => /^forced[-_ ]?ita/i.test(c));
  const picked = new Set();
  if (forcedIta) picked.add(forcedIta);
  else if (codes.includes('ita')) picked.add('ita');
  if (codes.includes('eng')) picked.add('eng');
  return picked;
}

// The source lists some languages more than once (two "ita", two "ger" on a
// single title). Two identical chips are two controls for one thing.
function dedupeLangs(codes) {
  return [...new Set(codes || [])];
}


// Guards against a stale response painting over a newer one: open a title, close
// it, open another before the first reply lands, and the first one used to win.
// The two fetches of one open land in either order, and each needs something
// the other has: the failure message depends on whether a fallback exists.





// ── Requesting ─────────────────────────────────────────────────────────────────

// Same form, different action: without the download permission the choice of
// audio and subtitles becomes a request instead of a job.
async function submitRequest(payload, label) {
  let data;
  try {
    data = await api.post('/api/requests', payload);
  } catch (e) { showToast(errText(e), 'danger'); return false; }

  try {
    const status = data.request.status;
    if (status === 'available') showToast(`${label} è già in libreria.`, 'info');
    else if (!data.created) showToast(`${label} era già stato richiesto: sarai avvisato.`, 'info');
    else showToast(`Richiesta inviata: ${label}`, 'success');

    _requestStatus[String(payload.external_id)] = { id: data.request.id, status };
    renderRequestRibbons();
    refreshNotifications();
    return true;
  } catch (e) {
    // The request went through; only the bookkeeping after it did not.
    console.error('submitRequest:', e);
    return true;
  }
}

// ── Film download ──────────────────────────────────────────────────────────────

async function startFilmDownload(id, title, year=null, scheduledAt=null, audioLangs=null, subLangs=null, poster=null) {
  if (!can('DOWNLOAD')) {
    const ok = await submitRequest({
      source: currentSource, media_type: 'film', external_id: String(id),
      title, year, poster,
      audio_languages: audioLangs || ['ita'],
      subtitle_languages: subLangs || [],
    }, title);
    if (ok) showPage('my-requests');
    return;
  }
  try {
    const endpoint = scheduledAt ? '/api/download/schedule/film' : '/api/download/film';
    const body = {
      id, title, year,
      audio_languages: audioLangs || ['ita'],
      subtitle_languages: subLangs || ['ita', 'eng'],
    };
    if (scheduledAt) body.scheduled_at = scheduledAt;
    await api.post(endpoint, body);
    showToast(scheduledAt
      ? `Programmato: ${title} — ${new Date(scheduledAt).toLocaleString('it-IT')}`
      : `Download avviato: ${title}`, 'success');
    showPage('downloads');
  } catch(e) { showToast(errText(e), 'danger'); }
}

// ── Episode Browser ────────────────────────────────────────────────────────────

let _epCtx = {};




async function startEpisodeDownload(epIndex) {
  const { tvId, tvName, slug, year, scheduledAt, token, episodes, currentSeason, audioLangs, subLangs, poster } = _epCtx;
  const ep = episodes[epIndex];
  const label = `${tvName} S${String(currentSeason).padStart(2,'0')}E${String(ep.n).padStart(2,'0')}`;

  if (!can('DOWNLOAD')) {
    await submitRequest({
      source: 'streamingcommunity', media_type: 'episode', external_id: String(tvId),
      slug, title: tvName, year, poster,
      season: currentSeason, episode_number: String(ep.n),
      audio_languages: audioLangs || ['ita'],
      subtitle_languages: subLangs || [],
    }, label);
    return;
  }

  const endpoint = scheduledAt ? '/api/download/schedule/episode' : '/api/download/episode';
  const body = {
    tv_id: tvId, eps: episodes, ep_index: epIndex, token,
    tv_name: tvName, season: currentSeason, year,
    audio_languages: audioLangs || ['ita'],
    subtitle_languages: subLangs || ['ita', 'eng'],
  };
  if (scheduledAt) body.scheduled_at = scheduledAt;
  try {
    await api.post(endpoint, body);
    showToast(scheduledAt ? `Programmato: ${label}` : `In coda: ${label}`, 'success');
  } catch(e) { showToast(errText(e), 'danger'); }
}

// Whole seasons and whole series are one call: the server lists the episodes
// itself and queues them as a batch. That is also what lets it report the season
// once at the end instead of pinging for every episode.
// modalId is optional: the title page is a page, so there is nothing to
// close behind it. It remains for the callers that are still modals.
async function _startBatch(path, body, modalId) {
  let data;
  try {
    data = await api.post(path, body);
  } catch (e) {
    showToast(errText(e, 'Errore avviando i download'), 'danger');
    return false;
  }
  showToast(
    body.scheduled_at ? `${data.count} episodi programmati` : `${data.count} episodi in coda`,
    'success',
  );
  if (modalId) hideModal(modalId);
  showPage('downloads');
  return true;
}

async function downloadWholeSeason(season) {
  const { tvId, slug, tvName, year, episodes, scheduledAt, audioLangs, subLangs } = _epCtx;
  const label = scheduledAt ? 'Programmare' : 'Aggiungere alla coda';
  if (!await scConfirm(`${label} tutti i ${episodes.length} episodi della stagione ${season}?`)) return;
  await _startBatch('/api/download/season', {
    tv_id: tvId, slug, tv_name: tvName, season, year,
    audio_languages: audioLangs, subtitle_languages: subLangs,
    scheduled_at: scheduledAt || null,
  });
}

async function downloadWholeSeries() {
  const { tvId, slug, tvName, year, scheduledAt, seasonsCount, audioLangs, subLangs } = _epCtx;
  const label = scheduledAt ? 'Programmare' : 'Aggiungere alla coda';
  if (!await scConfirm(`${label} tutte le ${seasonsCount} stagioni?`)) return;
  await _startBatch('/api/download/series', {
    tv_id: tvId, slug, tv_name: tvName, year,
    audio_languages: audioLangs, subtitle_languages: subLangs,
    scheduled_at: scheduledAt || null,
  });
}

// ── Anime Browser (AnimeUnity) ─────────────────────────────────────────────────


async function startAnimeDownload(epIndex) {
  const { animeId, animeName, animeType, animeYear, scheduledAt, episodes, audioLangs, subLangs } = _animeCtx;
  const episode = episodes[epIndex];
  const label = `${animeName} E${episode.number}`;

  if (!can('DOWNLOAD')) {
    await submitRequest({
      source: 'animeunity', media_type: 'anime', external_id: String(animeId),
      title: animeName, year: animeYear, anime_type: animeType,
      episode_number: String(episode.number),
      audio_languages: audioLangs || ['ita'],
      subtitle_languages: subLangs || [],
    }, label);
    return;
  }

  const endpoint = scheduledAt ? '/api/download/schedule/anime' : '/api/download/anime';
  const body = {
    anime_id: animeId, episode, anime_name: animeName, anime_type: animeType, year: animeYear,
    audio_languages: audioLangs || ['ita'],
    subtitle_languages: subLangs || ['ita', 'eng'],
  };
  if (scheduledAt) body.scheduled_at = scheduledAt;
  try {
    await api.post(endpoint, body);
    showToast(scheduledAt ? `Programmato: ${label}` : `In coda: ${label}`, 'success');
  } catch(e) { showToast(errText(e), 'danger'); }
}


async function downloadAllAnime() {
  const { animeId, animeName, animeType, animeYear, episodes, scheduledAt,
          audioLangs, subLangs } = _animeCtx;
  const label = scheduledAt ? 'Programmare' : 'Aggiungere alla coda';
  if (!await scConfirm(`${label} tutti i ${episodes.length} episodi?`)) return;
  await _startBatch('/api/download/anime-all', {
    anime_id: String(animeId), anime_name: animeName, anime_type: animeType,
    year: animeYear,
    audio_languages: audioLangs, subtitle_languages: subLangs,
    scheduled_at: scheduledAt || null,
  });
}


// ── Delegated handlers ───────────────────────────────────────────────────────
//
// The episode button used to name its handler as a string and interpolate the
// index into an attribute. Which of the two it is depends on the title's kind,
// which is state this page already holds, so the branch belongs here and not
// in the markup.

registerActions({
  'tp:back':          () => tpBack(),
  'tp:primary':       () => tpPrimary(),
  'tp:follow':        () => tpToggleFollow(),
  'tp:tab':           d => tpSetTab(d.tab),
  'tp:track':         d => tpToggleTrack(d.which, d.code),
  'tp:trailer':       () => tpPlayTrailer(),
  'tp:season':        d => tpLoadSeason(Number(d.n)),
  'tp:schedule':      (d, el) => tpSetSchedule(el.value),
  'tp:requestSeason': () => tpRequestSeason(),
  'tp:batchSeason':   d => downloadWholeSeason(Number(d.season)),
  'tp:batchAnime':    () => downloadAllAnime(),
  'tp:episode':       d => (_tp.type === 'anime' ? startAnimeDownload : startEpisodeDownload)(Number(d.index)),
});


// The title is a path — kind, id, slug — and the season is the one piece of
// state on the page worth carrying, because changing it is the main thing you
// do here and losing it on reload is what sent you back to S01.
//
// The chosen audio and subtitle tracks are deliberately left out: they are a
// decision about a download you have not started, not a place you are, and a
// link that silently pre-selects someone else's tracks is a link that starts
// the wrong download.
registerPageHash('detail', {
  read: () => (_tp ? {
    extra: [_tp.type, _tp.id, _tp.slug || ''],
    params: { s: _tp.season > 1 ? _tp.season : null },
  } : {}),
});
