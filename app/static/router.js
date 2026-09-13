// Hash routing.
//
// Every page has an address, and an address carries enough to rebuild what you
// were looking at: which page, and that page's own state — the query you
// typed, the filter you picked, the season you were on. Reload, paste a link,
// or press Back, and you land where you were rather than on the default page.
//
// Grammar: #/<page>[/<extra>...][?<k=v&...>]
//
//   #/downloads?f=error
//   #/search?q=dune&src=animeunity&type=tv
//   #/title/tv/1234/nome-serie?s=3
//   #/settings/nomi
//
// Two rules make this loop-free, and both matter:
//
//   - Writing the address uses history.replaceState, which does NOT fire
//     hashchange. That is what lets showPage() and every filter write the
//     address without routing straight back into themselves.
//   - Navigating assigns location.hash, which DOES fire hashchange, and the
//     routing happens there. So there is exactly one path into a page change.

// The word in the address is not always the internal page id: "detail" is the
// name of a div, "title" is what belongs in a URL.
const HASH_TO_PAGE = { title: 'detail' };
const PAGE_TO_HASH = { detail: 'title' };

// Each page says how to read its own state out and put it back. Registered by
// the page, not listed here, for the same reason actions are: the router has
// no business knowing what a filter is called.
//
//   read()  -> { extra: [...], params: {...} }  (both optional)
//   apply(params, extra)  -> set state, WITHOUT rendering; the loader that
//                            showPage() fires does the rendering.
const PAGE_HASH = Object.create(null);

function registerPageHash(page, spec) { PAGE_HASH[page] = spec; }

// Every list page has the same strip of pills, and each one had its own copy
// of this line.
function setActivePill(containerId, filter) {
  document.querySelectorAll(`#${containerId} .queue-filter`).forEach(el =>
    el.classList.toggle('active', el.dataset.filter === filter));
}

function parseHash() {
  const raw = (location.hash || '').replace(/^#\/?/, '');
  if (!raw) return null;
  const [path, query] = raw.split('?');
  const parts = path.split('/').filter(Boolean).map(decodeURIComponent);
  if (!parts.length) return null;
  const params = {};
  new URLSearchParams(query || '').forEach((value, key) => { params[key] = value; });
  return { page: HASH_TO_PAGE[parts[0]] || parts[0], extra: parts.slice(1), params };
}

function hashFor(page, extra = [], params = {}) {
  const path = [PAGE_TO_HASH[page] || page, ...extra]
    .filter(part => part !== null && part !== undefined && part !== '')
    .map(part => encodeURIComponent(String(part)))
    .join('/');
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    // A default is absent from the address rather than spelled out: the
    // common case should produce the short link.
    if (value !== null && value !== undefined && value !== '') query.set(key, String(value));
  }
  const suffix = query.toString();
  return `#/${path}${suffix ? `?${suffix}` : ''}`;
}

// Which page is on screen. Set by showPage, so syncHash() does not have to
// re-derive it from the DOM every time a filter moves.
let _routePage = null;

/** Write the current page and its state into the address. Fires nothing. */
function syncHash(page = _routePage) {
  if (!page) return;
  const state = PAGE_HASH[page]?.read?.() || {};
  const next = hashFor(page, state.extra || [], state.params || {});
  if (next !== location.hash) history.replaceState(null, '', next);
}

/** Go to a page. Assigns the hash, so the routing happens in one place. */
function navigate(page, extra = [], params = {}) {
  const next = hashFor(page, extra, params);
  if (next === location.hash) routeFromHash();   // same address, still re-enter
  else location.hash = next;
}

// A page is reachable when its nav link is. initAuth() has already hidden the
// ones this user has no permission for, so the sidebar is the single source of
// truth — rather than a second copy of the permission rule living here, which
// is exactly how three files came to disagree once before.
function pageIsReachable(page) {
  const link = document.querySelector(`.nav-link[data-page="${page}"]`);
  // detail and settings have no nav entry and carry their own gates.
  if (!link) return true;
  const item = link.closest('li');
  return !item || item.style.display !== 'none';
}

function routeFromHash() {
  const route = parseHash();

  // No address yet — a cold load, or Back past the first page.
  if (!route) { showPage(defaultPage()); return; }

  if (route.page === 'detail') {
    const [type, id, slug] = route.extra;
    if (!type || !id) { showPage(defaultPage()); return; }
    const season = parseInt(route.params.s, 10);
    // Re-entering the same title must not refetch it.
    if (_tp && _tp.type === type && String(_tp.id) === id) {
      showPage('detail');
      if (season > 0 && season !== _tp.season) tpLoadSeason(season);
      return;
    }
    loadTitlePage({ page: 'detail', type, id, slug: slug || '',
                    season: season > 0 ? season : 1 });
    return;
  }

  if (route.page === 'settings') { openSettingsPage(route.extra[0] || null); return; }

  // A link to a page this user cannot open lands them somewhere real, with the
  // address corrected, rather than on a blank pane fed by 403s.
  if (!pageIsReachable(route.page)) { showPage(defaultPage()); return; }

  showPage(route.page, route.params);
}

window.addEventListener('hashchange', routeFromHash);

// No DOMContentLoaded listener here on purpose. This file loads before app.js,
// so its listener would run before initAuth() — with no permissions resolved
// yet — and then app.js's boot would finish by calling showPage(defaultPage())
// and overwrite whatever the address had asked for. A pasted title link lost
// that race and landed on the search page. app.js's boot calls routeFromHash()
// as its last step instead, which is both ordered and the only caller.
