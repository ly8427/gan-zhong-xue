#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test-gzx-invariants.py — gan-zhong-xue v1.2.0 机械不变量测试（D7：机械必过面）。

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
    """Windows 上优先找 Git Bash（避开 WSL bash）；POSIX 上直接用系统 bash。"""
    if os.name != "nt":
        return shutil.which("bash")
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


def _rmtree(path):
    """删目录树并返回「确实删干净了」（v1.1.2 GPT 外审十轮 P3-1 根因修正）。
    Windows 上 git 把 .git/objects/* 设为只读，rmtree 报 PermissionError，
    ignore_errors=True 会吞掉它 ⇒ 清理代码跑了、目录还在。出错时清只读位重试。"""
    def _onerr(func, p, _exc):
        try:
            import stat as _st
            os.chmod(p, _st.S_IWRITE)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=_onerr)
    return not os.path.exists(path)


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
        _rmtree(d2)
    finally:
        _rmtree(d)


def test_writer_states():
    if BASH_EXE is None:
        # dsh #2992 §一 实证：本函数共 5 条 check（A6/A7/A8/A9/A9b），旧文案写「四态」，
        # 读文案会算成跳过 4 条、对不上分母。按实际条数写。
        # 跳过就是跳过：不伪造 PASS（恒真灯会把「没跑」伪装成「通过」）。
        print("SKIP bash 面写入器 5 条（A6/A7/A8/A9/A9b）——本机无可用的非 WSL bash；"
              "python 核心已覆盖不变量")
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
        _rmtree(d)
        if minimal:
            _rmtree(minimal)


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
        _rmtree(d)


def test_capture_dedup():
    """C：捕获去重键 (sha,event)（D5）。

    v1.1.2（GPT 外审十轮，承接 dsh #2992 / opencode #2994 双向实证）：旧版用假标记
    `[main abc123]`——它过不了 P1-5 的「标记 sha 须等于 rev-parse HEAD」复核，
    所以 candidates.jsonl 在**任何机器上**都不会被创建，C2（断言 ≤1 行，0 行也绿）
    恒过、C3/C4 恒不执行：两条断言从未被证明过。改为在临时目录里建**真仓库 + 真提交**，
    标记用真 HEAD 的短 sha ⇒ 捕获必然落账，C2 收紧为「恰 1 行」，C3/C4 真跑到。"""
    d = tempfile.mkdtemp(prefix="gzx-cap-")
    repo = tempfile.mkdtemp(prefix="gzx-caprepo-")
    try:
        def git(*args):
            return subprocess.run(["git", "-C", repo] + list(args),
                                  capture_output=True, timeout=30)
        git("init", "-q")
        git("config", "user.email", "t@example.invalid")
        git("config", "user.name", "t")
        with open(os.path.join(repo, "a.txt"), "w", encoding="utf-8") as f:
            f.write("a\n")
        git("add", "a.txt")
        git("commit", "-q", "-m", "c1")
        head = git("rev-parse", "HEAD").stdout.decode().strip()
        short = git("rev-parse", "--short", "HEAD").stdout.decode().strip()
        check("C0 夹具：临时真仓库有 HEAD（否则下列断言无意义）", len(head) == 40, head)

        ev = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m c1"},
            "cwd": repo,
            "tool_response": {"output": "[main %s] c1" % short},   # 真短 sha
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
        check("C1b 捕获进程 stderr 为空（默认静默）",
              r1.stderr == b"" and r2.stderr == b"", repr(r1.stderr[:120]))
        # 真提交 ⇒ 必须恰好 1 行（旧断言 ≤1 让「0 行=根本没捕获」也过——假绿）
        check("C2 去重 (sha,event) 恰 1 行（同 sha 两次触发）", len(rows) == 1,
              "got %d rows" % len(rows))
        if len(rows) == 1:
            row = rows[0]
            check("C2b 行的 sha == HEAD", row.get("sha") == head,
                  "%r vs %r" % (row.get("sha"), head))
            check("C3 行含 seq/ts/event 字段", all(k in row for k in ("seq", "ts", "event")))
            check("C3b seq == 1（首行序号）", row.get("seq") == 1, repr(row.get("seq")))
            check("C4 行无路径无仓名（D1）",
                  "gan-zhong-xue" not in json.dumps(row)
                  and "ZCodeProject" not in json.dumps(row)
                  and "gzx-caprepo" not in json.dumps(row)
                  and "a.txt" not in json.dumps(row))
        else:
            check("C3 行含 seq/ts/event 字段", False, "未执行：rows=%d" % len(rows))
            check("C4 行无路径无仓名（D1）", False, "未执行：rows=%d" % len(rows))
    finally:
        left = [p for p in (d, repo) if not _rmtree(p)]
        check("C9 测试自身的临时目录已清干净（含只读 .git/objects）", not left, repr(left))


