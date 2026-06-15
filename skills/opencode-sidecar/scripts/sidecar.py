#!/usr/bin/env python3
"""
OpenCode Sidecar Orchestrator

Delegates bounded coding sub-tasks from Claude Code to OpenCode worker agents
using cheaper models. Supports read-only exploration, review, log analysis,
and isolated worktree implementation attempts.

Usage:
    python sidecar.py explore --goal "<goal>" [--model <model>] [--dir <dir>]
    python sidecar.py review --scope "<scope>" [--model <model>] [--dir <dir>]
    python sidecar.py log --log-file <path> --goal "<goal>" [--model <model>]
    python sidecar.py implement --goal "<goal>" [--model <model>]
    python sidecar.py test-fix --goal "<goal>" [--model <model>]
    python sidecar.py collect --task-id <task-id>
    python sidecar.py list
    python sidecar.py cleanup --task-id <task-id>
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _kill_process_tree(proc: "subprocess.Popen") -> None:
    """Terminate a worker process and all of its children.

    opencode runs behind a shell/CMD wrapper that spawns node, so killing only
    the direct child leaves orphans. On Windows we use `taskkill /T` to reap the
    whole tree; on POSIX we signal the process group created via start_new_session.
    """
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=15,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001 - fall back to a direct kill
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        pass


# ── Constants ──────────────────────────────────────────────────────────────

SIDECAR_ROOT = ".agent_sidecars"
TASKS_DIR = "tasks"
WORKTREES_DIR = "worktrees"
INDEX_FILE = "index.json"

# Maps each mode to its OpenCode worker agent. The agent name equals the
# markdown file name (without .md) under opencode/agents/. These are OpenCode
# *primary* agents (mode: primary) so `opencode run --agent <name>` selects
# them directly without falling back to the default agent. They carry
# engine-enforced permissions (read-only vs. worktree-writable), so each worker
# is constrained by OpenCode itself, not only by prompt instructions.
#
# The agent name is also the canonical `worker` identity recorded in task.json
# and result.json (one naming system, matching schemas/*.schema.json).
AGENT_MAP = {
    "explore": "sidecar-explorer",
    "review": "sidecar-reviewer",
    "log": "sidecar-log-analyst",
    "implement": "sidecar-implementer",
    "test-fix": "sidecar-test-fixer",
}

# `worker` in the task/result envelopes is the agent name (single source of
# truth). Kept as an alias so existing references read clearly.
WORKER_MAP = AGENT_MAP

# Modes whose worker writes files; these MUST run inside an isolated git
# worktree (never the main working tree).
WRITABLE_MODES = {"implement", "test-fix"}

TEMPLATE_MAP = {
    "explore": "task_explore.md",
    "review": "task_review.md",
    "log": "task_log.md",
    "implement": "task_implement.md",
    "test-fix": "task_test_fix.md",
}

DEFAULT_TIMEOUT = {
    "explore": 180,
    "review": 180,
    "log": 180,
    "implement": 600,
    "test-fix": 600,
}

# Two-tier model routing. Modes map to one of two tiers based on whether the
# task is read-heavy/speed-sensitive (fast) or judgment/write-heavy (quality).
FAST_MODES = {"explore", "log"}
QUALITY_MODES = {"review", "implement", "test-fix"}
MODE_TIERS = {m: "fast" for m in FAST_MODES}
MODE_TIERS.update({m: "quality" for m in QUALITY_MODES})

CONFIG_FILENAME = ".opencode-sidecar.json"

# Env vars to override each tier (optional, one level above the config file).
ENV_FAST_MODEL = "OPENCODE_SIDECAR_FAST_MODEL"
ENV_QUALITY_MODEL = "OPENCODE_SIDECAR_QUALITY_MODEL"

# Glob patterns (fnmatch) for files a worker patch must never touch. Matched
# against each changed file path with fnmatch, so wildcards work correctly
# (a substring check could never match "*.pem"). Patterns are tested against
# both the full path and the basename so "*.pem" matches "certs/server.pem".
SENSITIVE_FILES = [
    "*.env", "*.env.*", ".env", ".env.*",
    "*.pem", "*.key", "*.p12", "*.pfx",
    "id_rsa", "id_ed25519", "*id_rsa*", "*id_ed25519*",
    "*secret*", "*credential*",
]

FORBIDDEN_COMMANDS = [
    "git commit", "git push",
    "npm install", "pnpm add", "yarn add",
    "pip install",
    "docker compose up", "docker compose down",
    "rm -rf",
]


# ── Helpers ────────────────────────────────────────────────────────────────

def generate_task_id(project_dir: Path) -> str:
    """Atomically claim a unique task ID in YYYY-MM-DD-NNN format.

    The ID is allocated under ``<project_dir>/.agent_sidecars/tasks/`` so that a
    task launched with ``--dir B`` only ever writes inside B — never the
    current working directory. This keeps the task-id allocation location and
    the actual artifact-write location identical.

    Concurrency-safe: the task directory is created with exist_ok=False as part
    of ID generation, so the OS guarantees only one caller can win a given ID.
    If two sidecar processes start at the same instant, the loser's mkdir fails
    and it retries with the next number rather than silently reusing an ID
    (which would cause two workers to write into the same task directory).
    """
    today = datetime.now().strftime("%Y-%m-%d")
    tasks_dir = project_dir.resolve() / SIDECAR_ROOT / TASKS_DIR
    tasks_dir.mkdir(parents=True, exist_ok=True)

    # Determine the highest existing number for today as a starting point.
    existing_nums = []
    for d in tasks_dir.iterdir():
        if d.is_dir() and d.name.startswith(today):
            try:
                existing_nums.append(int(d.name.split("-")[-1]))
            except ValueError:
                pass
    start = (max(existing_nums) + 1) if existing_nums else 1

    # Probe upward, claiming the first number whose directory we can create.
    for num in range(start, start + 1000):
        task_id = f"{today}-{num:03d}"
        try:
            (tasks_dir / task_id).mkdir(exist_ok=False)
            return task_id
        except FileExistsError:
            continue  # lost the race for this id; try the next one

    raise RuntimeError("Could not allocate a unique task id after 1000 attempts.")


def get_config_path(project_dir: Path) -> Path:
    """Path to the per-project sidecar model config."""
    return project_dir / CONFIG_FILENAME


def read_config(project_dir: Path) -> dict | None:
    """Read the per-project model config, or None if not configured."""
    return read_json(get_config_path(project_dir))


def write_config(project_dir: Path, fast_model: str, quality_model: str) -> Path:
    """Write the per-project model config. Returns the config path."""
    path = get_config_path(project_dir)
    data = {
        "fast_model": fast_model,
        "quality_model": quality_model,
        "configured_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


# Keyword scoring for auto-detecting which authed model is "fast" vs "quality".
# Higher score = stronger signal for that tier. Used only as a first-run
# fallback guess; the user confirms via `sidecar.py init`.
_FAST_SIGNALS = ["flash", "mini", "lite", "haiku", "nano", "small", "instant", "turbo"]
_QUALITY_SIGNALS = ["pro", "max", "ultra", "opus", "reasoning", "thinking", "o1", "r1"]


def _score_model(model_id: str) -> tuple[int, int]:
    """Return (fast_score, quality_score) for a model id."""
    low = model_id.lower()
    fast = sum(2 for s in _FAST_SIGNALS if s in low)
    quality = sum(2 for s in _QUALITY_SIGNALS if s in low)
    return fast, quality


def list_loaded_agents(config_dir: Path | None = None) -> dict[str, str]:
    """Return {agent_name: mode} as OpenCode resolves them.

    Runs `opencode agent list` with OPENCODE_CONFIG_DIR set to the bundled
    config dir (so the sidecar agents are included) and parses the
    "name (mode)" header lines it prints. Used by `doctor` to confirm each
    worker agent loads and is `primary` (not `subagent`, which would make
    `opencode run --agent` fall back to the default agent).
    """
    import re

    opencode = get_opencode_path()
    if not opencode:
        return {}
    env = os.environ.copy()
    env["OPENCODE_CONFIG_DIR"] = str(config_dir or get_opencode_config_dir())
    try:
        r = subprocess.run(
            [opencode, "agent", "list"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30, env=env,
        )
    except Exception:  # noqa: BLE001
        return {}
    out = r.stdout if r.returncode == 0 else ""
    agents: dict[str, str] = {}
    # Header lines look like: "sidecar-reviewer (primary)"
    pat = re.compile(r"^([A-Za-z0-9._-]+)\s+\((primary|subagent|all)\)\s*$")
    for line in out.splitlines():
        m = pat.match(_strip_ansi(line).strip())
        if m:
            agents[m.group(1)] = m.group(2)
    return agents


def detect_available_models() -> tuple[list[str], list[str]]:
    """Probe opencode for (all known model ids, authed provider display names).

    Runs `opencode models` (one `provider/model` id per line) and
    `opencode auth list` (the canonical command; `providers list` is an alias).
    `auth list` prints a boxed list where each authed provider is a
    "<bullet>  Name api" row. Returns two lists; either may be empty if the
    commands fail.
    """
    def _run(args: list[str]) -> str:
        try:
            r = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            return r.stdout if r.returncode == 0 else ""
        except Exception:  # noqa: BLE001
            return ""

    opencode = get_opencode_path() or "opencode"
    models_out = _run([opencode, "models"])
    # `auth list` is the documented command; fall back to its `providers` alias.
    auth_out = _run([opencode, "auth", "list"]) or _run([opencode, "providers", "list"])

    model_ids: list[str] = []
    for line in models_out.splitlines():
        line = _strip_ansi(line).strip()
        # model ids look like provider/model or provider/sub/model
        if "/" in line and not line.startswith("opencode ") and " " not in line:
            model_ids.append(line)

    authed_names: list[str] = []
    for line in auth_out.splitlines():
        # strip ANSI escape codes and box-drawing chars first
        clean_line = _strip_ansi(line)
        for box in ("┌", "│", "└", "├", "─"):
            clean_line = clean_line.replace(box, " ")
        # authed providers appear as "<bullet>  Name api" where bullet is
        # U+2022 (•) or U+25CF (●)
        has_bullet = any(b in clean_line for b in ("•", "●"))
        if has_bullet and "api" in clean_line:
            name = clean_line
            for b in ("•", "●"):
                name = name.replace(b, "")
            name = name.strip()
            name = name.rsplit("api", 1)[0].strip()
            if name:
                authed_names.append(name)

    return model_ids, authed_names


_ANSI_RE = None


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences (colors, cursor moves) from text."""
    global _ANSI_RE
    if _ANSI_RE is None:
        import re
        _ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
    return _ANSI_RE.sub("", text)


