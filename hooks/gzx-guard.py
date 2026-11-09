#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gzx-guard.py — gan-zhong-xue 数据面兜底 hook（v1.0.9，board thread #80 D3/D4 定案）。

定位：减速带 + 审计，不是安全边界（威胁模型与 mcp-review-board form_b nonce 同口径）。
真不变量在写入器 gzx-append.sh；本 hook 只兜"绕过写入器"的那条路。

判据（thread #80 #810→#831 收敛链；deny = 写能力指向受保护路径，非元字符黑名单）：
  范围条件：命令串引用了受保护目录（含 "gan-zhong-xue" 字样）才进入判定；
            未引用 → 直接放行、不审计（防审计刷噪声）。已知残余风险（#810 如实记录）：
            `cd ~ && rm -r .g*` 这类不提名字的写法拦不住——减速带定位，不是安全边界。
  gzx-append 写入器调用 → 放行（写入器是唯一 blessed 写路径）。
  结构层（未引号区间，shlex punctuation_chars，#829 §2 开关 + #830 §1 两行修正）：
    deny tokens: ';' '&&' '||' '&' '$(' '`' '<(' '>(' 换行；不可解析 → deny（#822 §1）。
    '|' 与 '|&' 不进拒集 → 交"两端皆只读白名单"层处置（#825 §1-2 / #831）。
      注：punctuation 词法器下无空格管道同样切出独立 '|'（dsh #829 §2 一手实测），
      故 `grep x map.md|head` 与有空格版本同走两端层——两写法语义全等，无安全差。
      （#829 §5 把无空格版本列 deny 与其自身 §2 实测矛盾；按机制层裁决，CHANGELOG 记偏离。）
    '>' '>>' '>|' '>&' '&>' '&>>' '<>' → 重定向层：看下一个 token 是否受保护路径
      （#831：目标判定必须看 > 的下一个 token；'2>&1' 切成 '2','>&','1'，数字目标 = fd 复制放行）。
  前缀层（每个管道段的首命令）：只读白名单 cat|grep|ls|wc|head|tail|stat|sha256sum|
    md5sum|find —— 裸 `python3 -c open('w')` 的唯一拦截点（#825 §1-3 保留前缀层）。
    bash/sh/source 包装调用仅在目标确为写入器脚本时放行。
    非白名单段首 → deny（D3：引用受保护路径时假阳性成本 > 假阴性成本；
    假阴性有写入器兜底，假阳性训练绕行习惯）。
  参数层（shlex.split 标准，与结构层分离——#829 §3(a)：两个 lexer 各管一件事）：
    find 排除集九项: -exec -execdir -ok -okdir -delete -fprint -fprint0 -fprintf -fls。
    sed|perl 前缀下的 -i（仅 sed/perl 段匹配，#817：grep -i 不误伤）。
  管道两端层：每段首命令 ∈ 只读白名单 ∪ {写入器调用}。
  Agent 提示词/Write|Edit 参数：提及受保护目录即 deny（#811② 原口径——子 agent 自带工具，
  参数面不做命令级解析，保守处置）。

审计（D1）：deny 与 allow（范围内）都落一行 audit.jsonl（经写入器，单写者纪律）；
  审计行只含 {ts, tool, decision, rule}——绝不落命令/参数原文（D1：审计行不落命令原文）。
  审计文件首行保证为 hook_alive（dsh #811①：活体证明，"没触发"与"没跑"必须可区分）。

协议：stdin = 宿主 hook JSON（tool_name / tool_input）；deny = exit 2 + stderr 原因；
      allow = exit 0。宿主事件不可解析 → fail-open（减速带定位：协议异常不冻住用户会话）。
      --selftest 跑 allow/deny 双侧用例矩阵（D6 安装验收的机械部分，双侧都要验——#815 §3）。
