# 子代理删除兼容性判定

本说明只处理一种已经复现并验证过的兼容冲突。机器可读指纹位于
[`subagent-delete-compatibility.json`](subagent-delete-compatibility.json)，实际判定必须由
`scripts/subagent_delete_compat.py` 完成，不能只看本文或只看版本号。

## 为什么会出现 `agent_jobs` 缺表

已验证的冲突由两个组件不在同一数据库时代造成：

- `codex 0.142.2` 的官方严格删除实现仍会更新 `agent_jobs` 和 `agent_job_items`；
- 本机 `state_5.sqlite` 已成功执行 migration 42 `drop agent jobs`，这两张表已经被删除；
- 因此官方删除在清理 rollout 后、删除线程索引前报 `no such table: agent_jobs`，形成“rollout 已消失，但线程行和 spawn edge 仍在”的部分删除状态。

官方依据：

- [0.142.2 创建 agent jobs 的 migration 14](https://github.com/openai/codex/blob/rust-v0.142.2/codex-rs/state/migrations/0014_agent_jobs.sql)
- [0.142.2 增加超时字段的 migration 15](https://github.com/openai/codex/blob/rust-v0.142.2/codex-rs/state/migrations/0015_agent_jobs_max_runtime_seconds.sql)
- [删除 agent jobs 的 migration 42](https://github.com/openai/codex/blob/687f05cb946d10c96f90dd7ce82e11465c6e20a7/codex-rs/state/migrations/0042_drop_agent_jobs.sql)
- [0.142.2 的严格线程删除实现](https://github.com/openai/codex/blob/rust-v0.142.2/codex-rs/state/src/runtime/threads.rs#L967-L1072)

## 决策矩阵

| 实时条件 | 判定 | 允许动作 |
| --- | --- | --- |
| CLI、schema 尚未命中已知故障，且没有 canary 结果 | `canary_required` | 先做新鲜在线备份，再只删一个 canary；不能预先建兼容表 |
| `0.142.2`、migration 最大值/校验和精确命中、4 个对象全缺、canary 报错和部分删除状态精确命中、外部备份新鲜有效 | `known_workaround_eligible` | 本次再次明确授权后，才可临时安装兼容对象并重试同一个 canary |
| 4 个对象全部存在、DDL 指纹精确且两表为空 | `compat_installed` | 不得重复安装；只能继续原事故流程，完成后凭本次安装结果移除 |
| 兼容矩阵超过复核日期 | `stale_profile_update_required` | 停删，先更新或重新验证技能 |
| CLI、migration 最大值、migration 校验和或对象结构未收录 | `unsupported_update_required` | 停删，检查可信更新；仍不支持则补证据、测试并创建 Draft PR |
| 报错不同、缺的是其他表、4 个对象部分存在/结构不符/非空、锁定、备份失败、`quick_check` 失败或 canary 再次失败 | `unsafe_stop` | 保留现场，禁止自动修库、自动重试或批量删除 |

“版本正确”不是一个独立结论。这里的正确组合是：CLI 版本、SQLx migration 最大值、指定 migration 的描述/成功标记/96 位校验和、4 个 SQLite 对象状态、原始 canary 错误和实时运行状态全部相符。即使全部命中，官方 canary 仍是最终判据。

目前唯一可安装的临时方案只覆盖：

- Codex CLI：`0.142.2`；
- 状态库：最大成功 migration 为 `42`；
- migration 14、15、42 的描述、成功值和校验和与机器配置完全相同；
- `agent_jobs`、`agent_job_items` 及两个索引全部不存在；
- 精确错误为 `no such table: agent_jobs`；
- canary 的 rollout 已缺失，但线程行和 spawn edge 仍存在，批量删除尚未开始；
- 故障证据文件不超过 24 小时，脚本实时复核 canary 线程行、精确 spawn-edge 集合和 rollout 缺失状态；
- `state_5.sqlite`、`goals_1.sqlite`、`memories_1.sqlite` 的外部在线备份仍在允许的新鲜期内，并重新通过源文件大小、备份大小、SHA-256 和 `quick_check` 验证。

任意一项不同，都不能复用这套 DDL。脚本不支持 `IF EXISTS` / `IF NOT EXISTS` 掩盖漂移，也不会从任意“参考数据库”复制表结构。

## 临时兼容对象的生命周期

1. `diagnose` 默认只读，不调用删除接口。
2. `install` 默认也只输出计划。执行时要求 `--execute`、固定确认口令和位于 `CodexHome` 外的新审计文件；执行路径只接受干净、已审查且 `main == origin/main` 的本仓库内置 profile，以及非 reparse 的 `CodexHome\state_5.sqlite`。脚本先写入 `prepared` 审计记录，再在 `BEGIN IMMEDIATE` 事务内复核 canary 的精确 edge 集合并创建 2 张空表和 2 个索引；提交前先将安装后的 SQLite `schema_version` 和对象 `rootpage` 写入 `commit-pending` 审计状态。它不修改 `_sqlx_migrations`。
3. 安装后只能通过官方路径重试同一个 canary。结果不同、数据库锁定、RPC/超时、保护 ID 漂移或再次部分删除时立即停止。
4. 批量流程完成并验证后，`remove` 必须在 24 小时自动移除窗口内读取本次 `install` 结果，核对当前 CLI、run nonce、数据库文件身份、profile/故障证据/备份摘要哈希、安装后的 `schema_version` 与 4 个对象的 `rootpage`，确认对象结构精确且两表仍为空，再按索引、子表、父表顺序移除。结果缺失、被改写、对象被重建或窗口超时则停止并人工评估，不能伪造结果继续。
5. 移除后再次验证 migration 最大值未变且 `quick_check = ok`。临时数据库备份在整个批次验证成功后才清除。

## 过时检测和自动更新

本配置的当前复核截止日为 `2026-11-01`。以下任一情况都会把技能视为需要更新：

- 到达复核截止日后仍要使用临时方案；
- CLI 版本不在矩阵中；
- 数据库 migration 或对象指纹不在矩阵中；
- 官方删除报错、协议或部分删除形态发生变化；
- 官方 Codex 源码已经调整相关 migration 或删除实现；
- 当前脚本、fixture 或文档无法复现新的实际行为。

自动更新分两层：

1. **可信上游已有更新**：`refresh_skill.py` 只在本地仓库干净、位于跟踪 `origin/main` 的 `main`、origin 精确命中允许仓库、TLS 校验未关闭、远端历史可快进时工作。它冻结刚 fetch 的确切提交 SHA，并在临时 worktree 对该提交做 front matter、JSON、Python 编译和 PowerShell 解析等静态验证；不会执行刚拉取的测试代码。合并前再次冻结 HEAD、工作区、origin、事故证据及本地 `origin/main`，只对已验证的同一 SHA 执行 `git merge --ff-only`，更新后静态复验。它不更新 Codex CLI，不接触 Codex 数据库，不调用删除接口，也不推送或合并远端分支。
2. **出现尚未支持的新组合**：技能应自动停止现场删除、收集脱敏后的 CLI/schema/错误证据，在最新 `origin/main` 上创建功能分支，补充 profile、fixture、脚本、文档和测试，然后推送并创建 Draft PR。不得直接改 `main`、force-push、自动合并或猜测未知 DDL。

如果 canary 已造成部分删除、批量已开始或临时兼容对象已经安装，禁止在事故处理中自动刷新技能源码。未知格式的事故证据也按不安全处理。一般更新必须显式声明当前没有活动清理；兼容诊断为未知版本/schema 时，应把诊断 JSON 交给更新器，使“远端无更新”明确转为 `draft_pr_required`。先保全证据和数据库备份并完成恢复评估；代码更新不能替代现场恢复，也不能触发自动重试。

自动快进的信任边界是允许列表中的 `origin/main` 维护者和审查流程。静态检查不能证明远端源码无恶意，因此功能分支必须先运行测试并通过 Draft PR 审查后才能进入 `main`；更新器不会用当前用户的文件、网络和 Git 凭据执行刚下载的测试。

网络、DNS、TLS、认证、origin 不匹配、脏工作区、历史分叉、远端改写或验证失败时，自动更新必须停止。不得自动 stash、reset、rebase、改 remote、绕过证书或覆盖本地文件。
