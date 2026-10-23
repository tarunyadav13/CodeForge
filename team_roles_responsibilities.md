# AI-Powered University Student Services Assistant
## Team Roles, Responsibilities, Deliverables, and Integration Plan

**Project:** AI-Powered University Student Services Assistant  
**Team size:** 3 members  
**Repository strategy:** One shared GitHub repository with individual feature branches  
**Primary goal:** Build a secure, grounded assistant that answers university-policy questions and student-specific questions using authoritative documents, deterministic tools, and auditable workflows.

---

## 1. Project Objectives

The application should:

- Answer university policy questions using retrieved evidence and clear citations.
- Resolve conflicting policy versions using the agreed authority, applicability, supersession, and effective-date rules.
- Answer student-specific questions using authorized student records and deterministic calculations.
- Support document ingestion so newly added documents become retrievable without restarting the application.
- Protect student data and refuse unauthorized cross-student access.
- Record useful audit information and provide a repeatable evaluation process.
- Run from documented setup instructions on another team member's computer.

## 2. Team Ownership at a Glance

| Member | Role | Primary ownership | Git branch |
|---|---|---|---|
| Member 1 | RAG and Knowledge Engineering | Document processing, retrieval, citations, policy precedence, ingestion | `feature/rag` |
| Member 2 | Student Data and Decision Tools | SQLite schema, synthetic data, student tools, eligibility calculations | `feature/student-tools` |
| Member 3 | API, Orchestration, UI, and Integration | FastAPI, workflow orchestration, identity/security integration, UI, audit, deployment | `feature/api-deployment` |

**Ownership rule:** Each member is the primary owner of their assigned files. Changes that affect another member's module must be discussed first. All members remain responsible for code quality, tests, documentation, and helping with integration.

---

## 3. Member 1 — RAG and Knowledge Engineering

### Mission
Build the document knowledge layer that retrieves relevant university policy evidence and explains which policy applies.

### Primary folders
- `app/retrieval/`
- `app/policies/`
- `tests/test_retrieval.py`
- `tests/test_policy_precedence.py`
- Relevant ingestion utilities, coordinated with the team

### Responsibilities

#### A. Document processing
- Identify and organize the official documents supplied for the project.
- Extract text from supported document formats.
- Preserve source metadata such as document name, page, section, authority level, programme/batch applicability, and effective dates when available.
- Chunk documents using a documented, testable strategy.
- Handle malformed, empty, duplicate, or unsupported documents gracefully.

#### B. Retrieval and citations
- Configure the embedding model and persistent ChromaDB collection.
- Implement semantic retrieval and relevant metadata filters.
- Return source-grounded evidence, not just untraceable text.
- Preserve citation details so the final answer can identify the source and page/section where possible.
- Evaluate retrieval quality using a shared question set.
- Compare embedding configurations on the same corpus and questions when time permits; report measured results rather than inventing metrics.

#### C. Policy precedence and conflicts
Implement and test the project's agreed precedence rules. The resolver should consider:
1. Applicability for the requested as-of date, programme, batch, and other relevant scope.
2. Explicit supersession by an authorized source.
3. Authority level, with higher-authority sources taking precedence as defined by the project requirements.
4. Effective date/recency when authority is equal.
5. Unresolved conflict handling: flag the conflict and preserve evidence for the competing sources instead of silently guessing.

- Keep rule provenance and the reason for the selected rule.
- Do not let a low-authority FAQ override a higher-authority rule merely because it is newer.
- Treat the requirements document and its defined source hierarchy as the authority for implementation.

#### D. Document ingestion
- Implement ingestion so uploaded documents can be processed and added to persistent retrieval storage without restarting the app.
- Update the source register and preserve metadata.
- Handle duplicate uploads and ingestion errors safely.
- Do not automatically treat every newly uploaded document as an approved executable rule. Policy authority and validation must be handled explicitly.

