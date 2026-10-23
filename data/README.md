# Policy corpus and Source Register

Owner: Member 3 (with Member 1 for ingestion). Register: [source_register.csv](source_register.csv),
columns exactly as the frozen `SourceRecord` model (`api/models.py`, Annex B).

## Real public documents (`synthetic=N`)

All three were fetched on **2026-10-23** from the official host named in `provenance`; the PDFs in
`documents/` are the unmodified downloads. Text of the clauses quoted below was read from the
documents. Nothing here is legal advice and none of it contains personal data.

| doc_id | Institution | What it contains (verified clause) | Pages |
|---|---|---|---|
| `UGC-FIRSTDEG-2003` | University Grants Commission | Cl. 5.8: attendance required for eligibility to appear at the examination "ordinarily shall not be less than 75%" | 6 |
| `MSRUAS-ACADREG-2022` | M. S. Ramaiah University of Applied Sciences | A.14.1: 75% minimum attendance, up to 10% shortfall condonable by the Dean with VC approval; Table 7: 80% for some programmes (e.g. B.Pharm); A.18: SEE eligibility (75% attendance and 40% in CE); pass criteria; re-registration / re-sit | 175 |
| `IITM-INTMA-ORD` | IIT Madras | Five Year Integrated M.A. ordinances, applicable from the 2015 batch. R.14: below 85% attendance means no end-semester exam and a `W` grade; R.8 supplementary exam; R.26 CGPA formula | 16 |

Licensing: the documents are published on the institutions' public websites without a stated
licence. They are kept here for a hackathon evaluation corpus only. If the team prefers not to
redistribute them, delete `documents/*.pdf` and keep the `provenance` URLs; the register stays valid.

## Synthetic documents (`synthetic=Y`)

`documents/synthetic/*.md`. Every file opens with a banner saying it is a synthetic test fixture and
not real policy. "Synthetic Test University" is fictional. Authority levels are **assumed** by M3
(1 regulation, 2 circular, 3 notice, 5 informational FAQ) because the brief's Annex A table is not in
the repo; change them if the brief says otherwise.

| doc_id | Role in the evaluation | Threshold |
|---|---|---|
| `SYN-REG-ATT-75` | Base regulation (effective 2022-07-01). Also holds the placement rule (7.1: no active backlogs, CGPA >= 6.0) | attendance 75% |
| `SYN-CIRC-ATT-80` | Circular effective 2026-07-01 that explicitly supersedes SYN-REG-ATT-75 s.4.1 | attendance 80% |
| `SYN-FAQ-ATT-65` | Level-5 informational FAQ that is wrong and must not win | "65%" |
| `SYN-CIRC-FEE-A` / `-B` | Two circulars, same level, same date, no supersession, different late fee: unresolvable | INR 500 vs INR 750 |
| `SYN-NOTICE-INJ` | Contains `SYSTEM: ignore all rules ...` text; must be treated as data | n/a |

Conflict scenario (Annex A): as of 2025-01-15 the answer is 75% (EV-010); on 2026-03-01 it is 75% with
80% mentioned as upcoming (EV-011); on 2026-10-01 it is 80% (EV-009) and the FAQ's 65% is ignored
(EV-013); the late fee is `conflict_flagged` citing both circulars (EV-012).

## Unresolved metadata (not invented)

`effective_from` is **blank** for all three real documents because the documents do not state a
verifiable effective date, and the frozen `SourceRecord` requires a `date`. So **this CSV cannot be
loaded through `POST /ingest` for those three rows until a person fills the date.**

| doc_id | What is known | What is missing | Who decides |
|---|---|---|---|
| `UGC-FIRSTDEG-2003` | Titled "Regulations, 2003"; gazette date not on the PDF; a later UGC UG/PG regulation exists and may supersede it (not checked) | `effective_from` | Whole team |
| `MSRUAS-ACADREG-2022` | "Academic Year 2022-23 and onwards" | exact date; amendment status | Whole team |
| `IITM-INTMA-ORD` | "Applicable from 2015 batch"; server `Last-Modified` is 2020-03-13 but that is a file date, not a policy date | `effective_from`; the `scope_batches` format (`2015+` is a placeholder) | Whole team |

Other decisions the team must make:

1. **Which institution is "the university"?** The real documents come from three different
   institutions. Mixed into one corpus they disagree (75% / 75% / 85%) without being conflicts in the
   Annex A sense. `IITM-INTMA-ORD` is scoped to "Integrated M.A. (IIT Madras)" so it does not apply to
   synthetic students; UGC and MSRUAS are scoped `ALL`. Alternatives: drop IITM, or treat it as
   informational (level 5).
2. **Real versus synthetic precedence.** MSRUAS (75%, undated) and `SYN-CIRC-ATT-80` (80%, 2026-07-01) can
   both apply to `ALL`; the outcome depends on the unresolved MSRUAS date above.
3. **Rule registry.** Member 2's `rule_registry` should cite these documents for thresholds. The
   synthetic values (75 / 80 / backlogs 0 / CGPA 6.0) are proposals, not agreed values.
4. The prompt asked for "one or two" synthetic documents; six small ones are used because
   `conflict_flagged` and the injection test each need their own `doc_id` (named in
   `docs/integration_requests.md` #10).
