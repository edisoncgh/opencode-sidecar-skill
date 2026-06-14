# Sidecar Task: Implementation

## Task ID

{{TASK_ID}}

## Worker Role

sidecar-implementer

## Goal

{{GOAL}}

## Scope

Implement the requested change in the isolated worktree. Keep changes minimal and focused.

## Worktree Path

{{WORKTREE_PATH}}

## Allowed Actions

- Edit files inside the worktree.
- Run documented tests.
- Run lint/typecheck if already configured.
- Inspect diff/status.

## Forbidden Actions

- Do not edit files outside the worktree.
- Do not commit.
- Do not push.
- Do not install dependencies unless explicitly instructed.
- Do not modify secrets or environment files.
- Do not perform broad refactors.
- Do not modify `.env`, `.env.*`, `*.pem`, `*.key`, or credential files.

## Output Requirements

Write a structured final answer with:

1. **Summary** - Brief overview of what was implemented.
2. **Files Changed** - List of modified files with descriptions.
3. **Patch Summary** - What the changes do.
4. **Tests Run** - Any tests that were executed and their results.
5. **Risks** - Potential issues with the implementation.
6. **Unfinished Items** - What still needs to be done.

Also write machine-readable JSON matching `schemas/result.schema.json`.