"""

import json
import os
import shlex
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "skills", "gan-zhong-xue"))
try:
    from gzx_lock_append import lock_append   # 写入器不变量核心（唯一实现）
except ImportError:
    lock_append = None

GZX_DIR = os.environ.get("GZX_DIR", os.path.expanduser("~/.gan-zhong-xue"))
PROTECTED_MARK = "gan-zhong-xue"          # 受保护目录的引用判据（字面，任何路径写法都含它）
WRITER_MARK = "gzx-append"                # 写入器调用判据

# 只读前缀白名单（#819 §1；find 的写开关在参数层单独排除）
READONLY_PREFIX = {
    "cat", "grep", "ls", "wc", "head", "tail", "stat", "sha256sum", "md5sum", "find",
}
# find 排除集九项（#825 §2 四个 -f* + #831 补 -exec 族另三个 → 与 #825 §2 合并为九项）
FIND_DENY_FLAGS = {
    "-exec", "-execdir", "-ok", "-okdir", "-delete",
    "-fprint", "-fprint0", "-fprintf", "-fls",
}
# 结构层拒集（token 相等判；'|' '|&' 不在此——走两端白名单层；裸 '&' 是命令分隔符，#831 §3）
STRUCTURE_DENY_TOKENS = {";", "&&", "||", "&", "$(", "`", "<(", ">(", "\n"}
# 重定向算子（下一 token = 目标，交目标路径判定；#830 §3 + #831 §2 全集）
REDIRECT_TOKENS = {">", ">>", ">|", ">&", "&>", "&>>", "<>"}
# 双向管道（与 '|' 同层处置，#831 §1-3）
PIPE_TOKENS = {"|", "|&"}
# 可包装写入器的 shell 解释器
SHELL_WRAPPERS = {"bash", "sh", "source", "."}


class Decision:
    def __init__(self, allow, rule):
        self.allow = allow
        self.rule = rule


def _structure_tokens(cmd):
    """结构层分词（#829 §2 开关 + #830 §1 两行修正）。
    返回 (tokens, None) 或 (None, 错误信息)。引号内算子不外泄；未闭合引号 = 不可解析。"""
    lex = shlex.shlex(cmd, posix=True, punctuation_chars="();<>|&`$\n")
    lex.whitespace_split = False
    lex.whitespace = " \t\r"      # ① \n 从 whitespace 摘出，交给 punctuation（#830）
    lex.commenters = ""           # ② 关注释，防注释行尾吞掉换行（#830）
    try:
        return list(lex), None
    except ValueError:
        return None, "unparseable"


def _is_writer_token(tok):
    base = tok.rsplit("/", 1)[-1]
    return base == WRITER_MARK or base == WRITER_MARK + ".sh"


def _seg_head_is_allowed(seg):
    head = seg[0].rsplit("/", 1)[-1]
    if head in READONLY_PREFIX:
        return True
    if _is_writer_token(seg[0]):
        return True
    if head in SHELL_WRAPPERS and any(_is_writer_token(t) for t in seg[1:]):
        return True   # bash/sh/source 包装写入器脚本；bash -c payload 不含独立写入器 token → 照 deny
    return False


def classify(cmd):
    """对一条 Bash 命令做守卫判定。返回 Decision。纯函数，便于测试。"""
    # 范围条件：不引用受保护目录 → 放行（D3：保守 deny 以引用受保护路径为条件）
    if PROTECTED_MARK not in cmd:
        return Decision(True, "out-of-scope")

    toks, err = _structure_tokens(cmd)
    if err:
        return Decision(False, "structure:unparseable")

    # 结构层扫描 + 管道分段
    segments = [[]]          # 按管道切分的命令段（token 列表）
    i = 0
    n = len(toks)
    while i < n:
        t = toks[i]
        if t in STRUCTURE_DENY_TOKENS:
            return Decision(False, "structure:" + t.replace("\n", "\\n"))
        if t in PIPE_TOKENS:
            segments.append([])
            i += 1
            continue
        if t in REDIRECT_TOKENS:
            # 重定向层：目标判定看下一个 token（#831 形状一致性）
            if i + 1 >= n:
                return Decision(False, "redirect:missing-target")
            tgt = toks[i + 1]
            if not tgt.isdigit() and _is_protected(tgt):
                return Decision(False, "redirect-to-protected:" + t)
            i += 2                                  # 数字 = fd 复制；否则跳过目标 token
            continue
        segments[-1].append(t)
        i += 1

    # 参数层 + 前缀层，逐段判定（更具体的规则先行：find 排除集、sed|perl -i 先于前缀层）
    for seg in segments:
        if not seg:
            continue
        head = seg[0].rsplit("/", 1)[-1]
        if head == "find":
            for t in seg[1:]:
                if t in FIND_DENY_FLAGS:
                    return Decision(False, "find:" + t)
        if head in ("sed", "perl"):
            # -i 仅匹配 sed|perl 前缀（#817：grep -i 不误伤；不做裸子串匹配）
            for t in seg[1:]:
                if t == "-i" or (t.startswith("-i") and len(t) > 2 and t[2] != "n"):
                    return Decision(False, head + ":-i")
        if not _seg_head_is_allowed(seg):
            # 前缀层：非白名单/写入器 → deny（裸 python3 -c open('w') 的唯一拦截点）
            return Decision(False, "prefix:" + head)
        # 写入器段：参数只是 target 名，无路径写能力，放行
        if _is_writer_token(seg[0]):
            continue

    return Decision(True, "readonly-ok")


def _is_protected(tok):
    return PROTECTED_MARK in tok


# ─────────────────────────── 审计（D1：不落命令原文）────────────────────────────

def _core_path():
    """写入器不变量核心 gzx-lock-append.py（唯一实现，bash 面与 python 面共用）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.join(here, "..", "skills", "gan-zhong-xue", "gzx-lock-append.py")
    if os.path.exists(cand):
        return cand
    return os.path.join(GZX_DIR, "gzx-lock-append.py")   # 首次运行复制到数据目录的副本


