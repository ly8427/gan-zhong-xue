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
import re
import shlex
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "skills", "gan-zhong-xue"))
try:
    # 写入器不变量核心（唯一实现）；_normalize_dir 供路径身份判定做跨形态换算
    from gzx_lock_append import lock_append, _normalize_dir
except ImportError:
    try:
        from gzx_lock_append import _normalize_dir
    except ImportError:
        _normalize_dir = None
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


def _canon_path(p):
    """路径解析身份的统一口径（board #122 dsh §4）：expanduser → 跨形态换算
    （POSIX python 收到 /c|/mnt/c 形态、Windows python 收到盘符形态，见
    gzx_lock_append._normalize_dir）→ normcase(realpath)。"""
    p = os.path.expanduser(p)
    if _normalize_dir is not None:
        p = _normalize_dir(p)
    return os.path.normcase(os.path.realpath(p))


def _writer_installed_path():
    """写入器的唯一合法安装位（数据目录里的 gzx-append.sh）。"""
    return _canon_path(os.path.join(GZX_DIR, "gzx-append.sh"))


def _is_writer_token(tok):
    # V1.1.1（board #122 M2/dsh §4）：身份 = 解析后路径 == 安装常量。
    # 旧口径（basename 相等即真）是绕过原语：/tmp/x/gzx-append 内容任意也放行。
    # PATH 直调先经 shutil.which 解析；名字先筛一层（名字都不像就不必解析）。
    base = tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if base not in (WRITER_MARK, WRITER_MARK + ".sh"):
        return False
    cand = os.path.expanduser(tok)
    return _canon_path(shutil.which(cand) or cand) == _writer_installed_path()


def _seg_head_is_allowed(seg):
    head = seg[0].rsplit("/", 1)[-1]
    if head in READONLY_PREFIX:
        return True
    if _is_writer_token(seg[0]):
        return True
    if head in SHELL_WRAPPERS and any(_is_writer_token(t) for t in seg[1:]):
        return True   # bash/sh/source 包装写入器脚本；bash -c payload 不含独立写入器 token → 照 deny
    return False


# V1.1.0（P1-2 引导豁免）：首次安装需要把写入器部署进数据目录并初始化权限——
# 这三段操作若被守卫拦下，新用户永远装不起来（GPT PR #4 审查 P1-2 实景）。
# 豁免口径（减速带定位内、逐段可判定）：
#   cp/install —— 恰 3 参 ∧ 源 basename ∈ {gzx-append.sh, gzx_lock_append.py}
#     ∧ 目标 basename ∈ 同集合 ∧ 目标 dirname 解析后 == GZX_DIR ∧ 来源目录含
#     SKILL.md（插件束形态判据：写入器与 SKILL.md 同目录随插件分发，SKILL.md:33）。
#     V1.1.1（board #122 M2/dsh §3）：弃「段内含 gzx-append 子串即豁免」——
#     `cp /tmp/gzx-append map.md` 一条绕过（dsh #2654 §3 实证）。
#     r12（GPT 外审八轮 P1）：M2 口径仍漏两格——目标文件名不验（源名合法的改名件
#     可覆盖 map.md）与来源不验（/tmp/evil/gzx-append.sh 可覆盖写入器本体）→ 本次
#     补齐；原「真写入器复制成 .bak 别名仍放行」余量随目标名检查一并收回（selftest 移 deny）。
#   mkdir —— 仅准建数据目录本身（r12 GPT 外审八轮 P2：不再泛化到任意子路径）。
#   chmod —— 仅两种初始化形状：写入器授执行（mode 同时含 + 与 x）与数据目录
#     owner-rwx（数值三位且首位 7）。r12（GPT 外审八轮 P2）：弃「chmod 数值不做
#     白名单」旧余量——chmod 000 可清零写入器/数据资产权限使机制失效。
#   mkdir/chmod 段内的单字符数字视为 fd 尾巴剥除（`2>/dev/null` 经重定向层剥离后
#     会把裸 `2` 残进段——"700" 三位数字不受扰）。
_BOOTSTRAP_SRC_NAMES = {"gzx-append.sh", "gzx_lock_append.py"}
_BOOTSTRAP_SETUP = {"mkdir", "chmod"}
_PLUGIN_BUNDLE_MARK = "SKILL.md"