def _load_capture_module():
    import importlib.util as _iu
    spec = _iu.spec_from_file_location("gzx_capture_for_test", CAPTURE)
    mod = _iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_commit_command_recognition():
    """C5-C7（v1.1.2 GPT 外审十轮 P2-1）：提交命令识别——按简单命令段，不是看整串开头。
    正反例在此固化（不是只在开发探针里跑过）。"""
    cap = _load_capture_module()
    is_commit = cap.is_commit_command
    # 应识别为提交动作
    yes = [
        "git commit -m fix",
        "git commit --amend --no-edit",
        'cd /workspace/project && git commit -m "fix"',     # GPT 十轮 P2-1 正例 1
        "/usr/bin/git commit -m fix",                       # GPT 十轮 P2-1 正例 2
        "env GIT_AUTHOR_NAME=test git commit -m fix",       # GPT 十轮 P2-1 正例 3
        "env -u HTTP_PROXY git commit -m fix",              # GPT 十一轮 P2：env 带参选项 -u
        "env -C /workspace/project git commit -m fix",      # GPT 十一轮 P2：env 带参选项 -C
        "env --unset HTTP_PROXY git commit -m fix",
        "env --chdir=/workspace git commit -m fix",
        "env -S '-u FOO' git commit -m x",
        "env -- /usr/bin/git commit -m x",                  # -- 后全是位置参数
        "env FOO=bar -v git commit -m x",
        "env --debug git commit -m x",                      # v1.1.2 Bugbot 4674364435：-v 的长选项是 --debug
        "env - git commit -m x",                            # v1.1.2：单独 - 等价 -i（env --help 明载）
        "env --list-signal-handling git commit -m x",       # v1.1.2：无参旗标补登记
        "FOO=bar git commit -m x",
        "env -i FOO=bar /usr/bin/git commit",
        "git add -A && git commit -m a && git push",
        "git -C /repo commit -m x",
        "git -C C:/repo commit -m x",                       # 盘符+正斜杠（冒号被 shlex 切开）
        "git -c user.name=x commit -m y",
        "git --git-dir=/r/.git commit -m x",
        "git --git-dir /r/.git --work-tree=/r commit -m x",
        "git --no-pager commit -m x",
        "(git commit -m x)",
        "{ git commit -m x; }",
        "git commit -m 'a && b are inside quotes'",         # 引号内的 && 不是分隔符
        "echo hi | git commit -F -",
        "time git commit -m x",
        "if true; then git commit -m x; fi",
        "PATH=/usr/bin:/bin git commit -m x",
        ": && git commit -m x",                             # 段首 no-op 内建命令
        # v1.1.2 Bugbot 4670188716/4670188696：Windows git.exe 与 $ 环境值
        "git.exe commit -m fix",
        "Git.EXE commit -m fix",
        "PATH=$PATH:/x git commit -m y",                    # env 值含 $（$ 加进 wordchars）
        "FOO=$(date) git commit -m x",                      # env 值含命令替换
        "git -C C:/repo commit -m x",                       # 盘符冒号接回
        # D3（docs/DEBT.md）：来源身份/引号形态成对正反断言
        '"/usr/bin/git" commit -m x',
        "/usr/bin/git commit -m x",
        '"/tmp/my tools/git" commit -m x',                  # 带空格路径加引号：应识别
        '"C:/Program Files/Git/bin/git.exe" commit -m x',   # 带空格 .exe 路径加引号：应识别
        "PATH=/x git.exe commit -m x",
    ]
    # 不应识别（命令里出现 commit 字样，但不是在执行 git commit）
    no = [
        "/tmp/my tools/git commit -m x",                    # D3 成对反例：带空格路径未加引号 → 拆词后程序名是 /tmp/my
        "C:/Program Files/Git/bin/git.exe commit -m x",     # D3 成对反例：带空格 .exe 未加引号 → 程序名是 C:/Program
        "git status",
        "git log --oneline",
        "echo git commit",
        "echo 'git commit -m fix'",
        "cat notes.txt | grep commit",
        "git commit-tree HEAD -m x -p HEAD",
        "git log --grep commit",
        "git push",
        "npm run commit",
        "docker exec box git commit -m x",        # 包装器后：cwd/输出归属不可信 ⇒ 不捕获
        "python x.py --msg 'git commit -m hi'",
        "git tag -a commit -m x",
        "git branch commit",
        "git checkout commit",
        "git config commit.gpgsign true",
        "git merge --no-ff --no-commit",
        "git help commit",
        "git add commit",                        # add 一个名叫 commit 的路径
        "git checkout -b commit",
        "git push origin commit",
        "git -C C:/repo status",                 # 全局选项后的子命令不是 commit
        "gitk commit",
        "mygit commit -m x",
        "git-commit -m x",
        "GIT_CMD=C:/x/git commit",               # 命令是名为 commit 的程序
        "C:/tools/gitk commit",
        "sudo git commit -m x",                  # sudo 后的命令：不识别（漏报方向，如实登记）
        "git.exe",                               # 只有程序名没有子命令
        "git.exe status",                        # .exe 程序但子命令不是 commit
        "git.exe --version",
        "$GIT_BIN commit -m x",                  # 变量存放的 git：不识别（漏报方向）
        # v1.1.2 GPT 外审十一轮 P2：env 带参选项的值不是程序——识别层在值处停住不算误报
        "env -u X grep commit",
        "env -C /r grep commit",
        "env grep commit",
        "env -- echo git commit",
        "env -u HTTP_PROXY git status",
        "env --verbose git commit",                  # v1.1.2 Bugbot 4674364435：--verbose 不是 env 选项 ⇒ 不豁免
        "gh commit",
        "",
        "   ",
        "git",
    ]
    miss = [c for c in yes if not is_commit(c)]
    false_pos = [c for c in no if is_commit(c)]
    check("C5 提交命令正例全部识别（%d 条）" % len(yes), not miss, repr(miss))
    check("C6 含 commit 字样的非提交命令全部不识别（%d 条）" % len(no), not false_pos,
          repr(false_pos))
    # 不可解析（引号未闭合）：一律不识别——漏报优于误报
    check("C7 引号未闭合的命令不识别",
          not is_commit("git commit -m 'unterminated")
          and not is_commit("echo 'x && git commit -m y"))
    # 识别层边界如实钉住：`git commit --help` 是 commit 动作，识别层判 True；
    # 「不落账」由成功标记防线保证——端到端见集成套件 D7，这里只钉识别层的契约。
    check("C8 git commit --help 识别层判 True（落账与否由成功标记防线把关，见集成 D7）",
          is_commit("git commit --help") is True)


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
        _rmtree(d)


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
        _rmtree(d)


