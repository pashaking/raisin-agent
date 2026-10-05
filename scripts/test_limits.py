#!/usr/bin/env python3
"""Unit tests for the agent-runtime hard limits (Phase 6): offline, no stack required.

Covers the loop-pattern detector (services/agent-runtime/limits.py, dependency-free) and the configured
ceilings. The end-to-end behavior (a real model run actually stopping at max_runtime / max_tool_calls /
repeated_tool_execution) is exercised qualitatively through the console, since it depends on live model
behavior that cannot be forced deterministically without mocking the LLM.
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
import config  # noqa: E402
import limits  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, info: str = ""):
    RESULTS.append((name, bool(cond), info))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{info}]" if info and not cond else ""))


def part_config():
    check("MAX_TOOL_ROUNDS is a positive int", isinstance(config.MAX_TOOL_ROUNDS, int) and config.MAX_TOOL_ROUNDS > 0)
    check("MAX_TOOL_CALLS >= MAX_TOOL_ROUNDS (at least one call/round possible)", config.MAX_TOOL_CALLS >= config.MAX_TOOL_ROUNDS)
    check("MAX_RUNTIME_S is positive", config.MAX_RUNTIME_S > 0)
    check("MAX_RETRIES_PER_TOOL is a non-negative int", isinstance(config.MAX_RETRIES_PER_TOOL, int) and config.MAX_RETRIES_PER_TOOL >= 0)
    check("MAX_TOKENS_PER_RUN is positive", config.MAX_TOKENS_PER_RUN > 0)


def part_loop_detector():
    # the plan's own worked example: Tool A, Tool B, Tool A, Tool B, Tool A
    check("plan example A,B,A,B,A -> detected", limits.detect_loop(["A", "B", "A", "B", "A"]))
    check("period-1 thrash X,X,X -> detected", limits.detect_loop(["X", "X", "X"]))
    check("period-3 thrash A,B,C,A,B,C,A,B -> detected", limits.detect_loop(["A", "B", "C", "A", "B", "C", "A", "B"]))
    check("no pattern -> not detected", not limits.detect_loop(["A", "B", "C", "D", "E"]))
    check("short history -> not detected", not limits.detect_loop(["A", "B"]))
    check("empty history -> not detected", not limits.detect_loop([]))
    check("varied-but-legitimate investigation sequence -> not detected",
          not limits.detect_loop(["get_campaign", "compare_transaction_periods", "get_payment_gateway_statistics", "get_fraud_signals"]))
    # two full cycles only (A,B,A,B) should not yet trip a period-2 pattern requiring ~2.5 cycles
    check("two full cycles only (A,B,A,B) -> not yet detected", not limits.detect_loop(["A", "B", "A", "B"]))
    check("TERMINATION_MESSAGES covers every non-complete reason",
          set(limits.TERMINATION_MESSAGES) == {"max_runtime", "max_tool_calls", "max_tokens", "max_steps", "repeated_tool_execution"})


if __name__ == "__main__":
    part_config()
    part_loop_detector()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\nLIMITS TESTS: {len(RESULTS) - len(failed)}/{len(RESULTS)} passed ->", "PASS" if not failed else "FAIL")
    sys.exit(0 if not failed else 1)
