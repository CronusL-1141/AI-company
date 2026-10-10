-- DDL of the agents, teams and agent_activities tables exactly as `sqlite3 aiteam.db .schema <table>`
-- prints it on a production database (v1.15.1, PRAGMA user_version 20260728), including the
-- columns the startup migration appended over time. These are the tables the attribution
-- repair reads and writes; tests build their databases from this file.
CREATE TABLE agents (
	id VARCHAR(36) NOT NULL, 
	team_id VARCHAR(36) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	role VARCHAR(100) NOT NULL, 
	system_prompt TEXT NOT NULL, 
	model VARCHAR(100) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	config JSON NOT NULL, 
	source VARCHAR(20) NOT NULL, 
	session_id VARCHAR(100), 
	cc_tool_use_id VARCHAR(100), 
	current_task TEXT, 
	project_id VARCHAR(36), 
	current_phase_id VARCHAR(36), 
	trust_score FLOAT NOT NULL, 
	created_at DATETIME NOT NULL, 
	last_active_at DATETIME, ctx_tokens INTEGER, ctx_window INTEGER, ctx_pct FLOAT, transcript_path TEXT, ctx_measured_at DATETIME, reuse_domain VARCHAR(200), input_tokens INTEGER, output_tokens INTEGER, cache_creation_tokens INTEGER, cache_read_tokens INTEGER, tokens_measured_at DATETIME, tokens_source VARCHAR(20), harness VARCHAR(20), harness_version VARCHAR(40), dispatch_call_id VARCHAR(64), reasoning_output_tokens INTEGER, attribution_exclusion VARCHAR(32), attribution_exclusion_of VARCHAR(36), usage_unmeasurable VARCHAR(32), 
	PRIMARY KEY (id)
);
CREATE INDEX ix_agents_team_id ON agents (team_id);
CREATE UNIQUE INDEX uq_agents_cc_tool_use_id ON agents (cc_tool_use_id) WHERE cc_tool_use_id IS NOT NULL AND cc_tool_use_id != '';
CREATE TABLE teams (
	id VARCHAR(36) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	mode VARCHAR(20) NOT NULL, 
	project_id VARCHAR(36), 
	leader_agent_id VARCHAR(36), 
	status VARCHAR(20) NOT NULL, 
	summary VARCHAR(500) NOT NULL, 
	config JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	completed_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);
CREATE TABLE agent_activities (
	id VARCHAR(36) NOT NULL, 
	agent_id VARCHAR(36) NOT NULL, 
	session_id VARCHAR(100) NOT NULL, 
	tool_name VARCHAR(100) NOT NULL, 
	input_summary TEXT NOT NULL, 
	output_summary TEXT NOT NULL, 
	timestamp DATETIME NOT NULL, 
	duration_ms INTEGER, 
	status VARCHAR(20) NOT NULL, 
	error TEXT, turn_id VARCHAR(64), 
	PRIMARY KEY (id)
);
CREATE INDEX ix_agent_activities_agent_id ON agent_activities (agent_id);
CREATE INDEX ix_agent_activities_session_id ON agent_activities (session_id);
