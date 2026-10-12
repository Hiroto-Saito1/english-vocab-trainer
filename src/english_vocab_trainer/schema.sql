CREATE TABLE IF NOT EXISTS schema_metadata (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    version INTEGER NOT NULL
);

INSERT OR IGNORE INTO schema_metadata(singleton, version) VALUES(1, 3);

CREATE TABLE IF NOT EXISTS words (
    id TEXT PRIMARY KEY,
    term TEXT NOT NULL,
    level INTEGER,
    definition TEXT,
    audio_key TEXT NOT NULL UNIQUE,
    tier TEXT NOT NULL CHECK(tier IN ('upper', 'ultra'))
);

CREATE TABLE IF NOT EXISTS word_state (
    word_id TEXT PRIMARY KEY REFERENCES words(id),
    due_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_events (
    id TEXT PRIMARY KEY,
    word_id TEXT NOT NULL REFERENCES words(id),
    rating TEXT NOT NULL CHECK(rating IN ('again', 'good')),
    reviewed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS login_attempts (
    attempted_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_state_due ON word_state(due_at);
CREATE INDEX IF NOT EXISTS idx_events_word ON review_events(word_id, reviewed_at, id);
CREATE INDEX IF NOT EXISTS idx_events_history ON review_events(reviewed_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_login_attempts_at ON login_attempts(attempted_at);
