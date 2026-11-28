# 机制层设计与验收说明（v1.2.0）

> 原收纳于 `SKILL.md` 的机制验收参数与边界论证迁移至此，保持运行时 Skill 指令精简聚焦。

## 1. 数据面不变量（Single-Writer & Append-Only）

- **唯一写入口**：
  - `skills/gan-zhong-xue/gzx_lock_append.py`：提供基于文件排他锁（Linux/macOS `fcntl.flock`、Windows `msvcrt.locking`）的单写者互斥追加写入 `lock_append`、双地图顺序追加 `lock_append_map_pair`（基于排他锁提供单写者互斥，不承诺跨文件崩溃原子事务；异常时输出告警）、原子提交去重 `lock_append_commit_unique`，以及**仅限 `pending.md`** 的 `lock_truncate`。
  - `skills/gan-zhong-xue/gzx_cli.py`：聚合 CLI，将初始化、`preflight`、双地图校验与同锁写入收口到单次调用，减少模型手搓多条命令与转义失误。
  - `skills/gan-zhong-xue/gzx-append.sh`：Bash 写入器入口（优先 `flock(1)`，无 `flock(1)` 时回落 `gzx_lock_append.py`；四态退出码 `0/21/22/23`）。
- **单一事实来源与发散检测（D2）**：
  - `gzx_cli.py save-cell` 在排他锁保护下顺序追加 `map.jsonl` 与渲染后的 `map.md`。为保证单一事实来源与可自愈性，`map.jsonl` 现已实现与 `map.md` 全字段模式对齐（含 `证据`、`顶点原理`、`用户更懂`），支持从 jsonl 无损重建 markdown。
  - `gzx_cli.py check-divergence`（及 `preflight`）做两级发散检测：
    - 计数级：`map.md` `^## ` 格数 vs `map.jsonl` 行数；
    - 字段级（重建 + diff）：jsonl 每行按 `(概念, 分层)` 与 md 格**按追加顺序逐位配对**，逐字段比对渲染面（正常格比对 证据/先猜/顶点原理/底部验证题/用户更懂/状态；⏸ 暂停格比对 来源/关联靶/关联节点/备注/状态），明细进 `cell_check.mismatches`；
    - 一致时返回 0；计数不匹配、字段不符（`field-mismatch`）或 md 孤立格（`orphan_md_cells > 0`）返回专用退出码 **24** 并输出两侧差异与从 `map.jsonl` 重建的指引；
    - 缺少依赖或文件不可读返回 21/22。
  - `gzx_cli.py cell`：同概念跨多个分层时返回 `ambiguous: true` 并以退出码 **25** 拒绝静默取格（`cell <概念> <分层>` 按 (概念, 分层) 精确取格），杜绝跨层 last-wins 取错验证题。

## 2. 兜底 Hook 定位（Claude Code）