def auto_pick_models() -> tuple[str | None, str | None]:
    """Pick a fast and a quality model from what opencode reports available.

    Heuristic: score every model id by keyword signals. The highest-scoring
    model wins each tier. Returns (fast, quality); either may be None if
    detection yields nothing.
    """
    model_ids, _authed = detect_available_models()
    if not model_ids:
        return None, None

    best_fast: tuple[int, str] | None = None
    best_quality: tuple[int, str] | None = None
    for mid in model_ids:
        f, q = _score_model(mid)
        if f > 0 and (best_fast is None or f > best_fast[0]):
            best_fast = (f, mid)
        if q > 0 and (best_quality is None or q > best_quality[0]):
            best_quality = (q, mid)

    fast = best_fast[1] if best_fast else None
    quality = best_quality[1] if best_quality else None
    # If only one tier got a match, fall back to the other
    if fast and not quality:
        quality = fast
    if quality and not fast:
        fast = quality
    return fast, quality


def resolve_model(mode: str, project_dir: Path, cli_model: str | None = None) -> str:
    """Resolve the model id for a task mode.

    Priority: CLI --model > tier env var > project config file > auto-detect.

    On first run with no config, this auto-detects from opencode's authed
    models, writes the config (so subsequent runs are stable), and prints a
    notice to stderr recommending `sidecar.py init` to confirm or change it.
    """
    if cli_model:
        return cli_model

    tier = MODE_TIERS.get(mode, "fast")
    env_var = ENV_FAST_MODEL if tier == "fast" else ENV_QUALITY_MODEL
    env_model = os.environ.get(env_var)
    if env_model:
        return env_model

    config = read_config(project_dir)
    if config:
        key = "fast_model" if tier == "fast" else "quality_model"
        configured = config.get(key)
        if configured:
            return configured

    # No config yet — auto-detect once and persist.
    fast, quality = auto_pick_models()
    chosen = fast if tier == "fast" else quality
    if not chosen:
        raise RuntimeError(
            "Could not auto-detect an opencode model for this task, and no "
            "model config exists. Run `sidecar.py init` to configure, or pass "
            "--model <provider/model> explicitly."
        )
    # Persist whatever we detected (filling the other tier if missing) so the
    # next run is stable and so the user can inspect/edit the file.
    write_config(project_dir, fast or chosen, quality or chosen)
    print(
        f"[sidecar] No model config found. Auto-detected and wrote "
        f"{CONFIG_FILENAME}: fast={fast or chosen}, quality={quality or chosen}. "
        f"Run `sidecar.py init` to review or change.",
        file=sys.stderr,
    )
    return chosen


def parse_patch_files(patch_text: str) -> set[str]:
    """Extract the set of file paths touched by a unified diff / git patch.

    Reads the `diff --git a/<path> b/<path>` headers (and falls back to
    `+++ b/<path>` lines) so the main agent can detect when two parallel
    worktree patches modify the same file before applying either.
    """
    files: set[str] = set()
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            # Format: diff --git a/path b/path
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                files.add(parts[1].strip())
        elif line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            if path and path != "/dev/null":
                files.add(path)
    return files


def get_skill_dir() -> Path:
    """Get the skill directory containing this script."""
    return Path(__file__).resolve().parent.parent
def get_templates_dir() -> Path:
    """Get the templates directory."""
    return get_skill_dir() / "templates"


def get_opencode_config_dir() -> Path:
    """Directory passed to OpenCode via OPENCODE_CONFIG_DIR.

    OpenCode loads agents/commands/modes/plugins from `<dir>/agents/` etc.,
    exactly like a `.opencode` directory. Pointing it at the skill's bundled
    `opencode/` folder makes the sidecar worker agents available without
    copying anything into the user's project `.opencode/agents/` and without
    losing the user's provider/auth config (verified on opencode 1.17.4).
    """
    return get_skill_dir() / "opencode"


def get_skill_agents_dir() -> Path:
    """Get the directory holding the bundled OpenCode worker agent definitions."""
    return get_opencode_config_dir() / "agents"


def check_git_repo(project_dir: Path) -> bool:
    """Check if the given directory is inside a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def check_opencode_available() -> bool:
    """Check if opencode CLI is available."""
    return get_opencode_path() is not None


def get_opencode_path() -> str | None:
    """Get the full path to the opencode executable."""
    return shutil.which("opencode")


def check_dirty_worktree(project_dir: Path) -> bool:
    """Check if the working tree has uncommitted changes."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return bool(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def load_template(mode: str, task_id: str, goal: str, scope: str = "", log_file: str = "", worktree_path: str = "") -> str:
    """Load and fill a task template."""
    template_name = TEMPLATE_MAP.get(mode)
    if not template_name:
        return f"# Sidecar Task\n\nTask ID: {task_id}\nGoal: {goal}\n"

    template_path = get_templates_dir() / template_name
    if not template_path.exists():
        return f"# Sidecar Task\n\nTask ID: {task_id}\nGoal: {goal}\n"

    content = template_path.read_text(encoding="utf-8")
    content = content.replace("{{TASK_ID}}", task_id)
    content = content.replace("{{GOAL}}", goal)
    content = content.replace("{{SCOPE}}", scope or "Current repository.")
    content = content.replace("{{LOG_FILE}}", log_file or "N/A")
    content = content.replace("{{WORKTREE_PATH}}", worktree_path or "N/A")
    return content


def check_sensitive_files(patch_content: str) -> list[str]:
    """Flag any sensitive files touched by a patch.

    Uses the changed-file set parsed from the diff headers and matches each
    path against SENSITIVE_FILES with fnmatch (so glob patterns like "*.pem"
    actually match). Each pattern is tested against both the full path and the
    basename, so "*.pem" catches "certs/server.pem".
    """
    import fnmatch

    warnings = []
    changed = parse_patch_files(patch_content)
    for path in sorted(changed):
        base = path.rsplit("/", 1)[-1]
        for pattern in SENSITIVE_FILES:
            if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(base, pattern):
                warnings.append(
                    f"Patch touches sensitive file: {path} (matched pattern: {pattern})"
                )
                break
    return warnings


