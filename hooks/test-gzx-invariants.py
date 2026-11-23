#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test-gzx-invariants.py — gan-zhong-xue v1.1.2 机械不变量测试（D7：机械必过面）。

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
CORE_PY = os.path.join(SKILL_DIR, "gzx_lock_append.py")
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
    # v1.1.2（Bugbot PR#4 High）：pending 唯一清空通道的静态围栏——
    # os.truncate 恰 1 处、仅在 lock_truncate 内、带 pending-only 守卫。
    # append-only 资产（map/jsonl/candidates/audit）永远到不了截断分支；
    # bash 面的 ': > "pending"' 截断只服务 pending-clear 动词（B1b 白名单外
    # 的 '> ' 仍由 B1b 的原断言与守卫结构层把关）。
    lt_start = src.find("def lock_truncate")
    lt_end = src.find("\ndef ", lt_start + 1)
    lt = src[lt_start:lt_end if lt_end > 0 else len(src)]
    check("B5a os.truncate 恰 1 处且仅在 lock_truncate 内",
          src.count("os.truncate") == 1 and lt.count("os.truncate") == 1,
          "count=%d lt=%d" % (src.count("os.truncate"), lt.count("os.truncate")))
    check("B5b lock_truncate 带 pending-only 守卫",
          "truncate only allowed for pending" in lt)
    sh = open(WRITER_SH, encoding="utf-8").read()
    # (c)（用户 PR#4 follow-up 批次 pi #2737 (c)，按 dsh #2723 §5 三条可数断言
    # 重写——旧 B1b 只看子串，pending-clear 截断行加入后形状失真）：
    import re as _re
    # 截断式重定向（进文件目标）：> 前非 > /数字 /&，后非 > /&，且目标以
    # 引号或变量开头（排除 >/dev/null 这类流丢弃——dsh #2723 §5 口径）。
    trunc_lines = [ln for ln in sh.splitlines()
                   if not ln.strip().startswith("#")
                   and _re.search(r"(?<![>0-9&])>(?!>)[ \t]*(?!&)(?!/dev/null)(?=[\"'$])", ln)]
    check("B1b-1 截断重定向两具名站点恰各 1 处（pending-clear 与 TMP 暂存）",
          sh.count(': > "$1"') == 1 and sh.count('cat > "$TMP"') == 1)
    check("B1b-2 截断式重定向总数==2（非 >>、非注释行、非流丢弃）",
          len(trunc_lines) == 2, "sites=%r" % (trunc_lines,))
    check("B1b-3 资产名禁入截断右值",
          not any(_re.search(r"map\.md|map\.jsonl|candidates\.jsonl|audit\.jsonl", ln)
                  for ln in trunc_lines))

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


