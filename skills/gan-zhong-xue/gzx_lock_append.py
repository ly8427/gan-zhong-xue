#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gzx-lock-append.py — 写入器不变量核心（v1.1.0，GPT round-18 P1-4/P2-7 收口）。

唯一实现"锁 + 只追加"的地方：gzx-append.sh（bash 面）与 hooks/ 的 python 调用方
（gzx-guard / gzx-capture）都落到这里，保证两个入口没有第二份不变量实现（无漂移）。

用法: python3 gzx-lock-append.py <target>      内容从 stdin 读
  target ∈ {map|jsonl|pending|candidates|audit}
退出码: 0 成功（dedup 视为成功跳过）/ 21 目标非法或空 stdin / 23 写失败。

v1.1.0（GPT PR #4 审查收口）:
  * lock_append 支持 prepend_if_missing——审计 hook_alive 首行的"存在性检查+
    预置"移入锁内（旧实现两步分离,并发时首行可能重复,P2-7）。
  * 新增 lock_append_commit_unique——candidates 的 去重检查/序号计算/追加
    全部在锁内一次完成（旧实现三步分离,并发时重复登记或序号错乱,P1-4）。
"""

import json
import os
import re
import sys
import time

TARGETS = {
    "map": "map.md",
    "jsonl": "map.jsonl",
    "pending": "pending.md",
    "candidates": "candidates.jsonl",
    "audit": "audit.jsonl",
}


def _lock(f):
    try:
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        f.seek(0, 2)
        return f
    except ImportError:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        return f


def _normalize_dir(d):
    """V1.1.0（GPT PR #4 F 组实证）：跨形态目录规范化——POSIX python 收到
    Windows 盘符路径 / Windows python 收到 /mnt|/c 形态时自动换算。
    根因：bash 写入器在 Git Bash 里经 cygpath 转 Windows 形态传给 python，
    但 exec 到的可能是 WSL 的 python3（POSIX）→ 字面量 'C:\\...' 相对目录，
    数据写到未知位置而 rc=0（静默写错处比写失败更险）。"""
    if not d:
        return d
    if os.name == "nt":
        m = re.match(r"^/mnt/([A-Za-z])/(.*)$", d) or re.match(r"^/([A-Za-z])/(.*)$", d)
        if m:
            return m.group(1).upper() + ":\\" + m.group(2).replace("/", "\\")
        return d
    m = re.match(r"^([A-Za-z]):[/\\](.*)$", d)
    if m:
        return "/mnt/" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/")
    return d


def lock_append(gzx_dir, target, data, prepend_if_missing=None):
    """锁住 gzx_dir/.lock 后追加 data 到对应文件。返回 (rc, message)。
    只追加：唯一的写模式是 'ab'，代码里不存在截断分支。
    prepend_if_missing（V1.1.0, P2-7）:目标文件缺失/为空时,先在锁内写入该行
    （如审计 hook_alive 首行）——存在性检查与预置在同一临界区,并发不重复。"""
    if target not in TARGETS:
        return 21, "unknown target: %s" % target
    if not data:
        return 21, "empty stdin"
    gzx_dir = _normalize_dir(gzx_dir)
    os.makedirs(gzx_dir, exist_ok=True)
    lockpath = os.path.join(gzx_dir, ".lock")
    targetpath = os.path.join(gzx_dir, TARGETS[target])
    try:
        with open(lockpath, "a+") as lf:
            _lock(lf)
            try:
                need_prepend = bool(prepend_if_missing) and (
                    not os.path.exists(targetpath) or os.path.getsize(targetpath) == 0)
                with open(targetpath, "ab") as f:   # 只追加：'ab'
                    if need_prepend:
                        f.write(prepend_if_missing.encode("utf-8")
                                if isinstance(prepend_if_missing, str) else prepend_if_missing)
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
            finally:
                lf.close()
    except OSError as e:
        return 23, "write failed: %s" % e
    return 0, "ok %s (+%d bytes)" % (targetpath, len(data))


def lock_append_commit_unique(gzx_dir, data):
    """V1.1.0（GPT round-18 P1-4）：candidates 提交捕获的原子去重追加。
    data = 含 sha 与 event 字段的 JSON 行（bytes/str 均可）。
    锁内一次完成：①查重（同 sha+event 已存在 → 跳过不写,rc=0 标记 dedup-skip）
    ②序号 = 现有完整行数 + 1（行序单调,不用时间戳——board #49 同秒歧义判例）
    ③追加。三步同一临界区,并发不再重复/乱序。"""
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    try:
        row = json.loads(data)
    except ValueError:
        return 21, "data is not a JSON object"
    if not isinstance(row, dict) or not row.get("sha"):
        return 21, "data must be a JSON object with sha"
    row.setdefault("event", "commit")
    os.makedirs(gzx_dir, exist_ok=True)
    lockpath = os.path.join(gzx_dir, ".lock")
    targetpath = os.path.join(gzx_dir, TARGETS["candidates"])
    try:
        with open(lockpath, "a+") as lf:
            _lock(lf)
            try:
                rows = []
                if os.path.exists(targetpath):
                    with open(targetpath, "r", encoding="utf-8", errors="replace") as f:
                        for ln in f:
                            ln = ln.strip()
                            if not ln:
                                continue
                            try:
                                rows.append(json.loads(ln))
                            except ValueError:
                                continue          # 坏行跳过,不整份丢弃
                for old in rows:
                    if old.get("sha") == row["sha"] and old.get("event") == row.get("event"):
                        return 0, "dedup-skip (sha=%s already captured)" % row["sha"][:12]
                row["seq"] = len(rows) + 1        # 锁内现算:行序单调,无同秒歧义
                with open(targetpath, "ab") as f:  # 只追加
                    f.write((json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
                    f.flush()
                    os.fsync(f.fileno())
            finally:
                lf.close()
    except OSError as e:
        return 23, "write failed: %s" % e
    return 0, "ok candidates (sha=%s, seq=%s)" % (str(row["sha"])[:12], row.get("seq"))


def main():
    gzx_dir = os.environ.get("GZX_DIR", os.path.expanduser("~/.gan-zhong-xue"))
    if len(sys.argv) != 2:
        sys.stderr.write("gzx-lock-append: usage <target>, stdin = content\n")
        raise SystemExit(21)
    data = sys.stdin.buffer.read()
    rc, msg = lock_append(gzx_dir, sys.argv[1], data)
    sys.stderr.write("gzx-append: %s\n" % msg)
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
