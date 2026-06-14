# OpenCode Sidecar Skill（中文文档）

一个 Claude Code Skill：把边界清晰、token 消耗大的编码子任务委托给 OpenCode worker（使用更便宜的模型执行），同时让主 Claude agent 始终保留最终决策权。

> English docs: see the project-level [README.md](../../README.md)

## 概述

这个 skill 实现了一套 **sidecar 执行系统**：

- **主脑**（Claude/Opus/GPT）负责规划、判断和最终决策。
- **Worker**（DeepSeek、Mimo、Qwen 等）承担 token-heavy、边界清晰的执行任务。
- 主脑与 worker 通过结构化 **artifact**（task.json → result.json）通信，而不是实时群聊。
- 可写任务在**隔离的 git worktree** 中运行，只导出 patch，绝不自动合并。

## 安装

本仓库遵循 [`skill`](https://www.npmjs.com/package/skill) CLI 的目录约定 —— skill 位于
`skills/opencode-sidecar/`。在目标项目里这样安装：

```bash
SKILL_BASE_URL=https://github.com/<你的-org>/<本仓库>/tree/main \
  npx skill skills/opencode-sidecar
```

它会把 `skills/opencode-sidecar/` 整个目录下载到项目本地的 skill 目录
（`.codebuddy/skills/opencode-sidecar/`，Claude Code 下为 `.claude/skills/opencode-sidecar/`）。

## 快速开始

在已安装的 skill 目录内运行编排器（路径相对 skill 根目录）：

```bash
# 探索代码库
python scripts/sidecar.py explore \
  --goal "找到用户认证逻辑在哪里处理。"

# 审查当前改动
python scripts/sidecar.py review \
  --scope "当前 git diff"

# 分析日志文件
python scripts/sidecar.py log \
  --log-file "test-failure.log" \
  --goal "定位最可能的根因。"

# 在隔离 worktree 中实现
python scripts/sidecar.py implement \
  --goal "给 user.location 加一个空值保护。" \
  --worktree

# 检测多个并行 worktree patch 之间的文件重叠
python scripts/sidecar.py check-conflicts
```

## 核心原则

1. **主脑保留最终决策权** —— 拆解任务、决定是否委托、审核 worker 输出、决定是否采纳与合并，都由主 agent 负责。
2. **worker 只做边界清晰的执行任务** —— 探索、审查、日志分析、小范围实现尝试；不碰最终架构、认证安全、依赖安装、commit/push/部署。
3. **通信走 artifact 协议** —— 主 agent 写 task envelope，worker 产出 result package，不依赖自然语言群聊。
4. **可写任务必须隔离** —— 在独立 git worktree 中改动，只产出 patch.diff，绝不自动 apply。

## 命令一览

| 命令 | 说明 | 类型 |
|------|------|------|
| `explore --goal "..."` | 代码库探索：找文件、梳理调用链、总结结构 | 只读 |
| `review --scope "..."` | 代码审查：bug、回归、缺失测试 | 只读 |
| `log --log-file <path> --goal "..."` | 日志/测试失败分析：根因假设 | 只读 |
| `implement --goal "..." --worktree` | 在隔离 worktree 中实现小范围改动 | 可写 |
| `test-fix --goal "..." --worktree` | 在隔离 worktree 中修复失败测试 | 可写 |
| `list` | 列出所有任务 | 管理 |
| `collect --task-id <id>` | 收集某任务的结果 | 管理 |
| `check-conflicts` | 检测并行 worktree patch 间的文件重叠 | 管理 |
| `cleanup --task-id <id>` | 清理任务及其 worktree | 管理 |

可选参数：`--model <model>`（覆盖模型）、`--dir <path>`（项目目录）、`--timeout <秒>`。

## 任务产物结构

```
.agent_sidecars/tasks/<task-id>/
├── task.json          # 结构化任务定义
├── task.md            # 人类可读的任务描述
├── result.json        # 结构化结果
├── result.md          # 人类可读的结果
├── stdout.log         # worker 完整输出（实时写盘，超时也保留）
├── stderr.log         # 错误输出
├── metadata.json      # 执行元数据
└── patch.diff         # （可写任务）git diff
```

## 并行执行与安全

- **task ID 原子认领**：任务目录用 ID 名以 `exist_ok=False` 创建，操作系统保证两个同时启动的 worker 不会复用同一 ID，可安全并行。
- **超时保护**：worker 输出实时流式写入日志文件，超时被 kill 时已产生的部分输出仍然保留；kill 时会终止整棵进程树（避免 Windows 上残留孤儿 worker 进程烧 token）。
- **冲突检测**：多个可写 worker 并行时各自产出独立 patch，apply 前用 `check-conflicts` 检查是否有文件被多个任务改动，重叠的需手动协调。
- **禁止命令检测**：从 worker 实际执行的 bash 命令（解析 JSON 事件流）判断违规，而不是扫描叙述文本，避免误报。
- **敏感文件检测**：patch 涉及 `.env`、密钥、凭证等文件时标记为 high risk。

## 模型配置

通过环境变量自定义各类 worker 使用的模型：

| 环境变量 | 用途 | 默认 |
|----------|------|------|
| `OPENCODE_SIDECAR_DEFAULT_MODEL` | 所有 worker 的默认模型 | `deepseek/deepseek-chat` |
| `OPENCODE_SIDECAR_EXPLORE_MODEL` | 探索 worker | 用默认值 |
| `OPENCODE_SIDECAR_REVIEW_MODEL` | 审查 worker | 用默认值 |
| `OPENCODE_SIDECAR_LOG_MODEL` | 日志分析 worker | 用默认值 |
| `OPENCODE_SIDECAR_IMPLEMENT_MODEL` | 实现 worker | 用默认值 |
| `OPENCODE_SIDECAR_TEST_FIX_MODEL` | 修测试 worker | 用默认值 |

## 目录结构

```
skills/opencode-sidecar/
├── SKILL.md                   # skill 指令
├── README.zh-CN.md            # 本文件
├── scripts/
│   └── sidecar.py             # 主编排器
├── templates/                 # 任务 / 结果模板
│   ├── task_explore.md
│   ├── task_review.md
│   ├── task_log.md
│   ├── task_implement.md
│   ├── task_test_fix.md
│   └── result_contract.md
├── schemas/
│   ├── task.schema.json       # task envelope JSON schema
│   └── result.schema.json     # result package JSON schema
└── opencode/agents/           # OpenCode worker agent 定义
    ├── sidecar-explorer.md
    ├── sidecar-reviewer.md
    ├── sidecar-log-analyst.md
    ├── sidecar-implementer.md
    └── sidecar-test-fixer.md
```

## 参考

- [OpenCode](https://github.com/opencode-ai/opencode) —— worker 运行时
- [`skill` CLI](https://www.npmjs.com/package/skill) —— 安装器
