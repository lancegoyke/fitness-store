# Design Grounding — 8 Google Sheets workout templates
Purpose: ground a "one editable grid, keyboard-first, freeform-text cells" redesign.
Connector: Drive as lancegoyke@gmail.com. All 8 captured. Raw on disk (see artifact list at bottom).

## THE 8 TEMPLATES + fileIds
| Name | fileId | Fetch route | Tabs |
|---|---|---|---|
| 101 | 1OGm5hRmLfPMRU9D2CBQ4pCUppAtkyykSXJCzxcBp73I | PDF export (multi-tab) | 5 |
| 102 | 161CookigixBU7iAQ92cYvAciRalTIQi37szIt6DXXx8 | xlsx export (multi-tab) | 2 |
| 103 | 1V29BZuXdt1377BKYKYHjbnsOdQTFxVBVE_wPp9WNpCU | read_file_content (single tab) | 1 |
| 321 | 1xn64AOIiJg3jB4HKNF53BERLlCow8p9EKvRBSNeJ7t8 | read_file_content | 1 |
| 402 | 1qD5PdKe1HUKJUyOfRV4elQ5yB7i5SGHJlLk9D11T2hU | read_file_content | 1 |
| 402G| 1J74P9E0-6-WJzOJJP7ZlYnWFyIkvRiELRuTRjSpQQ2Q | read_file_content | 1 |
| 601 | 1YQ2vJxuPW98f4FHONmnOe4ZuMMoEqDe7Xmgx9lKcXEQ | read_file_content | 1 |
| 501 | 15_UFhKVmzEv0j1kIUs2o1X5JDvLjoSQVyxQTM0Oi0xk | read_file_content | 1 |

TWO template families:
- **"Weekly Schedule" family (101/102/103)** — banner "Weekly Schedule - 3x resistance - 1x metabolic - 2-3x easy aerobic"; slot notation A) B) C1) C2) D1) D2); RPE weeks 8/9/6-7/10; Tempo often numeric.
- **"Whole body / hypertrophy" family (321/402/402G/501/601)** — banner "6-7 days total - 3x hypertrophy..." or "Three whole body days - 3x ...";  slot notation A) B) C) D) E); RPE weeks 8/9/6/10; explosive lead-off (EXP).