def _seg_is_bootstrap(seg):
    head = seg[0].rsplit("/", 1)[-1]
    if head in ("cp", "install"):
        if len(seg) != 3:
            return False
        src_path = os.path.expanduser(seg[1])
        src = src_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        dst_path = os.path.expanduser(seg[2])
        dst = dst_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        dst_dir = os.path.dirname(dst_path)
        if (src not in _BOOTSTRAP_SRC_NAMES
                or dst not in _BOOTSTRAP_SRC_NAMES
                or _canon_path(dst_dir) != _canon_path(GZX_DIR)):
            return False
        # 来源合法性（r12 GPT 外审八轮 P1）：源必须随插件束分发（所在目录含 SKILL.md），
        # 防 /tmp 改名件借豁免覆盖数据资产或写入器本体。路径先过 _normalize_dir 跨形态
        # 换算（MSYS /c/ 形态在 Windows python 里直接 isfile 会查错盘——B1 一手实证）；
        # 相对路径按仓库根解析（自检可能不从仓库根发起）。
        src_dir = os.path.dirname(src_path)
        if _normalize_dir is not None:
            src_dir = _normalize_dir(src_dir)
        if not os.path.isabs(src_dir):
            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            src_dir = os.path.join(repo_root, src_dir)
        return os.path.isfile(os.path.join(src_dir, _PLUGIN_BUNDLE_MARK))
    if head in _BOOTSTRAP_SETUP:
        # 单字符数字 = `2>/dev/null` 类 fd 尾巴在重定向层剥离后残进段的残留，剥除
        #（"700" 三位不受扰——fd 号只有 0-2 一位）。
        args = [t for t in seg[1:] if not (t.isdigit() and len(t) == 1)]
        # shlex 非 wordchar 切分：'chmod +x P' 实到段为 ['chmod','+','x','P']（'u+x'
        # 同理切成三段；'700' 数字是 wordchar 不切）——mode=路径样 token 之前的短
        # token 拼接，target=唯一路径样 token。
        pathish = [t for t in args if ("/" in t or "\\" in t or t.startswith("~"))]
        if head == "mkdir":
            # mkdir 的目标本身就是路径 token（pathish 是 chmod 的 mode/target 拆分，不适用于此）
            targets = [t for t in args if not t.startswith("-")]
            return (bool(targets)
                    and all(_canon_path(t) == _canon_path(GZX_DIR) for t in targets))
        modeparts = [t for t in args if t not in pathish]
        if len(pathish) != 1 or not modeparts:
            return False
        mode = "".join(modeparts)
        if mode.startswith("-"):                 # 旗标形态（-R 等）fail-closed 不豁免
            return False
        target = pathish[0]
        if _canon_path(target) == _writer_installed_path():
            return "+" in mode and "x" in mode   # 授执行形态（+x/a+x/u+x…）
        if _canon_path(target) == _canon_path(GZX_DIR):
            return mode.isdigit() and len(mode) == 3 and mode[0] == "7"
        return False
    return False


# V1.1.1（board #122 M2-ext，r4 按 grok #2661 + dsh #2662/#2663 六条绑定重写）：
# writer-heredoc 例外 = SKILL.md 正式保存的形状（写入器路径 target <<'定界符'）。
# 六条绑定（全部可进 --selftest）：
#   B1 只认**带引号**定界符（<<'X' / <<"X"）——未引号时 bash 会展开正文里的
#      $(…)/${…}，「正文是数据」的前提不成立（grok #2661 实跑 HEAD：未引号+
#      $(echo pwn) 当时 allow=True，那是本例外之前的既有漏，由 B2 修）；
#      未加引号与 <<- 一律不进例外（<<- 不支持，fail-closed）。
#   B2 结构层「token 含换行即拒」——punctuation_chars 会把连续标点粘成单 token
#      （如 `\n$(…)`），精确等于 \n 的旧口径被粘接躲过；含换行就拒，不枚举粘接。
#   B3 定界符**唯一且位于末行**（split("\n") 后等于定界符的行恰好是最后一行）——
#      挡「正文提前出现同形行 → bash 提前终结 → 后续行被执行」（SKILL.md 原
#      残余风险段记载的形状；例外挂上后后果从脏数据升级为命令执行）。
#   B4 切行只按 raw.split("\n")，不用 splitlines()/split()（unicode 行分隔符
#      与 bash 认知不一致，两个方向都会错）。
#   B5 首行只认**一个** heredoc 算子（token 相等判 <<，禁子串；<<< 是 herestring
#      无正文不进本例外路径，单行 writer 调用走原 allow 逻辑）。
#   B6 例外 allow 用独立 rule 名 writer-heredoc:<正文字节数>b 落审计（活体证明，
#      #811① 同源；仍不落命令原文）。
# 安全依据：带引号定界符的正文在 bash 语义下只进写入器 stdin、永不解释；写入器
# 自身不变量（只收 1 个 target 参、五个常量目标，gzx-append.sh:38-47）⇒ 首行
# 无路径写能力，判「写入器身份 + target」即够（dsh #2662 §4）。
# r5 身份判据两句（board #122 dsh #2668 §3，写死防再犯）：
#   ① 身份 = _is_writer_token(writer_token) 为真——writer_token 取结构层 token
#      流第 1 个 token（不是正则原文，引号信息在 token 层已被剥掉，dsh §2b）；
#   ② classify(首行) 的 out-of-scope **不算**身份通过——首行判定全部内联，
#      根本不调 classify，从根上排除「没进入判定」冒充「判定为写入器」。
_HEREDOC_WRITER_RE = re.compile(
    r"^[ \t]*(\S*gzx-append\.sh)[ \t]+(map|jsonl|pending|candidates|audit)"
    r"[ \t]+<<[ \t]*(['\"])([A-Za-z_][A-Za-z0-9_]*)\3\n((?:.*\n)?)\4$",
    re.DOTALL,
)


