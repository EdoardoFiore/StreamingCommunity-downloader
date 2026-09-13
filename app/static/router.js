// Hash routing.
//
// Only the pages that need an address have one: a title, and a settings tab.
// Everything else is still reached through showPage(), which this sits on top
// of rather than replacing.

const ROUTES = [
  {
    // #/title/<movie|tv|anime>/<id>[/<slug>]
    re: /^#\/title\/(movie|tv|anime)\/([^/]+)(?:\/([^/]*))?$/,
    parse: m => ({ page: 'detail', type: m[1],
                   id: decodeURIComponent(m[2]), slug: decodeURIComponent(m[3] || '') }),
  },
  {
    // #/settings[/<tab>]
    re: /^#\/settings(?:\/([a-z]+))?$/,
    parse: m => ({ page: 'settings', tab: m[1] || null }),
  },
];

function parseHash() {
  const hash = location.hash || '';
  for (const { re, parse } of ROUTES) {
    const m = hash.match(re);
    if (m) return parse(m);
  }
  return null;
}

function routeFromHash() {
  const route = parseHash();

  if (route?.page === 'detail') {
    // Re-entering the same title must not refetch it.
    if (_tp && _tp.type === route.type && String(_tp.id) === route.id) { showPage('detail'); return; }
    loadTitlePage(route);
    return;
  }

  if (route?.page === 'settings') {
    openSettingsPage(route.tab);
    return;
  }

  // The hash was cleared - by the back button, or by showPage releasing it.
  // Only act if an addressed page is what is currently on screen, so this
  // cannot yank someone off a plain page they navigated to normally.
  const onAddressed = ['page-detail', 'page-settings']
    .some(id => document.getElementById(id)?.style.display !== 'none');
  if (onAddressed) {
    _tp = null;
    showPage(defaultPage());
  }
}

window.addEventListener('hashchange', routeFromHash);
// A cold load may already name one. showPage(defaultPage()) has run by then,
// so this only takes over when there is something to take.
window.addEventListener('DOMContentLoaded', () => { if (parseHash()) routeFromHash(); });
