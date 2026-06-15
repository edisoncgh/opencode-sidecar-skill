#!/usr/bin/env python3
"""Minimal self-test for sidecar.py core logic.

No external test framework required — run it directly:

    python scripts/selftest.py

Functions are named ``test_*`` and use plain ``assert``, so they are also
discoverable by pytest if it happens to be installed:

    pytest scripts/selftest.py

Scope is deliberately narrow: pure functions and lightweight wiring only.
No opencode, no network, no real worker. Covers:

  1. parse_event_stream      — text extraction vs tool_use/step/error noise
  2. detect_agent_fallback   — the three fallback signatures
  3. check_sensitive_files   — glob matching (pem / env / secrets)
  4. TaskConfig              — writable modes force worktree=True
  5. SidecarOrchestrator     — update_index upserts by task_id (no dup rows)
"""

import json
import os
import sys
import tempfile
from pathlib import Path

# Import the module under test from this directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sidecar  # noqa: E402


def _event(event_type: str, **payload) -> str:
    """Serialize one OpenCode --format json event line."""
    return json.dumps({"type": event_type, "timestamp": "t", "sessionID": "s", **payload})


# ── 1. Event stream parsing ────────────────────────────────────────────────

def test_event_stream_extracts_text_only():
    stream = "\n".join([
        _event("step_start", part={"type": "step"}),
        _event("tool_use", part={"type": "tool", "tool": "bash",
                                 "state": {"input": {"command": "git diff"}}}),
        _event("text", part={"type": "text", "text": "Hello worker answer"}),
        _event("error", error={"message": "boom"}),
        _event("step_finish", part={"type": "step"}),
        "this line is not json",
    ])
    parsed = sidecar.parse_event_stream(stream)

    assert parsed["is_json"] is True
    # Worker prose comes only from text events.
    assert parsed["text"] == "Hello worker answer"
    # Executed bash command captured separately.
    assert parsed["commands"] == ["git diff"]
    # Error surfaced, but NOT mixed into worker_text.
    assert any("boom" in e for e in parsed["errors"])
    assert "git diff" not in parsed["text"]
    assert "boom" not in parsed["text"]


def test_event_stream_non_json_is_marked_plain():
    parsed = sidecar.parse_event_stream("just plain text\nno json here")
    assert parsed["is_json"] is False
    assert parsed["text"] == ""
    assert parsed["commands"] == []


def test_event_stream_concatenates_multiple_text_events():
    stream = "\n".join([
        _event("text", part={"type": "text", "text": "part one"}),
        _event("text", part={"type": "text", "text": "part two"}),
    ])
    parsed = sidecar.parse_event_stream(stream)
    assert "part one" in parsed["text"]
    assert "part two" in parsed["text"]


# ── 2. Agent fallback detection ────────────────────────────────────────────

def test_fallback_subagent_signature():
    out = 'agent "sidecar-x" is a subagent, not a primary agent. Falling back to default agent'
    reason = sidecar.detect_agent_fallback(out)
    assert reason is not None
    assert "subagent" in reason.lower()


def test_fallback_not_found_signature():
    out = 'Falling back to default agent: agent "sidecar-x" not found'
    assert sidecar.detect_agent_fallback(out) is not None


def test_fallback_plain_phrase():
    assert sidecar.detect_agent_fallback("FALLING BACK TO DEFAULT AGENT") is not None


def test_fallback_negative():
    assert sidecar.detect_agent_fallback("everything fine, no fallback happened") is None


# ── 3. Sensitive file globs ────────────────────────────────────────────────

def _patch(*files: str) -> str:
    return "\n".join(f"diff --git a/{f} b/{f}" for f in files)


def test_sensitive_files_matched():
    assert sidecar.check_sensitive_files(_patch("certs/server.pem"))
    assert sidecar.check_sensitive_files(_patch(".env.local"))
    assert sidecar.check_sensitive_files(_patch("config/secrets.json"))
    assert sidecar.check_sensitive_files(_patch("home/user/.ssh/id_rsa"))


def test_sensitive_files_not_overmatched():
    assert not sidecar.check_sensitive_files(_patch("src/normal_code.ts"))
    assert not sidecar.check_sensitive_files(_patch("package.json"))
    assert not sidecar.check_sensitive_files(_patch("docs/readme.md"))


