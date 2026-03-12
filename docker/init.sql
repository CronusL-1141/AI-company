-- AI Team OS — PostgreSQL 初始化脚本
-- 由 docker-entrypoint-initdb.d 自动执行

-- 向量搜索扩展 (pgvector)
CREATE EXTENSION IF NOT EXISTS vector;

-- 三字组模糊搜索扩展
CREATE EXTENSION IF NOT EXISTS pg_trgm;
