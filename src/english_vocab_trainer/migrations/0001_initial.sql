CREATE TABLE IF NOT EXISTS schema_metadata (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL CHECK(version=1),
 catalog_sha256 TEXT, catalog_count INTEGER CHECK(catalog_count IS NULL OR catalog_count>=0), published_at TEXT
);
INSERT OR IGNORE INTO schema_metadata(singleton,version) VALUES(1,1);
CREATE TABLE IF NOT EXISTS words (
 id TEXT PRIMARY KEY, term TEXT NOT NULL, level INTEGER, definition TEXT NOT NULL, audio_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS reviews (
 id TEXT PRIMARY KEY, word_id TEXT NOT NULL REFERENCES words(id),
 action TEXT NOT NULL CHECK(action IN ('known','unknown')), reviewed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS word_state (
 word_id TEXT PRIMARY KEY REFERENCES words(id), due_at TEXT NOT NULL, card_json TEXT NOT NULL,
 version INTEGER NOT NULL CHECK(version>0), mutation_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_state_due ON word_state(due_at);
CREATE INDEX IF NOT EXISTS idx_reviews_word ON reviews(word_id,reviewed_at,id);
CREATE INDEX IF NOT EXISTS idx_reviews_history ON reviews(reviewed_at DESC,id DESC);
