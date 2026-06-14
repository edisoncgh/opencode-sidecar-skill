# Result Package Contract

After completing a sidecar task, the worker must produce the following files:

## Required Files

| File | Description |
|------|-------------|
| `result.md` | Human-readable structured result |
| `result.json` | Machine-readable result matching `schemas/result.schema.json` |
| `stdout.log` | Full stdout output from the worker session |
| `stderr.log` | Full stderr output from the worker session |
| `metadata.json` | Task metadata and execution info |

## Optional Files (Writable Tasks)

| File | Description |
|------|-------------|
| `patch.diff` | Git diff of changes made in worktree |
| `files-changed.txt` | List of changed files |

## Optional Files (Test Tasks)

| File | Description |
|------|-------------|
| `test-output.txt` | Test execution output |

## result.json Schema

See `schemas/result.schema.json` for the complete schema.

### Required Fields

- `task_id` - Matches the task envelope
- `worker` - Worker agent type
- `model` - Model used
- `status` - One of: `completed`, `failed`, `partial`, `blocked`
- `confidence` - One of: `low`, `medium`, `high`
- `summary` - Brief result summary

### Status Values

| Status | Meaning |
|--------|---------|
| `completed` | Task finished successfully |
| `failed` | Task could not be completed |
| `partial` | Task partially completed |
| `blocked` | Task blocked by external dependency |

### Confidence Values

| Confidence | Meaning |
|------------|---------|
| `low` | Findings are uncertain, need verification |
| `medium` | Findings are likely correct |
| `high` | Findings are well-evidenced |

### Severity Levels

| Severity | Meaning |
|----------|---------|
| `critical` | Security vulnerability or data loss risk |
| `high` | Bug or significant quality issue |
| `medium` | Maintainability concern |
| `low` | Style or minor suggestion |
| `info` | Informational note |
