#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test-gzx-behavior.py — v1.2.0 守卫行为反例测试（board #129 dsh #3066 §4：别再枚举拼写）。

覆盖（全部在隔离临时目录跑；守卫的 GZX_DIR 指向一个**诱饵**数据目录，绝不碰真实 ~/.gan-zhong-xue）：
  L. 词法对照表：原文 → 词 / 操作符列表（表驱动，一行一例，含全部历史出洞形态）。
  B. 行为反例矩阵：启动器 × 路径写法 × 引号 / 前缀的笛卡尔积（自动生成，不手写）。
     凡守卫判「放行」的攻击形命令，真跑一遍（bash），断言诱饵数据目录未被写
     （哨兵文件哈希不变）。这能机械否证**未来**的拼写——任何「守卫放行但实际写到了
     诱饵目录」的命令都会红。三种环境逐一跑：
       ① 官方写入器不在 PATH；② 官方写入器在 PATH（裸名 = 合法直调，放行后真跑，
       写入必须只来自官方件）；③ 假写入器排在 PATH 最前。
  C. 牙口自证：不带官方名的诱饵脚本（设计上放行）必须被检出写入——证明「检出器」
     真的看得见写入；再把守卫的保护逐个摘掉（变异），矩阵必须变红。
     矩阵里另有一族「陪衬官方名」命令（Bugbot 4728123561）：官方路径只当参数带上，
     真正执行的是别的程序，直写诱饵目录——必须全拒。
