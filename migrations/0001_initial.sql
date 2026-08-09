PRAGMA foreign_keys = ON;
CREATE TABLE words (id TEXT PRIMARY KEY, term TEXT NOT NULL, level INTEGER, transcript TEXT, audio_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE user_word_state (user_id TEXT NOT NULL, word_id TEXT NOT NULL REFERENCES words(id), due_at TEXT NOT NULL, stability REAL NOT NULL DEFAULT 0, difficulty REAL NOT NULL DEFAULT 5, card_json TEXT, first_seen_at TEXT, first_known_at TEXT, last_known_at TEXT, version INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (user_id, word_id));
CREATE TABLE review_events (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, word_id TEXT NOT NULL REFERENCES words(id), rating TEXT NOT NULL CHECK(rating IN ('again','good','easy')), reviewed_at TEXT NOT NULL, voided_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE study_sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE session_items (session_id TEXT NOT NULL REFERENCES study_sessions(id), word_id TEXT NOT NULL REFERENCES words(id), ordinal INTEGER NOT NULL, PRIMARY KEY(session_id, word_id), UNIQUE(session_id, ordinal));
CREATE TABLE user_settings (user_id TEXT PRIMARY KEY, daily_target INTEGER NOT NULL DEFAULT 30 CHECK(daily_target BETWEEN 1 AND 100), updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX idx_state_due ON user_word_state(user_id, due_at);
CREATE INDEX idx_events_user_time ON review_events(user_id, reviewed_at);