#### E. Testing and documentation
- Unit-test chunking, metadata, retrieval, citations, date filters, precedence, supersession, and unresolved conflicts.
- Provide sample documents or safe fixtures for tests.
- Document configuration, expected metadata, and how to run tests.

### Expected deliverables
- Working retrieval module.
- Working policy resolver.
- Document-ingestion function/service.
- Citation/evidence structure agreed with Member 3.
- Unit tests and brief technical documentation.
- Measured retrieval evaluation results, if evaluation has been run.

### Suggested interfaces
```python
def retrieve_evidence(question: str, as_of_date: str, filters: dict | None = None) -> dict:
    """Return evidence, citations, and any detected conflicts."""

def resolve_policy(candidate_rules: list, as_of_date: str, scope: dict) -> dict:
    """Return the applicable rule, provenance, and conflict details."""

def ingest_document(file_path: str, metadata: dict) -> dict:
    """Ingest a document and return status/source metadata."""
```

The exact return schemas must be agreed upon and documented in `docs/module_contracts.md` before implementation.

---

## 4. Member 2 — Student Data and Decision Tools

### Mission
Build the structured student-data layer and deterministic tools that calculate student-specific outcomes from authorized records and applicable rules.

### Primary folders
- `app/data/`
- `app/tools/`
- `scripts/`
- `tests/test_student_tools.py`

### Responsibilities

#### A. Database design
- Implement the SQLite database and schema required by the project specification.
- Include the required entities for students, courses, attendance, results, and rule registry, plus any source/audit tables agreed with the team.
- Add primary keys, foreign keys, constraints, and useful indexes.
- Define database initialization and migration/seed procedures appropriate for the prototype.
- Keep schema names and field types documented and stable for the other members.

#### B. Synthetic data
- Generate and validate synthetic student data meeting the project requirements, including at least 30 students, at least two programmes, at least two batches, and at least six courses if these are the requirements in the supplied specification.
- Include edge cases such as missing records, low attendance, backlogs, boundary eligibility values, and students from different programmes/batches.
- Respect any judge-reserved student IDs or course-code ranges specified in the project materials.
- Never include real student personal data or secrets in the repository.
- Provide repeatable scripts to create/load the dataset and validate it.

#### C. Deterministic tools
Implement typed, validated functions for the agreed use cases, such as:
- `get_student_profile`
- `get_attendance`
- `get_exam_result`
- `get_active_backlogs`
- `get_applicable_rules` integration, coordinated with Member 1
- `check_exam_eligibility`
- `check_placement_eligibility`
- `simulate_what_if`

- Calculate percentages and eligibility in Python/SQL, not by asking the language model to do arithmetic.
- Read thresholds and rules from the approved rule registry or policy-resolution interface rather than duplicating hardcoded policy values.
- Return structured results, calculation details, and rule/source provenance where relevant.
- Handle missing student/course records and invalid inputs with controlled, documented errors.

#### D. Security and data integrity
- Use parameterized database queries; never construct SQL by concatenating user text.
- Do not give the LLM arbitrary SQL execution access.
- Keep student authorization enforcement at the API/security boundary. Tools must accept a validated student identity/context and must not trust a student ID extracted from natural-language text.
- Prevent a student from accessing another student's records.
- Avoid exposing unnecessary personal data in logs and responses.

#### E. Testing and documentation
- Unit-test calculations, boundaries, missing data, invalid courses, backlogs, eligibility, and what-if scenarios.
- Test that data loading is repeatable and validation catches malformed records.
- Document schema, seed/load commands, and tool return formats.

### Expected deliverables
- Database schema and initialization code.
- Synthetic-data generator, loader, and validator.
- Deterministic student tools.
- Unit tests and technical documentation.
- Stable interfaces for Member 3's orchestration layer.

