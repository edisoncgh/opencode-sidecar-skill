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

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `OPENCODE_SIDECAR_DEFAULT_MODEL` | Default model for all workers | `deepseek/deepseek-chat` |
| `OPENCODE_SIDECAR_EXPLORE_MODEL` | Model for exploration workers | (uses default) |
| `OPENCODE_SIDECAR_REVIEW_MODEL` | Model for review workers | (uses default) |
| `OPENCODE_SIDECAR_LOG_MODEL` | Model for log analysis workers | (uses default) |
| `OPENCODE_SIDECAR_IMPLEMENT_MODEL` | Model for implementation workers | (uses default) |
| `OPENCODE_SIDECAR_TEST_FIX_MODEL` | Model for test fix workers | (uses default) |

## Worker Roles

Each mode maps to a worker role. The role and its constraints (read-only vs.
worktree-writable, forbidden actions) are embedded directly in the task prompt
sent to OpenCode, so the workers do not depend on custom OpenCode agent
registration.

| Mode | Role | Access |
|------|------|--------|
| `explore` | Codebase exploration | Read-only |
| `review` | Code review | Read-only |
| `log` | Log / test failure analysis | Read-only |
| `implement` | Implementation | Worktree-writable |
| `test-fix` | Test fixing | Worktree-writable |

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
