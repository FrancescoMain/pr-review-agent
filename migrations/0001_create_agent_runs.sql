-- 0001 — Create agent_runs and schema_version.
--
-- Idempotent: uses IF NOT EXISTS so apply_migrations can replay it
-- safely on every startup. Schema evolves via numbered files; the
-- schema_version table tracks the highest applied version.

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id                  BIGSERIAL PRIMARY KEY,
    correlation_id      TEXT NOT NULL,
    repo                TEXT NOT NULL,
    pr_number           INTEGER NOT NULL,
    head_sha            TEXT NOT NULL,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    status              TEXT NOT NULL,
    triage_change_type  TEXT,
    triage_risk_level   TEXT,
    skipped             BOOLEAN NOT NULL DEFAULT FALSE,
    tool_calls_used     INTEGER NOT NULL DEFAULT 0,
    tokens_input        INTEGER NOT NULL DEFAULT 0,
    tokens_output       INTEGER NOT NULL DEFAULT 0,
    cost_usd            NUMERIC(10, 6) NOT NULL DEFAULT 0,
    per_model           JSONB NOT NULL DEFAULT '{}'::jsonb,
    error               TEXT
);

CREATE INDEX IF NOT EXISTS agent_runs_correlation_id_idx
    ON agent_runs (correlation_id);
CREATE INDEX IF NOT EXISTS agent_runs_repo_pr_idx
    ON agent_runs (repo, pr_number);

INSERT INTO schema_version (version) VALUES (1)
    ON CONFLICT (version) DO NOTHING;
