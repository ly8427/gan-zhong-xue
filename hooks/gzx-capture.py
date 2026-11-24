#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gzx-capture.py — 提交静默捕获 hook（v1.1.2，board thread #80 C' 定案 + GPT
PR #4 审查 P1-5 收口）。

定位：零打扰。PostToolUse 命中 git commit 时，不向用户说任何话，只往
~/.gan-zhong-xue/candidates.jsonl 追加一条 {sha, add, del, event, seq, ts}。
提醒只在下次 skill 被唤起时由第 -1 步兑现（"上次有 N 个提交没挖"）。

隐私（D1）：不写文件路径、不写仓库名——只有 sha 与增删行数。

v1.1.0（GPT PR #4 审查收口）：
  * P1-4：去重检查/序号计算/追加移入写入器核心的同一临界区
    （lock_append_commit_unique）——并发不再重复登记或序号错乱。
  * P1-5：commit 命令失败时不得捕获旧 HEAD——git commit 失败的方式远不止
    "nothing to commit"（钩子拒绝、合并冲突、消息编辑器中止…），一律以
    「工具输出中是否存在 git commit 的成功标记 `[分支名 <sha>]`」为准：
    提取不到该标记 → 不捕获；提取到 → 再与 rev-parse HEAD 复核一致才落账。
  * 残余如实：输出被宿主截断导致成功标记丢失 → 漏捕获（漏报可由用户在
    唤起时口头补挖，不产生错误记录——漏报优于误报）。

协议：stdin = 宿主 PostToolUse JSON；任何异常静默退出 0（捕获是旁路，绝不
阻塞工作流）。
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
    from gzx_lock_append import lock_append_commit_unique  # 写入器核心（唯一实现）
except ImportError:
    lock_append_commit_unique = None

# git commit 成功输出的标记：[分支名 <sha>]（GPT round-18 P1-5）
_COMMIT_OK_RE = re.compile(r"\[[\w./-]+ +([0-9a-f]{7,40})\]")


def _core_path():
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.join(here, "..", "skills", "gan-zhong-xue", "gzx_lock_append.py")
    return cand if os.path.exists(cand) else os.path.join(GZX_DIR, "gzx_lock_append.py")


def _extract_committed_sha(text):
    """从 commit 命令输出提取成功标记里的 sha；无标记 = 提交未成功 → None。"""
    m = _COMMIT_OK_RE.search(text or "")
    return m.group(1) if m else None


# 环境前缀赋值：NAME=value（出现在命令首部时是设置环境变量，不是要执行的命令）
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# 结构分隔符：命令链/管道/后台/换行/子 shell——它们切出「一条简单命令」的边界
_CMD_SEPARATORS = {"&&", "||", "|", "|&", ";", ";;", "&", "\n", "(", ")", "{", "}"}
# shell 保留字：它们自己不是要执行的程序，出现即开启一个新的命令段
#   （`if true; then git commit; fi`——then 之后的 git 才是命令首词）
_SHELL_RESERVED = {"if", "then", "else", "elif", "fi", "for", "while", "until", "do",
                   "done", "case", "esac", "in", "function", "time", "!", "{", "}",
                   "[[", "]]", "coproc", "select"}
# 重定向算子（下一词是目标文件——与本层无关，算子本身丢弃，目标词留着无害）
_REDIRECT_OPS = {">", ">>", "<", ">|", "<&", ">&", "&>", "&>>", "<>"}
# 全部边界词：分隔符 ∪ 保留字 ∪ 重定向算子——「冒号接回」不得跨越它们
_ALL_BOUNDARIES = _CMD_SEPARATORS | _SHELL_RESERVED | _REDIRECT_OPS


def _lex(cmd):
    """shell 切词（posix：引号内不外泄成结构符），并把 shlex 切开的冒号接回去。

    shlex 一旦启用 punctuation_chars，**任何冒号都被切成独立词元**（Python 文档
    明载：为支持 Windows 路径而如此设计）——实测 `git -C C:/r commit` 被切成
    ['git','-C','C',':','/r','commit']，`-C` 把 'C' 当成了路径值。
    后处理：一个独立的 `:` 词元，若它**不是段首**（前一词存在且不是边界符）且
    **后一词存在且不是边界符**，视为同一个词被切碎 ⇒ 前后接回。
    段首的 `:`（shell 的 no-op 内建命令，如 `: && git commit`）不接回。
    v1.1.2（Bugbot 4670188696）：`$` 不在 shlex 默认词字符表里，**永远被切成独立
    词元**——`PATH=$PATH:/x` 被切成 ['PATH=','$','PATH:/x']，环境赋值跳过匹配不到
    ⇒ 漏掉真提交。修法=把 `$` 加进 wordchars（`$VAR`、`$$`、`$(…` 保持完整词元；
    `$(` 的 `(` 仍是边界符，命令替换形态不受影响）。
    不可解析（引号未闭合等）→ 返回 None（漏报优于误报，与 P1-5 同向）。"""
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars="();<>|&\n")
        lex.whitespace_split = False
        lex.whitespace = " \t\r"
        lex.commenters = ""
        lex.wordchars += "$"
        raw = list(lex)
    except ValueError:
        return None
    # 冒号接回（见上）
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
    return merged


