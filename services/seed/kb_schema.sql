-- kb: knowledge base chunks with tenant metadata for filtered retrieval
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS chunks (
  id SERIAL PRIMARY KEY,
  doc_id TEXT NOT NULL,
  chunk_id INT NOT NULL,
  tenant_id TEXT NOT NULL,
  classification TEXT NOT NULL,
  content TEXT NOT NULL,
  embedding vector(1536) NOT NULL,
  UNIQUE (doc_id, chunk_id));
CREATE INDEX IF NOT EXISTS chunks_tenant_class ON chunks (tenant_id, classification);
