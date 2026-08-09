ALTER TABLE words ADD COLUMN tier TEXT NOT NULL DEFAULT 'unknown'
    CHECK(tier IN ('upper', 'ultra', 'unknown'));
