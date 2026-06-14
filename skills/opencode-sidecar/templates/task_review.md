# Sidecar Task: Code Review

## Task ID

{{TASK_ID}}

## Worker Role

sidecar-reviewer

## Goal

{{GOAL}}

## Scope

{{SCOPE}}

## Allowed Actions

- Read project files.
- Run `git status`.
- Run `git diff`.
- Run search commands such as `rg`.
- Run documented test commands only if explicitly necessary.

## Forbidden Actions

- Do not modify files.
- Do not commit.
- Do not push.
- Do not install dependencies.
- Do not read secrets, `.env` files, API keys, or private credentials.
- Do not deploy.
- Do not access external network unless explicitly required.

## Output Requirements

Write a structured final answer with:

1. **Summary** - Brief overview of review findings.
2. **Blocking Issues** - Issues that must be fixed before merge (with severity: critical/high).
3. **Non-Blocking Issues** - Issues that should be considered (with severity: medium/low/info).
4. **Test Gaps** - Missing test coverage.
5. **Suspicious Areas** - Code that needs human attention.
6. **Evidence** - File paths and line references for each finding.
7. **Suggested Fixes** - Concrete fix recommendations.
8. **Uncertainties** - What you couldn't determine.
9. **Requires Main Agent Decision** - Boolean flag.

Also write machine-readable JSON matching `schemas/result.schema.json`.