def classify(cmd):
    """对一条 Bash 命令做守卫判定。返回 Decision。纯函数，便于测试。"""
    # 范围条件：不引用受保护目录 → 放行（D3：保守 deny 以引用受保护路径为条件）
    if PROTECTED_MARK not in cmd:
        return Decision(True, "out-of-scope")

    # writer-heredoc 例外（V1.1.1，绑定 B1/B3/B4/B5/B6；B2 在下方 token 循环）：
    # r5 收口（grok #2667 + dsh #2668 §2/§3）：首行判定**全部内联、不递归
    # classify**——递归会经 out-of-scope 短路（首行不含保护目录时），把「没进
    # 入判定」错当「判定为写入器」。身份 = _is_writer_token(token 流第 1 个
    # token) 直接判，False ⇒ heredoc:writer_identity 拒（独立 rule 名，B5）。
    m = _HEREDOC_WRITER_RE.match(cmd)
    if m:
        delim = m.group(4)
        lines = cmd.split("\n")                      # B4：只按 \n 切
        # r7（grok #2674 实跑）：Git Bash 对定界符行末尾 \r 不止容忍一个——
        # N=1..8 全部终结 heredoc、其后行照跑 → 剥**全部**末尾 \r（不是恰好一个）。
        # ② 定界符行后**恰好一个**换行仍 allow（文档围栏原文自带末尾 \n），多于
        # 一个空行 = 末行不是定界符 → 照旧 deny（定界符后接命令也照旧 deny）。
        if lines and lines[-1] == "":
            lines = lines[:-1]
        lines = [l.rstrip("\r") for l in lines]
        if [i for i, l in enumerate(lines) if l == delim] != [len(lines) - 1]:
            return Decision(False, "heredoc:delim_repeats")   # B3：唯一且末行
        first = lines[0]
        toks, tok_err = _structure_tokens(first)
        if tok_err or sum(1 for t in toks if t == "<<") != 1 \
                or any(t.startswith("<<") and t != "<<" for t in toks):
            return Decision(False, "heredoc:operator")        # B5：单算子，token 相等
        i2 = 0
        while i2 < len(toks):                        # 结构层 + 重定向层（内联，B2）
            t = toks[i2]
            if t in STRUCTURE_DENY_TOKENS or "\n" in t:
                return Decision(False, "structure:" + t.replace("\n", "\\n")[:24])
            if t in REDIRECT_TOKENS:
                if i2 + 1 >= len(toks):
                    return Decision(False, "redirect:missing-target")
                tgt = toks[i2 + 1]
                if not tgt.isdigit() and _is_protected(tgt):
                    return Decision(False, "redirect-to-protected:" + t)
                i2 += 2
                continue
            i2 += 1
        if not _is_writer_token(toks[0]):            # 身份层：token 形态直判（dsh §2a/b）
            return Decision(False, "heredoc:writer_identity")
        return Decision(True, "writer-heredoc:%dB"   # B6：独立 rule 名 + 正文字节数
                        % len(m.group(5).encode("utf-8")))

    toks, err = _structure_tokens(cmd)
    if err:
        return Decision(False, "structure:unparseable")

    # 结构层扫描 + 管道分段
    segments = [[]]          # 按管道切分的命令段（token 列表）
    i = 0
    n = len(toks)
    while i < n:
        t = toks[i]
        # B2（board #122/grok #2661）：punctuation_chars 会把连续标点粘成单 token
        # （如 `\n$(…)`、`)\n`）——精确等于 \n 的旧口径被粘接躲过（未引号 heredoc
        # 正文 $(…) 展开实跑 allow）。含换行即拒，不枚举粘接组合。
        if t in STRUCTURE_DENY_TOKENS or "\n" in t:
            return Decision(False, "structure:" + t.replace("\n", "\\n")[:24])
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
        if _seg_is_bootstrap(seg):
            continue   # V1.1.0(P1-2):写入器引导安装/数据目录初始化 → 放行
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
    """写入器不变量核心 gzx_lock_append.py（唯一实现，bash 面与 python 面共用）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.join(here, "..", "skills", "gan-zhong-xue", "gzx_lock_append.py")
    if os.path.exists(cand):
        return cand
    return os.path.join(GZX_DIR, "gzx_lock_append.py")   # 首次运行复制到数据目录的副本


def _audit(tool, allowed, rule):
    """审计一行（经写入器核心，单写者纪律）。内容 = 元数据 only，绝无命令/参数原文。
    V1.1.0（P2-7）:hook_alive 首行的存在性检查+预置移入写入器核心锁内
    （prepend_if_missing）——并发守卫不再产生重复首行。"""
    try:
        audit_file = os.path.join(GZX_DIR, "audit.jsonl")
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
            lock_append(GZX_DIR, "audit", data, prepend_if_missing=alive)  # 进程内直调核心
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
        # V1.1.0(P1-2):首次安装引导——部署写入器/建目录/收权限（.bak 别名余量 r12 收回，移 deny）
        "cp skills/gan-zhong-xue/gzx-append.sh ~/.gan-zhong-xue/gzx-append.sh",
        "mkdir -p ~/.gan-zhong-xue",
        "chmod 700 ~/.gan-zhong-xue",
        # V1.1.1(board #122 M1/M2):SKILL.md 第 -1 步改版后的单句引导与自检
        "chmod +x ~/.gan-zhong-xue/gzx-append.sh",
        "cp skills/gan-zhong-xue/gzx_lock_append.py ~/.gan-zhong-xue/gzx_lock_append.py",
        "ls ~/.gan-zhong-xue/gzx-append.sh 2>/dev/null",
        "cat ~/.gan-zhong-xue/pending.md 2>/dev/null",
        "cat ~/.gan-zhong-xue/candidates.jsonl 2>/dev/null",
        # v1.1.2（Bugbot PR#4 High）：pending 唯一清空通道=写入器动词——两形态都放行
        "~/.gan-zhong-xue/gzx-append.sh pending-clear",
        "gzx-append pending-clear",
        "~/.gan-zhong-xue/gzx-append.sh badtarget </dev/null",   # 直执行自检(rc21 不落盘)
        # V1.1.1(M2-ext):writer-heredoc 例外——正式保存的文档原样形状
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
        # r6 正向(grok #2670 条件②)：定界符行后恰好一个换行（围栏原文形状）仍 allow
        "~/.gan-zhong-xue/gzx-append.sh map <<'E'\n## 概念\nE\n",
        "~/.gan-zhong-xue/gzx-append.sh jsonl <<< '{\"id\":2}'",   # B5 旁注:herestring 无正文,单行走原 allow
    ]
    denies = [
        # V1.1.1(board #122 M2):豁免收紧后的负向用例(dsh #2654 §3/§4 实证形状)
        ("cp /tmp/gzx-append ~/.gan-zhong-xue/map.md", "prefix:cp"),      # 源非写入器两件
        ("cp /tmp/evil ~/.gan-zhong-xue/gzx-append.sh", "prefix:cp"),     # 覆盖写入器本体
        ("cp a ~/.gan-zhong-xue/gzx-append.sh b", "prefix:cp"),           # 非 2 参不豁免
        # r12(GPT 外审八轮 P1/P2):目标文件名+来源合法性+chmod 形状收紧的负向用例
        ("cp /tmp/evil/gzx-append.sh ~/.gan-zhong-xue/map.md", "prefix:cp"),         # 源名合法但目标=数据资产
        ("cp /tmp/evil/gzx-append.sh ~/.gan-zhong-xue/gzx-append.sh", "prefix:cp"),  # 来源目录无 SKILL.md（改名件）
        ("cp ~/.gan-zhong-xue/gzx-append.sh ~/.gan-zhong-xue/gzx_lock_append.py.bak",
         "prefix:cp"),                                                    # .bak 别名余量收回（目标名不合法）
        ("chmod 000 ~/.gan-zhong-xue/map.md", "prefix:chmod"),            # 数据资产改权限
        ("chmod 000 ~/.gan-zhong-xue/gzx-append.sh", "prefix:chmod"),     # 写入器权限清零
        ("chmod -R 700 ~/.gan-zhong-xue", "prefix:chmod"),                # 旗标形态 fail-closed
        ("mkdir ~/.gan-zhong-xue/sub", "prefix:mkdir"),                   # 子路径不豁免
        # M2-ext 负向:heredoc 例外不可被滥用
        ("~/.gan-zhong-xue/gzx-append.sh map <<'E'\nx\nE\nrm ~/.gan-zhong-xue/map.md",
         "structure"),                                                    # 定界符后接命令
        ("cat > ~/.gan-zhong-xue/map.md <<'E'\nx\nE", "redirect"),        # 非写入器头 heredoc（重定向层先拦）
        # M2:第 -1 步旧写法(分支/赋值)按新口径全部拒——文档已改单句,这里钉死不回潮
        ("[ -s ~/.gan-zhong-xue/pending.md ]", "prefix:["),
        ("test -s ~/.gan-zhong-xue/pending.md", "prefix:test"),
        ("if [ -s ~/.gan-zhong-xue/map.jsonl ]; then cat ~/.gan-zhong-xue/map.jsonl; fi",
         "structure"),
        ("GZX_APP=~/.gan-zhong-xue/gzx-append.sh", "prefix:gzx-append.sh"),  # 赋值行假绿(dsh §4)
        ("mkdir -p ~/.gan-zhong-xue && chmod 700 ~/.gan-zhong-xue", "structure"),  # 复合引导仍拒
        # B1/B2 负向(grok #2661 实跑 HEAD 曾 allow)：未引号定界符 + $(…) —— B2 修复后必拒
        ("~/.gan-zhong-xue/gzx-append.sh map <<EOF\n$(echo pwn)\nEOF", "structure"),
        # B3 负向(dsh #2662 §1 形状)：正文提前出现同形定界符行 → bash 提前终结、后行被执行
        ("~/.gan-zhong-xue/gzx-append.sh map <<'E'\nhello\nE\nrm ~/.gan-zhong-xue/map.md\nE",
         "heredoc:delim_repeats"),
        # B1 负向：<<- 不进例外（fail-closed，不支持——计划已写死）
        ("~/.gan-zhong-xue/gzx-append.sh map <<-'E'\nx\nE", "structure"),
        # B5 负向：双算子落不进例外 → 换行拒
        ("~/.gan-zhong-xue/gzx-append.sh map <<'A' <<'B'\nx\nA", "structure"),
        # B5 负向(grok #2667 实跑形状)：假写入器路径 + 正文提保护目录 —— 身份层必拒
        ("/tmp/evil/gzx-append.sh map <<'E'\n~/.gan-zhong-xue/map.md\nE",
         "heredoc:writer_identity"),
        ("./gzx-append.sh map <<'E'\n~/.gan-zhong-xue/map.md\nE",
         "heredoc:writer_identity"),
        # r6 负向(grok #2670 实跑)：正文 `E\r` 在 Git Bash 是定界符行、其后 rm 照跑
        ("~/.gan-zhong-xue/gzx-append.sh map <<'E'\nE\r\nrm ~/.gan-zhong-xue/map.md\nE",
         "heredoc:delim_repeats"),
        # r7 负向(grok #2674 实跑)：两个及以上末尾 \r 同样终结 heredoc → 必拒
        ("~/.gan-zhong-xue/gzx-append.sh map <<'E'\nE\r\r\nrm ~/.gan-zhong-xue/map.md\nE",
         "heredoc:delim_repeats"),
        ("cat x > ~/.gan-zhong-xue/map.jsonl", "redirect"),                     # #815 §3
        ("cp ~/.gan-zhong-xue/map.md /tmp/evil.md", "prefix:cp"),               # V1.1.0:拷数据出受保护目录仍拦
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
