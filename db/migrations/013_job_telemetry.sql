CREATE TABLE IF NOT EXISTS job_runs (
    job_run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_name TEXT NOT NULL,
    run_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    trigger_source TEXT,
    command TEXT,
    pid INTEGER,
    host TEXT,
    log_path TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    duration_seconds NUMERIC(12,3),
    error_type TEXT,
    error_message TEXT,
    summary TEXT,
    metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    context_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_job_runs_job_name_started_at
    ON job_runs (job_name, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_job_runs_status_started_at
    ON job_runs (status, started_at DESC);

CREATE TABLE IF NOT EXISTS job_incidents (
    incident_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canary_name TEXT NOT NULL,
    target_name TEXT NOT NULL,
    status TEXT NOT NULL,
    diagnosis_category TEXT,
    summary TEXT,
    details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    report_json_path TEXT,
    report_markdown_path TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_job_incidents_target_created_at
    ON job_incidents (target_name, created_at DESC);