运行：python hooks/test-gzx-behavior.py   （0 = 全过）
环境变量 GZX_GUARD_UNDER_TEST=<守卫路径> 可指定被测守卫（默认同目录 gzx-guard.py；
用于「把旧头守卫放进来跑，必须红」的对照）。
"""

import hashlib
import importlib.util
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

# Windows CI 运行器控制台编码非 UTF-8（cp1252 实测）：测试输出强制 UTF-8。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.environ.pop("GZX_GUARD_UNDER_TEST", None) or os.path.join(HERE, "gzx-guard.py")
PWNED = "PWNED"
OFFICIAL = "OFFICIAL-WRITE"
SENTINEL = "## 诱饵哨兵格 · 🟡 · 2026-01-01\n- 状态：待复验\n"
DECOY_FILES = ("map.md", "map.jsonl", "pending.md", "audit.jsonl")

FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ((" — " + detail) if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def _posix(p):
    """Git Bash 形态(/c/...)；POSIX 上原样。"""
    p = p.replace("\\", "/")
    if len(p) > 1 and p[1] == ":":
        p = "/" + p[0].lower() + p[2:]
    return p


def _find_bash():
    """跨平台探测可用 bash：POSIX 直接用系统 bash；Windows 优先找 Git Bash（避开 WSL）。"""
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


def _rmtree(path):
    """删目录树并返回「确实删干净了」（出错时清只读位重试）。"""
    def _onerr(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=_onerr)
    return not os.path.exists(path)


def _write(path, text, exec_bit=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    if exec_bit:
        try:
            os.chmod(path, 0o755)
        except OSError:
            pass


# ═════════════════════════════ L. 词法对照表 ═════════════════════════════

def op(*ops):
    return [("o", x) for x in ops]


# (原文, 期望项序列)。期望里字符串=词（引号已剥、转义已解），("o", x)=操作符；None=不可解析。
LEX_ROWS = [
    # ── 冒号 / PATH 前缀（Bugbot 4727166297 High）──
    (r"PATH=/usr/bin:/bin gzx-append.sh", ["PATH=/usr/bin:/bin", "gzx-append.sh"]),
    (r"PATH=/usr/bin:/bin bash /tmp/evil/gzx-append.sh map",
     ["PATH=/usr/bin:/bin", "bash", "/tmp/evil/gzx-append.sh", "map"]),
    (r"python3 -X pycache_prefix=/tmp/a:b /tmp/evil/gzx_cli.py",
     ["python3", "-X", "pycache_prefix=/tmp/a:b", "/tmp/evil/gzx_cli.py"]),
    # ── $ 劈裂族：$ 及其后的路径是同一个词 ──
    (r"cat x > $HOME/.gan-zhong-xue/map.jsonl",
     ["cat", "x"] + op(">") + ["$HOME/.gan-zhong-xue/map.jsonl"]),
    (r"cat x >$HOME/y", ["cat", "x"] + op(">") + ["$HOME/y"]),
    (r"cat x > ${HOME}/y", ["cat", "x"] + op(">") + ["${HOME}/y"]),
    (r"cat x > $(echo p)", ["cat", "x"] + op(">") + ["$(echo p)"]),
    (r"cat x > `echo p`", ["cat", "x"] + op(">") + ["`echo p`"]),
    (r"bash $(echo /tmp/evil/gzx-append.sh) map",
     ["bash", "$(echo /tmp/evil/gzx-append.sh)", "map"]),
    (r"echo $(( 1 << 2 ))", ["echo", "$(( 1 << 2 ))"]),          # 算术展开：<< 不切词
    (r"cmd > out $ARGS", ["cmd"] + op(">") + ["out", "$ARGS"]),   # $ARGS 是独立参数，不是目标
    (r"cmd 2>/dev/null $ARG", ["cmd", "2"] + op(">") + ["/dev/null", "$ARG"]),
    (r"cat a.txt >> " + '"$LOG"', ["cat", "a.txt"] + op(">>") + ["$LOG"]),
    (r"bash $SCRIPT map", ["bash", "$SCRIPT", "map"]),
    (r"bash$WR map", ["bash$WR", "map"]),                          # 粘连尾巴属于命令名
    (r"echo$HOME", ["echo$HOME"]),
    # ── 重定向操作符 ──
    (r"cat 2>&1 ~/.gan-zhong-xue/map.md",
     ["cat", "2"] + op(">&") + ["1", "~/.gan-zhong-xue/map.md"]),
    (r"cat x >~/.gan-zhong-xue/map.md", ["cat", "x"] + op(">") + ["~/.gan-zhong-xue/map.md"]),
    (r"cat map.md &> ~/.gan-zhong-xue/map.jsonl",
     ["cat", "map.md"] + op("&>") + ["~/.gan-zhong-xue/map.jsonl"]),
    (r"cat map.md >| out", ["cat", "map.md"] + op(">|") + ["out"]),
    (r"cat map.md &>> out", ["cat", "map.md"] + op("&>>") + ["out"]),
    (r"cat > /tmp/out < ~/.gan-zhong-xue/map.md",
     ["cat"] + op(">") + ["/tmp/out"] + op("<") + ["~/.gan-zhong-xue/map.md"]),
    (r"cat x 2> err", ["cat", "x", "2"] + op(">") + ["err"]),
    (r"cat x >", ["cat", "x"] + op(">")),
    (r"echo '> $HOME'", ["echo", "> $HOME"]),                   # 引号里的 > 不是重定向
    (r'echo "a > b"', ["echo", "a > b"]),
    # ── 引号粘合族（claude #3065 §2 / grok #3069 §2）──
    (r'bash "fake gzx-append.sh" map', ["bash", "fake gzx-append.sh", "map"]),
    (r'bash"fake gzx-append.sh"map', ["bashfake gzx-append.shmap"]),
    (r'bash"my notes.txt"', ["bashmy notes.txt"]),
    (r'bash --rcfile "a b" gzx-append.sh', ["bash", "--rcfile", "a b", "gzx-append.sh"]),
    (r'python3 -X "pycache_prefix=/tmp/a b" /tmp/evil/gzx_cli.py save-cell',
     ["python3", "-X", "pycache_prefix=/tmp/a b", "/tmp/evil/gzx_cli.py", "save-cell"]),
    (r"bash ' gzx-append.sh' map", ["bash", " gzx-append.sh", "map"]),
    # ── 引号 / 转义拼接族（dsh #3055 §1）──
    (r'cat x > ~/.gan-zhong-""xue/map.jsonl',
     ["cat", "x"] + op(">") + ["~/.gan-zhong-xue/map.jsonl"]),
    (r"cat x > ~/.gan-zhong-'xue'/map.jsonl",
     ["cat", "x"] + op(">") + ["~/.gan-zhong-xue/map.jsonl"]),
    (r"cat x > ~/.gan-zhong-xu\e/map.jsonl",
     ["cat", "x"] + op(">") + ["~/.gan-zhong-xue/map.jsonl"]),
    (r"cat x > $'/home/u/.gan-zhong-xue/map.jsonl'",
     ["cat", "x"] + op(">") + ["$/home/u/.gan-zhong-xue/map.jsonl"]),
    (r"grep -c a\ b file", ["grep", "-c", "a b", "file"]),        # 转义空格同属一个词
    (r"C:\Program Files\x", ["C:Program", "Filesx"]),             # 词外反斜杠会吃掉字符
    (r"'C:\Program Files\x'", ["C:\\Program Files\\x"]),
    (r'"C:\Program Files\x"', ["C:\\Program Files\\x"]),          # 双引号里 \P \x 不被转义
    (r'echo "a\"b"', ["echo", 'a"b']),
    (r'echo "a\$b"', ["echo", "a$b"]),
    (r'a=b c="d e" cmd', ["a=b", "c=d e", "cmd"]),
    # ── [ 与保留字（if [ 不粘连）──
    (r"[ -s /tmp/x ]", ["[", "-s", "/tmp/x", "]"]),
    (r"if [ -s x ]; then cat y; fi",
     ["if", "[", "-s", "x", "]"] + op(";") + ["then", "cat", "y"] + op(";") + ["fi"]),
    # ── 结构符 / 管道 / 子壳 / 进程替换 ──
    (r"cat a;cat b", ["cat", "a"] + op(";") + ["cat", "b"]),
    (r"a && b || c | d |& e & f",
     ["a"] + op("&&") + ["b"] + op("||") + ["c"] + op("|") + ["d"] + op("|&") + ["e"]
     + op("&") + ["f"]),
    (r"( cd x )", op("(") + ["cd", "x"] + op(")")),
    (r"diff <(a) >(b)", ["diff"] + op("<(") + ["a"] + op(")") + op(">(") + ["b"] + op(")")),
    ("cat a\ncat b", ["cat", "a"] + op("\n") + ["cat", "b"]),
    (r"find . -exec rm {} \;", ["find", ".", "-exec", "rm", "{}", ";"]),   # 转义分号是字面词
    (r"case $x in a) b;; esac", ["case", "$x", "in", "a"] + op(")") + ["b"] + op(";;") + ["esac"]),
    # ── heredoc / herestring 首行 ──
    (r"python3 - <<'PY'", ["python3", "-"] + op("<<") + ["PY"]),
    (r"cat x <<-'E'", ["cat", "x"] + op("<<-") + ["E"]),
    (r"cat <<< " + '"$LINE"', ["cat"] + op("<<<") + ["$LINE"]),
    (r"grep '<<' notes.txt", ["grep", "<<", "notes.txt"]),       # 引号里的 << 不是操作符
    # ── 命令替换：活的留在词里并标 subst；单引号里是字面 ──
    (r'echo "$(rm x)y"', ["echo", "$(rm x)y"]),
    (r'echo "`rm x`"', ["echo", "`rm x`"]),
    (r"echo '$(rm x)'", ["echo", "$(rm x)"]),
    (r"echo '$x'", ["echo", "$x"]),
    # ── 注释（词首 #）与其余 ──
    (r"echo a#b # comment ; rm x", ["echo", "a#b"]),
    (r"ls ~/.gan-zhong-xue # see ~/.gan-zhong-xue/map.md", ["ls", "~/.gan-zhong-xue"]),
    (r"", []),
    (r"# only a comment", []),
    (r"ls *.py", ["ls", "*.py"]),
    (r"echo {a,b}", ["echo", "{a,b}"]),
    ("cat   x\ty", ["cat", "x", "y"]),
    # ── 不可解析（引号 / 反引号 / $( 未闭合、末尾悬挂反斜杠）──
    ("git commit -m 'unterminated", None),
    ("echo `x", None),
    ("echo $(x", None),
    ("echo a\\", None),
]

# (原文, 词序号, 期望 quoted, 期望 subst)
LEX_FLAGS = [
    (r'bash "my notes.txt"', 1, True, False),
    (r"bash my notes.txt", 1, False, False),
    (r'bash"my notes.txt"', 0, True, False),
    (r'echo "$(rm x)"', 1, True, True),
    (r"echo '$(rm x)'", 1, True, False),
    (r"echo $(rm x)", 1, False, True),
    (r"echo `rm x`", 1, False, True),
    (r"echo '$x'", 1, True, False),
    (r"echo $x", 1, False, False),
]

_PLAIN_ROW = re.compile(r"[#\\$`();<>&|\n'\"]")


def _seq(items):
    return [(t.kind, t.text) for t in items]


def _want(seq):
    """期望序列：字符串=词，("o", x)=操作符。"""
    return [("w", x) if isinstance(x, str) else x for x in seq]


def test_lex(g):
    if not hasattr(g, "_lex"):
        print("SKIP 词法对照表：被测守卫无词法底座 _lex（旧头对照跑专用）")
        return
    bad = []
    for raw, want in LEX_ROWS:
        toks, err = g._lex(raw)
        if want is None:
            if not err:
                bad.append("%r 应判不可解析" % raw)
            continue
        if err:
            bad.append("%r 不可解析: %s" % (raw, err))
            continue
        if _seq(toks) != _want(want):
            bad.append("%r\n        得 %r\n        期 %r" % (raw, _seq(toks), _want(want)))
    check("L1 词法对照表「原文 → 词/操作符」%d 行逐行一致" % len(LEX_ROWS), not bad,
          "\n        ".join(bad[:6]))

    # 跨度是原文的真实切片，且每个词的切片单独再扫仍得到同一个词（词法幂等）
    bad = []
    for raw, want in LEX_ROWS:
        if want is None:
            continue
        toks, _err = g._lex(raw)
        end = 0
        for t in toks:
            if not (0 <= t.start < t.end <= len(raw)) or t.start < end:
                bad.append("%r 跨度越界/重叠 @%d-%d" % (raw, t.start, t.end))
                break
            end = t.end
            if t.kind == "w":
                again, err = g._lex(raw[t.start:t.end])
                if err or len(again) != 1 or again[0].text != t.text:
                    bad.append("%r 词切片 %r 再扫不一致" % (raw, raw[t.start:t.end]))
                    break
    check("L2 词跨度=原文真实切片且逐词再扫幂等", not bad, "; ".join(bad[:4]))

    # 无特殊字符的行与标准库 shlex 互证（两套独立实现对同一原文给出相同的词）
    bad = []
    n = 0
    for raw, want in LEX_ROWS:
        if want is None or _PLAIN_ROW.search(raw):
            continue
        toks, _err = g._lex(raw)
        mine = [t.text for t in toks if t.kind == "w"]
        n += 1
        ref = shlex.split(raw)
        if mine != ref:
            bad.append("%r: 本词法 %r ≠ shlex %r" % (raw, mine, ref))
    check("L3 纯文本行与 shlex 互证一致（%d 行）" % n, not bad, "; ".join(bad[:4]))

    bad = []
    for raw, idx, quoted, subst in LEX_FLAGS:
        toks, err = g._lex(raw)
        if err or len(toks) <= idx or toks[idx].quoted != quoted or toks[idx].subst != subst:
            bad.append("%r 词%d 期望 quoted=%s subst=%s" % (raw, idx, quoted, subst))
    check("L4 引号 / 命令替换标记（quoted、subst）%d 行一致" % len(LEX_FLAGS), not bad,
          "; ".join(bad[:4]))


# ═════════════════════════════ B. 行为反例矩阵 ═════════════════════════════

FAKE_WRITER = r"""#!/usr/bin/env bash
# 行为测试夹具：假写入器。只往诱饵目录写一行可识别标记；
# 变量缺失即退出（sudo 清环境、env -i 时不会落到任何别的盘）。
D="${GZX_BEHAVIOR_DECOY_POSIX:-}"
[ -n "$D" ] || exit 0
printf 'PWNED writer %s\n' "$0 $*" >> "$D/map.md"
"""
FAKE_CLI = r"""#!/usr/bin/env python3
# 行为测试夹具：假 CLI。只往诱饵目录写一行可识别标记；变量缺失即退出。
import os, sys
d = os.environ.get("GZX_BEHAVIOR_DECOY") or ""
if d:
    with open(os.path.join(d, "map.md"), "a", encoding="utf-8") as f:
        f.write("PWNED cli %s\n" % (sys.argv[1:],))
