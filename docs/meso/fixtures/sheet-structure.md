# Lance Goyke program — workbook structure

Source: Google Sheet "Lance Goyke program" (id `1FLOdWQJn403nP42lWE-xWPQpO8d8LGRhfE5UjnOtaSg`),
exported to `lance-program.xlsx`. Parsed with openpyxl (`data_only=True`).

Total tabs: **68** = **4 reference/metadata tabs** + **64 program-block tabs**.
Full cell grids for every tab are in `sheet-dump.json`.

---

## 1. Tab inventory

### Reference / metadata tabs (4, first in order)
| # | Tab | Shape | Purpose |
|---|-----|-------|---------|
| 1 | **Athlete** | 13×2 | Athlete profile card. Merged name header `B1:G1='Lance Goyke'`; field labels in col A: Email, Phone, Address, Birthdate, Age, Height, Weight, Sex, Goals, Concerns, Equipment. Values blank in this export. |
| 2 | **FAQ** | 26×2 | 12 Q/A pairs (heavy cell merging). Program focus, how-to video, breathing drills, warm-up sets, RPE/RIR explainer, double progression, supersets/circuits, exercise-link, warm-up choice, social share. |
| 3 | **Periodization** | 19×2 | Master log: `Program Start Date` → `Focus`. 16 dated rows 2021-07-06 … 2023-05-08 (work capacity, strength endurance, hypertrophy, fat loss, HIIT, testing, etc.). |
| 4 | **Warm Up** | 41×5 | 4 reusable warm-up **Options**, each a table of Exercise / Sets / Reps / Notes (~8 exercises each). |

### Program-block tabs (64)
`301, 302, 303, 401, 402, 403, 404, 601, 602, 602.5, 603, 701, 702, 703,`
`401 - 0822, 402 - 1022, 403 - 1022, 404 - 1122, 501 - 0123, 502 - 0223, 503 - 0323,`
`H000 - 0423, 201 - 0423, 202 - 0523, 203v2 - 0623, 601 - 0723, 602 - 0823, 603 - 0923,`
`412 - 1023, 413 - 1123, 414 - 1223, 321 - 0124, 322 - 0224, 323 - 0324, 324 - 0424,`
`201 - 0524, 202 - 0624, 203v2 - 0724, 601 - 0824, 602 - 0924, 603 - 1024,`
`101 - 1124, 102 - 1224, 103 - 0125, 104 - 0225, 105 - 0325, 106 - 0425, 107 - 0525,`
`107 - 0525 (1), 110 - 0625, 601 - 0725, 611 - 0825, 602 - 0825, 603 - 0925,`
`321 - 1125, 322 - 1225, 602.5 - 0126, 323 - 0126, 324 - 0226, 412 - 0326, 413 - 0426,`
`414 - 0526, 415 - 0626, 416 - 0726`

Each program tab is ~140–170 rows × 10–13 cols (301 is the exception at 25 cols).

---

## 2. Program-tab naming convention

Format: **`<block code>`** optionally **`- <MMYY>`** (month/year the block started).
- **Block code** = 3 digits (e.g. `402`, `301`, `611`). Leading digit encodes the mesocycle
  *focus family* (recurs across years — e.g. `60x`/`601-603` blocks, `20x`, `30x`, `40x`,
  `10x`), trailing digits = sequence within that family. Same code recurs in different
  years (`402`, `402 - 1022`; `601` appears 2021/2023/2024/2025).
- **`- MMYY` suffix** = block start month/year → this cross-references the **Periodization**
  tab's `Program Start Date` column. 15 older tabs have **no** suffix; 49 have it.
- **Irregular codes**: `H000` (health/testing baseline), `602.5` / `602.5 - 0126` (half-step
  insert), `203v2` (revised version), `611` (variant), `107 - 0525 (1)` (duplicate of `107`).

---

## 3. Program-tab layout / schema

**Every program tab contains exactly 7 `Day N` sections** (Day 1 … Day 7 in column A).
Each day is an independent sub-table with its own header row.

