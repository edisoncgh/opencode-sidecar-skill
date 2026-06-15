---
name: opencode-sidecar
description: Delegate bounded coding sub-tasks from Claude Code to OpenCode worker agents using cheaper models. Use for codebase exploration, log analysis, first-pass code review, test failure diagnosis, and isolated worktree implementation attempts. The main Claude agent retains final decision authority.
---

# OpenCode Sidecar Skill

Use this skill when the user wants Claude Code to offload bounded, token-heavy coding tasks to OpenCode worker agents.

## When to Use

Use for:
- Codebase exploration (finding files, call chains, module structure).
- Current git diff review.
- Test/build/runtime log analysis.
- Finding relevant files or duplicate implementations.
- Independent second opinion on code changes.
- Small implementation attempts in isolated git worktree.

Do not use for:
- Final architecture decisions.
- Auth/security/secrets-sensitive changes.
- Git commit, push, deploy, release.
- Direct modification of the main working tree.
- Dependency installation unless the user explicitly approves.
- Large automatic refactors.

## Mandatory Rules

1. The main Claude agent owns planning, synthesis, and final decisions.
2. Workers receive explicit task envelopes (task.json + task.md).
3. Workers return structured result packages (result.json + result.md).
4. Writable workers must run inside isolated git worktrees.
5. Never auto-merge worker patches — always review first.
6. Never expose secrets or private env values to workers.
7. Prefer read-only delegation by default.

## Commands

Run the orchestrator from inside this skill directory (paths are relative to the skill root):

### Read-Only Tasks

```bash
# Explore codebase
python scripts/sidecar.py explore \
  --goal "Find where item location and storage status are handled."

# Review current diff
python scripts/sidecar.py review \
  --scope "Current git diff"

# Analyze log file
python scripts/sidecar.py log \
  --log-file "path/to/test-failure.log" \
  --goal "Identify the most likely root cause of this test failure."
```

### Writable Tasks (Isolated Worktree)

Writable modes (`implement`, `test-fix`) always run in an isolated git
worktree — this is enforced by `sidecar.py`, so they can never modify the main
working tree. The worker only produces a `patch.diff`; it is never applied,
committed, or pushed automatically.

```bash
# Implement in worktree
python scripts/sidecar.py implement \
  --goal "Add a null guard for item.location in the update flow."

# Fix failing tests in worktree
python scripts/sidecar.py test-fix \
  --goal "Fix the failing test in items.test.ts."
```

### Management

```bash
# Diagnose the setup (opencode present, agents load as primary, models authed)
python scripts/sidecar.py doctor

# Confirm a worker agent runs without falling back to the default agent
python scripts/sidecar.py verify-agent

# List all tasks
python scripts/sidecar.py list

# Collect results for a task
python scripts/sidecar.py collect --task-id <task-id>

# Detect file overlaps between parallel worktree patches
python scripts/sidecar.py check-conflicts

# Clean up a task
python scripts/sidecar.py cleanup --task-id <task-id>
```

## Running Workers in Parallel

Task IDs are claimed atomically (the task directory is created with the ID, so
two simultaneous launches can never reuse the same ID), and each writable
worker runs in its own git worktree. This makes it safe to launch several
sidecar tasks at once.

When you run more than one **writable** task in parallel, each produces an
independent `patch.diff`. Before applying any of them, run `check-conflicts`:
it maps every patch to the files it changes and reports any file touched by
more than one task. Reconcile overlapping patches manually; apply
non-overlapping patches independently.

## After the Worker Finishes

Inspect the result files:

```
.agent_sidecars/tasks/<task-id>/result.md      # Human-readable result
.agent_sidecars/tasks/<task-id>/result.json    # Machine-readable result
.agent_sidecars/tasks/<task-id>/worker_text.md  # Worker's text answer (from JSON text events)
.agent_sidecars/tasks/<task-id>/events.jsonl    # Raw OpenCode event stream (one JSON object/line)
.agent_sidecars/tasks/<task-id>/patch.diff      # (writable tasks only)
.agent_sidecars/tasks/<task-id>/metadata.json   # Execution metadata
```

The main agent must verify all findings before acting.

### Status semantics for writable tasks

A writable task (`implement` / `test-fix`) only reports `completed` when its
changes actually reached the worktree. `sidecar.py` exports the full worktree
diff — **including new files** (`git add -N` before `git diff`, so a created
file is not silently dropped) — then fact-checks it: if the worker invoked a
write tool or claimed file changes in its report but `patch.diff` is empty, the
task is forced to `status: failed` with a CRITICAL warning rather than a
misleading `completed`. This catches the case where the worker's edit was
denied and it fell back to a non-persisting sandbox/exec tool (e.g. an MCP
`ctx_execute`) whose writes never touch disk. So: trust `patch.diff` as the
source of truth, not the worker's prose.

## Model Routing (two tiers)

Tasks split into two tiers by what they need:

