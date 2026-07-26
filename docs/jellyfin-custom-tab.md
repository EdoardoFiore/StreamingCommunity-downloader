# Embedding the panel as a Jellyfin custom tab

Jellyfin can show an arbitrary page as a tab in its own UI. Putting the panel there works, but a
cross-site iframe changes two things: whether the session cookie is sent at all, and whether the
visitor has to log in a second time.

## The cookie

A `SameSite=Lax` session cookie — the default, and correct for every normal deployment — is not sent
on requests made from inside a cross-site iframe. Framed on a different domain, a
port-with-different-scheme, or behind a different reverse proxy than the panel's own origin, every
request inside the frame looks logged out and login bounces straight back to itself: an apparent
infinite loop.

Two ways to fix it:

- **Reverse-proxy the panel under the same site and scheme as the embedding page** (e.g. as a subpath
  of your Jellyfin domain). No configuration change needed — the iframe stops being cross-site.
- **Set `COOKIE_SAMESITE=none`**, together with `COOKIE_SECURE=1`. The panel must be served over
  HTTPS, since browsers reject a `SameSite=None` cookie that isn't also `Secure`. This is safe to
  relax: CSRF protection here does not depend on `SameSite` — every state-changing request already
  carries a double-submit token no outside origin can read.

Chrome and other Chromium-based browsers separately enforce **Private Network Access**: a page loaded
from a public address cannot embed a frame that resolves to a private/LAN address, and vice versa.
Both the Jellyfin page and the panel have to resolve to addresses in the *same* tier — both public or
both private — from the browser's point of view, or the iframe fails with "the connection was
blocked".

## Skipping the second login

The panel is already embedded in a page where Jellyfin itself is signed in, so the visitor should not
have to log in again. `POST /api/auth/jellyfin-token` trades an already-issued Jellyfin access token
for a panel session; the panel's login page listens for one over `postMessage` and, if it gets one,
skips straight past the login form.

The token has to come from the parent frame, since that is the only place it exists. Add this to the
custom tab HTML that holds the iframe — Jellyfin's own page, not the panel — replacing
`PANEL_ORIGIN` with the panel's exact origin:

```html
<script>
(function () {
  var PANEL_ORIGIN = 'https://request.example.com'; // ← your panel's origin, no trailing slash
  window.addEventListener('message', function (event) {
    if (event.origin !== PANEL_ORIGIN) return;
    if (!event.data || event.data.type !== 'sc-panel-ready') return;
    try {
      var token = window.ApiClient.accessToken();
      if (token) event.source.postMessage({ type: 'sc-panel-jellyfin-token', token: token }, PANEL_ORIGIN);
    } catch (e) { /* ApiClient not ready yet */ }
  });
})();
</script>
```

The token is never trusted blindly: the panel checks it live against the configured Jellyfin server
before issuing a session, exactly as it would a password. Nothing changes for a Jellyfin user who has
not been imported into the panel, or who visits the panel outside the iframe — they still see the
normal login form.