### Suggested interfaces
```python
def get_attendance(student_id: str, course_code: str) -> dict:
    """Return the stored attendance values and calculated percentage."""

def get_exam_result(student_id: str, course_code: str, session: str) -> dict:
    """Return the student's result for the requested course/session."""

def check_exam_eligibility(student_id: str, course_code: str, as_of_date: str) -> dict:
    """Return eligibility, calculation details, and rule provenance."""

def check_placement_eligibility(student_id: str, as_of_date: str) -> dict:
    """Return placement eligibility and the applied rule details."""
```

These are illustrative interfaces; agree on exact parameters, error handling, and output schemas in `docs/module_contracts.md`.

---

## 5. Member 3 — API, Orchestration, UI, and Integration

### Mission
Build the application entry points and connect the retrieval and student-data modules into a secure, testable end-to-end assistant.

### Primary folders
- `app/api/`
- `app/orchestration/`
- `app/audit/`
- `app/ui/`
- `Dockerfile`
- `docker-compose.yml`
- Shared integration and setup documentation

### Responsibilities

#### A. API and request/response contracts
- Build the FastAPI application with Pydantic request and response validation.
- Implement the agreed endpoints, as required by the project:
  - `POST /ask`
  - `POST /ingest`
  - `GET /health`
  - `GET /audit/`
  - `GET /sources`
- Define consistent error responses, input validation, trace IDs, and date handling.
- Keep endpoint behavior and response schemas documented.

#### B. Orchestration and answer generation
- Build the LangGraph workflow (or the agreed orchestration layer).
- Route policy questions to retrieval and student-specific questions to authorized tools.
- Combine evidence and deterministic tool outputs.
- Use the LLM to interpret questions and explain validated results, not to invent policy facts or independently calculate authoritative outcomes.
- Require citations for policy claims where appropriate.
- Abstain or clearly communicate uncertainty when evidence is missing or conflicting.
- Track tools invoked, applied rules, conflicts, and the as-of date in the response where required.

#### C. Identity, security, and prompt-injection defenses
- Establish a trusted student identity from the authenticated/request context, such as a validated `X-Student-Id` in the prototype's agreed trust model.
- Never use a student ID stated only in the question as proof of authorization.
- Refuse cross-student data access.
- Validate tool arguments and enforce authorization in code.
- Treat retrieved documents as untrusted content: instructions inside documents must not override system rules, permissions, or tool restrictions.
- Keep secrets in environment variables and out of Git.

#### D. UI and audit
- Build the Streamlit chat interface and useful status/error states.
- Display answers, citations, and relevant rule explanations clearly.
- Log trace IDs, timestamps, selected tools, source/rule identifiers, and outcomes as appropriate.
- Avoid logging secrets or unnecessary student information.
- Implement source and audit views only for authorized users.

#### E. Integration, deployment, and testing
- Provide mock retrieval and student-tool modules while the other members are developing.
- Integrate the real modules through the agreed contracts.
- Maintain dependency versions in `requirements.txt`.
- Configure Docker/Compose if included in the delivery plan.
- Write setup instructions and coordinate end-to-end testing.
- Ensure a clean clone can be configured and run by another team member.

### Expected deliverables
- Working FastAPI API.
- Working orchestration flow.
- Streamlit UI.
- Identity and access checks integrated with tools.
- Audit trail and health/source endpoints.
- Deployment configuration and README instructions.
- End-to-end tests and final integration report.

### Suggested interface
```python
def answer_question(question: str, student_id: str, as_of_date: str) -> dict:
    """Orchestrate retrieval/tools and return the agreed response schema."""
```

The API must validate the student identity before calling this function. The exact interface and response schema must be agreed upon with the other members.

---

## 6. Shared Repository Structure