| Tier | Modes | Why |
|------|-------|-----|
| **fast** | `explore`, `log` | Read-heavy (find files, scan logs); speed over judgment |
| **quality** | `review`, `implement`, `test-fix` | Find bugs / write code / fix tests; wrong output is costly |

The model for each tier is resolved in this priority:

1. CLI `--model <id>` (per-task override)
2. Env var: `OPENCODE_SIDECAR_FAST_MODEL` / `OPENCODE_SIDECAR_QUALITY_MODEL`
3. Project config file `.opencode-sidecar.json`
4. **Auto-detect on first run** — probes `opencode models` + `opencode auth list`,
   keyword-scores models, picks one fast + one quality, and writes the config. A
   stderr notice points the user at `init` to confirm or change it.

### Onboarding flow

When the user first sets up the skill (or asks to configure it), guide them through:

```bash
# 1. Probe what's authed and what models exist, with an auto-guessed split:
python scripts/sidecar.py init

# 2. Recommend a fast + quality model based on the output (use your model
#    knowledge + the authed providers), then let the user pick.

# 3. Persist the choice:
python scripts/sidecar.py config set \
  --fast "deepseek/deepseek-v4-flash" \
  --quality "deepseek/deepseek-v4-pro"

# 4. Verify:
python scripts/sidecar.py config show
```

`init` is the deterministic half (probe + display). The recommendation itself is
the main agent's job — reason about which authed model suits "fast" vs "quality"
from the listed ids and provider names, and ask the user to confirm.

## Worker Roles & Engine-Enforced Permissions

Each mode maps to a dedicated OpenCode **worker agent** (a `mode: primary`
agent defined under `opencode/agents/`). `sidecar.py` runs the worker with
`opencode run --agent <name>` and points OpenCode at the bundled agents via the
`OPENCODE_CONFIG_DIR` environment variable. OpenCode then enforces the agent's
permissions at the engine level — a read-only worker physically cannot edit
files, regardless of what the prompt says. The prompt constraints are a
secondary guard, not the primary one.

These agents are deliberately `mode: primary` (not `subagent`): `opencode run
--agent` only accepts a primary agent and silently falls back to the default
agent if given a subagent. They are still *sidecar worker agents* in product
terms — Claude is the main brain; these workers only execute bounded tasks.

| Mode | Worker agent | Access | Engine permissions |
|------|--------------|--------|--------------------|
| `explore` | `sidecar-explorer` | Read-only | `edit` denied; secret files denied for `read`; `bash` limited to read-only commands |
| `review` | `sidecar-reviewer` | Read-only | `edit` denied; secret files denied for `read`; `bash` limited to read-only commands |
| `log` | `sidecar-log-analyst` | Read-only | `edit` denied; secret files denied for `read`; `bash` limited to read-only commands |
| `implement` | `sidecar-implementer` | Worktree-writable | `edit` allowed; `git commit`/`push`/`add`/`reset`, installs, `rm -rf` denied |
| `test-fix` | `sidecar-test-fixer` | Worktree-writable | `edit` allowed; `git commit`/`push`/`add`/`reset`, installs, `rm -rf` denied |

Agents load from the skill's bundled `opencode/` directory via
`OPENCODE_CONFIG_DIR` — nothing is copied into your project's `.opencode/`, and
your global/provider/auth config is left intact. Because the env var (not the
working directory) controls discovery, the same agents load whether a task runs
at the project root (read-only modes) or inside a nested worktree (writable
modes).

To verify the setup:
- `python scripts/sidecar.py doctor` — static check: opencode present, every
  worker agent loads as `primary`, models/credentials available.
- `python scripts/sidecar.py verify-agent` — runs a minimal real prompt and
  confirms OpenCode did not fall back to the default agent.
- `OPENCODE_CONFIG_DIR=<skill>/opencode opencode agent list` — prints each
  agent's resolved permission rules.

## Error Handling

The script handles:
- OpenCode not installed → clear error message.
- Not a git repository → clear error message.
- Worker agent missing or not `primary` → OpenCode would fall back to the
  default agent (losing engine permissions); the script detects the fallback
  warning, flags it as a CRITICAL security warning, and marks the task failed.
- Worker timeout → the worker is killed but any partial output it already
  produced is preserved in `stdout.log` (output is streamed to disk, not
  buffered in memory).
- Empty worker output → partial status.
- Worktree creation failure → failed status.
- Writable mode without a worktree → refused (writable workers never touch the
  main working tree).
- The OpenCode `--format json` output is a line-delimited **event stream**, not
  a single answer JSON. The script parses it into events (`events.jsonl`),
  extracts the worker's text (`worker_text.md`), and reads the structured
  result JSON from that text.
- Forbidden commands are detected from the worker's *executed* shell commands
  (parsed from the JSON event stream `tool_use` events), not from narration
  that merely mentions them — avoiding false positives.

All errors produce task directories with metadata for debugging.
