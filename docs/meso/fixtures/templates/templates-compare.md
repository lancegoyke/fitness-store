# Workout-Program Template Structural Comparison

Sample of 10 template spreadsheets (+3-tab baseline workbook) fetched from Google Drive
(lancegoyke@gmail.com), 2026-07. Each is a single-sheet reusable program template that gets
COPIED into a client workbook. Reference grid structure per the task brief was confirmed.

Artifacts saved alongside this file in `templates/` (absolute paths at bottom).

---

## Per-template facts table

| name | kind | Day sections | week cols | strength vs aerobic/metabolic | Tempo? | RPE? | %1RM? | superset style | blank or filled |
|---|---|---|---|---|---|---|---|---|---|
| 101 | **INTAKE FORM (not a program)** | 0 | 0 | n/a | no | no | no | n/a | blank intake fields (Name/Email/Goals/Equipment…) |
| 201 | program | 7 | 4 | mixed: strength (sets×TIME + sets×reps) + 2 aerobic + circuit | yes | no | no | A1/A2/A3, B1/B2/B3 | **blank (prescription-only)** |
| 301 | program | 7 | 4 | mixed: strength(+RPE) + aerobic + EXP intervals + circuit | yes | **yes (sub-row)** | no | A) B) C) | blank |
| 402 | program | 7 | 4 | hypertrophy-by-bodypart + EXP power + aerobic + named metcon | yes | no | no | A) B) C) D) E) sequential | blank |
| 405G | **group** | 7 | 4 | 4xx hypertrophy + power; adds %1RM & RPE sub-rows; interval day | yes | **yes (sub-row)** | **yes (~50% 1RM sub-row)** | A) B) C) D) E) | blank |
| 501 | program | 7 | 4 | alactic sprint intervals + strength(+RPE) + threshold benchmark | yes | **yes (sub-row)** | no | A)–E) + D1/D2 | blank |
| 601 | program | 7 | 4 | metabolic density (EDT, time-based) + threshold + BB circuit | yes | no | no | A) B) C) + inner 1./2. | blank |
| 701 | program | 7 | 4 | bodyweight/gymnastic skill (ISO holds, progressions) + aerobic + challenge | yes | no | no | A)–E) | blank |
| 801G | **group** | 7 | 4 | 6xx/8xx metabolic (EDT) — structurally == 601 | yes | no | no | A) B) C) + inner 1./2. | blank |
| H000 | **TESTING battery (baseline, tab1)** | 7 | **1** | assessment: jumps / ?RM lifts / sprint tests w/ measures | yes | no | no | A)–E) + D1/D2 | blank (fields to be measured) |
| 3G000 | **TESTING battery (baseline, tab2, group)** | 7 | **1** | assessment (byte-identical to H000) | yes | no | no | A)–E) + D1/D2 | blank (fields to be measured) |
| 5G000 | **TESTING battery (baseline, tab3, group)** | 7 | **1** | assessment (byte-identical to H000) | yes | no | no | A)–E) + D1/D2 | blank (fields to be measured) |

---

## Q1 — Blank (prescription-only) or filled with actuals?  → **BLANK. Template == a Plan with no logged data.**

Every sampled PROGRAM template is prescription-only. For each exercise the **header/first row** carries
the prescribed dose in the per-week columns (`sets x reps`, `5x30s`, `3 x 5`, `15'`, `Up to 8 x 8s`),
and the **N set-detail rows beneath it are EMPTY**. There are NO logged actuals anywhere (no `225lbs x 5`,
no recorded loads/times). The only non-prescription pre-filled content is coach instruction text
(Coach Comments) and structural labels/banners. Athlete Comments columns are empty.

Where RPE / %1RM appear they are **prescribed targets** (`RPE 8`, `~50% 1RM`), still prescription — not actuals.

This is the clean inverse of a filled-in CLIENT copy (which fills those set-detail rows with measured
loads/reps). **Verdict for the parity doc: a template maps to a Plan/Mesocycle whose prescriptions are
populated but whose per-set logged data is entirely empty.**

## Q2 — Do all templates share the mapped grid schema?  → **Mostly yes; 3 classes of deviation.**

8 of 9 program templates (201/301/402/405G/501/601/701/801G) share the exact reference 10-column schema,
keyed off header LABELS (column letters drift between generations, as warned):

`Day N | Exercise | Tempo | Week 1 | Week 2 | Week 3 | Week 4 | Coach Comments | Athlete Comments | Rest`

Shared traits: a `Date:` helper row above every Day header (in the Tempo column); Exercise/Tempo/
Coach-Comments/Athlete-Comments/Rest vertically **merged** across each exercise's set-detail rows; a
full-width merged instructional **banner** as row 1; blank set-detail rows.

