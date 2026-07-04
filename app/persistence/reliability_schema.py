RELIABILITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS domain_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','processing','retry','done','failed','dead_letter')),
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    next_retry_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    worker_id TEXT,
    trace_id TEXT NOT NULL,
    user_id INTEGER,
    last_error TEXT,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_domain_events_dispatch
    ON domain_events(status,next_retry_at,id);
CREATE INDEX IF NOT EXISTS idx_domain_events_trace ON domain_events(trace_id);

CREATE TABLE IF NOT EXISTS notification_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id TEXT NOT NULL UNIQUE,
    channel TEXT NOT NULL CHECK(channel IN ('in_app','email','webhook')),
    recipient TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    event_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','processing','sent','retry','failed','dead_letter')),
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    next_retry_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    trace_id TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notification_queue_dispatch
    ON notification_queue(status,next_retry_at,id);

CREATE TABLE IF NOT EXISTS background_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL UNIQUE,
    job_type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','processing','retry','done','failed','dead_letter')),
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    next_run_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    worker_id TEXT,
    trace_id TEXT NOT NULL,
    last_error TEXT,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_background_jobs_dispatch
    ON background_jobs(status,next_run_at,id);

CREATE TABLE IF NOT EXISTS worker_heartbeats (
    worker_name TEXT PRIMARY KEY,
    worker_id TEXT NOT NULL,
    status TEXT NOT NULL,
    last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS person_merge_workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL UNIQUE,
    source_person_id INTEGER NOT NULL,
    target_person_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK(status IN ('candidate','confirmed','rolled_back','failed')),
    requested_by INTEGER,
    confirmed_by INTEGER,
    rolled_back_by INTEGER,
    trace_id TEXT NOT NULL,
    snapshot_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(source_person_id != target_person_id)
);
CREATE INDEX IF NOT EXISTS idx_person_merge_status
    ON person_merge_workflows(status,created_at);
"""


def initialize_reliability_schema(connection):
    connection.executescript(RELIABILITY_SCHEMA)
