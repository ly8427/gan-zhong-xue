# gan-zhong-xue 欠账清单

评审带账项（board thread #122 定案：带账合并、不挡合并、每项带可执行验收）。
新欠账随发现随追加；销账时在行内记销账版本与验证结果。

| # | 项 | 严重度 | 现象（可核量） | 修向 | 可执行验收 | 状态 |
|---|---|---|---|---|---|---|
| D1 | ③ 临时文件泄漏 | 低 | 无 flock(1) 路径：`gzx-append.sh` 使用 `trap 'rm -f "$TMP"' EXIT` + `exec "$PY"`——exec 替换进程后 EXIT trap 不执行，每次写入漏 1 个临时文件 | 路径 B 改为普通调用 `$PY "$CORE" …; rc=$?` + `rm -f "$TMP"` + `exit $rc`（四态退出码不变） | 无 flock 路径连续写入验证，tmp 目录 `gzx-append.*` 残留 = 0 | **已销账（v1.2.0）** |
| D2 | ④ 双地图发散 | 中低 | `map.md` 与 `map.jsonl` 原先分两次独立加锁写入，单侧失败时无检测/原子保障 | ① 新增 `gzx_lock_append.lock_append_map_pair` 与 `gzx_cli.py save-cell` 同锁互斥双写（单写者互斥；**非跨文件事务原子**）；② 新增 `gzx_cli.py check-divergence` 与 `preflight` 发散检测（v1.2.0 计数比对，本批升级为按 (概念,分层) 重建+diff，字段不符/缺格/孤立格亦报） | 注入人为发散的 map/jsonl → `check-divergence` 退出 24 并输出两侧计数与首个不符字段；一致文件返回 0 | **已销账（v1.2.0）；检测面本批升级** |
| D3 | ⑤ 捕获识别覆盖缺口 | 低 | `hooks/gzx-capture.py` `is_commit_command` 的来源身份/引号形态区分尚无成对正反测试覆盖 | 在 `test-gzx-invariants.py` 补齐引号包裹与去引号成对正反断言（含带空格路径、`.exe`、环境值） | `test_commit_command_recognition` 包含成对正反断言且全部通过 | **已销账（v1.2.0）** |
| D4 | ⑥ cell 跨层取格 | 中低 | 同概念跨多个分层时 `cell <概念>` 按 last-wins 静默返回，可取错层级的验证题（board #129 dsh #3055 §5-1） | 短期：多分层报歧义（退出码 25）+ `cell <概念> <分层>` 精确取格（本批已落地）；根治：稳定格 id 同时写入 md 首行与 jsonl 行，save-cell 生成并回读校验，连接不靠概念名 | 追加 `## Foo · 甲 · D1` 与 `## Foo · 乙 · D2` 后，`cell Foo` 报歧义（25）；`cell Foo 甲` 精确命中甲 | **短期已销账（本批）；根治（稳定 id）未实施** |
| D5 | ⑦ 词边界固有盲区 | 低 | 分词后 word→frag 边界有无空白不可区分：`echo$HOME`（粘连命令名）与 `echo $HOME`（参数）同形——命令名闸按静态前缀截断，粘连形态的真实动态命令名放行（漏报方向，罕见）；`cmd >o $X` 与 `cmd>o $X` 同判（宁枉勿纵） | 静态层面无解（bash 展开语义不可模拟）；如需收紧须在 hook 外层做真实 shell 回显核对（另立评审） | 登记即边界：mechanism.md 已知残余已声明；`bash$SCRIPT` 形态由包装器尾巴闸覆盖（本批实测） | **登记（设计边界）** |
| D8 | ⑧ capture 单事件单记录 | 低 | 一条 Bash 命令内两次 commit（如 `( cd repo && git commit ); git commit`）只记录最后匹配上下文的一条 sha；`);` 粘连拆分后外层已不漏，内层仍单记 | capture 主循环改为逐上下文匹配全部成功标记并各记一条（去重键 (sha,event) 已支持） | 构造双提交命令，candidates.jsonl 出现两条 sha | **未实施** |
