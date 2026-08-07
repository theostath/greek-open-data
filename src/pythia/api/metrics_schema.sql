-- Metrics store (issue #22). One row per answered question.
--
-- There is deliberately NO column that could hold the question text, the narration, or any
-- cell value. The question is user content (CLAUDE.md §6) and the rows are third-party data;
-- neither belongs in a file that exists only to count things. `question_chars` is the safe
-- substitute, and a test asserts no text-shaped column reappears.

CREATE TABLE IF NOT EXISTS answers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    asked_at            TEXT    NOT NULL,   -- ISO-8601 UTC
    question_chars      INTEGER NOT NULL,   -- length only, never the text
    language            TEXT    NOT NULL,   -- detection label: el | en | greeklish
    pinned              INTEGER NOT NULL,   -- 1 = browse handoff, so retrieval was bypassed

    -- Planning
    plan_status         TEXT,               -- matched | unsupported | no_match
    dataset_id          TEXT,               -- public catalogue id; not personal data
    confidence          REAL,
    plan_degraded       INTEGER NOT NULL,   -- chosen on the score floor, LLM unavailable

    -- Synthesis
    answer_status       TEXT    NOT NULL,   -- answered | partial | refused
    refusal_shape       TEXT,               -- no_match | unsupported | matched_but_refused
    narration_rejected  INTEGER NOT NULL,   -- the claim guard fired
    caveats             INTEGER NOT NULL,

    -- Access
    row_count           INTEGER,            -- NULL when nothing was fetched
    complete            INTEGER,
    from_cache          INTEGER,

    -- Cost
    prompt_tokens       INTEGER NOT NULL,
    completion_tokens   INTEGER NOT NULL,
    llm_calls           INTEGER NOT NULL,
    llm_ms              REAL    NOT NULL,

    -- Stage wall-clock, derived from the same callback that drives the progress fragment,
    -- so what is measured is exactly what the user was shown.
    plan_ms             REAL    NOT NULL DEFAULT 0,
    fetch_ms            REAL    NOT NULL DEFAULT 0,
    synth_ms            REAL    NOT NULL DEFAULT 0,
    total_ms            REAL    NOT NULL
);

-- Every aggregate is either time-ordered or a full scan; this serves the purge and the
-- recent-window views.
CREATE INDEX IF NOT EXISTS idx_answers_asked_at ON answers(asked_at);
