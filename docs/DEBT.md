# gan-zhong-xue 欠账清单

评审带账项（board thread #122 定案：带账合并、不挡合并、每项带可执行验收）。
新欠账随发现随追加；销账时在行内记销账版本与验证结果。

| # | 项 | 严重度 | 现象（可核量） | 修向 | 可执行验收 | 状态 |
|---|---|---|---|---|---|---|
| D1 | ③ 临时文件泄漏 | 低 | 无 flock(1) 路径：`gzx-append.sh` 使用 `trap 'rm -f "$TMP"' EXIT` + `exec "$PY"`——exec 替换进程后 EXIT trap 不执行，每次写入漏 1 个临时文件 | 路径 B 改为普通调用 `$PY "$CORE" …; rc=$?` + `rm -f "$TMP"` + `exit $rc`（四态退出码不变） | 无 flock 路径连续写入验证，tmp 目录 `gzx-append.*` 残留 = 0 | **已销账（v1.2.0）** |
| D2 | ④ 双地图发散 | 中低 | `map.md` 与 `map.jsonl` 原先分两次独立加锁写入，单侧失败时无检测/原子保障 | ① 新增 `gzx_lock_append.lock_append_map_pair` 与 `gzx_cli.py save-cell` 同锁原子双写；② 新增 `gzx_cli.py check-divergence` 与 `preflight` 发散检测 | 注入人为发散的 map/jsonl → `check-divergence` 退出非 0 并输出两侧计数；一致文件返回 0 | **已销账（v1.2.0）** |
| D3 | ⑤ 捕获识别覆盖缺口 | 低 | `hooks/gzx-capture.py` `is_commit_command` 的来源身份/引号形态区分尚无成对正反测试覆盖 | 在 `test-gzx-invariants.py` 补齐引号包裹与去引号成对正反断言（含带空格路径、`.exe`、环境值） | `test_commit_command_recognition` 包含成对正反断言且全部通过 | **已销账（v1.2.0）** |
