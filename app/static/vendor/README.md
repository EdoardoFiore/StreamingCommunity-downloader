# Vendored front-end assets

These were served from jsDelivr and Google Fonts until they were copied here.
Two reasons: a self-hosted panel with no outbound internet lost all of its
styling, and every page load announced the panel to two third parties with its
own URL in the referer.

Nothing here is edited except where noted below. To update a library, drop the
new release in a **new directory carrying its version** and repoint the
`{{ asset(...) }}` links — the font files inside these directories are fetched
by relative `url()` from within a stylesheet, so they never pass through
`asset()` and get no content hash of their own. The version in the directory
name is what keeps a browser from pairing a new stylesheet with an old font.

| Directory | Version | Upstream | Licence |
|---|---|---|---|
| `tabler-1.4.0/` | 1.4.0 | `https://cdn.jsdelivr.net/npm/@tabler/core@1.4.0/dist/{css/tabler.min.css,js/tabler.min.js}` | MIT |
| `tabler-icons-3.19.0/` | 3.19.0 | `https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.19.0/dist/tabler-icons.min.css` + `dist/fonts/` | MIT |
| `fonts/` | — | `https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Karla:ital,wght@0,300;0,400;0,500;0,600;0,700;1,400&display=swap` | SIL Open Font License 1.1 |

## The two edits

**`tabler-icons-3.19.0/tabler-icons.min.css`** — the `@font-face` shipped
`woff2`, `woff` and `truetype`. Only the `woff2` is kept, and the other two
files are not vendored at all: they are 3.7 MB, and every browser that can run
this panel's JavaScript has supported `woff2` for a decade.

**`fonts/fonts.css`** — Google's stylesheet, with each `https://fonts.gstatic.com/…`
rewritten to `./files/…`. The file names carry Google's own content hashes, so
they are already unique per revision. Fetch it with a desktop Chrome user agent
or Google serves `ttf` instead of `woff2`.