## ITEM 1 — Prescription cell TEXT FORMAT (the key item)
Coaches type a single freeform string into each Week cell. Canonical shape: `<sets> x <reps>[ <unit/each>]`.
- sets×reps: `3 x 12`, `4 x 6 each`, `3 x 15 each`, `2 x 8 each`, `3 x 10`
- rep RANGE: `3 x 12-15`, `3 x 10-15`
- rep as duration/hold (ISO): `3 x 30s`, `3 x 45s`, `3 x 1m`, `3 x 5 breaths`
- time / conditioning: `20-60m`, `20-75 min`, `15'`, `12'`, `1x`
- open-ended / benchmark: `AMRAP`, `Up to 8 x 8s`, `Up to 8 x 10s`, `1x`
- placeholder (coach TBD): `3 x ?`
- "each"/"e" suffix marks per-side: `3 x 15e`, `3 x 6 each`
WHERE things live:
- **sets×reps** → the Week cell (freeform text).
- **RPE** → its OWN sub-row directly under the exercise (one RPE cell per week column): `RPE 8 | RPE 9 | RPE 6-7 | RPE 10`. NOT in the same cell as reps.
- **Tempo** → its own dedicated column, next to Exercise. Values: `201`, `212`, `2020`, `2121`, `301`, `353`, `101`, `111`, `EXP`, `ISO`, `DYN`, `202`, `303`. (In 102's new tab Tempo is stored as a NUMBER e.g. 201.0.)
- **Rest** → its own dedicated column: `75s`, `90s`, `2m`, `2-3m`, `60-90s`, `PRN`, `1m`, `120s`, `30s`.
- **LOAD (lbs/kg)** → essentially NEVER prescribed in the template. There is NO load value in any new-format cell. Intensity is governed by RPE (sub-row) + coach-comment cues like "Max fatigue" / "Max speed!". The ONLY place a Load column exists is the LEGACY hidden grid (102 'Program' sheet), where `Load` is a dedicated column left BLANK in the template (athlete fills actuals; Volume auto-computes).

=> This DIRECTLY refutes the "225→235→245→255 load ramp per week" hypothesis. These coaches do not ramp absolute load across week columns; they ramp REPS/SETS and RPE. Load, if tracked at all, is an athlete-entered actual, not a coach prescription.

## ITEM 2 — Week layout
Weeks are FOUR SEPARATE SIDE-BY-SIDE COLUMNS (Week 1 | Week 2 | Week 3 | Week 4), one cell per exercise per week. A multi-week progression is NEVER packed into one cell. Example (verbatim, one row across its 4 week columns):
- 102 Front squat: `3 x 12` | `4 x 12` | `3 x 8` | `4 x 10`   (+ RPE row `RPE 8` | `RPE 9` | `RPE 6-7` | `RPE 10`)
- 402 Split squat: `3 x 10` | `3 x 12` | `3 x 8` | `3 x 12`
- 501 Explosive repeats: `Up to 8 x 8s` | `Up to 8 x 8s` | `Up to 8 x 10s` | `Up to 8 x 10s`
Constant values are repeated across all four columns (e.g. `3 x 3` `3 x 3` `3 x 3` `3 x 3`, `20-60m` ×4, `1x` ×4) — cells are never left "blank = same as wk1"; they are filled per week.
The legacy hidden grid (102 'Program') also lays weeks side-by-side, but as whole COLUMN BLOCKS (Wk1=A–I, Wk2=L–T, Wk3=W–AE, Wk4=AH–AP), each block = Exercise|MyNotes|YourNotes|Sets|Reps|Rest|Load|Volume.

## ITEM 3 — 101→102→103 sequential macrocycle
YES, a sequential macrocycle (Base 1 → Base 2 → Base 3), not independent programs. Evidence:
- Same banner, same 7-day structure (Day1/3/5 resistance, Day2/4 aerobic cardiac-output 20–60/75 min, Day6 metabolic benchmark, Day7 off/walk), same slot pattern (A main squat/hinge, B secondary, C1/C2 superset, D1/D2 superset), same weekly wave (wk1 base → wk2 +sets → wk3 heavier/fewer reps → wk4 peak) and same RPE wave 8/9/6-7/10.
- 101's "Welcome" tab states it outright: "we will be keeping the same exercises for multiple blocks, so select weights that allow future progression." 101 also carries a "Periodization" tab: "15 Nov 2021 | Muscular endurance; movement re-education".
- Exercises ROTATE within the same movement-pattern slots; some persist verbatim across blocks.

Day-1 comparison:
| Slot | 101 (Base 1) | 102 (Base 2) | 103 (Base 3) |
|---|---|---|---|
| A | Heels-elevated goblet squat | Front squat | Front loaded split squat |
| B | Split squat | Front foot-elevated split squat | Heels-elevated goblet squat |
| C1 | Slider hamstring curls | Hamstring curls | Hamstring curls |
| C2 | Short side plank genie twists | Short side plank genie twists | Short side plank genie twists |
| D1 | Push up position backwards bear crawl | Bicycle crunch | Alternating high plank to low plank |
| D2 | Hanging plank from bar | Hanging side-to-side knee raise | Bear walk |
Note "Heels-elevated goblet squat" migrates A(101)→B(103); "Short side plank genie twists" and hamstring curls persist. Volume/intensity is comparable block-to-block (same wave), i.e. this is re-exposure + variation, not linear escalation.
=> IMPORT AS ONE multi-block plan (3 blocks × 4 weeks ≈ 12-week macrocycle), authored as 3 separate spreadsheets.

## ITEM 4 — 402 vs 402G (equipment variant)
Same day + week structure; differ ONLY in exercise selection (402=home/minimal, 402G=gym/barbell) plus 402G adds an RPE sub-row on the main lift. Rep waves identical (B: 3x10/12/8/12; C: 3x12/15/10/15). Divergent rows (Day 1):
- A: Squat jump (402) vs Box jump (402G)
- B: Split squat, no RPE (402) vs Barbell back squat, +`RPE 8/9/6/10` (402G)
- D: Prone quad extension `3 x ?` (402) vs Landmine front-loaded backward lunge `3 x 15` (402G)
Also Day-6 benchmark differs (Allison's Week 2 vs Fit Fall (L4)). "G" = Gym. Full table in 402G-program.md.

## ITEM 5 — Edge cases that stress a sets×reps×load grid
- **601 (metabolic/EDT/time):**
  - EDT block packs an inner numbered pair inside ONE merged exercise cell: `A) Escalating Density Training  1. RDL x 6 2. Bench press x 6`; `B) ... 1. SLRDL x 6 each 2. Push up to 1-arm support x 6 each`.
  - The "prescription" cell is a TIME CAP, not sets×reps: `15'` | `15'` | `12'` | `15'`. Coach cell: "Get as many rounds as possible in allotted time. If you hit 15 rounds, increase weights."
  - A full-width merged SEPARATOR row between EDT blocks: `Rest 5 minutes` (spans all columns).
  - ISO hold: `3 x 5 breaths`. Day-6 threshold benchmark `1x` (DYN).
  - Day-6 (hypertrophy) is a whole circuit packed in ONE cell: `Bodybuilding circuit A1) Overhead/incline press AMRAP A2) Row or pull up AMRAP B1) Backward lunge AMRAP B2) SLRDL AMRAP C1) Curls or other C2) Triceps pushdowns or other D) Front plank as long as possible` with week cells `2x each`.
- **501 (alactic/threshold/intervals):**
  - Sprint interval cell: `Up to 8 x 8s` / `Up to 8 x 10s` (reps×seconds, "up to"). Coach: "Sprint 8-10s, hard rest 50s... cut the series if output drops >10%. Max 8 rounds." Rest column `50s`.
  - Threshold benchmark: `A) Living Room Glacier (L4)` `1x`, "track your time in my app... Level 4 is hardest..." Rest `PRN`.
  - Free-text substitution exercise: "Hamstrings" `3 x 15` with note "Some sort of hamstring isolation, e.g., slider curls, banded leg curls, hamstring machine, or glute-ham raise".
  - Per-side suffix: `3 x 15e`. Paired accessory D1)/D2).
