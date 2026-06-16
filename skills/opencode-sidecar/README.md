# OpenCode Sidecar

**Strong model thinks. Cheap model works.**

You're running Opus or GPT in Claude Code — the smartest agent in the room. But
it's expensive, and a lot of what it does is grunt work: reading a hundred files
to find where a function lives, scanning a 50KB log for the actual error, doing
a first-pass review of a diff. That's not what you're paying premium tokens for.

This skill lets the strong model stay the **brain** — plan, judge, decide — while
offloading the token-heavy, bounded stuff to a cheaper OpenCode worker
(DeepSeek, MiMo, Qwen, whatever you've authed). The brain reads the worker's
findings, sanity-checks them, and decides what to do. The worker never touches
the main working tree.

```
   Claude (the brain)            OpenCode worker (the hands)
   ───────────────────           ───────────────────────────
   plan → delegate task    ──►   explore / review / log
                              ◄──   structured findings
   verify, synthesize           implement / test-fix
   decide & merge          ──►   (in isolated worktree)
                              ◄──   patch (you review before applying)
```

Communication is through files, not chat. Each task is an envelope
(`task.json`) that produces a result package (`result.md` + `result.json`). No
agent free-styling, no lost context, fully auditable.

## What it does

- **`explore`** — "where is X handled?" Find files, trace call chains, map
  modules. Read-only.
- **`review`** — review the current diff for bugs, regressions, missing tests.
  Read-only.
- **`log`** — point it at a failing test log, get back a root-cause hypothesis.
  Read-only.
- **`implement`** — make a small bounded change in an **isolated git worktree**,
  hand back a patch you review before applying.
- **`test-fix`** — fix failing tests in a worktree (production code first, never
  deletes tests).

A few things that matter for trust:

- **Workers are engine-permissioned, not just prompt-constrained.** Read-only
  workers get `edit: deny` at the OpenCode layer — they physically can't write,
  regardless of what the prompt says. Check it: `opencode agent list`.
- **Writable tasks are isolated.** They run in a fresh git worktree and only
  produce a patch. Nothing is auto-merged.
- **Two model tiers.** Speed jobs (`explore`, `log`) run on a fast model;
  judgment jobs (`review`, `implement`, `test-fix`) run on a quality model.
  First run auto-detects what you've authed and picks sensible defaults — then
  you confirm.

## Install

```bash
SKILL_BASE_URL=https://github.com/edisoncgh/opencode-sidecar-skill/tree/main \
  npx skill skills/opencode-sidecar
```

Drops the skill into `.codebuddy/skills/opencode-sidecar/`
(or `.claude/skills/opencode-sidecar/` for Claude Code).

## First run: pick your models

```bash
cd skills/opencode-sidecar
python scripts/sidecar.py init
```

This probes `opencode` for what you've authed and what models exist, then prints
an auto-guessed fast/quality split. The main agent (you, or Claude guided by
SKILL.md) recommends a pair, you pick, then:

```bash
python scripts/sidecar.py config set \
  --fast "deepseek/deepseek-v4-flash" \
  --quality "deepseek/deepseek-v4-pro"
```

That's it. Written to `.opencode-sidecar.json` (gitignored — it's machine-local).
If you skip this, the first task auto-detects and writes it for you.

## Use it

```bash
python scripts/sidecar.py explore --goal "Find where auth tokens are validated."
python scripts/sidecar.py review  --scope "Current git diff"
python scripts/sidecar.py log     --log-file crash.log --goal "Root cause."
python scripts/sidecar.py implement --goal "Add null guard for user.location."
python scripts/sidecar.py check-conflicts   # if you ran several implement tasks in parallel
python scripts/sidecar.py list              # see all tasks
python scripts/sidecar.py collect --task-id 2026-06-15-001   # pull a task's results
```

Each task lands in `.agent_sidecars/tasks/<id>/`. The full envelope + result
package is: `task.md`, `task.json`, `events.jsonl`, `worker_text.md`,
`result.md`, `result.json`, and (for writable tasks) `patch.diff`. **The main
agent must review findings/patches before acting** — that's the whole point of
the split.

The result package also includes a lightweight capability audit. OpenCode may
inherit the user's global/project MCP tools; sidecar does not try to become a
full security sandbox. Instead it records what actually happened: tools used,
MCP/custom tools, web-like access, write-capable tools, and reads of internal
artifacts like `.agent_sidecars/` or `.git/`. If a worker used unexpected tools
or read runtime artifacts, treat that as review evidence before trusting the
answer.

`result.json` has a `contract_status`: `structured` means the worker emitted a
parseable sidecar-style JSON block with the expected result fields, while
`fallback` means sidecar synthesized JSON from the Markdown report and marked
confidence low. The Markdown report is always preserved in `worker_text.md` /
`result.md`.

## How it works (briefly)

```
skills/opencode-sidecar/
├── SKILL.md              what the main agent reads to drive the skill
├── scripts/sidecar.py    the orchestrator (probe, dispatch, collect)
├── opencode/agents/      5 worker agent definitions (mode: primary, engine-permissioned)
├── templates/            task envelope + result contract templates
└── schemas/              task.json / result.json JSON schemas
```

`sidecar.py` is the only moving part. Worker agents are OpenCode **primary**
agents (`mode: primary`) — in product terms they're sidecar workers, but inside
OpenCode they're ordinary primary agents that `opencode run --agent <name>`
starts directly. They load via the `OPENCODE_CONFIG_DIR` env var pointing at the
bundled `opencode/` folder — nothing is copied into your project's
`.opencode/agents/`, and your provider/auth config stays intact. For each task
`sidecar.py`: claims a unique id atomically (so parallel tasks can't collide),
writes a task envelope, runs `opencode run --agent <name> --format json`, parses
the JSONL event stream into `events.jsonl` + `worker_text.md`, streams raw
output to disk (so a timeout still preserves partial results), kills the whole
process tree on timeout (no orphaned workers burning tokens), and emits a
structured result. `implement`/`test-fix` always run inside an isolated git
worktree and only produce a `patch.diff` — they never auto-merge, commit, or
push. That's it — no server, no queue, no dashboard.

> 中文文档: [README.md](../../README.md) ·
> Full spec: [design.md](../../design.md) (in `.gitignore`, dev-only)
