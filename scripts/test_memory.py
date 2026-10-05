#!/usr/bin/env python3
"""Unit tests for agent memory validation (Phase 8): offline, no stack required.

Covers services/agent-runtime/memory.py's validate_key/validate_value, the two gates save_task_state runs
model-supplied arguments through before anything reaches Postgres. The read/write paths themselves (get_user_context,
get_session_context, save_task_state against the kb database) are exercised end to end through the console/evals
against the running stack, since they need a real session and a real OPA decision.
"""
import os
import sys

os.environ.setdefault("LITELLM_BASE_URL", "http://localhost")
os.environ.setdefault("OPA_URL", "http://localhost")
os.environ.setdefault("GUARDRAILS_URL", "http://localhost")
os.environ.setdefault("PRESIDIO_ANALYZER_URL", "http://localhost")
os.environ.setdefault("PRESIDIO_ANONYMIZER_URL", "http://localhost")
os.environ.setdefault("RAISIN_API_URL", "http://localhost")
os.environ.setdefault("RUNTIME_SERVICE_TOKEN", "test")
os.environ.setdefault("KB_DATABASE_URL", "postgresql://test/test")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "agent-runtime"))
import memory  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, info: str = ""):
    RESULTS.append((name, bool(cond), info))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{info}]" if info and not cond else ""))


def part_validate_key():
    check("plain key -> valid", memory.validate_key("campaign_notes") is None)
    check("dotted/dashed key -> valid", memory.validate_key("step-1.result") is None)
    check("empty key -> invalid", memory.validate_key("") is not None)
    check("key with space -> invalid", memory.validate_key("has space") is not None)
    check("key with path traversal -> invalid", memory.validate_key("../../etc/passwd") is not None)
    check("65-char key -> invalid (max 64)", memory.validate_key("a" * 65) is not None)
    check("64-char key -> valid", memory.validate_key("a" * 64) is None)


def part_validate_value():
    check("small dict -> valid", memory.validate_value({"donor_id": 24, "note": "re-check next week"}) is None)
    check("string -> valid", memory.validate_value("ok") is None)
    check("number -> valid", memory.validate_value(42) is None)
    check("not JSON-serializable -> invalid", memory.validate_value(object()) is not None)
    check("oversized value -> invalid (max bytes)", memory.validate_value({"blob": "x" * (memory.MAX_VALUE_BYTES + 1)}) is not None)
    check("value right at the byte limit -> valid",
          memory.validate_value("x" * (memory.MAX_VALUE_BYTES - 2)) is None)  # -2 for the JSON-encoded quotes


if __name__ == "__main__":
    part_validate_key()
    part_validate_value()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\nMEMORY TESTS: {len(RESULTS) - len(failed)}/{len(RESULTS)} passed ->", "PASS" if not failed else "FAIL")
    sys.exit(0 if not failed else 1)
