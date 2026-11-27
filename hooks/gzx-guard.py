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
    # 臂③：basename 包含写入器或 CLI（即使正文不含数据目录字样也必须进入判定，防假写入器短路放行）
    if re.search(r"\b(?:gzx-append(?:\.sh)?|gzx_cli(?:\.py)?)\b", text, re.I):
        return True
    return False


def _is_protected(tok):
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
    # 裸名直调（从 PATH 解析）：若 PATH 找到则必须指向官方安装位；若 PATH 未配则允许（shell 自身会 command not found，非越权写）
    w = shutil.which(tok)
    if w:
        return _canon_path(w) == _writer_installed_path()
    return True


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
    if head in SHELL_WRAPPERS and any(_is_writer_token(t) for t in seg[1:]):
        return True
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


def _check_redirect(toks, i):
    """统一的重定向检查与连续目标消费（修复 $ 劈裂与粘连算子）。
    返回 (is_redir, decision_if_deny, next_i)。若非重定向，返回 (False, None, i)。"""
    t = toks[i]
    m = _REDIR_RE.match(t)
    if not m:
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

    # 连续消费拼接目标：遇到分隔符、换行、管道或下一个重定向算子时停止
    while next_i < len(toks):
        nxt = toks[next_i]
        if nxt in STRUCTURE_DENY_TOKENS or "\n" in nxt or nxt in PIPE_TOKENS or _REDIR_RE.match(nxt):
            break
        target_parts.append(nxt)
        next_i += 1

    full_target = "".join(target_parts)
    # 数字目标（fd 复制，如 2>&1 或 1>&2）
    if full_target.isdigit() and op in (">&", ">"):
        return True, None, next_i

    # 动态展开目标（包含 $、${、反引号等）
    has_dynamic = any("$" in p or "`" in p or "{" in p for p in target_parts)
    if has_dynamic:
        if _is_protected(full_target) or _in_scope(full_target) or any(_is_protected(p) for p in target_parts):
            return True, Decision(False, "redirect-to-protected:" + op), next_i

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
    """对一条 Bash 命令做守卫判定。返回 Decision。纯函数，便于测试。"""
    if not _in_scope(cmd):
        return Decision(True, "out-of-scope")

    m = _HEREDOC_WRITER_RE.match(cmd)
    if m:
        return _check_heredoc_first_line(cmd, m.group(3), m.group(4), is_cli=False)

    mc = _HEREDOC_CLI_RE.match(cmd)
    if mc:
        return _check_heredoc_first_line(cmd, mc.group(3), mc.group(4), is_cli=True)

    toks, err = _structure_tokens(cmd)
    if err:
        return Decision(False, "structure:unparseable")

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
        "gzx-append map",
        "cat payload | gzx-append candidates",
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
        "gzx-append pending-clear",
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
    ]
    denies = [
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
        # #128 A 组红线：臂③（正文不提数据目录的假写入器/假 CLI）+ $ 劈裂 + 大小写变体 + /c/ 路径
        ("/tmp/evil/gzx-append.sh map <<'E'\nhello\nE", "heredoc:writer_identity"),
        ("python3 /tmp/evil/gzx_cli.py save-cell <<'E'\nhello\nE", "heredoc:cli_identity"),
        ("cat x > $HOME/.gan-zhong-xue/map.jsonl", "redirect-to-protected"),
        ("cat x >$HOME/.gan-zhong-xue/map.jsonl", "redirect-to-protected"),
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
    ]
    fails = []
    for c in allows:
        d = classify(c)
        if not d.allow:
            fails.append("ALLOW case denied: %r -> %s" % (c, d.rule))
    for c, rule in denies:
        d = classify(c)
        if d.allow:
            fails.append("DENY case allowed: %r" % c)
        elif rule and not d.rule.startswith(rule):
            fails.append("DENY case %r hit %s, expected %s*" % (c, d.rule, rule))
    for f in fails:
        print("FAIL " + f)
    print("selftest: %d allow + %d deny cases, %d failures"
          % (len(allows), len(denies), len(fails)))
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
