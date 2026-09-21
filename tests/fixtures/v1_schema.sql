-- tests/fixtures/v1_schema.sql
-- Schema v1 DDL snapshot (docs/07_db_design_v1_1.md, section 8.6 / DQ1).
-- Generated from Base.metadata.create_all of src/database/models.py at schema version 1
-- (SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type DESC, name).
-- Do not edit by hand: tests/test_migration.py builds its v1 file from this script.

CREATE TABLE chunks (
	id INTEGER NOT NULL, 
	job_id VARCHAR(36) NOT NULL, 
	kind VARCHAR(10) NOT NULL, 
	translatable BOOLEAN NOT NULL, 
	source_text TEXT NOT NULL, 
	content_hash VARCHAR(64) NOT NULL, 
	char_count INTEGER NOT NULL, 
	parent_block_id INTEGER, 
	sub_index INTEGER, 
	sub_count INTEGER, 
	heading_path TEXT DEFAULT '[]' NOT NULL, 
	status VARCHAR(10) DEFAULT 'PENDING' NOT NULL, 
	retry_count INTEGER DEFAULT 0 NOT NULL, 
	next_attempt_at VARCHAR(27), 
	last_error_code VARCHAR(40), 
	last_error VARCHAR(2000), 
	translated_text TEXT, 
	review_flag BOOLEAN DEFAULT 0 NOT NULL, 
	warnings TEXT DEFAULT '[]' NOT NULL, 
	provider VARCHAR(40), 
	glossary_strategy VARCHAR(12), 
	glossary_hash VARCHAR(64), 
	chars_sent INTEGER DEFAULT 0 NOT NULL, 
	chars_billed INTEGER DEFAULT 0 NOT NULL, 
	latency_ms INTEGER, 
	attempts INTEGER DEFAULT 0 NOT NULL, 
	last_run_id VARCHAR(12), 
	claimed_at VARCHAR(27), 
	completed_at VARCHAR(27), 
	created_at VARCHAR(27) NOT NULL, 
	updated_at VARCHAR(27) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_chunks_id CHECK (id >= 1), 
	CONSTRAINT ck_chunks_kind CHECK (kind IN ('TEXT', 'HEADING', 'LIST', 'TABLE', 'CODE', 'IMAGE', 'HR', 'FOOTNOTE')), 
	CONSTRAINT ck_chunks_nontranslatable_kinds CHECK (kind NOT IN ('CODE', 'IMAGE', 'HR') OR translatable = 0), 
	CONSTRAINT ck_chunks_content_hash_len CHECK (length(content_hash) = 64), 
	CONSTRAINT ck_chunks_char_count CHECK (char_count >= 0), 
	CONSTRAINT ck_chunks_subsplit CHECK (((parent_block_id IS NULL) = (sub_index IS NULL)) AND ((sub_index IS NULL) = (sub_count IS NULL)) AND (sub_index IS NULL OR (sub_index >= 0 AND sub_count >= 1 AND sub_index < sub_count))), 
	CONSTRAINT ck_chunks_status CHECK (status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED')), 
	CONSTRAINT ck_chunks_retry_count CHECK (retry_count >= 0), 
	CONSTRAINT ck_chunks_next_attempt_pending_only CHECK (status = 'PENDING' OR next_attempt_at IS NULL), 
	CONSTRAINT ck_chunks_last_error_len CHECK (last_error IS NULL OR length(last_error) <= 2000), 
	CONSTRAINT ck_chunks_translated_text_completed CHECK ((status = 'COMPLETED') = (translated_text IS NOT NULL)), 
	CONSTRAINT ck_chunks_glossary_strategy CHECK (glossary_strategy IS NULL OR glossary_strategy IN ('native', 'prompt', 'post_replace', 'none')), 
	CONSTRAINT ck_chunks_chars_sent CHECK (chars_sent >= 0), 
	CONSTRAINT ck_chunks_chars_billed CHECK (chars_billed >= 0), 
	CONSTRAINT ck_chunks_latency_ms CHECK (latency_ms IS NULL OR latency_ms >= 0), 
	CONSTRAINT ck_chunks_attempts CHECK (attempts >= 0), 
	CONSTRAINT ck_chunks_next_attempt_at_len CHECK (next_attempt_at IS NULL OR length(next_attempt_at) = 27), 
	CONSTRAINT ck_chunks_claimed_at_len CHECK (claimed_at IS NULL OR length(claimed_at) = 27), 
	CONSTRAINT ck_chunks_completed_at_len CHECK (completed_at IS NULL OR length(completed_at) = 27), 
	CONSTRAINT ck_chunks_created_at_len CHECK (created_at IS NULL OR length(created_at) = 27), 
	CONSTRAINT ck_chunks_updated_at_len CHECK (updated_at IS NULL OR length(updated_at) = 27), 
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE, 
	CONSTRAINT ck_chunks_translatable CHECK (translatable IN (0, 1)), 
	CONSTRAINT ck_chunks_review_flag CHECK (review_flag IN (0, 1))
);

CREATE TABLE jobs (
	id VARCHAR(36) NOT NULL, 
	singleton INTEGER DEFAULT 1 NOT NULL, 
	input_path TEXT NOT NULL, 
	input_sha256 VARCHAR(64) NOT NULL, 
	source_md_sha256 VARCHAR(64), 
	status VARCHAR(12) DEFAULT 'CREATED' NOT NULL, 
	tool_version VARCHAR(40) NOT NULL, 
	extractor_name VARCHAR(20), 
	fallback_used BOOLEAN DEFAULT 0 NOT NULL, 
	detected_language VARCHAR(8), 
	chunk_min_chars INTEGER, 
	chunk_max_chars INTEGER, 
	provider VARCHAR(40), 
	glossary_strategy VARCHAR(12), 
	provider_glossary_id VARCHAR(120), 
	glossary_hash VARCHAR(64), 
	warnings TEXT DEFAULT '[]' NOT NULL, 
	created_at VARCHAR(27) NOT NULL, 
	updated_at VARCHAR(27) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_jobs_singleton UNIQUE (singleton), 
	CONSTRAINT ck_jobs_id_len CHECK (length(id) = 36), 
	CONSTRAINT ck_jobs_singleton CHECK (singleton = 1), 
	CONSTRAINT ck_jobs_input_sha256_len CHECK (length(input_sha256) = 64), 
	CONSTRAINT ck_jobs_source_md_sha256_len CHECK (source_md_sha256 IS NULL OR length(source_md_sha256) = 64), 
	CONSTRAINT ck_jobs_status CHECK (status IN ('CREATED', 'EXTRACTED', 'CHUNKED', 'TRANSLATING', 'PAUSED', 'TRANSLATED', 'EXPORTED')), 
	CONSTRAINT ck_jobs_chunk_min_chars CHECK (chunk_min_chars IS NULL OR chunk_min_chars > 0), 
	CONSTRAINT ck_jobs_chunk_max_chars CHECK (chunk_max_chars IS NULL OR chunk_max_chars >= chunk_min_chars), 
	CONSTRAINT ck_jobs_glossary_strategy CHECK (glossary_strategy IS NULL OR glossary_strategy IN ('native', 'prompt', 'post_replace', 'none')), 
	CONSTRAINT ck_jobs_created_at_len CHECK (created_at IS NULL OR length(created_at) = 27), 
	CONSTRAINT ck_jobs_updated_at_len CHECK (updated_at IS NULL OR length(updated_at) = 27), 
	CONSTRAINT ck_jobs_fallback_used CHECK (fallback_used IN (0, 1))
);

CREATE TABLE lease (
	job_id VARCHAR(36) NOT NULL, 
	holder_pid INTEGER NOT NULL, 
	holder_host VARCHAR(255) NOT NULL, 
	run_id VARCHAR(12) NOT NULL, 
	acquired_at VARCHAR(27) NOT NULL, 
	heartbeat_at VARCHAR(27) NOT NULL, 
	PRIMARY KEY (job_id), 
	CONSTRAINT ck_lease_acquired_at_len CHECK (acquired_at IS NULL OR length(acquired_at) = 27), 
	CONSTRAINT ck_lease_heartbeat_at_len CHECK (heartbeat_at IS NULL OR length(heartbeat_at) = 27), 
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE
);

CREATE TABLE runs (
	id VARCHAR(12) NOT NULL, 
	job_id VARCHAR(36) NOT NULL, 
	command VARCHAR(10) NOT NULL, 
	tool_version VARCHAR(40) NOT NULL, 
	provider VARCHAR(40), 
	provider_switch_allowed BOOLEAN DEFAULT 0 NOT NULL, 
	started_at VARCHAR(27) NOT NULL, 
	finished_at VARCHAR(27), 
	outcome VARCHAR(12), 
	exit_code INTEGER, 
	chunks_completed INTEGER DEFAULT 0 NOT NULL, 
	chunks_failed INTEGER DEFAULT 0 NOT NULL, 
	chars_sent INTEGER DEFAULT 0 NOT NULL, 
	provider_calls INTEGER DEFAULT 0 NOT NULL, 
	rate_limited_count INTEGER DEFAULT 0 NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_runs_command CHECK (command IN ('run', 'extract', 'glossary', 'translate', 'export')), 
	CONSTRAINT ck_runs_outcome CHECK (outcome IS NULL OR outcome IN ('SUCCESS', 'PARTIAL', 'PAUSED', 'FAILED', 'INTERRUPTED')), 
	CONSTRAINT ck_runs_finished_outcome CHECK ((finished_at IS NULL) = (outcome IS NULL)), 
	CONSTRAINT ck_runs_chunks_completed CHECK (chunks_completed >= 0), 
	CONSTRAINT ck_runs_chunks_failed CHECK (chunks_failed >= 0), 
	CONSTRAINT ck_runs_chars_sent CHECK (chars_sent >= 0), 
	CONSTRAINT ck_runs_provider_calls CHECK (provider_calls >= 0), 
	CONSTRAINT ck_runs_rate_limited_count CHECK (rate_limited_count >= 0), 
	CONSTRAINT ck_runs_started_at_len CHECK (started_at IS NULL OR length(started_at) = 27), 
	CONSTRAINT ck_runs_finished_at_len CHECK (finished_at IS NULL OR length(finished_at) = 27), 
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE, 
	CONSTRAINT ck_runs_provider_switch_allowed CHECK (provider_switch_allowed IN (0, 1))
);

CREATE TABLE schema_meta (
	id INTEGER NOT NULL, 
	schema_version INTEGER NOT NULL, 
	created_by_tool_version VARCHAR(40) NOT NULL, 
	created_at VARCHAR(27) NOT NULL, 
	upgraded_by_tool_version VARCHAR(40), 
	upgraded_at VARCHAR(27), 
	PRIMARY KEY (id), 
	CONSTRAINT ck_schema_meta_id CHECK (id = 1), 
	CONSTRAINT ck_schema_meta_schema_version CHECK (schema_version >= 1), 
	CONSTRAINT ck_schema_meta_created_at_len CHECK (created_at IS NULL OR length(created_at) = 27), 
	CONSTRAINT ck_schema_meta_upgraded_at_len CHECK (upgraded_at IS NULL OR length(upgraded_at) = 27)
);

CREATE INDEX ix_chunks_job_status ON chunks (job_id, status);

CREATE INDEX ix_chunks_pending_backoff ON chunks (job_id, next_attempt_at) WHERE status = 'PENDING';

CREATE INDEX ix_chunks_review ON chunks (job_id) WHERE review_flag = 1;

INSERT INTO schema_meta (id, schema_version, created_by_tool_version, created_at, upgraded_by_tool_version, upgraded_at)
VALUES (1, 1, '0.1.0-v1', '2026-09-01T00:00:00.000000Z', NULL, NULL);