Deviations:
1. **101 is not a grid at all** — a 2-column athlete INTAKE form (Athlete Name/Email/Phone/Birthdate/
   Age/Height/Weight/Sex/Goals/Concerns/Equipment).
2. **Testing sheets (H000 + the 3G/5G group tabs)** use a DIFFERENT schema: only **ONE** week column
   (7 columns total) and **column-A day-theme labels** ("Anterior chain"/"Posterior chain"/"Energy systems").
3. **Optional per-exercise SUB-ROWS**: RPE (301/405G/501) and %1RM (405G) are encoded as an extra row
   *beneath* the prescription row, sharing the merged Exercise cell — NOT as columns.

## Q3 — Family layout differences (1xx…8xx), strength vs aerobic/metabolic

- **1xx (101):** athlete INTAKE/profile form. (Single sample — 1xx numbering may be reserved for
  client-info sheets, or 101 specifically is the intake. Flagging as a caveat.)
- **2xx (201):** whole-body beginner. Strength as **sets×TIME** (`5x30s`) and sets×reps (`4 x 6`),
  paired supersets A1/A2/A3. Two "Cardiac output development" aerobic days + a Day-6 bodybuilding circuit. No RPE.
- **3xx (301):** whole-body strength + conditioning. Main lifts RPE-driven (RPE sub-row), plus **EXP
  interval** work (`8 x 8s:45s`, `5 x 45s:15s`), aerobic circuits, and named benchmark workouts
  ("Summer Requests (L4)", "Living Room Glacier (L4)").
- **4xx (402 / 405G):** hypertrophy split by body part (legs/pull/push), EXP power lead-off, descending-rep
  hypertrophy; 405G adds `~50% 1RM` and `RPE` sub-rows and a `Cardio intervals` day. Strength/hypertrophy dominant.
- **5xx (501):** alactic-aerobic/strength. Sprint intervals (`Up to 8 x 8s`) + strength with RPE sub-rows +
  a threshold benchmark day. Aerobic-power ↔ strength blend.
- **6xx (601):** metabolic **density** — Escalating Density Training (EDT) AMRAP blocks capped by TIME
  (`15'`, `12'`) with a full-width `Rest 5 minutes` separator; threshold benchmark; BB circuit. Metabolic dominant.
- **7xx (701):** bodyweight/gymnastic strength — handstand/planche/L-sit progressions, heavy `ISO` tempo,
  `15 min` practice prescriptions, numbered skill ladders inside Coach Comments; Day-6 named challenge with a URL.
- **8xx (801G):** metabolic (EDT) GROUP — structurally identical to 601.

**Strength families (sets×reps fits the grid): 2xx, 4xx, 7xx** and the strength portions of 3xx/5xx.
NB the *load* is NOT in the prescription cell — the grid prescribes sets×reps/sets×time and the athlete
writes the LOAD into the blank set-detail rows in the client copy.

**Aerobic / metabolic families that DON'T fit sets×reps×load:**
- Cardiac-output aerobic days (all families): prescription is DURATION + HR target, e.g.
  `Cardiac output development | (no tempo) | 20-60m | 20-60m | 20-60m | 20-60m | Keep heart rate 120-150 bpm | (no rest)`
- Interval days (3xx/5xx): `A) Explosive repeats | EXP | Up to 8 x 8s | … | Sprint 8-10s, hard rest 50s … Max 8 rounds | 50s`  (Rest col sometimes `←`).
- Density days (6xx/8xx): `A) Escalating Density Training 1. RDL x 6 2. Bench press x 6 | 201 | 15' | 15' | 12' | 15' | AMRAP rounds in time | PRN`  (+ full-width `Rest 5 minutes` row).
- Named benchmark/challenge days (3xx/5xx/6xx/7xx): `1x` / `4 rounds`, tempo `EXP`/`DYN`, rest `PRN`, comment often a URL — a timed challenge, tracked by completion time.
- Bodybuilding circuits: an entire circuit packed into ONE merged Exercise cell, prescription `2x each`, "No need to record".

Contrast rows (strength vs aerobic), quoted:
- Strength: `A) Squat | 201 | 3 x 5 | 4 x 5 | 3 x 3 | 5 x 5` with sub-row `RPE 7-8 | RPE 8-9 | RPE 6 | RPE 9-10`.
- Aerobic: `Cardiac output development | | 20-60m | 20-60m | 20-60m | 20-60m | Keep heart rate 120-150 bpm |`.

## Q4 — The G (group) variant  → **Same grid, NOT multi-athlete. G = a shared plan on the individual schema.**

