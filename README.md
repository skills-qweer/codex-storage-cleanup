# Codex Storage Cleanup

一个面向 Windows 的 Codex 存储审计与安全清理技能。它用于解释 `CodexHome` 为什么占用大量磁盘空间，并在保留主对话、归档对话和用户产物的前提下，清理经过验证的缓存、旧日志、安装残留、SQLite 空闲页，以及明确已经结束的子代理记录。

仓库地址：<https://github.com/skills-qweer/codex-storage-cleanup>

## 能做什么

- 只读统计 `CodexHome` 总占用、最大文件、主对话、归档对话、用户产物和候选清理项。
- 在线安全清理已损坏的 LibreOffice 备份和已确认安装完成的安装包。
- 离线安全清理插件市场 staging、可重建缓存和旧的 sandbox 日志。
- 离线维护 Codex SQLite 数据库：先在 `CodexHome` 外备份，再执行 checkpoint、`VACUUM` 和 `quick_check`。
- 识别并清理已经结束的子代理：保护当前主对话和运行中的代理，先生成清单和临时数据库快照，再通过官方 `codex delete` / `thread/delete` 先单个验证、后分批删除。
- 用机器可读兼容矩阵核对子代理删除所使用的 CLI、数据库 migration、对象指纹、canary 原始错误和备份有效性，避免把旧修复误用到新版本。
- 检测技能兼容规则是否过期；可信上游已有更新时可验证后自动快进本地技能，未知组合则停删并通过功能分支和 Draft PR 更新仓库。
- 输出清理前后字节数、已删除项、保留项、失败项和仍需离线处理的内容。

### 默认保护范围

本技能不会自动删除以下内容：

- `sessions` 和 `archived_sessions` 中的主对话或归档对话；
- `generated_images`、`visualizations`、`attachments` 等用户产物；
- memories、凭据、配置、已安装的技能、插件、packages、tools 和 runtime；
- 当前主对话、运行中的子代理，以及状态不明确的代理记录。

> 子代理清理包含在技能中，但 `subagent_inventory.py` 本身只生成只读清单。真正删除前必须重新核对运行状态，并临时备份三个索引数据库；默认不会复制全部 rollout。删除只走 Codex 官方删除路径，不会直接手工删除 JSONL。遇到数据库结构不匹配、锁定或部分删除时会停止；只有确认是此前同一种 `agent_jobs` 版本缺陷后，才允许在备份后复现已验证的临时兼容处理。

## 环境要求

- Windows 和 PowerShell；
- Python 3（数据库和子代理脚本只使用 Python 标准库）；
- 已安装 Codex CLI；只有实际删除子代理时需要 `codex delete`；
- Git；只有检查或更新技能源码时需要联网，创建 Draft PR 还需要 GitHub CLI 和仓库写入权限；
- 默认存储目录为 `D:\CodexHome`，也可以通过参数传入其他绝对路径。

## 安装

将源码克隆到本地技能项目目录：

```powershell
git clone https://github.com/skills-qweer/codex-storage-cleanup.git 'E:\skills\codex-storage-cleanup'
```

建议用 Junction 将源码目录挂载到 Codex 的技能目录，这样更新仓库后无需重复复制：

```powershell
New-Item -ItemType Junction `
  -Path 'D:\CodexHome\skills\codex-storage-cleanup' `
  -Target 'E:\skills\codex-storage-cleanup'
```

如果你的 `CodexHome` 不在 `D:\CodexHome`，请相应修改 Junction 路径。安装后新建一个 Codex 任务，让技能列表重新加载。

## 在 Codex 中使用

在 Codex 输入框中直接点名技能，并说明允许处理的范围。以下示例都可以直接使用：

```text
$codex-storage-cleanup 检查 D:\CodexHome 为什么这么大，只读审计，不要删除任何内容。
```

```text
$codex-storage-cleanup 清理可以安全删除的缓存和旧日志，不要处理主对话、归档对话或用户产物。
```

```text
$codex-storage-cleanup 清理已经结束的子代理，保留全部主对话、归档对话、当前任务和所有运行中的代理；先给我候选数量与预计释放空间，再执行。
```

```text
$codex-storage-cleanup 检查 SQLite 可回收空间，并为完全退出 Codex 后的离线维护准备命令和外部备份位置。
```

