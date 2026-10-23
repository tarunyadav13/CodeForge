-- University Student Services Assistant - SQLite schema (Member 2).
--
-- Required tables follow Annex C of the official HCLTech brief. Column names
-- are fixed by the brief and must not be renamed or removed. Extra tables /
-- columns are allowed (source_register, audit, schema_meta and a few
-- bookkeeping columns).
--
-- Notes
--  * Idempotent: every statement is IF NOT EXISTS, so init is safe to repeat.
--  * Foreign-key enforcement is a per-connection setting in SQLite; it is
--    switched on in db/data_access.py:connect() for every connection.
--  * Reserved judge IDs (S9000-S9999, JDG* courses) are deliberately NOT
--    rejected here: judges load their own students through the same schema.
--    Our own data is checked for reserved IDs by scripts/validate.py.
--  * Attendance percentage is never stored; tools compute it on request.
--  * ISO dates (YYYY-MM-DD) are stored as TEXT; empty effective_to is NULL.

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Source Register (Annex B). Same fields as POST /ingest metadata, plus
-- bookkeeping columns written by the ingestion endpoint.
-- Created first because rule_registry references it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_register (
    doc_id           TEXT PRIMARY KEY CHECK (length(trim(doc_id)) > 0),
    title            TEXT NOT NULL,
    issuer           TEXT NOT NULL,
    authority_level  INTEGER NOT NULL CHECK (typeof(authority_level) = 'integer'
                                             AND authority_level BETWEEN 1 AND 5),
    doc_type         TEXT NOT NULL CHECK (doc_type IN
                         ('regulation','circular','notice','faq','handbook','unofficial')),
    version          TEXT NOT NULL,
    effective_from   TEXT NOT NULL CHECK (effective_from GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
                                          AND date(effective_from) IS NOT NULL AND date(effective_from) = effective_from),
    effective_to     TEXT CHECK (effective_to IS NULL OR
                                 (effective_to GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
                                  AND date(effective_to) IS NOT NULL AND date(effective_to) = effective_to
                                  AND effective_to >= effective_from)),
    supersedes       TEXT NOT NULL DEFAULT '',
    scope_programmes TEXT NOT NULL DEFAULT 'ALL',
    scope_batches    TEXT NOT NULL DEFAULT 'ALL',
    provenance       TEXT NOT NULL,
    retrieved_on     TEXT NOT NULL CHECK (retrieved_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
                                          AND date(retrieved_on) IS NOT NULL AND date(retrieved_on) = retrieved_on),
    synthetic        TEXT NOT NULL DEFAULT 'N' CHECK (synthetic IN ('Y','N')),
    -- bookkeeping (not part of Annex B)
    content_sha256   TEXT,
    chunks_indexed   INTEGER CHECK (chunks_indexed IS NULL OR chunks_indexed >= 0),
    registered_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- ---------------------------------------------------------------------------
-- Annex C: students
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS students (
    student_id       TEXT PRIMARY KEY
                         CHECK (student_id GLOB 'S[0-9][0-9][0-9][0-9]'),
    full_name        TEXT NOT NULL CHECK (length(trim(full_name)) > 0),
    programme        TEXT NOT NULL CHECK (length(trim(programme)) > 0),
    batch_year       INTEGER NOT NULL CHECK (typeof(batch_year) = 'integer'
                                             AND batch_year BETWEEN 1990 AND 2100),
    current_semester INTEGER NOT NULL CHECK (typeof(current_semester) = 'integer'
                                             AND current_semester BETWEEN 1 AND 10),
    cgpa             REAL NOT NULL CHECK (typeof(cgpa) IN ('real','integer')
                                          AND cgpa BETWEEN 0 AND 10),
    active_backlogs  INTEGER NOT NULL DEFAULT 0 CHECK (typeof(active_backlogs) = 'integer'
                                                       AND active_backlogs >= 0)
);

-- ---------------------------------------------------------------------------
-- Annex C: courses
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS courses (
    course_code TEXT PRIMARY KEY CHECK (length(trim(course_code)) > 0),
    course_name TEXT NOT NULL CHECK (length(trim(course_name)) > 0),
    programme   TEXT NOT NULL CHECK (length(trim(programme)) > 0),
    semester    INTEGER NOT NULL CHECK (typeof(semester) = 'integer'
                                        AND semester BETWEEN 1 AND 10),
    credits     INTEGER NOT NULL CHECK (typeof(credits) = 'integer' AND credits > 0)
);

-- ---------------------------------------------------------------------------
-- Annex C: attendance. The percentage is COMPUTED by tools, never stored.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS attendance (
    student_id       TEXT NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
    course_code      TEXT NOT NULL REFERENCES courses(course_code) ON DELETE CASCADE,
    classes_held     INTEGER NOT NULL CHECK (typeof(classes_held) = 'integer' AND classes_held > 0),
    classes_attended INTEGER NOT NULL CHECK (typeof(classes_attended) = 'integer'
                                             AND classes_attended >= 0),
    PRIMARY KEY (student_id, course_code),
    CHECK (classes_attended <= classes_held)
);

-- ---------------------------------------------------------------------------
-- Annex C: results
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS results (
    student_id     TEXT NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
    course_code    TEXT NOT NULL REFERENCES courses(course_code) ON DELETE CASCADE,
    exam_session   TEXT NOT NULL CHECK (length(trim(exam_session)) > 0),
    exam_type      TEXT NOT NULL CHECK (exam_type IN ('REGULAR','SUPPLEMENTARY')),
    internal_marks INTEGER NOT NULL CHECK (typeof(internal_marks) = 'integer' AND internal_marks >= 0),
    external_marks INTEGER NOT NULL CHECK (typeof(external_marks) = 'integer' AND external_marks >= 0),
    total_marks    INTEGER NOT NULL CHECK (typeof(total_marks) = 'integer'),
    max_marks      INTEGER NOT NULL CHECK (typeof(max_marks) = 'integer' AND max_marks > 0),
    result         TEXT NOT NULL CHECK (result IN ('PASS','FAIL','ABSENT','DETAINED')),
    PRIMARY KEY (student_id, course_code, exam_session, exam_type),
    CHECK (total_marks = internal_marks + external_marks),
    CHECK (total_marks <= max_marks)
);

-- ---------------------------------------------------------------------------
-- Annex C: rule_registry. Tools read thresholds ONLY from here.
-- source_doc_id must exist in the Source Register (enforced by FK).
-- scope_programmes: 'ALL' or a comma/semicolon list ('B.Tech' also covers
--                   'B.Tech CSE'). scope_batches: 'ALL', '2023+', '2023',
--                   '2022-2024' or a comma list of those.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rule_registry (
    rule_id          TEXT PRIMARY KEY CHECK (length(trim(rule_id)) > 0),
    description      TEXT NOT NULL,
    parameter        TEXT NOT NULL CHECK (length(trim(parameter)) > 0),
    operator         TEXT NOT NULL CHECK (operator IN ('>=','<=','>','<','==','!=','between')),
    value            TEXT NOT NULL CHECK (length(trim(value)) > 0),
    scope_programmes TEXT NOT NULL DEFAULT 'ALL',
    scope_batches    TEXT NOT NULL DEFAULT 'ALL',
    effective_from   TEXT NOT NULL CHECK (effective_from GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
                                          AND date(effective_from) IS NOT NULL AND date(effective_from) = effective_from),
    effective_to     TEXT CHECK (effective_to IS NULL OR
                                 (effective_to GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
                                  AND date(effective_to) IS NOT NULL AND date(effective_to) = effective_to
                                  AND effective_to >= effective_from)),
    source_doc_id    TEXT NOT NULL REFERENCES source_register(doc_id),
    source_section   TEXT NOT NULL CHECK (length(trim(source_section)) > 0)
);

-- ---------------------------------------------------------------------------
-- Audit (Annex D + R10). Never holds raw question text. JSON columns hold
-- serialised lists. applied_rules / conflicts_detected / citations /
-- as_of_date are additive to the frozen AuditRecord model (R10 requires
-- rules and conflicts in the audit record).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit (
    trace_id            TEXT PRIMARY KEY CHECK (length(trim(trace_id)) > 0),
    timestamp           TEXT NOT NULL,
    student_id          TEXT,   -- no FK on purpose: audit must outlive student rows
    question_category   TEXT NOT NULL,
    sources_retrieved   TEXT NOT NULL DEFAULT '[]',
    precedence_decision TEXT,
    tools_invoked       TEXT NOT NULL DEFAULT '[]',
    answer_type         TEXT NOT NULL CHECK (answer_type IN
                            ('retrieved_fact','calculated','not_found',
                             'clarification_needed','refused','conflict_flagged')),
    model               TEXT NOT NULL,
    llm_calls           INTEGER NOT NULL DEFAULT 0 CHECK (llm_calls >= 0),
    tokens              INTEGER NOT NULL DEFAULT 0 CHECK (tokens >= 0),
    latency_ms          INTEGER NOT NULL DEFAULT 0 CHECK (latency_ms >= 0),
    applied_rules       TEXT NOT NULL DEFAULT '[]',
    conflicts_detected  TEXT NOT NULL DEFAULT '[]',
    citations           TEXT NOT NULL DEFAULT '[]',
    as_of_date          TEXT
);

-- ---------------------------------------------------------------------------
-- Indexes (primary-key lookups are already indexed).
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_students_prog_batch ON students (programme, batch_year);
CREATE INDEX IF NOT EXISTS idx_courses_programme    ON courses (programme);
CREATE INDEX IF NOT EXISTS idx_attendance_course    ON attendance (course_code);
CREATE INDEX IF NOT EXISTS idx_results_student      ON results (student_id, course_code);
CREATE INDEX IF NOT EXISTS idx_results_course       ON results (course_code);
CREATE INDEX IF NOT EXISTS idx_rules_param_dates    ON rule_registry (parameter, effective_from, effective_to);
CREATE INDEX IF NOT EXISTS idx_rules_source         ON rule_registry (source_doc_id);
CREATE INDEX IF NOT EXISTS idx_audit_student        ON audit (student_id);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp      ON audit (timestamp);

INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', '1');
