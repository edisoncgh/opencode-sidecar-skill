# OpenCode Sidecar Skill

A Claude Code Skill that delegates bounded coding sub-tasks to OpenCode worker agents using cheaper models, while keeping the main Claude agent in control of final decisions.

> 中文文档见 [skills/opencode-sidecar/README.zh-CN.md](skills/opencode-sidecar/README.zh-CN.md)

## Overview

This skill implements a **sidecar execution system** where:

- **Main brain** (Claude/Opus/GPT) handles planning, judgment, and final decisions.
- **Workers** (DeepSeek, Mimo, Qwen, etc.) handle token-heavy, bounded tasks.
- Communication is via structured **artifacts** (task.json → result.json).
- Writable tasks run in **isolated git worktrees** with patch export.

## Install

This repo follows the [`skill`](https://www.npmjs.com/package/skill) CLI layout — the
skill lives under `skills/opencode-sidecar/`. Install it into a project with:

```bash
SKILL_BASE_URL=https://github.com/<your-org>/<this-repo>/tree/main \
  npx skill skills/opencode-sidecar
```

This downloads `skills/opencode-sidecar/` into the project's local skills directory
(`.codebuddy/skills/opencode-sidecar/`, or `.claude/skills/opencode-sidecar/` for Claude Code).

## Quick Start

Run the orchestrator from inside the installed skill directory (paths are relative to the skill root):

```bash
# Explore a codebase
python scripts/sidecar.py explore \
  --goal "Find where user authentication is handled."

# Review current changes
python scripts/sidecar.py review \
  --scope "Current git diff"

# Analyze a log file
python scripts/sidecar.py log \
  --log-file "test-failure.log" \
  --goal "Identify the root cause."

# Implement in isolated worktree
python scripts/sidecar.py implement \
  --goal "Add null guard for user.location." \
  --worktree

# Detect file overlaps between parallel worktree patches
python scripts/sidecar.py check-conflicts
```

## Architecture

```
Claude Code Main Agent
    │
    ├── Generates task envelope
    ├── Calls sidecar.py
    │
    └── .agent_sidecars/tasks/<task-id>/
        ├── task.json          # Structured task definition
        ├── task.md            # Human-readable task description
        ├── result.json        # Structured result
        ├── result.md          # Human-readable result
        ├── stdout.log         # Full worker output (streamed to disk)
        ├── stderr.log         # Error output
        ├── metadata.json      # Execution metadata
        └── patch.diff         # (writable tasks) Git diff
```

## Worker Modes

The worker role and its constraints are embedded directly in the task prompt sent to
OpenCode (no custom OpenCode agent registration required).

| Mode | Role | Access |
|------|------|--------|
| `explore` | Codebase exploration — find files, trace call chains, map modules | Read-only |
| `review` | Code review — bugs, regressions, missing tests | Read-only |
| `log` | Log / test-failure analysis — root-cause hypotheses | Read-only |
| `implement` | Implement small bounded changes | Worktree-writable |
| `test-fix` | Fix failing tests | Worktree-writable |

## Configuration

Set environment variables to customize model selection:

```bash
export OPENCODE_SIDECAR_DEFAULT_MODEL=deepseek/deepseek-chat
export OPENCODE_SIDECAR_EXPLORE_MODEL=deepseek/deepseek-chat
export OPENCODE_SIDECAR_IMPLEMENT_MODEL=mimo/mimo-pro
```

## Repository Layout

```
<repo-root>/
├── README.md                          # This file (project-level)
├── design.md                          # Full specification
└── skills/
    └── opencode-sidecar/              # The installable skill
        ├── SKILL.md                   # Skill instructions
        ├── README.zh-CN.md            # 中文文档
        ├── scripts/
        │   └── sidecar.py             # Main orchestrator
        ├── templates/                 # Task / result templates
        ├── schemas/                   # task & result JSON schemas
        └── opencode/agents/           # OpenCode worker agent definitions
```

## Implementation Status

### Phase 1 — MVP Read-Only Workers
- [x] `explore`, `review`, `log` commands
- [x] Structured task/result artifacts
- [x] stdout/stderr streamed to disk (preserved on timeout)
- [x] Metadata generation and error handling

### Phase 2 — Writable Workers + Worktree
- [x] `implement` / `test-fix` with `--worktree`
- [x] Automatic worktree creation and patch export
- [x] Sensitive-file detection
- [x] Atomic task-id claim (parallel-safe) + `check-conflicts`
- [x] Process-tree kill on timeout (no orphaned workers)

### Phase 3 — Server Attach (Planned)
- [ ] Connect to running `opencode serve`, fallback to CLI

### Phase 4 — HTTP API (Future)
- [ ] Direct API calls, concurrent workers, task queue

## References

- [Design Document](design.md) — Full specification
- [OpenCode](https://github.com/opencode-ai/opencode) — Worker runtime
- [`skill` CLI](https://www.npmjs.com/package/skill) — Installer