- **`hooks/gzx-guard.py`（PreToolUse）**：
  - 定位为**减速带 + 审计**，而非安全沙箱。
  - 判定在分词之后进行（heredoc 正文先行剥离，正文是数据不是 shell）：**范围门** = 命令词元引用运行时数据目录（`.gan-zhong-xue` / `GZX_DIR`）∨ heredoc 正文含标记 ∨ 名字臂命中；常规仓内开发、`git commit`、文档 `Write`/`Edit` 及不含数据目录引用的 `Agent` 提示词直接放行，避免误杀。
  - **字面性闸**（与范围无关，进出范围两向都拦）：有效命令名、包装器（bash/sh/source/.）与解释器（python/python3）参数、输出重定向目标——出现 `$` 变量 / 命令替换 / `` ` `` / `*?[]{}` 等扩展算子 ⇒ 拒（规则 `expansion:unprovable`）。判据是"目标/命令名没有扩展算子"而非"展开后像不像受保护路径"（守卫不做展开模拟；$ 断片、字面前缀+$、通配符三族 bypass 实证）。**已知代价**：范围外的 `echo hi > $OUT`、`cmd > out $ARGS` 等动态写目标也被拒（宁枉勿纵，README 同步声明）。
  - **名字臂**（进范围的第三条件）：写入器/CLI 名按**执行位**逐词 basename 相等进范围——有效命令名、包装器参数、解释器脚本位；`git add`/`echo` 的参数不在执行位 ⇒ 同仓三条回归钉保持放行。
  - **身份层不放宽**：官方写入器/CLI 按解析路径精确相等；包装器参数中的写入器名解析不到官方安装位一律拒（裸名直调在装了官方写入器的机器上放行）；`unparseable` 命令默认拒。
  - 放行官方 `gzx-append.sh`、`gzx_cli.py`（含 `save-cell` / `pending-add` heredoc）、`cp`/`mkdir`/`chmod 700` 引导安装及只读前缀白名单。
  - 审计只记录 `{ts, tool, decision, rule}` 元数据，首行保证写入 `hook_alive` 活体事件。
- **`hooks/gzx-capture.py`（PostToolUse）**：
  - 静默捕获真正成功的 `git commit`（输出匹配成功标记 `[... <sha>]`，支持 `root-commit` 与 `detached HEAD`，并跟随命令行 `cd` / `env -C` / `git -C` / `--git-dir` 定位目标仓库 `HEAD` 复核）。
  - 支持复合命令中的目录跟踪：支持大括号命令组 `{ cd repo; git commit; }`（当前 shell 连续变更目录）与小括号子 shell `( cd repo; git commit )`（子 shell 独立 cwd 栈，退出后恢复）。
  - 通过 `lock_append_commit_unique` 以 `(sha, event)` 在锁内原子去重并计算单调 `seq`，不写仓库名与文件路径。

## 3. 三阶段验收口径

1. **安装验收**：
   - `python3 hooks/gzx-guard.py --selftest` 全绿（allow + deny 双侧）。
   - `python3 hooks/test-gzx-invariants.py` 与 `python3 hooks/test-gzx-integration.py` 全绿。
   - `~/.gan-zhong-xue/audit.jsonl` 首行含 `hook_alive`。
2. **运行健康度**：`candidates.jsonl` 与 `audit.jsonl` 正常推进、无命令原文泄漏、写入失败必带非零退出码与 stderr 状态行。
3. **已知残余边界**：
   - `cd` 到 `~/.gan-zhong-xue` 后不提路径名的破坏性操作仍拦不住（减速带定位）。
   - `gzx-capture.py` 的静态 `cd` 解析仅支持字面相对/绝对路径与 `~` 展开；动态变量路径（如 `cd $REPO_DIR`）及历史切换（如 `cd -`）不在静态推导范围内。
   - `lock_append_map_pair` 为单写者互斥追加，不构成跨文件崩溃原子事务；双文件一致性依赖 `check-divergence` 退出码 24 告警，并由 `map.jsonl` 的全字段模式支撑无损恢复。
   - 宿主 hook 接线失败时 `gzx-guard.py` 采用 fail-open，不冻结用户会话。
   - `hooks/matcher.json` 仍保留待实测标记，跨宿主接线能力以实际运行验证为准。
   - **字面性闸代价与盲区**（board #129 评审共识接受）：范围外含动态写目标的命令被拒（`echo hi > $OUT` 等）；词元层无法区分 `echo$HOME`（粘连命令名）与 `echo $HOME`（参数），命令名闸按静态前缀截断——粘连形态的真实动态命令名放行（罕见，漏报方向）；`bash -c '…'` 的载荷字符串不审计；一条 Bash 命令内两次 commit 只记录最后匹配上下文的一条（capture 单事件单记录）。
   - `cell` 跨层取格靠 `(概念, 分层)` 键 + 歧义报告缓解，**稳定格 id 未实施**（根治方案见 docs/DEBT.md）。