"""
OFFICIAL_WRITER = r"""#!/usr/bin/env bash
# 行为测试夹具：诱饵目录里的「官方写入器」。写入带 OFFICIAL 标记（合法直调的证明）。
D="${GZX_BEHAVIOR_DECOY_POSIX:-}"
[ -n "$D" ] || exit 0
printf 'OFFICIAL-WRITE %s\n' "$*" >> "$D/map.md"
"""
PLAIN_SH = r"""#!/usr/bin/env bash
# 行为测试夹具：**不带官方名**的诱饵脚本——设计上守卫放行（已申报边界），证明检出器有牙口。
D="${GZX_BEHAVIOR_DECOY_POSIX:-}"
[ -n "$D" ] || exit 0
printf 'PWNED plain-sh\n' >> "$D/map.md"
"""
PLAIN_PY = r"""#!/usr/bin/env python3
import os
d = os.environ.get("GZX_BEHAVIOR_DECOY") or ""
if d:
    with open(os.path.join(d, "map.md"), "a", encoding="utf-8") as f:
        f.write("PWNED plain-py\n")
"""

# 启动器形态（{t}=目标路径写法）。
WRAPPERS_SH = [
    "{t} map", "bash {t} map", "sh {t} map", "zsh {t} map", "dash {t} map", "ksh {t} map",
    "source {t} map", ". {t} map",
    "env bash {t} map", "nice bash {t} map", "nice -n 5 bash {t} map",
    "timeout 5 bash {t} map", "nohup bash {t} map", "setsid bash {t} map",
    "stdbuf -o0 bash {t} map", "xargs -n1 bash {t} map", "sudo -n bash {t} map",
    "command bash {t} map", "exec bash {t} map", "time bash {t} map", "busybox sh {t} map",
    "strace -f bash {t} map", "doas bash {t} map", "pkexec bash {t} map", "chroot / bash {t} map",
    "sudo -n env nice -n 5 timeout 5 bash {t} map", "env -i bash {t} map",
    "bash.exe {t} map",
    # dsh #3075 §3 点名的同族启动器（此前不在名单里，带官方名也不扫）
    "fakeroot bash {t} map", "unshare -r bash {t} map", "flock /tmp/lock bash {t} map",
    "taskset -c 0 bash {t} map", "nsenter -t 1 bash {t} map", "runuser -u root -- bash {t} map",
    "sg root -c 'bash {t} map'", "systemd-run bash {t} map", "chrt -f 99 bash {t} map",
    "ionice -c 3 bash {t} map",
    # 启动器链首的「第一个位置参数不是程序」：官方件会被当参数、执行后面的程序
    "script -q {t} bash {E}/plain.sh", "flock {t} bash {E}/plain.sh",
    # find -exec 族（claude #3074 §2）：{{}} 是格式串里的字面花括号
    "find . -name x -exec bash {t} map {{}} \\;",
    "find . -name x -execdir bash {t} map {{}} +",
    "find . -name x -ok python3 {t} save-cell {{}} \\;",
    # 启动器直接包着裸名（不经 shell 解释器）——参数位的裸名同样不可证，守卫应拒
    "env {t} map", "sudo -n {t} map", "command {t} map", "exec {t} map", "time {t} map",
    "nohup {t} map", "timeout 5 {t} map", "nice {t} map", "xargs -n1 {t} map",
]
WRAPPERS_PY = [
    "python3 {t} save-cell", "python3 -W ignore {t} save-cell", "python3 -x {t} save-cell",
    "python3 -X dev {t} save-cell", "python3 --check-hash-based-pycs default {t} save-cell",
    "python3 -- {t} save-cell", "python3.12 {t} save-cell", "python {t} save-cell",
    "perl {t}", "ruby {t}", "node {t}",
    "env python3 {t} save-cell", "sudo -n python3 {t} save-cell",
    "timeout 5 python3 {t} save-cell", "xargs -n1 python3 {t} save-cell",
    "py {t} save-cell", "pythonw {t}", "fakeroot python3 {t} save-cell",
]
# 把命令放进「带引号的程序串」的形态
PAYLOAD_WRAPPERS = [
    "su -c 'bash {t} map'", "script -qc 'bash {t} map' /dev/null", "watch -n1 'bash {t} map'",
    "bash -c 'sh {t} map'", "bash -lc 'sh {t} map'", "bash <<< '{t} map'",
    "bash -c 'python3 {t} save-cell'",
]
QUOTES = [("{t}", ""), ("'{t}'", "single"), ('"{t}"', "double")]
PREFIXES = [("", ""), ("FOO=bar ", "assign"), ("PATH=/usr/bin:/bin ", "path-colon"),
            ("PATH=$PATH:/usr/bin ", "path-dollar")]


def _snapshot(root):
    out = {}
    for dp, _dn, fns in os.walk(root):
        for fn in fns:
            p = os.path.join(dp, fn)
            try:
                with open(p, "rb") as f:
                    out[os.path.relpath(p, root)] = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                out[os.path.relpath(p, root)] = "<unreadable>"
    return out


def _kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run(bash, cmd, env, cwd, decoy, timeout=8.0):
    """真跑一条命令。诱饵目录一有变化立即树杀（watch / setsid 这类会反复写或常驻）。"""
    try:
        proc = subprocess.Popen(
            [bash, "-c", cmd], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=env, cwd=cwd,
            start_new_session=(os.name != "nt"))
    except OSError:
        return
    try:
        proc.stdin.write(b"x\n")
        proc.stdin.close()
    except Exception:
        pass
    before = _snapshot(decoy)
    deadline = time.time() + timeout
    while proc.poll() is None:
        if _snapshot(decoy) != before:
            _kill_tree(proc)
            break
        if time.time() > deadline:
            _kill_tree(proc)
            break
        time.sleep(0.05)
    try:
        proc.wait(timeout=10)
    except Exception:
        pass
    time.sleep(0.25)                     # 留给被树杀的孙进程落定


def _contains(root, needle):
    for dp, _dn, fns in os.walk(root):
        for fn in fns:
            try:
                with open(os.path.join(dp, fn), "rb") as f:
                    if needle.encode("utf-8") in f.read():
                        return True
            except OSError:
                pass
    return False


def test_behavior(g, bash, work):
    home = os.path.join(work, "home")
    decoy = os.path.join(home, ".gan-zhong-xue")
    evil = os.path.join(work, "evil")
    spaced = os.path.join(work, "evil dir")
    shim = os.path.join(work, "shim")
    _write(os.path.join(decoy, "gzx-append.sh"), OFFICIAL_WRITER, exec_bit=True)
    for d in (evil, spaced):
        _write(os.path.join(d, "gzx-append.sh"), FAKE_WRITER, exec_bit=True)
        _write(os.path.join(d, "gzx-append"), FAKE_WRITER, exec_bit=True)
        _write(os.path.join(d, "gzx_cli.py"), FAKE_CLI)
    _write(os.path.join(evil, "plain.sh"), PLAIN_SH, exec_bit=True)
    _write(os.path.join(evil, "plain.py"), PLAIN_PY)
    # 不带官方名、但文件名叫 bash 的诱饵程序（带路径的同名「启动器」不能因此获得豁免）
    _write(os.path.join(evil, "bash"), PLAIN_SH, exec_bit=True)
    py = _posix(sys.executable)
    for name in ("python3", "python", "python3.12"):
        # 解释器垫片：三个名字都转调当前解释器（Windows 商店版 python3 是起不来的别名桩）
        _write(os.path.join(shim, name), '#!/usr/bin/env bash\nexec "%s" "$@"\n' % py,
               exec_bit=True)

    def restore():
        _write(os.path.join(decoy, "gzx-append.sh"), OFFICIAL_WRITER, exec_bit=True)
        _write(os.path.join(decoy, "map.md"), SENTINEL)
        _write(os.path.join(decoy, "map.jsonl"), '{"概念":"诱饵"}\n')
        _write(os.path.join(decoy, "pending.md"), "- 诱饵未挖点\n")
        _write(os.path.join(decoy, "audit.jsonl"), '{"event":"hook_alive"}\n')
        for dp, _dn, fns in os.walk(decoy):
            for fn in fns:
                if fn not in DECOY_FILES + ("gzx-append.sh",):
                    try:
                        os.remove(os.path.join(dp, fn))
                    except OSError:
                        pass

    base_path = os.environ.get("PATH", "")
    # 解释器垫片排在 PATH 最前：商店版 WindowsApps\python3.exe 是起不来的别名桩，
    # 排在前面会把垫片挡住，python 形态就测不到真的执行。
    shimmed = shim + os.pathsep + base_path

    def guard_path(extra=""):
        return (extra + os.pathsep if extra else "") + shimmed

    env_common = dict(os.environ)
    env_common.pop("GZX_DIR", None)
    env_common["GZX_BEHAVIOR_DECOY"] = decoy
    env_common["GZX_BEHAVIOR_DECOY_POSIX"] = _posix(decoy)

    def make_env(extra=""):
        return dict(env_common, PATH=guard_path(extra))

    e_posix, s_posix = _posix(evil), _posix(spaced)
    # (形态名, 路径写法, 允许的引号变体)。目录名带空格的路径不加引号会被拆成两个词
    # （命令名变成目录路径本身，bash 报错不执行）——那不是一种「路径写法」，只生成加引号的变体。
    targets_sh = [("<E>/gzx-append.sh", e_posix + "/gzx-append.sh", None),
                  ("<E>/gzx-append", e_posix + "/gzx-append", None),
                  ("./gzx-append.sh", "./gzx-append.sh", None),
                  ("<S>/gzx-append.sh", s_posix + "/gzx-append.sh", QUOTES[1:]),
                  ("bare gzx-append.sh", "gzx-append.sh", None),
                  ("bare gzx-append", "gzx-append", None)]
    targets_py = [("<E>/gzx_cli.py", e_posix + "/gzx_cli.py", None),
                  ("<S>/gzx_cli.py", s_posix + "/gzx_cli.py", QUOTES[1:]),
                  ("bare gzx_cli.py", "gzx_cli.py", None)]

    def gen(targets, wrappers):
        for tk, t, quotes in targets:
            for w in wrappers:
                for q, _qk in (quotes or QUOTES):
                    for pre, _pk in PREFIXES:
                        yield (pre + w.format(t=q.format(t=t), E=e_posix),
                               (tk, w.replace("{E}", "<E>")))

    matrix = (list(gen(targets_sh, WRAPPERS_SH + PAYLOAD_WRAPPERS))
              + list(gen(targets_py, WRAPPERS_PY))
              + list(gen(targets_sh, WRAPPERS_PY[:2])))
    # ── 陪衬官方名族（Bugbot 4728123561）：官方路径只当参数带上，真正执行的是别的程序。
    #    {D}=诱饵数据目录（守卫的 GZX_DIR），{E}=假件目录。这些必须全拒；一旦放行就会真写诱饵。
    #    （`env bash <无官方名脚本> <官方路径>` 不在此列：被执行的是不带官方名的脚本，属已申报边界。）
    d_posix = _posix(decoy)
    DECOY_ARG = [
        "env tee {D}/map.md {D}/gzx-append.sh",
        "env sh -c 'printf x >> {D}/map.md' {D}/gzx-append.sh",
        "sudo -n sh -c 'printf x >> {D}/map.md' {D}/gzx-append.sh",
        "timeout 5 bash -c 'printf x >> {D}/map.md' {D}/gzx-append.sh",
        "nohup sh -c 'printf x >> {D}/map.md' bash {D}/gzx-append.sh",
        "env -i sh -c 'printf x >> {D}/map.md' {D}/gzx-append.sh",
        "env bash {E}/plain.sh {D}/map.md {D}/gzx-append.sh",
        "W={D}/gzx-append.sh sudo -n tee {D}/map.md $W",
        "FOO={D}/gzx-append.sh env bash {E}/plain.sh",
        "env -i bash {E}/plain.sh {D}/gzx-append.sh",
        # dsh #3075 §2 判别用例：贴在选项上的载荷（整段无空白、无扩展算子）
        "python3 -c'exec(open(\"/tmp/p\").read())' {D}/gzx-append.sh",
        "sudo -n tee x {D}/gzx-append.sh",
        "bash -c'cat>{D}/map.md' {D}/gzx-append.sh",
        # Bugbot 4728174715：官方件在前 / 只带官方件——写入器本体被当输出文件
        "sudo -n tee {D}/gzx-append.sh",
        "sudo -n mv {D}/gzx-append.sh {D}/map.md",
        "strace -o {D}/gzx-append.sh bash {E}/plain.sh",
        # 带路径的「同名启动器」：/…/bash 叫 bash，却是未知程序
        "env {E}/bash {D}/gzx-append.sh",
    ]
    decoy_arg = [(w.replace("{D}", d_posix).replace("{E}", e_posix), ("decoy-arg", w))
                 for w in DECOY_ARG]
    matrix += decoy_arg
    check("B0 矩阵规模（启动器 × 路径写法 × 引号 × 前缀的笛卡尔积）≥ 2000", len(matrix) >= 2000,
          "got %d" % len(matrix))

    saved_path = os.environ.get("PATH")

    def use_path(p):
        if p is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = p

    def classify_all(cmds):
        out = []
        for c, k in cmds:
            d = g.classify(c)
            out.append((c, k, d.allow, d.rule))
        return out

    def run_allowed(cmds, env, official_ok):
        """逐条真跑放行的命令。返回 [(命令, 原因)]；official_ok=True 时只允许官方件写入。"""
        bad = []
        for c in cmds:
            restore()
            before = _snapshot(decoy)
            _run(bash, c, env, evil, decoy)
            if _contains(decoy, PWNED):
                bad.append((c, "诱饵被假件写入（PWNED）"))
            elif _snapshot(decoy) != before and not official_ok:
                bad.append((c, "诱饵被改动"))
            elif _snapshot(decoy) != before and not _contains(decoy, OFFICIAL):
                bad.append((c, "诱饵被改动且不是官方件写的"))
        restore()
        return bad

    try:
        # ── 环境①：官方写入器不在 PATH ──
        use_path(guard_path())
        env1 = make_env()
        res1 = classify_all(matrix)
        allowed1 = [(c, k) for c, k, a, _r in res1 if a]
        check("B1 环境①（官方件不在 PATH）：矩阵 %d 条**全部拒**" % len(matrix), not allowed1,
              "放行 %d 条，例如 %r" % (len(allowed1), allowed1[:3]))
        da_allowed = [(c, r) for c, k, a, r in res1 if k[0] == "decoy-arg" and a]
        check("B6 陪衬官方名族（Bugbot 4728123561 / 4728174715、dsh #3075 §2）：官方路径当参数、"
              "贴在选项上的载荷、同名「启动器」等 %d 条**全拒**" % len(decoy_arg), not da_allowed,
              "放行 %d 条，例如 %r" % (len(da_allowed), da_allowed[:2]))
        # 判据是「拒 + 规则名」，不是「没观察到写」：逐条列出规则名
        da_rules = {}
        for c, k, a, r in res1:
            if k[0] == "decoy-arg":
                da_rules[r.split(":")[0]] = da_rules.get(r.split(":")[0], 0) + 1
        check("B6b 陪衬官方名族的拒绝都带明确规则名（prefix=不给豁免 / identity=名字不在固定位 / "
              "payload=程序串里的官方名）：%s" % da_rules,
              set(da_rules) <= {"prefix", "identity", "payload"} and sum(da_rules.values()) == len(decoy_arg),
              "规则名分布 %s" % da_rules)

        # ── 良性命令不得误伤（收窄启动器清单与 find 专项的对照面）──
        benign = [
            "find . -name gzx_cli.py",
            "find . -name '*.py' -exec python3 -m py_compile {} \\;",
            "find . -name '*.md' -exec grep -l gzx-append {} \\;",     # 官方名只是搜索模式
            "git add skills/gan-zhong-xue/gzx_cli.py",
            "git commit -m 'touch gzx_cli.py'",
            "echo see gzx-append",
            "cp skills/gan-zhong-xue/gzx_cli.py /tmp/x.py",
            "vim gzx_cli.py",
            "awk '{print NR}' gzx_cli.py",
            "python3 hooks/test-gzx-invariants.py",
            "flock /tmp/lock python3 -m pytest",
            "taskset -c 0 make",
            "fakeroot make package",
        ]
        ben_denied = [(c, g.classify(c).rule) for c in benign if not g.classify(c).allow]
        check("B7 良性命令不误伤（同仓开发、find 搜索模式与 -exec 的数据参数、不提官方名的启动器命令）"
              "%d 条全放行" % len(benign), not ben_denied, "误拒 %r" % ben_denied[:3])

        # ── 环境③：假写入器排在 PATH 最前 ──
        use_path(guard_path(evil))
        bare = [(c, k) for c, k, _a, _r in res1 if k[0].startswith("bare")]
        allowed3 = [(c, k) for c, k, a, _r in classify_all(bare) if a]
        check("B3 环境③（假件排在 PATH 最前）：裸名命令 %d 条**全部拒**" % len(bare),
              not allowed3, "放行 %d 条，例如 %r" % (len(allowed3), allowed3[:3]))

        # ── 环境②：官方写入器在 PATH（裸名 = 合法直调）──
        use_path(guard_path(decoy))
        env2 = make_env(decoy)
        d_direct = g.classify("gzx-append.sh map")
        check("B2a 环境②夹具成立：裸名 gzx-append.sh 解析到诱饵目录的官方件（守卫判放行）",
              d_direct.allow, "rule=%s" % d_direct.rule)
        res2 = classify_all(matrix)
        allowed2 = [(c, k) for c, k, a, _r in res2 if a]
        # 实测（见文件头）：bash/sh 找脚本参数时先看当前目录，所以裸名只在**命令名位**可信
        legit2 = [(c, k) for c, k in allowed2 if k[1] == "{t} map"
                  and k[0] in ("bare gzx-append.sh", "bare gzx-append")
                  and not c.startswith("PATH=")]
        check("B2b 环境②：放行的**只有**命令名位的裸写入器名（路径形态、参数位的裸名、裸 CLI 名全拒）",
              sorted(legit2) == sorted(allowed2),
              "多放行 %d 条，例如 %r" % (len(set(allowed2) - set(legit2)),
                                    sorted(set(allowed2) - set(legit2))[:3]))
        with_path = [c for c, k in allowed2 if c.startswith("PATH=")]
        check("B2c 环境②：带 PATH= 前缀的裸名一律拒（守卫按自己的 PATH 解析，与命令里的不一致）",
              not with_path, "%d 条被放行，例如 %r" % (len(with_path), with_path[:3]))
        check("B2d 环境②：命令名位的裸名确被放行（否则本环境没测到合法直调）",
              len(allowed2) > 0, "放行 0 条")

        # ── 环境④：PATH 最前面有「当前目录项」（.），官方件排在其后 ──
        use_path("." + os.pathsep + guard_path(decoy))     # 当前目录项排在官方件前面
        d_dot = g.classify("gzx-append.sh map")
        check("B2e 环境④（PATH 最前是 .）：命令名位的裸名也不可证 ⇒ 拒"
              "（当前目录里的同名假件会排在官方件前面）",
              not d_dot.allow, "rule=%s——守卫放行了带当前目录项的 PATH 解析" % d_dot.rule)
    finally:
        use_path(saved_path)

    # ── 真跑 ──
    def _fmt(violations):
        pwn = [1 for _c, w in violations if "PWNED" in w]
        return ("共 %d 条命令改动了诱饵目录（其中 %d 条是假件写入）；例如 "
                % (len(violations), len(pwn))
                + "；".join("%s：%s" % (c, w) for c, w in violations[:3]))

    restore()
    v1 = run_allowed([c for c, _k in allowed1], env1, False)
    check("B4 环境①：守卫放行的攻击形命令真跑后诱饵未被写（放行并真跑 %d 条）"
          % len(allowed1), not v1, _fmt(v1))

    # 环境②：放行的命令名位裸名真跑——写入只许来自官方件
    sample2 = [c for c, k in allowed2]
    restore()
    v2 = run_allowed(sample2, env2, True)
    check("B5 环境②：命令名位的裸名真跑 %d 条，写入只来自官方件（无 PWNED）" % len(sample2),
          not v2, _fmt(v2))

    # ── C. 牙口自证 ──
    use_path(guard_path())
    try:
        canaries = ["bash %s/plain.sh" % e_posix, "%s/plain.sh" % e_posix,
                    "python3 %s/plain.py" % e_posix, "env python3 %s/plain.py" % e_posix]
        seen = []
        for c in canaries:
            restore()
            d = g.classify(c)
            _run(bash, c, env1, evil, decoy)
            if _contains(decoy, PWNED):
                seen.append((c, d.allow))
        restore()
        check("C1 检出器有牙口：不带官方名的诱饵脚本写入被检出（%d/%d 条真写出标记）"
              % (len(seen), len(canaries)), len(seen) >= 2,
              "只检出 %r——bash / python 垫片或夹具在本机不可执行，本套件的「未写」断言无证明力"
              % [c for c, _a in seen])
        check("C1b 不带官方名的脚本是已申报边界（守卫放行，不伪造通过）",
              bool(seen) and all(a for _c, a in seen))

        # 环境②的合法直调确实经官方件落了盘（证明该环境真的执行了，而不是全靠「命令不存在」）
        restore()
        use_path(guard_path(decoy))
        _run(bash, "gzx-append.sh map", env2, evil, decoy)
        official_seen = _contains(decoy, OFFICIAL) and not _contains(decoy, PWNED)
        use_path(guard_path())
        restore()
        check("C1c 环境②合法直调真跑：命令名位裸名经官方件落盘（标记在、假件标记不在）——"
              "同时印证夹具前提：bash 对命令名只搜 PATH、不看当前目录（当前目录就有同名假件）",
              official_seen, "gzx-append.sh map 未经诱饵目录官方件落盘")

        # 变异：逐个摘掉保护，矩阵必须变红（测试有牙）
        subset = [
            "bash %s/gzx-append.sh map" % e_posix,
            "%s/gzx-append.sh map" % e_posix,
            "env bash %s/gzx-append.sh map" % e_posix,
            "nice -n 5 bash %s/gzx-append.sh map" % e_posix,
            "xargs -n1 bash %s/gzx-append.sh map" % e_posix,
            "source %s/gzx-append.sh map" % e_posix,
            "python3 -x %s/gzx_cli.py save-cell" % e_posix,
            "python3 %s/gzx_cli.py save-cell" % e_posix,
            "env python3 %s/gzx_cli.py save-cell" % e_posix,
            "bash -c 'sh %s/gzx-append.sh map'" % e_posix,
            "bash <<< '%s/gzx-append.sh map'" % e_posix,
            "env bash gzx-append.sh map",          # 参数位裸名（当前目录即 evil，有同名假件）
            "python3 gzx_cli.py save-cell",
            decoy_arg[0][0], decoy_arg[1][0],      # 陪衬官方名：别的程序直写诱饵
            decoy_arg[11][0],                      # 贴在选项上的载荷（dsh #3075 §2）
            "fakeroot bash %s/gzx-append.sh map" % e_posix,
        ]
        names = ("_identity_ok", "_is_launcher_cmd", "_name_hit", "_has_expansion",
                 "_official_chunk_clean")
        orig = dict((n, getattr(g, n, None)) for n in names)

        def undo():
            for n, fn in orig.items():
                if fn is not None:
                    setattr(g, n, fn)

        def mutated(label, patch, minimum):
            if any(orig[n] is None for n in names):
                print("SKIP 变异「%s」：被测守卫无对应的保护函数（旧头对照跑专用）" % label)
                return
            patch()
            try:
                allowed = [c for c in subset if g.classify(c).allow]
                bad = run_allowed(allowed, env1, False)
            finally:
                undo()
            check("C2 变异「%s」：摘掉后矩阵变红（放行 %d 条，真跑后诱饵被写 %d 条，需 ≥%d）"
                  % (label, len(allowed), len(bad), minimum), len(bad) >= minimum,
                  "放行 %d 条但只检出 %d 条写入" % (len(allowed), len(bad)))

        def m1():
            g._identity_ok = lambda path, name: True

        def m2():
            g._is_launcher_cmd = lambda name: name in g._NAME_BASENAMES

        def m3():
            real = orig["_name_hit"]

            def no_payload(text):
                hit = real(text)
                return None if (hit and hit[0] == "payload") else hit
            g._name_hit = no_payload

        def m4():
            g._official_chunk_clean = lambda c: True
        mutated("身份检查（_identity_ok 恒真）", m1, 5)
        mutated("启动器清单（只剩官方名为命令名）", m2, 4)
        mutated("程序串里的官方名（payload 规则）", m3, 1)
        mutated("陪衬官方名（_official_chunk_clean 恒真）", m4, 2)

        if orig["_has_expansion"] is not None:
            g._has_expansion = lambda s: False
            try:
                leaked = [c for c in ("bash $SCRIPT map", "bash gzx_*.sh map", "cat x > $VAR")
                          if g.classify(c).allow]
            finally:
                undo()
            check("C2 变异「字面性闸（_has_expansion 恒假）」：$ 与通配形态变为放行", bool(leaked),
                  "摘掉后三条仍全拒——字面性闸不在判定路径上")
    finally:
        use_path(saved_path)
        restore()


def main():
    work = tempfile.mkdtemp(prefix="gzx-beh-")
    try:
        decoy = os.path.join(work, "home", ".gan-zhong-xue")
        _write(os.path.join(decoy, "map.md"), SENTINEL)
        _write(os.path.join(decoy, "map.jsonl"), '{"概念":"诱饵"}\n')
        _write(os.path.join(decoy, "pending.md"), "- 诱饵未挖点\n")
        _write(os.path.join(decoy, "audit.jsonl"), '{"event":"hook_alive"}\n')
        # 被测守卫以「诱饵目录」为 GZX_DIR 装载：守卫保护的就是诱饵，真实数据目录不可达
        os.environ["GZX_DIR"] = decoy
        try:
            spec = importlib.util.spec_from_file_location("gzx_guard_behavior", GUARD)
            g = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(g)
        finally:
            os.environ.pop("GZX_DIR", None)
        guard_dir = os.path.normcase(os.path.realpath(getattr(g, "GZX_DIR", "")))
        check("B00 夹具隔离：守卫的数据目录就是诱饵目录（真实 ~/.gan-zhong-xue 不可达）",
              guard_dir == os.path.normcase(os.path.realpath(decoy)),
              "guard GZX_DIR=%r" % guard_dir)
        test_lex(g)
        bash = _find_bash()
        if bash is None:
            print("SKIP 行为反例矩阵：本机无可用 bash（跳过即跳过，不伪造 PASS）")
        else:
            test_behavior(g, bash, work)
    finally:
        if not _rmtree(work):
            print("WARN 行为测试临时目录未能清干净: %s" % work)
    print("\n%s：%d 项失败" % ("FAILED" if FAILS else "ALL PASS", len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
