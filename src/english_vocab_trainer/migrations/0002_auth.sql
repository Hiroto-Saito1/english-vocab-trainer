CREATE TABLE login_attempts (attempted_at INTEGER NOT NULL);
CREATE INDEX idx_login_attempts_at ON login_attempts(attempted_at);
