# Jev `/v1/systemone` contract check

One live call, recorded verbatim to `tests/fixtures/jev/systemone_all_types.json`
by `scripts/jev_record_fixtures.py`. Request carried all three question types
(`choice`, `noul`, `score`) in a single call, per
`docs/specs/2026-09-23-jev-decision-layers.md:29`.

| Check | Observed |
|---|---|
| HTTP status | 200 |
| Top-level keys | `model`, `answers`, `usage` |
| `verdict` (choice) | `choice`, `confidence`, `probabilities` all present |
| `risky` (noul) | `confidence` **absent** — only `noul` present |
| `severity.score` | float (`1.75`) |
| `severity.legend` | present, keyed by score index as string (`"0"`..`"4"` → `harmless`..`catastrophic`) |
| `usage` keys | `input_tokens`, `output_tokens` |

## Match against spec

- Top-level shape `{model, answers, usage}` matches `docs/specs/2026-09-23-jev-decision-layers.md:265`.
- `choice` carrying `choice`/`confidence`/`probabilities` matches C1
  (`docs/specs/2026-09-23-jev-decision-layers.md:29`).
- `noul` carrying **no** `confidence` field matches the asymmetry asserted at
  `docs/specs/2026-09-23-jev-decision-layers.md:238` — this was the specific
  claim under test and it holds. The review-gate comparable-confidence formula
  (`abs(noul - 0.5) * 2`) is safe to hand-port as written.
- `score` carrying a float `score` plus `confidence` matches C1
  (`docs/specs/2026-09-23-jev-decision-layers.md:29`, `:238`).
- `legend` on the `score` answer is not mentioned anywhere in the spec's
  constraint set or data-flow section; it is additional information beyond
  what the spec commits to, not a contradiction of anything stated. Keyed by
  integer index (as a JSON string) matching the position in the `criteria`
  array supplied in the request, not by the raw float `score` value.

**Finding: no mismatch.** Every assertion in the spec that this fixture could
test against a live response held exactly as written, including the one the
task flagged as the asymmetry to watch (`noul` lacking `confidence`).