```text
$codex-storage-cleanup 检查子代理删除兼容规则是否过时；如果可信上游已有更新就安全更新本地技能，如果需要新增规则则补齐测试并更新仓库的 Draft PR，不要自动合并。
```

技能默认先审计。如果要执行删除，请明确授权具体类别；子代理清理和数据库维护属于独立的高风险步骤，不会因为授权清理缓存而自动执行。

## 直接运行脚本

以下命令均从本仓库根目录运行。未带执行开关时只读，不会删除文件。

### 1. 审计整体占用

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File scripts\audit_storage.ps1 `
  -CodexHome 'D:\CodexHome'
```

可用 `-TopFiles 50` 调整最大文件的显示数量。

### 2. 在线安全清理

先预览候选项：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File scripts\cleanup_storage.ps1 `
  -Phase OnlineSafe `
  -CodexHome 'D:\CodexHome'
```

确认输出中的绝对路径、候选数量和字节数后执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File scripts\cleanup_storage.ps1 `
  -Phase OnlineSafe `
  -CodexHome 'D:\CodexHome' `
  -Execute `
  -ConfirmToken CLEAN_CODEX_STORAGE
```

如果相关 LibreOffice 进程仍在运行，脚本会拒绝执行。

### 3. 离线安全清理

离线阶段处理插件 staging、可重建缓存和旧 sandbox 日志。可以先在 Codex 中预览：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File scripts\cleanup_storage.ps1 `
  -Phase OfflineSafe `
  -CodexHome 'D:\CodexHome'
```

执行前完全退出 Codex、ChatGPT，以及可能仍在运行 Codex 的 VS Code 扩展，然后从外部 PowerShell 运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File 'E:\skills\codex-storage-cleanup\scripts\cleanup_storage.ps1' `
  -Phase OfflineSafe `
  -CodexHome 'D:\CodexHome' `
  -Execute `
  -ConfirmToken CLEAN_CODEX_STORAGE
```

检测到 `ChatGPT.exe` 或 `codex.exe` 时，执行会被阻止。

### 4. SQLite 审计和维护

只读查看数据库大小、WAL 和内部空闲页：

```powershell
python scripts\maintain_sqlite.py `
  --codex-home 'D:\CodexHome' `
  --database logs_2.sqlite
```

支持的数据库为 `goals_1.sqlite`、`logs_2.sqlite`、`memories_1.sqlite` 和 `state_5.sqlite`。执行维护时必须完全退出 Codex，并把备份目录放在 `CodexHome` 外：

```powershell
python 'E:\skills\codex-storage-cleanup\scripts\maintain_sqlite.py' `
  --codex-home 'D:\CodexHome' `
  --database logs_2.sqlite `
  --execute `
  --backup-dir 'C:\Codex-maintenance-backups' `
  --confirm-token MAINTAIN_CODEX_SQLITE
```

### 5. 已结束子代理清理

先生成只读清单，并把当前主任务和每个运行中子代理的 ID 都加入 `--protect`：

```powershell
python scripts\subagent_inventory.py `
  --codex-home 'D:\CodexHome' `
  --protect CURRENT_THREAD_ID `
  --protect ACTIVE_SUBAGENT_ID `
  --output 'C:\Codex-cleanup-manifests\subagent-manifest.json'