def _audit(tool, allowed, rule):
    """审计一行（经写入器核心，单写者纪律）。内容 = 元数据 only，绝无命令/参数原文。"""
    try:
        audit_file = os.path.join(GZX_DIR, "audit.jsonl")
        lines = []
        if not os.path.exists(audit_file):
            lines.append(json.dumps({
                "ts": _now(), "event": "hook_alive", "hook": "gzx-guard",
            }, ensure_ascii=False))
        lines.append(json.dumps({
            "ts": _now(),
            "tool": tool,
            "decision": "allow" if allowed else "deny",
            "rule": rule,
        }, ensure_ascii=False))
        data = ("\n".join(lines) + "\n").encode()
        if lock_append is not None:
            lock_append(GZX_DIR, "audit", data)          # 进程内直调核心（无子进程）
        else:
            subprocess.run([sys.executable, _core_path(), "audit"], input=data,
                           capture_output=True, timeout=20)   # 独立部署兜底
    except Exception:
        pass  # 审计尽力而为；审计失败不改变已作出的判定


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ─────────────────────────── hook 协议 ────────────────────────────────────────

def _handle(event):
    tool = event.get("tool_name", "")
    ti = event.get("tool_input", {}) or {}
    if tool == "Bash":
        subject = ti.get("command", "")
    elif tool in ("Write", "Edit", "MultiEdit"):
        subject = json.dumps(ti, ensure_ascii=False)   # 文件路径+内容整体当参数面
    elif tool == "Agent":
        subject = ti.get("prompt", "")
    else:
        return 0

    d = classify(subject)
    if d.allow and d.rule == "out-of-scope":
        return 0                                       # 不引用受保护目录：不审计、放行
    _audit(tool, d.allow, d.rule)
    if not d.allow:
        sys.stderr.write(
            "gzx-guard deny (%s)：受保护目录 ~/.gan-zhong-xue 的写入只能经写入器 "
            "gzx-append.sh（append-only 不变量所在）；只读命令请确认前缀在白名单。\n" % d.rule)
        return 2
    return 0


