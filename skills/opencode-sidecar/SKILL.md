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

```bash
# Implement in worktree
python scripts/sidecar.py implement \
  --goal "Add a null guard for item.location in the update flow." \
  --worktree

# Fix failing tests in worktree
python scripts/sidecar.py test-fix \
  --goal "Fix the failing test in items.test.ts." \
  --worktree
```

### Management

```bash
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
.agent_sidecars/tasks/<task-id>/patch.diff      # (writable tasks only)
.agent_sidecars/tasks/<task-id>/metadata.json   # Execution metadata
```

The main agent must verify all findings before acting.

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
4. **Auto-detect on first run** — probes `opencode models` + `opencode providers list`,
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

Each mode maps to a dedicated OpenCode **subagent** (defined under
`opencode/agents/`). On the first task run, `sidecar.py` syncs these agent
definitions into the project's `.opencode/agents/` and invokes the worker with
`opencode run --agent <name>`. OpenCode then enforces the agent's permissions at
the engine level — a read-only worker physically cannot edit files, regardless
of what the prompt says. The prompt constraints are a secondary guard, not the
primary one.

| Mode | Subagent | Access | Engine permissions |
|------|----------|--------|--------------------|
| `explore` | `sidecar-explorer` | Read-only | `edit`/`write` denied; `bash` limited to read-only commands |
| `review` | `sidecar-reviewer` | Read-only | `edit`/`write` denied; `bash` limited to read-only commands |
| `log` | `sidecar-log-analyst` | Read-only | `edit`/`write` denied; `bash` limited to read-only commands |
| `implement` | `sidecar-implementer` | Worktree-writable | `edit`/`write` allowed; `git commit`/`push`, installs, `rm -rf` denied |
| `test-fix` | `sidecar-test-fixer` | Worktree-writable | `edit`/`write` allowed; `git commit`/`push`, installs, `rm -rf` denied |

OpenCode discovers agents by walking up from the working directory to find a
`.opencode/agents/` folder, so the synced agents apply both to read-only tasks
(run at the project root) and writable tasks (run inside a nested worktree).
Only `sidecar-*.md` files are written — user-authored agents are left untouched.

To verify the loaded permissions yourself: `opencode agent list`.

## Error Handling

The script handles:
- OpenCode not installed → clear error message.
- Not a git repository → clear error message.
- Worker timeout → the worker is killed but any partial output it already
  produced is preserved in `stdout.log` (output is streamed to disk, not
  buffered in memory).
- Empty worker output → partial status.
- Worktree creation failure → failed status.
- Forbidden commands are detected from the worker's *executed* shell commands
  (parsed from the JSON event stream), not from narration that merely mentions
  them — avoiding false positives.

All errors produce task directories with metadata for debugging.
