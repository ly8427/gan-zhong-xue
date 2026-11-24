# gan-zhong-xue 欠账清单

评审带账项（board thread #122 定案：带账合并、不挡合并、每项带可执行验收）。
新欠账随发现随追加；销账时在行内记销账 commit。

| # | 项 | 严重度 | 现象（可核量） | 修向 | 可执行验收 | 来源 |
|---|---|---|---|---|---|---|
| D1 | ③ 临时文件泄漏 | 低 | Windows 无 flock(1) 路径：`gzx-append.sh:51` `trap 'rm -f "$TMP"' EXIT` + `:81` `exec "$PY"`——exec 替换进程后 EXIT trap 不执行，**每次写入漏 1 个临时文件**（`$TMPDIR/gzx-append.XXXXXX`） | 路径 B 不再 `exec`：改普通调用 `$PY "$CORE" …; rc=$?` + `rm -f "$TMP"` + `exit $rc`（退出码四态语义不变） | Git Bash 无 flock 环境：连续写入 100 次 → tmp 目录 `gzx-append.*` 残留 = 0；四态退出码 0/21/22/23 语义经套件不变 | grok bot 方案 §二③，board #122 #2648/#2653 |
| D2 | ④ 双地图发散 | 中低 | `gzx_lock_append.py:66-96`：map.md 与 map.jsonl 两次独立 `lock_append`（各自拿锁），一次成功一次 rc23 时无检测/恢复/重试去重 → 静默发散 | 自检必答项加一条发散检测：比对 md 格数（`^## ` 计数）与 jsonl 行数，不一致即报错；恢复指引 = 以 jsonl 为准重建对应 md 格 | 注入一对人为发散的 map/jsonl → 自检必报错且**输出两侧计数两个数字**（md 格数、jsonl 行数）；一致文件 → 不误报 | grok bot 方案 §二④，board #122 #2648/#2651/#2654 §5 |
