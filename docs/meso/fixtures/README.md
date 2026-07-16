# Spreadsheet-parity fixtures

Real-data fixtures grounding [`../spreadsheet-parity-plan.md`](../spreadsheet-parity-plan.md).
They will feed the Phase-3 importer and its tests. Exported from Lance's **personal**
Google account via the claude.ai Drive connector (2026-07-16). Source fileIds are
recorded in memory (`spreadsheet-parity-template-ids`).

## Computability — what needs the LLM vs. plain code
- **Raw `.xlsx` → parsed grids / JSON / `.md`:** deterministic code
  (`explore_sheet.py` etc., openpyxl). No LLM.
- **Google Sheet → raw `.xlsx`:** needs the LLM + the Drive connector. Not reproducible
  by plain code.

So the raw exports are the source of truth; everything downstream re-derives by code.
We currently hold **raw `.xlsx` for only a few** sheets — the rest are LLM-rendered
`.md` (the connector's text view returns just a sheet's first tab), which regenerate
only via another connector fetch.

| Have raw `.xlsx`/`.pdf` | Have `.md` grid only (needs re-fetch for raw) |
|---|---|
| `102.xlsx`, `baseline-H000.xlsx`, `annotated-program-sample.xlsx`, `101.pdf`, `lance-program.xlsx` (gitignored) | `101/103/201/301/321/402/402G/405G/501/601/701/801G/3G000/5G000` |

**Still needed for a clean Phase-3 import set:** raw `.xlsx` for the import targets
`101, 103, 402, 601` (currently `.md`/`.pdf` only). Re-export via the connector when
building the importer.

## The anonymized annotated sample
`templates/annotated-program-sample.xlsx` (+ `.md`) is derived from a real client's
program tab `415 - 0626` by `make_annotated_sample.py`: only the program grid is
copied (exercises, prescriptions, executions, RPE, coach cues — no PII), training
dates are dropped, and the workbook's identity tabs are not copied. The raw client
workbook was **deleted, never committed**. This sample is the fixture for the §2.6
polymorphic-cell behavior (in-cell `skip`, swaps like `DB pullover`, per-week execution).

## Git handling
- **Committed:** the template grids (`templates/*.md`, `*.txt`), the raw template
  inputs (`102.xlsx`, `baseline-H000.xlsx`, `101.pdf`), the anonymized sample, the
  parsing scripts, and `sheet-structure.md`.
- **Gitignored (persist locally, not committed):** `lance-program.xlsx` and
  `sheet-dump.json` (large personal data / re-derivable). See `.gitignore`.

## Column layout of a program grid (importer reference)
`A = Day label · B = Exercise (merged down the block) · C = Tempo · D–G = Week 1–4
(unmerged) · H = Coach Comment / instructions (merged) · I = Athlete Comment (merged)
· J = Rest (merged)`. Header repeats per Day section; column letters drift between
template generations — **parse by header label, not position.**
