# Sidecar Task: Log Analysis

## Task ID

{{TASK_ID}}

## Worker Role

sidecar-log-analyst

## Goal

{{GOAL}}

## Scope

Analyze the provided log file and identify root cause hypotheses.

## Log File

{{LOG_FILE}}

## Allowed Actions

- Read the specified log file.
- Search project source code for error references.
- Run `git status` and `git diff` for context.

## Forbidden Actions

- Do not modify files.
- Do not commit.
- Do not push.
- Do not install dependencies.
- Do not claim certainty without evidence.
- Do not invent command results.

## Output Requirements

Write a structured final answer with:

1. **Summary** - Brief overview of the log analysis.
2. **Root Cause Hypothesis** - Most likely cause of the failure.
3. **Supporting Evidence** - Specific log lines and code references.
4. **Verification Commands** - Commands or tests to run to confirm the hypothesis.
5. **Likely Fix Direction** - Suggested approach to resolve the issue.
6. **Uncertainties** - What you couldn't determine.

Also write machine-readable JSON matching `schemas/result.schema.json`.
