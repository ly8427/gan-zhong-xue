#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gzx-capture.py — 提交静默捕获 hook（v1.2.0，支持 root-commit / detached HEAD / -C 与 cd 上下文）。

定位：零打扰。PostToolUse 命中 git commit 时，不向用户说任何话，只往
~/.gan-zhong-xue/candidates.jsonl 追加一条 {sha, add, del, event, seq, ts}。
"""

import json
import os
import re
import shlex
import subprocess
import sys
import time

GZX_DIR = os.environ.get("GZX_DIR", os.path.expanduser("~/.gan-zhong-xue"))
CANDIDATES = os.path.join(GZX_DIR, "candidates.jsonl")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "skills", "gan-zhong-xue"))
try:
    from gzx_lock_append import lock_append_commit_unique, _normalize_dir
except ImportError:
    lock_append_commit_unique = None
    _normalize_dir = None

# git commit 成功输出标记：支持 [main abc1234]、[main (root-commit) abc1234]、
# [detached HEAD abc1234] 等形态。
_COMMIT_OK_RE = re.compile(r"\[[^\]]+\s+([0-9a-f]{7,40})\]")


def _core_path():
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.join(here, "..", "skills", "gan-zhong-xue", "gzx_lock_append.py")
    return cand if os.path.exists(cand) else os.path.join(GZX_DIR, "gzx_lock_append.py")


def _extract_committed_shas(text):
    """提取 commit 输出中所有成功标记里的 sha 候选列表。"""
    return [m.group(1) for m in _COMMIT_OK_RE.finditer(text or "")]


def _extract_committed_sha(text):
    """兼容旧接口：返回首个成功标记里的 sha；无标记 → None。"""
    shas = _extract_committed_shas(text)
    return shas[0] if shas else None


_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_CMD_SEPARATORS = {"&&", "||", "|", "|&", ";", ";;", "&", "\n", "(", ")", "{", "}"}
_SHELL_RESERVED = {"if", "then", "else", "elif", "fi", "for", "while", "until", "do",
                   "done", "case", "esac", "in", "function", "time", "!", "{", "}",
                   "[[", "]]", "coproc", "select"}
_REDIRECT_OPS = {">", ">>", "<", ">|", "<&", ">&", "&>", "&>>", "<>"}
_ALL_BOUNDARIES = _CMD_SEPARATORS | _SHELL_RESERVED | _REDIRECT_OPS
_PUNCT_SET = set("();<>|&\n")
# 全标点粘连词元的拆分表：已知复合操作符最长匹配，其余拆单字符。
# `);` 粘连是 punctuation_chars 把连续标点并成单 token 的第三次现形
# （grok #3054/#3059、dsh #3055 §5-4）：`( cd x && git commit ); git commit`
# 的外层 commit 因 `);` 不在分隔符集而漏捕获。
_GLUED_COMPOUND = ("&&", "||", "|&", "&>>", "&>", ">>", ">|", "<>", ">&",
                   "<<<", "<<", ";;")


def _split_glued_punct(tok):
    """全标点词元 → 复合操作符最长匹配 + 单字符；含非标点字符的词元原样返回。"""
    if tok in _ALL_BOUNDARIES:
        return [tok]
    if not tok or any(c not in _PUNCT_SET for c in tok):
        return [tok]
    out = []
    i = 0
    while i < len(tok):
        for op in _GLUED_COMPOUND:
            if tok.startswith(op, i):
                out.append(op)
                i += len(op)
                break
        else:
            out.append(tok[i])
            i += 1
    return out


def _lex(cmd):
    """shell 切词（posix：引号内不外泄成结构符），并接回被切开的冒号。"""
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars="();<>|&\n")
        lex.whitespace_split = False
        lex.whitespace = " \t\r"
        lex.commenters = ""
        lex.wordchars += "$"
        raw = list(lex)
    except ValueError:
        return None
    merged = []
    i = 0
    n = len(raw)
    while i < n:
        tok = raw[i]
        if (tok == ":" and merged and merged[-1] not in _ALL_BOUNDARIES
                and i + 1 < n and raw[i + 1] not in _ALL_BOUNDARIES):
            merged[-1] = merged[-1] + ":" + raw[i + 1]
            i += 2
            continue
        merged.append(tok)
        i += 1
    final = []
    for tok in merged:
        final.extend(_split_glued_punct(tok))
    return final


def _iter_simple_commands_with_seps(cmd):
    """产出 (subshell_depth, sep_before, seg)，用于跟踪 cd 与子 shell 边界。
    注意：( ) 建立子 shell 隔离；{ } 是当前 shell 复合命令分组，不产生子 shell。"""
    tokens = _lex(cmd or "")
    if tokens is None:
        return
    seg = []
    subshell_depth = 0
    sep_before = ""
    for tok in tokens:
        if tok in _REDIRECT_OPS:
            continue
        if tok in _CMD_SEPARATORS or tok in _SHELL_RESERVED:
            if seg:
                yield subshell_depth, sep_before, seg
                seg = []
            if tok == "(":
                subshell_depth += 1
            elif tok == ")":
                subshell_depth = max(0, subshell_depth - 1)
            sep_before = tok
            continue
        seg.append(tok)
    if seg:
        yield subshell_depth, sep_before, seg


def _iter_simple_commands(cmd):
    """把一条 shell 命令串切成「简单命令」的词元列表（生成器）。"""
    for _, _, seg in _iter_simple_commands_with_seps(cmd):
        yield seg


_GIT_GLOBAL_WITH_ARG = {"-C", "-c"}
_GIT_GLOBAL_OPTIONAL_VAL = {"--exec-path", "--git-dir", "--work-tree", "--namespace",
                            "--config-env"}
_GIT_GLOBAL_FLAGS = {"-v", "--version", "-h", "--help", "-p", "--paginate", "-P",
                     "--no-pager", "--no-replace-objects", "--no-lazy-fetch",
                     "--no-optional-locks", "--no-advice", "--bare"}


def _skip_git_globals(seg, i):
    n = len(seg)
    while i < n:
        w = seg[i]
        if w in _GIT_GLOBAL_WITH_ARG:
            i += 2
        elif w.split("=", 1)[0] in _GIT_GLOBAL_OPTIONAL_VAL:
            i += 2 if "=" not in w else 1
        elif w in _GIT_GLOBAL_FLAGS:
            i += 1
        else:
            break
    return i


def _strip_env_prefix(seg):
    """跳过前置 VAR=val 和 env 包装，返回实际程序词下标（或 >= len(seg)）。"""
    i = 0
    while i < len(seg) and _ENV_ASSIGN_RE.match(seg[i]):
        i += 1
    if i < len(seg) and seg[i].rsplit("/", 1)[-1] == "env":
        i += 1
        n = len(seg)
        while i < n:
            w = seg[i]
            if w == "--":
                i += 1
                break
            if w in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"):
                i += 2
            elif w in ("-i", "--ignore-environment", "-0", "--null", "-v", "--debug",
                       "-", "--list-signal-handling",
                       "--block-signal", "--default-signal", "--ignore-signal"):
                i += 1
            elif w.startswith("--") and "=" in w:
                i += 1
            elif _ENV_ASSIGN_RE.match(w):
                i += 1
            else:
                break
    return i


def _is_git_commit_segment(seg):
    """一个简单命令段是否为 `git commit`。"""
    i = _strip_env_prefix(seg)
    if i >= len(seg):
        return False
    git_prog = seg[i].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    if git_prog not in ("git", "git.exe"):
        return False
    i = _skip_git_globals(seg, i + 1)
    return i < len(seg) and seg[i] == "commit"


def is_commit_command(cmd):
    """命令串里是否执行了 git commit。"""
    return any(_is_git_commit_segment(seg) for seg in _iter_simple_commands(cmd or ""))


def _resolve_dir(base, target):
    p = os.path.expanduser(target)
    if _normalize_dir is not None:
        p = _normalize_dir(p)
    if not os.path.isabs(p) and base:
        p = os.path.join(base, p)
    return os.path.normpath(p)


def _extract_git_contexts(cmd, initial_cwd):
    """追踪命令行中的 cd / env -C / git -C / --git-dir / --work-tree，
    返回每个 git commit 命中的上下文列表 [(cwd, git_dir, work_tree), ...]。
    支持当前 shell 大括号分组 { cd repo; git commit; } 与子 shell 栈隔离 ( cd repo && git commit )。
    已知残余漏报：变量目标 cd（目标含 $）与 cd - 暂不追踪，在 docs/mechanism.md 如实记录。"""
    cur_cwd = initial_cwd
    if cur_cwd and _normalize_dir is not None:
        cur_cwd = _normalize_dir(cur_cwd)
    contexts = []
    cwd_stack = []
    curr_depth = 0
    for depth, sep_before, seg in _iter_simple_commands_with_seps(cmd or ""):
        if depth > curr_depth:
            for _ in range(depth - curr_depth):
                cwd_stack.append(cur_cwd)
            curr_depth = depth
        elif depth < curr_depth:
            for _ in range(curr_depth - depth):
                if cwd_stack:
                    cur_cwd = cwd_stack.pop()
            curr_depth = depth

        # 管道右侧的 cd 属于子进程不影响外层；其余（包括顶层、花括号分组、子 shell 内部）均更新当前上下文
        if sep_before not in ("|", "|&") and seg and seg[0] == "cd":
            args = [a for a in seg[1:] if a != "--" and not a.startswith("-")]
            if args and args[0] != "-" and "$" not in args[0]:
                cur_cwd = _resolve_dir(cur_cwd, args[0])
            continue
        if not _is_git_commit_segment(seg):
            continue
        eff_cwd = cur_cwd
        # 也支持 env -C <dir>
        j = 0
        while j < len(seg) and _ENV_ASSIGN_RE.match(seg[j]):
            j += 1
        if j < len(seg) and seg[j].rsplit("/", 1)[-1] == "env":
            k = j + 1
            while k < len(seg):
                w = seg[k]
                if w == "--":
                    break
                if w in ("-C", "--chdir") and k + 1 < len(seg):
                    eff_cwd = _resolve_dir(eff_cwd, seg[k + 1])
                    k += 2
                elif (w.startswith("--chdir=") or w.startswith("-C=")):
                    eff_cwd = _resolve_dir(eff_cwd, w.split("=", 1)[1])
                    k += 1
                elif w in ("-u", "--unset", "-S", "--split-string"):
                    k += 2
                elif w in ("-i", "--ignore-environment", "-0", "--null", "-v", "--debug",
                           "-", "--list-signal-handling",
                           "--block-signal", "--default-signal", "--ignore-signal") or (
                        w.startswith("--") and "=" in w) or _ENV_ASSIGN_RE.match(w):
                    k += 1
                else:
                    break
        git_idx = _strip_env_prefix(seg) + 1
        git_dir = None
        work_tree = None
        while git_idx < len(seg):
            w = seg[git_idx]
            if w == "-C" and git_idx + 1 < len(seg):
                eff_cwd = _resolve_dir(eff_cwd, seg[git_idx + 1])
                git_idx += 2
            elif w == "-c":
                git_idx += 2
            elif w == "--git-dir" and git_idx + 1 < len(seg):
                git_dir = _resolve_dir(eff_cwd, seg[git_idx + 1])
                git_idx += 2
            elif w.startswith("--git-dir="):
                git_dir = _resolve_dir(eff_cwd, w.split("=", 1)[1])
                git_idx += 1
            elif w == "--work-tree" and git_idx + 1 < len(seg):
                work_tree = _resolve_dir(eff_cwd, seg[git_idx + 1])
                git_idx += 2
            elif w.startswith("--work-tree="):
                work_tree = _resolve_dir(eff_cwd, w.split("=", 1)[1])
                git_idx += 1
            elif w.split("=", 1)[0] in _GIT_GLOBAL_OPTIONAL_VAL:
                git_idx += 2 if "=" not in w else 1
            elif w in _GIT_GLOBAL_FLAGS:
                git_idx += 1
            else:
                break
        contexts.append((eff_cwd, git_dir, work_tree))
    return contexts


def _already_captured(sha):
    try:
        with open(CANDIDATES, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("sha") == sha and row.get("event") == "commit":
                    return True
    except OSError:
        pass
    return False


def _dbg(msg):
    if os.environ.get("RG_CAPTURE_DEBUG"):
        try:
            with open(os.path.join(GZX_DIR, "capture-trace.txt"), "a",
                      encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass


def main():
    try:
        raw = sys.stdin.read()
        event = json.loads(raw)
        ti = event.get("tool_input", {}) or {}
        cmd = (ti.get("command") or "").strip()

        if not is_commit_command(cmd):
            _dbg("skip: not git commit")
            return
        resp_text = json.dumps(event.get("tool_response", {}), ensure_ascii=False)

        sha_cands = _extract_committed_shas(resp_text)
        _dbg("markers=%r" % sha_cands)
        if not sha_cands:
            _dbg("skip: no marker")
            return

        base_cwd = event.get("cwd") or None
        contexts = _extract_git_contexts(cmd, base_cwd) or [(base_cwd, None, None)]

        matched = None
        for eff_cwd, git_dir, work_tree in reversed(contexts):
            git_base = ["git"]
            if git_dir:
                git_base.extend(["--git-dir", git_dir])
            if work_tree:
                git_base.extend(["--work-tree", work_tree])
            head = subprocess.run(git_base + ["rev-parse", "HEAD"], cwd=eff_cwd,
                                  capture_output=True, timeout=15)
            if head.returncode != 0:
                continue
            sha = head.stdout.decode().strip()
            if any(sha.startswith(s) or s.startswith(sha[:len(s)]) for s in sha_cands):
                matched = (sha, git_base, eff_cwd)
                break

        if matched is None:
            _dbg("skip: no matching HEAD across contexts")
            return

        sha, git_base, eff_cwd = matched
        _dbg("dedup-pass")
        if _already_captured(sha):
            _dbg("skip: dup")
            return

        numstat = subprocess.run(
            git_base + ["show", "--numstat", "--format=", "HEAD"], cwd=eff_cwd,
            capture_output=True, timeout=15)
        add = ddel = 0
        for ln in numstat.stdout.decode(errors="replace").splitlines():
            parts = ln.split("\t")
            if len(parts) >= 2:
                add += int(parts[0]) if parts[0].isdigit() else 0
                ddel += int(parts[1]) if parts[1].isdigit() else 0

        row = json.dumps({
            "sha": sha,
            "add": add,
            "del": ddel,
            "event": "commit",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, ensure_ascii=False)

        data = (row + "\n").encode()
        _dbg("writer unique=%s" % (lock_append_commit_unique is not None))
        if lock_append_commit_unique is not None:
            lock_append_commit_unique(GZX_DIR, data)
        else:
            core = _core_path()
            subprocess.run([sys.executable, core, "candidates"], input=data,
                           capture_output=True, timeout=20)
    except Exception:
        if os.environ.get("RG_CAPTURE_DEBUG"):
            try:
                import traceback as _tb
                _t = _tb.format_exc()
                sys.stderr.write("[capture-debug] exception:\n%s\n" % _t)
                with open(os.path.join(GZX_DIR, "capture-trace.txt"), "a",
                          encoding="utf-8") as _f:
                    _f.write("EXCEPTION %s\n" % _t)
            except Exception:
                pass
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
