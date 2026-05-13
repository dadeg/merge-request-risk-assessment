CREATE TABLE IF NOT EXISTS seen_mrs (
  project_id INTEGER NOT NULL,
  mr_iid     INTEGER NOT NULL,
  head_sha   TEXT    NOT NULL,
  decided_at INTEGER NOT NULL,
  decision   TEXT    NOT NULL,
  risk       TEXT,
  confidence TEXT,
  PRIMARY KEY (project_id, mr_iid, head_sha)
);

CREATE TABLE IF NOT EXISTS rate_log (
  ts     INTEGER NOT NULL,
  action TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS rate_log_ts ON rate_log(ts);
