# Front-end tests

Vitest unit tests for the site's hand-written JavaScript — the places with
non-trivial client-side logic worth testing in isolation:

- **`meso_athlete.test.js`** — the athlete session logger
  (`app/store_project/static/js/meso_athlete.js`): the offline write queue
  (stash on network failure, one save per session, replay on reconnect) and the
  save/flush state machine.
- **`timer.test.js`** — the interval timer
  (`app/store_project/static/js/timer.js`): the timeline engine that compiles a
  work/rest or EMOM setup into segments and answers "where am I?" / "what cue
  is owed?" each second, plus a DOM pass over the controller on a fake clock.

## Running

```bash
npm install   # once
npm test            # or: just test-js
npm run test:watch
npm run test:coverage
```

## How the source is imported

These files are served to the browser as classic `<script>`s (Alpine.js), so
they can't use ES module `export`. Each defines a factory (`createLogger()`) or
an init function (`initTimer()`), wires itself up when running in a browser, and
exposes the internals via `module.exports` for Node-based runners. The tests
import those and exercise them directly — no Alpine runtime, and only the markup
a test needs. DOM-bound helpers (`$nextTick`, `$refs`) are stubbed per test.

## CI & deploys

These tests run in the **Frontend CI** workflow (`.github/workflows/frontend.yml`),
kept separate from **Django CI**. Production deploys key off Django CI, and this
directory (plus `package.json` / `vitest.config.js`) is in Django CI's
`paths-ignore`, so test-only changes gate the PR without triggering a deploy.
Changes to the served JS under `app/store_project/static/js/**` still run Django
CI and deploy, since they ship via `collectstatic`.
