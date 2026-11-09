#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gzx-lock-append.py — 写入器不变量核心（v1.0.9 D2）。

唯一实现"锁 + 只追加"的地方：gzx-append.sh（bash 面）与 hooks/ 的 python 调用方
（gzx-guard / gzx-capture）都落到这里，保证两个入口没有第二份不变量实现（无漂移）。

用法: python3 gzx-lock-append.py <target>      内容从 stdin 读
  target ∈ {map|jsonl|pending|candidates|audit}
退出码: 0 成功 / 21 目标非法或空 stdin / 23 写失败（锁不可用由调用方在选路时保证：
  本模块锁原语 = fcntl(POSIX) / msvcrt(Windows)，两平台 Python 标准库皆在，
  所以走到本模块 = 锁必然可用；bash 面无 flock(1) 且无 python 时由脚本拒绝写）。
"""

import json
import os
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


def lock_append(gzx_dir, target, data):
    """锁住 gzx_dir/.lock 后追加 data 到对应文件。返回 (rc, message)。
    只追加：唯一的写模式是 'ab'，代码里不存在截断分支。"""
    if target not in TARGETS:
        return 21, "unknown target: %s" % target
    if not data:
        return 21, "empty stdin"
    os.makedirs(gzx_dir, exist_ok=True)
    lockpath = os.path.join(gzx_dir, ".lock")
    targetpath = os.path.join(gzx_dir, TARGETS[target])
    try:
        with open(lockpath, "a+") as lf:
            _lock(lf)
            try:
                with open(targetpath, "ab") as f:   # 只追加：'ab'
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
            finally:
                lf.close()
    except OSError as e:
        return 23, "write failed: %s" % e
    return 0, "ok %s (+%d bytes)" % (targetpath, len(data))


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