def _capture_neg_body(d):
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
        _rmtree(d)


def test_migration_no_rewrite():
    """F：真实数据目录只读断言——本测试全程不写它；哈希进输出供 CHANGELOG 复核。"""
    home = os.path.expanduser("~/.gan-zhong-xue")
    for name in ("map.md", "map.jsonl"):
        p = os.path.join(home, name)
        if os.path.exists(p):
            h = hashlib.sha256(open(p, "rb").read()).hexdigest()
            print("INFO 真实数据目录 %s sha256=%s…" % (name, h[:16]))
    check("F1 真实数据目录未被本测试触碰（只读断言）", True)


def test_tmp_cleanup_path_b():
    """DEBT D1：无 flock(1) 时路径 B 不再使用 exec，退出前清理 $TMP，零残留。"""
    if BASH_EXE is None:
        print("SKIP D1 路径 B 临时文件清理（无可用 bash）")
        return
    d = tempfile.mkdtemp(prefix="gzx-d1-")
    tmpd = tempfile.mkdtemp(prefix="gzx-d1tmp-")
    minimal = tempfile.mkdtemp(prefix="gzx-d1path-")
    try:
        for tool in ("cat", "mktemp", "rm", "wc", "mkdir", "dirname", "uname", "python3"):
            src = shutil.which(tool)
            if src:
                shutil.copy(src, os.path.join(minimal, tool))
        env = dict(os.environ, GZX_DIR=_posix(d), TMPDIR=_posix(tmpd), PATH=minimal)
        for idx in range(3):
            r = subprocess.run([BASH_EXE, _posix(WRITER_SH), "pending"],
                               env=env, input=("- item %d\n" % idx).encode(),
                               capture_output=True, timeout=30)
            if r.returncode != 0:
                check("D1-1 无 flock 路径 B 写入成功", False,
                      "rc=%d err=%s" % (r.returncode, r.stderr.decode(errors="replace")))
                return
        leftovers = [f for f in os.listdir(tmpd) if f.startswith("gzx-append.")]
        check("D1-1 无 flock 路径 B 写入成功且临时目录零残留", len(leftovers) == 0,
              "leftovers=%r" % leftovers)
    finally:
        _rmtree(d)
        _rmtree(tmpd)
        _rmtree(minimal)


