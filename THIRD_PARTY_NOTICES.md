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

The build does not bundle any other third-party runtime code.  The server and
client use only the Python standard library.
