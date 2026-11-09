#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gzx-capture.py — 提交静默捕获 hook（v1.0.9，board thread #80 C' 定案 + D5 修正）。

定位：零打扰。PostToolUse 命中 git commit 时，不向用户说任何话，只往
~/.gan-zhong-xue/candidates.jsonl 追加一条 {sha, add, del, event, seq, ts}。
提醒只在下次 skill 被唤起时由第 -1 步兑现（"上次有 N 个提交没挖"）——捕获与打扰解耦，
把"每步提醒=噪声、用户会关"从根上去掉（pi #810）。

隐私（D1）：不写文件路径、不写仓库名——只有 sha 与增删行数（行数聚合，非路径明细）。
D5 修正（pi #810 接纳条目）：
  * 去重键 (sha, event)：hook 重试/宿主重放不会双计。
  * 排序键 = seq（事件序号，= 本文件行号，append-only 下单调）；不用时间戳（同秒歧义，
    board #49 判例）。
  * 每行带事件时间戳（dsh #811④：活体证明——空文件时"没提交"与"hook 没跑"不可分）。
  * 写入走 gzx-append.sh（单写者纪律；D2 目标集含 candidates）。

协议：stdin = 宿主 PostToolUse JSON；任何异常静默退出 0（捕获是旁路，绝不阻塞工作流）。
"""

import json
import os
import subprocess
import sys
import time

GZX_DIR = os.environ.get("GZX_DIR", os.path.expanduser("~/.gan-zhong-xue"))
CANDIDATES = os.path.join(GZX_DIR, "candidates.jsonl")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "skills", "gan-zhong-xue"))
try:
    from gzx_lock_append import lock_append   # 写入器不变量核心（唯一实现）
except ImportError:
    lock_append = None


def _core_path():
    """写入器不变量核心（独立部署兜底用）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.join(here, "..", "skills", "gan-zhong-xue", "gzx-lock-append.py")
    return cand if os.path.exists(cand) else os.path.join(GZX_DIR, "gzx-lock-append.py")


def _already_captured(sha):
    """D5 去重键 (sha, event)：同一提交重复触发只记一次。"""
    try:
        with open(CANDIDATES, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # 坏行跳过，不整份丢弃
                if row.get("sha") == sha and row.get("event") == "commit":
                    return True
    except OSError:
        pass
    return False


def main():
    try:
        raw = sys.stdin.read()
        event = json.loads(raw)
        ti = event.get("tool_input", {}) or {}
        cmd = (ti.get("command") or "").strip()

        # 只捕获 git commit（其他 Bash 调用无关）
        if not cmd.startswith("git") or "commit" not in cmd.split():
            return
        # commit 结果必须真成功（PostToolUse 会带 tool_response；失败提交不记）
        resp = json.dumps(event.get("tool_response", {}), ensure_ascii=False)
        if "nothing to commit" in resp or "not a git repository" in resp.lower():
            return

        cwd = event.get("cwd") or None
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd,
                             capture_output=True, timeout=15)
        if sha.returncode != 0:
            return
        sha = sha.stdout.decode().strip()
        if not sha:
            return
        if _already_captured(sha):
            return

        numstat = subprocess.run(
            ["git", "show", "--numstat", "--format=", "HEAD"], cwd=cwd,
            capture_output=True, timeout=15)
        add = ddel = 0
        for ln in numstat.stdout.decode(errors="replace").splitlines():
            parts = ln.split("\t")
            if len(parts) >= 2:
                add += int(parts[0]) if parts[0].isdigit() else 0
                ddel += int(parts[1]) if parts[1].isdigit() else 0

        seq = 1
        try:
            with open(CANDIDATES, "r", encoding="utf-8", errors="replace") as f:
                seq = sum(1 for ln in f if ln.strip()) + 1
        except OSError:
            pass

        row = json.dumps({
            "sha": sha,
            "add": add,
            "del": ddel,
            "event": "commit",
            "seq": seq,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, ensure_ascii=False)
        data = (row + "\n").encode()
        if lock_append is not None:
            lock_append(GZX_DIR, "candidates", data)     # 进程内直调核心（无子进程）
        else:
            core = _core_path()
            subprocess.run([sys.executable, core, "candidates"], input=data,
                           capture_output=True, timeout=20)
    except Exception:
        pass  # 旁路捕获：任何异常静默退出，绝不阻塞用户工作流
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