def selftest():
    """D6 安装验收：allow/deny 双侧用例矩阵（#819/#822/#825/#827/#829/#830/#831 用例全量）。
    0 = 全过。两侧都要验——只验 deny 侧等于没验 allow 侧（#815 §3）。"""
    allows = [
        "sha256sum ~/.gan-zhong-xue/map.md",                 # #815 迁移哈希场景
        "grep -i 先猜 ~/.gan-zhong-xue/map.md",              # #817：-i 仅 sed/perl 前缀
        "grep 先猜 ~/.gan-zhong-xue/map.md | head",          # #819：管道两端白名单
        "grep 先猜 ~/.gan-zhong-xue/map.md|head",            # 同上，无空格（#829 §2 实测切独立 |）
        "cat ~/.gan-zhong-xue/map.md > /dev/null",           # #829 §5：非保护目标放行
        "cat ~/.gan-zhong-xue/map.md 2>/dev/null",           # stderr 丢弃（'2','>','/dev/null'）
        "echo \"a > b\"",                                    # 引号内 > 不误伤（#825 ctrl，范围外顺带验）
        "~/.gan-zhong-xue/gzx-append.sh map < payload",      # 写入器：绝对路径直调
        "gzx-append map",                                    # 写入器：PATH 直调（范围外，顺带验）
        "cat payload | gzx-append candidates",               # 写入器：管道段尾
        "bash ~/.gan-zhong-xue/gzx-append.sh map",           # 写入器：shell 包装
    ]
    denies = [
        ("cat x > ~/.gan-zhong-xue/map.jsonl", "redirect"),                     # #815 §3
        ("cat ~/.gan-zhong-xue/map.md > ~/.gan-zhong-xue/map.jsonl", "redirect"),
        ("sed -i s/a/b/ ~/.gan-zhong-xue/map.md", "sed"),                       # #819 §1
        ("cat ~/.gan-zhong-xue/map.md | tee ~/.gan-zhong-xue/map.jsonl", "prefix:tee"),
        ("cat ~/.gan-zhong-xue/map.md && python3 -c \"open('m','w')\"", "structure"),  # #822 §1
        ("cat ~/.gan-zhong-xue/map.md; rm ~/.gan-zhong-xue/map.md", "structure"),      # #827 §1-1
        ("cat ~/.gan-zhong-xue/map.md>~/.gan-zhong-xue/map.jsonl", "redirect"),        # #827 §1-2
        ("cat ~/.gan-zhong-xue/map.md >| ~/.gan-zhong-xue/map.jsonl", "redirect"),     # #830 §3
        ("cat ~/.gan-zhong-xue/map.md &> ~/.gan-zhong-xue/map.jsonl", "redirect"),     # #831 §2
        ("cat ~/.gan-zhong-xue/map.md\nrm ~/.gan-zhong-xue/map.md", "structure"),      # #830 §1 换行
        ("cat ~/.gan-zhong-xue/map.md #c\nrm ~/.gan-zhong-xue/map.md", "structure"),   # #830 注释吞换行
        ("cat ~/.gan-zhong-xue/map.md & python3 -c \"open('m','w')\"", "structure"),   # #831 §3 裸 &
        ("find ~/.gan-zhong-xue -name map.md -fprint ~/.gan-zhong-xue/map.md", "find"),  # #822 §2
        ("find ~/.gan-zhong-xue -name map.md -delete", "find"),
        ("find ~/.gan-zhong-xue -name map.md -execdir rm {} \\;", "structure"),  # \; 是字面分号 token，结构层先拦（判定同向：deny）
        ("python3 -c \"open('~/.gan-zhong-xue/map.md','w')\"", "prefix:python3"),  # #825 §1-3
        ("rm ~/.gan-zhong-xue/map.md", "prefix:rm"),
        ("perl -i -pe s/a/b/ ~/.gan-zhong-xue/map.md", "perl"),
        ("cat map.md | gzx-append map > ~/.gan-zhong-xue/map.md", "redirect"),  # 写入器行内夹重定向仍拒
        ("cat ~/.gan-zhong-xue/map.md 'oops", "structure"),                     # 不可解析 → deny（#822 §1）
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
        return 0   # fail-open：宿主协议异常不冻住用户会话（减速带定位）
    raise SystemExit(_handle(event))


if __name__ == "__main__":
    main()