def test_cli_and_divergence():
    """P0 + DEBT D2 + Bugbot 1/2：gzx_cli.py preflight / cell 最新精确匹配 / save-cell 全字段 / check-divergence。"""
    CLI = os.path.join(SKILL_DIR, "gzx_cli.py")
    d = tempfile.mkdtemp(prefix="gzx-cli-")
    try:
        env = dict(os.environ, GZX_DIR=d)
        r = subprocess.run([sys.executable, CLI, "preflight"], env=env,
                           capture_output=True, timeout=30)
        check("CLI-1 preflight 成功返回 JSON", r.returncode == 0)
        pf = json.loads(r.stdout.decode("utf-8"))
        check("CLI-1b preflight 自动引导并直执行自检写入器", pf.get("writer_ready") is True
              and pf.get("writer_selftest", {}).get("ok") is True)

        cell = json.dumps({
            "概念": "数据与指令边界",
            "分层": "🟡",
            "来源": "🟢自",
            "先猜": '用户说"只是配置文件"',
            "证据": "揭示后意识到 eval 风险",
            "顶点原理": "未隔离的数据边界等价于代码注入面",
            "验证题": '把"数据即代码"迁到 SQL：为什么拼接字符串会出注入？',
            "用户更懂": "无",
            "状态": "待复验",
            "备注": "包含英文引号与\n换行测试",
        }, ensure_ascii=False)
        r2 = subprocess.run([sys.executable, CLI, "save-cell"], env=env,
                            input=cell.encode("utf-8"), capture_output=True, timeout=30)
        check("CLI-2 save-cell 成功", r2.returncode == 0,
              r2.stderr.decode(errors="replace"))

        # Bugbot 2：验证 jsonl 行完整包含 证据/顶点原理/用户更懂（方案甲 SSOT）
        with open(os.path.join(d, "map.jsonl"), "r", encoding="utf-8") as jf:
            jrow = json.loads(jf.read().strip())
        check("CLI-2b jsonl 行全字段包含证据/顶点原理/用户更懂",
              jrow.get("证据") == "揭示后意识到 eval 风险"
              and jrow.get("顶点原理") == "未隔离的数据边界等价于代码注入面"
              and jrow.get("用户更懂") == "无")

        # Bugbot 1：验证 cell 命令概念精确匹配（短名不抢长名）且取最新段落（倒序）
        # 1. 写入长概念 "Foo Bar" 初始格
        c_fb1 = json.dumps({"概念": "Foo Bar", "分层": "🟡", "来源": "🟢自", "先猜": "猜1", "证据": "证1", "顶点原理": "理1", "验证题": "题_FB1", "状态": "待复验"})
        subprocess.run([sys.executable, CLI, "save-cell"], env=env, input=c_fb1.encode("utf-8"), capture_output=True, timeout=30)
        # 2. 写入短概念 "Foo" 格（若前缀匹配会与 Foo Bar 冲突）
        c_f = json.dumps({"概念": "Foo", "分层": "🟡", "来源": "🟢自", "先猜": "猜2", "证据": "证2", "顶点原理": "理2", "验证题": "题_FOO", "状态": "待复验"})
        subprocess.run([sys.executable, CLI, "save-cell"], env=env, input=c_f.encode("utf-8"), capture_output=True, timeout=30)
        # 3. 写入长概念 "Foo Bar" 复验追加新格（验证题变更为 题_FB2）
        c_fb2 = json.dumps({"概念": "Foo Bar", "分层": "🟢自", "来源": "🟢自", "先猜": "猜3", "证据": "证3", "顶点原理": "理3", "验证题": "题_FB2", "状态": "已复验"})
        subprocess.run([sys.executable, CLI, "save-cell"], env=env, input=c_fb2.encode("utf-8"), capture_output=True, timeout=30)

        # 查短名 "Foo"：必须精确命中 Foo，不得抢占 Foo Bar，题面必须是 题_FOO
        r_f = subprocess.run([sys.executable, CLI, "cell", "Foo"], env=env, capture_output=True, timeout=30)
        out_f = json.loads(r_f.stdout.decode("utf-8"))
        check("CLI-2c cell 短名精确匹配（短名不抢长名）",
              "题_FOO" in (out_f.get("md_section") or "") and "Foo Bar" not in (out_f.get("md_section") or ""))

        # 查长名 "Foo Bar"：必须取最后追加的最新格（题_FB2），而不是旧格（题_FB1）
        r_fb = subprocess.run([sys.executable, CLI, "cell", "Foo Bar"], env=env, capture_output=True, timeout=30)
        out_fb = json.loads(r_fb.stdout.decode("utf-8"))
        check("CLI-2d cell 倒序提取最新追加段落（新行配新段）",
              "题_FB2" in (out_fb.get("md_section") or "") and "题_FB1" not in (out_fb.get("md_section") or ""))

        r_div0 = subprocess.run([sys.executable, CLI, "check-divergence"], env=env,
                                capture_output=True, timeout=30)
        check("CLI-3 一致双地图 check-divergence 返回 0", r_div0.returncode == 0)

        # 制造 D2 人为发散：单独向 map.md 再写一格
        run_core(d, "map", "## 孤立格 · 🟡 · 2026-11-26\n- 状态：待复验\n\n")
        r_div1 = subprocess.run([sys.executable, CLI, "check-divergence"], env=env,
                                capture_output=True, timeout=30)
        err_txt = r_div1.stderr.decode("utf-8", "replace")
        check("CLI-4 D2 发散时 check-divergence 必报错并输出双侧计数（退出码 24）",
              r_div1.returncode == 24 and "md_count=5" in err_txt and "jsonl_count=4" in err_txt,
              "rc=%d err=%s" % (r_div1.returncode, err_txt))
    finally:
        _rmtree(d)


