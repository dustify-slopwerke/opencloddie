# Third-party notices

opencloddie itself is MIT licensed (see `LICENSE`).  The following third-party
assets are vendored in the web UI and remain under their own licenses.

## xterm.js

- `@xterm/xterm` 5.5.0 — `opencloddie/web/vendor/xterm.js`, `xterm.css`
- `@xterm/addon-fit` 0.10.0 — `opencloddie/web/vendor/addon-fit.js`
- `@xterm/addon-web-links` 0.11.0 — `opencloddie/web/vendor/addon-web-links.js`
- `@xterm/addon-webgl` 0.18.0 — `opencloddie/web/vendor/addon-webgl.js`

License: MIT — <https://github.com/xtermjs/xterm.js/blob/master/LICENSE>

Source: <https://cdn.jsdelivr.net/npm/@xterm/>

## marked

- `marked` 12.0.2 — `opencloddie/web/vendor/marked.min.js`

License: MIT — <https://github.com/markedjs/marked/blob/master/LICENSE.md>

Source: <https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js>

## DOMPurify

- `dompurify` 3.1.6 — `opencloddie/web/vendor/purify.min.js`

License: Apache-2.0 OR MPL-2.0 — <https://github.com/cure53/DOMPurify/blob/main/LICENSE>

Source: <https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.1.6/purify.min.js>

The build does not bundle any other third-party runtime code.  The server and
client use only the Python standard library.