### Standard header row (labels, per day)
`A=Day N | B=Exercise | Tempo | Week 1 | Week 2 | … | Coach Comments | Athlete Comments | Rest`

Exact column letters **drift between tab generations** (concept is stable):
- Older (e.g. **402**): Tempo=**D**, weeks **E–H**, Coach=I, Athlete=J, Rest=K.
- Newer (e.g. **416 - 0726**): Tempo=**C**, weeks **D–G**, Coach=H, Athlete=I, Rest=J.

### Where weeks live — side-by-side columns
Weeks are **adjacent columns**, not stacked blocks. Distribution across 64 tabs:
- **4 weeks, 1 col each** → 53 tabs (dominant)
- **5 weeks** → 6 tabs · **6 weeks** → 3 tabs · **1 week** → 1 tab
- **301** = **6 weeks × 3 columns each** (E/H/K/N/Q/T headers) → 25 cols, 952 merges (outlier).

### Per-exercise block (the repeating unit)
An exercise = a **header row** + **N set-detail rows** below it:
- Header row: `B`=exercise name (with superset prefix), `Tempo` (e.g. `201`, `EXP`, `302`),
  the **prescription** in each week column as `sets x reps` (e.g. `3 x 12`, `5 x 5`,
  `3 x 10-12`, `3 x ?` = AMRAP), `Coach Comments` (cue), `Rest` (e.g. `2m`, `75s`, `PRN`).
- Set rows: one row per prescribed set; the athlete logs **actual load × reps** here
  (e.g. `225lbs x 5`, `30x10`), or a **time** for conditioning (`23m 24s`), or reps.
- **Merges** (~112–118/tab): exercise name merged across `B:C` vertically down the block;
  Tempo merged vertically over the same rows. That's what the 118-merge count represents.

Representative rows (tab **402**, Day 1):
```
r3:  A3=Day 1 | B3=Exercise | D3=Tempo | E3=Week 1 | F3=Week 2 | G3=Week 3 | H3=Week 4 | I3=Coach Comments | J3=Athlete Comments | K3=Rest
r10: B10=B) Split squat | D10=201 | E10=3 x 10 | F10=3 x 12 | G10=3 x 8 | H10=3 x 12 | I10=Max fatigue | K10=2m
r11: E11=30x10 | F11=35x12 | G11=35x8 | H11=45x12          <- set 1 actuals per week
r12: E12=35x10 | F12=35x12 | G12=35x8 | H12=45x12          <- set 2
r13: E13=40x10 | F13=40x12 | G13=35x8 | H13=55x12          <- set 3
```

### Week-over-week progression
Represented by the **side-by-side week columns** on the same exercise row — the prescription
(`sets x reps`) changes column to column (double progression per the FAQ: hit the top of the
rep range across all sets, then add load). Newer tabs add a **per-week RPE target sub-row**
directly under the prescription (tab **416 - 0726**, r11): `D11=RPE 8 | E11=RPE 9 | F11=RPE 6 | G11=RPE 10`.

### Supersets / circuits
Two prefix encodings are actually used:
- **Single letter** `A) B) C) D) E)` — 639 rows (sequential exercises).
- **Grouped** `A1) A2) A3) / B1) B2) B3)` — 301 rows across 25 tabs (a lettered group done
  as a circuit; e.g. tab 301 Day 1 = A1 Front squat / A2 Floor press / A3 Bent row).
- The FAQ describes a **`5a`/`5b`** number-letter scheme, but that form has **0 occurrences**
  in the data — the sheets standardized on letters. (Vocabulary/FAQ drift to note.)

### Day/session delimiting
Days are delimited by the `Day N` label in column A plus a repeated header row. Each day is
usually preceded by a **`Date:` row** giving that day's calendar date for each week
(`C='Date:', D–G = 4 dates`); 56 tabs use the labeled `Date:` form, 4 older tabs (402/301-era)
put bare date cells in row 2, 4 have no dates. Days can be strength, metabolic, or aerobic
(e.g. `Cardiac output development`, `20-60 min`, HR 120-150 bpm; or `Off or long walk`).