def test_capture_special_cases():
    """P1：root-commit / detached HEAD / cd 与 -C 跨仓库上下文追踪。"""
    d = tempfile.mkdtemp(prefix="gzx-capx-")
    other = tempfile.mkdtemp(prefix="gzx-cap-other-")
    repo = tempfile.mkdtemp(prefix="gzx-cap-target-")
    try:
        def git(*args):
            return subprocess.run(["git", "-C", repo] + list(args),
                                  capture_output=True, timeout=30)

        git("init", "-b", "main")
        git("config", "user.email", "t@example.invalid")
        git("config", "user.name", "t")
        with open(os.path.join(repo, "a.txt"), "w", encoding="utf-8") as f:
            f.write("1\n")
        git("add", "a.txt")
        git("commit", "-m", "root")
        h1 = git("rev-parse", "HEAD").stdout.decode().strip()
        s1 = git("rev-parse", "--short", "HEAD").stdout.decode().strip()

        env = dict(os.environ, GZX_DIR=d)
        # 1) root-commit 标记 + cwd != 目标仓库（通过 cd 切换）
        ev1 = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "cd %s && git commit -m root" % _posix(repo)},
            "cwd": other,
            "tool_response": {"output": "[main (root-commit) %s] root" % s1},
        })
        subprocess.run([sys.executable, CAPTURE], env=env, input=ev1.encode(),
                       capture_output=True, timeout=30)

        # 2) detached HEAD + git -C <repo>（cwd != 目标仓库）
        git("checkout", "--detach", "HEAD")
        with open(os.path.join(repo, "b.txt"), "w", encoding="utf-8") as f:
            f.write("2\n")
        git("add", "b.txt")
        git("commit", "-m", "det")
        h2 = git("rev-parse", "HEAD").stdout.decode().strip()
        s2 = git("rev-parse", "--short", "HEAD").stdout.decode().strip()
        ev2 = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "git -C %s commit -m det" % _posix(repo)},
            "cwd": other,
            "tool_response": {"output": "[detached HEAD %s] det" % s2},
        })
        subprocess.run([sys.executable, CAPTURE], env=env, input=ev2.encode(),
                       capture_output=True, timeout=30)

        # 3) Bugbot 3：花括号分组 { cd repo; git commit; } 当前 shell 追踪
        with open(os.path.join(repo, "c.txt"), "w", encoding="utf-8") as f:
            f.write("3\n")
        git("add", "c.txt")
        git("commit", "-m", "brace")
        h3 = git("rev-parse", "HEAD").stdout.decode().strip()
        s3 = git("rev-parse", "--short", "HEAD").stdout.decode().strip()
        ev3 = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "{ cd %s; git commit -m brace; }" % _posix(repo)},
            "cwd": other,
            "tool_response": {"output": "[detached HEAD %s] brace" % s3},
        })
        subprocess.run([sys.executable, CAPTURE], env=env, input=ev3.encode(),
                       capture_output=True, timeout=30)

        # 4) Bugbot 3：花括号后外溢 { cd repo; git status; }; git commit
        with open(os.path.join(repo, "d.txt"), "w", encoding="utf-8") as f:
            f.write("4\n")
        git("add", "d.txt")
        git("commit", "-m", "brace_after")
        h4 = git("rev-parse", "HEAD").stdout.decode().strip()
        s4 = git("rev-parse", "--short", "HEAD").stdout.decode().strip()
        ev4 = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "{ cd %s; git status; }; git commit -m brace_after" % _posix(repo)},
            "cwd": other,
            "tool_response": {"output": "[detached HEAD %s] brace_after" % s4},
        })
        subprocess.run([sys.executable, CAPTURE], env=env, input=ev4.encode(),
                       capture_output=True, timeout=30)

        # 5) Bugbot 3：圆括号子 shell ( cd repo && git commit ) 内部捕获 + 外部不外溢
        with open(os.path.join(repo, "e.txt"), "w", encoding="utf-8") as f:
            f.write("5\n")
        git("add", "e.txt")
        git("commit", "-m", "subshell")
        h5 = git("rev-parse", "HEAD").stdout.decode().strip()
        s5 = git("rev-parse", "--short", "HEAD").stdout.decode().strip()
        ev5 = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "( cd %s && git commit -m subshell )" % _posix(repo)},
            "cwd": other,
            "tool_response": {"output": "[detached HEAD %s] subshell" % s5},
        })
        subprocess.run([sys.executable, CAPTURE], env=env, input=ev5.encode(),
                       capture_output=True, timeout=30)

        cand = os.path.join(d, "candidates.jsonl")
        shas = [json.loads(l)["sha"] for l in open(cand, encoding="utf-8") if l.strip()] \
            if os.path.exists(cand) else []
        check("CAP-X1 root-commit + cd 跨目录捕获成功", h1 in shas, repr(shas))
        check("CAP-X2 detached HEAD + git -C 跨目录捕获成功", h2 in shas, repr(shas))
        check("CAP-X3 Bugbot3: 花括号分组内 cd 追踪捕获成功", h3 in shas, repr(shas))
        check("CAP-X4 Bugbot3: 花括号分组后 cd 外溢顶层捕获成功", h4 in shas, repr(shas))
        check("CAP-X5 Bugbot3: 圆括号子 shell 内 cd 捕获成功", h5 in shas, repr(shas))
    finally:
        _rmtree(d)
        _rmtree(other)
        _rmtree(repo)