```text
university-ai-assistant/
├── app/
│   ├── api/                 # Member 3
│   ├── orchestration/       # Member 3
│   ├── retrieval/           # Member 1
│   ├── policies/            # Member 1
│   ├── data/                # Member 2
│   ├── tools/               # Member 2
│   ├── audit/               # Member 3
│   └── ui/                  # Member 3
├── data/
│   ├── documents/           # Approved/sample source docs; follow licensing rules
│   └── synthetic/           # Synthetic test data only
├── tests/
├── scripts/
├── docs/
│   └── module_contracts.md  # Shared contracts agreed by all members
├── .env.example             # Example variable names only; no real secrets
├── .gitignore
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── README.md
```

Keep generated vector databases, local SQLite databases, environment files, model files, caches, and secrets out of Git unless the team has explicitly decided a small test artifact must be versioned. Share repeatable scripts and instructions instead of large generated state.

---

## 7. GitHub Collaboration Workflow

### Repository and branch setup
- Use one shared GitHub repository.
- Keep `main` as the stable, runnable branch.
- Give each member write access to the repository.
- Each member clones the repository and works locally on their assigned feature branch.

| Member | Branch |
|---|---|
| Member 1 | `feature/rag` |
| Member 2 | `feature/student-tools` |
| Member 3 | `feature/api-deployment` |

### Standard daily workflow
1. Start from an updated branch.
2. Work only on the assigned module unless a change is agreed with the team.
3. Run the relevant tests locally.
4. Inspect changes with `git status` and review the diff.
5. Commit a small, coherent change with a descriptive message.
6. Push the feature branch to GitHub.
7. Open or update a Pull Request targeting `main`.
8. Ask at least one teammate to review it.
9. Fix review comments and ensure tests pass.
10. Merge only after the change is compatible with the shared contracts.

Example commands:
```bash
git switch main
git pull origin main
git switch -c feature/rag

# After making changes
git status
git add app/retrieval app/policies tests
git commit -m "Implement retrieval and policy precedence"
git push -u origin feature/rag
```

For an existing branch, switch to it rather than creating it again:
```bash
git fetch origin
git switch feature/rag
git merge origin/main
```

If a merge conflict occurs, resolve it carefully, run tests, and push the resolved branch. Do not blindly accept one side of a conflict.

### Shared-file ownership
Coordinate edits to shared files such as `requirements.txt`, `README.md`, `docs/module_contracts.md`, and configuration. Assign one person to consolidate dependency and configuration changes. Never commit API keys, `.env` files, real student records, or other secrets.

---

## 8. Integration Contracts — Agree Before Parallel Coding

Create and maintain `docs/module_contracts.md`. At minimum, define:

- Function names, parameters, types, and return values.
- Error formats and missing-data behavior.
- Citation/evidence schema.
- Policy rule/provenance schema.
- Student identity and authorization expectations.
- Tool names and input validation.
- API request/response schema.
- Date and time conventions.
- Configuration names and dependency ownership.

**Do not wait until the end to discover that modules use incompatible inputs or output formats.**

During parallel development, Member 3 can use clearly marked test mocks/fixtures for unfinished modules. Replace mocks with real implementations before acceptance testing.

---

## 9. Testing and Acceptance Checklist

### Member 1 — Retrieval and policies
- [ ] Documents extract correctly and retain source metadata.
- [ ] Retrieval returns relevant evidence.
- [ ] Citations point to actual source/page/section details where available.
- [ ] Applicability filters use the requested date and scope.
- [ ] Explicit supersession is handled correctly.
- [ ] Higher-authority rules take precedence according to the specification.
- [ ] Recency is applied only at the appropriate precedence stage.
- [ ] Unresolved conflicts are flagged rather than silently guessed.
- [ ] Ingestion updates persistent retrieval storage without app restart.
- [ ] Invalid/duplicate document handling is tested.

### Member 2 — Data and tools
- [ ] Database schema and relationships initialize successfully.
- [ ] Synthetic data meets specified minimums and includes edge cases.
- [ ] Data loader and validator run repeatably.
- [ ] Attendance calculations are correct, including boundary values.
- [ ] Results and backlog queries handle missing records.
- [ ] Eligibility uses applicable, traceable rules.
- [ ] What-if simulations do not mutate official records.
- [ ] Parameterized SQL is used.
- [ ] Cross-student access is prevented through the application authorization boundary.
- [ ] No real student personal data is committed.

