#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test-gzx-integration.py — v1.1.2 集成回归（GPT PR #4 审查 P2-8：真实 Git 工作流）。

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
import time
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
    # 技能目录放一份写入器（模拟插件分发件——A 段真实文件操作用；B1 的守卫判定
    # 自 r13 起要求来源=守卫所在插件包的真实技能目录，不再认任何临时假束）
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
          gmod.classify("cp %s/gzx-append.sh %s/gzx-append.sh" % (_posix(SKILL_DIR), _posix(gzx_dir))).allow)
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
    # 并发（v1.1.2 重写——Bugbot PR#4 Medium：旧版 worker 用 python -c 且换行
    # 写成字面 \\n，两个进程直接 SyntaxError 全灭；D1 的旧行让断言空过，
    # lock_append_commit_unique 的并发路径从未被测过。改为 worker 落临时
    # 文件（真换行）+ 回收 rc 证明真跑了 + 争用断言真正咬合）。
    worker_src = (
        "import sys\n"
        "sys.path.insert(0, r'" + SKILL_DIR + "')\n"
        "import gzx_lock_append as c\n"
        "rc, msg = c.lock_append_commit_unique(\n"
        "    r'" + gzx_dir + "', sys.stdin.buffer.read())\n"
        "sys.stderr.write(str(rc))\n"
    )
    worker_py = os.path.join(tempfile.mkdtemp(prefix="gzx_e1_"), "worker.py")
    with open(worker_py, "w", encoding="utf-8") as f:
        f.write(worker_src)

    def spawn_workers(row_objs):
        # GPT 外审(PR#4 六/七轮): 双阶段屏障——worker 启动后先写自己的 READY
        # 文件再等 GO；父进程等齐全部 READY(30s 超时)才放行 GO。Popen 返回≠
        # worker 就绪,单 GO 文件仍可能串行;双阶段才保证争用确定性发生。
        # 超时 ⇒ 杀 worker(其 rc 非 0 ⇒ 用例自然红)。
        # Bugbot 4661969381(82b3cc2): worker 等的是 GO 文件,不是 bar 目录——
        # 目录 mkdtemp 时已存在,等它=空等,GO 无人检查,屏障形同虚设。
        bar_dir = tempfile.mkdtemp(prefix="gzx-e2e-bar-")
        go_path = os.path.join(bar_dir, "GO")
        bootstrap = (
            "import os, sys, time, runpy\n"
            "go, worker, data, ready = sys.argv[1:5]\n"
            "open(ready, 'w').close()\n"
            "while not os.path.exists(go):\n"
            "    time.sleep(0.005)\n"
            "sys.argv = [worker, data]\n"
            "sys.stdin = open(data, 'r', newline='')\n"
            "runpy.run_path(worker, run_name='__main__')\n"
        )
        procs, readies, infiles = [], [], []
        for i, row_obj in enumerate(row_objs):
            infile = tempfile.NamedTemporaryFile(
                "wb", prefix="gzx-e2e-", suffix=".json", delete=False)
            infile.write((json.dumps(row_obj, ensure_ascii=False) + "\n").encode("utf-8"))
            infile.close()
            ready = os.path.join(bar_dir, "ready-%d" % i)
            readies.append(ready)
            infiles.append(infile.name)
            pr = subprocess.Popen(
                [sys.executable, "-c", bootstrap, go_path, worker_py,
                 infile.name, ready],
                stdin=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
            procs.append(pr)
        # 等齐全部 READY(30s);超时 ⇒ 杀 worker(rc 非 0 ⇒ 用例红,不留假绿)
        deadline = time.time() + 30
        while time.time() < deadline:
            if all(os.path.exists(r) for r in readies):
                break
            time.sleep(0.02)
        if not all(os.path.exists(r) for r in readies):
            for pr in procs:
                pr.kill()
        with open(go_path, "w") as _g:
            _g.write("go")
        outs = []
        for pr, infile_name in zip(procs, infiles):
            _, err = pr.communicate(timeout=30)
            outs.append((pr.returncode, err.decode("utf-8", "replace")))
        for infile_name in infiles:
            try:
                os.unlink(infile_name)
            except OSError:
                pass
        try:
            shutil.rmtree(bar_dir, ignore_errors=True)
        except OSError:
            pass
        return outs

    def cand_rows():
        return [json.loads(l) for l in open(cand, encoding="utf-8") if l.strip()]

    # E1 并发争用（不同 sha）：两 worker 同时在跑 → 两行都在
    rcs = spawn_workers([{"sha": head + "-e1a", "add": 1, "del": 0,
                          "event": "commit", "ts": "2026-11-11T00:00:00Z"},
                         {"sha": head + "-e1b", "add": 2, "del": 0,
                          "event": "commit", "ts": "2026-11-11T00:00:01Z"}])
    check("E1a 两 worker 真跑了(rc=0)", all(rc == 0 for rc, _ in rcs),
          "rcs=%r" % (rcs,))
    got = sorted(r_["sha"] for r_ in cand_rows()
                 if str(r_["sha"]).endswith(("-e1a", "-e1b")))
    check("E1b 并发两行都在(锁串行化)", len(got) == 2, "got %r" % (got,))
    rows = cand_rows()
    seqs = sorted(r_.get("seq") for r_ in rows)
    check("E2 seq 全文件唯一且连续(锁内现算)",
          seqs == list(range(1, len(rows) + 1)), "seqs=%r" % (seqs,))

    # E3 同 sha 并发竞态：两 worker 同 sha → 零新增（去重原子性在争用下成立）
    before = len(cand_rows())
    rcs = spawn_workers([{"sha": head, "add": 1, "del": 0,
                          "event": "commit", "ts": "2026-11-11T00:00:00Z"}
                         for _ in range(2)])
    check("E3a 两 worker 真跑了(rc=0)", all(rc == 0 for rc, _ in rcs),
          "rcs=%r" % (rcs,))
    check("E3b 同 sha 并发零新增(去重原子)", len(cand_rows()) == before,
          "新增 %d" % (len(cand_rows()) - before))

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

    # F3 pending 清空唯一通道（v1.1.2，Bugbot PR#4 High 端到端回归）：
    # pending-clear 清空 pending.md；append-only 资产 candidates.jsonl 原样。
    cand_before = open(cand, encoding="utf-8").read() if os.path.exists(cand) else ""
    rr = subprocess.run([GITBASH, _posix(app_dst), "pending-clear"],
                        capture_output=True, env=env, timeout=30)
    check("F3a pending-clear rc=0（写入器动词放行且锁内截断）", rr.returncode == 0,
          rr.stderr.decode("utf-8", "replace")[:200])
    check("F3b pending.md 已空", os.path.getsize(pend) == 0)
    check("F3c candidates.jsonl 原样（append-only 不受清空影响）",
          (open(cand, encoding="utf-8").read() if os.path.exists(cand) else "") == cand_before)

    # P2（用户 PR#4 follow-up）：插件版本双文件一致性——plugin.json 与
    # marketplace.json 条目版本不得漂移（1.0.8 vs 1.1.1 曾静默并存）。
    pj = json.load(open(os.path.join(REPO, ".claude-plugin", "plugin.json"),
                        encoding="utf-8"))
    mj = json.load(open(os.path.join(REPO, ".claude-plugin", "marketplace.json"),
                        encoding="utf-8"))
    plugs = mj.get("plugins") or []
    mv = next((p_.get("version") for p_ in plugs
               if p_.get("name") == pj.get("name")), None)
    # GPT 外审(grok #2812 flip 条件): 双文件互等但≠权威解析值必绿(1.1.1==1.1.1
    # 假绿)——P2 改为「两文件相等 且 == CHANGELOG 首个 ## vX.Y.Z 解析值」。
    chg_authority = None
    with open(os.path.join(REPO, "CHANGELOG.md"), encoding="utf-8") as _cf:
        for _ln in _cf:
            _m = re.match(r"^## (v\d+\.\d+\.\d+)", _ln)
            if _m:
                chg_authority = _m.group(1)
                break
    check("P2 插件版本双文件一致且等于 CHANGELOG 权威值",
          mv == pj.get("version")
          and pj.get("version") == (chg_authority or "").lstrip("v"),
          "plugin.json=%s marketplace.json=%s CHANGELOG=%s"
          % (pj.get("version"), mv, chg_authority))

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
                cmds.append(ln.replace("<SKILL_DIR>", _posix(SKILL_DIR)))
    # 抽取器变换清单（board #122 dsh #2675 §2，写死防误读）：
    #   {fence dedent（隐含于 splitlines+strip）, CRLF→LF, `<SKILL_DIR>` 占位符替换}
    # ——占位符字面含 <>，会被分词器切进重定向层使段长 ≠ 3（prefix:cp 假红），
    #   故替换是验收前提；断言占位符无残留（合法的 2>/dev/null 类重定向不算）。
    bad_xform = [c for c in cmds if "<SKILL_DIR" in c]
    check("G1a 抽取器变换后无 <SKILL_DIR> 占位符残留", not bad_xform,
          "; ".join(repr(c) for c in bad_xform))
    # r13(GPT 外审九轮 P1):守卫来源身份=守卫所在插件包的真实技能目录——占位符
    # 替换值必须是真插件目录（本测试即 SKILL_DIR=仓库技能目录），不再喂假束：
    # 假束（含 SKILL.md）自 r13 起一律 deny（见 G22），喂假束会反向假红。
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
                    # 围栏原文含定界符行后的那个换行——逐字喂（grok #2670 条件②：
                    # 围栏原文直接喂 classify 必须绿，不许用切行方式丢掉末尾换行）
                    save_cmds.append("\n".join(lines[i:j + 1]) + "\n")
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
    # G10:假写入器路径(首行不含保护目录)——身份层直判,不许经 out-of-scope 短路
    check("G10 /tmp 假写入器 + 正文提保护目录 → deny（身份层）",
          not gdoc.classify("/tmp/evil/gzx-append.sh map <<'E'\n~/.gan-zhong-xue/map.md\nE").allow)
    check("G11 相对路径假写入器 + 正文提保护目录 → deny（身份层）",
          not gdoc.classify("./gzx-append.sh map <<'E'\n~/.gan-zhong-xue/map.md\nE").allow)
    # G12/G13:grok #2670 两条——Git Bash 把 `E\r` 当定界符行（其后行照跑）必须 deny；
    # 定界符行后恰好一个换行（围栏原文形状）必须 allow
    check("G12 正文 E\\r 终结 + 其后接 rm → deny（\\r 归一后唯一性破）",
          not gdoc.classify("~/.gan-zhong-xue/gzx-append.sh map <<'E'\nE\r\n"
                            "rm ~/.gan-zhong-xue/map.md\nE").allow)
    check("G13 定界符行后恰好一个换行 → allow（围栏原文字节）",
          gdoc.classify("~/.gan-zhong-xue/gzx-append.sh map <<'E'\n## 概念\nE\n").allow
          and gdoc.classify("~/.gan-zhong-xue/gzx-append.sh map <<'E'\n## 概念\nE\n").rule.startswith("writer-heredoc"))
    check("G14 定界符行后两个换行 → deny（只放一个）",
          not gdoc.classify("~/.gan-zhong-xue/gzx-append.sh map <<'E'\n## 概念\nE\n\n").allow)
    # G15:dsh #2675 §5 请有 shell 方补的一格——<SKILL_DIR> 字面形状 deny、替换后 allow
    check("G15a <SKILL_DIR> 占位符字面 → deny（占位符不可直接执行）",
          not gdoc.classify("cp <SKILL_DIR>/gzx-append.sh ~/.gan-zhong-xue/gzx-append.sh").allow)
    check("G15b 占位符替换成实路径 → allow（G1 已覆盖,此处单独钉）",
          gdoc.classify("cp %s/gzx-append.sh ~/.gan-zhong-xue/gzx-append.sh" % _posix(SKILL_DIR)).allow)
    check("G16 正文多 CR（E\\r\\r）+ 其后接 rm → deny（r7 全剥 CR 后唯一性破）",
          not gdoc.classify("~/.gan-zhong-xue/gzx-append.sh map <<'E'\nE\r\r\n"
                            "rm ~/.gan-zhong-xue/map.md\nE").allow)
    # G17-G21:r12(GPT 外审八轮 P1/P2)——引导豁免收紧的边界负例（与 selftest 同源钉双侧）
    check("G17 cp 源名合法改名件覆盖 map.md → deny（目标文件名层）",
          not gdoc.classify("cp /tmp/evil/gzx-append.sh ~/.gan-zhong-xue/map.md").allow)
    check("G18 cp 改名件覆盖写入器本体 → deny（来源合法性层）",
          not gdoc.classify("cp /tmp/evil/gzx-append.sh ~/.gan-zhong-xue/gzx-append.sh").allow)
    check("G19 chmod 000 数据资产 → deny（chmod 仅初始化形状）",
          not gdoc.classify("chmod 000 ~/.gan-zhong-xue/map.md").allow)
    check("G20 chmod 000 写入器 → deny（权限清零使机制失效）",
          not gdoc.classify("chmod 000 ~/.gan-zhong-xue/gzx-append.sh").allow)
    check("G21 mkdir 数据目录子路径 → deny（mkdir 仅目录本身）",
          not gdoc.classify("mkdir ~/.gan-zhong-xue/sub").allow)
    # G22:r13(GPT 外审九轮 P1)——SKILL.md 形态标记可伪造：真造一个带 SKILL.md 的假
    # 插件束（含同名写入器），来源身份按路径精确相等判定 ⇒ 必 deny。这正是 r12
    # 形态检查会放行、r13 收紧后的关键负例（GPT 点名形状）。
    fake_plugin = "/tmp/gzx-fake-plugin"
    os.makedirs(fake_plugin, exist_ok=True)
    for f in ("SKILL.md", "gzx-append.sh", "gzx_lock_append.py"):
        shutil.copy(os.path.join(SKILL_DIR, f), os.path.join(fake_plugin, f))
    check("G22 假插件束（SKILL.md+同名写入器）覆盖写入器 → deny（来源身份层）",
          not gdoc.classify("cp /tmp/gzx-fake-plugin/gzx-append.sh ~/.gan-zhong-xue/gzx-append.sh").allow)
    shutil.rmtree(fake_plugin, ignore_errors=True)
    # G23:r13(GPT 外审九轮 P2)——首位 7 不再放行 750/755/777，恰 700 才豁免
    check("G23 chmod 777 数据目录 → deny（本地数据隐私,仅 700）",
          not gdoc.classify("chmod 777 ~/.gan-zhong-xue").allow)

    print("\\n%s：%d 项失败" % ("FAILED" if FAILS else "ALL PASS", len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