def parse_event_stream(output: str) -> dict:
    """Parse an `opencode run --format json` event stream.

    The stream is line-delimited JSON (one object per line), NOT a single
    final-answer JSON. Each object looks like:

        {"type": "<type>", "timestamp": ..., "sessionID": "...", "part": {...}}

    Verified event types (opencode 1.17.4): `step_start`, `text`,
    `step_finish`, `tool_use`, `reasoning`, `error`. The assistant's prose
    lives in `text` events under `part.text`; an executed shell command lives
    in a `tool_use` event whose `part.tool == "bash"` at
    `part.state.input.command`.

    Returns a dict with:
      - "is_json":  True if at least one well-formed event line was seen.
      - "text":     concatenation of all `text` event `part.text` values
                    (the worker's human-readable answer).
      - "commands": list of executed shell command strings.
      - "errors":   list of error event payloads (stringified).
      - "events":   the raw parsed event objects (for events.jsonl / debugging).
    """
    text_parts: list[str] = []
    commands: list[str] = []
    errors: list[str] = []
    events: list[dict] = []
    saw_json = False

    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        saw_json = True
        events.append(event)

        etype = event.get("type")
        part = event.get("part") if isinstance(event.get("part"), dict) else {}

        if etype == "text":
            txt = part.get("text")
            if isinstance(txt, str) and txt.strip():
                text_parts.append(txt)
        elif etype == "tool_use" and part.get("tool") == "bash":
            state = part.get("state") or {}
            inp = state.get("input") or {}
            cmd = inp.get("command")
            if isinstance(cmd, str):
                commands.append(cmd)
        elif etype == "error":
            err = event.get("error")
            if err is not None:
                errors.append(json.dumps(err, ensure_ascii=False) if not isinstance(err, str) else err)

    return {
        "is_json": saw_json,
        "text": "\n".join(text_parts),
        "commands": commands,
        "errors": errors,
        "events": events,
    }


def _extract_executed_commands(output: str) -> list[str] | None:
    """Pull actually-executed shell commands out of opencode JSON event output.

    Returns the list of executed command strings, or None if the output isn't
    a structured JSON event stream (so the caller can fall back to a raw
    substring scan of plain text).
    """
    parsed = parse_event_stream(output)
    return parsed["commands"] if parsed["is_json"] else None


def check_forbidden_commands(output: str) -> list[str]:
    """Flag forbidden command invocations the worker actually executed.

    Prefers parsing the structured JSON event stream so that forbidden strings
    appearing only in the agent's narration or injected reminders are not
    mistaken for executed commands. Falls back to a raw substring scan for
    plain-text output.
    """
    executed = _extract_executed_commands(output)
    haystacks = executed if executed is not None else [output]

    violations = []
    for cmd in FORBIDDEN_COMMANDS:
        if any(cmd in h for h in haystacks):
            violations.append(f"Forbidden command detected: {cmd}")
    return violations


def detect_agent_fallback(output: str) -> str | None:
    """Detect OpenCode's "falling back to default agent" warning.

    `opencode run --agent <name>` prints a warning and silently runs the
    default agent when the named agent is missing or is a subagent (verified
    in run.ts `localAgent()` / `attachAgent()`). When that happens the worker
    runs WITHOUT the agent's engine-enforced permissions, so the sidecar
    safety model is void. Returns the matched reason string, or None.
    """
    low = output.lower()
    if "falling back to default agent" in low:
        if "is a subagent" in low:
            return "named agent is a subagent (must be mode: primary)"
        if "not found" in low:
            return "named agent not found"
        return "unspecified fallback"
    return None