405G (vs individual 402) and 801G (vs individual 601) are **structurally identical to individual templates**:
same 10-column schema, same 7 Day sections, same 4 week columns, **no extra per-athlete columns, no
side-by-side multi-person layout, no different day count.** The only differences are exercise SELECTION and
(for 405G) the presence of %1RM/RPE sub-rows.

So the `G` suffix means a program **authored for a group setting, written on the identical individual grid** —
it is NOT a different multi-athlete spreadsheet shape. **Parity implication:** `G` maps cleanly to Meso's
`MesoGroup` **shared-plan** model (one prescription tree shared to many athletes). Crucially, the template
carries **no per-athlete override columns** — the shared grid is a single set of prescriptions, so a `G`
template imports as the shared base plan with zero per-athlete deltas baked in (overrides, if ever used,
are layered on later in the app, not stored in the sheet).

## Q5 — Baseline H000 / 3G000 / 5G000  → **Testing/assessment layout (single week), NOT 4-week programs.**

**H000** (workbook tab 1, title "*TEMPLATE - 000 - Testing") is a **testing/assessment battery**:
- 7-column schema with **only ONE week column** (`Day | Exercise | Tempo | Week 1 | Coach Comments | Athlete Comments | Rest`).
- Column A carries **day-theme labels**: Day1 "Anterior chain", Day3 "Posterior chain", Day5 "Energy systems".
- Prescription cells are **test targets / measurement fields**, not doses: `Vertical jump → Max height`,
  `Squat/Bench/Deadlift → ?RM` (rep-max work-up), `Broad jump → Max distance`, and sprint tests
  `10 sec / 1 min / 10 min` with sub-rows `Distance:`, `Max power:`, `Avg power:`, `HR @ end:`, `HR @ 1m rest:`, `Avg HR:`.
- Aerobic (`Cardiac output development`, tempo `DYN`, `20-60 min`, links to mastering.fitness/cardio) and a
  Day-6 benchmark ("Living Room Glacier"). Day 7 off.
- It is the **week-0 assessment sheet** used to establish baselines before a program block. Set-detail rows
  blank (to be filled with measured results).

**3G000 / 5G000** (tabs 2 & 3) — **group testing variants, structurally byte-identical to H000.**
Hard evidence from the decoded .xlsx ZIP central directory: all three worksheet XML parts have the *exact*
same uncompressed size (31900 bytes; compressed 5607/5602/5601, distinct CRCs). Three independent sheets
matching to the byte ⇒ an identical grid template (same rows, columns, populated-cell positions, merges,
styles) that differ ONLY in which shared strings each cell references — i.e. content (specific test exercises /
day-theme wording), not shape. So both are the same **single-week, 7-column, 7-day testing battery** as H000,
with **no** per-athlete/multi-person columns. The leading digit is a naming tag (inference: 3G = the 3-day
group split's baseline battery, 5G = the 5-day group split's) — all three still carry 7 Day sections.

CAVEAT: the literal per-cell exercise text of tabs 2 & 3 could NOT be extracted — Google Drive's
read_file_content renders only the first sheet (H000), and the multi-sheet xlsx binary could not be
losslessly reconstructed by hand. The structural conclusions rest on the reliably-decoded ZIP central
directory + the documented G-variant convention (405G/801G = same grid, different exercise selection only),
not on reading tab 2/3 cells. To get the exact exercises, re-export tabs 2 & 3 as their own single-tab sheets.

**Parity takeaway for the baseline workbook:** the "000 - Testing" workbook is an ASSESSMENT template, not a
training block — one H (individual) + two G (group) single-week batteries used to capture baselines. It maps
to a distinct "assessment/testing session" concept (measured fields, no dose progression), separate from the
4-week program Plans.

## Q6 — Per template: Day count / week count / Tempo-RPE-Rest / superset notation / merged-cell quirks

See the facts table. Consolidated notes:
- **Day count:** every program & testing sheet has **7 Day sections** (Day 1–7, Day 7 = "Off or walk").
  101 (intake) has none.
- **Week columns:** programs = **4** (Week 1–4); testing sheets (H000/3G/5G) = **1**.
- **Tempo:** present on every program/testing sheet (column 3). Vocabulary is a mix of digit codes
  (`201`, `301`, `302`, `303`, `353`, `212`, `221`, `222`, `111`, `151`, `202`, `101`, `2020`) and word codes
  (`EXP` explosive, `DYN` dynamic, `ISO` isometric, `EXP`; blank for pure-cardio). Tempo `EXP` also tags benchmark/interval work.
- **RPE:** only 301, 405G, 501 — as a per-week SUB-ROW (`RPE 8/9/6/10`), never a column.
- **%1RM:** only 405G — as a SUB-ROW (`~50% 1RM`).
- **Rest:** present on every program/testing sheet (last column). Values are heterogeneous: durations
  (`30s`, `90s`, `2-3m`), `PRN`, `←` (see-left), `→`.