def _iter_simple_commands(cmd):
    """把一条 shell 命令串切成「简单命令」的词元列表（生成器）。

    v1.1.2（GPT 外审十轮 P2-1）：捕获判定不能只看整串的开头——`cd X && git commit`、
    `/usr/bin/git commit`、`env A=b git commit` 都是真实提交却会被旧判定漏掉。
    用 _lex 切词，按结构分隔符与 shell 保留字切段；不可解析 → 不产出任何命令。

    已知不识别的形态（如实登记，漏报方向）：`sudo git commit`、`docker exec … git commit`
    ——包装器后面的命令，cwd 与输出归属不可信；`xargs`/`find -exec` 间接提交。
    识别为「是 commit 动作」但不会产生提交的形态（如 `git commit --help`）不在本层
    排除：是否真有提交由后道防线把关（输出须有成功标记且与 HEAD 复核一致，P1-5）。"""
    tokens = _lex(cmd or "")
    if tokens is None:
        return
    seg = []
    for tok in tokens:
        if tok in _REDIRECT_OPS:
            continue                           # 重定向符本身：跳过（目标词留在段内无害）
        if tok in _CMD_SEPARATORS or tok in _SHELL_RESERVED:
            if seg:
                yield seg
            seg = []
            continue
        seg.append(tok)
    if seg:
        yield seg


# git 全局选项（权威=`git --help` 用法行；写在子命令 <command> 之前）。
#   分开写、下一个词是值：-C <path>、-c <name>=<value>
#   分开写或内联值：      --exec-path[=<path>]、--git-dir=<path>、--work-tree=<path>、
#                        --namespace=<name>、--config-env=<name>=<envvar>
#   无值旗标：            -v --version -h --help -p --paginate -P --no-pager
#                        --no-replace-objects --no-lazy-fetch --no-optional-locks
#                        --no-advice --bare
_GIT_GLOBAL_WITH_ARG = {"-C", "-c"}
_GIT_GLOBAL_OPTIONAL_VAL = {"--exec-path", "--git-dir", "--work-tree", "--namespace",
                            "--config-env"}
_GIT_GLOBAL_FLAGS = {"-v", "--version", "-h", "--help", "-p", "--paginate", "-P",
                     "--no-pager", "--no-replace-objects", "--no-lazy-fetch",
                     "--no-optional-locks", "--no-advice", "--bare"}


def _skip_git_globals(seg, i):
    """从 seg[i] 起跳过 git 的全局选项，返回子命令词的下标（到头则返回 len）。

    判据全部对齐 `git --help` 用法行（v1.1.2 GPT 外审十轮 P2-1）：只认清单内的
    全局选项，清单外的词一律视为子命令——不做「像旗标就跳过」的模糊判断。"""
    n = len(seg)
    while i < n:
        w = seg[i]
        if w in _GIT_GLOBAL_WITH_ARG:
            i += 2                              # 短选项，值是下一个词
        elif w.split("=", 1)[0] in _GIT_GLOBAL_OPTIONAL_VAL:
            # 值可内联也可分开：内联（词里带 '='）则本词自足；分开写则值是下一个词
            i += 2 if "=" not in w else 1
        elif w in _GIT_GLOBAL_FLAGS:
            i += 1
        else:
            break                               # 不在清单内 ⇒ 这就是子命令
    return i


