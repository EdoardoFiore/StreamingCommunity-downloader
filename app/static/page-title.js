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
  return `#/title/${kind}/${encodeURIComponent(item.id)}/${encodeURIComponent(item.slug || '')}`;
}

// From a result card or a shelf.
function openTitle(idx) {
  const item = _searchResults[idx];
  if (!item) return;
  _tpSeed = item;                     // spares the first render a round trip
  location.hash = titleHash(item);
}
let _tpSeed = null;

function _tpParseHash() {
  const m = (location.hash || '').match(/^#\/title\/(movie|tv|anime)\/([^/]+)(?:\/([^/]*))?$/);
  if (!m) return null;
  return { type: m[1], id: decodeURIComponent(m[2]), slug: decodeURIComponent(m[3] || '') };
}

// Every call below carries the site version, and it is fetched asynchronously
// at boot - so a pasted link can arrive before it is known. It used to be sent
// empty and the endpoint answered "field required".
async function _tpEnsureVersion() {
  if (currentVersion) return;
  try {
    const d = await api.get('/api/domain');
    currentDomain = currentDomain || d.domain || '';
    currentVersion = d.version || '';
  } catch { /* the calls that follow report the real failure */ }
}

async function loadTitlePage(route) {
  const token = ++_tpToken;
  await _tpEnsureVersion();
  if (token !== _tpToken) return;
  const seed = _tpSeed; _tpSeed = null;

  _tp = {
    ...route,
    name: seed?.name || '',
    year: seed ? itemYear(seed) : null,
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
    season: 1,
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

  if (route.type !== 'anime') {
    const q = { slug: route.slug, version: currentVersion || '' };
    // Metadata and the track list are independent: one reads the title page,
    // the other reaches the stream host. Neither waits on the other.
    api.get(`/api/metadata/${route.type}/${route.id}`, q)
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

  if (route.type === 'tv') await tpLoadSeason(1, token);
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
  if (!_tp.seasonsCount && meta.seasons_count) _tp.seasonsCount = meta.seasons_count;
}

// ── Episodes ──────────────────────────────────────────────────────────────────

async function tpLoadSeason(n, token = _tpToken) {
  _tp.season = n;
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
    `<button class="th-back" onclick="tpBack()" title="Torna alla ricerca">
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
  const scope = _tp.type === 'movie' ? ''
    : (_tp.type === 'anime' ? ' tutti gli episodi'
    : (_tp.seasonsCount > 1 ? ' la serie' : ' la stagione'));
  const label = `${verb}${scope}`;
  const icon = wants ? 'ti-send' : (_tp.seasonsCount > 1 && _tp.type === 'tv' ? 'ti-stack-2' : 'ti-download');
  const followable = _tp.type !== 'movie';

  // Scheduling is part of the download privilege: a requester picks tracks and
  // an approver decides when it runs.
  const sched = can('DOWNLOAD') ? `
    <label class="th-sched" title="Lascia vuoto per scaricare subito">
      <i class="ti ti-clock"></i>
      <input type="datetime-local" id="th-sched-at" value="${escapeHtml(_tp.scheduledRaw)}"
             onchange="tpSetSchedule(this.value)">
    </label>` : '';

  document.getElementById('th-cta').innerHTML = `
    ${sched}
    <button class="btn btn-primary" onclick="tpPrimary()">
      <i class="ti ${icon} me-1"></i>${label}
    </button>
    <!-- Left bare: _renderFollowButton owns this button's class and label,
         and rewriting them here would fight it. -->
    ${followable ? `<button id="th-follow-btn" onclick="tpToggleFollow()"></button>` : ''}
    <button class="th-icon-btn" onclick="tpSetTab('tracks')" title="Scegli audio e sottotitoli">
      <i class="ti ti-adjustments"></i>
    </button>`;
  if (followable) tpRefreshFollow();
}

function _tpRenderTabsBar() {
  document.getElementById('th-tabs').innerHTML = _tpTabs()
    .map(([k, label]) =>
      `<button class="th-tab${_tp.tab === k ? ' active' : ''}" data-tab="${k}" role="tab"
        onclick="tpSetTab('${k}')">${escapeHtml(label)}</button>`).join('');
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
          onclick="tpToggleTrack('${which}','${escapeHtml(t.code)}')">${escapeHtml(langName(t.code))}</button>`).join('')
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
    `<button class="th-trailer-facade" ${still} onclick="tpPlayTrailer()" aria-label="Riproduci il trailer"></button>`;
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
          onclick="tpLoadSeason(${n})">S${String(n).padStart(2, '0')}</button>`).join('')}</div>` : '';

  // Batching is a download privilege; a requester asks episode by episode.
  // The verb follows the schedule field, so the button says what will happen.
  const verb = _tp.scheduledAt ? 'Programma' : 'Scarica';
  let batch = '';
  if (can('DOWNLOAD') && _tp.episodes?.length) {
    batch = _tp.type === 'anime'
      ? `<button class="btn btn-sm btn-outline-primary" onclick="downloadAllAnime()">
           <i class="ti ti-download me-1"></i>${verb} tutti gli episodi</button>`
      : `<button class="btn btn-sm btn-outline-primary" onclick="downloadWholeSeason(${_tp.season})">
           <i class="ti ti-download me-1"></i>${verb} la stagione</button>`;
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
  const fn = _tp.type === 'anime' ? 'startAnimeDownload' : 'startEpisodeDownload';
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
        <button class="btn btn-sm ${wants ? 'btn-outline-primary' : 'btn-primary'}" onclick="${fn}(${i})">
          <i class="ti ${wants ? 'ti-send' : 'ti-download'} me-1"></i>${wants ? 'Richiedi' : 'Scarica'}
        </button>
      </div>`;
  }).join('');

  el.innerHTML = head + `<div class="th-eps">${rows}</div>`;
}

// ── Actions ───────────────────────────────────────────────────────────────────

function tpPrimary() {
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

// ── Routing ───────────────────────────────────────────────────────────────────
//
// The only page with an address so far. Everything else is still driven by
// showPage(); this hooks the hash so a title can be reloaded, shared, and
// reached with the back button.

function _tpRoute() {
  const route = _tpParseHash();
  if (route) {
    // Re-entering the same title (back out of a tab, say) must not refetch it.
    if (_tp && _tp.type === route.type && String(_tp.id) === route.id) { showPage('detail'); return; }
    loadTitlePage(route);
  } else if (_tp) {
    _tp = null;
    showPage(defaultPage());
  }
}

window.addEventListener('hashchange', _tpRoute);
// On a cold load the hash may already name a title. showPage(defaultPage())
// has run by then, so this just takes over when there is something to take.
window.addEventListener('DOMContentLoaded', () => { if (_tpParseHash()) _tpRoute(); });

// Back to wherever the visitor came from, and to the search page when they
// arrived by pasted link and there is no history to go back to.
function tpBack() {
  if (history.length > 1 && document.referrer !== '') { history.back(); return; }
  location.hash = '';
  showPage('search');
}