---

## 4. Warm Up tab schema

`B1:E1='Warm Up and Cool Down'`. Then **Option 1..4**, each:
`A=Option N | B=Exercise | C=Sets | D=Reps | E=Notes`, ~8 exercises per option.
Example: `Rockback with abs | 3 | 5 breaths | Nasal breathing only`. Notes are almost all
"Nasal breathing only" (+ cues like "heels stay flat", "knees locked"). Reusable across
blocks — the FAQ says pick any option.

---

## 5. Athlete / FAQ / Periodization field lists (confirmed)

- **Athlete**: name (merged B1), then Email, Phone, Address, Birthdate, Age, Height, Weight,
  Sex, Goals, Concerns, Equipment (labels only; values empty in export).
- **FAQ**: Program focus (`Muscular endurance and work capacity`), How to read/fill (video),
  breathing drills, warm-up sets, example warm-up, **RPE/RIR** ("9RPE=1RIR…"), selecting
  weights, **double progression**, **supersets & circuits** ("5a paired with 5b"),
  exercise link `https://mastering.fitness/exercises/`, which warm-up, social sharing.
- **Periodization**: 16 rows of `Program Start Date → Focus` (2021-07-06 → 2023-05-08).
  Dates stored as datetime. This is the master mesocycle map.

---

## 6. Data-entry / tracking columns

There are **no dedicated "actuals" columns**. Logging is **inline** in the same grid:
- The **blank set-detail rows** under each exercise are the log target — athlete fills actual
  `load × reps` (or time/reps) per week column.
- **`Athlete Comments`** column = athlete's free-text log ("bad", "low back ow", "skip",
  "traveled last week", "quad burn").
- **`Coach Comments`** = coaching cue (prescription-side, e.g. "Max fatigue", "Max speed!").
- **`Rest`** = prescribed rest per set. **`Tempo`** = eccentric/pause/concentric code.
- The `Date:` rows capture the actual calendar date each week's session was performed.

So prescription and performed-actuals share one grid (set rows), keyed by the week column.

---

## 7. Exercise vocabulary

**387 distinct** exercise names (prefix-stripped, lowercased) across the 64 program tabs.
Most frequent: 3-point dumbbell row (27), push up (18), explosive repeats (17), split squat
(13), deadlift (12), dead bug (12), dumbbell floor press (11), front squat (9), walking lunge
(9), romanian deadlift (9), squat (8), bench press (8), squat jump (8). Names read like a
controlled catalog and the FAQ explicitly links `https://mastering.fitness/exercises/` — a
strong candidate to map onto the mastering.fitness exercise catalog.

---

## 8. Irregularities / notes

- **301** — unique 6-week × 3-col-per-week layout, 952 merged ranges; header typo `T41='Week 4'`
  (should be Week 6).
- **110 - 0625** — short (48 rows) metabolic block: workout-for-time ladders logged as times
  (`23m 24s`), embedded URL `https://mastering.fitness/cardio/`, emoji in names (`100 ➡️ 10`).
- **H000 / 602.5 / 203v2 / 611 / 107 - 0525 (1)** — off-pattern block codes (baseline, half-step,
  revised version, variant, duplicate).
- **RPE** used 367×; **RIR** literally 0× (RPE only); **`%`** rare (18×); AMRAP shown as `3 x ?`
  (30×). Loads are absolute lbs/kg or bodyweight, not %1RM.
- Dates stored as Python datetime (`2021-11-29 00:00:00`). Many cells carry multi-line notes
  (`\n`) and emoji (😎). No cross-tab formulas survived (`data_only` cached values only).
- Column-letter drift between generations is the main parity hazard for a rigid parser — key
  off header **labels** (Exercise/Tempo/Week N/Coach Comments/Athlete Comments/Rest), not
  fixed columns.
