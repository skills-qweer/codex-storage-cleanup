# 子代理删除兼容性判定

本说明描述两条彼此隔离的官方删除路径：

- 正常路径直接使用当前桌面应用实际运行、且 Authenticode 签名有效的 `app-server`；
- legacy 回退只处理已经复现并独立审查过的 `codex 0.142.2` / `agent_jobs` 缺表事故。

[`subagent-delete-compatibility.json`](subagent-delete-compatibility.json) 和 [`subagent-delete-tail-certificates.json`](subagent-delete-tail-certificates.json) 是冻结的 legacy 事故恢复配置；其中历史 native 字段保留给旧审计格式校验，不再控制日常预检。实际判定由 `scripts/subagent_delete_compat.py` 完成，不能只看本文、版本号、路径名或 migration 最大值。

## 为什么旧流程反复失效

桌面 Codex 会频繁更新，并由它自己的 bundled backend 打开和迁移 `state_5.sqlite`。如果定时任务随后调用 PATH 上较旧的独立 CLI，就可能出现“新 backend 写库、旧 CLI 删任务”的跨代组合。

已确认的 legacy 冲突是：

- [`0.142.2` 的严格删除实现](https://github.com/openai/codex/blob/rust-v0.142.2/codex-rs/state/src/runtime/threads.rs#L967-L1072)仍会更新 `agent_jobs` 和 `agent_job_items`；
- [migration 42](https://github.com/openai/codex/commit/687f05cb946d10c96f90dd7ce82e11465c6e20a7)删除了这两张表，并同步移除了新删除实现对它们的访问；
- 旧 CLI 因而可能在 rollout 已清理后报 `no such table: agent_jobs`，留下线程行和 spawn edge，形成部分删除。

较新的已修复删除实现不再访问这两张表，例如 [`0.146.0-alpha.9.2`](https://github.com/openai/codex/blob/rust-v0.146.0-alpha.9.2/codex-rs/state/src/runtime/threads.rs#L1042-L1090)。因此长期方案的首选不是不断给旧 CLI 追加 migration 最大值，而是严格配对桌面应用当前正在运行的官方 backend。

## 轻量 preflight

任何 rollout 全量扫描或数据库备份之前，先运行：

```powershell
New-Item -ItemType Directory -Force 'C:\Codex-cleanup' | Out-Null
python scripts\subagent_delete_compat.py preflight `
  --codex-home 'D:\CodexHome' `
  --output 'C:\Codex-cleanup\runtime-preflight.json'
```

preflight 读取实时进程、桌面父进程/后台的签名、实际后台文件身份和少量 SQLite 结构证据。它在隔离临时目录调用该后台的 `app-server generate-json-schema --experimental`，检查本清理器实际发送的请求结构，不启动真实删除探针。不会执行 `quick_check`、扫描全部 rollout 或创建数据库备份。

只有同时满足以下条件，结果才会给出 `allow_expensive_inventory: true`：

- 唯一候选是由签名有效的 OpenAI 桌面程序启动的 `app-server`，不限定安装目录或桌面程序文件名；
- 直接使用该进程的可执行文件，文件不是 reparse point，且 Authenticode 签名为有效的 `OpenAI OpCo, LLC`；
- 后台导出的接口仍支持清理器实际使用的 `initialize` 和 `thread/delete(threadId)` 请求；允许新增可选参数，拒绝缺少方法、改名参数或新增未满足的必需参数；
- `threads` 仍具有 `id / rollout_path / updated_at / archived`，`thread_spawn_edges` 仍具有 `parent_thread_id / child_thread_id / status`；允许额外字段和表；
- 没有失败 migration；不要求历史 migration 编号/校验和，也不限制最大值；
- 排除已知早于原生删除修复的 `<0.145.0` 后台，其余版本由实际能力判断，没有版本上限或 native 复核截止日期；
- 四个临时兼容对象全不存在。

正常 native 路径不按“未知尾 migration”停机，因为所选运行时就是当前桌面后台；仍必须完成新鲜备份和一个真实官方 canary。preflight schema v3 的 `condition_key` 绑定进程身份、后台文件和当前 schema/迁移账本，只在本批内比较，不用于禁止下一批采用新版本。旧 v2 证据需重新盘点，不能直接续跑。

## 决策矩阵

| 实时条件 | 判定 | 允许动作 |
| --- | --- | --- |
| 当前签名有效的桌面 backend、所需接口和数据库字段兼容 | `canary_required` | 可开始活动状态盘点、外部备份，并只用 `recommended_codex_exe` 做 canary；不代表已经验证真实删除 |
| writer-lock 或精确 active-writer 拒绝且现场未变 | 跳过当前根 | 继续其他独立根，下一次成功删除才算 canary |
| native canary 未知错误、超时或部分删除 | `unsafe_stop` | 保存原始错误并停止整批；绝不能转用 legacy shim，也不能自动重试 |
| legacy `0.142.2`、锚点和精确已审 43/44 尾链命中，但没有既有 canary 失败证据 | `unsupported_update_required` | 该路径仅用于恢复既有事故；返回 native preflight，不能用旧 CLI 新开 canary |
| legacy 精确缺表错误、部分删除状态、实时 canary 状态和新鲜外部备份全部命中 | `known_workaround_eligible` | 仅凭本次授权和脚本固定口令临时安装四个对象，然后只重试同一个 canary |
| 四个兼容对象全部精确存在、为空，且有同一事故的匹配 install journal | `compat_installed` | 不得重装；完成同一批次后按匹配 journal 移除 |
| legacy profile 或尾证书超过 `review_after` | `stale_profile_update_required` | 仅旧事故恢复需重新审查；不影响日常 native 预检 |
| runtime 身份、签名、必需接口/字段不兼容，或 legacy 尾证书不匹配 | `unsupported_update_required` | 不做昂贵盘点/备份；按具体错误维护，不因普通升级改规则 |
| 对象部分存在、账本/schema 漂移、证据失效、备份/`quick_check` 失败 | `unsafe_stop` | 保留现场，禁止自动修库、自动重试或批量删除 |

## legacy 尾迁移证书

当前唯一证书是 `codex-state-42-to-44-delete-unrelated-v1`，它绑定固定删除 contract，并只接受完整连续链：

- [migration 43](https://github.com/openai/codex/blob/400ee190c30d5e4a88549c070a2335311f0baa91/codex-rs/state/migrations/0043_threads_is_pinned.sql)：精确 description、SHA-384 和不可变 40 位 commit；
- [migration 44](https://github.com/openai/codex/blob/ce803c45aed425b08b94d8e3c5fb7db0d2193568/codex-rs/state/migrations/0044_external_agent_config_imports_provider_id.sql)：同样使用精确证据。

证书不接受范围、通配符、mutable `main` URL、description-only 匹配、自我声明“已审”或不同 chain 的拼接。legacy 路径出现 migration 45、缺口、错误 checksum、失败行或证书漂移都会停止。新增尾 migration 必须先做独立审查并通过 Draft PR；不能临场按 regex 猜测 SQL 是否安全。

## legacy 临时对象生命周期

1. `diagnose` 默认只读；故障证据必须绑定同一个 canary UUID、`-32603` app-server code、唯一的 `agent_jobs` 缺表错误和实时部分删除状态。
2. `install` 默认只输出计划。执行要求固定口令、新审计输出、干净且 `main == origin/main` 的可信仓库，以及同一事故的新鲜备份。
3. install journal 记录实际 migration 最大值、完整 migration ledger hash、semantic contract hash、排除四个 shim 对象后的基础 schema hash、证书 hash、数据库身份、profile/故障/备份 hash、run nonce、`schema_version` 和 rootpage。
4. `BEGIN IMMEDIATE` 内会重新验证所有 hash 和 canary 状态；创建对象后再次确认 ledger 和基础 schema 未变，再提交。脚本不修改 `_sqlx_migrations`。
5. 安装后只可通过官方路径重试同一个 canary。结果不同或第二次失败立即停止。
6. `remove` 只能在 24 小时窗口内使用同一 install journal；完整 ledger、基础 schema、证据、rootpage、空表状态或 CLI 任一漂移都会拒绝移除。
7. 移除后再次确认临时对象全无、ledger 和基础 schema 未变且 `quick_check = ok`。外部备份、manifest 和审计日志不由该脚本删除。

## 更新与定时任务

legacy 恢复配置的复核截止日为 `2026-11-01`，不适用于日常 native 清理。`refresh_skill.py` 只允许可信仓库的精确 fast-forward，并保留冻结 legacy 配置/证书的静态校验；单凭 legacy 到期不再提示整个技能必须更新。它不更新 Codex CLI、不打开 Codex 数据库、不调用删除 API，也不自动合并 Draft PR。

脚本不会自行创建、暂停或恢复定时任务。是否自动运行遵循用户当前选择；普通 Codex 升级只需下一批重新预检，无需重新授权。若另行配置监控，相同不可操作条件应去重，不重复扫描和备份。

日常 root-local 跳过和 batch-global 停止规则以 `SKILL.md` 为准。legacy 写入仍保留仓库干净、精确证据和复核窗口等事故级门禁，不能扩散到普通清理。
