#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gzx-guard.py — gan-zhong-xue 数据面兜底 hook（v1.2.0）。

定位：减速带 + 审计，不是安全边界。真不变量在写入器 gzx-append.sh / gzx_cli.py。
v1.2.0 收窄范围判据：不再以泛化的 "gan-zhong-xue" 字样触发拦截（避免误杀同仓开发、
文档编辑及 Agent 提示词），改为精确指向运行时数据目录（.gan-zhong-xue / GZX_DIR）。

判定流（board #129 收敛，2026-11-28 守卫重写）——只问两个能可靠回答的问题：
  ① 这条命令的命令名是不是「会启动程序的类型」（启动器封闭清单，_is_launcher_cmd）；
  ② 命令里任何位置有没有出现官方写入器 / CLI 的名字（_name_hit）。
两个都是 ⇒ 每个出现处的完整路径必须精确等于官方固定位（_identity_ok），否则拒。
不分析「谁执行谁」（launcher 任意嵌套，位置分析无界），不维护解释器选项表。
"""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "skills", "gan-zhong-xue"))
try:
    from gzx_lock_append import lock_append, _normalize_dir
except ImportError:
    try:
        from gzx_lock_append import _normalize_dir
    except ImportError:
        _normalize_dir = None
    lock_append = None

GZX_DIR = os.environ.get("GZX_DIR", os.path.expanduser("~/.gan-zhong-xue"))
PROTECTED_DIR_MARK = ".gan-zhong-xue"     # 运行时数据目录标记（含前导点，不误伤仓库/技能名）
WRITER_MARK = "gzx-append"
CLI_FILENAME = "gzx_cli.py"

READONLY_PREFIX = {
    "cat", "grep", "ls", "wc", "head", "tail", "stat", "sha256sum", "md5sum", "find",
}
FIND_DENY_FLAGS = {
    "-exec", "-execdir", "-ok", "-okdir", "-delete",
    "-fprint", "-fprint0", "-fprintf", "-fls",
}

# ── 词法底座（一遍扫出「词」与「操作符」，各自带原文起止位置）──
# 守卫所需的 bash 词法子集：
#   * 未加引号的空白分隔词；词首未加引号的 # 到行尾是注释；
#   * '…' 字面；"…" 里反斜杠只转义 $ ` " \ 与换行；词外反斜杠转义下一个字符；
#   * $(…) 与反引号是词的一部分：按原字符留在词文本里（守卫不展开，只判「含扩展算子」），
#     其内部的空白与 ; | & < > ( ) 不切词；
#   * 词外的换行 ; | & < > ( ) 是操作符。
# 词文本 = 引号剥除、转义已解之后的 shell 词；quoted = 该词含引号部分。
_WS = " \t\r"
_CTRL = "\n;|&<>()"
_OPS = sorted([
    ";;&", ";;", ";&", ";", "|&", "||", "|", "&>>", "&>", "&&", "&", "\n",
    "<<-", "<<<", "<<", "<>", "<&", "<(", "<", ">>", ">&", ">|", ">(", ">",
    "(", ")",
], key=len, reverse=True)
_SEP_OPS = {"\n", ";", ";;", ";;&", ";&", "&&", "&", "|", "||", "|&",
            "(", ")", "<(", ">("}                    # 命令块边界
_STRUCT_OPS = _SEP_OPS - {"|", "|&"}                # 范围内一律拒；管道只切块
_OUT_OPS = {">", ">>", ">|", "&>", "&>>", "<>"}      # 写文件的重定向
_IN_OPS = {"<", "<<", "<<-", "<<<"}                   # 读 / heredoc / herestring（不查目标）
_EXPANSION_CHARS = "*?[]{}$`"

# ── 启动器封闭清单（board #129 重写计划 v2；清单外 = 已声明边界，见 README）──
_SHELL_FAMILY = {"bash", "sh", "zsh", "dash", "ksh", "source", "."}
_INTERP_FAMILY = {"python", "python3", "perl", "ruby", "node"}
_PY_VERSIONED_RE = re.compile(r"^python\d+(\.\d+)*$")   # python3.12 / python2 一类同名解释器
_LAUNCHER_FAMILY = {
    "env", "nice", "nohup", "setsid", "stdbuf", "timeout", "sudo", "su", "doas",
    "xargs", "command", "exec", "builtin", "time", "script", "watch", "busybox",
    "strace", "ltrace", "chroot", "pkexec",
}
# 会把「脚本文件参数」当程序执行的命令（命令名在该段的词之后，参数必须字面）
_SCRIPT_RUNNERS = _SHELL_FAMILY | _INTERP_FAMILY
_WRITER_BASENAMES = (WRITER_MARK, WRITER_MARK + ".sh")
_NAME_BASENAMES = set(_WRITER_BASENAMES) | {CLI_FILENAME}
_NAME_ORDER = (WRITER_MARK + ".sh", WRITER_MARK, CLI_FILENAME)   # 长名在前
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PATH_ASSIGN_RE = re.compile(r"^PATH=", re.IGNORECASE)
_SUBST_RE = re.compile(r"\$\(|\)|`")            # 命令替换的边界字符


class Decision:
    def __init__(self, allow, rule):
        self.allow = allow
        self.rule = rule


class _Tok(object):
    """词法项。kind='w' 词（引号已剥、转义已解）或 kind='o' 操作符；
    start/end 是原文下标；quoted=该词含引号部分；subst=含活的命令替换
    （$(…) / 反引号，且不在单引号里）。"""
    __slots__ = ("kind", "text", "start", "end", "quoted", "subst")

    def __init__(self, kind, text, start, end, quoted=False, subst=False):
        self.kind = kind
        self.text = text
        self.start = start
        self.end = end
        self.quoted = quoted
        self.subst = subst

    def __repr__(self):
        return "%s%r" % (self.kind, self.text)


def _lex(s):
    """Bash 词法（守卫所需子集）。返回 (items, None) 或 (None, "unparseable")。
    引号未闭合 / 末尾悬挂反斜杠 / $( 或反引号未闭合 ⇒ unparseable（调用方默认拒）。"""
    items = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in _WS:
            i += 1
            continue
        if c == "#":                     # 只在词首到达这里：注释到行尾
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if c in _CTRL:
            op = s[i]
            for cand in _OPS:
                if s.startswith(cand, i):
                    op = cand
                    break
            items.append(_Tok("o", op, i, i + len(op)))
            i += len(op)
            continue
        start = i
        buf = []
        quoted = False
        subst = False
        q = None            # 当前引号：None / "'" / '"'
        depth = 0           # 未闭合的 $( 与其内 ( 的层数
        ticks = 0           # 是否在反引号内
        while i < n:
            c = s[i]
            nxt = s[i + 1] if i + 1 < n else ""
            if q == "'":
                if c == "'":
                    q = None
                else:
                    buf.append(c)
                i += 1
            elif c == "\\":
                if nxt == "\n":                      # 续行
                    i += 2
                elif q == '"':
                    if nxt and nxt in '$`"\\':       # 双引号内只有这五个字符可被转义
                        buf.append(nxt)
                        i += 2
                    else:
                        buf.append("\\")
                        i += 1
                elif nxt:
                    buf.append(nxt)
                    i += 2
                else:
                    return None, "unparseable"
            elif c == "'" and q is None:
                q = "'"
                quoted = True
                i += 1
            elif c == '"':
                if q == '"':
                    q = None
                else:
                    q = '"'
                    quoted = True
                i += 1
            elif c == "`":
                buf.append(c)
                ticks ^= 1
                subst = True
                i += 1
            elif c == "$" and nxt == "(":
                subst = True
                if s.startswith("$((", i):
                    buf.append("$((")
                    depth += 2
                    i += 3
                else:
                    buf.append("$(")
                    depth += 1
                    i += 2
            elif depth > 0 or ticks:
                if not ticks:
                    if c == "(":
                        depth += 1
                    elif c == ")":
                        depth -= 1
                buf.append(c)
                i += 1
            elif q == '"':
                buf.append(c)
                i += 1
            elif c in _WS or c == "\n" or c in _CTRL:
                break
            else:
                buf.append(c)
                i += 1
        if i >= n and (q is not None or depth or ticks):
            return None, "unparseable"
        items.append(_Tok("w", "".join(buf), start, i, quoted, subst))
    return items, None


def _basename(tok):
    return tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


def _cmd_name(text):
    """命令名（比较用）：basename、小写、去 .exe（Windows 的 bash.exe / python.exe）。"""
    b = _basename(text).lower()
    if b.endswith(".exe") and len(b) > 4:
        b = b[:-4]
    return b


def _has_expansion(s):
    return any(c in _EXPANSION_CHARS for c in s)


def _has_ws(s):
    return any(c in " \t\r\n" for c in s)


def _canon_path(p):
    p = os.path.expanduser(p)
    if _normalize_dir is not None:
        p = _normalize_dir(p)
    return os.path.normcase(os.path.realpath(p))


def _posix_form(p):
    p = p.replace("\\", "/")
    if len(p) > 1 and p[1] == ":":
        p = "/" + p[0].lower() + p[2:]
    return p


def _protected_markers():
    """返回判定受保护数据目录引用的精确标记集合。"""
    marks = {PROTECTED_DIR_MARK}
    raw_dir = GZX_DIR
    if raw_dir:
        marks.add(raw_dir)
        marks.add(_posix_form(raw_dir))
        if _normalize_dir is not None:
            marks.add(_normalize_dir(raw_dir))
        canon = _canon_path(raw_dir)
        if canon:
            marks.add(canon)
            marks.add(_posix_form(canon))
    return {m for m in marks if m and m != "/"}


def _in_scope(text):
    if not text:
        return False
    low = text.lower()
    for m in _protected_markers():
        if m in text or m.lower() in low:
            return True
    return False


def _is_protected(tok):
    if not tok:
        return False
    if _in_scope(tok):
        return True
    # 检查路径规范化后是否落入 GZX_DIR 之下
    if "/" in tok or "\\" in tok or tok.startswith("~"):
        try:
            canon_tok = _canon_path(tok)
            canon_dir = _canon_path(GZX_DIR)
            if canon_tok == canon_dir or canon_tok.startswith(canon_dir + os.sep):
                return True
        except Exception:
            pass
    return False


def _plugin_skill_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "skills", "gan-zhong-xue")


def _writer_installed_path():
    return _canon_path(os.path.join(GZX_DIR, "gzx-append.sh"))


def _cli_official_path():
    return _canon_path(os.path.join(_plugin_skill_dir(), CLI_FILENAME))


def _resolve_bare(name):
    """裸**命令名**按 PATH 顺序解析（实测：bash 对命令名只搜 PATH，不看当前目录）。
    返回 (命中文件, 命中前是否遇到过「当前目录项」)；找不到返回 (None, …)。
    空项与 . 都表示「当前目录」——守卫看不到工具的当前目录，排在前面的这类项使解析不可证。
    Windows 的 shutil.which 只认 PATHEXT 后缀（.sh 不在内）而 Git Bash 按 shebang 判可执行，
    故不借用 which，自己扫：存在且（POSIX 上）可执行即命中。"""
    dot_before = False
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d.strip() in ("", ".", "./"):
            dot_before = True
            continue
        cand = os.path.join(d, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand, dot_before
    return None, dot_before


def _identity_ok(path, name, provable=True):
    """path 是否精确等于官方名 name 的固定位。
    赋值前缀（W=…）先剥掉；带分隔符 / 带空白的路径按规范化路径精确相等；
    裸写入器名只可能是**命令名**（参数位上的裸名在调用处已一律拒，见 _gates），
    按 PATH 解析到官方位才算数，provable=False（命令自带 PATH= 前缀）时不可证；
    裸 CLI 名按当前目录解析（python 打开脚本就是相对当前目录）。"""
    m = _ENV_ASSIGN_RE.match(path)
    if m:
        path = path[m.end():]
    if not path:
        return False
    official = _cli_official_path() if name == CLI_FILENAME else _writer_installed_path()
    if name != CLI_FILENAME and not any(c in path for c in "/\\ \t"):
        if not provable:
            return False
        w, dot_before = _resolve_bare(path)
        return bool(w) and not dot_before and _canon_path(w) == official
    return _canon_path(path) == official


def _name_hit(text):
    """词里的官方名出现处。返回 None / ("path", 路径文本, 名字) / ("payload", 名字)。
    整词以官方名结尾（basename 相等）⇒ 这个词是一个路径，交 _identity_ok 核固定位
    （dsh #3072 §1-4 (i)：可分词对象用 basename 相等；装在带空格目录下的官方件也走这里）。
    否则若词里带空白（必来自引号）⇒ 它是「带引号的程序串」（bash -c / su -c / watch 的
    命令串、herestring）：后半段守卫无法审计，串里出现官方名**子串**即 payload
    （dsh #3072 §1-4 (ii)：边界不可恢复的对象用子串、仅 deny——串内的引号与标点
    会让 basename 不再干净，如 bash -c "python3 '/x/gzx_cli.py' save-cell"）。"""
    base = _basename(text).lower()
    if base not in _NAME_BASENAMES:
        # 带空格的路径（装在带空格目录下的官方件）：官方名是最后一个空白段
        stripped = text.rstrip()
        base = _basename(stripped.rsplit(None, 1)[-1]).lower() if stripped else ""
        if base not in _NAME_BASENAMES:
            if not _has_ws(text):
                return None
            name = _name_substring(text)
            return ("payload", text, name) if name else None
    return ("path", text, base)


def _name_substring(text):
    low = text.lower()
    return next((n for n in _NAME_ORDER if n in low), None)


def _is_script_runner(name):
    return name in _SCRIPT_RUNNERS or _PY_VERSIONED_RE.match(name) is not None


def _is_py_name(name):
    return name in ("python", "python3") or _PY_VERSIONED_RE.match(name) is not None


def _is_launcher_cmd(name):
    """问题①：命令名是不是「会启动程序的类型」= 启动器封闭清单 ∪ 官方名字集合。"""
    return (_is_script_runner(name) or name in _LAUNCHER_FAMILY
            or name in _NAME_BASENAMES)


def _has_path_assign(words):
    """命令里是否有 PATH=… 赋值词（含 env 的 PATH=… 参数）。"""
    return any(_PATH_ASSIGN_RE.match(t.text) for t in words)


def _parse_redirects(block):
    """块内重定向：返回 ([(操作符项, 操作符, 目标词文本或 None)], 被重定向吃掉的词下标集)。
    被吃掉的词 = io 号、操作符、操作数——它们不再参与命令名 / 参数判定。"""
    redirs = []
    consumed = set()
    i, n = 0, len(block)
    while i < n:
        t = block[i]
        if t.kind == "o" and (t.text in _OUT_OPS or t.text in _IN_OPS
                              or t.text in (">&", "<&")):
            if (i > 0 and block[i - 1].kind == "w" and block[i - 1].text.isdigit()
                    and block[i - 1].end == t.start):
                consumed.add(i - 1)                  # 紧贴操作符的纯数字 = io 号
            consumed.add(i)
            tgt = None
            if i + 1 < n and block[i + 1].kind == "w":
                tgt = block[i + 1].text
                consumed.add(i + 1)
            redirs.append((t, t.text, tgt))
            i += 2 if tgt is not None else 1
            continue
        i += 1
    return redirs, consumed


def _is_file_output(op, target):
    """写文件的重定向？fd 复制（2>&1、2>&-）与输入重定向不算。"""
    if op in (">&", "<&"):
        return op == ">&" and not (target is None or target.isdigit() or target == "-")
    return op in _OUT_OPS


def _output_targets(redirs):
    return [(op, tgt) for _t, op, tgt in redirs if _is_file_output(op, tgt)]


def _parse_chunks(toks):
    """按命令块边界（; & | 换行 括号 进程替换）切块，并逐块解析。"""
    blocks = [[]]
    for t in toks:
        if t.kind == "o" and t.text in _SEP_OPS:
            blocks.append([])
        else:
            blocks[-1].append(t)
    cmds = []
    for block in blocks:
        redirs, consumed = _parse_redirects(block)
        words = [t for k, t in enumerate(block) if t.kind == "w" and k not in consumed]
        assign_only = bool(words) and all(_ENV_ASSIGN_RE.match(t.text) for t in words)
        head = None
        for t in words:
            if not _ENV_ASSIGN_RE.match(t.text):
                head = t
                break
        if head is None and words:
            head = words[0]       # 纯赋值块取首词（基线行为保持：赋值行里的官方名/数据目录仍进范围）
        cmds.append({"toks": block, "words": words, "redirs": redirs,
                     "assign_only": assign_only, "head": head, "official_at": []})
    return cmds


def _structure_rule(op):
    return "structure:" + ("\\n" if op == "\n" else op)


def _official_chunk_clean(c):
    """靠官方名放行的启动器命令块是否干净。命令名位置起到**第一个在命令词里的官方名**之间，
    只允许启动器名、选项、io 号、字面赋值这类不可能是「另一个程序」的词；这段里若还有别的
    程序（带空白的程序串、含扩展算子或命令替换的词）或对数据目录的引用 ⇒ 不放行。
    Bugbot 4728123561：官方路径当陪衬参数，会让 env / sudo / timeout / bash -c 借官方名
    过前缀检查直写数据目录（`env tee <数据目录>/map.md <官方写入器>`）。官方名之后的词是
    官方件自己的参数，不受此限。"""
    words = c["words"]
    head_i = next((i for i, w in enumerate(words) if w is c["head"]), 0)
    first = next((k for k in c["official_at"] if k >= head_i), None)
    if first is None:
        return False        # 官方名只在赋值前缀 / 重定向操作数里：被执行的不是官方件
    for t in words[:first]:
        if t.subst or _has_ws(t.text) or _has_expansion(t.text) or _in_scope(t.text):
            return False
    return True


def _gates(cmds):
    """与范围无关的三道闸（进出范围两向都拦）。返回 (deny Decision 或 None, 核实过官方名的块下标集)。
    闸一：写文件的重定向目标必须字面（落盘路径由 shell 产生即不可静态证明）。
    闸二：命令名必须完全字面；命令名带空白（引号粘合，词边界不可恢复）且含官方名 ⇒ 拒。
    闸三：命令名是启动器类型 ⇒ 块内**任何位置**的官方名都必须在固定位；
          名字出现在带引号的程序串里（-c 载荷等）⇒ 拒；脚本执行型命令（shell / 解释器）
          的其余参数必须字面（带空白的引号串除外——那是程序文本 / 自由文本，不是脚本路径）。"""
    official = set()
    for ci, c in enumerate(cmds):
        for _op, tgt in _output_targets(c["redirs"]):
            if tgt is not None and _has_expansion(tgt):
                return Decision(False, "expansion:unprovable"), official
        head = c["head"]
        if head is None:
            continue
        if not c["assign_only"]:
            # [ 与 { 是 test 内建 / 花括号组保留字：自身含的括号字符不是扩展算子
            if head.text not in ("[", "[[", "{") and _has_expansion(head.text):
                return Decision(False, "expansion:unprovable"), official
            if _has_ws(head.text):
                hit = _name_hit(head.text)
                if hit is not None and hit[0] == "payload":
                    return Decision(False, "expansion:unprovable"), official
                if hit is not None and not _identity_ok(hit[1], hit[2]):
                    return Decision(False, "identity:" + hit[2]), official
        if c["assign_only"]:
            if _basename(head.text).lower() not in _NAME_BASENAMES:
                continue
        elif not _is_launcher_cmd(_cmd_name(head.text)):
            continue
        for t in c["toks"]:
            if t.kind != "w":
                continue
            if t.subst:
                # 命令替换里的名字（bash $(echo /x/gzx-append.sh)）：整词不可能等于固定位
                name = _name_substring(_SUBST_RE.sub(" ", t.text))
                if name:
                    return Decision(False, "identity:" + name), official
                continue
            hit = _name_hit(t.text)
            if hit is None:
                continue
            if hit[0] == "payload":
                return Decision(False, "payload:" + hit[2]), official
            bare_writer = (hit[2] != CLI_FILENAME
                           and not any(c in hit[1] for c in "/\\ \t"))
            if bare_writer and (t is not head or _has_path_assign(c["words"])):
                # 裸写入器名只在命令名位可信。参数位上一律拒——实测 bash/sh 找脚本参数
                # 时**先看当前目录再看 PATH**（python 则只看当前目录），当前目录里的同名
                # 假件会遮住 PATH 上的官方件；命令自带 PATH= 前缀时守卫也看不到那个 PATH。
                return Decision(False, "identity:" + hit[2]), official
            if not _identity_ok(hit[1], hit[2]):
                return Decision(False, "identity:" + hit[2]), official
            official.add(ci)
            for k, w in enumerate(c["words"]):
                if w is t:
                    c["official_at"].append(k)
                    break
        if c["assign_only"] or not _is_script_runner(_cmd_name(head.text)):
            continue
        seen = False
        for t in c["words"]:
            if t is head:
                seen = True
                continue
            if not seen:
                continue
            # 活的命令替换一律不可证；其余带空白的引号串是程序文本 / 自由文本，不是脚本路径
            if t.subst or (_has_expansion(t.text) and not (t.quoted and _has_ws(t.text))):
                return Decision(False, "expansion:unprovable"), official
    return None, official


# ── heredoc ──
# 官方 writer / CLI heredoc 例外（支持无引号与引号包裹的路径）：命中即进身份层。
_HEREDOC_WRITER_RE = re.compile(
    r"^[ \t]*(?:'[^'\n]*gzx-append\.sh'|\"[^\"\n]*gzx-append\.sh\"|\S*gzx-append\.sh)"
    r"[ \t]+(map|jsonl|pending|candidates|audit)"
    r"[ \t]+<<[ \t]*(['\"])([A-Za-z_][A-Za-z0-9_]*)\2\n((?:.*\n)?)\3$",
    re.DOTALL,
)
_HEREDOC_CLI_RE = re.compile(
    r"^[ \t]*(?:'[^'\n]*python3?(?:\.exe)?'|\"[^\"\n]*python3?(?:\.exe)?\"|\S*python3?(?:\.exe)?)[ \t]+"
    r"(?:'[^'\n]*gzx_cli\.py'|\"[^\"\n]*gzx_cli\.py\"|\S*gzx_cli\.py)"
    r"[ \t]+(save-cell|pending-add)"
    r"[ \t]+<<[ \t]*(['\"])([A-Za-z_][A-Za-z0-9_]*)\2\n((?:.*\n)?)\3$",
    re.DOTALL,
)


def _find_heredoc_delims(cmdline):
    """引号感知地找出命令行里全部 heredoc 起始：[(chomp_tabs, 定界符原文, 引号形态), ...]。
    <<< 与算术区（定界符非标识符/引号形状）不算 heredoc。"""
    out = []
    quote = None
    i = 0
    n = len(cmdline)
    while i < n:
        c = cmdline[i]
        if quote:
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "\"'":
            quote = c
            i += 1
            continue
        if c == "<" and cmdline[i:i + 3] == "<<<":
            i += 3
            continue
        if c == "<" and cmdline[i:i + 2] == "<<":
            j = i + 2
            chomp = False
            if cmdline[j:j + 1] == "-":
                chomp = True
                j += 1
            # 跳过定界符前空白
            while j < n and cmdline[j] in " \t":
                j += 1
            m = re.match(r"('([^']*)'|\"([^\"]*)\"|[A-Za-z_][A-Za-z0-9_]*)",
                         cmdline[j:])
            if not m:
                i += 2
                continue        # 非字面定界符（变量/算术）——非 heredoc，跳过
            raw = m.group(1)
            delim = m.group(2) if raw.startswith("'") else (
                m.group(3) if raw.startswith("\"") else raw)
            out.append((chomp, delim, raw))
            i = j + len(raw)
            continue
        i += 1
    return out


def _strip_heredocs(cmd):
    """剥离 heredoc 正文。返回 (命令行, 正文行列表, 状态)；状态 ok / none / bad。
    bad ⇒ 调用方按 structure:unparseable 拒（无法可靠剥离 ⇒ 判据不可信）。"""
    if "<<" not in cmd or "\n" not in cmd:
        return cmd, [], "none"
    starts = _find_heredoc_delims(cmd.split("\n", 1)[0])
    if not starts:
        return cmd, [], "none"
    lines = cmd.split("\n")
    cmdline = lines[0]
    bodies = []
    consumed = 1                     # 正文起始行
    for (chomp, delim, _raw) in starts:
        if consumed >= len(lines):
            return cmd, bodies, "bad"
        j = consumed
        while j < len(lines):
            cand = lines[j].rstrip("\r")
            if chomp:
                cand = cand.lstrip("\t")
            if cand == delim:
                break
            j += 1
        if j >= len(lines):
            return cmd, bodies, "bad"        # 终结行缺失
        bodies.extend(lines[consumed:j])
        trailing = lines[j + 1:]
        # 终结后：非空行是后续 shell（重新进命令）；空行只许 1 个（G13/G14 口径）
        nonempty = [k for k, t in enumerate(trailing) if t.strip()]
        if len(trailing) - len(nonempty) > 1:
            return cmd, bodies, "bad"
        if nonempty:
            rest = "\n".join(trailing[nonempty[0]:])
            return cmdline + "\n" + rest, bodies, "ok"
        consumed = j + 1
    # 全部 heredoc 已剥离；命令行还留着 << 起始（供主循环识别）
    return cmdline, bodies, "ok"


def _check_heredoc_first_line(cmd, delim, body, is_cli=False):
    """官方 writer / CLI heredoc 例外：定界符唯一且末行、首行恰一个 <<、无结构符、
    重定向不落数据目录，且命令名确是官方件（解析路径精确相等）。"""
    lines = cmd.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    lines = [l.rstrip("\r") for l in lines]
    if [i for i, l in enumerate(lines) if l == delim] != [len(lines) - 1]:
        return Decision(False, "heredoc:delim_repeats")
    toks, err = _lex(lines[0])
    hd = [t for t in toks if t.kind == "o" and t.text in ("<<", "<<-", "<<<")]
    if err or len(hd) != 1 or hd[0].text != "<<":
        return Decision(False, "heredoc:operator")
    for t in toks:
        if t.kind == "o":
            if t.text in _STRUCT_OPS:
                return Decision(False, _structure_rule(t.text))
        elif t.subst:
            return Decision(False, "structure:" + ("$(" if "$(" in t.text else "`"))
    cmds = _parse_chunks(toks)
    if len(cmds) != 1:
        return Decision(False, "heredoc:operator")
    c = cmds[0]
    for op, tgt in _output_targets(c["redirs"]):
        if tgt is None:
            return Decision(False, "redirect:missing-target")
        if _is_protected(tgt):
            return Decision(False, "redirect-to-protected:" + op)
    head = c["head"]
    if is_cli:
        ok = (head is not None and _is_py_name(_cmd_name(head.text))
              and any(_name_hit(t.text) == ("path", t.text, CLI_FILENAME)
                      and _identity_ok(t.text, CLI_FILENAME) for t in c["words"]))
        if not ok:
            return Decision(False, "heredoc:cli_identity")
        return Decision(True, "cli-heredoc:%dB" % len(body.encode("utf-8")))
    hit = _name_hit(head.text) if head is not None else None
    if not (hit and hit[0] == "path" and hit[2] in _WRITER_BASENAMES
            and _identity_ok(hit[1], hit[2])):
        return Decision(False, "heredoc:writer_identity")
    return Decision(True, "writer-heredoc:%dB" % len(body.encode("utf-8")))


_BOOTSTRAP_SRC_NAMES = {"gzx-append.sh", "gzx_lock_append.py"}
_BOOTSTRAP_SETUP = {"mkdir", "chmod"}


def _seg_is_bootstrap(head, args):
    """引导安装豁免：cp 官方件到数据目录 / mkdir 数据目录 / chmod 仅两种形状。
    head=命令名（_cmd_name），args=命令名之后的参数文本。"""
    cur_dir = GZX_DIR
    if head in ("cp", "install"):
        if len(args) != 2:
            return False
        src_path = os.path.expanduser(args[0])
        src = _basename(src_path)
        dst_path = os.path.expanduser(args[1])
        dst = _basename(dst_path)
        dst_dir = os.path.dirname(dst_path)
        if (src not in _BOOTSTRAP_SRC_NAMES
                or dst not in _BOOTSTRAP_SRC_NAMES
                or _canon_path(dst_dir) != _canon_path(cur_dir)):
            return False
        return _canon_path(src_path) == _canon_path(
            os.path.join(_plugin_skill_dir(), src))
    if head in _BOOTSTRAP_SETUP:
        aa = [t for t in args if not (t.isdigit() and len(t) == 1)]
        pathish = [t for t in aa if ("/" in t or "\\" in t or t.startswith("~"))]
        if head == "mkdir":
            targets = [t for t in aa if not t.startswith("-")]
            return (bool(targets)
                    and all(_canon_path(t) == _canon_path(cur_dir) for t in targets))
        modeparts = [t for t in aa if t not in pathish]
        if len(pathish) != 1 or not modeparts:
            return False
        mode = "".join(modeparts)
        if mode.startswith("-"):
            return False
        target = pathish[0]
        if _canon_path(target) == _writer_installed_path():
            return "+" in mode and "x" in mode
        if _canon_path(target) == _canon_path(cur_dir):
            return mode == "700"
        return False
    return False


def classify(cmd):
    """对一条 Bash 命令做守卫判定。返回 Decision。纯函数，便于测试。

    流序（board #129 收敛共识）：
    0. 官方 writer / CLI heredoc 例外 → 身份层（与基线一致，先于一切）；
    1. heredoc 正文剥离（正文是数据不是 shell，判定只看命令行）；剥不动 ⇒ 拒；
    2. 分词（一遍扫出词与操作符）；失败 ⇒ structure:unparseable（默认拒）；
    3. 与范围无关的三道闸（_gates）：重定向目标字面 / 命令名字面 /
       启动器命令里的官方名必须在固定位；
    4. 范围门：词里引用数据目录 ∨ heredoc 正文含标记 ∨ 启动器命令里出现已核实的官方名；
    5. 范围外 ⇒ 放行 out-of-scope；
    6. 范围内：结构符 / 命令替换 / 重定向落数据目录 / 逐命令块前缀检查，与基线同序。"""
    # 0. 官方 writer / CLI heredoc 语法必须进入身份层检验（即使正文不含保护目录字样，假写入器也必拒）
    m = _HEREDOC_WRITER_RE.match(cmd)
    if m:
        return _check_heredoc_first_line(cmd, m.group(3), m.group(4), is_cli=False)

    mc = _HEREDOC_CLI_RE.match(cmd)
    if mc:
        return _check_heredoc_first_line(cmd, mc.group(3), mc.group(4), is_cli=True)

    # 1. heredoc 正文剥离
    bodies = []
    work = cmd
    has_heredoc = False
    if "\n" in cmd and "<<" in cmd:
        work, bodies, status = _strip_heredocs(cmd)
        if status == "bad":
            return Decision(False, "structure:unparseable")
        if status == "ok":
            has_heredoc = True

    # 2. 分词
    toks, err = _lex(work)
    if err:
        return Decision(False, "structure:unparseable")
    cmds = _parse_chunks(toks)

    # 3. 与范围无关的三道闸
    d_gate, official = _gates(cmds)
    if d_gate:
        return d_gate

    # 4./5. 范围门
    in_scope = bool(official) or any(_in_scope(b) for b in bodies)
    if not in_scope:
        for t in toks:
            if t.kind == "w" and _in_scope(t.text):
                in_scope = True
                break
    if not in_scope:
        return Decision(True, "out-of-scope")

    # 6. 范围内（基线同序：结构符拒 → 重定向判定 → 逐命令块检查）
    redir_at = {}
    for c in cmds:
        for t, op, tgt in c["redirs"]:
            redir_at[t] = (op, tgt)
    for t in toks:
        if t.kind == "o":
            if t.text in _STRUCT_OPS:
                return Decision(False, _structure_rule(t.text))
            r = redir_at.get(t)
            if r is not None and _is_file_output(r[0], r[1]):
                if r[1] is None:
                    return Decision(False, "redirect:missing-target")
                if _is_protected(r[1]):
                    return Decision(False, "redirect-to-protected:" + r[0])
        elif t.subst:
            # 命令替换会执行嵌套命令，只读白名单管不到它（含藏在双引号里的形态）
            return Decision(False, "structure:" + ("$(" if "$(" in t.text else "`"))
    for ci, c in enumerate(cmds):
        words = c["words"]
        if not words:
            continue
        head = c["head"]
        if c["assign_only"]:
            # 纯赋值行（基线行为保持：提到数据目录 / 官方名的赋值行仍拒）
            return Decision(False, "prefix:" + _basename(words[0].text))
        name = _cmd_name(head.text)
        args = [t.text for t in words[words.index(head) + 1:]]
        if name == "find":
            for a in args:
                if a in FIND_DENY_FLAGS:
                    return Decision(False, "find:" + a)
        if name in ("sed", "perl"):
            for a in args:
                if a == "-i" or (a.startswith("-i") and len(a) > 2 and a[2] != "n"):
                    return Decision(False, name + ":-i")
        if _seg_is_bootstrap(name, args):
            continue
        if name in READONLY_PREFIX:
            continue
        if ci in official and _official_chunk_clean(c):
            continue
        return Decision(False, "prefix:" + _basename(head.text))

    # 7. 非官方 heredoc（范围内）一律拒：正文是数据不是 shell，不可静态审计；
    #    官方 writer / CLI 形态已在第 0 步进身份层。
    if has_heredoc:
        return Decision(False, "structure:heredoc")

    return Decision(True, "readonly-ok")


def _core_path():
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.join(here, "..", "skills", "gan-zhong-xue", "gzx_lock_append.py")
    if os.path.exists(cand):
        return cand
    return os.path.join(GZX_DIR, "gzx_lock_append.py")


def _audit(tool, allowed, rule):
    cur_dir = GZX_DIR
    try:
        audit_file = os.path.join(cur_dir, "audit.jsonl")
        alive = None
        if not os.path.exists(audit_file):
            alive = json.dumps({
                "ts": _now(), "event": "hook_alive", "hook": "gzx-guard",
            }, ensure_ascii=False) + "\n"
        line = json.dumps({
            "ts": _now(),
            "tool": tool,
            "decision": "allow" if allowed else "deny",
            "rule": rule,
        }, ensure_ascii=False) + "\n"
        data = line.encode()
        if lock_append is not None:
            lock_append(cur_dir, "audit", data, prepend_if_missing=alive)
        else:
            subprocess.run([sys.executable, _core_path(), "audit"], input=data,
                           capture_output=True, timeout=20)
    except Exception:
        pass


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _handle(event):
    tool = event.get("tool_name", "")
    ti = event.get("tool_input", {}) or {}
    if tool == "Bash":
        d = classify(ti.get("command", ""))
    elif tool in ("Write", "Edit", "MultiEdit"):
        # 只判定目标文件路径（Write/Edit 文档中提到 ~/.gan-zhong-xue 不误杀）
        fp = ti.get("file_path") or ti.get("path") or ""
        if not _is_protected(fp):
            return 0
        d = Decision(False, "write-tool:protected-path")
    elif tool == "Agent":
        prompt = ti.get("prompt", "")
        if not _in_scope(prompt):
            return 0
        d = Decision(False, "agent:protected-ref")
    else:
        return 0

    if d.allow and d.rule == "out-of-scope":
        return 0
    _audit(tool, d.allow, d.rule)
    if not d.allow:
        sys.stderr.write(
            "gzx-guard deny (%s)：受保护目录 ~/.gan-zhong-xue 的写入只能经写入器 "
            "gzx-append.sh / gzx_cli.py（append-only 不变量所在）；只读命令请确认前缀在白名单。\n"
            % d.rule)
        return 2
    return 0


def _head_is_official(text):
    hit = _name_hit(text)
    return bool(hit) and hit[0] == "path" and _identity_ok(hit[1], hit[2])


def selftest():
    # 自检是测试入口：Windows CI 运行器控制台编码非 UTF-8（cp1252 实测），本函数
    # 打印中文 SKIP/FAIL 行即 UnicodeEncodeError。输出强制 UTF-8，与控制台无关。
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    _pkg_skill = _posix_form(_plugin_skill_dir())
    _cli_native = os.path.join(_plugin_skill_dir(), CLI_FILENAME)
    _E = "/tmp/evil/gzx-append.sh"
    allows = [
        # ── 只读 ──
        "sha256sum ~/.gan-zhong-xue/map.md",
        "grep -i 先猜 ~/.gan-zhong-xue/map.md",
        "grep 先猜 ~/.gan-zhong-xue/map.md | head",
        "grep 先猜 ~/.gan-zhong-xue/map.md|head",
        "cat ~/.gan-zhong-xue/map.md > /dev/null",
        "cat ~/.gan-zhong-xue/map.md 2>/dev/null",
        "cat 2>&1 ~/.gan-zhong-xue/map.md",
        "cat > /tmp/out < ~/.gan-zhong-xue/map.md",
        "cat < ~/.gan-zhong-xue/map.jsonl",
        "ls ~/.gan-zhong-xue/gzx-append.sh 2>/dev/null",
        "cat ~/.gan-zhong-xue/pending.md 2>/dev/null",
        "cat ~/.gan-zhong-xue/candidates.jsonl 2>/dev/null",
        "grep $PAT ~/.gan-zhong-xue/map.md",
        "FOO=bar cat ~/.gan-zhong-xue/map.md",
        # ── 官方写入器 / 官方 CLI（命令名是官方名，或启动器链 + 已核实的官方名）──
        "~/.gan-zhong-xue/gzx-append.sh map < payload",
        "~/.gan-zhong-xue/gzx-append.sh pending-clear",
        "~/.gan-zhong-xue/gzx-append.sh badtarget </dev/null",
        "~/.gan-zhong-xue/gzx-append.sh jsonl <<< '{\"id\":2}'",
        "cat payload | ~/.gan-zhong-xue/gzx-append.sh candidates",
        "bash ~/.gan-zhong-xue/gzx-append.sh map",
        "'~/.gan-zhong-xue/gzx-append.sh' map",
        "bash \"~/.gan-zhong-xue/gzx-append.sh\" map",
        "bash.exe ~/.gan-zhong-xue/gzx-append.sh map",
        "env FOO=bar ~/.gan-zhong-xue/gzx-append.sh map",
        "FOO=bar bash ~/.gan-zhong-xue/gzx-append.sh map",
        "python3 '%s/gzx_cli.py' preflight" % _pkg_skill,
        "python3 %s/gzx_cli.py preflight" % _pkg_skill,
        "python3 '%s/gzx_cli.py' cell '<概念>'" % _pkg_skill,
        # 解释器选项不再影响判定（旧选项表写差即漏；现只问名字在不在固定位）
        "python3 -W ignore '%s/gzx_cli.py' preflight" % _pkg_skill,
        "python3 -X dev '%s/gzx_cli.py' preflight" % _pkg_skill,
        "python3 --check-hash-based-pycs default '%s/gzx_cli.py' preflight" % _pkg_skill,
        "python3 -- '%s/gzx_cli.py' preflight" % _pkg_skill,
        "python.exe '%s' preflight" % _cli_native,
        ("python3 '%s/gzx_cli.py' save-cell <<'GZX_JSON_EOF'\n"
         '{"概念":"~/.gan-zhong-xue 范围测试","分层":"🟡","来源":"🟢自","先猜":"猜","验证题":"题","状态":"待复验"}\n'
         "GZX_JSON_EOF") % _pkg_skill,
        # 裸名直调（命令名位）依赖 PATH 解析到官方安装位：装了才测，没装显式跳过
        # （dsh #3055 §3：不许用 fail-open 换绿灯；用例按安装态条件生效，两种环境都诚实）。
        # 裸名出现在**参数位**则一律拒、不设安装态条件——见下方 identity 钉的注释。
        ("gzx-append map", "writer-on-path"),
        ("gzx-append pending-clear", "writer-on-path"),
        # 启动器链 + 官方写入器的**路径**形态
        "env ~/.gan-zhong-xue/gzx-append.sh map",
        "sudo -n ~/.gan-zhong-xue/gzx-append.sh map",
        "timeout 5 ~/.gan-zhong-xue/gzx-append.sh map",
        "command ~/.gan-zhong-xue/gzx-append.sh map",
        "exec ~/.gan-zhong-xue/gzx-append.sh map",
        "source ~/.gan-zhong-xue/gzx-append.sh map",
        # ── 引导安装 ──
        "cp '%s/gzx-append.sh' ~/.gan-zhong-xue/gzx-append.sh" % _pkg_skill,
        "cp '%s/gzx_lock_append.py' ~/.gan-zhong-xue/gzx_lock_append.py" % _pkg_skill,
        "mkdir -p ~/.gan-zhong-xue",
        "chmod 700 ~/.gan-zhong-xue",
        "chmod +x ~/.gan-zhong-xue/gzx-append.sh",
        # ── 官方 writer heredoc（引号 / 无引号路径）──
        ("'~/.gan-zhong-xue/gzx-append.sh' map <<'GZX_MD_EOF'\n"
         "## 概念示例\n"
         "GZX_MD_EOF"),
        ("~/.gan-zhong-xue/gzx-append.sh map <<'GZX_MD_EOF'\n"
         "## 概念示例\n"
         "- 先猜: 保守假说\n"
         "- 揭示: 实测修正\n"
         "GZX_MD_EOF"),
        ('~/.gan-zhong-xue/gzx-append.sh jsonl <<\'GZX_JSONL_EOF\'\n'
         '{"id":1,"概念":"示例"}\n'
         'GZX_JSONL_EOF'),
        ("~/.gan-zhong-xue/gzx-append.sh pending <<'PENDING_EOF'\n"
         "- 未挖点: 示例\n"
         "PENDING_EOF"),
        "~/.gan-zhong-xue/gzx-append.sh map <<'E'\n## 概念\nE\n",
        # ── 同仓开发：命令名不是启动器类 = 只是在处理一个叫这名字的文件 ──
        "cd /tmp/gan-zhong-xue && git status",
        "git commit -m \"fix gan-zhong-xue bug\"",
        "python3 hooks/test-gzx-invariants.py",
        "git add skills/gan-zhong-xue/gzx_cli.py",
        "git commit -m 'update gzx_cli.py'",
        "git commit -m \"touch gzx_cli.py\"",
        "echo see gzx-append",
        "grep -n gzx-append SKILL.md",
        "cp skills/gan-zhong-xue/gzx_cli.py /tmp/x.py",
        "ls -l gzx-append.sh",
        "vim gzx_cli.py",
        "find . -name gzx_cli.py",
        "cat gzx_cli.py",
        "echo $HOME",
        "bash \"my notes.txt\"",
        "bash\"my notes.txt\"",
        # ── 范围外日常命令（词法底座对照：冒号 / 算术 / heredoc / 注释不误伤）──
        "echo \"a > b\"",
        "ls *.py",
        "find . -name '*.py'",
        "echo $(( 1 << 2 ))",
        "[ -s /tmp/x ]",
        "git log --oneline -10 2>/dev/null",
        "git diff HEAD~1 --stat 2>/dev/null",
        "FOO=bar cmd",
        "PATH=/usr/bin:/bin cmd",
        "cd $HOME && make",
        "cat <<EOF > /tmp/y\nbody $X\nEOF",
        "cat <<-EOF\n\tindented\n\tEOF",
        "python3 - <<'PY'\nprint('$x')\nPY",
        "grep '<<' notes.txt",
        "find . -name '*.py' -exec python3 -m py_compile {} \\;",
        "xargs -I{} bash -c 'echo $0' {}",
        "nohup python3 server.py --port $PORT",
        "cmd > out $ARGS",
        "cmd 2>/dev/null $ARG",
        "echo '> $HOME'",
        # ── 已申报边界（每条在这里有一条 allow 钉「当前不审计」，并与一条 deny 钉配对；
        #    README「已知边界」逐条对应，dsh #3072 §1-1 对价 (ii)）──
        "bash -c 'ls'",
        "bash -c 'cat x > $HOME/y'",                  # 载荷不含官方名 = 不审计
        "sudo bash $SCRIPT map",                      # 泛启动器后的非字面参数不查
        "rm map.md",                                  # cd 进数据目录后不提名字
        "bash /tmp/evil.sh",                          # 完全不出现官方名与数据目录的脚本
        # 被执行的是不带官方名的脚本、官方路径只当参数（等价于上一条，已申报边界）
        "env bash /tmp/evil.sh ~/.gan-zhong-xue/gzx-append.sh",
        "python3 '%s/gzx_cli.py' cell 'Foo Bar'" % _pkg_skill,     # 官方件自己的参数可带空格
    ]
    denies = [
        # ── 假写入器 / 假 CLI：官方名不在固定位（identity:<名字>）──
        ("bash ./gzx-append.sh map < ~/.gan-zhong-xue/pending.md", "identity:gzx-append.sh"),
        ("bash gzx-append.sh map < ~/.gan-zhong-xue/pending.md",
         "identity:gzx-append.sh"),
        ("python3 /tmp/evil/gzx_cli.py save-cell", "identity:gzx_cli.py"),
        ("python3 -u /tmp/evil/gzx_cli.py save-cell", "identity:gzx_cli.py"),
        ("/tmp/evil/gzx-append.sh map", "identity:gzx-append.sh"),
        ("sh /tmp/evil/gzx-append.sh map", "identity:gzx-append.sh"),
        ("bash -x /tmp/evil/gzx-append.sh map", "identity:gzx-append.sh"),
        ("gzx-append.sh map", "identity:gzx-append.sh", "writer-not-on-path"),
        ("bash gzx-append.sh map", "identity:gzx-append.sh"),
        ("bash gzx-append.sh map <<E\nhello\nE", "identity:gzx-append.sh"),
        ("bash /tmp/evil/gzx-append.sh map <<'E'\nhello\nE", "identity:gzx-append.sh"),
        ("bash --rcfile \"a b\" gzx-append.sh", "identity:gzx-append.sh"),
        ("python3 -X \"pycache_prefix=/tmp/a b\" /tmp/evil/gzx_cli.py save-cell",
         "identity:gzx_cli.py"),
        ("bash --rcfile /dev/null gzx-append.sh", "identity:gzx-append.sh"),
        ("python3 -X pycache_prefix=/tmp/ab /tmp/evil/gzx_cli.py save-cell", "identity:gzx_cli.py"),
        # Bugbot 4727029062（High）：Python 选项表写差的三形态——名字出现即查路径，与选项无关
        ("python3 -x /tmp/evil/gzx_cli.py save-cell", "identity:gzx_cli.py"),
        ("python3 --check-hash-based-pycs default /tmp/evil/gzx_cli.py save-cell",
         "identity:gzx_cli.py"),
        ("python3 -- /tmp/evil/gzx_cli.py save-cell", "identity:gzx_cli.py"),
        # dsh #3072 §1-2 判别用例：与上一组同一个 rule 名（判「全部位置」而非「脚本位」）
        ("python3 -W ignore /tmp/evil/gzx_cli.py save-cell", "identity:gzx_cli.py"),
        ("python3 -X dev /tmp/evil/gzx_cli.py save-cell", "identity:gzx_cli.py"),
        # Bugbot 4727166297（High）：冒号切分——PATH 前缀 / 选项值里的冒号不再把命令名认错
        ("PATH=/usr/bin:/bin gzx-append.sh map", "identity:gzx-append.sh"),
        ("PATH=/usr/bin:/bin bash /tmp/evil/gzx-append.sh map", "identity:gzx-append.sh"),
        # 命令自带 PATH= 前缀时裸名解析不可证（守卫按自己的 PATH 找，bash 按命令里的 PATH 找）
        ("PATH=/tmp/evil bash gzx-append.sh map", "identity:gzx-append.sh"),
        ("env PATH=/tmp/evil bash gzx-append.sh map", "identity:gzx-append.sh"),
        ("A=1 PATH=/usr/bin:/bin gzx-append.sh map", "identity:gzx-append.sh"),
        ("python3 -X pycache_prefix=/tmp/a:b /tmp/evil/gzx_cli.py save-cell",
         "identity:gzx_cli.py"),
        ("python3 -W ignore:x /tmp/evil/gzx_cli.py save-cell", "identity:gzx_cli.py"),
        # Bugbot 4727029063（Medium）：.exe 包装器
        ("bash.exe /tmp/evil/gzx-append.sh map", "identity:gzx-append.sh"),
        ("sh.exe /tmp/evil/gzx-append.sh map", "identity:gzx-append.sh"),
        ("source.exe /tmp/evil/gzx-append.sh map", "identity:gzx-append.sh"),
        # dsh #3066 §1-2 启动器探针 17 条：路径形态（无条件）
        ("env bash %s map" % _E, "identity:gzx-append.sh"),
        ("nice bash %s map" % _E, "identity:gzx-append.sh"),
        ("timeout 5 bash %s map" % _E, "identity:gzx-append.sh"),
        ("nohup bash %s map" % _E, "identity:gzx-append.sh"),
        ("setsid bash %s map" % _E, "identity:gzx-append.sh"),
        ("stdbuf -o0 bash %s map" % _E, "identity:gzx-append.sh"),
        ("xargs -n1 bash %s map" % _E, "identity:gzx-append.sh"),
        ("sudo bash %s map" % _E, "identity:gzx-append.sh"),
        ("zsh %s map" % _E, "identity:gzx-append.sh"),
        ("dash %s map" % _E, "identity:gzx-append.sh"),
        ("busybox sh %s map" % _E, "identity:gzx-append.sh"),
        ("su -c 'bash %s map'" % _E, "payload:gzx-append.sh"),
        ("script -qc 'bash %s map'" % _E, "payload:gzx-append.sh"),
        ("watch -n1 'bash %s map'" % _E, "payload:gzx-append.sh"),
        ("bash -c 'sh %s map'" % _E, "payload:gzx-append.sh"),
        ("bash -lc 'sh %s map'" % _E, "payload:gzx-append.sh"),
        ("bash <<< '%s map'" % _E, "payload:gzx-append.sh"),
        # dsh 17 条原文（裸名）。实测（与 test-gzx-behavior.py 同口径）：bash/sh 找脚本
        # 参数时**先看当前目录再看 PATH**——当前目录里的同名假件会遮住 PATH 上的官方件，所以
        # 参数位上的裸名与安装态无关、一律拒；合法直调请写路径或用命令名位（上方 writer-on-path 钉）。
        ("env bash gzx-append.sh map", "identity:gzx-append.sh"),
        ("nice bash gzx-append.sh map", "identity:gzx-append.sh"),
        ("timeout 5 bash gzx-append.sh map", "identity:gzx-append.sh"),
        ("nohup bash gzx-append.sh map", "identity:gzx-append.sh"),
        ("setsid bash gzx-append.sh map", "identity:gzx-append.sh"),
        ("stdbuf -o0 bash gzx-append.sh map", "identity:gzx-append.sh"),
        ("xargs -n1 bash gzx-append.sh map", "identity:gzx-append.sh"),
        ("sudo bash gzx-append.sh map", "identity:gzx-append.sh"),
        ("zsh gzx-append.sh map", "identity:gzx-append.sh"),
        ("dash gzx-append.sh map", "identity:gzx-append.sh"),
        ("busybox sh gzx-append.sh map", "identity:gzx-append.sh"),
        ("su -c 'bash gzx-append.sh map'", "payload:gzx-append.sh"),
        ("script -qc 'bash gzx-append.sh map'", "payload:gzx-append.sh"),
        ("watch -n1 'bash gzx-append.sh map'", "payload:gzx-append.sh"),
        ("bash -c 'sh gzx-append.sh map'", "payload:gzx-append.sh"),
        ("bash -lc 'gzx-append.sh map'", "payload:gzx-append.sh"),
        ("bash <<< 'gzx-append.sh map'", "payload:gzx-append.sh"),
        # 启动器族泛化形态（.exe / 版本号解释器 / 嵌套 / 命令替换里）
        ("sudo -u root python3 -x /tmp/evil/gzx_cli.py save-cell", "identity:gzx_cli.py"),
        ("env -i python3.12 /tmp/evil/gzx_cli.py save-cell", "identity:gzx_cli.py"),
        ("perl /tmp/evil/gzx_cli.py", "identity:gzx_cli.py"),
        ("node /tmp/evil/gzx_cli.py", "identity:gzx_cli.py"),
        ("timeout --kill-after=5s env nice -n 5 bash /tmp/evil/gzx-append.sh map",
         "identity:gzx-append.sh"),
        ("exec bash /tmp/evil/gzx-append.sh map", "identity:gzx-append.sh"),
        ("command bash /tmp/evil/gzx-append.sh map", "identity:gzx-append.sh"),
        ("time bash /tmp/evil/gzx-append.sh map", "identity:gzx-append.sh"),
        ("watch -n1 'gzx-append.sh'", "identity:gzx-append.sh"),
        ("bash $(echo /tmp/evil/gzx-append.sh) map", "identity:gzx-append.sh"),
        ("W=/tmp/evil/gzx-append.sh", "identity:gzx-append.sh"),
        ("W=/tmp/evil/gzx-append.sh bash $W map", "identity:gzx-append.sh"),
        # 引号参数族（claude #3065 §2 / pi #3067 §3 / grok #3069 §2）
        ("bash \"fake gzx-append.sh\" map", "identity:gzx-append.sh"),
        ("python3 \"fake gzx_cli.py\" save-cell", "identity:gzx_cli.py"),
        ("bash \" gzx-append.sh\" map", "identity:gzx-append.sh"),
        ("bash\"fake gzx-append.sh\"map", "expansion:unprovable"),
        ("bash\"fake gzx_cli.py\"save-cell", "expansion:unprovable"),
        ("echo\"see gzx-append\"", "identity:gzx-append"),
        # bash -c 载荷（dsh #3072 §1-3：与 allow 的 `bash -c 'ls'` 配对；Bugbot 4727125345）
        ("bash -c '~/.gan-zhong-xue/gzx-append.sh map'", "payload:gzx-append.sh"),
        ("bash -c \"python3 '%s/gzx_cli.py' preflight\"" % _pkg_skill, "payload:gzx_cli.py"),
        # 载荷里名字带内层引号（basename 不再干净）：程序串按子串判，dsh #3072 §1-4 (ii)
        ("bash -c \"python3 '/tmp/evil/gzx_cli.py' save-cell\"", "payload:gzx_cli.py"),
        # Bugbot 4728123561（High）：官方路径当陪衬参数，不能让别的程序借官方名过前缀检查
        ("env tee ~/.gan-zhong-xue/map.md ~/.gan-zhong-xue/gzx-append.sh", "prefix:env"),
        ("env sh -c 'printf x >> ~/.gan-zhong-xue/map.md' ~/.gan-zhong-xue/gzx-append.sh",
         "prefix:env"),
        ("sudo -n sh -c 'cat > ~/.gan-zhong-xue/map.md' ~/.gan-zhong-xue/gzx-append.sh",
         "prefix:sudo"),
        ("timeout 5 bash -c 'cat > ~/.gan-zhong-xue/map.md' ~/.gan-zhong-xue/gzx-append.sh",
         "prefix:timeout"),
        ("env bash /tmp/evil.sh ~/.gan-zhong-xue/map.md ~/.gan-zhong-xue/gzx-append.sh",
         "prefix:env"),
        ("W=~/.gan-zhong-xue/gzx-append.sh sudo -n tee ~/.gan-zhong-xue/map.md $W",
         "prefix:sudo"),
        ("python3 -c \"open('~/.gan-zhong-xue/map.md','w')\" '%s/gzx_cli.py'" % _pkg_skill,
         "prefix:python3"),
        ("env python3 '%s/gzx_cli.py' check <(tee ~/.gan-zhong-xue/map.md)" % _pkg_skill,
         "structure:<("),
        ("bash -c 'cat ~/.gan-zhong-xue/map.md'", "prefix:bash"),
        # ── 引导与数据目录直写（基线）──
        ("cp /tmp/gzx-append ~/.gan-zhong-xue/map.md", "prefix:cp"),
        ("cp /tmp/evil ~/.gan-zhong-xue/gzx-append.sh", "prefix:cp"),
        ("cp a ~/.gan-zhong-xue/gzx-append.sh b", "prefix:cp"),
        ("cp /tmp/evil/gzx-append.sh ~/.gan-zhong-xue/map.md", "prefix:cp"),
        ("cp /tmp/evil/gzx-append.sh ~/.gan-zhong-xue/gzx-append.sh", "prefix:cp"),
        ("cp ~/.gan-zhong-xue/gzx-append.sh ~/.gan-zhong-xue/gzx_lock_append.py.bak",
         "prefix:cp"),
        ("chmod 000 ~/.gan-zhong-xue/map.md", "prefix:chmod"),
        ("chmod 000 ~/.gan-zhong-xue/gzx-append.sh", "prefix:chmod"),
        ("chmod -R 700 ~/.gan-zhong-xue", "prefix:chmod"),
        ("mkdir ~/.gan-zhong-xue/sub", "prefix:mkdir"),
        ("cp /tmp/fake-plugin/gzx-append.sh ~/.gan-zhong-xue/gzx-append.sh", "prefix:cp"),
        ("chmod 777 ~/.gan-zhong-xue", "prefix:chmod"),
        ("chmod 755 ~/.gan-zhong-xue", "prefix:chmod"),
        ("cp ~/.gan-zhong-xue/map.md /tmp/evil.md", "prefix:cp"),
        ("cat ~/.gan-zhong-xue/map.md | tee ~/.gan-zhong-xue/map.jsonl", "prefix:tee"),
        ("rm ~/.gan-zhong-xue/map.md", "prefix:rm"),
        ("[ -s ~/.gan-zhong-xue/pending.md ]", "prefix:["),
        ("test -s ~/.gan-zhong-xue/pending.md", "prefix:test"),
        ("GZX_APP=~/.gan-zhong-xue/gzx-append.sh", "prefix:gzx-append.sh"),
        ("sed -i s/a/b/ ~/.gan-zhong-xue/map.md", "sed"),
        ("perl -i -pe s/a/b/ ~/.gan-zhong-xue/map.md", "perl"),
        ("python3 -c \"open('~/.gan-zhong-xue/map.md','w')\"", "prefix:python3"),
        ("echo hi > /tmp/out ~/.gan-zhong-xue/map.md", "prefix:echo"),
        # ── 结构层 ──
        ("~/.gan-zhong-xue/gzx-append.sh map <<'E'\nx\nE\nrm ~/.gan-zhong-xue/map.md",
         "structure"),
        ("if [ -s ~/.gan-zhong-xue/map.jsonl ]; then cat ~/.gan-zhong-xue/map.jsonl; fi",
         "structure"),
        ("mkdir -p ~/.gan-zhong-xue && chmod 700 ~/.gan-zhong-xue", "structure"),
        ("~/.gan-zhong-xue/gzx-append.sh map <<EOF\n$(echo pwn)\nEOF", "structure"),
        ("~/.gan-zhong-xue/gzx-append.sh map <<-'E'\nx\nE", "structure"),
        ("~/.gan-zhong-xue/gzx-append.sh map <<'A' <<'B'\nx\nA", "structure"),
        ("cat ~/.gan-zhong-xue/map.md && python3 -c \"open('m','w')\"", "structure"),
        ("cat ~/.gan-zhong-xue/map.md; rm ~/.gan-zhong-xue/map.md", "structure"),
        ("cat ~/.gan-zhong-xue/map.md\nrm ~/.gan-zhong-xue/map.md", "structure"),
        ("cat ~/.gan-zhong-xue/map.md #c\nrm ~/.gan-zhong-xue/map.md", "structure"),
        ("cat ~/.gan-zhong-xue/map.md & python3 -c \"open('m','w')\"", "structure"),
        ("find ~/.gan-zhong-xue -name map.md -execdir rm {} \\;", "find:-execdir"),
        ("cat ~/.gan-zhong-xue/map.md 'oops", "structure"),
        # 命令替换（含藏在双引号里的形态——以前随引号串漏过只读白名单）
        ("cat \"`rm ~/.gan-zhong-xue/map.md`\"", "structure:`"),
        ("cat \"$(rm ~/.gan-zhong-xue/map.md)\"", "structure:$("),
        ("cat ~/.gan-zhong-xue/map.md | grep \"$(rm ~/.gan-zhong-xue/map.md)\"", "structure:$("),
        # heredoc 形状（官方 heredoc 例外内的身份层与定界符约束，规则名保持）
        ("~/.gan-zhong-xue/gzx-append.sh map <<'E'\nhello\nE\nrm ~/.gan-zhong-xue/map.md\nE",
         "heredoc:delim_repeats"),
        ("~/.gan-zhong-xue/gzx-append.sh map <<'E'\nE\r\nrm ~/.gan-zhong-xue/map.md\nE",
         "heredoc:delim_repeats"),
        ("~/.gan-zhong-xue/gzx-append.sh map <<'E'\nE\r\r\nrm ~/.gan-zhong-xue/map.md\nE",
         "heredoc:delim_repeats"),
        ("/tmp/evil/gzx-append.sh map <<'E'\n~/.gan-zhong-xue/map.md\nE",
         "heredoc:writer_identity"),
        ("./gzx-append.sh map <<'E'\n~/.gan-zhong-xue/map.md\nE",
         "heredoc:writer_identity"),
        ("python3 /tmp/evil/gzx_cli.py save-cell <<'E'\n~/.gan-zhong-xue/map.md\nE",
         "heredoc:cli_identity"),
        ("/tmp/evil/gzx-append.sh map <<'E'\nhello\nE", "heredoc:writer_identity"),
        ("python3 /tmp/evil/gzx_cli.py save-cell <<'E'\nhello\nE", "heredoc:cli_identity"),
        ("bash ~/.gan-zhong-xue/gzx-append.sh map <<'E'\n## x\nE", "structure:heredoc"),
        # ── 重定向到数据目录 ──
        ("cat > ~/.gan-zhong-xue/map.md <<'E'\nx\nE", "redirect"),
        ("cat x > ~/.GAN-ZHONG-XUE/map.jsonl", "redirect-to-protected"),
        ("cat x > /c/Users/foo/.gan-zhong-xue/map.jsonl", "redirect-to-protected"),
        ("cat x > ~/.gan-zhong-xue/map.jsonl", "redirect"),
        ("cat ~/.gan-zhong-xue/map.md > ~/.gan-zhong-xue/map.jsonl", "redirect"),
        ("cat ~/.gan-zhong-xue/map.md>~/.gan-zhong-xue/map.jsonl", "redirect"),
        ("cat ~/.gan-zhong-xue/map.md >| ~/.gan-zhong-xue/map.jsonl", "redirect"),
        ("cat ~/.gan-zhong-xue/map.md &> ~/.gan-zhong-xue/map.jsonl", "redirect"),
        ("cat map.md | gzx-append map > ~/.gan-zhong-xue/map.md", "identity:gzx-append"),
        ("cat x >~/.gan-zhong-xue/map.md", "redirect-to-protected"),
        ("cat x >>~/.gan-zhong-xue/map.md", "redirect-to-protected"),
        ("cat x 2>~/.gan-zhong-xue/err.log", "redirect-to-protected"),
        ("cat x &>>~/.gan-zhong-xue/map.md", "redirect-to-protected"),
        ("find ~/.gan-zhong-xue -name map.md -fprint ~/.gan-zhong-xue/map.md", "find"),
        ("find ~/.gan-zhong-xue -name map.md -delete", "find"),
        # ── 字面性闸：动态目标 / 通配符 / 引号转义拼接 / 身份位 ──
        ("cat x > $HOME/.gan-zhong-xue/map.jsonl", "expansion:unprovable"),
        ("cat x >$HOME/.gan-zhong-xue/map.jsonl", "expansion:unprovable"),
        ("cat x > $VAR", "expansion:unprovable"),
        ("cat x >$VAR", "expansion:unprovable"),
        ("cat x > ${HOME}/.gan-zhong-xue/map.jsonl", "expansion:unprovable"),
        ("cat x > /home/$USER/.gan-zhong-xue/map.jsonl", "expansion:unprovable"),
        ("cat x > $(echo ~/.gan-zhong-xue/map.jsonl)", "expansion:unprovable"),
        ("cat x > `echo ~/.gan-zhong-xue/map.jsonl`", "expansion:unprovable"),
        ("cat x > \"$OUT\"", "expansion:unprovable"),
        ("cat x > '$OUT'", "expansion:unprovable"),
        ("cat x > out$N.log", "expansion:unprovable"),
        ("cat x > cache-[abc].log", "expansion:unprovable"),
        ("cat x > ~/.gan-zhong-*/map.jsonl", "expansion:unprovable"),
        ("cat ~/.gan-zhong-xue/map.md > ~/.gan-zhong-*/map.jsonl", "expansion:unprovable"),
        ("cat x > ~/.gan-zhong-x?e/map.jsonl", "expansion:unprovable"),
        ("cat x > ~/.gan-zhong-xu[e]/map.jsonl", "expansion:unprovable"),
        ("cat x > $'/home/u/.gan-zhong-xue/map.jsonl'", "expansion:unprovable"),
        ("cat x > ~/.gan-zhong-\"\"xue/map.jsonl", "redirect-to-protected"),
        ("cat x > ~/.gan-zhong-xu\\e/map.jsonl", "redirect-to-protected"),
        ("cat x > ~/.gan-zhong-'xue'/map.jsonl", "redirect-to-protected"),
        ("cat > $F <<'EOF'\nx\nEOF", "expansion:unprovable"),
        ("$SCRIPT map", "expansion:unprovable"),
        ("\"$PY\" /tmp/evil/gzx_cli.py save-cell", "expansion:unprovable"),
        ("bash $SCRIPT map", "expansion:unprovable"),
        ("bash gzx_*.sh map", "expansion:unprovable"),
        ("bash gzx-app*.sh map", "expansion:unprovable"),
        ("python3 /tmp/gzx_cl?.py save-cell", "expansion:unprovable"),
        ("bash$WR map", "expansion:unprovable"),
        ("echo$HOME", "expansion:unprovable"),
        ("$EDITOR file.txt", "expansion:unprovable"),
        # ── 已申报代价（拒绝方向；README「已知代价」逐条对应）──
        ("python3 -m py_compile /tmp/evil/gzx_cli.py", "identity:gzx_cli.py"),
        ("python3 tool.py gzx_cli.py", "identity:gzx_cli.py"),
        ("sudo cp /tmp/evil/gzx_cli.py /tmp/x", "identity:gzx_cli.py"),
        ("env X=$Y python3 hooks/check.py /tmp/evil/gzx_cli.py", "identity:gzx_cli.py"),
        ("command -v gzx-append.sh", "identity:gzx-append.sh"),
        # 参数位的裸名（启动器包着的直调也一样）：请写路径
        ("env gzx-append.sh map", "identity:gzx-append.sh"),
        ("sudo -n gzx-append.sh map", "identity:gzx-append.sh"),
        ("timeout 5 gzx-append.sh map", "identity:gzx-append.sh"),
        ("source gzx-append.sh map", "identity:gzx-append.sh"),
        (". gzx-append.sh map", "identity:gzx-append.sh"),
        ("bash <<< 'gzx-append.sh'", "identity:gzx-append.sh"),
    ]
    # 环境条件（dsh #3055 §3：裸名用例按「安装态是否存在」条件生效；跳过显式打印
    # 且留在分母里，不伪造 PASS——#128 C3「跳过不得表现为通过」的守卫侧同款）
    conds = {
        "writer-on-path": lambda: _head_is_official(WRITER_MARK),
        "writer-not-on-path": lambda: not _head_is_official(WRITER_MARK + ".sh"),
    }
    fails = []
    skipped = []
    for entry in allows:
        c, cond = entry if isinstance(entry, tuple) else (entry, None)
        if cond and not conds[cond]():
            skipped.append("ALLOW %r (env %s 未满足)" % (c, cond))
            continue
        d = classify(c)
        if not d.allow:
            fails.append("ALLOW case denied: %r -> %s" % (c, d.rule))
    for entry in denies:
        c, rule, cond = entry if len(entry) == 3 else (entry[0], entry[1], None)
        if cond and not conds[cond]():
            skipped.append("DENY %r (env %s 未满足)" % (c, cond))
            continue
        d = classify(c)
        if d.allow:
            fails.append("DENY case allowed: %r" % c)
        elif rule and not d.rule.startswith(rule):
            fails.append("DENY case %r hit %s, expected %s*" % (c, d.rule, rule))
    for f in fails:
        print("FAIL " + f)
    for s in skipped:
        print("SKIP " + s)
    print("selftest: %d allow + %d deny cases (分母含跳过), %d env-skipped, %d failures"
          % (len(allows), len(denies), len(skipped), len(fails)))
    return 1 if fails else 0


def main():
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    raw = sys.stdin.read()
    try:
        event = json.loads(raw)
    except ValueError:
        return 0
    raise SystemExit(_handle(event))


if __name__ == "__main__":
    main()
