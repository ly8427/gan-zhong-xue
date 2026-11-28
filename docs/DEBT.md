# gan-zhong-xue 欠账清单

评审带账项（board thread #122 定案：带账合并、不挡合并、每项带可执行验收）。
新欠账随发现随追加；销账时在行内记销账版本与验证结果。

| # | 项 | 严重度 | 现象（可核量） | 修向 | 可执行验收 | 状态 |
|---|---|---|---|---|---|---|
| D1 | ③ 临时文件泄漏 | 低 | 无 flock(1) 路径：`gzx-append.sh` 使用 `trap 'rm -f "$TMP"' EXIT` + `exec "$PY"`——exec 替换进程后 EXIT trap 不执行，每次写入漏 1 个临时文件 | 路径 B 改为普通调用 `$PY "$CORE" …; rc=$?` + `rm -f "$TMP"` + `exit $rc`（四态退出码不变） | 无 flock 路径连续写入验证，tmp 目录 `gzx-append.*` 残留 = 0 | **已销账（v1.2.0）** |
| D2 | ④ 双地图发散 | 中低 | `map.md` 与 `map.jsonl` 原先分两次独立加锁写入，单侧失败时无检测/原子保障 | ① 新增 `gzx_lock_append.lock_append_map_pair` 与 `gzx_cli.py save-cell` 同锁互斥双写（单写者互斥；**非跨文件事务原子**）；② 新增 `gzx_cli.py check-divergence` 与 `preflight` 发散检测（v1.2.0 计数比对，本批升级为按 (概念,分层) 重建+diff，字段不符/缺格/孤立格亦报） | 注入人为发散的 map/jsonl → `check-divergence` 退出 24 并输出两侧计数与首个不符字段；一致文件返回 0 | **已销账（v1.2.0）；检测面本批升级** |
| D3 | ⑤ 捕获识别覆盖缺口 | 低 | `hooks/gzx-capture.py` `is_commit_command` 的来源身份/引号形态区分尚无成对正反测试覆盖 | 在 `test-gzx-invariants.py` 补齐引号包裹与去引号成对正反断言（含带空格路径、`.exe`、环境值） | `test_commit_command_recognition` 包含成对正反断言且全部通过 | **已销账（v1.2.0）** |
| D4 | ⑥ cell 跨层取格 | 中低 | 同概念跨多个分层时 `cell <概念>` 按 last-wins 静默返回，可取错层级的验证题（board #129 dsh #3055 §5-1） | 短期：多分层报歧义（退出码 25）+ `cell <概念> <分层>` 精确取格（本批已落地）；根治：稳定格 id 同时写入 md 首行与 jsonl 行，save-cell 生成并回读校验，连接不靠概念名 | 追加 `## Foo · 甲 · D1` 与 `## Foo · 乙 · D2` 后，`cell Foo` 报歧义（25）；`cell Foo 甲` 精确命中甲 | **短期已销账（本批）；根治（稳定 id）未实施** |
| D5 | ⑦ 词边界固有盲区 | 低 | 旧词法（shlex 切词后回找）丢掉空白与引号信息：`echo$HOME`（粘连命令名）与 `echo $HOME`（参数）同形——命令名闸只能按静态前缀截断，粘连形态放行（漏报方向，罕见）；`cmd >o $X` 与 `cmd>o $X` 同判（宁枉勿纵） | 守卫自带词法底座（board #129 重写）：一遍扫出词与操作符，带原文起止位置与引号标记，不再靠分词后的词形猜测 | 命令名必须完全字面：`echo$HOME` → 拒 `expansion:unprovable`；`$X` 是独立参数时不再被当成重定向目标（`cmd > out $ARGS` 放行，`cmd >o$X` 拒）；selftest 两条钉在案 | **已销账（v1.2.0 守卫重写）** |
| D8 | ⑧ capture 单事件单记录 | 低 | 一条 Bash 命令内两次 commit（如 `( cd repo && git commit ); git commit`）只记录最后匹配上下文的一条 sha；`);` 粘连拆分后外层已不漏，内层仍单记 | capture 主循环改为逐上下文匹配全部成功标记并各记一条（去重键 (sha,event) 已支持） | 构造双提交命令，candidates.jsonl 出现两条 sha | **未实施** |
| D9 | 守卫：官方 heredoc 例外的触发仍是正则 | 低 | 官方 CLI / 写入器的 heredoc 调用只在首行恰为「解释器 + 路径 + 动词 + <<'定界符」时才进身份层；带解释器选项或重定向的合法形态（`python3 -u '<技能目录>/gzx_cli.py' save-cell <<'E'`、`~/.gan-zhong-xue/gzx-append.sh map 2>/dev/null <<'E'`）落回通用流程被拒 `structure:heredoc`（误拒方向，board #129 重写范围外） | 把「官方件 + 形状合法的 heredoc」判定统一到词法底座（去掉两条正则触发器），定界符唯一 / 末行约束不变 | 上述两条 classify 放行且规则名以 `cli-heredoc` / `writer-heredoc` 开头；假写入器 / 假 CLI 的同形 heredoc 仍拒 | **未实施** |
| D10 | 守卫：裸命令名与相对路径的身份依赖 hook 进程的 PATH / cwd | 低 | 命令名位的裸写入器名按 hook 进程的 PATH 解析、相对路径按 hook 进程的 cwd 规范化；宿主若让 Bash 工具跑在不同的 PATH / cwd 下，两者会错位（更常见的是误拒；仅当 hook 进程 cwd 恰为技能目录时存在误放行的理论面） | 宿主实测两处是否一致；不一致时从事件负载取工具侧 cwd / PATH，或取消命令名位裸名的放行 | 在 Claude Code 上实测 hook 进程与 Bash 工具的 PATH、cwd 是否一致，结论写进 mechanism.md | **未实施（待宿主实测）** |