def write_json(path: Path, data: dict) -> None:
    """Write JSON data to a file."""
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict | None:
    """Read JSON data from a file."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        return None


# ── Task Config ────────────────────────────────────────────────────────────

class TaskConfig:
    """Configuration for a sidecar task."""

    def __init__(self, mode: str, goal: str, model: str, project_dir: Path,
                 worktree: bool = False, scope: str = "", log_file: str = "",
                 timeout: int | None = None):
        self.task_id = generate_task_id(project_dir)
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.mode = mode
        self.worker = WORKER_MAP[mode]
        self.model = model
        self.project_dir = str(project_dir.resolve())
        # Writable modes MUST run in an isolated git worktree — never the main
        # working tree. Enforced here so no CLI path can bypass it.
        self.worktree = True if mode in WRITABLE_MODES else worktree
        self.goal = goal
        self.scope = scope
        self.log_file = log_file
        self.timeout = timeout or DEFAULT_TIMEOUT.get(mode, 300)
        self.status = "created"

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "created_at": self.created_at,
            "mode": self.mode,
            "worker": self.worker,
            "model": self.model,
            "project_dir": self.project_dir,
            "worktree": self.worktree,
            "goal": self.goal,
            "scope": self.scope,
            "log_file": self.log_file,
            "allowed_actions": self._allowed_actions(),
            "forbidden_actions": self._forbidden_actions(),
            "output_contract": "result.json and result.md",
            "status": self.status,
        }

    def _allowed_actions(self) -> list[str]:
        base = ["read_files", "list_files", "search_files", "run_git_status", "run_git_diff"]
        if self.mode in ("explore", "review", "log"):
            base.append("run_search_commands")
        if self.mode in ("implement", "test-fix"):
            base.extend(["edit_files_in_worktree", "run_tests", "run_lint"])
        return base

    def _forbidden_actions(self) -> list[str]:
        base = ["commit", "push", "install_dependencies", "read_secrets", "deploy"]
        if self.mode in ("explore", "review", "log"):
            base.append("modify_files")
        if self.mode in ("implement", "test-fix"):
            base.extend(["edit_main_worktree", "modify_env_files"])
        return base


# ── Orchestrator ───────────────────────────────────────────────────────────

class SidecarOrchestrator:
    """Main orchestrator for sidecar tasks."""

    def __init__(self, project_dir: Path):
        self.project_dir = project_dir.resolve()
        self.sidecar_dir = self.project_dir / SIDECAR_ROOT
        self.tasks_dir = self.sidecar_dir / TASKS_DIR
        self.worktrees_dir = self.sidecar_dir / WORKTREES_DIR

    def ensure_dirs(self) -> None:
        """Create sidecar directories if they don't exist."""
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)

    def available_agents(self) -> list[str]:
        """List bundled worker agent names that exist on disk.

        Agents are NOT copied into the user's project. They are loaded directly
        from the skill's bundled `opencode/` directory by passing
        `OPENCODE_CONFIG_DIR` to the opencode subprocess (see
        `_run_opencode_streamed`). This keeps the user's project `.opencode/`
        and global config untouched, and works regardless of the worker's cwd
        (so worktree-nested runs load the same agents).
        """
        src_dir = get_skill_agents_dir()
        return [
            name for name in AGENT_MAP.values()
            if (src_dir / f"{name}.md").exists()
        ]

    def get_task_dir(self, task_id: str) -> Path:
        """Get the directory for a specific task."""
        return self.tasks_dir / task_id

    def update_index(self, task_config: TaskConfig, task_dir: Path | None = None,
                     worktree_path: Path | None = None) -> None:
        """Upsert the task's index entry, keyed by ``task_id``.

        A task transitions running -> completed/failed/partial by being written
        twice (once at start, once at finish). Upserting — instead of appending
        — keeps exactly one row per task so ``list`` never shows duplicates and
        the status reflects the latest write. ``created_at`` is preserved from
        the first write; ``updated_at`` always reflects this write.
        """
        index_path = self.sidecar_dir / INDEX_FILE
        index = read_json(index_path) or {"tasks": []}
        tasks = index.setdefault("tasks", [])
        now = datetime.now(timezone.utc).isoformat()

        patch_path = ""
        if task_dir and task_config.worktree:
            patch_path = str(task_dir / "patch.diff")

        entry = {
            "task_id": task_config.task_id,
            "mode": task_config.mode,
            "worker": task_config.worker,
            "agent": AGENT_MAP.get(task_config.mode),
            "model": task_config.model,
            "status": task_config.status,
            "created_at": task_config.created_at,
            "updated_at": now,
            "task_dir": str(task_dir) if task_dir else "",
            "worktree": task_config.worktree,
            "result_path": str(task_dir / "result.json") if task_dir else "",
            "patch_path": patch_path,
        }

        # Find the existing row for this task_id and replace it in place;
        # preserve the original created_at so a finish-write doesn't reset it.
        for i, existing in enumerate(tasks):
            if existing.get("task_id") == task_config.task_id:
                entry["created_at"] = existing.get("created_at", task_config.created_at)
                tasks[i] = entry
                write_json(index_path, index)
                return

        tasks.append(entry)
        write_json(index_path, index)

    def create_worktree(self, task_id: str) -> Path | None:
        """Create a git worktree for writable tasks."""
        worktree_path = self.worktrees_dir / task_id
        branch_name = f"sidecar/{task_id}"

        # Try to create worktree with unique branch name
        for suffix in ["", "-1", "-2", "-3"]:
            try_branch = branch_name + suffix
            result = subprocess.run(
                ["git", "worktree", "add", str(worktree_path), "-b", try_branch],
                cwd=self.project_dir,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                return worktree_path
            if "already exists" not in result.stderr:
                break

        print(f"ERROR: Failed to create worktree for task {task_id}", file=sys.stderr)
        print(f"  stderr: {result.stderr.strip()}", file=sys.stderr)
        return None

    def remove_worktree(self, task_id: str) -> bool:
        """Remove a git worktree."""
        worktree_path = self.worktrees_dir / task_id
        if not worktree_path.exists():
            return True

        result = subprocess.run(
            ["git", "worktree", "remove", str(worktree_path), "--force"],
            cwd=self.project_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0

    def run_task(self, task_config: TaskConfig) -> dict:
        """Execute a sidecar task and return results."""
        self.ensure_dirs()
        # Worker agents are loaded by OpenCode from the bundled config dir via
        # OPENCODE_CONFIG_DIR (set in _run_opencode_streamed) — nothing is
        # copied into the user's project. Verify the agent file exists so a
        # missing definition fails loudly instead of silently falling back to
        # the default agent.
        agent_name = AGENT_MAP.get(task_config.mode)
        if agent_name and agent_name not in self.available_agents():
            task_dir = self.get_task_dir(task_config.task_id)
            task_dir.mkdir(parents=True, exist_ok=True)
            task_config.status = "failed"
            self._write_error_result(
                task_dir, task_config,
                f"Worker agent '{agent_name}' not found in bundled config dir "
                f"{get_skill_agents_dir()}. Cannot run without it (would fall "
                f"back to the default agent).",
            )
            self.update_index(task_config, task_dir)
            return self._build_return(task_config, task_dir)
        task_dir = self.get_task_dir(task_config.task_id)
        task_dir.mkdir(parents=True, exist_ok=True)

        # Write task envelope
        task_config.status = "running"
        write_json(task_dir / "task.json", task_config.to_dict())
        task_md = load_template(
            task_config.mode,
            task_config.task_id,
            task_config.goal,
            task_config.scope,
            task_config.log_file,
            str(self.worktrees_dir / task_config.task_id) if task_config.worktree else "",
        )
        (task_dir / "task.md").write_text(task_md, encoding="utf-8")

        # Update index (first write: status=running; upserted on finish below)
        self.update_index(task_config, task_dir)

        # Create worktree if needed. Writable modes are forced to worktree in
        # TaskConfig; guard here so a writable task can never execute against
        # the main working tree even if invoked through some other path.
        worktree_path = None
        if task_config.mode in WRITABLE_MODES and not task_config.worktree:
            task_config.status = "failed"
            self._write_error_result(
                task_dir, task_config,
                f"Mode '{task_config.mode}' is writable and must run in an "
                f"isolated git worktree, but worktree is disabled. Refusing to "
                f"run against the main working tree.",
            )
            self.update_index(task_config, task_dir)
            return self._build_return(task_config, task_dir)
        if task_config.worktree:
            worktree_path = self.create_worktree(task_config.task_id)
            if not worktree_path:
                task_config.status = "failed"
                self._write_error_result(task_dir, task_config, "Failed to create git worktree.")
                self.update_index(task_config, task_dir)
                return self._build_return(task_config, task_dir)

        # Build opencode command
        cmd = self._build_command(task_config, worktree_path)
        print(f"Running: {' '.join(cmd)}", file=sys.stderr)

        # Execute opencode, streaming output straight to log files so that
        # partial output is preserved even if the worker is killed on timeout.
        # (Capturing in-memory and discarding on TimeoutExpired loses the
        # worker's answer when it produces output but lingers before exiting.)
        stdout, stderr, returncode = self._run_opencode_streamed(
            cmd, worktree_path, task_dir, task_config.timeout
        )

        # Security checks
        security_warnings = []
        security_warnings.extend(check_forbidden_commands(stdout))
        security_warnings.extend(check_forbidden_commands(stderr))

        # Detect agent fallback: opencode prints a warning and runs the default
        # agent (losing all engine-enforced permissions) when --agent names a
        # missing agent or a subagent. Treat it as a hard failure — the worker
        # ran unconstrained, so its output cannot be trusted as sidecar-safe.
        fallback = detect_agent_fallback(stdout) or detect_agent_fallback(stderr)
        if fallback:
            security_warnings.append(
                f"CRITICAL: worker fell back to the default agent "
                f"({fallback}). Engine permissions were NOT enforced."
            )

        # Export patch for writable tasks
        if task_config.worktree and worktree_path and worktree_path.exists():
            self._export_patch(task_config.task_id, worktree_path, task_dir)
            self._export_files_changed(task_config.task_id, worktree_path, task_dir)

            # Check sensitive files in patch
            patch_path = task_dir / "patch.diff"
            if patch_path.exists():
                patch_content = patch_path.read_text(encoding="utf-8")
                security_warnings.extend(check_sensitive_files(patch_content))

        # Determine status
        if returncode != 0:
            task_config.status = "failed"
        elif fallback:
            # Agent fell back to default → ran without engine permissions.
            task_config.status = "failed"
        elif not stdout.strip():
            task_config.status = "partial"
        else:
            task_config.status = "completed"

        # Generate result files
        self._generate_result(task_config, task_dir, stdout, stderr, security_warnings, returncode)

        # Write metadata
        metadata = {
            "task_id": task_config.task_id,
            "mode": task_config.mode,
            "worker": task_config.worker,
            "model": task_config.model,
            "agent": AGENT_MAP.get(task_config.mode),
            "status": task_config.status,
            "returncode": returncode,
            "timeout": task_config.timeout,
            "worktree": task_config.worktree,
            "worktree_path": str(worktree_path) if worktree_path else None,
            "project_dir": task_config.project_dir,
            "started_at": task_config.created_at,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "security_warnings": security_warnings,
        }
        write_json(task_dir / "metadata.json", metadata)

        # Update index with final status (upserts the running row written above)
        self.update_index(task_config, task_dir, worktree_path)

        return self._build_return(task_config, task_dir)

    def _run_opencode_streamed(
        self,
        cmd: list[str],
        worktree_path: Path | None,
        task_dir: Path,
        timeout: int,
    ) -> tuple[str, str, int]:
        """Run opencode, streaming stdout/stderr to log files in real time.

        Returns (stdout, stderr, returncode). On timeout the worker is killed
        but whatever it had already written to the logs is preserved and
        returned, so a worker that produced an answer but lingered is not lost.
        """
        stdout_path = task_dir / "stdout.log"
        stderr_path = task_dir / "stderr.log"
        cwd = str(worktree_path or self.project_dir)

        # Launch in a new process group / job so we can kill the whole tree on
        # timeout. opencode is spawned via a CMD/shell wrapper on Windows that
        # itself spawns node; a plain proc.kill() would only reap the wrapper
        # and leave an orphaned worker running (burning tokens, holding file
        # handles open).
        popen_kwargs = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        # Load the bundled worker agents via OPENCODE_CONFIG_DIR instead of
        # copying them into the user's project. OpenCode reads agents/commands/
        # modes/plugins from this dir like a .opencode folder, merged after the
        # user's global + project config (so provider/auth stays intact).
        # Verified on opencode 1.17.4: the agent resolves as primary with no
        # fallback, and `opencode auth list` still shows all credentials.
        env = os.environ.copy()
        env["OPENCODE_CONFIG_DIR"] = str(get_opencode_config_dir())

        try:
            with open(stdout_path, "w", encoding="utf-8") as out_f, \
                 open(stderr_path, "w", encoding="utf-8") as err_f:
                proc = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    stdout=out_f,
                    stderr=err_f,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    **popen_kwargs,
                )
                try:
                    returncode = proc.wait(timeout=timeout)
                    timed_out = False
                except subprocess.TimeoutExpired:
                    _kill_process_tree(proc)
                    returncode = -1
                    timed_out = True
        except FileNotFoundError:
            stderr_path.write_text(
                "opencode command not found. Please install OpenCode first.",
                encoding="utf-8",
            )
            return "", "opencode command not found.", -1
        except Exception as e:  # noqa: BLE001 - surface any launch failure
            msg = f"Unexpected error running opencode: {e}"
            stderr_path.write_text(msg, encoding="utf-8")
            return "", msg, -1

        # Read back whatever was written (preserved even after a kill).
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")

        if timed_out:
            note = (
                f"\n[sidecar] Task timed out after {timeout} seconds; "
                f"worker killed. Partial output above (if any) was preserved."
            )
            stderr = stderr + note
            stderr_path.write_text(stderr, encoding="utf-8")

        return stdout, stderr, returncode

    def _build_command(self, task_config: TaskConfig, worktree_path: Path | None) -> list[str]:
        """Build the opencode run command."""
        opencode_path = get_opencode_path()
        if not opencode_path:
            raise RuntimeError("opencode command not found. Please install OpenCode first.")
        cmd = [opencode_path, "run"]

        # Directory
        target_dir = str(worktree_path or self.project_dir)
        cmd.extend(["--dir", target_dir])

        # Model
        cmd.extend(["--model", task_config.model])

        # Agent — selects the engine-permissioned worker agent for this mode.
        # It is a mode: primary agent (loaded via OPENCODE_CONFIG_DIR), so
        # `opencode run --agent` uses it directly instead of falling back to
        # the default agent. The agent enforces read-only vs. writable access
        # at the OpenCode layer; the prompt constraints below are a secondary
        # guard.
        agent_name = AGENT_MAP.get(task_config.mode)
        if agent_name:
            cmd.extend(["--agent", agent_name])

        # Format
        cmd.extend(["--format", "json"])

        # Title
        cmd.extend(["--title", f"sidecar-{task_config.task_id}"])

        # Message - the task prompt (role enforced by --agent; prompt reinforces)
        prompt = self._build_prompt(task_config)
        cmd.append(prompt)

        return cmd

    def _build_prompt(self, task_config: TaskConfig) -> str:
        """Build the task prompt for the worker."""
        # Direct task instructions — no role preamble, just tell it what to do
        parts = [
            f"TASK: {task_config.goal}",
        ]

        if task_config.scope:
            parts.append(f"SCOPE: {task_config.scope}")

        if task_config.log_file:
            parts.append(f"LOG FILE: {task_config.log_file}")

        if task_config.worktree:
            parts.append("NOTE: You are in an isolated git worktree. Only edit files here.")

        # Mode-specific constraints
        if task_config.mode in ("explore", "review", "log"):
            parts.append("CONSTRAINT: Do NOT modify any files. Read-only analysis only.")

        if task_config.mode == "review":
            parts.append("CONSTRAINT: Do NOT commit or push.")

        if task_config.mode in ("implement", "test-fix"):
            parts.append("CONSTRAINT: Do NOT commit, push, or install dependencies.")

        parts.append("")
        parts.append("Execute this task now using the available tools.")
        parts.append("Produce a structured final report with your findings.")

        return "\n".join(parts)

    def _generate_result(self, task_config: TaskConfig, task_dir: Path,
                         stdout: str, stderr: str, security_warnings: list[str],
                         returncode: int) -> None:
        """Generate result artifacts from the worker's --format json output.

        The opencode stdout is a JSONL event stream, not the worker's answer.
        We parse it once and write:
          - events.jsonl   : the raw parsed events (one JSON object per line)
          - worker_text.md : the worker's human-readable text (text events)
        The structured result.json is then extracted from worker_text (which
        is where a worker emits its ```json block), falling back to a scan of
        the whole stream only if needed.
        """
        parsed = parse_event_stream(stdout)
        worker_text = parsed["text"]

        # Persist the raw event stream for debugging/audit.
        if parsed["events"]:
            events_lines = [json.dumps(e, ensure_ascii=False) for e in parsed["events"]]
            (task_dir / "events.jsonl").write_text(
                "\n".join(events_lines) + "\n", encoding="utf-8"
            )

        # Persist the extracted worker text (the actual answer).
        (task_dir / "worker_text.md").write_text(
            worker_text if worker_text.strip() else "(no text events emitted)\n",
            encoding="utf-8",
        )

        # Extract structured result from the worker text first (that's where
        # the worker writes its JSON block); fall back to the raw stream.
        result_json = self._extract_result_json(worker_text, task_config)
        if result_json.get("_auto_generated") and stdout.strip() and stdout != worker_text:
            alt = self._extract_result_json(stdout, task_config)
            if not alt.get("_auto_generated"):
                result_json = alt
        result_json.pop("_auto_generated", None)

        # Surface executed commands the parser already found.
        if parsed["commands"] and not result_json.get("commands_run"):
            result_json["commands_run"] = parsed["commands"]

        # Generate result.md
        result_md_parts = [
            f"# Sidecar Result",
            f"",
            f"## Task ID",
            f"",
            f"{task_config.task_id}",
            f"",
            f"## Worker",
            f"",
            f"{task_config.worker} ({task_config.model})",
            f"",
            f"## Status",
            f"",
            f"{task_config.status}",
            f"",
        ]

        if security_warnings:
            result_md_parts.extend([
                "## ⚠️ Security Warnings",
                "",
            ])
            for warning in security_warnings:
                result_md_parts.append(f"- {warning}")
            result_md_parts.append("")

        if task_config.worktree:
            result_md_parts.extend([
                "## Patch",
                "",
                "WARNING: This patch was generated by a sidecar worker.",
                "The main agent must review `patch.diff` before applying.",
                "",
            ])

        # Show the extracted worker text (the answer), not the raw event stream.
        if worker_text.strip():
            result_md_parts.extend([
                "## Worker Output",
                "",
                worker_text[:10000],  # Truncate very long output
                "",
            ])
        else:
            result_md_parts.extend([
                "## Worker Output",
                "",
                "(no text events emitted — see events.jsonl and stdout.log)",
                "",
            ])

        if stderr.strip():
            result_md_parts.extend([
                "## Errors",
                "",
                "```",
                stderr[:5000],
                "```",
                "",
            ])

        (task_dir / "result.md").write_text("\n".join(result_md_parts), encoding="utf-8")

        # Generate result.json
        write_json(task_dir / "result.json", result_json)

    def _extract_result_json(self, text: str, task_config: TaskConfig) -> dict:
        """Extract a structured JSON result from the worker's text.

        `text` should be the worker's human-readable answer (the concatenated
        text events), where a worker emits its ```json result block. Returns
        the parsed result with envelope fields filled in. If no usable JSON is
        found, returns an auto-generated fallback marked with
        `_auto_generated: True` so the caller can retry against another source.
        """
        def _finish(data: dict) -> dict:
            data.setdefault("task_id", task_config.task_id)
            data.setdefault("worker", task_config.worker)
            data.setdefault("model", task_config.model)
            data.setdefault("status", task_config.status)
            return data

        # Collect fenced code block candidates (```json ... ``` or ``` ... ```).
        json_candidates = []
        in_json = False
        json_lines: list[str] = []
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("```"):
                if in_json:
                    json_candidates.append("\n".join(json_lines))
                    json_lines = []
                    in_json = False
                else:
                    in_json = True
                continue
            if in_json:
                json_lines.append(line)
        if json_lines:
            json_candidates.append("\n".join(json_lines))

        # Also consider the whole text as a candidate (worker may emit bare JSON).
        json_candidates.append(text)

        # Prefer the candidate that parses to a dict carrying the most sidecar
        # result fields (so a "summary"-only object loses to a full result).
        sidecar_keys = (
            "summary", "findings", "status", "confidence", "files_changed",
            "risks", "uncertainties", "requires_main_agent_decision",
        )
        best: dict | None = None
        best_score = -1
        for candidate in json_candidates:
            candidate = candidate.strip()
            if not candidate:
                continue
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if not ("task_id" in data or "summary" in data or "findings" in data):
                continue
            score = sum(1 for k in sidecar_keys if k in data)
            if score > best_score:
                best, best_score = data, score
        if best is not None:
            return _finish(best)

        # Fallback: synthesize a minimal result from the raw text.
        return {
            "task_id": task_config.task_id,
            "worker": task_config.worker,
            "model": task_config.model,
            "status": task_config.status,
            "confidence": "low",
            "summary": text[:500] if text.strip() else "No worker text captured.",
            "findings": [],
            "files_changed": [],
            "commands_run": [],
            "tests_run": [],
            "risks": ["Worker did not produce structured JSON output."],
            "uncertainties": ["Result was auto-generated from raw worker text."],
            "requires_main_agent_decision": True,
            "_auto_generated": True,
        }

    def _export_patch(self, task_id: str, worktree_path: Path, task_dir: Path) -> None:
        """Export git diff as patch from worktree."""
        result = subprocess.run(
            ["git", "diff"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            (task_dir / "patch.diff").write_text(result.stdout, encoding="utf-8")
            print(f"Patch exported: {task_dir / 'patch.diff'}", file=sys.stderr)
        else:
            print("No changes to export (empty diff).", file=sys.stderr)

    def _export_files_changed(self, task_id: str, worktree_path: Path, task_dir: Path) -> None:
        """Export list of changed files from worktree."""
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            (task_dir / "files-changed.txt").write_text(result.stdout, encoding="utf-8")

    def _write_error_result(self, task_dir: Path, task_config: TaskConfig, error_msg: str) -> None:
        """Write error result files."""
        result = {
            "task_id": task_config.task_id,
            "worker": task_config.worker,
            "model": task_config.model,
            "status": "failed",
            "confidence": "low",
            "summary": error_msg,
            "findings": [],
            "risks": [error_msg],
            "requires_main_agent_decision": True,
        }
        write_json(task_dir / "result.json", result)
        (task_dir / "result.md").write_text(f"# Sidecar Result\n\nStatus: failed\n\n{error_msg}\n", encoding="utf-8")
        write_json(task_dir / "metadata.json", {
            "task_id": task_config.task_id,
            "status": "failed",
            "error": error_msg,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })

    def _build_return(self, task_config: TaskConfig, task_dir: Path) -> dict:
        """Build the return dictionary for the orchestrator."""
        return {
            "task_id": task_config.task_id,
            "status": task_config.status,
            "task_dir": str(task_dir),
            "result_md": str(task_dir / "result.md"),
            "result_json": str(task_dir / "result.json"),
            "metadata_json": str(task_dir / "metadata.json"),
            "stdout_log": str(task_dir / "stdout.log"),
            "stderr_log": str(task_dir / "stderr.log"),
            "events_jsonl": str(task_dir / "events.jsonl"),
            "worker_text": str(task_dir / "worker_text.md"),
            "patch_diff": str(task_dir / "patch.diff") if task_config.worktree else None,
        }


# ── CLI Commands ───────────────────────────────────────────────────────────

def cmd_explore(args, orchestrator: SidecarOrchestrator) -> None:
    """Run an exploration task."""
    model = resolve_model("explore", orchestrator.project_dir, args.model)
    config = TaskConfig(
        mode="explore",
        goal=args.goal,
        model=model,
        project_dir=orchestrator.project_dir,
        timeout=args.timeout,
    )
    result = orchestrator.run_task(config)
    print_result(result)


def cmd_review(args, orchestrator: SidecarOrchestrator) -> None:
    """Run a review task."""
    model = resolve_model("review", orchestrator.project_dir, args.model)
    config = TaskConfig(
        mode="review",
        goal=args.goal or "Review the current git diff for correctness and missing tests.",
        model=model,
        project_dir=orchestrator.project_dir,
        scope=args.scope or "Current git diff only.",
        timeout=args.timeout,
    )
    result = orchestrator.run_task(config)
    print_result(result)


def cmd_log(args, orchestrator: SidecarOrchestrator) -> None:
    """Run a log analysis task."""
    model = resolve_model("log", orchestrator.project_dir, args.model)
    config = TaskConfig(
        mode="log",
        goal=args.goal,
        model=model,
        project_dir=orchestrator.project_dir,
        log_file=args.log_file,
        timeout=args.timeout,
    )
    result = orchestrator.run_task(config)
    print_result(result)


def cmd_implement(args, orchestrator: SidecarOrchestrator) -> None:
    """Run an implementation task in an isolated worktree."""
    model = resolve_model("implement", orchestrator.project_dir, args.model)
    config = TaskConfig(
        mode="implement",
        goal=args.goal,
        model=model,
        project_dir=orchestrator.project_dir,
        worktree=args.worktree,
        timeout=args.timeout,
    )
    result = orchestrator.run_task(config)
    print_result(result)


def cmd_test_fix(args, orchestrator: SidecarOrchestrator) -> None:
    """Run a test fix task in an isolated worktree."""
    model = resolve_model("test-fix", orchestrator.project_dir, args.model)
    config = TaskConfig(
        mode="test-fix",
        goal=args.goal,
        model=model,
        project_dir=orchestrator.project_dir,
        worktree=args.worktree,
        timeout=args.timeout,
    )
    result = orchestrator.run_task(config)
    print_result(result)


def cmd_collect(args, orchestrator: SidecarOrchestrator) -> None:
    """Collect and display results for a task."""
    task_dir = orchestrator.get_task_dir(args.task_id)
    if not task_dir.exists():
        print(f"ERROR: Task {args.task_id} not found.", file=sys.stderr)
        sys.exit(1)

    result_md = task_dir / "result.md"
    result_json = task_dir / "result.json"
    metadata = task_dir / "metadata.json"

    print(f"Task ID: {args.task_id}")
    print(f"Task dir: {task_dir}")
    print()

    if metadata.exists():
        data = read_json(metadata)
        print(f"Status: {data.get('status', 'unknown')}")
        print(f"Model: {data.get('model', 'unknown')}")
        print()

    if result_md.exists():
        print(result_md.read_text(encoding="utf-8"))
    else:
        print("No result.md found.")

    if result_json.exists():
        print("\n--- result.json ---")
        print(json.dumps(read_json(result_json), indent=2, ensure_ascii=False))


def cmd_list(args, orchestrator: SidecarOrchestrator) -> None:
    """List all sidecar tasks."""
    index_path = orchestrator.sidecar_dir / INDEX_FILE
    if not index_path.exists():
        print("No tasks found.")
        return

    index = read_json(index_path)
    if not index or not index.get("tasks"):
        print("No tasks found.")
        return

    print(f"{'Task ID':<20} {'Mode':<12} {'Worker':<25} {'Status':<12} {'Model'}")
    print("-" * 90)
    for task in index["tasks"]:
        print(f"{task['task_id']:<20} {task['mode']:<12} {task['worker']:<25} {task['status']:<12} {task['model']}")


def cmd_cleanup(args, orchestrator: SidecarOrchestrator) -> None:
    """Clean up a task and its worktree."""
    task_dir = orchestrator.get_task_dir(args.task_id)
    if not task_dir.exists():
        print(f"ERROR: Task {args.task_id} not found.", file=sys.stderr)
        sys.exit(1)

    # Check if task has a worktree
    metadata = read_json(task_dir / "metadata.json")
    if metadata and metadata.get("worktree"):
        print(f"Removing worktree for task {args.task_id}...")
        orchestrator.remove_worktree(args.task_id)

    # Remove task directory
    shutil.rmtree(task_dir)
    print(f"Task {args.task_id} cleaned up.")

    # Update index
    index_path = orchestrator.sidecar_dir / INDEX_FILE
    if index_path.exists():
        index = read_json(index_path)
        if index and "tasks" in index:
            index["tasks"] = [t for t in index["tasks"] if t["task_id"] != args.task_id]
            write_json(index_path, index)


def cmd_check_conflicts(args, orchestrator: SidecarOrchestrator) -> None:
    """Detect file overlaps between worktree task patches.

    When several writable workers run in parallel, each produces an isolated
    patch.diff. Before the main agent applies any of them it must know whether
    two patches touch the same file (a lost-update hazard). This command maps
    each task's patch to the files it changes and reports every overlap.
    """
    tasks_dir = orchestrator.tasks_dir
    if not tasks_dir.exists():
        print("No tasks found.")
        return

    # task_id -> set(files), only for tasks that produced a patch
    task_files: dict[str, set[str]] = {}
    for task_dir in sorted(tasks_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        patch_path = task_dir / "patch.diff"
        if patch_path.exists():
            files = parse_patch_files(patch_path.read_text(encoding="utf-8", errors="replace"))
            if files:
                task_files[task_dir.name] = files

    if not task_files:
        print("No patches found. Nothing to check.")
        return

    # file -> [task_ids that touch it]
    file_owners: dict[str, list[str]] = {}
    for task_id, files in task_files.items():
        for f in files:
            file_owners.setdefault(f, []).append(task_id)

    conflicts = {f: owners for f, owners in file_owners.items() if len(owners) > 1}

    print(f"Checked {len(task_files)} patch(es) across {len(file_owners)} file(s).")
    print()
    if not conflicts:
        print("No conflicts: every patched file is touched by exactly one task.")
        print("Patches can be reviewed and applied independently.")
        return

    print(f"WARNING: {len(conflicts)} file(s) are modified by more than one task.")
    print("The main agent must reconcile these before applying any patch:")
    print()
    for f in sorted(conflicts):
        print(f"  {f}")
        for task_id in sorted(conflicts[f]):
            print(f"      <- {task_id}")


def cmd_init(args, orchestrator: SidecarOrchestrator) -> None:
    """Onboarding: probe available models and print guidance.

    This is the deterministic half of the onboarding flow. It surfaces what
    opencode has authed and what models exist, plus an auto-detected guess for
    fast/quality tiers. The main agent (guided by SKILL.md) uses this output
    to recommend models to the user, then calls `config set` to persist.
    """
    print(f"Project config path: {get_config_path(orchestrator.project_dir)}")
    existing = read_config(orchestrator.project_dir)
    if existing:
        print(f"Current config: fast={existing.get('fast_model')} "
              f"quality={existing.get('quality_model')} "
              f"(configured {existing.get('configured_at', '?')})")
        print()
    else:
        print("No config yet. Run `config set` after picking models below.\n")

    model_ids, authed_names = detect_available_models()
    print(f"Authed providers ({len(authed_names)}):")
    for n in authed_names:
        print(f"  - {n}")
    print()

    print(f"Available models ({len(model_ids)}):")
    for mid in sorted(model_ids):
        f, q = _score_model(mid)
        tag = ""
        if q >= 2 and q >= f:
            tag = "  [quality-ish]"
        elif f >= 2 and f > q:
            tag = "  [fast-ish]"
        print(f"  {mid}{tag}")

    fast, quality = auto_pick_models()
    print()
    print("Auto-detected guess:")
    print(f"  fast    = {fast or '(none matched)'}")
    print(f"  quality = {quality or '(none matched)'}")
    print()
    print("To configure, run:")
    print("  python scripts/sidecar.py config set "
          f"--fast \"{fast or '<model>'}\" --quality \"{quality or '<model>'}\"")
    print("\nThen review: python scripts/sidecar.py config show")


def cmd_config_show(args, orchestrator: SidecarOrchestrator) -> None:
    """Show the current model config."""
    path = get_config_path(orchestrator.project_dir)
    config = read_config(orchestrator.project_dir)
    if not config:
        print(f"No config at {path}.")
        print("Run `init` to detect models, then `config set` to configure.")
        return
    print(f"Config: {path}")
    print(json.dumps(config, indent=2, ensure_ascii=False))
    print()
    print("Mode routing:")
    for mode in ("explore", "log"):
        print(f"  {mode:10} -> fast    -> {config.get('fast_model')}")
    for mode in ("review", "implement", "test-fix"):
        print(f"  {mode:10} -> quality -> {config.get('quality_model')}")


def cmd_config_set(args, orchestrator: SidecarOrchestrator) -> None:
    """Persist the fast/quality model choice to the project config."""
    if not args.fast or not args.quality:
        print("ERROR: both --fast and --quality are required.", file=sys.stderr)
        sys.exit(1)
    path = write_config(orchestrator.project_dir, args.fast, args.quality)
    print(f"Config written: {path}")
    print(f"  fast    = {args.fast}")
    print(f"  quality = {args.quality}")
    print()
    print("Mode routing:")
    print(f"  explore, log          -> fast    -> {args.fast}")
    print(f"  review, implement, test-fix -> quality -> {args.quality}")


def cmd_doctor(args, orchestrator: SidecarOrchestrator) -> None:
    """Diagnose the sidecar setup without running a worker.

    Checks: opencode is installed; the bundled config dir + agent files exist;
    each mode's worker agent loads via OPENCODE_CONFIG_DIR and is `primary`
    (so `opencode run --agent` won't fall back to the default agent); and that
    at least one model/credential is available. Prints a checklist and exits
    non-zero if any hard check fails.
    """
    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        mark = "PASS" if passed else "FAIL"
        if not passed:
            ok = False
        print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))

    print("OpenCode Sidecar Doctor")
    print()

    # 1. opencode present
    opencode_path = get_opencode_path()
    check("opencode CLI on PATH", bool(opencode_path), opencode_path or "not found")

    # 2. bundled config dir + agent files
    cfg_dir = get_opencode_config_dir()
    agents_dir = get_skill_agents_dir()
    check("bundled config dir exists", cfg_dir.is_dir(), str(cfg_dir))
    available = orchestrator.available_agents()
    expected = sorted(set(AGENT_MAP.values()))
    missing = [a for a in expected if a not in available]
    check("all worker agent files present", not missing,
          "missing: " + ", ".join(missing) if missing else f"{len(expected)} agents")

    # 3. agents load and are primary (only if opencode is available)
    if opencode_path:
        loaded = list_loaded_agents(cfg_dir)
        for mode, agent_name in AGENT_MAP.items():
            mode_label = loaded.get(agent_name)
            if mode_label is None:
                check(f"agent '{agent_name}' ({mode}) loads", False, "not listed by `opencode agent list`")
            elif mode_label == "subagent":
                check(f"agent '{agent_name}' is primary", False,
                      "mode is 'subagent' → run --agent would fall back to default")
            else:
                check(f"agent '{agent_name}' is primary", True, f"mode: {mode_label}")

        # 4. models / credentials
        model_ids, authed = detect_available_models()
        check("models available (`opencode models`)", bool(model_ids), f"{len(model_ids)} models")
        check("credentials present (`opencode auth list`)", bool(authed),
              ", ".join(authed) if authed else "none — run `opencode auth login`")
    else:
        print("  [skip] agent/model checks (opencode not installed)")

    # 5. project config
    cfg = read_config(orchestrator.project_dir)
    if cfg:
        print(f"  [info] model config: fast={cfg.get('fast_model')} quality={cfg.get('quality_model')}")
    else:
        print("  [info] no model config yet — run `init` then `config set` (auto-detect also works)")

    print()
    print("Doctor result:", "OK" if ok else "PROBLEMS FOUND")
    if not ok:
        sys.exit(1)


def cmd_verify_agent(args, orchestrator: SidecarOrchestrator) -> None:
    """Run a minimal real prompt against a worker agent to confirm no fallback.

    Unlike `doctor` (static checks), this actually invokes
    `opencode run --agent <name> --format json` with a trivial prompt and the
    bundled OPENCODE_CONFIG_DIR, then checks that OpenCode did NOT print the
    "falling back to default agent" warning. Use it to confirm end-to-end that
    a worker runs under its own (engine-enforced) agent.
    """
    agent_name = args.agent or AGENT_MAP["review"]
    model = args.model or resolve_model("review", orchestrator.project_dir, None)
    opencode_path = get_opencode_path()
    if not opencode_path:
        print("ERROR: opencode not found.", file=sys.stderr)
        sys.exit(1)

    cmd = [
        opencode_path, "run",
        "--agent", agent_name,
        "--model", model,
        "--format", "json",
        "Reply with exactly: VERIFY_OK. Do not modify any files.",
    ]
    env = os.environ.copy()
    env["OPENCODE_CONFIG_DIR"] = str(get_opencode_config_dir())

    print(f"Verifying agent '{agent_name}' with model '{model}'...")
    try:
        r = subprocess.run(
            cmd, cwd=str(orchestrator.project_dir),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=args.timeout or 120, env=env,
        )
    except subprocess.TimeoutExpired:
        print("FAIL: verification timed out.", file=sys.stderr)
        sys.exit(1)

    combined = (r.stdout or "") + "\n" + (r.stderr or "")
    fallback = detect_agent_fallback(combined)
    parsed = parse_event_stream(r.stdout or "")

    if fallback:
        print(f"  [FAIL] agent fell back to default: {fallback}")
        print("  The agent is NOT being used. Check that it exists and is mode: primary.")
        sys.exit(1)

    print(f"  [PASS] no fallback warning — '{agent_name}' ran as the selected agent.")
    if parsed["is_json"]:
        print(f"  [PASS] received {len(parsed['events'])} JSON events; "
              f"worker text: {parsed['text'][:80]!r}")
    else:
        print("  [warn] output was not a JSON event stream (check --format support).")
    if r.returncode != 0:
        print(f"  [warn] opencode exited {r.returncode}.")




# ── Output ─────────────────────────────────────────────────────────────────

def print_result(result: dict) -> None:
    """Print the final result summary."""
    print()
    print("Sidecar task completed.")
    print()
    print(f"Task ID: {result['task_id']}")
    print(f"Status: {result['status']}")
    print(f"Result: {result['result_md']}")
    print(f"JSON: {result['result_json']}")
    if result.get("patch_diff"):
        print(f"Patch: {result['patch_diff']}")
    print()

    if result["status"] == "failed":
        print("[WARNING] Task failed. Check stderr.log for details.")
        stderr_path = Path(result["stderr_log"])
        if stderr_path.exists():
            stderr = stderr_path.read_text(encoding="utf-8")
            if stderr.strip():
                print(f"Errors:\n{stderr[:1000]}")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="OpenCode Sidecar Orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # explore
    p_explore = subparsers.add_parser("explore", help="Run codebase exploration")
    p_explore.add_argument("--goal", required=True, help="Exploration goal")
    p_explore.add_argument("--model", help="Model to use")
    p_explore.add_argument("--dir", help="Project directory")
    p_explore.add_argument("--timeout", type=int, help="Timeout in seconds")

    # review
    p_review = subparsers.add_parser("review", help="Run code review")
    p_review.add_argument("--goal", help="Review goal")
    p_review.add_argument("--scope", help="Review scope")
    p_review.add_argument("--model", help="Model to use")
    p_review.add_argument("--dir", help="Project directory")
    p_review.add_argument("--timeout", type=int, help="Timeout in seconds")

    # log
    p_log = subparsers.add_parser("log", help="Run log analysis")
    p_log.add_argument("--goal", required=True, help="Analysis goal")
    p_log.add_argument("--log-file", required=True, help="Path to log file")
    p_log.add_argument("--model", help="Model to use")
    p_log.add_argument("--dir", help="Project directory")
    p_log.add_argument("--timeout", type=int, help="Timeout in seconds")

    # implement
    p_implement = subparsers.add_parser("implement", help="Run implementation (always in an isolated worktree)")
    p_implement.add_argument("--goal", required=True, help="Implementation goal")
    p_implement.add_argument("--worktree", action="store_true", default=True,
                             help="Deprecated no-op; writable modes always use an isolated worktree.")
    p_implement.add_argument("--model", help="Model to use")
    p_implement.add_argument("--dir", help="Project directory")
    p_implement.add_argument("--timeout", type=int, help="Timeout in seconds")

    # test-fix
    p_test_fix = subparsers.add_parser("test-fix", help="Run test fix (always in an isolated worktree)")
    p_test_fix.add_argument("--goal", required=True, help="Test fix goal")
    p_test_fix.add_argument("--worktree", action="store_true", default=True,
                            help="Deprecated no-op; writable modes always use an isolated worktree.")
    p_test_fix.add_argument("--model", help="Model to use")
    p_test_fix.add_argument("--dir", help="Project directory")
    p_test_fix.add_argument("--timeout", type=int, help="Timeout in seconds")

    # collect
    p_collect = subparsers.add_parser("collect", help="Collect task results")
    p_collect.add_argument("--task-id", required=True, help="Task ID")
    p_collect.add_argument("--dir", help="Project directory")

    # list
    p_list = subparsers.add_parser("list", help="List all tasks")
    p_list.add_argument("--dir", help="Project directory")

    # cleanup
    p_cleanup = subparsers.add_parser("cleanup", help="Clean up a task")
    p_cleanup.add_argument("--task-id", required=True, help="Task ID")
    p_cleanup.add_argument("--dir", help="Project directory")

    # check-conflicts
    p_conflicts = subparsers.add_parser(
        "check-conflicts",
        help="Detect file overlaps between worktree task patches",
    )
    p_conflicts.add_argument("--dir", help="Project directory")

    # init — onboarding: probe models and print guidance
    p_init = subparsers.add_parser(
        "init",
        help="Probe available opencode models and print onboarding guidance",
    )
    p_init.add_argument("--dir", help="Project directory")

    # config — show / set the project model config
    p_config = subparsers.add_parser("config", help="Manage model config")
    config_sub = p_config.add_subparsers(dest="config_command")
    p_config_show = config_sub.add_parser("show", help="Show current model config")
    p_config_show.add_argument("--dir", help="Project directory")
    p_config_set = config_sub.add_parser("set", help="Set fast/quality models")
    p_config_set.add_argument("--fast", required=True, help="Model id for fast tier")
    p_config_set.add_argument("--quality", required=True, help="Model id for quality tier")
    p_config_set.add_argument("--dir", help="Project directory")

    # doctor — static health check (no worker run)
    p_doctor = subparsers.add_parser(
        "doctor",
        help="Diagnose setup: opencode, agents loaded as primary, permissions, models",
    )
    p_doctor.add_argument("--dir", help="Project directory")

    # verify-agent — run a minimal prompt to confirm no fallback to default agent
    p_verify = subparsers.add_parser(
        "verify-agent",
        help="Run a minimal prompt to confirm a worker agent runs without fallback",
    )
    p_verify.add_argument("--agent", help="Agent name to verify (default: sidecar-reviewer)")
    p_verify.add_argument("--model", help="Model to use")
    p_verify.add_argument("--dir", help="Project directory")
    p_verify.add_argument("--timeout", type=int, help="Timeout in seconds")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Determine project directory
    project_dir = Path(getattr(args, "dir", None) or os.getcwd())

    # Pre-flight checks
    if not check_git_repo(project_dir):
        print(f"ERROR: {project_dir} is not inside a git repository.", file=sys.stderr)
        print("Please run this command from within a git repository.", file=sys.stderr)
        sys.exit(1)

    # Commands that don't invoke opencode directly: don't require it installed.
    # `doctor` intentionally runs anyway — diagnosing a missing opencode is its
    # job, so it reports the problem instead of aborting in the pre-flight.
    no_opencode_commands = ("collect", "list", "cleanup", "check-conflicts", "config", "doctor")
    if args.command not in no_opencode_commands:
        if not check_opencode_available():
            print("ERROR: opencode command not found.", file=sys.stderr)
            print("Please install OpenCode: https://github.com/opencode-ai/opencode", file=sys.stderr)
            sys.exit(1)

    # Create orchestrator and run command
    orchestrator = SidecarOrchestrator(project_dir)

    commands = {
        "explore": cmd_explore,
        "review": cmd_review,
        "log": cmd_log,
        "implement": cmd_implement,
        "test-fix": cmd_test_fix,
        "collect": cmd_collect,
        "list": cmd_list,
        "cleanup": cmd_cleanup,
        "check-conflicts": cmd_check_conflicts,
        "init": cmd_init,
        "doctor": cmd_doctor,
        "verify-agent": cmd_verify_agent,
    }

    if args.command == "config":
        if not args.config_command:
            p_config.print_help()
            sys.exit(1)
        if args.config_command == "show":
            cmd_config_show(args, orchestrator)
        elif args.config_command == "set":
            cmd_config_set(args, orchestrator)
    else:
        commands[args.command](args, orchestrator)


if __name__ == "__main__":
    main()
