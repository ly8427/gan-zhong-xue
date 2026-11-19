#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test-gzx-integration.py — v1.1.0 集成回归（GPT PR #4 审查 P2-8：真实 Git 工作流）。

覆盖（全部在隔离临时目录跑，绝不碰真实 ~/.gan-zhong-xue 或用户仓库）：
  A. 首次安装引导（P1-1）：数据目录无写入器 → 从技能目录复制 → 写入器可用
  B. 守卫引导豁免（P1-2）：cp 安装/mkdir/chmod 放行；拷数据出目录仍拦
  C. 守卫审计首行（P2-7）：两次审计 → hook_alive 恰 1 行
  D. 真实 git 提交捕获（P1-5）：真 commit → 捕获且 sha==HEAD；重试 → 去重；
     失败提交（输出无成功标记）→ 不捕获
  E. 并发捕获（P1-4）：双进程同 sha → 恰 1 行、seq 连续
  F. pending 走写入器（P1-3）：追加语义 + 四态退出码

运行：python hooks/test-gzx-integration.py   （0 = 全过）
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SKILL_DIR = os.path.join(REPO, "skills", "gan-zhong-xue")
GUARD = os.path.join(HERE, "gzx-guard.py")
CAPTURE = os.path.join(HERE, "gzx-capture.py")

FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ((" — " + detail) if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def _posix(p):
    """Git Bash 形态(/c/...)——本文件的 bash 调用全部显式锁 Git Bash。
    (F 组实证:裸 'bash' 按 PATH 解析到 MSYS bash 时 /mnt 形态不可读,
    /c 形态才是 Git Bash 的挂载点;WSL 形态只属于 wsl -e 执行桩。)"""
    p = p.replace("\\", "/")
    if len(p) > 1 and p[1] == ":":
        p = "/" + p[0].lower() + p[2:]
    return p


def run_capture(env, payload):
    return subprocess.run([sys.executable, CAPTURE], env=env,
                          input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          capture_output=True, timeout=30)


def main():
    # ---- 场景工作区：真实 git 仓库 + 隔离数据目录 + 模拟插件技能目录 ----
    work = tempfile.mkdtemp(prefix="gzx-int-")
    gzx_dir = os.path.join(work, "data", ".gan-zhong-xue")   # 故意缺写入器（场景 A 前提）
    repo = os.path.join(work, "repo")
    skill_dir = os.path.join(work, "plugins", "gan-zhong-xue", "skills", "gan-zhong-xue")
    os.makedirs(os.path.join(gzx_dir, "data"), exist_ok=True)
    os.makedirs(repo, exist_ok=True)
    os.makedirs(skill_dir, exist_ok=True)
    # 技能目录放一份写入器（模拟插件分发件）
    for f in ("gzx-append.sh", "gzx_lock_append.py"):
        shutil.copy(os.path.join(SKILL_DIR, f), os.path.join(skill_dir, f))

    # ---- A: 首次安装引导（P1-1）----
    # Windows 裸 'bash' 按 PATH 解析（System32\bash.exe=WSL 时读不了 /c/ 路径）——
    # 钩子的部署形态是 Git Bash（VBS/Host 同款），测试显式锁定同一解释器。
    GITBASH = r"C:\Program Files\Git\bin\bash.exe"
    app_dst = os.path.join(gzx_dir, "gzx-append.sh")
    check("A0 场景前提:数据目录无写入器", not os.path.exists(app_dst))
    # SKILL 第 -1 步引导块的模型侧动作:cp 技能目录两件 → 数据目录
    shutil.copy(os.path.join(skill_dir, "gzx-append.sh"), app_dst)
    shutil.copy(os.path.join(skill_dir, "gzx_lock_append.py"), os.path.join(gzx_dir, "gzx_lock_append.py"))
    env = dict(os.environ, GZX_DIR=gzx_dir, RB_ROLLOUT_DIR=os.path.join(work, "roll"))
    os.makedirs(env["RB_ROLLOUT_DIR"], exist_ok=True)
    r = subprocess.run([GITBASH, _posix(app_dst), "pending"],
                       input=b"bootstrap check\n", capture_output=True, env=env, timeout=30)
    check("A1 部署后的写入器可用", r.returncode == 0, r.stderr.decode(errors="replace")[:150])

    # ---- B: 守卫引导豁免与反向拦截(导入前先把 GZX_DIR 指向隔离目录,防误写真实审计) ----
    os.environ["GZX_DIR"] = gzx_dir
    import importlib.util
    spec = importlib.util.spec_from_file_location("gzx_guard", GUARD)
    gmod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gmod)
    check("B1 cp 安装写入器 → 放行",
          gmod.classify("cp %s/gzx-append.sh %s/gzx-append.sh" % (_posix(skill_dir), _posix(gzx_dir))).allow)
    check("B2 mkdir 初始化 → 放行",
          gmod.classify("mkdir -p %s" % _posix(gzx_dir)).allow)
    check("B3 chmod 收权限 → 放行",
          gmod.classify("chmod 700 %s" % _posix(gzx_dir)).allow)
    d = gmod.classify("cp %s/map.md /tmp/evil.md" % _posix(gzx_dir))
    check("B4 拷数据出受保护目录 → 仍拦", not d.allow)

    # ---- C: 审计首行 hook_alive 恰一行(P2-7) ----
    for i in range(2):
        gmod._audit("Bash", True, "readonly-ok")
    audit = os.path.join(gzx_dir, "audit.jsonl")
    with open(audit, encoding="utf-8") as f:
        rows = [json.loads(l) for l in f if l.strip()]
    alive = [r_ for r_ in rows if r_.get("event") == "hook_alive"]
    check("C1 两次审计恰一行 hook_alive", len(alive) == 1, "got %d" % len(alive))
    check("C2 deny/allow 双向在案",
          any(r_.get("decision") == "allow" for r_ in rows))

    # ---- D+E: 真实 git 提交捕获 / 去重 / 失败提交不捕获 / 并发 ----
    def git(*args):
        return subprocess.run(["git", "-C", repo] + list(args),
                              capture_output=True, timeout=30)

    git("init", "-b", "main")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    with open(os.path.join(repo, "base.txt"), "w", encoding="utf-8") as f:
        f.write("base\n")
    git("add", "base.txt")
    git("commit", "-m", "base")            # 前置提交:此后 HEAD 存在
    with open(os.path.join(repo, "a.txt"), "w", encoding="utf-8") as f:
        f.write("hello\n")
    git("add", "a.txt")
    git("commit", "-m", "test")            # 被捕获的那个真实提交
    short_sha = git("rev-parse", "--short", "HEAD").stdout.decode().strip()

    def capture_env():
        return dict(os.environ, GZX_DIR=gzx_dir)

    payload_ok = {                       # dict——序列化由 run_capture 负责(勿二次 dumps)
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -m test"},
        "cwd": repo,
        "tool_response": {"output": "[main %s] test" % short_sha},
    }
    r1 = run_capture(capture_env(), payload_ok)
    head = git("rev-parse", "HEAD").stdout.decode().strip()
    cand = os.path.join(gzx_dir, "candidates.jsonl")
    rows = [json.loads(l) for l in open(cand, encoding="utf-8") if l.strip()] \
        if os.path.exists(cand) else []
    check("D1 真提交被捕获且 sha==HEAD", len(rows) == 1 and rows[0]["sha"] == head)
    check("D2 无路径无仓名(D1)", "repo" not in json.dumps(rows))

    # 重试同提交 → 去重(经写入器核心锁内)
    r2 = run_capture(capture_env(), payload_ok)
    rows = [json.loads(l) for l in open(cand, encoding="utf-8") if l.strip()]
    check("D3 重试同 sha 去重", len(rows) == 1)

    # 失败提交(输出无 [分支 sha] 成功标记) → 不捕获,也不把旧 HEAD 记成新提交(P1-5)
    payload_fail = {                     # dict——同上
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -m second"},
        "cwd": repo,
        "tool_response": {"output": "On branch main\nnothing to commit, working tree clean"},
    }
    run_capture(capture_env(), payload_fail)
    rows = [json.loads(l) for l in open(cand, encoding="utf-8") if l.strip()]
    check("D4 失败提交(无成功标记)不捕获", len(rows) == 1)

    # 并发:双进程同 sha → 恰 1 行(P1-4 原子性)
    sys.path.insert(0, SKILL_DIR)
    import importlib.util as _iu
    spec2 = _iu.spec_from_file_location("gzx_lock_append",
                                        os.path.join(SKILL_DIR, "gzx_lock_append.py"))
    core = _iu.module_from_spec(spec2)
    spec2.loader.exec_module(core)
    row = json.dumps({"sha": head, "add": 1, "del": 0, "event": "commit",
                      "ts": "2026-11-11T00:00:00Z"}, ensure_ascii=False)
    procs = []
    for _ in range(2):
        pr = subprocess.Popen([sys.executable, "-c",
                               "import sys, json\\n"
                               "sys.path.insert(0, r'" + SKILL_DIR + "')\\n"
                               "import gzx_lock_append as c\\n"
                               "rc, msg = c.lock_append_commit_unique("
                               "r'" + gzx_dir + "', sys.stdin.read())\\n"
                               "sys.stderr.write(str(rc))"],
                              stdin=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        pr.stdin.write((row + "\\n").encode("utf-8"))
        pr.stdin.close()
        procs.append(pr)
    for pr in procs:
        pr.communicate(timeout=30)
    rows = [json.loads(l) for l in open(cand, encoding="utf-8") if l.strip()]
    check("E1 并发双进程同 sha 恰 1 行", len(rows) == 1, "got %d" % len(rows))
    seqs = sorted(r_.get("seq") for r_ in rows)
    check("E2 seq 连续(锁内现算)", seqs == [1])

    # ---- F: pending 走写入器(P1-3) ----
    r = run_capture(env, {"tool_name": "Read"})  # 触发一次无关调用,确认 pending 仍空
    pend = os.path.join(gzx_dir, "pending.md")
    rr = subprocess.run([GITBASH, _posix(app_dst), "pending"],
                        input="- 未挖点:某机制的并发写安全\n".encode("utf-8"),
                        capture_output=True, env=env, timeout=30)
    check("F1 pending 经写入器追加成功", rr.returncode == 0)
    if rr.returncode != 0:
        print("DBG F1 rc", rr.returncode, "stderr",
              rr.stderr.decode("utf-8", "replace")[:300])
    with open(pend, encoding="utf-8") as f:
        check("F2 pending 内容在", "未挖点" in f.read())

    # ---- G: 文档字节验收（board #122 M2/M2-ext：红绿两侧都钉在 SKILL.md 原文上，
    # 不许用"实现者手写变体"当用例——dsh #2656 §3 / grok #2658 同款验收口径）----
    # 独立守卫实例（默认 HOME 数据目录口径；classify 是纯函数，不写盘）
    saved_gzx_dir = os.environ.pop("GZX_DIR", None)
    spec3 = importlib.util.spec_from_file_location("gzx_guard_docbytes", GUARD)
    gdoc = importlib.util.module_from_spec(spec3)
    spec3.loader.exec_module(gdoc)
    if saved_gzx_dir is not None:
        os.environ["GZX_DIR"] = saved_gzx_dir

    skill_md = open(os.path.join(SKILL_DIR, "SKILL.md"), encoding="utf-8").read()
    step_m1 = skill_md.split("### 第 -1 步", 1)[1].split("\n### ", 1)[0]
    cmds = []
    for blk in re.findall(r"```bash\n(.*?)```", step_m1, re.S):
        for ln in blk.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                cmds.append(ln.replace("<SKILL_DIR>", "/tmp/gzx-fake-skill-dir"))
    bad = [(c, gdoc.classify(c)) for c in cmds if not gdoc.classify(c).allow]
    check("G1 第 -1 步文档字节逐条 classify 全 allow", not bad,
          "; ".join("%r -> %s" % (c, d.rule) for c, d in bad))
    check("G2 第 -1 步文档命令数≥10（两个块都被抽到）", len(cmds) >= 10, "got %d" % len(cmds))

    # 正式保存 heredoc：从全文 bash 围栏里逐字抽「写入器头 + 定界符终结」的完整命令。
    # 围栏常在 markdown 列表内带缩进——那是排版缩进不是命令字节，真发给 shell 的
    # 是 dedent 后的形态（bash 语义：空格缩进的定界符行不会终结 heredoc，守卫
    # 正则因此保持严格，这里必须先 dedent 再喂——钉的就是模型实际会发的那串）。
    import textwrap
    save_cmds = []
    for blk in re.findall(r"```bash\n(.*?)```", skill_md, re.S):
        # CRLF 归一是抽取器的责任、不是判据的例外（board #122 dsh #2662 §3）——
        # 工作树可能是 CRLF，bash 只认 \n，喂 classify 前必须归一
        lines = textwrap.dedent(blk).replace("\r\n", "\n").replace("\r", "\n").splitlines()
        i = 0
        while i < len(lines):
            m = re.match(r"^\s*~/.gan-zhong-xue/gzx-append\.sh (map|jsonl|pending) <<'([A-Za-z_]\w*)'\s*$",
                         lines[i])
            if m:
                delim = m.group(2)
                j = i + 1
                while j < len(lines) and lines[j].strip() != delim:
                    j += 1
                if j < len(lines):
                    save_cmds.append("\n".join(lines[i:j + 1]))
                i = j
            i += 1
    bad = [(c.splitlines()[0].strip()[:44], gdoc.classify(c).rule)
           for c in save_cmds if not gdoc.classify(c).allow]
    check("G3 正式保存 heredoc 原文逐字 classify 全 allow（抽到≥2 处）",
          len(save_cmds) >= 2 and not bad,
          "saves=%d; %s" % (len(save_cmds), bad))

    # 负向：M2 收紧后必须 deny（dsh #2654 §3/§4 实证形状）
    check("G4 cp 假写入器覆盖 map.md → deny",
          not gdoc.classify("cp /tmp/gzx-append ~/.gan-zhong-xue/map.md").allow)
    check("G5 cp 任意文件覆盖写入器本体 → deny",
          not gdoc.classify("cp /tmp/evil ~/.gan-zhong-xue/gzx-append.sh").allow)
    trailing = "~/.gan-zhong-xue/gzx-append.sh map <<'E'\nx\nE\nrm ~/.gan-zhong-xue/map.md"
    check("G6 heredoc 定界符后接命令 → deny", not gdoc.classify(trailing).allow)
    check("G7 旧自检 bash 前缀+临时目录整段 → deny",
          not gdoc.classify('TMPD=$(mktemp -d 2>/dev/null) && { echo ok | GZX_DIR="$TMPD" '
                            'bash "$HOME/.gan-zhong-xue/gzx-append.sh" pending >/dev/null 2>&1 ; '
                            'rm -rf "$TMPD" ; }').allow)
    # G8/G9:六条绑定里 grok #2661 实跑曾 allow 的既有漏 + dsh #2662 §1 提前终结形状
    check("G8 未引号定界符+$(…) 正文 → deny（B2 修粘接漏）",
          not gdoc.classify("~/.gan-zhong-xue/gzx-append.sh map <<EOF\n$(echo pwn)\nEOF").allow)
    check("G9 正文提前出现同形定界符行 → deny（B3）",
          not gdoc.classify("~/.gan-zhong-xue/gzx-append.sh map <<'E'\nhello\nE\n"
                            "rm ~/.gan-zhong-xue/map.md\nE").allow)

    print("\\n%s：%d 项失败" % ("FAILED" if FAILS else "ALL PASS", len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
