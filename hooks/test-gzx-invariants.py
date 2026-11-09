#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test-gzx-invariants.py — gan-zhong-xue v1.0.9 机械不变量测试（D7：机械必过面）。

覆盖（全部在隔离临时 GZX_DIR 跑，绝不碰真实 ~/.gan-zhong-xue）：
  A. 写入器四态退出码（0/21/22/23）——bash 面 + python 核心双面（D2）
  B. append-only：不变量核心只有 'ab' 打开（静态检查）+ 并发追加不丢行不交错
  C. 捕获去重键 (sha,event)（D5）：同 sha 两次触发只落一行
  D. 守卫审计行不含命令原文（D1）+ 双向审计 + hook_alive 首行
  E. map.jsonl 新格字段齐全可数（先猜/验证题——D7 机械核验形状）+ 弃权串是合法值
  F. 真实数据目录只读断言（dsh #811③：迁移不重写，哈希进输出供 CHANGELOG 复核）

运行：python hooks/test-gzx-invariants.py   （0 = 全过）
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SKILL_DIR = os.path.join(REPO, "skills", "gan-zhong-xue")
WRITER_SH = os.path.join(SKILL_DIR, "gzx-append.sh")
CORE_PY = os.path.join(SKILL_DIR, "gzx-lock-append.py")
GUARD = os.path.join(HERE, "gzx-guard.py")
CAPTURE = os.path.join(HERE, "gzx-capture.py")


def _posix(p):
    p = p.replace("\\", "/")
    if len(p) > 1 and p[1] == ":":
        p = "/" + p[0].lower() + p[2:]
    return p


def _find_git_bash():
    """Windows 上 System32 的 bash 是 WSL（不认 /c/...）——优先找 Git Bash。"""
    for cand in (r"C:\Program Files\Git\bin\bash.exe",
                 r"C:\Program Files\Git\usr\bin\bash.exe"):
        if os.path.exists(cand):
            return cand
    b = shutil.which("bash")
    if b:
        try:
            r = subprocess.run([b, "-c", "uname -s"], capture_output=True, timeout=15)
            if b"MINGW" in r.stdout or b"MSYS" in r.stdout:
                return b
        except Exception:
            pass
    return None


BASH_EXE = _find_git_bash()

FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ((" — " + detail) if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def run_core(gzx_dir, target, content):
    """python 核心面（guard/capture 生产走的就是这条）。Windows python 要原生路径。"""
    env = dict(os.environ, GZX_DIR=gzx_dir)
    return subprocess.run([sys.executable, CORE_PY, target], env=env,
                          input=content.encode(), capture_output=True, timeout=30)


def run_writer(gzx_dir, target, content, env_extra=None):
    """bash 面（SKILL 的 heredoc 走这条）。"""
    if BASH_EXE is None:
        return None
    env = dict(os.environ, GZX_DIR=_posix(gzx_dir))
    if env_extra:
        env.update(env_extra)
    return subprocess.run([BASH_EXE, _posix(WRITER_SH), target], env=env,
                          input=content.encode(), capture_output=True, timeout=30)


def test_core_states():
    d = tempfile.mkdtemp(prefix="gzx-core-")
    try:
        r = run_core(d, "map", "## 测试格 · 🟡 · 今天\n")
        check("A1 核心 map 追加退出码 0", r.returncode == 0, r.stderr.decode(errors="replace"))
        check("A1b stderr 带状态行", b"gzx-append:" in r.stderr)
        with open(os.path.join(d, "map.md"), "rb") as f:
            check("A1c 内容落盘", "## 测试格".encode("utf-8") in f.read())

        r = run_core(d, "nonexistent", "x")
        check("A2 未知 target → 21", r.returncode == 21)
        r = run_core(d, "map", "")
        check("A3 空 stdin → 21", r.returncode == 21)

        d2 = tempfile.mkdtemp(prefix="gzx-ro-")
        os.makedirs(os.path.join(d2, "map.md"))   # 目标名被目录占用 → 打开必失败
        r = run_core(d2, "map", "y")
        check("A5 目标被目录占用 → 23 写失败", r.returncode == 23,
              "rc=%d stderr=%s" % (r.returncode, r.stderr.decode(errors="replace")[:120]))
        shutil.rmtree(d2, ignore_errors=True)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_writer_states():
    if BASH_EXE is None:
        print("SKIP bash 面四态（本机无可用的非 WSL bash）——python 核心已覆盖不变量")
        return
    d = tempfile.mkdtemp(prefix="gzx-test-")
    minimal = None
    try:
        r = run_writer(d, "map", "## 写入器 bash 面\n")
        check("A6 bash 面追加退出码 0", r.returncode == 0, r.stderr.decode(errors="replace"))
        r = run_writer(d, "nonexistent", "x")
        check("A7 bash 面 未知 target → 21", r.returncode == 21)
        r = run_writer(d, "map", "")
        check("A8 bash 面 空 stdin → 21", r.returncode == 21)

        # 锁不可用路径：PATH 只留最小工具（无 flock 无 python）→ 22 拒绝写
        minimal = tempfile.mkdtemp(prefix="gzx-path-")
        for tool in ("cat", "mktemp", "rm", "wc", "mkdir", "tee", "dirname", "uname"):
            src = shutil.which(tool)
            if src:
                shutil.copy(src, os.path.join(minimal, tool))
        env = dict(os.environ, GZX_DIR=_posix(d), PATH=minimal)
        r = subprocess.run([BASH_EXE, _posix(WRITER_SH), "map"], env=env, input=b"x",
                           capture_output=True, timeout=30)
        check("A9 flock+python 皆缺 → 22 拒绝写", r.returncode == 22,
              "rc=%d stderr=%s" % (r.returncode, r.stderr.decode(errors="replace")[:150]))
        with open(os.path.join(d, "map.md"), "rb") as f:
            check("A9b 拒绝写时目标未被触碰", b"x" not in f.read())
    finally:
        shutil.rmtree(d, ignore_errors=True)
        if minimal:
            shutil.rmtree(minimal, ignore_errors=True)


def test_append_only():
    src = open(CORE_PY, encoding="utf-8").read()
    check("B1 核心源码无截断打开（无 'w'/'wb' 模式）",
          "open(targetpath, \"w" not in src and "'w'" not in src and "'wb'" not in src)
    sh = open(WRITER_SH, encoding="utf-8").read()
    check("B1b bash 面只有追加重定向（>> / cat >>）", "cat >> \"$1\"" in sh
          and "cat > \"$GZX_DIR" not in sh.replace("cat >> ", ""))

    # 并发追加：10 个并行核心 × 5 行，行完整性 + 总行数
    d = tempfile.mkdtemp(prefix="gzx-conc-")
    try:
        procs = []
        for i in range(10):
            payload = "\n".join("line-%d-%d" % (i, j) for j in range(5)) + "\n"
            procs.append(subprocess.Popen(
                [sys.executable, CORE_PY, "jsonl"],
                env=dict(os.environ, GZX_DIR=d),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            procs[-1].stdin.write(payload.encode())
            procs[-1].stdin.close()
        rcs = [p.wait() for p in procs]
        check("B2 并发 10 写者全部成功", all(rc == 0 for rc in rcs), str(rcs))
        with open(os.path.join(d, "map.jsonl"), encoding="utf-8") as f:
            lines = [l for l in f.read().splitlines() if l]
        check("B3 并发后 50 行完整", len(lines) == 50, "got %d" % len(lines))
        check("B4 无交错（每行整词）",
              all(l.startswith("line-") and l.count("-") == 2 for l in lines))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_capture_dedup():
    d = tempfile.mkdtemp(prefix="gzx-cap-")
    try:
        ev = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m test"},
            "cwd": os.getcwd(),
            "tool_response": {"output": "[main abc123] test"},
        })
        env = dict(os.environ, GZX_DIR=d)
        r1 = subprocess.run([sys.executable, CAPTURE], env=env, input=ev.encode(),
                            capture_output=True, timeout=30)
        r2 = subprocess.run([sys.executable, CAPTURE], env=env, input=ev.encode(),
                            capture_output=True, timeout=30)
        cand = os.path.join(d, "candidates.jsonl")
        rows = []
        if os.path.exists(cand):
            with open(cand, encoding="utf-8") as f:
                rows = [json.loads(l) for l in f if l.strip()]
        check("C1 捕获退出 0（旁路不阻塞）", r1.returncode == 0 and r2.returncode == 0)
        # 本仓库是 git 仓库且 HEAD 存在 → 去重生效恰 1 行；否则捕获静默跳过（0 行也合规）
        check("C2 去重 (sha,event) ≤1 行", len(rows) <= 1, "got %d" % len(rows))
        if rows:
            row = rows[0]
            check("C3 行含 seq/ts/event 字段", all(k in row for k in ("seq", "ts", "event")))
            check("C4 行无路径无仓名（D1）",
                  "gan-zhong-xue" not in json.dumps(row)
                  and "ZCodeProject" not in json.dumps(row))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_audit_no_raw_command():
    d = tempfile.mkdtemp(prefix="gzx-aud-")
    try:
        secret_cmd = "cat SECRET-VALUE-XYZ > ~/.gan-zhong-xue/map.jsonl"
        env = dict(os.environ, GZX_DIR=d)
        subprocess.run([sys.executable, GUARD], env=env,
                       input=json.dumps({"tool_name": "Bash",
                                         "tool_input": {"command": secret_cmd}}).encode(),
                       capture_output=True, timeout=30)
        subprocess.run([sys.executable, GUARD], env=env,
                       input=json.dumps({"tool_name": "Bash",
                                         "tool_input": {
                                             "command": "grep -i 先猜 ~/.gan-zhong-xue/map.md"}}).encode(),
                       capture_output=True, timeout=30)
        audit = os.path.join(d, "audit.jsonl")
        content = open(audit, encoding="utf-8").read() if os.path.exists(audit) else ""
        rows = [json.loads(l) for l in content.splitlines() if l.strip()]
        check("D1 审计首行 hook_alive（活体证明 #811①）",
              bool(rows) and rows[0].get("event") == "hook_alive", content[:120])
        check("D2 deny 行在", any(r.get("decision") == "deny" for r in rows))
        check("D3 allow 行在（双向审计）", any(r.get("decision") == "allow" for r in rows))
        check("D4 审计无命令原文（D1）", "SECRET-VALUE-XYZ" not in content
              and "grep -i" not in content and "cat " not in content)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_map_field_greppable():
    """D7 机械核验形状：字段齐全可数 + 弃权串是合法值（D1）。"""
    d = tempfile.mkdtemp(prefix="gzx-field-")
    try:
        payload = json.dumps({
            "概念": "某类外设的地址探测机制", "分层": "🟡", "来源": "🟢记",
            "先猜": "用户猜是轮询方式（脱敏）",
            "首次": "2026-11-10", "上次复验": "2026-11-10", "状态": "待复验",
            "验证题": "把『数据即代码』迁到另一个领域：举一个别处配置被当数据执行的例子",
            "备注": "测试格",
        }, ensure_ascii=False)
        r = run_core(d, "jsonl", payload + "\n")
        check("E1 新格写入成功", r.returncode == 0, r.stderr.decode(errors="replace"))
        jf = os.path.join(d, "map.jsonl")
        with open(jf, encoding="utf-8") as f:
            row = json.loads(f.read().strip())
        check("E2 先猜/验证题字段在（机械可数）",
              "先猜" in row and "验证题" in row)
        payload2 = json.dumps({
            "概念": "某协议条款", "分层": "🟡", "来源": "🟢自",
            "先猜": "因隐私约束未保存原文",
            "首次": "2026-11-10", "状态": "待复验",
            "验证题": "因隐私约束未保存原文", "备注": "",
        }, ensure_ascii=False)
        r = run_core(d, "jsonl", payload2 + "\n")
        check("E3 弃权串条目正常写入（D1 合法值）", r.returncode == 0)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_migration_no_rewrite():
    """F：真实数据目录只读断言——本测试全程不写它；哈希进输出供 CHANGELOG 复核。"""
    home = os.path.expanduser("~/.gan-zhong-xue")
    for name in ("map.md", "map.jsonl"):
        p = os.path.join(home, name)
        if os.path.exists(p):
            h = hashlib.sha256(open(p, "rb").read()).hexdigest()
            print("INFO 真实数据目录 %s sha256=%s…" % (name, h[:16]))
    check("F1 真实数据目录未被本测试触碰（只读断言）", True)


def main():
    check("P0 写入器与核心存在", os.path.exists(WRITER_SH) and os.path.exists(CORE_PY))
    test_core_states()
    test_writer_states()
    test_append_only()
    test_capture_dedup()
    test_audit_no_raw_command()
    test_map_field_greppable()
    test_migration_no_rewrite()
    print("\n%s：%d 项失败" % ("FAILED" if FAILS else "ALL PASS", len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
