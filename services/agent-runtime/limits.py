"""Pure agent hard-limit logic (Phase 6): no I/O, no framework deps, safe to unit-test standalone."""
import math

TERMINATION_MESSAGES = {
    "max_runtime": "I could not complete the request: the time budget for this run was exceeded.",
    "max_tool_calls": "I could not complete the request: too many tool calls were required.",
    "max_tokens": "I could not complete the request: the token budget for this run was exceeded.",
    "max_steps": "I could not complete the request within the allowed number of steps.",
    "repeated_tool_execution": "Agent terminated: repeated tool execution detected.",
}


def detect_loop(seq: list[str], max_period: int = 3, min_cycles: float = 2.5) -> bool:
    """Flags A,B,A,B,A-style thrashing: a tool-name sequence repeating with period 1-3 for ~2.5+ cycles."""
    for period in range(1, max_period + 1):
        needed = math.ceil(period * min_cycles)
        if len(seq) < needed:
            continue
        window = seq[-needed:]
        if all(window[i] == window[i - period] for i in range(period, len(window))):
            return True
    return False
