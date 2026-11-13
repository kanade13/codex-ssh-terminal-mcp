Pinned browser assets, downloaded from the official npm registry with
`npm pack --ignore-scripts`. Both packages use the MIT license; the original
license files are retained beside each package.

- `@xterm/xterm` 6.0.0
  - https://registry.npmjs.org/@xterm/xterm/-/xterm-6.0.0.tgz
  - npm integrity: `sha512-TQwDdQGtwwDt+2cgKDLn0IRaSxYu1tSUjgKarSDkUM0ZNiSRXFpjxEsvc/Zgc5kq5omJ+V0a8/kIM2WD3sMOYg==`
- `@xterm/addon-fit` 0.11.0
  - https://registry.npmjs.org/@xterm/addon-fit/-/addon-fit-0.11.0.tgz
  - npm integrity: `sha512-jYcgT6xtVYhnhgxh3QgYDnnNMYTcf8ElbxxFzX0IZo+vabQqSPAjC3c1wJrKB5E19VwQei89QCiZZP86DCPF7g==`

Run `node scripts/build_terminal_assets.cjs` to rebuild the two static bundles.
The MCP App contains the renderer and stylesheet inline and makes no CDN
requests. The standalone page loads `terminal-engine.js` from the local bridge.