```

该脚本只纳入有明确 spawn 关系、代理元数据或 rollout 标记的记录；证据不足的记录会被排除。清单生成后，技能按前面真实清理时采用的顺序处理：

1. 重新查询当前任务和运行中的代理，更新保护 ID；
2. 核对候选根任务、子树数量、精确字节数和最近修改时间；
3. 通过 SQLite 在线备份 API，将 `state_5.sqlite`、`goals_1.sqlite` 和 `memories_1.sqlite` 临时备份到 `CodexHome` 外；不复制全部 rollout；
4. 先运行只读兼容诊断，再对一个候选根任务执行 `codex delete --force <root-thread-id>`；兼容诊断不能代替 canary；
5. canary 失败时保存未经改写的 CLI/app-server 报错和部分删除状态，再把故障证据与刚生成的备份摘要交给诊断器；
6. 大批量时通过本机 `codex app-server --stdio` 的 `thread/delete`，逐个根任务删除；每个根任务执行前都复核实时子树、保护 ID 和清单生成后的文件修改；
7. 验证受保护任务仍可打开、候选文件和索引记录已经消失、各数据库 `quick_check` 为 `ok`，再继续下一批；
8. 锁定、RPC/超时、报错指纹不同、保护范围变化或任何新的部分删除都立即停止；只有诊断结果精确为 `known_workaround_eligible`，并再次获得本次授权后，才允许安装已验证的临时兼容对象；
9. 全部验证通过后移除本次临时兼容对象，再删除临时数据库备份和辅助程序，只保留体积很小的清单与结果日志，从而得到实际净释放空间。

不要逐个删除同一子树的所有后代，也不要把闲置主对话或归档对话误判为子代理。更推荐让技能完成实时核对、canary 和结果验证，而不是手工批量执行删除命令。

#### 删除兼容性：版本正确也不能跳过检查

版本号本身不能证明“有问题”或“已修复”。可靠判定需要同时核对：

- `codex --version` 的精确版本；
- `state_5.sqlite` 的最大成功 migration；
- 相关 migration 的描述、成功标记和校验和；
- `agent_jobs`、`agent_job_items` 和两个索引的存在状态与 DDL 指纹；
- 官方 canary 的原始错误、rollout/线程行/spawn edge 的部分删除形态；
- 三个外部在线备份的时间、路径、大小、SHA-256 和实时 `quick_check`。

先运行只读诊断：

```powershell
python scripts\subagent_delete_compat.py diagnose `
  --codex-home 'D:\CodexHome'
```

canary 失败后，把当次原始证据和新鲜备份摘要一并传入：

```powershell
python scripts\subagent_delete_compat.py diagnose `
  --codex-home 'D:\CodexHome' `
  --failure-evidence 'C:\Codex-cleanup\canary-failure.json' `
  --backup-summary 'C:\Codex-cleanup\db-backup-summary.json' `
  --output 'C:\Codex-cleanup\compat-diagnosis.json'
```

诊断结果的含义：

| 结果 | 含义和处理 |
| --- | --- |
| `canary_required` | 还没有精确 canary 失败；只运行一个官方 canary，不能预先建表 |
| `known_workaround_eligible` | 所有已验证指纹和新鲜备份均命中；仍需本次明确授权才能安装临时对象 |
| `compat_installed` | 临时对象已存在且结构精确、两表为空；不能重复安装，完成后必须凭本次安装结果移除 |
| `stale_profile_update_required` | 兼容 profile 超过复核日期；停删并先更新或重新验证技能 |
| `unsupported_update_required` | CLI/schema 组合不在矩阵中；停删，检查可信更新或创建 Draft PR 补支持 |
| `unsafe_stop` | 错误不同、对象部分存在/非空、备份或数据库健康失败等；保留现场，禁止套用旧方案 |

当前唯一已验证的临时方案只覆盖 `codex 0.142.2 + state migration 42 + 精确的 no such table: agent_jobs canary 指纹`。它不是“只要缺 `agent_jobs` 就建表”的通用修复。不同 CLI、不同 migration 最大值/校验和、缺其他表、已有部分对象、表内有数据、锁/I/O/超时、`quick_check` 非 `ok` 或 canary 再次失败时都必须停下。

`install` 和 `remove` 默认只输出计划。真正执行还要求命令打印的固定确认口令、位于 `CodexHome` 外的新审计文件，以及针对当次操作的明确授权。执行只接受干净、已审查且 `main == origin/main` 的本仓库内置 profile，以及真实的 `CodexHome\state_5.sqlite`；安装先写 `prepared` 审计记录，在事务内复核实时 canary 的线程行、精确 spawn-edge 集合和 rollout 状态，创建 2 张空表和 2 个索引后再写入 `commit-pending` 状态，随后才提交事务。它不改 `_sqlx_migrations`，也不调用删除接口。故障证据最多使用 24 小时；自动移除也限定在安装后 24 小时内，并核对当前 CLI、run nonce、数据库文件身份、profile/证据/备份哈希、SQLite `schema_version`、4 个对象的 `rootpage`/DDL 指纹和空表状态。结果缺失、被修改或超时就停止人工评估。完整矩阵、限制和官方源码依据见 [`references/subagent-delete-compatibility.md`](references/subagent-delete-compatibility.md)。

