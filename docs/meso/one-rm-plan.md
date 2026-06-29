# Meso — persisted estimated 1RM (S2 follow-up)

**Status:** Phase 1 built · branch `meso-one-rm-phase1`
**Context:** the deferred follow-up flagged at the end of the **units & RPE/%1RM
slice (S2)**. Phase 2b gave the athlete a client-side estimated-1RM helper so a
"75%" target could be turned into a bar load — but the estimate lived only in the
browser's `localStorage`, keyed by prescription id: **per-device, lost on a
device change, and invisible to the coach.** This slice promotes it to a real,
**auto-derived** row.

See [`units-rpe-plan.md`](./units-rpe-plan.md) for the slice it completes and
[`decisions.md`](./decisions.md) for the standing decisions.

---

## The idea

A %1RM target is an *intensity*, not a weight; converting it needs the athlete's
one-rep max. The best, least-effort source of that number is **what the athlete
already lifted** — the `LoggedSet` rows the athlete slice produces. So:

> the persisted 1RM = the **best Epley estimate** across the athlete's *completed*
> logged sets for that lift, refreshed every time they log.

No manual coach/athlete entry is required for it to work (though a per-device
typed override still layers on top — see below). It is a property of the
**athlete + lift**, global across plans and coaches, so the same number powers
the athlete's logger on any device *and* shows the coach what a %1RM resolves to
when they design.

## Model — `AthleteOneRm`

One row per `(athlete, lift)`. Lift identity follows the **B4 hybrid** exactly
(`serializers._exercise_key`): a catalog-linked lift by its `Exercise` FK, a
free-text lift by normalized name. A denormalized `key` (`"id:<pk>"` /
`"name:<lower>"`) carries that identity so a single `unique(athlete, key)`
constraint spans both halves — `save()` keeps `key` authoritative so it can
never be hand-set blank (which would collide every lift for an athlete).

| field | meaning |
|-------|---------|
| `athlete` | FK → User |
| `exercise` | nullable FK → catalog `Exercise` (B4) |
| `name` | the lift name (display + free-text identity source) |
| `key` | derived identity, `unique(athlete, key)` |
| `value` | `Decimal(7,2)` — the estimate in `unit` |
| `unit` | kg/lb the value is denominated in |

Migration `0012_athleteonerm` (schema) + `0013_backfill_one_rms` (data): the
backfill derives the best Epley estimate per `(athlete, lift)` from **existing**
completed logs and creates the rows, so history written before this slice (the
demo seed's logged session included) shows a 1RM immediately rather than only
after the athlete logs again. Idempotent (`get_or_create`), so a fresher value
already in place is never clobbered.

## `one_rm.py` — derive / refresh / read

- `epley_one_rm(load, reps)` — the per-set estimate, **mirroring
  `meso_athlete.js`'s `epleyOneRm` exactly** (single rep = the load; `None` for
  the grid's free-text cells). Client and server agree by construction; both are
  pinned by tests.
- `derive_one_rm_values(athlete, keys=…)` — one query over the athlete's `DONE`
  logged sets → `{key: best float}`. A pending "Save progress" draft doesn't
  count (same rule as the results / "last" surfaces).
- `refresh_one_rms(athlete, prescriptions, unit)` — recompute + upsert the rows
  for the lifts in a session. Called from the **log endpoint** after a *done*
  save, so the estimate tracks what the athlete actually did (a heavier set
  raises it; an edit that drops the PR lowers it — it recomputes from scratch).
- `one_rm_values(athlete, prescriptions)` — read the stored rows for a batch of
  prescriptions (one query, by identity so the same lift surfaces against every
  prescription of it), for the two display surfaces.

## Surfaces

- **Athlete logger** (`meso_athlete.js` + `athlete_session.html`): each exercise
  payload carries the derived `one_rm`. `effectiveOneRm` = the athlete's typed
  per-device estimate (still localStorage, `e1rm`) when present, else the derived
  `one_rm` — so the suggested bar load appears **with no manual entry**, and the
  derived value shows as the input placeholder + a "from your logs" hint. A typed
  value still overrides locally (it's their belief about a lift they may not have
  logged yet).
- **Coach designer** (`serialize_plan` + `designer.html`): for an *individual*
  plan, each row gets the athlete's `one_rm`; the grid shows a `1RM: 140 kg`
  badge on a `%1RM` row. A *group* plan has no single athlete, so it carries none.

## Tests

- `test_one_rm.py` (pytest): the identity key + uniqueness, Epley (incl. the
  free-text/out-of-range cases), `derive` (best-across-logs / pending-ignored /
  athlete-scoped / keys filter), `refresh` (create / update-upward / no-usable-set
  left untouched), the read helper, the **log-endpoint integration** (done writes,
  pending doesn't, re-log recomputes downward), the presenter + serializer
  threading, and the **backfill migration** (derives + is idempotent).
- `meso_athlete.test.js` (Vitest): `effectiveOneRm` / `suggestedLoad` precedence
  (derived default, typed override, non-numeric fallback), `usingDerivedOneRm`,
  and `one_rm` hydration.
- `test_seed_demo.py`: the demo's %1RM Box Squat shows Maya's derived 1RM (84).

## Deferred (Phase 2+)

- **Manual entry persisted server-side** — the typed override is still per-device
  localStorage. A `source` (`logged`/`manual`) field + an endpoint would sync a
  typed value across devices and let it lower the estimate explicitly (today logs
  only ever raise it). Held back to keep Phase 1's precedence rules simple.
- **Coach-editable 1RM** (a coach setting an athlete's max directly).
- **Smarter derivation** — e.g. an average of recent tops, or unit conversion
  when an athlete trains plans in different units (today the value records its
  own unit but isn't converted across plans).