- **321:** rep RANGE `3 x 12-15`; ISO `3 x 30s`; `20-60m`; power lead-off `3 x 3 each` (EXP).
- **Non-strength rows generally:** cardiac `20-60m` / `20-75 min`, benchmark `1x`, all live in the same reps column, so a rigid numeric sets/reps/load schema cannot hold them — the cell MUST accept freeform text.

## ITEM 6 — Per-template facts
| T | Family | Day sections | Week cols | Tempo col | RPE sub-row | Rest col | Superset notation | Cells filled? |
|---|---|---|---|---|---|---|---|---|
|101| Weekly Sched | 7 | 4 | yes | yes (8/9/6-7/10) | yes | A/B/C1/C2/D1/D2 | filled |
|102| Weekly Sched | 7 | 4 | yes(numeric) | yes | yes | A/B/C1/C2/D1/D2 | filled (some `3 x ?`) |
|103| Weekly Sched | 7 | 4 | yes | yes | yes | A/B/C1/C2/D1/D2 | filled |
|321| Whole-body | 7 | 4 | yes | partial (main lift only) | yes | A/B/C/D/E | filled |
|402| Hypertrophy | 7 | 4 | yes | NO | yes | A/B/C/D/E | filled (some `3 x ?`) |
|402G| Hypertrophy | 7 | 4 | yes | yes (main lift) | yes | A/B/C/D/E | filled (some `3 x ?`) |
|601| Metabolic | 7 | 4 | yes | NO | yes | A/B/C + inner 1./2. + circuit | filled |
|501| Alactic/thr | 7 | 4 | yes | yes (multiple) | yes | A/B/C/D/E + D1/D2 | filled |
All 8 are FILLED (real coached programs used as templates), not blank scaffolds. All 8 = 7 Day sections, 4 week columns.

## ITEM 7 — What a keyboard-first, freeform-cell grid MUST accommodate
1. **Freeform text prescription cells** — the single most important requirement. One cell must hold any of: `4 x 6, each`, `3 x 12-15`, `3 x 45s`, `3 x 5 breaths`, `15'`, `20-60m`, `Up to 8 x 8s`, `AMRAP`, `1x`, `3 x ?`. Do NOT force separate numeric sets/reps/load fields — the coach abandoned exactly that (see #5 below).
2. **RPE lives on a per-exercise sub-row**, one value per week column, distinct from the reps row. Model needs an optional second (or Nth) line per exercise that is itself per-week.
3. **Dedicated Tempo and Rest columns** (per exercise, usually constant across weeks). Tempo is a small controlled vocab but also free ("EXP","ISO","DYN"); Rest is free ("PRN","2-3m","60-90s").
4. **4 side-by-side week columns**; progression = editing each week cell; constants are repeated, not inherited.
5. **The coach migrated FROM a structured grid TO freeform cells.** 102 still carries the hidden legacy sheet with real `Sets|Reps|Rest|Load|Volume` columns + Volume formulas + weekly volume roll-ups; the delivered visible sheet replaced all of that with freeform `3 x 12` text + an RPE row. Strong signal: rigid columns were friction; freeform won. Load/Volume columns went unused (blank) even when present.
6. **Merged multi-line cells / circuit blocks packed in one cell** — EDT `1. X x6  2. Y x6`, the whole "Bodybuilding circuit A1)...D)" in one cell, multi-line coach comments with `\n`. The cell renderer must preserve line breaks.
7. **Full-width banner/separator/footer rows** that are NOT part of the grid: top schedule banner (merged across all columns), `Rest 5 minutes` separators between EDT blocks, `Date:` marker rows, and the `END OF WEEK ... request a new program` footer.
8. **Non-exercise/aerobic/benchmark rows** in the same grid (Cardiac output `20-60m`; named benchmarks like "Living Room Glacier (L4)", "Surviving (L3)", "Band-Toasted Shoulders" with `1x`). Exercise "name" is free text and sometimes a workout/benchmark name or a substitution instruction; Coach cell sometimes holds a URL.
9. **Slot / superset prefixes are part of the exercise name string** — `A)`, `B)`, `C1)`, `C2)`, `D1)`, `D2)`, and inner `1.`/`2.`. No separate grouping column; the letter/number prefix carries the grouping.
10. **Placeholders** (`3 x ?`) and **units/suffixes** (`each`, `e`, `s`, `m`, `'`, `min`, `breaths`, `rounds`, `bpm`) and **emoji** (💪) appear in cells — the field must be unicode/emoji-safe and tolerate partial authoring.
11. **Tab bundles are inconsistent** — 101 bundles intake+warm-up(4 options)+welcome/how-to+periodization+program; 102 bundles hidden-legacy+visible; 103 is program-only. A redesign should treat the program grid as the core object and the rest (intake, warm-up, instructions, periodization/notes) as optional attached sections, not assume a fixed workbook shape.
