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
    - 一致时返回 0；计数不匹配、字段不符（`field-mismatch`）或 md 孤立格（`orphan_md_cells > 0`）返回专用退出码 **24** 并输出两侧差异与从 `map.jsonl` 重建的指引（mismatches 明细上限 10 条防刷屏，超出截断）；
    - 缺少依赖或文件不可读返回 21/22。
  - `gzx_cli.py cell`：同概念跨多个分层时返回 `ambiguous: true` 并以退出码 **25** 拒绝静默取格（`cell <概念> <分层>` 按 (概念, 分层) 精确取格），杜绝跨层 last-wins 取错验证题。

## 2. 兜底 Hook 定位（Claude Code）

- **`hooks/gzx-guard.py`（PreToolUse）**：
  - 定位为**减速带 + 审计**，而非安全沙箱。两条防线分开写（dsh #3066 §4）：**手滑面**（模型自己写错命令）靠名字 / 路径判据 + 自检 + 行为反例测试；**对抗面**（提示注入刻意绕过）字符串判据**不承诺完备**——完全不出现官方名与数据目录的写法不在承诺内。
  - **词法底座**（2026-11-28 重写，board #129）：一遍扫出「词」与「操作符」，各带原文起止位置——引号剥除、转义已解；`$(…)` 与反引号按原字符留在词里（守卫不展开，只判「含扩展算子」）；冒号、`$`、`[` 不再把词切碎。对照表见 `hooks/test-gzx-behavior.py` 的 L 段。
  - **判定只问两个能可靠回答的问题**：① 命令名是不是「会启动程序的类型」——shell 族（bash / sh / zsh / dash / ksh / source / `.`）、解释器族（python / python3 及带版本号的 `pythonN.N` / perl / ruby / node）、启动器族（env nice nohup setsid stdbuf timeout sudo su doas xargs command exec builtin time script watch busybox strace ltrace chroot pkexec）；② 命令里**任何位置**有没有出现官方写入器 / CLI 的名字（`gzx-append` / `gzx-append.sh` / `gzx_cli.py`：整词以官方名结尾按 basename 相等；带引号的程序串按子串）。两条都是 ⇒ 每个出现处的完整路径必须精确等于官方固定位（数据目录下的 `gzx-append.sh`、插件技能目录下的 `gzx_cli.py`），否则拒（规则 `identity:<名字>`）。不分析「谁执行谁」（启动器任意嵌套，位置分析无界），不维护解释器选项表——选项值、`--`、`.exe`、PATH 前缀里的冒号都不影响判定。
  - **范围门**：词里引用运行时数据目录（`.gan-zhong-xue` / `GZX_DIR`）∨ heredoc 正文含标记 ∨ 启动器命令里出现已核实的官方名；范围外直接放行（`out-of-scope`）。常规仓内开发、`git commit`、文档 `Write`/`Edit` 及不含数据目录引用的 `Agent` 提示词不受影响——`git add` / `cp` / `vim` 的命令名不是启动器类，只是在处理一个叫这名字的文件。
  - **字面性闸**（与范围无关，进出范围两向都拦）：命令名、写文件的重定向目标出现 `$` 变量 / 命令替换 / `` ` `` / `*?[]{}` ⇒ 拒（`expansion:unprovable`）；脚本执行型命令（shell / 解释器）的其余参数同样必须字面（带空白的引号串除外——那是程序文本，不是脚本路径）。fd 复制（`2>&1`）与输入重定向（`<` `<<` `<<<`）不查。判据是「落盘路径 / 被执行的脚本能不能静态证明」，不是「展开后像不像受保护路径」（守卫不做展开模拟）。
  - **范围内结构层**：`;` `&&` `||` `&` 换行 括号 进程替换、活的命令替换（含藏在双引号里的 `` `…` `` 与 `$(…)`）、重定向落数据目录、非官方 heredoc 一律拒；逐命令块查只读前缀白名单与引导安装豁免。
  - **身份层不放宽**：官方写入器 / CLI 按解析路径精确相等；`unparseable` 命令默认拒。**裸写入器名只在命令名位可信**（`gzx-append map`，且要求 PATH 上解析到官方位、PATH 里没有排在它前面的「当前目录项」、命令不带 `PATH=` 前缀）——实测 bash / sh 找脚本参数时**先看当前目录再看 PATH**（python 只看当前目录），当前目录里的同名假件会遮住 PATH 上的官方件，所以参数位上的裸名（`bash gzx-append.sh`、`env gzx-append.sh`）与安装态无关、一律拒。
  - 放行官方 `gzx-append.sh`、`gzx_cli.py`（含 `save-cell` / `pending-add` heredoc）、`cp`/`mkdir`/`chmod 700` 引导安装及只读前缀白名单。
  - 审计只记录 `{ts, tool, decision, rule}` 元数据，首行保证写入 `hook_alive` 活体事件。规则名：放行 = `out-of-scope` / `readonly-ok` / `writer-heredoc:<n>B` / `cli-heredoc:<n>B`；拒 = `identity:<名字>` / `payload:<名字>` / `expansion:unprovable` / `structure:<操作符>` / `structure:unparseable` / `structure:heredoc` / `redirect-to-protected:<操作符>` / `redirect:missing-target` / `prefix:<命令名>` / `find:<旗标>` / `sed:-i` / `heredoc:*` / `write-tool:protected-path` / `agent:protected-ref`。
- **`hooks/gzx-capture.py`（PostToolUse）**：
  - 静默捕获真正成功的 `git commit`（输出匹配成功标记 `[... <sha>]`，支持 `root-commit` 与 `detached HEAD`，并跟随命令行 `cd` / `env -C` / `git -C` / `--git-dir` 定位目标仓库 `HEAD` 复核）。
  - 支持复合命令中的目录跟踪：支持大括号命令组 `{ cd repo; git commit; }`（当前 shell 连续变更目录）与小括号子 shell `( cd repo; git commit )`（子 shell 独立 cwd 栈，退出后恢复）。
  - 通过 `lock_append_commit_unique` 以 `(sha, event)` 在锁内原子去重并计算单调 `seq`，不写仓库名与文件路径。

## 3. 三阶段验收口径

1. **安装验收**：
   - `python3 hooks/gzx-guard.py --selftest` 全绿（allow + deny 双侧；项数以实跑输出为准）。
   - `python3 hooks/test-gzx-invariants.py`、`python3 hooks/test-gzx-integration.py`、`python3 hooks/test-gzx-behavior.py` 全绿。
   - `~/.gan-zhong-xue/audit.jsonl` 首行含 `hook_alive`。
2. **运行健康度**：`candidates.jsonl` 与 `audit.jsonl` 正常推进、无命令原文泄漏、写入失败必带非零退出码与 stderr 状态行。
3. **已知残余边界**：
   - `cd` 到 `~/.gan-zhong-xue` 后不提路径名的破坏性操作仍拦不住（减速带定位）。
   - `gzx-capture.py` 的静态 `cd` 解析仅支持字面相对/绝对路径与 `~` 展开；动态变量路径（如 `cd $REPO_DIR`）及历史切换（如 `cd -`）不在静态推导范围内。
   - `lock_append_map_pair` 为单写者互斥追加，不构成跨文件崩溃原子事务；双文件一致性依赖 `check-divergence` 退出码 24 告警，并由 `map.jsonl` 的全字段模式支撑无损恢复。
   - 宿主 hook 接线失败时 `gzx-guard.py` 采用 fail-open，不冻结用户会话。
   - `hooks/matcher.json` 仍保留待实测标记，跨宿主接线能力以实际运行验证为准。
   - **守卫的已知代价与边界**（board #129 评审共识；按族列出，每族在 `gzx-guard.py --selftest` 里配一对用例——一条 allow 钉「当前不审计」，一条 deny 钉已收口的部分，dsh #3072 §1-1 对价）：
     - 完全不出现官方名与数据目录的脚本不审计：`bash /tmp/evil.sh`（放行）↔ `bash /tmp/evil/gzx-append.sh map`（拒）。
     - 启动器清单是**封闭**的：清单外的自定义 wrapper（`py`、`pypy3`、`ssh host bash …`、`docker exec … bash …` 等）不识别，补清单是配置不是机制 ↔ 清单内任意嵌套 `sudo -n env nice -n 5 timeout 5 bash /tmp/evil/gzx-append.sh map`（拒）。
     - 泛启动器（env / sudo / nohup / timeout / xargs …）之后的**非字面参数**不查：`sudo bash $SCRIPT map`、`nohup python3 server.py --port $PORT`（放行）↔ 脚本执行型命令自身的参数必须字面：`bash $SCRIPT map`（拒）。要收需要白名单封闭，会误伤 `find -exec python3 … {}`、`xargs -I{} bash -c … {}` 这类日常写法，故登记为契约边界。
     - 参数位的裸写入器名一律拒（`bash gzx-append.sh`、`env gzx-append.sh`）↔ 命令名位的裸名在 PATH 解析到官方位时放行（`gzx-append map`；`test-gzx-behavior.py` 环境② 把这个安装态造出来真跑，两种平台都不跳过）。
     - 带引号的程序串（`bash -c` / `su -c` / `watch` 的命令串）里出现官方名即拒，**包括官方件**：`bash -c 'sh gzx-append.sh map'`、`bash -c "python3 '<技能目录>/gzx_cli.py' preflight"`（拒 `payload:<名字>`）↔ 不含官方名的载荷不审计：`bash -c 'ls'`、`bash -c 'cat x > $HOME/y'`（放行）。
     - 解释器 / 启动器命令里出现的官方名必须是官方位，哪怕它只是数据参数：`python3 -m py_compile /x/gzx_cli.py`、`sudo cp /x/gzx_cli.py /tmp/x`（拒）↔ 同样的文件操作在命令名不是启动器类时放行：`cp skills/gan-zhong-xue/gzx_cli.py /tmp/x.py`、`git add skills/gan-zhong-xue/gzx_cli.py`、`vim gzx_cli.py`。
     - 范围外含动态**写目标**的命令被拒（宁枉勿纵）：`echo hi > $OUT`、`cat a.txt >> "$LOG"`（拒）↔ 写目标是字面、`$` 只出现在参数上时放行：`cmd > out $ARGS`、`echo '> $HOME'`（旧词法把引号串开头的 `>` 和后面的参数误当成重定向目标，重写后解除）。
     - `cd` 进数据目录后不提路径名：`rm map.md`（放行）↔ `rm ~/.gan-zhong-xue/map.md`（拒）。
   - **#128 红线③的适用域**（dsh #3072 §1-4 落地一行）：臂③——可分词对象用 basename 相等；不可分词对象（引号粘成一个词、被吸收进命令名）用子串、仅 deny。
   - 一条 Bash 命令内两次 commit 只记录最后匹配上下文的一条（capture 单事件单记录）。
   - `cell` 跨层取格靠 `(概念, 分层)` 键 + 歧义报告缓解，**稳定格 id 未实施**（根治方案见 docs/DEBT.md）。