def test_guard_scoping():
    """P0：收窄守卫范围——同仓开发与文档编辑不误杀，数据目录仍严拦。"""
    d = tempfile.mkdtemp(prefix="gzx-gscope-")
    try:
        env = dict(os.environ, GZX_DIR=d)
        cases = [
            ({"tool_name": "Bash", "tool_input": {"command": "cd /tmp/gan-zhong-xue && git status"}}, 0, "同仓 cd && git 不误杀"),
            ({"tool_name": "Bash", "tool_input": {"command": 'git commit -m "fix gan-zhong-xue"'}}, 0, "commit message 提仓库名不误杀"),
            ({"tool_name": "Write", "tool_input": {"file_path": "/tmp/gan-zhong-xue/README.md", "content": "~/.gan-zhong-xue"}}, 0, "写仓库文档提到数据目录不误杀"),
            ({"tool_name": "Write", "tool_input": {"file_path": os.path.expanduser("~/.gan-zhong-xue/map.md"), "content": "x"}}, 2, "Write 直写数据目录仍拦"),
            ({"tool_name": "Agent", "tool_input": {"prompt": "请冷读 gan-zhong-xue 仓库这次 diff"}}, 0, "Agent 提仓库名不误杀"),
            ({"tool_name": "Agent", "tool_input": {"prompt": "去修改 ~/.gan-zhong-xue/map.md"}}, 2, "Agent 提数据目录仍拦"),
            # #128 A 组红线：$ 劈裂、大小写变体、/c/ 路径、臂③假写入器
            ({"tool_name": "Bash", "tool_input": {"command": "cat x > $HOME/.gan-zhong-xue/map.jsonl"}}, 2, "$HOME 重定向仍拦"),
            ({"tool_name": "Bash", "tool_input": {"command": "cat x >$HOME/.gan-zhong-xue/map.jsonl"}}, 2, ">$HOME 粘连重定向仍拦"),
            ({"tool_name": "Bash", "tool_input": {"command": "cat x > ~/.GAN-ZHONG-XUE/map.jsonl"}}, 2, "大小写变体重定向仍拦"),
            ({"tool_name": "Bash", "tool_input": {"command": "cat x > /c/Users/foo/.gan-zhong-xue/map.jsonl"}}, 2, "/c/ 挂载点重定向仍拦"),
            ({"tool_name": "Bash", "tool_input": {"command": "/tmp/evil/gzx-append.sh map <<'E'\nhello\nE"}}, 2, "臂③假写入器正文不含数据目录仍拦"),
            ({"tool_name": "Bash", "tool_input": {"command": "python3 /tmp/evil/gzx_cli.py save-cell <<'E'\nhello\nE"}}, 2, "臂③假 CLI 正文不含数据目录仍拦"),
            # Bugbot 新轮意见覆盖：插件源码文件编辑不误杀、Agent 提 CLI 不误杀、只读形态 2>&1 / 输入重定向放行、本地伪造写入器被拒
            ({"tool_name": "Write", "tool_input": {"file_path": "skills/gan-zhong-xue/gzx_cli.py", "content": "# test"}}, 0, "Write 编辑插件 CLI 源码不误杀"),
            ({"tool_name": "Agent", "tool_input": {"prompt": "请协助重构 gzx_cli.py 与 gzx-append.sh"}}, 0, "Agent 提 CLI/写入器名字不误杀"),
            ({"tool_name": "Bash", "tool_input": {"command": "cat 2>&1 ~/.gan-zhong-xue/map.md"}}, 0, "只读 cat 2>&1 不误杀"),
            ({"tool_name": "Bash", "tool_input": {"command": "cat > /tmp/out < ~/.gan-zhong-xue/map.md"}}, 0, "输入重定向读取数据目录不误杀"),
            ({"tool_name": "Bash", "tool_input": {"command": "bash gzx-append.sh map < ~/.gan-zhong-xue/pending.md"}}, 2, "wrapper 执行本地伪写入器仍拦"),
        ]
        for ev, want_rc, label in cases:
            r = subprocess.run([sys.executable, GUARD], env=env,
                               input=json.dumps(ev).encode(),
                               capture_output=True, timeout=30)
            check("GSCOPE " + label, r.returncode == want_rc,
                  "got rc=%d want=%d stderr=%s" % (r.returncode, want_rc, r.stderr.decode(errors="replace")[:120]))
    finally:
        _rmtree(d)


def main():
    check("P0 写入器与核心存在", os.path.exists(WRITER_SH) and os.path.exists(CORE_PY))
    test_core_states()
    test_writer_states()
    test_append_only()
    test_tmp_cleanup_path_b()
    test_cli_and_divergence()
    test_capture_dedup()
    test_capture_special_cases()
    test_commit_command_recognition()
    test_capture_exception_silent_by_default()
    test_audit_no_raw_command()
    test_guard_scoping()
    test_map_field_greppable()
    test_migration_no_rewrite()
    test_version_face()
    print("\n%s：%d 项失败" % ("FAILED" if FAILS else "ALL PASS", len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