# ── 4. Writable modes force worktree ───────────────────────────────────────

def test_writable_modes_force_worktree():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        impl = sidecar.TaskConfig(mode="implement", goal="g", model="m", project_dir=proj)
        fix = sidecar.TaskConfig(mode="test-fix", goal="g", model="m", project_dir=proj)
        assert impl.worktree is True
        assert fix.worktree is True


def test_readonly_modes_do_not_force_worktree():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        for mode in ("explore", "review", "log"):
            tc = sidecar.TaskConfig(mode=mode, goal="g", model="m", project_dir=proj)
            assert tc.worktree is False, f"{mode} should not force worktree"


# ── 5. Lost-write fact-check ───────────────────────────────────────────────

def _tool_event(tool: str, status: str = "completed") -> str:
    return json.dumps({"type": "tool_use", "part": {"type": "tool", "tool": tool,
                                                     "state": {"status": status}}})


def test_attempted_writes_detects_write_tool():
    parsed = sidecar.parse_event_stream(_tool_event("write"))
    assert sidecar.worker_attempted_writes(parsed) is True


def test_attempted_writes_detects_mcp_tool():
    # ctx_execute (an MCP tool, not a known built-in) counts as a write attempt.
    parsed = sidecar.parse_event_stream(_tool_event("ctx_execute"))
    assert sidecar.worker_attempted_writes(parsed) is True


def test_attempted_writes_ignores_readonly_builtins():
    stream = "\n".join([_tool_event("read"), _tool_event("bash"), _tool_event("grep")])
    parsed = sidecar.parse_event_stream(stream)
    assert sidecar.worker_attempted_writes(parsed) is False


def test_claimed_changes_true_when_file_listed():
    text = "**Summary:** done\n\n**Files Changed:**\n- hello.txt — new file\n\n**Tests Run:** none"
    assert sidecar.worker_claimed_changes(text) is True


def test_claimed_changes_false_when_none():
    text = "**Summary:** nothing to do\n\n**Files Changed:**\n- None\n\n**Risks:** none"
    assert sidecar.worker_claimed_changes(text) is False


def test_claimed_changes_false_without_section():
    assert sidecar.worker_claimed_changes("just some prose, no report") is False


# ── 6. Index upsert ────────────────────────────────────────────────────────

def test_index_upsert_single_row_across_status_changes():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        orch = sidecar.SidecarOrchestrator(proj)
        tc = sidecar.TaskConfig(mode="explore", goal="g", model="m", project_dir=proj)
        task_dir = orch.get_task_dir(tc.task_id)
        task_dir.mkdir(parents=True, exist_ok=True)

        tc.status = "running"
        orch.update_index(tc, task_dir)
        tc.status = "completed"
        orch.update_index(tc, task_dir)

        index = sidecar.read_json(orch.sidecar_dir / sidecar.INDEX_FILE)
        rows = [t for t in index["tasks"] if t["task_id"] == tc.task_id]
        assert len(rows) == 1, f"expected exactly 1 row, got {len(rows)}"
        assert rows[0]["status"] == "completed"
        # created_at must survive the second write (not overwritten).
        assert rows[0]["created_at"] == tc.created_at


def test_index_distinct_tasks_get_distinct_rows():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        orch = sidecar.SidecarOrchestrator(proj)
        ids = []
        for mode in ("explore", "review"):
            tc = sidecar.TaskConfig(mode=mode, goal="g", model="m", project_dir=proj)
            task_dir = orch.get_task_dir(tc.task_id)
            task_dir.mkdir(parents=True, exist_ok=True)
            orch.update_index(tc, task_dir)
            ids.append(tc.task_id)
        index = sidecar.read_json(orch.sidecar_dir / sidecar.INDEX_FILE)
        task_ids = [t["task_id"] for t in index["tasks"]]
        assert sorted(task_ids) == sorted(ids)


# ── Runner ─────────────────────────────────────────────────────────────────

_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


def main() -> int:
    # Plain ASCII markers: Windows GBK console chokes on emoji / box drawing.
    passed = 0
    failed = 0
    for fn in _TESTS:
        try:
            fn()
            print(f"  [PASS] {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {fn.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001 - surface any unexpected error
            print(f"  [ERR ] {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print()
    print(f"{passed} passed, {failed} failed, {len(_TESTS)} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
