# Data card

Covers what Member 3 produced (policy corpus, Source Register, evaluation set). The synthetic
student database belongs to Member 2 and stays a template below until their generator exists.

## Policy documents (Source Register, Annex B): `data/source_register.csv`

**Purpose:** a small corpus for retrieval, citation, precedence and prompt-injection tests.
**Composition:** 9 documents: 3 real (`synthetic=N`) and 6 synthetic (`synthetic=Y`).

| doc_id | Real? | Source | Retrieved |
|---|---|---|---|
| `UGC-FIRSTDEG-2003` | real | University Grants Commission, https://www.ugc.gov.in/oldpdf/regulations/firstdegree_regulation.pdf (6 pp) | 2026-10-23 |
| `MSRUAS-ACADREG-2022` | real | M. S. Ramaiah University of Applied Sciences, https://www.msruas.ac.in/assets/frontend/pdf/academic-regulations-2022-23.pdf (175 pp) | 2026-10-23 |
| `IITM-INTMA-ORD` | real | IIT Madras, https://www.iitm.ac.in/sites/default/files/Ordinances/ma-ordinances_0.pdf (16 pp) | 2026-10-23 |
| `SYN-REG-ATT-75`, `SYN-CIRC-ATT-80`, `SYN-FAQ-ATT-65`, `SYN-CIRC-FEE-A`, `SYN-CIRC-FEE-B`, `SYN-NOTICE-INJ` | **synthetic** | Written by Member 3 in `data/documents/synthetic/`, each marked as a test fixture and not real policy | 2026-10-23 |

**Provenance and privacy.** The real documents are public regulations; they contain no personal data
(no result lists or student records). Files are unmodified downloads. The synthetic documents describe
a fictional "Synthetic Test University".

**Missing or unverified metadata (not invented).** `effective_from` is blank for all three real
documents; version strings are copied from the documents' own wording ("2003", "AY 2022-23 and
onwards", "Applicable from 2015 batch"); `scope_batches` for the IITM row (`2015+`) is a placeholder
format; authority levels of real documents are M3's judgement; synthetic authority levels are
assumptions. Details: `data/README.md`.

**Known limitations and possible bias.** Three Indian institutions only (engineering, applied
sciences, a central regulator): no humanities-only, state-university or non-Indian policy; the rules
(75/80/85% attendance) differ across them; the corpus is tiny and not representative; synthetic
documents were written to produce specific test outcomes, so passing them shows precedence logic
works on those cases, not in general.

**Intended use:** testing this hackathon prototype. **Prohibited:** presenting synthetic documents or
their rules as real university policy; giving students advice based on this corpus; loading real
student records alongside it.

## Evaluation set: `eval/eval_set.jsonl`

30 hand-written questions (see `docs/evaluation.md`), no personal data. Student IDs (`S1001` to
`S1003`) and course codes (`CS201`, `CS202`, `XY999`) are placeholders; none is in the judge-reserved
ranges. Expected values were read from the documents above, not produced by a model, and have not been
validated against a running backend.

## Synthetic student data (Annex C tables): Member 2

- **Generator / prompts:** _path to `scripts/gen_data.py` and `prompts/`: TBD (M2)_
- **Model used to generate:** _TBD (M2)_
- **Size:** _N students (>=30), programmes (>=2), batches (>=2), courses (>=6): TBD (M2)_
- **Edge cases included:** _attendance exactly at threshold, one class below, just-below pass mark,
  ABSENT, DETAINED, multiple backlogs, CGPA at a cut-off: confirm each (M2)_
- **Reserved ranges avoided:** student IDs S9000 to S9999 and course codes starting `JDG` must not be used.
- **Validation:** _paste `scripts/validate.py` output (M2)_
- **Intended use:** testing this prototype only. Not real people; no real records.
