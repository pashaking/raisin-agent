"""Thin psycopg helper. Every query is a `db.query` span with operation + table only; the SQL
text and parameters are never recorded (canary PII discipline)."""
import os

import psycopg
from opentelemetry import trace

tracer = trace.get_tracer("raisin-api.db")
DB_URL = os.environ["RAISIN_DATABASE_URL"]


def query(sql: str, params=(), one: bool = False, table: str = ""):
    op = sql.strip().split()[0].upper()
    with tracer.start_as_current_span("db.query") as span:
        span.set_attribute("db.system", "postgresql")
        span.set_attribute("db.operation", op)
        if table:
            span.set_attribute("db.sql.table", table)
        with psycopg.connect(DB_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description is None:
                return None
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            span.set_attribute("db.rows", len(rows))
            return (rows[0] if rows else None) if one else rows
