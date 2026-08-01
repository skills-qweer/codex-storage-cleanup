# Codex Storage Cleanup

一个面向 Windows 的 Codex 存储审计与安全清理技能。它用于解释 `CodexHome` 为什么占用大量磁盘空间，并在保留主对话、归档对话和用户产物的前提下，清理经过验证的缓存、旧日志、安装残留、SQLite 空闲页，以及明确已经结束的子代理记录。

仓库地址：<https://github.com/skills-qweer/codex-storage-cleanup>

## 能做什么

- 只读统计 `CodexHome` 总占用、最大文件、主对话、归档对话、用户产物和候选清理项。
- 在线安全清理已损坏的 LibreOffice 备份和已确认安装完成的安装包。
- 离线安全清理插件市场 staging、可重建缓存和旧的 sandbox 日志。
- 离线维护 Codex SQLite 数据库：先在 `CodexHome` 外备份，再执行 checkpoint、`VACUUM` 和 `quick_check`。
- 识别并清理已经结束的子代理：保护当前主对话和运行中的代理，先生成清单和临时数据库快照，再通过官方 `codex delete` / `thread/delete` 先单个验证、后分批删除。
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
4. 先对一个候选根任务执行 `codex delete --force <root-thread-id>`；
5. 大批量时通过本机 `codex app-server --stdio` 的 `thread/delete`，逐个根任务删除；每个根任务执行前都复核实时子树、保护 ID 和清单生成后的文件修改；
6. 验证受保护任务仍可打开、候选文件和索引记录已经消失、各数据库 `quick_check` 为 `ok`，再继续下一批；
7. 如果输出出现 `agent_jobs`、缺表、结构不匹配、文件锁定、RPC 失败或部分删除，立即停止；其中只有已确认与此前相同的 `agent_jobs` 版本缺陷，才可在数据库备份后使用临时空兼容表，完成后立即移除并再次检查数据库；
8. 全部验证通过后，删除临时数据库备份和辅助程序，只保留体积很小的清单与结果日志，从而得到实际净释放空间。

不要逐个删除同一子树的所有后代，也不要把闲置主对话或归档对话误判为子代理。更推荐让技能完成实时核对、canary 和结果验证，而不是手工批量执行删除命令。

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
- 当时的旧版 CLI 存在已确认的 `agent_jobs` 删除兼容问题，因此备份后临时创建了空兼容表；批量删除完成后已移除兼容表、临时数据库备份和辅助程序，只保留清单与结果日志。

## 推荐工作流

1. 先运行整体只读审计，记录 `CodexHome` 当前物理大小；
2. 检查候选项的绝对路径、类别和精确字节数；
3. 只授权本次需要处理的类别；
4. 先完成 OnlineSafe，再准备需要从外部 PowerShell 执行的 OfflineSafe 或 SQLite 命令；
5. 子代理清理单独确认，并在每批删除前重新核对活动状态；
6. 清理后复测目录大小、数据库健康状态和受保护任务；验证通过后清除临时数据库备份与辅助程序，只保留清单和结果日志，并报告净释放空间。

文件属性中的“大小”和“占用空间”可能不同；最终报告应同时说明逻辑字节数、实际释放空间，以及仍由主对话、归档对话或用户产物占用但未处理的部分。

## 仓库结构

```text
codex-storage-cleanup/
├─ SKILL.md                         # Codex 执行规则与安全流程
├─ agents/openai.yaml               # 技能在 Codex 中的名称和默认提示
└─ scripts/
   ├─ audit_storage.ps1             # 只读空间审计
   ├─ cleanup_storage.ps1           # OnlineSafe / OfflineSafe 清理
   ├─ maintain_sqlite.py            # SQLite 审计、备份和维护
   └─ subagent_inventory.py         # 已结束子代理只读清单
```