### Member 3 — API, workflow, UI, and security
- [ ] API request and response schemas validate.
- [ ] `/ask` routes to the required module(s).
- [ ] `/ingest`, `/health`, `/audit/`, and `/sources` behave as specified.
- [ ] Student identity is trusted only from the agreed validated context.
- [ ] Cross-student requests are refused.
- [ ] Prompt-injection content cannot alter permissions or tool policy.
- [ ] UI displays answers, citations, and errors clearly.
- [ ] Audit records contain useful traceability without exposing secrets.
- [ ] Clean setup instructions work on another laptop.

### Whole-team end-to-end tests
- [ ] A policy question receives a grounded answer with citations.
- [ ] A student attendance question returns the correct deterministic result.
- [ ] An eligibility question uses the applicable rule and explains its provenance.
- [ ] A conflicting-policy question follows the precedence rules.
- [ ] An unanswerable question does not receive fabricated information.
- [ ] An unauthorized cross-student request is refused.
- [ ] A newly ingested document becomes retrievable.
- [ ] All tests run from a clean clone using documented instructions.

---

## 10. Evaluation Plan

The team should maintain a shared evaluation set of at least 20 questions if required by the project specification. Include:
- At least 3 unanswerable questions.
- At least 3 policy-version/conflict questions.
- At least 4 student-specific tool questions.
- At least 2 cross-student access attempts.
- At least 2 multi-step questions.

Track, where applicable:
- Answer correctness.
- Citation correctness and support.
- Abstention behavior for unanswerable questions.
- Tool-result correctness.
- Retrieval hit rate.
- Response latency.
- LLM usage/cost or local resource usage, as relevant.

Record actual measured results only. Do not claim a metric has passed unless the team has run the test and recorded the result.

---

## 11. Integration Milestones

| Milestone | Expected outcome | Primary coordinator |
|---|---|---|
| M1: Agree contracts | Repository structure, interfaces, schemas, and ownership documented | All members |
| M2: Skeleton running | API/UI can run with test mocks | Member 3 |
| M3: Modules independently testable | Retrieval and tools pass their unit tests | Members 1 and 2 |
| M4: First real integration | API calls real retrieval and student tools | Member 3, with all members |
| M5: Security and ingestion | Authorization, conflict handling, audit, and ingestion tested | All members |
| M6: Acceptance and demo | Full evaluation, clean startup, demo script, known limitations documented | All members |

Adjust dates to the hackathon deadline; these milestones define the order rather than fixed durations.

---

## 12. Team Working Agreements

1. Communicate changes that affect another module before implementing them.
2. Keep commits small and meaningful.
3. Never push untested code directly to `main`.
4. Do not commit secrets, `.env`, real student records, or generated local databases.
5. Use mock interfaces only during development; remove or clearly isolate them from production paths.
6. Keep the README and contracts current.
7. Report blockers early and ask for review before integration becomes urgent.
8. Review another member's work when possible.
9. Test the complete application after merging changes.
10. Be transparent about incomplete features, measured results, and known limitations.

---

## 13. Definition of Done

A task is complete only when:
- The code is implemented and follows the agreed interface.
- Relevant tests pass.
- Errors and edge cases are handled.
- The code has been committed and pushed to the correct branch.
- The Pull Request has been reviewed and merged.
- Documentation/configuration is updated where needed.
- The integrated application still passes the relevant tests.

The project is ready for demonstration when a fresh clone can be configured and started using the README, core policy and student-specific questions work, citations and rule provenance are traceable, unauthorized access is refused, ingestion and audit behavior have been tested, and the evaluation results reflect actual runs.
