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
import stat
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


def _rmtree(path):
    """删目录树并返回「确实删干净了」。

    v1.1.2（GPT 外审十轮 P3-1 根因修正）：Windows 上 git 把 .git/objects/* 设为只读
    （mode 444），shutil.rmtree 删只读文件报 PermissionError——而 ignore_errors=True
    把这个错吞掉，于是「清理代码跑了、目录还在」（实测系统临时目录积了 84 个 gzx-int-*）。
    对症：出错时清只读位再重试；返回值让调用方能断言真的删掉，不再静默。"""
    def _onerr(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=_onerr)
    return not os.path.exists(path)


def main():
    # v1.1.2（GPT 外审十轮 P3-1）：全部临时物统一建在一个根目录下，退出时整体清理——
    # 此前 worker 脚本目录（gzx_e1_*）、屏障目录与输入文件、场景根目录（gzx-int-*）
    # 每次运行都遗留。try/finally 保证断言失败抛异常时也不遗留。
    work = tempfile.mkdtemp(prefix="gzx-int-")
    try:
        return _run(work)
    finally:
        cleaned = _rmtree(work)
        if not cleaned:
            # 清理失败要看得见：以前是 ignore_errors=True 静默吞掉
            print("WARN 集成测试临时目录未能清干净: %s" % work)


