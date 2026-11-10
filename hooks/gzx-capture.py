#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gzx-capture.py — 提交静默捕获 hook（v1.1.0，board thread #80 C' 定案 + GPT
PR #4 审查 P1-5 收口）。

定位：零打扰。PostToolUse 命中 git commit 时，不向用户说任何话，只往
~/.gan-zhong-xue/candidates.jsonl 追加一条 {sha, add, del, event, seq, ts}。
提醒只在下次 skill 被唤起时由第 -1 步兑现（"上次有 N 个提交没挖"）。

隐私（D1）：不写文件路径、不写仓库名——只有 sha 与增删行数。

v1.1.0（GPT PR #4 审查收口）：
  * P1-4：去重检查/序号计算/追加移入写入器核心的同一临界区
    （lock_append_commit_unique）——并发不再重复登记或序号错乱。
  * P1-5：commit 命令失败时不得捕获旧 HEAD——git commit 失败的方式远不止
    "nothing to commit"（钩子拒绝、合并冲突、消息编辑器中止…），一律以
    「工具输出中是否存在 git commit 的成功标记 `[分支名 <sha>]`」为准：
    提取不到该标记 → 不捕获；提取到 → 再与 rev-parse HEAD 复核一致才落账。
  * 残余如实：输出被宿主截断导致成功标记丢失 → 漏捕获（漏报可由用户在
    唤起时口头补挖，不产生错误记录——漏报优于误报）。

协议：stdin = 宿主 PostToolUse JSON；任何异常静默退出 0（捕获是旁路，绝不
阻塞工作流）。
"""

import json
import os
import re
import subprocess
import sys
import time

GZX_DIR = os.environ.get("GZX_DIR", os.path.expanduser("~/.gan-zhong-xue"))
CANDIDATES = os.path.join(GZX_DIR, "candidates.jsonl")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "skills", "gan-zhong-xue"))
try:
    from gzx_lock_append import lock_append_commit_unique  # 写入器核心（唯一实现）
except ImportError:
    lock_append_commit_unique = None

# git commit 成功输出的标记：[分支名 <sha>]（GPT round-18 P1-5）
_COMMIT_OK_RE = re.compile(r"\[[\w./-]+ +([0-9a-f]{7,40})\]")


def _core_path():
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.join(here, "..", "skills", "gan-zhong-xue", "gzx_lock_append.py")
    return cand if os.path.exists(cand) else os.path.join(GZX_DIR, "gzx_lock_append.py")


def _extract_committed_sha(text):
    """从 commit 命令输出提取成功标记里的 sha；无标记 = 提交未成功 → None。"""
    m = _COMMIT_OK_RE.search(text or "")
    return m.group(1) if m else None


def _already_captured(sha):
    """D5 去重键 (sha, event) 的读面快查（真正的原子去重在写入器核心锁内）。"""
    try:
        with open(CANDIDATES, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("sha") == sha and row.get("event") == "commit":
                    return True
    except OSError:
        pass
    return False




def _dbg(msg):
    if os.environ.get("RG_CAPTURE_DEBUG"):
        try:
            with open(os.path.join(GZX_DIR, "capture-trace.txt"), "a",
                      encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass

def main():
    try:
        raw = sys.stdin.read()
        event = json.loads(raw)
        ti = event.get("tool_input", {}) or {}
        cmd = (ti.get("command") or "").strip()

        # 只捕获 git commit（其他 Bash 调用无关）
        if not cmd.startswith("git") or "commit" not in cmd.split():
            _dbg("skip: not git commit")
            return
        resp_text = json.dumps(event.get("tool_response", {}), ensure_ascii=False)

        # P1-5：提交必须真的成功——输出里要有 [分支 <sha>] 成功标记
        sha_out = _extract_committed_sha(resp_text)
        _dbg("marker=%r" % sha_out)
        if not sha_out:
            _dbg("skip: no marker")
            return

        cwd = event.get("cwd") or None
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd,
                              capture_output=True, timeout=15)
        if head.returncode != 0:
            _dbg("skip: rev-parse")
            return
        sha = head.stdout.decode().strip()
        # 复核：输出标记的 sha 与当前 HEAD 一致（防旧 HEAD 误捕获）
        if not (sha.startswith(sha_out) or sha_out.startswith(sha[:len(sha_out)])):
            return
        _dbg("dedup-pass")
        if _already_captured(sha):
            _dbg("skip: dup")
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

        row = json.dumps({
            "sha": sha,
            "add": add,
            "del": ddel,
            "event": "commit",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, ensure_ascii=False)   # seq 由写入器核心在锁内现算(P1-4)

        data = (row + "\n").encode()
        _dbg("writer unique=%s" % (lock_append_commit_unique is not None))
        if lock_append_commit_unique is not None:
            lock_append_commit_unique(GZX_DIR, data)     # 原子去重+序号+追加
        else:
            core = _core_path()
            subprocess.run([sys.executable, core, "candidates"], input=data,
                           capture_output=True, timeout=20)
    except Exception as _e:
        try:
            import traceback as _tb
            _t = _tb.format_exc()
            sys.stderr.write("[capture-debug] exception:\n%s\n" % _t)
            with open(os.path.join(GZX_DIR, "capture-trace.txt"), "a",
                      encoding="utf-8") as _f:
                _f.write("EXCEPTION %s\n" % _t)
        except Exception:
            pass
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
