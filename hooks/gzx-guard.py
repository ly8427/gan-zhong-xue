#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gzx-guard.py — gan-zhong-xue 数据面兜底 hook（v1.2.0）。

定位：减速带 + 审计，不是安全边界。真不变量在写入器 gzx-append.sh / gzx_cli.py。
v1.2.0 收窄范围判据：不再以泛化的 "gan-zhong-xue" 字样触发拦截（避免误杀同仓开发、
文档编辑及 Agent 提示词），改为精确指向运行时数据目录（.gan-zhong-xue / GZX_DIR）。
"""

import json
import os
import re
import shlex
import shutil
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
STRUCTURE_DENY_TOKENS = {";", "&&", "||", "&", "$(", "`", "<(", ">(", "\n"}
REDIRECT_TOKENS = {">", ">>", ">|", ">&", "&>", "&>>", "<>"}
PIPE_TOKENS = {"|", "|&"}
SHELL_WRAPPERS = {"bash", "sh", "source", "."}
PYTHON_BINS = {"python", "python3"}


class Decision:
    def __init__(self, allow, rule):
        self.allow = allow
        self.rule = rule


def _basename(tok):
    return tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


def _structure_tokens(cmd):
    """结构层分词。返回 (tokens, None) 或 (None, 错误信息)。"""
    lex = shlex.shlex(cmd, posix=True, punctuation_chars="();<>|&`$\n")
    lex.whitespace_split = False
    lex.whitespace = " \t\r"
    lex.commenters = ""
    try:
        return list(lex), None
    except ValueError:
        return None, "unparseable"


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


def _is_writer_token(tok):
    base = _basename(tok)
    if base not in (WRITER_MARK, WRITER_MARK + ".sh"):
        return False
    # 如果带路径（相对或绝对路径），必须严格指向官方安装位
    if "/" in tok or "\\" in tok:
        cand = os.path.expanduser(tok)
        return _canon_path(cand) == _writer_installed_path()
    # 裸名直调（从 PATH 解析）：必须真实存在且指向官方安装位，未命中则拒绝
    w = shutil.which(tok)
    if w:
        return _canon_path(w) == _writer_installed_path()
    return False


def _is_cli_token(tok):
    if _basename(tok) != CLI_FILENAME:
        return False
    cand = os.path.expanduser(tok)
    return _canon_path(cand) == _cli_official_path()


def _is_cli_segment(seg):
    if len(seg) < 2:
        return False
    head = _basename(seg[0]).lower()
    if head.endswith(".exe"):
        head = head[:-4]
    return head in PYTHON_BINS and _is_cli_token(seg[1])


def _seg_head_is_allowed(seg):
    head = _basename(seg[0])
    if head in READONLY_PREFIX:
        return True
    if _is_writer_token(seg[0]):
        return True
    if _is_cli_segment(seg):
        return True
    if head in SHELL_WRAPPERS:
        # 包装器（bash/sh/source/.）参数扩展检查（dsh #3055 §3 / grok #3054 High /
        # pi #3058 §B-2）：参数里出现写入器名（裸名或带路径）必须能解析到官方安装位，
        # 解析不到（which 未命中 / 指向别处）一律拒——fail-closed，不许用
        # 「查不到即放行」换绿灯。裸名在装了官方写入器（PATH 可达）的机器上解析
        # 成功 ⇒ 放行（合法直调），所以裸名 allow 用例按安装态条件生效。
        ok = False
        for t in seg[1:]:
            b = _basename(t)
            if b in _WRITER_BASENAMES and not _is_writer_token(t):
                return False
            if _is_writer_token(t):
                ok = True
            elif ("/" in t or "\\" in t or t.startswith("~")) and \
                    _canon_path(t) == _writer_installed_path():
                ok = True
        return ok
    return False


_BOOTSTRAP_SRC_NAMES = {"gzx-append.sh", "gzx_lock_append.py"}
_BOOTSTRAP_SETUP = {"mkdir", "chmod"}


def _seg_is_bootstrap(seg):
    head = _basename(seg[0])
    cur_dir = GZX_DIR
    if head in ("cp", "install"):
        if len(seg) != 3:
            return False
        src_path = os.path.expanduser(seg[1])
        src = _basename(src_path)
        dst_path = os.path.expanduser(seg[2])
        dst = _basename(dst_path)
        dst_dir = os.path.dirname(dst_path)
        if (src not in _BOOTSTRAP_SRC_NAMES
                or dst not in _BOOTSTRAP_SRC_NAMES
                or _canon_path(dst_dir) != _canon_path(cur_dir)):
            return False
        return _canon_path(src_path) == _canon_path(
            os.path.join(_plugin_skill_dir(), src))
    if head in _BOOTSTRAP_SETUP:
        args = [t for t in seg[1:] if not (t.isdigit() and len(t) == 1)]
        pathish = [t for t in args if ("/" in t or "\\" in t or t.startswith("~"))]
        if head == "mkdir":
            targets = [t for t in args if not t.startswith("-")]
            return (bool(targets)
                    and all(_canon_path(t) == _canon_path(cur_dir) for t in targets))
        modeparts = [t for t in args if t not in pathish]
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


# writer-heredoc 例外（支持无引号与引号包裹的写入器路径）
_HEREDOC_WRITER_RE = re.compile(
    r"^[ \t]*(?:'[^'\n]*gzx-append\.sh'|\"[^\"\n]*gzx-append\.sh\"|\S*gzx-append\.sh)"
    r"[ \t]+(map|jsonl|pending|candidates|audit)"
    r"[ \t]+<<[ \t]*(['\"])([A-Za-z_][A-Za-z0-9_]*)\2\n((?:.*\n)?)\3$",
    re.DOTALL,
)

# 官方 CLI heredoc 例外（save-cell / pending-add）
_HEREDOC_CLI_RE = re.compile(
    r"^[ \t]*(?:'[^'\n]*python3?(?:\.exe)?'|\"[^\"\n]*python3?(?:\.exe)?\"|\S*python3?(?:\.exe)?)[ \t]+"
    r"(?:'[^'\n]*gzx_cli\.py'|\"[^\"\n]*gzx_cli\.py\"|\S*gzx_cli\.py)"
    r"[ \t]+(save-cell|pending-add)"
    r"[ \t]+<<[ \t]*(['\"])([A-Za-z_][A-Za-z0-9_]*)\2\n((?:.*\n)?)\3$",
    re.DOTALL,
)


_REDIR_RE = re.compile(r"^(?:[0-9]+)?(>>|>&|&>>|&>|>\||<>|>)(.*)$")
_ALL_REDIR_RE = re.compile(r"^(?:[0-9]+)?(<<<|<<|<|>>|>&|&>>|&>|>\||<>|>)(.*)$")


# ── 字面性闸与范围判定（board #129 收敛共识：dsh #3055 §6-1/#3061 §2、grok #3054
#    High/#3059 下一刀 1-3、pi #3058 §B、claude #3053 A-1/A-3、Bugbot 4722010420/4722010427）──
# 事实基础（分词器 = shlex posix + punctuation_chars="();<>|&`$\n"，字符类由源码与
# 实测钉死，见 Lib/shlex.py __init__：punctuation_chars 非空时 wordchars += '~-./*?='）：
#   wordchars = 字母数字 + _ ~ - . / * ? = ；标点集 = ( ) ; < > | & ` $ \n ；
#   其余可打印字符（[ ] { } , : 等）成为单字符分隔词元；posix 引号剥除后，
#   含空白的词元必来自引号内容。未加引号的 $ 与 ` 是标点 ⇒ 把词切碎。
# 词边界唯一硬规则：两个相邻 word 词元之间必有空白（否则分词器会并成一个词元）。
# 其余相邻关系（frag/quoted 与任何词元）有无空白不可区分 ⇒ 按同词续接（宁枉勿纵）。

_EXPANSION_CHARS = "*?[]{}$`"
_WRITER_BASENAMES = (WRITER_MARK, WRITER_MARK + ".sh")
_NAME_BASENAMES = set(_WRITER_BASENAMES) | {CLI_FILENAME}
_WORDCHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_~-./?*=")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_HEREDOC_DELIM_RE = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*|'[^']*'|\"[^\"]*\")$")
# bash 保留字（与 gzx-capture.py _SHELL_RESERVED 同源口径）：保留字必须独立成词，
# 其后的分隔词元（[ { 等）必是新词——否则 `if [ -s x ]` 的 if 与 [ 会被词重建
# 粘成 `if[`（word→frag 边界有无空白不可区分，保留字是唯一可靠的断词信号）。
_SHELL_RESERVED = {"if", "then", "else", "elif", "fi", "while", "until", "do",
                   "done", "for", "case", "esac", "in", "function", "time",
                   "!", "coproc", "select"}
# 权威来源：python3 --help（3.12 实测）。-c/-m/- 终止选项表（其后无脚本位）。
_PY_NOARG_OPTS = {"-b", "-B", "-d", "-E", "-h", "-i", "-I", "-O", "-OO", "-P",
                  "-q", "-s", "-S", "-u", "-v", "-V", "-?", "--help",
                  "--help-env", "--help-xoptions", "--help-all", "--version"}
_PY_CONSUME_OPTS = ("-W", "-x", "-X", "--check-hash-based-pycs")
_PY_STOP_OPTS = {"-c", "-m", "-"}


def _has_expansion(s):
    return any(c in _EXPANSION_CHARS for c in s)


def _head_static_prefix(w):
    """命令名词的静态前缀：到第一个扩展算子/非 wordchar 碎片边界为止。
    'echo$HOME' → 'echo'（$ 处是碎片边界，动态部分可能是独立参数——word→frag
    边界的空白不可区分，命令名闸只认静态前缀）；'$SCRIPT' → ''（命令名以动态
    字符开头 ⇒ 不可证）；'gzx-*.sh' → 全词（* ? 是 wordchar，通配在词内）。"""
    for idx, c in enumerate(w):
        if c in _EXPANSION_CHARS or c not in _WORDCHARS:
            return w[:idx]
    return w


def _tok_kind(t):
    """word=wordchar 连续串；quoted=含空白（必来自引号内容，永不为操作符）；
    frag=标点串或单字符分隔符。"""
    if " " in t or "\t" in t or "\r" in t:
        return "quoted"
    if all(c in _WORDCHARS for c in t):
        return "word"
    return "frag"


def _segment_words(frags):
    """把一个段内的词元重建为「词」：[(文本, 是否含引用内容)]。
    相邻 word+word ⇒ 新词（必有空白）；其余相邻续接同词；
    词首孤立 [ 或 { 视为完整词（test 内建 / 花括号组保留基线语义）；
    当前词恰为保留字时必断词（`if [` 的 if 与 [ 不可粘）。"""
    out = []
    cur = None      # (text, quoted)
    prev_kind = None
    for t in frags:
        kind = _tok_kind(t)
        q = kind == "quoted"
        new_word = cur is None or (prev_kind == "word" and kind == "word")
        if cur is not None and cur[0] in _SHELL_RESERVED and not cur[1]:
            new_word = True
        if not new_word:
            cur = (cur[0] + t, cur[1] or q)
        else:
            if cur is not None:
                out.append(cur)
            if t in ("[", "{"):
                out.append((t, False))
                cur = None
            else:
                cur = (t, q)
        prev_kind = kind
    if cur is not None:
        out.append(cur)
    return out


def _split_segments(toks):
    """按管道/结构符切段（不产生判定，供字面性闸与名字臂使用）。"""
    segs = [[]]
    for t in toks:
        if t in STRUCTURE_DENY_TOKENS or "\n" in t:
            segs.append([])
            continue
        if t in PIPE_TOKENS:
            segs.append([])
            continue
        segs[-1].append(t)
    return [s for s in segs if s]


def _effective_head(words):
    """跳过 VAR=… 赋值前缀后的命令名词元（无则 None）。"""
    for w, _q in words:
        if _ENV_ASSIGN_RE.match(w):
            continue
        return w
    return None


def _is_py_head(head):
    if head in PYTHON_BINS:
        return True
    return head.endswith(".exe") and head[:-4] in PYTHON_BINS


def _py_script_slot_idx(arg_toks):
    """解释器段：按 python3 --help 选项表定位脚本位词元下标（无则 None）。"""
    i = 0
    n = len(arg_toks)
    while i < n:
        t = arg_toks[i]
        if t in _PY_STOP_OPTS:
            return None
        if t in _PY_NOARG_OPTS:
            i += 1
            continue
        if t.startswith(_PY_CONSUME_OPTS):
            # 值连写（-Xutf8 / --check-hash-based-pycs=default）只跳自身；独立值形式吃下一个词元
            i += 1 if ("=" in t or len(t) > 2) else 2
            continue
        if t.startswith("-"):
            return None       # 未知选项：保守——不认定脚本位
        return i
    return None


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


def _pregate_expansion(toks, segments, seg_words):
    """字面性闸（与范围无关，进出范围两向都拦）。返回 deny Decision 或 None。
    位置：a. 各段有效命令名；b. bash/sh/source/. 段的全部未引用参数
    （bash 展开一切参数通配）；c. python/python3 段全部未引用参数（保守，防
    脚本位被选项推后）；d. 输出重定向目标（含被 $ ` [ { 劈碎的续接碎片）。"""
    for seg, words in zip(segments, seg_words):
        head_w = _effective_head(words)
        if head_w is None:
            continue
        if head_w not in ("[", "{"):
            prefix = _head_static_prefix(head_w)
            if _has_expansion(prefix) or prefix == "":
                return Decision(False, "expansion:unprovable")
        else:
            prefix = head_w
        head_base = prefix.lower()
        is_wrapper = head_base in SHELL_WRAPPERS
        is_py = _is_py_head(head_base)
        if is_wrapper or is_py:
            # 参数闸（含命令名词里粘着的动态尾巴：bash$SCRIPT 的 $SCRIPT 就是
            # 它的第一个参数——word→frag 边界的空白不可区分，按参数对待）。
            # 引用串（含空白）豁免：bash -c 'payload' 的载荷不在此审计。
            arg_words = []
            if head_w != prefix:
                arg_words.append((head_w[len(prefix):], False))
            arg_words.extend(words[1:])
            for w, q in arg_words:
                if not q and _has_expansion(w):
                    return Decision(False, "expansion:unprovable")
    # 输出重定向目标：op 的 tail + 其后按词边界续接的碎片
    i = 0
    n = len(toks)
    while i < n:
        t = toks[i]
        if t in STRUCTURE_DENY_TOKENS or "\n" in t or t in PIPE_TOKENS:
            i += 1
            continue
        m = _REDIR_RE.match(t)
        if not m:
            if t in ("<", "<<<"):
                i += 2
                continue
            i += 1
            continue
        target = m.group(2)
        j = i + 1
        prev_kind = "frag" if target else None
        # tail 为空时目标是下一个词元起；续接规则与 _segment_words 一致
        while j < n:
            nt = toks[j]
            if nt in STRUCTURE_DENY_TOKENS or "\n" in nt or nt in PIPE_TOKENS:
                # 目标区域内的结构算子（$( ` 等）含扩展字符 ⇒ 目标不可证；否则目标完整
                if _has_expansion(nt):
                    return Decision(False, "expansion:unprovable")
                break
            kind = _tok_kind(nt)
            if target and prev_kind == "word" and kind == "word":
                break                     # 两个 word 相邻 ⇒ 必有空白 ⇒ 目标已完整
            m2 = _REDIR_RE.match(nt)
            if m2 and _tok_kind(nt) != "quoted":
                break                     # 下一个重定向操作符
            target += nt
            prev_kind = kind
            j += 1
        if target and _has_expansion(target):
            return Decision(False, "expansion:unprovable")
        i = j if j > i else i + 1
    return None


def _name_arm_hit(seg_words):
    """名字臂（臂③，进范围的第三条件）：执行位的写入器/CLI 名按逐词 basename
    相等（禁全文正则——21531ca 全文正则误杀同仓，38231ee 撤名臂后假 CLI 直跑
    重开；此处为收敛形态）。执行位 = 有效命令名、bash/sh/source/. 的参数、
    python/python3 的脚本位。git add / echo 的参数不在执行位 ⇒ 同仓三条保持
    放行（pi #3058 §B-3）。"""
    for words in seg_words:
        head_w = _effective_head(words)
        if head_w is None:
            continue
        head_base = _basename(head_w).lower()
        if head_base in _NAME_BASENAMES:
            return True
        if head_base in SHELL_WRAPPERS:
            if any(_basename(w).lower() in _NAME_BASENAMES for w, _q in words[1:]):
                return True
        elif _is_py_head(head_base):
            idx = _py_script_slot_idx([w for w, _q in words[1:]])
            if idx is not None:
                w = [w for w, _q in words[1:]][idx]
                if _basename(w).lower() in _NAME_BASENAMES:
                    return True
    return False


def _in_scope_tokens(toks, bodies, seg_words):
    """token 级范围判定（dsh #3055 §6-1：范围门挪到分词之后）。
    任一命令行词元含数据目录标记，或 heredoc 正文含标记，或名字臂命中 ⇒ 进范围。"""
    for t in toks:
        low = t.lower()
        for m in _protected_markers():
            if m in t or m.lower() in low:
                return True
    for b in bodies:
        low = b.lower()
        for m in _protected_markers():
            if m in b or m.lower() in low:
                return True
    return _name_arm_hit(seg_words)


def _check_redirect(toks, i):
    """统一的重定向检查与目标消费（修复 $ 劈裂、输入重定向与只读穿透）。
    返回 (is_redir, decision_if_deny, next_i)。若非重定向，返回 (False, None, i)。"""
    t = toks[i]
    m = _REDIR_RE.match(t)
    if not m:
        # 输入重定向（<, <<<）：只读行为，跳过其目标操作数，不当作输出写目标
        if t in ("<", "<<<"):
            return True, None, min(i + 2, len(toks))
        return False, None, i

    op = m.group(1)
    tail = m.group(2)
    next_i = i + 1
    target_parts = []
    if tail:
        target_parts.append(tail)
    else:
        if next_i >= len(toks):
            return True, Decision(False, "redirect:missing-target"), next_i
        target_parts.append(toks[next_i])
        next_i += 1

    # 数字目标（fd 复制，如 2>&1 或 1>&2），重定向自身已封闭，绝不向下多吞普通参数
    if len(target_parts) == 1 and target_parts[0].isdigit() and op in (">&", ">"):
        return True, None, next_i

    # $ 碎片拼接启发式已移除：含 $/`/[/{ 的目标在前置字面性闸（_pregate_expansion，
    # 范围无关）一律 deny expansion:unprovable，到不了这里——拼接式「拼出像不像受
    # 保护路径」判据被 $ 断片 / 字面前缀+$ / 通配符三族实证绕过，判据必须是
    # 「没有扩展算子」（board #129 dsh #3061 §2 收敛）。

    full_target = "".join(target_parts)
    if _is_protected(full_target) or any(_is_protected(p) for p in target_parts):
        return True, Decision(False, "redirect-to-protected:" + op), next_i

    return True, None, next_i


def _check_heredoc_first_line(cmd, delim, body, is_cli=False):
    lines = cmd.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    lines = [l.rstrip("\r") for l in lines]
    if [i for i, l in enumerate(lines) if l == delim] != [len(lines) - 1]:
        return Decision(False, "heredoc:delim_repeats")
    first = lines[0]
    toks, tok_err = _structure_tokens(first)
    if tok_err or sum(1 for t in toks if t == "<<") != 1 \
            or any(t.startswith("<<") and t != "<<" for t in toks):
        return Decision(False, "heredoc:operator")
    i2 = 0
    while i2 < len(toks):
        t = toks[i2]
        if t in STRUCTURE_DENY_TOKENS or "\n" in t:
            return Decision(False, "structure:" + t.replace("\n", "\\n")[:24])
        is_redir, d_redir, next_i2 = _check_redirect(toks, i2)
        if is_redir:
            if d_redir:
                return d_redir
            i2 = next_i2
            continue
        i2 += 1
    if is_cli:
        if len(toks) < 2 or not _is_cli_segment(toks):
            return Decision(False, "heredoc:cli_identity")
        return Decision(True, "cli-heredoc:%dB" % len(body.encode("utf-8")))
    if not toks or not _is_writer_token(toks[0]):
        return Decision(False, "heredoc:writer_identity")
    return Decision(True, "writer-heredoc:%dB" % len(body.encode("utf-8")))


def classify(cmd):
    """对一条 Bash 命令做守卫判定。返回 Decision。纯函数，便于测试。

    流序（board #129 收敛共识，dsh #3055 §6-1 / #3061 §2、grok #3059 下一刀 1-3、
    pi #3058 §B、claude #3053 A-1/A-3、Bugbot 4722010420/4722010427）：
    0. 官方 writer/CLI heredoc 例外 → 身份层（与基线一致，先于一切）；
    1. heredoc 正文剥离（正文是数据不是 shell，判定只看命令行；pi #3058 §B-2：
       包装器 + heredoc 形态此前到不了身份层，剥离后可达）；剥不动 ⇒ 拒；
    2. 分词命令行；失败 ⇒ structure:unparseable（dsh #3055 §6-1 默认拒）；
    3. 字面性闸（与范围无关）：命令名 / 包装器与解释器参数 / 输出重定向目标
       含扩展算子 ⇒ expansion:unprovable；
    4. 范围门：标记词元 ∨ heredoc 正文标记 ∨ 名字臂（执行位 basename 相等）；
    5. 范围外 ⇒ 放行 out-of-scope（零审计，与基线一致）；
    6. 范围内：非官方 heredoc ⇒ structure:heredoc（基线由换行结构符拒，剥离后
       改由本规则承担）；结构符/重定向/逐段检查与基线同序。"""
    # 0. 臂③：writer / CLI heredoc 语法必须进入身份层检验（即使正文不含保护目录字样，假写入器也必拒）
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
    toks, err = _structure_tokens(work)
    if err:
        return Decision(False, "structure:unparseable")

    # 3. 字面性闸（范围无关，进出范围两向都拦）
    segments = _split_segments(toks)
    seg_words = [_segment_words(s) for s in segments]
    d_gate = _pregate_expansion(toks, segments, seg_words)
    if d_gate:
        return d_gate

    # 4./5. 范围门（token 级标记 + heredoc 正文标记 + 名字臂）
    if not _in_scope_tokens(toks, bodies, seg_words):
        return Decision(True, "out-of-scope")

    # 6. 范围内主循环（基线同序：结构符拒 → 管道分段 → 重定向判定）
    segments = [[]]
    i = 0
    n = len(toks)
    while i < n:
        t = toks[i]
        if t in STRUCTURE_DENY_TOKENS or "\n" in t:
            return Decision(False, "structure:" + t.replace("\n", "\\n")[:24])
        if t in PIPE_TOKENS:
            segments.append([])
            i += 1
            continue
        is_redir, d_redir, next_i = _check_redirect(toks, i)
        if is_redir:
            if d_redir:
                return d_redir
            i = next_i
            continue
        segments[-1].append(t)
        i += 1

    for seg in segments:
        if not seg:
            continue
        head = _basename(seg[0])
        if head == "find":
            for t in seg[1:]:
                if t in FIND_DENY_FLAGS:
                    return Decision(False, "find:" + t)
        if head in ("sed", "perl"):
            for t in seg[1:]:
                if t == "-i" or (t.startswith("-i") and len(t) > 2 and t[2] != "n"):
                    return Decision(False, head + ":-i")
        if _seg_is_bootstrap(seg):
            continue
        if not _seg_head_is_allowed(seg):
            return Decision(False, "prefix:" + head)
        if _is_writer_token(seg[0]) or _is_cli_segment(seg):
            continue

    # 7. 非官方 heredoc（范围内）一律拒：正文是数据不是 shell，不可静态审计；
    #    官方 writer/CLI 形态已在第 0 步进身份层。基线此形状由换行结构符拒绝
    #    （正文含换行），正文剥离后改由本规则承担，拒绝方向不变。
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


def selftest():
    _pkg_skill = _posix_form(_plugin_skill_dir())
    allows = [
        "sha256sum ~/.gan-zhong-xue/map.md",
        "grep -i 先猜 ~/.gan-zhong-xue/map.md",
        "grep 先猜 ~/.gan-zhong-xue/map.md | head",
        "grep 先猜 ~/.gan-zhong-xue/map.md|head",
        "cat ~/.gan-zhong-xue/map.md > /dev/null",
        "cat ~/.gan-zhong-xue/map.md 2>/dev/null",
        "echo \"a > b\"",
        "~/.gan-zhong-xue/gzx-append.sh map < payload",
        # 裸名直调依赖 PATH 解析到官方安装位：装了才测，没装显式跳过（dsh #3055 §3：
        # 不许用 fail-open 换绿灯；用例按安装态条件生效，两种环境都诚实）
        ("gzx-append map", "writer-on-path"),
        ("bash gzx-append.sh map", "writer-on-path"),
        "cat payload | ~/.gan-zhong-xue/gzx-append.sh candidates",
        "bash ~/.gan-zhong-xue/gzx-append.sh map",
        "cp '%s/gzx-append.sh' ~/.gan-zhong-xue/gzx-append.sh" % _pkg_skill,
        "mkdir -p ~/.gan-zhong-xue",
        "chmod 700 ~/.gan-zhong-xue",
        "chmod +x ~/.gan-zhong-xue/gzx-append.sh",
        "cp '%s/gzx_lock_append.py' ~/.gan-zhong-xue/gzx_lock_append.py" % _pkg_skill,
        "ls ~/.gan-zhong-xue/gzx-append.sh 2>/dev/null",
        "cat ~/.gan-zhong-xue/pending.md 2>/dev/null",
        "cat ~/.gan-zhong-xue/candidates.jsonl 2>/dev/null",
        "~/.gan-zhong-xue/gzx-append.sh pending-clear",
        ("gzx-append pending-clear", "writer-on-path"),
        "~/.gan-zhong-xue/gzx-append.sh badtarget </dev/null",
        # v1.2.0: 聚合 CLI 与同仓开发不再被误杀
        "python3 '%s/gzx_cli.py' preflight" % _pkg_skill,
        ("python3 '%s/gzx_cli.py' save-cell <<'GZX_JSON_EOF'\n"
         '{"概念":"~/.gan-zhong-xue 范围测试","分层":"🟡","来源":"🟢自","先猜":"猜","验证题":"题","状态":"待复验"}\n'
         "GZX_JSON_EOF") % _pkg_skill,
        "cd /tmp/gan-zhong-xue && git status",
        "git commit -m \"fix gan-zhong-xue bug\"",
        "python3 hooks/test-gzx-invariants.py",
        # 带引号的写入器 heredoc 路径
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
        "~/.gan-zhong-xue/gzx-append.sh jsonl <<< '{\"id\":2}'",
        # Bugbot 新增防护用例：只读形态含 2>&1 或输入重定向放行，同仓 git 命令放行
        "cat 2>&1 ~/.gan-zhong-xue/map.md",
        "cat > /tmp/out < ~/.gan-zhong-xue/map.md",
        "git add skills/gan-zhong-xue/gzx_cli.py",
        "git commit -m 'update gzx_cli.py'",
        # #129 收敛放行面回归钉（pi #3058 §B-3 同仓三条）：名字臂只认执行位
        "echo see gzx-append",
        "echo $HOME",
        "grep $PAT ~/.gan-zhong-xue/map.md",
        "python3 -m py_compile skills/gan-zhong-xue/gzx_cli.py",
    ]
    denies = [
        # Bugbot 新增防护用例：shell wrapper 运行本地目录伪造写入器触碰数据目录必拒
        ("bash ./gzx-append.sh map < ~/.gan-zhong-xue/pending.md", "prefix:bash"),
        # 裸名经包装器：which 解析不到官方安装位必拒；解析得到（写环境装了官方写入器
        # 且 PATH 可达）则合法直调 → 该钉按安装态条件生效（两种环境都诚实）
        ("bash gzx-append.sh map < ~/.gan-zhong-xue/pending.md", "prefix:bash",
         "writer-not-on-path"),
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
        ("~/.gan-zhong-xue/gzx-append.sh map <<'E'\nx\nE\nrm ~/.gan-zhong-xue/map.md",
         "structure"),
        ("cat > ~/.gan-zhong-xue/map.md <<'E'\nx\nE", "redirect"),
        ("[ -s ~/.gan-zhong-xue/pending.md ]", "prefix:["),
        ("test -s ~/.gan-zhong-xue/pending.md", "prefix:test"),
        ("if [ -s ~/.gan-zhong-xue/map.jsonl ]; then cat ~/.gan-zhong-xue/map.jsonl; fi",
         "structure"),
        ("GZX_APP=~/.gan-zhong-xue/gzx-append.sh", "prefix:gzx-append.sh"),
        ("mkdir -p ~/.gan-zhong-xue && chmod 700 ~/.gan-zhong-xue", "structure"),
        ("~/.gan-zhong-xue/gzx-append.sh map <<EOF\n$(echo pwn)\nEOF", "structure"),
        ("~/.gan-zhong-xue/gzx-append.sh map <<'E'\nhello\nE\nrm ~/.gan-zhong-xue/map.md\nE",
         "heredoc:delim_repeats"),
        ("~/.gan-zhong-xue/gzx-append.sh map <<-'E'\nx\nE", "structure"),
        ("~/.gan-zhong-xue/gzx-append.sh map <<'A' <<'B'\nx\nA", "structure"),
        ("/tmp/evil/gzx-append.sh map <<'E'\n~/.gan-zhong-xue/map.md\nE",
         "heredoc:writer_identity"),
        ("./gzx-append.sh map <<'E'\n~/.gan-zhong-xue/map.md\nE",
         "heredoc:writer_identity"),
        ("python3 /tmp/evil/gzx_cli.py save-cell <<'E'\n~/.gan-zhong-xue/map.md\nE",
         "heredoc:cli_identity"),
        # #128 A 组红线：臂③ heredoc 假写入器/假 CLI（规则名不变）+ $ 劈裂（规则名
        # 按 #129 共识归入字面性闸，grok #3059 下一刀1「给独立 rule 名」）
        ("/tmp/evil/gzx-append.sh map <<'E'\nhello\nE", "heredoc:writer_identity"),
        ("python3 /tmp/evil/gzx_cli.py save-cell <<'E'\nhello\nE", "heredoc:cli_identity"),
        ("cat x > $HOME/.gan-zhong-xue/map.jsonl", "expansion:unprovable"),
        ("cat x >$HOME/.gan-zhong-xue/map.jsonl", "expansion:unprovable"),
        ("cat x > ~/.GAN-ZHONG-XUE/map.jsonl", "redirect-to-protected"),
        ("cat x > /c/Users/foo/.gan-zhong-xue/map.jsonl", "redirect-to-protected"),
        ("~/.gan-zhong-xue/gzx-append.sh map <<'E'\nE\r\nrm ~/.gan-zhong-xue/map.md\nE",
         "heredoc:delim_repeats"),
        ("~/.gan-zhong-xue/gzx-append.sh map <<'E'\nE\r\r\nrm ~/.gan-zhong-xue/map.md\nE",
         "heredoc:delim_repeats"),
        ("cat x > ~/.gan-zhong-xue/map.jsonl", "redirect"),
        ("cp ~/.gan-zhong-xue/map.md /tmp/evil.md", "prefix:cp"),
        ("cat ~/.gan-zhong-xue/map.md > ~/.gan-zhong-xue/map.jsonl", "redirect"),
        ("sed -i s/a/b/ ~/.gan-zhong-xue/map.md", "sed"),
        ("cat ~/.gan-zhong-xue/map.md | tee ~/.gan-zhong-xue/map.jsonl", "prefix:tee"),
        ("cat ~/.gan-zhong-xue/map.md && python3 -c \"open('m','w')\"", "structure"),
        ("cat ~/.gan-zhong-xue/map.md; rm ~/.gan-zhong-xue/map.md", "structure"),
        ("cat ~/.gan-zhong-xue/map.md>~/.gan-zhong-xue/map.jsonl", "redirect"),
        ("cat ~/.gan-zhong-xue/map.md >| ~/.gan-zhong-xue/map.jsonl", "redirect"),
        ("cat ~/.gan-zhong-xue/map.md &> ~/.gan-zhong-xue/map.jsonl", "redirect"),
        ("cat ~/.gan-zhong-xue/map.md\nrm ~/.gan-zhong-xue/map.md", "structure"),
        ("cat ~/.gan-zhong-xue/map.md #c\nrm ~/.gan-zhong-xue/map.md", "structure"),
        ("cat ~/.gan-zhong-xue/map.md & python3 -c \"open('m','w')\"", "structure"),
        ("find ~/.gan-zhong-xue -name map.md -fprint ~/.gan-zhong-xue/map.md", "find"),
        ("find ~/.gan-zhong-xue -name map.md -delete", "find"),
        ("find ~/.gan-zhong-xue -name map.md -execdir rm {} \\;", "structure"),
        ("python3 -c \"open('~/.gan-zhong-xue/map.md','w')\"", "prefix:python3"),
        ("rm ~/.gan-zhong-xue/map.md", "prefix:rm"),
        ("perl -i -pe s/a/b/ ~/.gan-zhong-xue/map.md", "perl"),
        ("cat map.md | gzx-append map > ~/.gan-zhong-xue/map.md", "redirect"),
        ("cat ~/.gan-zhong-xue/map.md 'oops", "structure"),
        # ── #129 三族收敛（dsh #3055 §1/§6、#3061 §2、grok #3054/#3059、pi #3058 §B、
        #    Bugbot 4722010420/4722010427）——判据=「目标/命令名没有扩展算子」──
        # 动态目标族：$ 变量 / ${} / 命令替换 / 字面前缀+$ 漏拼 / 反引号 / 引号内变量
        ("cat x > $VAR", "expansion:unprovable"),
        ("cat x >$VAR", "expansion:unprovable"),
        ("cat x > ${HOME}/.gan-zhong-xue/map.jsonl", "expansion:unprovable"),
        ("cat x > /home/$USER/.gan-zhong-xue/map.jsonl", "expansion:unprovable"),
        ("cat x > $(echo ~/.gan-zhong-xue/map.jsonl)", "expansion:unprovable"),
        ("cat x > `echo ~/.gan-zhong-xue/map.jsonl`", "expansion:unprovable"),
        ("cat x > \"$OUT\"", "expansion:unprovable"),
        ("cat x > '$OUT'", "expansion:unprovable"),
        ("cat x > out$N.log", "expansion:unprovable"),
        # 通配符族（dsh #3061 §2：(a) 范围外逃逸 (b) 已进范围但目标按字面判）
        ("cat x > ~/.gan-zhong-*/map.jsonl", "expansion:unprovable"),
        ("cat ~/.gan-zhong-xue/map.md > ~/.gan-zhong-*/map.jsonl", "expansion:unprovable"),
        ("cat x > ~/.gan-zhong-x?e/map.jsonl", "expansion:unprovable"),
        ("cat x > ~/.gan-zhong-xu[e]/map.jsonl", "expansion:unprovable"),
        # 引号/转义拼接族（dsh #3055 §1）：分词引号消除后真实词元含标记 → 范围门命中
        ("cat x > ~/.gan-zhong-\"\"xue/map.jsonl", "redirect-to-protected"),
        ("cat x > ~/.gan-zhong-xu\\e/map.jsonl", "redirect-to-protected"),
        # 非 heredoc 假写入器 / 假 CLI（名字臂执行位，pi #3058 §B-2/B-3、grok #3059 A-3）
        ("python3 /tmp/evil/gzx_cli.py save-cell", "prefix:python3"),
        ("python3 -u /tmp/evil/gzx_cli.py save-cell", "prefix:python3"),
        ("/tmp/evil/gzx-append.sh map", "prefix:gzx-append.sh"),
        ("sh /tmp/evil/gzx-append.sh map", "prefix:sh"),
        ("bash -x /tmp/evil/gzx-append.sh map", "prefix:bash"),
        ("gzx-append.sh map", "prefix:gzx-append.sh"),
        ("bash gzx-append.sh map", "prefix:bash", "writer-not-on-path"),
        # heredoc 包装器 / 无引号定界符形态到不了身份层 → 剥离后进身份层被拒
        ("bash gzx-append.sh map <<E\nhello\nE", "prefix:bash", "writer-not-on-path"),
        ("bash /tmp/evil/gzx-append.sh map <<'E'\nhello\nE", "prefix:bash"),
        # 身份位扩展算子（dsh #3061 §2 身份位同病）
        ("$SCRIPT map", "expansion:unprovable"),
        ("\"$PY\" /tmp/evil/gzx_cli.py save-cell", "expansion:unprovable"),
        ("bash $SCRIPT map", "expansion:unprovable"),
        ("bash gzx_*.sh map", "expansion:unprovable"),
        ("bash gzx-app*.sh map", "expansion:unprovable"),
        ("python3 /tmp/gzx_cl?.py save-cell", "expansion:unprovable"),
    ]
    # 环境条件（dsh #3055 §3：裸名用例按「安装态是否存在」条件生效；跳过显式打印
    # 且留在分母里，不伪造 PASS——#128 C3「跳过不得表现为通过」的守卫侧同款）
    conds = {
        "writer-on-path": lambda: _is_writer_token(WRITER_MARK),
        "writer-not-on-path": lambda: not _is_writer_token(WRITER_MARK + ".sh"),
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
