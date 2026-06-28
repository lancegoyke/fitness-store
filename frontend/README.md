# Front-end tests

Vitest unit tests for the meso app's hand-written JavaScript — the only place in
the repo with non-trivial client-side logic worth testing in isolation:

- **`meso_athlete.test.js`** — the athlete session logger
  (`app/store_project/static/js/meso_athlete.js`): the offline write queue
  (stash on network failure, one save per session, replay on reconnect) and the
  save/flush state machine.
- **`meso.test.js`** — the designer's agent poll loop
  (`app/store_project/static/js/meso.js`): `pollBatch` (drafting → resolve /
  fail / timeout / error) plus the response-shaping helpers.

## Running

```bash
npm install   # once
npm test            # or: just test-js
npm run test:watch
npm run test:coverage
```

## How the source is imported

The meso JS files are served to the browser as classic `<script>`s (Alpine.js),
so they can't use ES module `export`. Each file defines a factory
(`createLogger()` / `createMeso()`), registers it on `alpine:init` when running
in a browser, and exposes it via `module.exports` for Node-based runners. The
tests import that factory and exercise the methods directly — no Alpine runtime,
no DOM rendering. DOM-bound helpers (`$nextTick`, `$refs`) are stubbed per test.

## CI & deploys

These tests run in the **Frontend CI** workflow (`.github/workflows/frontend.yml`),
kept separate from **Django CI**. Production deploys key off Django CI, and this
directory (plus `package.json` / `vitest.config.js`) is in Django CI's
`paths-ignore`, so test-only changes gate the PR without triggering a deploy.
Changes to the served JS under `app/store_project/static/js/**` still run Django
CI and deploy, since they ship via `collectstatic`.
