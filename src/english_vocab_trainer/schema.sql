CREATE TABLE IF NOT EXISTS words (
    id TEXT PRIMARY KEY,
    term TEXT NOT NULL,
    level INTEGER,
    transcript TEXT,
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
    rating TEXT NOT NULL CHECK(rating IN ('again', 'good', 'easy')),
    reviewed_at TEXT NOT NULL,
    voided_at TEXT
);

CREATE TABLE IF NOT EXISTS login_attempts (
    attempted_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_state_due ON word_state(due_at);
CREATE INDEX IF NOT EXISTS idx_events_word ON review_events(word_id, reviewed_at);
CREATE INDEX IF NOT EXISTS idx_login_attempts_at ON login_attempts(attempted_at);
