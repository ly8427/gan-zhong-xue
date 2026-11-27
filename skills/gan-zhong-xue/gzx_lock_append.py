#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gzx-lock-append.py — 写入器不变量核心（v1.2.0，单写者锁与 append-only 保障）。

唯一实现"锁 + 追加（append-only 资产）"与"pending 清空（lock_truncate）"的地方：
gzx-append.sh（bash 面）、gzx_cli.py（聚合 CLI）与 hooks/ 的 python 调用方
都落到这里，保证各入口没有第二份不变量实现（无漂移）。

用法: python3 gzx_lock_append.py <target>      内容从 stdin 读
  target ∈ {map|jsonl|pending|candidates|audit}
退出码: 0 成功（dedup 视为成功跳过）/ 21 目标非法或空 stdin / 23 写失败。
"""

import json
import os
import re
import sys

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
    """跨形态目录规范化——POSIX python 收到 Windows 盘符路径 / Windows python
    收到 /mnt|/c 形态时自动换算。"""
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


def _to_bytes(data):
    return data.encode("utf-8") if isinstance(data, str) else data


def lock_append(gzx_dir, target, data, prepend_if_missing=None):
    """锁住 gzx_dir/.lock 后追加 data 到对应文件。返回 (rc, message)。
    只追加：唯一的写模式是 'ab'，代码里不存在截断分支。"""
    if target not in TARGETS:
        return 21, "unknown target: %s" % target
    if not data:
        return 21, "empty stdin"
    raw = _to_bytes(data)
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
                        f.write(_to_bytes(prepend_if_missing))
                    f.write(raw)
                    f.flush()
                    os.fsync(f.fileno())
            finally:
                lf.close()
    except OSError as e:
        return 23, "write failed: %s" % e
    return 0, "ok %s (+%d bytes)" % (targetpath, len(raw))


def lock_append_map_pair(gzx_dir, md_data, jsonl_data):
    """同一把排他锁内依次追加 map.jsonl 与 map.md（D2 双地图互斥写入）。
    锁提供单写者互斥防护，非跨文件事务原子；jsonl 包含全部元数据字段，
    如遇写入中断导致发散，可据 jsonl 完好重建 md。"""
    if not md_data or not jsonl_data:
        return 21, "empty md or jsonl payload"
    md_bytes = _to_bytes(md_data)
    jsonl_bytes = _to_bytes(jsonl_data)
    gzx_dir = _normalize_dir(gzx_dir)
    os.makedirs(gzx_dir, exist_ok=True)
    lockpath = os.path.join(gzx_dir, ".lock")
    md_path = os.path.join(gzx_dir, TARGETS["map"])
    jsonl_path = os.path.join(gzx_dir, TARGETS["jsonl"])
    try:
        with open(lockpath, "a+") as lf:
            _lock(lf)
            try:
                with open(jsonl_path, "ab") as jf, open(md_path, "ab") as mf:
                    jf.write(jsonl_bytes)
                    jf.flush()
                    os.fsync(jf.fileno())
                    mf.write(md_bytes)
                    mf.flush()
                    os.fsync(mf.fileno())
            finally:
                lf.close()
    except OSError as e:
        return 23, "write failed: %s" % e
    return 0, "ok map+jsonl (+%d/+%d bytes)" % (len(md_bytes), len(jsonl_bytes))


def lock_append_commit_unique(gzx_dir, data):
    """candidates 提交捕获的原子去重追加。
    锁内一次完成：查重（sha+event）→ 序号（现有完整行数 + 1）→ 追加。"""
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    try:
        row = json.loads(data)
    except ValueError:
        return 21, "data is not a JSON object"
    if not isinstance(row, dict) or not row.get("sha"):
        return 21, "data must be a JSON object with sha"
    row.setdefault("event", "commit")
    gzx_dir = _normalize_dir(gzx_dir)
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


def lock_truncate(gzx_dir, target):
    """锁内核截断目标文件。**只许 pending**；append-only 资产一律拒绝。"""
    if target != "pending":
        return 21, "truncate only allowed for pending (append-only assets never truncate)"
    gzx_dir = _normalize_dir(gzx_dir)
    os.makedirs(gzx_dir, exist_ok=True)
    lockpath = os.path.join(gzx_dir, ".lock")
    targetpath = os.path.join(gzx_dir, TARGETS[target])
    try:
        with open(lockpath, "a+") as lf:
            _lock(lf)
            try:
                if not os.path.exists(targetpath):
                    return 0, "ok pending (already empty)"
                os.truncate(targetpath, 0)
            finally:
                lf.close()
    except OSError as e:
        return 23, "truncate failed: %s" % e
    return 0, "ok pending (cleared)"


def main():
    gzx_dir = os.environ.get("GZX_DIR", os.path.expanduser("~/.gan-zhong-xue"))
    if len(sys.argv) == 3 and sys.argv[1] == "--truncate":
        if sys.argv[2] != "pending":
            sys.stderr.write("gzx-lock-append: --truncate only supports pending\n")
            raise SystemExit(21)
        rc, msg = lock_truncate(gzx_dir, "pending")
        sys.stderr.write("gzx-append: %s\n" % msg)
        raise SystemExit(rc)
    if len(sys.argv) != 2:
        sys.stderr.write("gzx-lock-append: usage <target>, stdin = content\n")
        raise SystemExit(21)
    data = sys.stdin.buffer.read()
    rc, msg = lock_append(gzx_dir, sys.argv[1], data)
    sys.stderr.write("gzx-append: %s\n" % msg)
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