- **Superset notation is inconsistent across families:** `A1/A2/A3` (201), single-letter `A) B) C)` (301),
  sequential `A)–E)` (402/501/701/H000), paired `D1/D2` (501/H000), and inner numbered `1./2.` lists inside
  one merged EDT cell (601/801G).
- **Merged-cell quirks:** Exercise/Tempo/Comments/Rest merged vertically across each exercise's set-detail
  rows; in some generations the Exercise name spans **B:C** (two columns wide); full-width merged **top banner**
  ("6-7 days total…", "Three whole body days…", "Split: …", "Weekly Schedule …"); full-width merged **footer**
  ("END OF WEEK  Send a weekly update…"); full-width merged mid-day **`Rest 5 minutes`** separators (6xx/8xx);
  whole circuits/EDT blocks packed into ONE merged Exercise cell.

## Q7 — What would BREAK a generic importer

1. **Sheet-type ambiguity:** 101 is an intake form (2-col), testing sheets are 7-col/1-week, programs are
   10-col/4-week. An importer must first classify the sheet, not assume the program grid.
2. **Variable column count / week count** (7 vs 10 cols; 1 vs 4 week columns) — keying to "4 week columns" breaks on testing sheets.
3. **RPE / %1RM as SUB-ROWS** beneath the prescription row (sharing the merged Exercise cell) — a naive
   row-per-exercise parser reads them as phantom exercises or as set-detail. Must recognize `RPE n` / `~n% 1RM` tokens.
4. **Heterogeneous prescription cells:** `4 x 6`, `5x30s`, `3 x 10 each`, `15'`, `20-60m`, `8 x 8s:45s`,
   `Up to 8 x 8s`, `?RM`, `3 x ?`, `1x`, `2x each`, `Max height`, `Distance:`, `RPE 8`. No single parse rule.
5. **Rest column non-durations:** `←`, `→`, `PRN` instead of a time.
6. **Circuits / EDT blocks in one merged cell** with embedded `A1)/A2)` or `1./2.` lists — not decomposable into rows.
7. **Non-grid rows interleaved with data:** full-width merged top banners, `END OF WEEK` footers, and mid-day
   `Rest 5 minutes` separators sit between real exercise rows.
8. **Column letters drift between template generations** — must key off header LABELS (as the reference notes),
   and Exercise may occupy a merged B:C span.
9. **Trailing blank merged rows** padding each day to a fixed height would create phantom empty exercises if not filtered.
10. **Coach Comments carry multi-line text, URLs, and numbered skill-progression ladders** (701) — not machine dose data.
11. **Day-theme labels in column A** appear only on testing sheets (would be misread as a "Day" value or exercise elsewhere).
12. No images observed; the only embedded "media" are hyperlink URLs inside Coach Comments (mastering.fitness/cardio,
    record.lancegoyke.com/challenges/...).

---

## Artifacts on disk (absolute paths)

Directory: `/tmp/claude-1000/-home-lance-fitness-store/9e637151-b54d-4e07-b1c3-8cea6b5e828c/scratchpad/templates/`

| file | bytes | what |
|---|---|---|
| `101.md` | 354 | intake form (not a program) |
| `201.md` | 3384 | 2xx whole-body program grid |
| `301.md` | 2816 | 3xx strength+conditioning grid |
| `402.md` | 2000 | 4xx hypertrophy grid (individual) |
| `405G.md` | 2454 | 4xx hypertrophy grid (GROUP) |
| `501.md` | 2264 | 5xx alactic/strength grid |
| `601.md` | 2288 | 6xx metabolic/EDT grid |
| `701.md` | 2129 | 7xx bodyweight/gymnastic grid |
| `801G.md` | 2059 | 8xx metabolic/EDT grid (GROUP) |
| `H000.md` | 2572 | baseline tab1 — individual testing battery (full grid) |
| `3G000.md` | 4243 | baseline tab2 — group testing battery (structural, from ZIP dir) |
| `5G000.md` | 4337 | baseline tab3 — group testing battery (structural, from ZIP dir) |
| `baseline-H000.xlsx` | 8807 | **reconstruction of tab1 (H000) only** — valid xlsx (144 rows × 7 cols, 106 merges), NOT the raw original multi-tab binary (that could not be losslessly extracted) |
| `templates-compare.md` | — | this report |

NOTE on the .xlsx: the original Drive export is a 3-tab workbook, but the raw binary could not be reproduced
to disk from the base64 losslessly; `baseline-H000.xlsx` is a faithful single-tab rebuild of H000 for
reference. Tabs 2 & 3 were characterized from the decoded ZIP central directory (identical sheet sizes),
not re-serialized.