#### “备份”会不会抵消释放空间？

不会要求把全部待删子代理原样复制一份到同一块磁盘。前面真实执行时采用的最小安全集是：

- 一份体积很小的候选清单；
- `state_5.sqlite`、`goals_1.sqlite` 和 `memories_1.sqlite` 的临时在线快照；
- 每一批的逐项结果日志。

这些临时数据库快照应放在 `CodexHome` 外，最好放到另一块磁盘或移动硬盘。它们用于恢复索引数据库，不包含全部子代理正文，也不会等同于待删除的几十 GiB rollout。canary 和批量结果都验证正常后，技能会按清理授权删除临时快照和辅助程序；最终只保留清单和结果日志。

默认不会为所有批量候选保存完整副本，因此成功删除并清除临时数据库快照后，那些子代理的详细过程将不可恢复。如果确实需要整批可回滚，应先压缩所有候选 rollout 到另一块磁盘；这种模式会暂时占用额外空间，最终报告必须扣除仍保留备份的大小，给出净释放空间。

#### 2026-08-01 实际清理记录

前面这次清理的真实流程和结果如下，可作为技能重复执行时的基准：

- 清单识别出 861 个子代理、804 个候选根任务，共 60,386,024,139 字节（56.239 GiB）；
- 保护了 2 个正在运行的主任务，并确认当时没有运行中的子代理；
- 只临时备份了三个索引数据库，没有复制 56.239 GiB 的 rollout；当次临时生成的候选 CSV 为 282,607 字节（当前仓库的 `subagent_inventory.py --output` 使用 JSON，作用相同但格式不同）；
- 先用官方删除路径完成 1 个根任务 canary，再通过 `thread/delete` 分批处理其余根任务；
- 每个根任务删除前都复核实时子树、保护 ID 和文件修改时间；
- 完成后 `subagents = 0`、候选文件剩余 0，`state_5.sqlite`、`goals_1.sqlite` 和 `memories_1.sqlite` 的 `quick_check` 均为 `ok`，两个受保护主任务仍可读取；
- 当时的组合是 `codex 0.142.2` 与最大成功 migration 为 42 的状态库：CLI 的删除实现仍访问 `agent_jobs`，而 migration 42 已删除这些表。精确错误和 migration 校验和命中后，才在备份后临时创建空兼容对象；批量删除完成后已移除兼容对象、临时数据库备份和辅助程序，只保留清单与结果日志。这个历史结果不是其他版本的永久兼容保证。

### 6. 检查和更新技能

兼容矩阵带有 `review_after`。当前 profile 的复核截止日是 `2026-11-01`；到期、出现新 CLI、未知 migration/对象指纹、不同删除报错或官方实现变化时，现场删除会停止并提示更新技能。

只读检查本地仓库和可信 `origin/main`：

```powershell
python scripts\refresh_skill.py check `
  --output 'C:\Codex-cleanup\skill-update-check.json'
```

如果检查由 canary 故障触发，应传入事故证据：

```powershell
python scripts\refresh_skill.py check `
  --incident-evidence 'C:\Codex-cleanup\canary-failure.json'
```

只要证据显示 rollout 已部分删除、批量已经开始或兼容对象已经安装，更新器就拒绝在事故处理中刷新源码。先保全现场、备份和恢复证据；更新代码不会自动恢复已删除内容，也不会自动重试 canary。

如果只读兼容诊断返回 `stale_profile_update_required` 或 `unsupported_update_required`，把诊断输出交给更新检查：

```powershell
python scripts\refresh_skill.py check `
  --compat-diagnosis 'C:\Codex-cleanup\compat-diagnosis.json'
```

当 `origin/main` 没有更高版本时，这会返回 `draft_pr_required`，不会误报为普通的 `up_to_date`。

当结果为 `trusted_fast_forward_available`，并且用户明确授权更新本地技能时：

```powershell
python scripts\refresh_skill.py apply `
  --confirm-no-active-cleanup `
  --execute `
  --confirm-token UPDATE_CODEX_STORAGE_SKILL `
  --output 'C:\Codex-cleanup\skill-update-result.json'
