/* StreamingCommunity Web Panel — app.js */

// ── State ──────────────────────────────────────────────────────────────────────
let currentDomain = '';
let currentVersion = '';
let currentSource = 'streamingcommunity'; // 'streamingcommunity' | 'animeunity'
let _searchResults = [];
let _libraries = [];
let _jobPhases = {};      // job_id → current phase string
const _jobs = new Map();  // job_id → job dict (source of truth)
let _animeCtx = {};       // context for anime episode browser
let _requestStatus = {};  // external_id → { id, status } for the result cards

function itemYear(item) {
  const d = item.release_date || item.last_air_date || '';
  return d ? d.slice(0, 4) : null;
}

// ── Init ───────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
  if (!await initAuth()) return;
  if (can('REQUEST') || can('DOWNLOAD') || can('MANAGE_SETTINGS')) {
    await loadDomainStatus();
    loadDomainCandidate();
  }
  if (can('MANAGE_SETTINGS')) await Promise.all([loadLibraries(), loadPerfSettings()]);
  if (can('DOWNLOAD') || can('MANAGE_REQUESTS')) {
    connectGlobalStream();
  } else {
    // Without access to the job stream there is nothing to push on, so the bell
    // polls instead. Cheap: one indexed count per minute.
    setInterval(refreshNotifications, 60000);
  }
  if (can('VIEW_LIBRARY')) { setupFileManager(); loadSidebarDisk(); }
  setupSettingsTabs();
  setupSearchDebounce();
  renderSearchFilters();
  refreshNotifications();
  refreshQueueBadge();
  // Last, and the only entry point into routing at boot: permissions are
  // resolved by now, so an address naming a page this user cannot open falls
  // back correctly instead of racing initAuth.
  routeFromHash();
});

function defaultPage() {
  if (can('REQUEST') || can('DOWNLOAD')) return 'search';
  if (can('MANAGE_REQUESTS')) return 'requests';
  if (can('VIEW_LIBRARY')) return 'files';
  return 'search';
}

// ── Domain ─────────────────────────────────────────────────────────────────────

async function loadDomainStatus() {
  try {
    const data = await api.get('/api/domain');
    currentDomain = data.domain || '';
    currentVersion = data.version || '';
    const badge = document.getElementById('domain-badge');
    if (data.valid) {
      badge.className = 'badge bg-success';
      badge.textContent = currentDomain;
    } else {
      badge.className = 'badge bg-danger';
      badge.textContent = 'Domain non configurato';
      openSettings();
    }
  } catch(e) { console.error('loadDomainStatus:', e); }
}

// ── Source domain moved ────────────────────────────────────────────────────────
//
// The panel proposes; a person applies. See app/core/domain_recovery.py for why
// adopting a domain published on a page we do not control is not something that
// happens quietly.

let _domainCandidate = null;

async function loadDomainCandidate() {
  try {
    const data = await api.get('/api/domain/candidate');
    _domainCandidate = data.candidate || null;
    renderDomainBanner();
  } catch (e) { /* a missing banner is not worth a console error */ }
}

function renderDomainBanner() {
  const banner = document.getElementById('domain-banner');
  if (!banner) return;
  if (!_domainCandidate) { banner.style.display = 'none'; return; }
  document.getElementById('domain-banner-host').textContent = _domainCandidate.host;
  banner.style.display = '';
}

async function applyDomainCandidate() {
  if (!_domainCandidate) return;
  const btn = document.getElementById('domain-banner-apply');
  btn.disabled = true;
  try {
    // The host is echoed back as a confirmation token: the server compares it
    // with what it found and refuses a mismatch rather than trusting it.
    const data = await api.post('/api/domain/candidate/apply', {domain: _domainCandidate.host});
    showToast(`Dominio aggiornato: ${data.domain}`, 'success');
    _domainCandidate = null;
    renderDomainBanner();
    await loadDomainStatus();
  } catch (e) {
    showToast(errText(e, 'Impossibile applicare il dominio'), 'danger');
  } finally { btn.disabled = false; }
}

async function dismissDomainCandidate() {
  if (!await scConfirm('Ignorare il dominio trovato? Il pannello resta sul dominio attuale.')) return;
  try {
    await api.post('/api/domain/candidate/dismiss');
  } catch (e) { /* clearing a banner is best effort */ }
  _domainCandidate = null;
  renderDomainBanner();
}

// The banner is the shell's, not the search page's: it is about the source
// every page reads from, and it happens to be rendered above the first one.
registerActions({
  'domain:apply':   () => applyDomainCandidate(),
  'domain:dismiss': () => dismissDomainCandidate(),
  // The sidebar's nav. The page name rides on the same data-page the active
  // highlight already reads, so the two cannot disagree about which link is
  // which. Through navigate(), not showPage(), so a click leaves an address
  // behind and Back works between sections.
  'nav':            d => navigate(d.page),
});

// ── Navigation ─────────────────────────────────────────────────────────────────

