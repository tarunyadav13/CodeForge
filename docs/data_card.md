# Data card

> Template. Member 2 owns the synthetic data generator; fill this in from `scripts/gen_data.py`,
> `scripts/validate.py` output and the saved prompts once they exist. Nothing below is a claim
> about data that has been generated.

## Synthetic student data (Annex C tables)
- **Generator / prompts:** _path to `scripts/gen_data.py` and `prompts/` — TBD (M2)_
- **Model used to generate:** _TBD_
- **Size:** _N students (≥30), programmes (≥2), batches (≥2), courses (≥6) — TBD_
- **Edge cases included:** _attendance exactly at threshold, one class below, just-below pass
  mark, ABSENT, DETAINED, multiple backlogs, CGPA at a cut-off — confirm each_
- **Reserved ranges avoided:** student IDs S9000–S9999 and course codes starting `JDG` are not used.
- **Validation:** _paste `scripts/validate.py` output_
- **Intended use:** testing this prototype only. Not real people; no real records.

## Policy documents (Source Register, Annex B)
- **Real public documents:** _doc_id, issuer, URL, retrieved_on — TBD (M3 + M1)_
- **Synthetic documents (synthetic=Y):** conflict and prompt-injection fixtures listed in
  `docs/integration_requests.md` #10 — not yet written.

## Evaluation set
- `eval/eval_set.jsonl`: 29 hand-written questions (see `docs/evaluation.md`). IDs inside are placeholders.