def _run(work):
    # ---- 场景工作区：真实 git 仓库 + 隔离数据目录 + 模拟插件技能目录 ----
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

    # D5（v1.1.2 GPT 外审十轮 P2-1）：命令链/绝对路径/env 前缀的提交也要捕获。
    # 每条都是真提交（真改文件真 commit），捕获后断言 sha == 新 HEAD——端到端，不是只测识别函数。
    def real_commit_and_capture(n, command):
        with open(os.path.join(repo, "f%d.txt" % n), "w", encoding="utf-8") as f:
            f.write("x%d\n" % n)
        git("add", "f%d.txt" % n)
        git("commit", "-m", "c%d" % n)
        new_head = git("rev-parse", "HEAD").stdout.decode().strip()
        new_short = git("rev-parse", "--short", "HEAD").stdout.decode().strip()
        run_capture(capture_env(), {
            "tool_name": "Bash", "cwd": repo,
            "tool_input": {"command": command},
            "tool_response": {"output": "[main %s] c%d" % (new_short, n)},
        })
        return new_head

    forms = [
        'cd %s && git commit -m "fix"' % _posix(repo),   # GPT 十轮 P2-1 正例 1
        "/usr/bin/git commit -m fix",                     # GPT 十轮 P2-1 正例 2
        "env GIT_AUTHOR_NAME=test git commit -m fix",     # GPT 十轮 P2-1 正例 3
        "git -C %s commit -m x" % _posix(repo),           # 全局选项 -C（MSYS /c/ 形态）
        "git -C '%s' commit -m x" % repo.replace("\\", "/"),   # 全局选项 -C（盘符+正斜杠，冒号接回）
        "git --no-pager commit --amend --no-edit",        # 全局旗标 + 子命令选项
        "git add -A && git commit -m gen && git push",    # 命令链中段
    ]
    d5_ok, d5_detail = True, ""
    for n, command in enumerate(forms, start=1):
        want = real_commit_and_capture(n, command)
        with open(cand, encoding="utf-8") as f:
            shas = [json.loads(l)["sha"] for l in f if l.strip()]
        if want not in shas:
            d5_ok, d5_detail = False, "form %r not captured (have %d rows)" % (command, len(shas))
            break
    check("D5 命令链/绝对路径/env 前缀/全局选项的提交均被捕获（%d 种形态）" % len(forms),
          d5_ok, d5_detail)

    # D6（同轮反例）：命令里出现 commit 字样但不是提交动作 → 不落账。
    with open(cand, encoding="utf-8") as f:
        before_neg = len([l for l in f if l.strip()])
    non_commits = [
        "echo git commit -m fix",
        "git status",
        "git log --oneline",
        "git log --grep commit",
        "git config commit.gpgsign true",
        "cat notes.txt | grep commit",
        "python x.py --msg 'git commit -m hi'",
        "git tag -a v9 -m commit",
        "docker exec box git commit -m x",       # 包装器后：cwd 不可信，不捕获（漏报方向）
    ]
    for command in non_commits:
        # 输出故意带成功标记形状：识别层若误判，后道防线会因标记与 HEAD 不符而拦——
        # 这里要证明的是识别层根本不放行（第一道门就关）。
        run_capture(capture_env(), {
            "tool_name": "Bash", "cwd": repo,
            "tool_input": {"command": command},
            "tool_response": {"output": "[main deadbee] whatever"},
        })
    with open(cand, encoding="utf-8") as f:
        after_neg = len([l for l in f if l.strip()])
    check("D6 含 commit 字样但非提交动作的命令不落账（%d 种形态）" % len(non_commits),
          after_neg == before_neg, "before=%d after=%d" % (before_neg, after_neg))

    # D7（同轮）：`git commit --help` 被识别层判为 commit 动作，但输出无成功标记 → 不落账
    # （后道防线 P1-5 兜住；证明识别层不做 --help 特判也不会误捕获）。
    with open(cand, encoding="utf-8") as f:
        before_help = len([l for l in f if l.strip()])
    run_capture(capture_env(), {
        "tool_name": "Bash", "cwd": repo,
        "tool_input": {"command": "git commit --help"},
        "tool_response": {"output": "GIT-COMMIT(1)\n\nNAME\n       git-commit - Record changes"},
    })
    with open(cand, encoding="utf-8") as f:
        after_help = len([l for l in f if l.strip()])
    check("D7 git commit --help 不落账（成功标记防线）", after_help == before_help,
          "before=%d after=%d" % (before_help, after_help))

    # 并发:双进程同 sha → 恰 1 行(P1-4 原子性)
    sys.path.insert(0, SKILL_DIR)
    import importlib.util as _iu
    spec2 = _iu.spec_from_file_location("gzx_lock_append",
                                        os.path.join(SKILL_DIR, "gzx_lock_append.py"))
    core = _iu.module_from_spec(spec2)
    spec2.loader.exec_module(core)
    # 并发（v1.1.2 重写——Bugbot PR#4 Medium：旧版 worker 用 python -c 且换行
    # 写成字面 \\n，两个进程直接 SyntaxError 全灭；D1 的旧行让断言空过，
    # lock_append_commit_unique 的并发路径从未被测过。改为 worker 真跑 + 回收 rc
    # 证明真跑了 + 争用断言真正咬合）。
    # worker 源码：三引号原文，换行是真换行（不是 "\\n" 字面量——那正是旧缺陷的成因）。
    # 屏障设计（v1.1.2 GPT 外审十轮 P2-2）：READY 必须意味着「除了进锁，一切都做完了」——
    #   导入模块 → 读入数据 → 解析参数 → 【写 READY】→ 等 GO → 调用锁函数。
    # 旧版 READY 写在 bootstrap 一开始，GO 之后还要做模块加载/读文件，操作系统完全
    # 可以让第一个 worker 把这些步骤连同整个写入跑完、第二个才被调度 ⇒ 测不到争用。
    # 现在 GO 释放后，worker 距离锁入口只剩一次函数调用。
    bar_root = os.path.join(work, "barrier")
    os.makedirs(bar_root, exist_ok=True)
    worker_src = '''
import json
import os
import sys
import time

go_path, data_path, core_dir, gzx_dir, ready_path = sys.argv[1:6]
sys.path.insert(0, core_dir)
import gzx_lock_append as core

with open(data_path, "rb") as f:
    data = f.read()
json.loads(data.decode("utf-8"))            # 参数在等 GO 之前就备好（解析失败在此暴露）

open(ready_path, "w").close()               # READY = 只差进锁
deadline = time.time() + 60
while not os.path.exists(go_path):
    if time.time() > deadline:
        sys.stderr.write("timeout waiting GO")
        sys.exit(3)
    time.sleep(0.001)

rc, msg = core.lock_append_commit_unique(gzx_dir, data)
sys.stderr.write(str(rc))
sys.exit(0 if rc == 0 else 4)
'''

    def spawn_workers(row_objs):
        """并发启动 len(row_objs) 个 worker；全部 READY 后统一放 GO。
        返回 [(returncode, stderr), ...]，顺序与 row_objs 对应。"""
        bar_dir = tempfile.mkdtemp(prefix="run-", dir=bar_root)
        go_path = os.path.join(bar_dir, "GO")
        procs, readies, infiles = [], [], []
        for i, row_obj in enumerate(row_objs):
            infile = os.path.join(bar_dir, "in-%d.json" % i)
            with open(infile, "wb") as f:
                f.write((json.dumps(row_obj, ensure_ascii=False) + "\n").encode("utf-8"))
            ready = os.path.join(bar_dir, "ready-%d" % i)
            procs.append(subprocess.Popen(
                [sys.executable, "-c", worker_src, go_path, infile,
                 SKILL_DIR, gzx_dir, ready],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, env=env))
            readies.append(ready)
            infiles.append(infile)
        # 等齐全部 READY(60s)；超时 ⇒ 杀 worker(其 rc 非 0 ⇒ 用例红,不留假绿)
        deadline = time.time() + 60
        while time.time() < deadline:
            if all(os.path.exists(r) for r in readies):
                break
            time.sleep(0.002)
        if not all(os.path.exists(r) for r in readies):
            for pr in procs:
                pr.kill()
        with open(go_path, "w") as _g:
            _g.write("go")
        outs = []
        for pr in procs:
            _, err = pr.communicate(timeout=60)
            outs.append((pr.returncode, err.decode("utf-8", "replace")))
        _rmtree(bar_dir)          # 每轮即清；根目录在 main 的 finally 兜底
        return outs

    def cand_rows():
        if not os.path.exists(cand):
            return []
        with open(cand, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]

    def seq_is_contiguous(rows):
        seqs = sorted(r_.get("seq") for r_ in rows)
        return seqs == list(range(1, len(rows) + 1)), seqs

    # ---- E：并发不变量（P1-4 原子性）。多轮压力（GPT 十轮 P2-2）：
    #   单轮里操作系统完全可能把 worker 串行调度；多轮 + 多 worker 让「每轮都
    #   恰好串行、且每次都掩盖住锁缺陷」的概率随轮数指数下降。
    #   如实边界：测试无法从外部观测「两进程同时持锁」，那由锁实现本身保证；
    #   这里的证明强度 = 高强度争用下三条不变量（不丢行 / seq 唯一连续 / 同 sha 不重复）
    #   逐轮成立。
    #   旧版 E3 的 sha 用的是 head——它在 D1 阶段已被捕获过，所以旧断言「零新增」
    #   测的其实是「已存在的 sha 不重写」，不是并发去重；这里每轮用全新 sha，
    #   断言「恰 +1」才是真正的并发去重。
    STRESS_ROUNDS = 20
    WORKERS = 8
    e1_ok, e1_detail = True, ""
    for rnd in range(STRESS_ROUNDS):
        before = len(cand_rows())
        tag = "r%d-" % rnd
        rows_in = [{"sha": "%s%s-%d" % (head[:12], tag, k), "add": k + 1, "del": 0,
                    "event": "commit", "ts": "2026-11-11T00:00:00Z"} for k in range(WORKERS)]
        rcs = spawn_workers(rows_in)
        if not all(rc == 0 for rc, _ in rcs):
            e1_ok, e1_detail = False, "round %d rcs=%r" % (rnd, rcs)
            break
        got = [r_["sha"] for r_ in cand_rows() if str(r_["sha"]).startswith(head[:12] + tag)]
        if sorted(got) != sorted(r_["sha"] for r_ in rows_in):
            e1_ok, e1_detail = False, "round %d lost/dup rows: got %d want %d" % (
                rnd, len(got), len(rows_in))
            break
        ok_seq, seqs = seq_is_contiguous(cand_rows())
        if not ok_seq or len(seqs) != before + WORKERS:
            e1_ok, e1_detail = False, "round %d seq broken: %r" % (rnd, seqs[-12:])
            break
    check("E1 %d worker × %d 轮：不同 sha 并发不丢行不重复（锁串行化）" % (WORKERS, STRESS_ROUNDS),
          e1_ok, e1_detail)

    ok_seq, seqs = seq_is_contiguous(cand_rows())
    check("E2 E1 压力后 seq 全文件唯一且连续(锁内现算)", ok_seq,
          "n=%d seqs=%r" % (len(seqs), seqs[-12:]))

    e3_ok, e3_detail = True, ""
    for rnd in range(STRESS_ROUNDS):
        before = len(cand_rows())
        same_sha = "%s-e3-%d" % (head[:12], rnd)
        rcs = spawn_workers([{"sha": same_sha, "add": 1, "del": 0,
                              "event": "commit", "ts": "2026-11-11T00:00:00Z"}
                             for _ in range(WORKERS)])
        if not all(rc == 0 for rc, _ in rcs):
            e3_ok, e3_detail = False, "round %d rcs=%r" % (rnd, rcs)
            break
        after = len(cand_rows())
        if after != before + 1:
            e3_ok, e3_detail = False, "round %d: before=%d after=%d (want +1)" % (
                rnd, before, after)
            break
    check("E3 %d worker 同(新)sha × %d 轮：每轮恰落 1 行（去重原子）" % (WORKERS, STRESS_ROUNDS),
          e3_ok, e3_detail)

    ok_seq, seqs = seq_is_contiguous(cand_rows())
    check("E4 全部压力后 seq 仍唯一且连续", ok_seq, "n=%d" % len(seqs))

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
                # r15(Bugbot 4668735266):替换值**不加引号**——文档 cp 命令自 r14 起自带
                # 引号（`cp '<SKILL_DIR>/...'`），抽取器再包一层得 `''路径''`：空格落在
                # 引号外，带空格路径照样被拆词（bash/shlex 同判）⇒ G1 假红。文档引号即
                # 空格保护，替换只做纯字面替换。
                cmds.append(ln.replace("<SKILL_DIR>", _posix(SKILL_DIR)))
    # 抽取器变换清单（board #122 dsh #2675 §2，写死防误读）：
    #   {fence dedent（隐含于 splitlines+strip）, CRLF→LF, `<SKILL_DIR>` 占位符纯替换}
    #   r14(Bugbot "spaced installs"):文档 cp 命令带单引号（含空格安装路径须引号防
    #   shlex 切碎；shlex 撇引号后路径仍是一个 token）
    # ——占位符字面含 <>，会被分词器切进重定向层使段长 ≠ 3（prefix:cp 假红），
    #   故替换是验收前提；断言占位符无残留（合法的 2>/dev/null 类重定向不算）。
    bad_xform = [c for c in cmds if "<SKILL_DIR" in c]
    check("G1a 抽取器变换后无 <SKILL_DIR> 占位符残留", not bad_xform,
          "; ".join(repr(c) for c in bad_xform))
    # G1c:r15(Bugbot 4668735266)——变换后禁止嵌套空引号（`''`）：文档引号+替换值再包
    # 一层即产出 `''路径''`，空格落在引号外=引号保护失效。此断言钉死双层引号回归。
    bad_dq = [c for c in cmds if "''" in c]
    check("G1c 变换后无嵌套空引号（防文档引号+替换值引号叠加）", not bad_dq,
          "; ".join(repr(c) for c in bad_dq))
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
    check("G15b 占位符替换成实路径（引号形态）→ allow（G1 已覆盖,此处单独钉）",
          gdoc.classify("cp '%s/gzx-append.sh' ~/.gan-zhong-xue/gzx-append.sh" % _posix(SKILL_DIR)).allow)
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
    # v1.1.2(GPT 外审十轮 P3-1 同族清理):假束建在统一根目录下，不再写死 /tmp。
    fake_plugin = os.path.join(work, "fake-plugin")
    os.makedirs(fake_plugin, exist_ok=True)
    for f in ("SKILL.md", "gzx-append.sh", "gzx_lock_append.py"):
        shutil.copy(os.path.join(SKILL_DIR, f), os.path.join(fake_plugin, f))
    check("G22 假插件束（SKILL.md+同名写入器）覆盖写入器 → deny（来源身份层）",
          not gdoc.classify("cp %s/gzx-append.sh ~/.gan-zhong-xue/gzx-append.sh"
                            % _posix(fake_plugin)).allow)
    # G23:r13(GPT 外审九轮 P2)——首位 7 不再放行 750/755/777，恰 700 才豁免
    check("G23 chmod 777 数据目录 → deny（本地数据隐私,仅 700）",
          not gdoc.classify("chmod 777 ~/.gan-zhong-xue").allow)

    print("\n%s：%d 项失败" % ("FAILED" if FAILS else "ALL PASS", len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
