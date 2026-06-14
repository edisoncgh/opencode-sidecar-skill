# Sidecar Task: Test Fix

## Task ID

{{TASK_ID}}

## Worker Role

sidecar-test-fixer

## Goal

{{GOAL}}

## Scope

Fix failing tests in the isolated worktree. Prioritize fixing production code over modifying tests.

## Worktree Path

{{WORKTREE_PATH}}

## Allowed Actions

- Edit files inside the worktree.
- Run tests.
- Run lint/typecheck if already configured.
- Inspect diff/status.

## Forbidden Actions

- Do not edit files outside the worktree.
- Do not commit.
- Do not push.
- Do not install dependencies unless explicitly instructed.
- Do not modify secrets or environment files.
- Do not delete tests to make them pass.

## Rules

- Fix production code first, tests second.
- If you must modify a test, explain why.
- Never delete a test to make the suite pass.

## Output Requirements

Write a structured final answer with:

1. **Summary** - Brief overview of what was fixed.
2. **Files Changed** - List of modified files with descriptions.
3. **Tests Fixed** - Which tests now pass.
4. **Test Modification Justification** - If any tests were modified, explain why.
5. **Patch Summary** - What the changes do.
6. **Risks** - Potential issues.
7. **Unfinished Items** - What still needs to be done.

Also write machine-readable JSON matching `schemas/result.schema.json`.