def _is_git_commit_segment(seg):
    """一个简单命令段是否为 `git commit`（允许 env 赋值前缀、env 命令包装、git 全局选项）。"""
    i = 0
    # 跳过前置环境赋值：FOO=bar git commit …
    while i < len(seg) and _ENV_ASSIGN_RE.match(seg[i]):
        i += 1
    # env 包装：env [选项/VAR=x …] git commit …
    # v1.1.2（GPT 外审十一轮 P2）：env 的选项分「无参」与「带参」两类，旧循环把
    # 所有 - 开头的词都当无参跳过 ⇒ `env -u HTTP_PROXY git commit` 在 -u 后停在
    # 变量名上（把变量名当程序名）⇒ 真提交漏捕获。带参选项的值是下一个词。
    #   无参：-i --ignore-environment -0 --null -v --verbose
    #         --block-signal --default-signal --ignore-signal（=值内联或省略）
    #   带参：-u/--unset、-C/--chdir、-S/--split-string（下一个词是值）
    #   内联值（--unset=X 等）：一个词自足
    #   `--`：其后全是位置参数（程序+参数），env 部分结束
    if i < len(seg) and seg[i].rsplit("/", 1)[-1] == "env":
        i += 1
        n = len(seg)
        while i < n:
            w = seg[i]
            if w == "--":
                i += 1
                break
            if w in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"):
                i += 2                          # 带值选项：值是下一个词
            elif w in ("-i", "--ignore-environment", "-0", "--null", "-v", "--debug",
                       "-", "--list-signal-handling",
                       "--block-signal", "--default-signal", "--ignore-signal"):
                # v1.1.2（Bugbot 4674364435）：-v 的长选项是 --debug 不是 --verbose
                # （对齐 `env --help`）；`-` 单独出现等价于 -i（env --help 明载）；
                # --list-signal-handling 是无参旗标（此前漏登记）。
                i += 1                          # 无参旗标
            elif w.startswith("--") and "=" in w:
                i += 1                          # 内联值：--unset=X 等
            elif _ENV_ASSIGN_RE.match(w):
                i += 1                          # env 的 VAR=value 参数
            else:
                break                           # 不再是 env 的参数 ⇒ 这是程序名
    if i >= len(seg):
        return False
    # 程序名识别（v1.1.2 Bugbot 4670188716）：basename 恰为 git 或 git.exe
    # （Windows 可执行文件名带 .exe；大小写不敏感——Windows 文件名不区分大小写）。
    # 旧判定 startswith("git") 能识别 git.exe，本层不得回退。
    git_prog = seg[i].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    if git_prog not in ("git", "git.exe"):
        return False
    i = _skip_git_globals(seg, i + 1)
    return i < len(seg) and seg[i] == "commit"


def is_commit_command(cmd):
    """命令串里是否**执行**了 git commit（v1.1.2 GPT 外审十轮 P2-1）。"""
    return any(_is_git_commit_segment(seg) for seg in _iter_simple_commands(cmd or ""))


def _already_captured(sha):
    """D5 去重键 (sha, event) 的读面快查（真正的原子去重在写入器核心锁内）。"""
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

        # 只捕获真正执行了 git commit 的命令（其他 Bash 调用无关）。
        # v1.1.2（GPT 外审十轮 P2-1）：按简单命令段识别——支持 `cd X && git commit`、
        # `/usr/bin/git commit`、`env A=b git commit`；引号里的 commit、非 git 命令的
        # commit 参数不算（见 is_commit_command）。
        if not is_commit_command(cmd):
            _dbg("skip: not git commit")
            return
        resp_text = json.dumps(event.get("tool_response", {}), ensure_ascii=False)

        # P1-5：提交必须真的成功——输出里要有 [分支 <sha>] 成功标记
        sha_out = _extract_committed_sha(resp_text)
        _dbg("marker=%r" % sha_out)
        if not sha_out:
            _dbg("skip: no marker")
            return

        cwd = event.get("cwd") or None
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd,
                              capture_output=True, timeout=15)
        if head.returncode != 0:
            _dbg("skip: rev-parse")
            return
        sha = head.stdout.decode().strip()
        # 复核：输出标记的 sha 与当前 HEAD 一致（防旧 HEAD 误捕获）
        if not (sha.startswith(sha_out) or sha_out.startswith(sha[:len(sha_out)])):
            return
        _dbg("dedup-pass")
        if _already_captured(sha):
            _dbg("skip: dup")
            return

        numstat = subprocess.run(
            ["git", "show", "--numstat", "--format=", "HEAD"], cwd=cwd,
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
        }, ensure_ascii=False)   # seq 由写入器核心在锁内现算(P1-4)

        data = (row + "\n").encode()
        _dbg("writer unique=%s" % (lock_append_commit_unique is not None))
        if lock_append_commit_unique is not None:
            lock_append_commit_unique(GZX_DIR, data)     # 原子去重+序号+追加
        else:
            core = _core_path()
            subprocess.run([sys.executable, core, "candidates"], input=data,
                           capture_output=True, timeout=20)
    except Exception as _e:
        # GPT 外审(PR#4 五轮): 默认路径必须真静默(零 stderr、不写 trace 文件)
        # ——traceback 可能含本机绝对路径,与最小化记录原则不一致;诊断只在
        # RG_CAPTURE_DEBUG 显式开启时输出(与 _dbg 同一开关)。
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