// `params` arrives from the address. It is applied before the loaders run, so
// a page restored from a link renders its own state once rather than rendering
// the default and then correcting itself.
function showPage(page, params = null) {
  // Close mobile menu if open
  const mobileMenu = document.getElementById('sidebar-menu');
  if (mobileMenu && mobileMenu.classList.contains('show')) {
    mobileMenu.classList.remove('show');
  }
  if (params) PAGE_HASH[page]?.apply?.(params);
  ['search','downloads','files','requests','my-requests','watches','users','detail','settings'].forEach(p => {
    const el = document.getElementById(`page-${p}`);
    if (el) el.style.display = p === page ? '' : 'none';
  });
  document.getElementById('page-title').textContent = {
    search:'Cerca', downloads:'Download', files:'File',
    requests:'Coda richieste', 'my-requests':'Le mie richieste',
    watches:'Serie seguite', users:'Utenti', detail:'', settings:'Impostazioni',
  }[page] ?? 'Cerca';
  document.querySelectorAll('.nav-link[data-page]').forEach(el =>
    el.classList.toggle('active', el.dataset.page === page));
  // Set before the loaders, so anything they call that writes the address —
  // a filter applying itself, a season loading — knows which page it is on.
  _routePage = page;
  if (page === 'search') searchPageEnter();
  if (page === 'downloads') refreshJobs();
  if (page === 'files') loadFiles();
  if (page === 'requests') loadRequestQueue();
  if (page === 'my-requests') loadMyRequests();
  if (page === 'watches') loadWatches();
  if (page === 'users') loadUsersPage();
  // Every page has an address now. This used to do the opposite — strip the
  // hash on the way out of the two pages that had one — because they were the
  // only two, and leaving a stale title in the bar meant the next reload
  // reopened a title the user had left. With all of them addressed there is
  // no stale case to clean up: the address simply follows the page.
  //
  // replaceState, never location.hash: assigning fires hashchange and would
  // route straight back into here.
  syncHash(page);
}

// ── Spazio disco, nella sidebar ──────────────────────────────────────────────

// The library volume, in the sidebar, under the source it fills.
//
// One volume only: the libraries are nearly always folders on one mount, and
// three identical bars said nothing. Where they genuinely differ, the fullest
// is the one worth warning about.
async function loadSidebarDisk() {
  const box = document.getElementById('sidebar-disk');
  if (!box || !can('VIEW_LIBRARY')) return;
  try {
    const data = await api.get('/api/files/disk-usage');
    const volumes = (data.volumes || []).filter(v => v.total > 0);
    if (!volumes.length) { box.hidden = true; return; }
    const v = volumes.reduce((a, b) => (a.used / a.total >= b.used / b.total ? a : b));
    const pct = Math.min(100, Math.round(v.used / v.total * 100));
    box.hidden = false;
    const fill = document.getElementById('sb-disk-fill');
    fill.style.width = `${pct}%`;
    // Colour only where it means something: a nearly full volume is the one
    // fact here worth interrupting for.
    fill.className = diskLevel(pct);
    document.getElementById('sb-disk-text').textContent =
      `${fmtBytes(v.free)} liberi di ${fmtBytes(v.total)}`;
    box.title = `${pct}% occupato — ${(v.paths || []).join(', ')}`;
  } catch {
    box.hidden = true;    // a figure nobody can read is worse than none
  }
}

// ── Vocabolario notifiche ────────────────────────────────────────────────────
//
// Mirrors notify.ALL_EVENTS on the server. The bell reads the icons; the
// per-channel picker in Impostazioni reads the labels and the grouping.

const NOTIFICATION_ICONS = {
  request_created: 'ti-inbox',
  request_joined: 'ti-users',
  request_approved: 'ti-circle-check',
  request_denied: 'ti-circle-x',
  request_downloading: 'ti-download',
  request_completed: 'ti-device-tv',
  request_failed: 'ti-alert-triangle',
  request_needs_attention: 'ti-alert-circle',
  request_available: 'ti-library',
  download_completed: 'ti-circle-check',
  download_failed: 'ti-alert-triangle',
  download_batch_completed: 'ti-checkbox',
  download_batch_failed: 'ti-alert-octagon',
  watch_needs_approval: 'ti-bell-question',
  watch_auto_approved: 'ti-bell-check',
  source_domain_found: 'ti-world-search',
  source_domain_applied: 'ti-world-check',
  hook_failed: 'ti-webhook-off',
};

const NOTIFICATION_LABELS = {
  request_created: 'Nuova richiesta',
  request_joined: 'Richiesta già presente',
  request_approved: 'Richiesta approvata',
  request_denied: 'Richiesta rifiutata',
  request_downloading: 'Richiesta in download',
  request_completed: 'Richiesta completata',
  request_failed: 'Richiesta fallita',
  request_needs_attention: 'Richiesta da verificare',
  request_available: 'Già in libreria',
  download_completed: 'Download completato',
  download_failed: 'Download fallito',
  download_batch_completed: 'Stagione o serie completata',
  download_batch_failed: 'Stagione o serie fallita',
  watch_needs_approval: 'Serie seguita da approvare',
  watch_auto_approved: 'Serie approvata',
  source_domain_found: 'Nuovo dominio trovato',
  source_domain_applied: 'Dominio aggiornato',
  hook_failed: 'Hook fallito',
};

// Explicit order, so the picker does not depend on object key order.
const NOTIFICATION_EVENT_GROUPS = [
  {
    label: 'Richieste',
    events: ['request_created', 'request_joined', 'request_approved', 'request_denied',
             'request_downloading', 'request_completed', 'request_failed',
             'request_needs_attention', 'request_available'],
  },
  {
    label: 'Download diretti',
    events: ['download_completed', 'download_failed',
             'download_batch_completed', 'download_batch_failed'],
  },
  {
    label: 'Serie seguite',
    events: ['watch_needs_approval', 'watch_auto_approved'],
  },
  {
    label: 'Sorgente',
    events: ['source_domain_found', 'source_domain_applied'],
  },
  {
    label: 'Hook',
    events: ['hook_failed'],
  },
];

const ALL_NOTIFICATION_EVENTS = NOTIFICATION_EVENT_GROUPS.flatMap(g => g.events);
