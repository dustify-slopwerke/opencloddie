# Vendored browser assets

These files are served from `/vendor/` so the web UI has no CDN or build-step
dependency.

| File | Package | Version | License |
| --- | --- | --- | --- |
| `xterm.js` / `xterm.css` | `@xterm/xterm` | 5.5.0 | MIT |
| `addon-fit.js` | `@xterm/addon-fit` | 0.10.0 | MIT |
| `addon-web-links.js` | `@xterm/addon-web-links` | 0.11.0 | MIT |
| `addon-webgl.js` | `@xterm/addon-webgl` | 0.18.0 | MIT |
| `marked.min.js` | `marked` | 12.0.2 | MIT |
| `purify.min.js` | `dompurify` | 3.1.6 | Apache-2.0 OR MPL-2.0 |

xterm source: <https://cdn.jsdelivr.net/npm/@xterm/>.  Markdown source:
<https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js>.
Sanitizer source:
<https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.1.6/purify.min.js>.
Upgrade by downloading the same paths from jsDelivr/cdnjs and updating this
table.  `chat-render.js` falls back to a small safe Markdown renderer if either
library fails to load.
