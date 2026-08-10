# Codex Storage Cleanup

Windows 下的 CodexHome 存储审计与清理技能。它可以统计占用、清理明确可重建的数据、维护 SQLite 空闲页，并删除已经结束且不再使用的子代理记录。

仓库：<https://github.com/skills-qweer/codex-storage-cleanup>

## 不会自动删除

- 主对话和归档对话；
- 当前活动、中断、状态未知、最近变化或仍可能复用的任务；
- `generated_images`、`visualizations`、附件和 memories；
- 凭据、配置、已安装技能、插件、packages、tools 和 runtime。

## 安装

推荐把仓库放在固定位置，再让 Codex 技能目录指向它：

```powershell
git clone https://github.com/skills-qweer/codex-storage-cleanup.git E:\skills\codex-storage-cleanup
New-Item -ItemType Junction `
  -Path 'D:\CodexHome\skills\codex-storage-cleanup' `
  -Target 'E:\skills\codex-storage-cleanup'
```

当前机器上 `D:\CodexHome\skills\codex-storage-cleanup` 已是指向该仓库的 junction，因此 Git 更新会立即同步到本地技能。

## 常用命令

只读统计：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\audit_storage.ps1 -CodexHome 'D:\CodexHome'
```

在线安全项先生成计划，获得授权后再执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\cleanup_storage.ps1 -Phase OnlineSafe -CodexHome 'D:\CodexHome'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\cleanup_storage.ps1 -Phase OnlineSafe -CodexHome 'D:\CodexHome' -Execute -ConfirmToken CLEAN_CODEX_STORAGE
```

SQLite 维护必须完全退出 Codex，并把备份放到 CodexHome 外：

```powershell
python scripts\maintain_sqlite.py --codex-home 'D:\CodexHome' --database logs_2.sqlite
```

## 已结束子代理清理

正式入口是 `scripts/cleanup_completed_subagents.py`。日常 native 清理不再使用任务目录里的临时循环。

第一步：传入当前单调保护集合，做一次 matched-runtime preflight 和强证据盘点：

```powershell
python scripts\cleanup_completed_subagents.py inventory `
  --codex-home 'D:\CodexHome' `
  --run-dir 'C:\Codex-cleanup\run-YYYYMMDD-HHMMSS' `
  --protect CURRENT_TASK_ID
```

第二步：按 `status-targets.json`，每个任务单独调用一次 `wait_threads(timeoutMs: 0)`，最多并发八个调用，再把结果写入同一运行目录的 `status-evidence.json`。不要把八个任务塞进同一次 wait，也不要为普通状态判断加载完整历史。

第三步：在已有删除授权时直接运行：

```powershell
python scripts\cleanup_completed_subagents.py run `
  --codex-home 'D:\CodexHome' `
  --run-dir 'C:\Codex-cleanup\run-YYYYMMDD-HHMMSS' `
  --status-evidence 'C:\Codex-cleanup\run-YYYYMMDD-HHMMSS\status-evidence.json' `
  --protect CURRENT_TASK_ID
```

脚本会完成精确快照、外部 SQLite online backup、canary、同一 app-server 批量删除、逐根验证、可续跑结果日志和最终 `quick_check`。意外中断且没有完整性错误时，使用同一命令加 `--resume`。

### 隔离规则

以下只跳过当前根并继续：

- writer-lock；
- 精确 `-32600` / `already has an active writer`，且复核未发生部分删除；
- live status、保护交集、子树、行状态、rollout 路径/大小/mtime 变化；
- 根已被其他流程完整清除。

以下才停止整批：

- 部分删除；
- SQLite 异常或 `quick_check` 失败；
- 全局任务、协作代理或 host/source 状态不完整；
- 桌面 PID、`condition_key`、matched runtime、schema 或 migration 指纹变化；
- 超时或未知 RPC 错误。

批次开始和 canary 前运行完整 preflight；其余根只做轻量指纹与自身快照核对。保护集合只增不减，任务从 active 变 idle 不会触发停批，也不会从保护集合移除。

legacy `0.142.2` 只用于恢复已经存在且精确命中的旧部分删除事故，不进入普通清理。需要恢复时再读 [`references/subagent-delete-compatibility.md`](references/subagent-delete-compatibility.md)。

## 项目防跑偏机制

- `CURRENT_TASK.md`：唯一当前任务和验收标准；
- `COMPLETED_WORK.md`：只归档已完成且已验证的结果；
- `HANDOFF.md`：后续对话必须继承的仓库事实和规则；
- `AGENTS.md`：要求每次动作前对照当前任务，并在连续两轮没有推进验收标准时主动收窄方案。

废弃、过时和被替代的计划直接删除，不进入完成归档，也不作为后续任务来源。

## 验证

```powershell
python -m unittest discover -s tests -v
python D:\CodexHome\skills\.system\skill-creator\scripts\quick_validate.py E:\skills\codex-storage-cleanup
git diff --check
```

最终报告应给出实际删除根数、子代理数、跳过/失败数、释放字节、CodexHome 新占用、数据库验证和外部运行目录。备份、manifest 与审计结果不会被自动删除。