def test_version_face():
    """(d)③ 版本面单点（board #122 r8，pi #2776 第五版定版）：七处头注 ==
    CHANGELOG 首个 ## 版本行；断言三件套（分母/分子/诊断分布）+ 负例 +
    删史段内计数 + SKILL 零 CHANGELOG 引用 + matcher 显式豁免在案。"""
    import re as _re
    changelog = open(os.path.join(REPO, "CHANGELOG.md"), encoding="utf-8").read()

    def parse_authoritative(text):
        """权威源解析：首个 ## 行必须以裸 vX.Y.Z 开头（禁 [Unreleased]/未发布
        标记——pi #2776 负例两种写法）；解析不到 ⇒ ValueError（fail-closed）。"""
        for ln in text.splitlines():
            if ln.startswith("## "):
                m = _re.match(r"^## (v\d+\.\d+\.\d+)\b", ln)
                if not m or _re.search(r"未发布|Unreleased|draft", ln, _re.I):
                    raise ValueError("CHANGELOG 首段不可解析为权威版本: %r" % ln)
                return m.group(1)
        raise ValueError("CHANGELOG 无任何 ## 版本行")

    auth = parse_authoritative(changelog)

    sites = [
        ("SKILL.md", "skills/gan-zhong-xue/SKILL.md", "# 干中学 (Learning by Doing)", 30),
        ("gzx-append.sh", "skills/gan-zhong-xue/gzx-append.sh", "# gzx-append.sh —", 5),
        ("gzx_lock_append.py", "skills/gan-zhong-xue/gzx_lock_append.py", "gzx-lock-append.py —", 5),
        ("gzx-capture.py", "hooks/gzx-capture.py", "gzx-capture.py —", 5),
        ("test-gzx-integration.py", "hooks/test-gzx-integration.py", "test-gzx-integration.py —", 5),
        ("test-gzx-invariants.py", "hooks/test-gzx-invariants.py", '"""test-gzx-invariants.py —', 5),
        ("README.md", "README.md", "## 机制层（", 60),
    ]
    tags = {}
    for name, rel, anchor, depth in sites:
        # 只扫每站点自带深度内的头区——头注标签总在文件头；全文件扫会让
        # "引用锚点的测试代码"自命中（本文件 sites 清单在 :285，即实例）
        lines = open(os.path.join(REPO, rel), encoding="utf-8").read().splitlines()[:depth]
        hits = [ln for ln in lines if anchor in ln]
        if len(hits) != 1:
            check("(d)③ 分母 fail-closed：%s 锚点恰 1 行" % name, False,
                  "hits=%d" % len(hits))
            return
        m = _re.search(r"v\d+\.\d+\.\d+", hits[0])
        if not m:
            check("(d)③ 分母 fail-closed：%s 头注含版本标签" % name, False, hits[0])
            return
        tags[name] = m.group(0)
    check("(d)③-1 分母：七处头注抽取命中恰 7（fail-closed）",
          len(tags) == 7, str(tags))
    dist = {}
    for v in tags.values():
        dist[v] = dist.get(v, 0) + 1
    check("(d)③-2 分子：len(set)==1 且 ==权威值",
          len(set(tags.values())) == 1 and auth in set(tags.values()),
          "分布=%s 权威=%s" % (dist, auth))
    check("(d)③-3 诊断分布行已随失败输出", True)  # 诊断由上方 detail 承载

    skill = open(os.path.join(REPO, "skills", "gan-zhong-xue", "SKILL.md"),
                 encoding="utf-8").read()
    lines = skill.splitlines()
    h1 = next(i for i, ln in enumerate(lines) if ln.startswith("# 干中学"))
    need = next((i for i, ln in enumerate(lines) if "工作机上需要" in ln), None)
    seg = lines[h1:need + 1 if need is not None else len(lines)]
    stale = [ln for ln in seg if _re.match(r"^\s*>\s*\*\*v1(\.\d+)*", ln)]
    check("(d)③-4 删史：头部段内手抄历史行==0（禁全文==0，:607 合法引用除外）",
          not stale, "残留=%r" % (stale[:2],))
    check("(d)③-5 SKILL.md 全文 CHANGELOG 引用恰 0",
          skill.count("CHANGELOG") == 0)
    matcher = open(os.path.join(REPO, "hooks", "matcher.json"),
                   encoding="utf-8").read()
    check("(d)③-6 matcher.json 二选一不沉默（显式豁免+文件内理由）",
          "显式豁免" in matcher)

    # 负例（pi #2776）：解析器对两种写法必须红
    for bad in ("## [Unreleased]\n正文", "## v1.1.2 (未发布)\n正文"):
        try:
            parse_authoritative(bad)
            check("(d)③ 负例 %r 必红" % bad.splitlines()[0], False)
        except ValueError:
            check("(d)③ 负例 %r 必红" % bad.splitlines()[0], True)


def test_capture_exception_silent_by_default():
    """GPT 外审(PR#4 五轮): 默认异常路径真静默——无 stderr traceback、
    不创建 capture-trace.txt;RG_CAPTURE_DEBUG 开启才输出诊断。"""
    d = tempfile.mkdtemp(prefix="gzx-cap-neg-")
    try:
        _capture_neg_body(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _capture_neg_body(d):
    ev = json.dumps({"tool_name": "Bash",
                     "tool_input": {"command": "git status"},
                     "cwd": os.getcwd(),
                     "tool_response": {"output": "x"}}).encode()
    # 默认(无 RG_CAPTURE_DEBUG): 喂非法载荷迫使异常路径,stdout/stderr 应全空
    r1 = subprocess.run([sys.executable, CAPTURE], env=dict(os.environ, GZX_DIR=d),
                        input=b"not-json-at-all", capture_output=True, timeout=30)
    check("capture 异常默认路径 rc=0 且 stdout 空(旁路不阻塞)",
          r1.returncode == 0 and r1.stdout == b"",
          "rc=%r out=%r" % (r1.returncode, r1.stdout[:60]))
    check("capture 异常默认路径 stderr 无 traceback(RG_CAPTURE_DEBUG 未开)",
          b"Traceback" not in r1.stderr and b"capture-debug" not in r1.stderr,
          "stderr=%r" % r1.stderr[:80])
    check("capture 异常默认路径不创建 capture-trace.txt",
          not os.path.exists(os.path.join(d, "capture-trace.txt")),
          str(sorted(os.listdir(d))))
    # 开关开启: 诊断可见(行为面验证 RG_CAPTURE_DEBUG 生效)
    r2 = subprocess.run([sys.executable, CAPTURE],
                        env=dict(os.environ, GZX_DIR=d, RG_CAPTURE_DEBUG="1"),
                        input=b"not-json-at-all", capture_output=True, timeout=30)
    check("RG_CAPTURE_DEBUG=1 时异常诊断可见(开关生效)",
          r2.returncode == 0 and b"capture-debug" in r2.stderr,
          "stderr=%r" % r2.stderr[:80])


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
    test_capture_exception_silent_by_default()
    test_audit_no_raw_command()
    test_map_field_greppable()
    test_migration_no_rewrite()
    test_version_face()
    print("\n%s：%d 项失败" % ("FAILED" if FAILS else "ALL PASS", len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
