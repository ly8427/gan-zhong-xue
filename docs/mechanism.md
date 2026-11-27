# 机制层设计与验收说明（v1.2.0）

> 原收纳于 `SKILL.md` 的机制验收参数与边界论证迁移至此，保持运行时 Skill 指令精简聚焦。

## 1. 数据面不变量（Single-Writer & Append-Only）

- **唯一写入口**：
  - `skills/gan-zhong-xue/gzx_lock_append.py`：提供基于文件排他锁（Linux/macOS `fcntl.flock`、Windows `msvcrt.locking`）的只追加写入 `lock_append`、双地图同锁原子追加 `lock_append_map_pair`、原子提交去重 `lock_append_commit_unique`，以及**仅限 `pending.md`** 的 `lock_truncate`。
  - `skills/gan-zhong-xue/gzx_cli.py`：聚合 CLI，将初始化、`preflight`、双地图校验与同锁写入收口到单次调用，减少模型手搓多条命令与转义失误。
  - `skills/gan-zhong-xue/gzx-append.sh`：Bash 写入器入口（优先 `flock(1)`，无 `flock(1)` 时回落 `gzx_lock_append.py`；四态退出码 `0/21/22/23`）。
- **单一事实来源与发散检测（D2）**：
  - `gzx_cli.py save-cell` 在同一把排他锁内同时追加 `map.jsonl` 与渲染后的 `map.md`，避免两次独立加锁导致的静默发散。
  - `gzx_cli.py check-divergence`（及 `preflight`）比对 `map.md` `^## ` 格数与 `map.jsonl` 行数，发现不一致时显式报错并输出两侧计数。

## 2. 兜底 Hook 定位（Claude Code）

- **`hooks/gzx-guard.py`（PreToolUse）**：
  - 定位为**减速带 + 审计**，而非安全沙箱。
  - 范围条件收窄为明确引用运行时数据目录（`.gan-zhong-xue` / `GZX_DIR`）的调用；常规仓内开发、`git commit`、文档 `Write`/`Edit` 及不含数据目录引用的 `Agent` 提示词直接放行，避免误杀。
  - 放行官方 `gzx-append.sh`、`gzx_cli.py`（含 `save-cell` / `pending-add` heredoc）、`cp`/`mkdir`/`chmod 700` 引导安装及只读前缀白名单。
  - 审计只记录 `{ts, tool, decision, rule}` 元数据，首行保证写入 `hook_alive` 活体事件。
- **`hooks/gzx-capture.py`（PostToolUse）**：
  - 静默捕获真正成功的 `git commit`（输出匹配成功标记 `[... <sha>]`，支持 `root-commit` 与 `detached HEAD`，并跟随命令行 `cd` / `env -C` / `git -C` / `--git-dir` 定位目标仓库 `HEAD` 复核）。
  - 通过 `lock_append_commit_unique` 以 `(sha, event)` 在锁内原子去重并计算单调 `seq`，不写仓库名与文件路径。

## 3. 三阶段验收口径

1. **安装验收**：
   - `python3 hooks/gzx-guard.py --selftest` 全绿（allow + deny 双侧）。
   - `python3 hooks/test-gzx-invariants.py` 与 `python3 hooks/test-gzx-integration.py` 全绿。
   - `~/.gan-zhong-xue/audit.jsonl` 首行含 `hook_alive`。
2. **运行健康度**：`candidates.jsonl` 与 `audit.jsonl` 正常推进、无命令原文泄漏、写入失败必带非零退出码与 stderr 状态行。
3. **已知残余边界**：
   - `cd` 到 `~/.gan-zhong-xue` 后不提路径名的破坏性操作仍拦不住（减速带定位）。
   - 宿主 hook 接线失败时 `gzx-guard.py` 采用 fail-open，不冻结用户会话。
   - `hooks/matcher.json` 仍保留待实测标记，跨宿主接线能力以实际运行验证为准。