```

这个自动更新只更新本地技能源码，不更新 Codex CLI。执行条件全部满足时才会工作：

- 仓库完全干净，包括 tracked、staged 和 untracked 文件；
- 当前位于跟踪 `origin/main` 的 `main`，且没有 merge/rebase/cherry-pick 等未完成操作；
- origin 规范化后精确为本仓库，网络/TLS/认证正常；
- 远端 `main` 是本地 HEAD 的后代，只允许 `git merge --ff-only`；
- 先在临时 worktree 静态验证 SKILL front matter、兼容 JSON、Python 编译和 PowerShell 语法；不会执行刚下载的测试代码；
- 合并前再次核对 HEAD、工作区、origin、事故证据，以及本地 `origin/main` 是否仍等于刚验证的远端提交；只按这个确切 SHA 快进，并在本地静态复验。

没有事故证据的一般更新必须明确传入 `--confirm-no-active-cleanup`。由未知 CLI/schema 触发时，应先把兼容诊断保存成 JSON，再用 `--compat-diagnosis` 传给 `check`；如果远端没有对应更新，结果会明确变成 `draft_pr_required`。未知格式的事故或诊断 JSON 一律阻止自动更新。

更新器绝不会自动 stash、reset、rebase、force-push、改 remote、绕过证书、覆盖本地文件、打开 Codex 数据库、调用删除接口、升级 Codex CLI、推送分支或合并 PR。自动快进仍信任允许列表中 `origin/main` 的维护者与审查流程；静态检查不能证明源码绝对无恶意，所以更新器不会使用当前用户的文件、网络和 Git 凭据运行刚拉取的测试代码。新兼容代码必须在功能分支上先运行完整测试并经 Draft PR 审查，才能进入 `main`。

如果本地 profile 已过期，而可信 `origin/main` 没有更新，或新 CLI/schema 仍未收录，结果会要求 Draft PR。获得仓库更新授权后，Codex 可以自动收集脱敏后的第一方证据，在最新 `origin/main` 上创建功能分支，补 profile、fixture、脚本、文档和测试，验证后推送该分支并创建 Draft PR。它不会直接写 `main`、自动合并、force-push，也不会根据未知数据库猜 DDL。认证或网络失败时只保留本地报告，不留下半完成的远端变更。

更新完成后必须重新运行只读兼容诊断和新的单个 canary；不能自动续跑上次失败的删除。

## 推荐工作流

1. 先运行整体只读审计，记录 `CodexHome` 当前物理大小；
2. 检查候选项的绝对路径、类别和精确字节数；
3. 只授权本次需要处理的类别；
4. 先完成 OnlineSafe，再准备需要从外部 PowerShell 执行的 OfflineSafe 或 SQLite 命令；
5. 子代理清理单独确认，先做兼容诊断，并在每批删除前重新核对活动状态；
6. profile 过期或组合未知时先停删，再按“可信快进”或“功能分支 + Draft PR”更新技能；
7. 清理后复测目录大小、数据库健康状态和受保护任务；验证通过后清除临时数据库备份与辅助程序，只保留清单和结果日志，并报告净释放空间。

文件属性中的“大小”和“占用空间”可能不同；最终报告应同时说明逻辑字节数、实际释放空间，以及仍由主对话、归档对话或用户产物占用但未处理的部分。

## 仓库结构

```text
codex-storage-cleanup/
├─ SKILL.md                         # Codex 执行规则与安全流程
├─ agents/openai.yaml               # 技能在 Codex 中的名称和默认提示
├─ references/
│  ├─ subagent-delete-compatibility.json  # 机器可读兼容指纹和 DDL 哈希
│  └─ subagent-delete-compatibility.md    # 决策矩阵、限制和更新生命周期
├─ scripts/
   ├─ audit_storage.ps1             # 只读空间审计
   ├─ cleanup_storage.ps1           # OnlineSafe / OfflineSafe 清理
   ├─ maintain_sqlite.py            # SQLite 审计、备份和维护
   ├─ subagent_inventory.py         # 已结束子代理只读清单
   ├─ subagent_delete_compat.py      # 删除兼容诊断及临时对象生命周期
   └─ refresh_skill.py               # 可信本地 fast-forward 更新器
└─ tests/                            # 兼容矩阵、安装/移除和更新安全测试
```
