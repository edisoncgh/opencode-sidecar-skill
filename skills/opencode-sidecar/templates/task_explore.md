# Sidecar Task: Codebase Exploration

## Task ID

{{TASK_ID}}

## Worker Role

sidecar-explorer

## Goal

{{GOAL}}

## Scope

Explore the current project directory. Find relevant files, trace call chains, and map module relationships.

## Allowed Actions

- Read project files.
- Run `git status`.
- Run `git diff`.
- Run search commands (`rg`, `find`, `grep`).
- List directory contents.

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

1. **Summary** - Brief overview of what was found.
2. **Relevant Files** - List of files related to the goal, with paths.
3. **Key Functions/Classes** - Important symbols and their locations.
4. **Call Flow** - How modules connect (if applicable).
5. **Uncertainties** - What you couldn't determine.
6. **Recommended Next Steps** - What the main agent should do next.

Also write machine-readable JSON matching `schemas/result.schema.json`.
