#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gzx_cli.py — gan-zhong-xue 聚合 CLI（v1.2.0）。

将第 -1 步多条碎命令与第 3 步双写/校验收进确定性 Python 接口：
  preflight         初始化目录与写入器、读取 pending/candidates/map.jsonl 摘要、检测双地图发散
  cell <概念>       读取指定概念的最新 jsonl 记录与 map.md 对应段落
                    （同概念跨多分层时报歧义退出码 25；cell <概念> <分层> 精确取格）
  save-cell         校验 stdin JSON 并经同一把锁互斥追加到 map.jsonl 与 map.md
                    （单写者互斥；非跨文件事务原子，见 gzx_lock_append.py 模块头）
  pending-add       从 stdin 追加到 pending.md（走写入器锁）
  pending-clear     锁内清空 pending.md
  check-divergence  按 (概念,分层) 将 map.jsonl 行与 map.md 格逐位配对、逐字段
                    比对渲染面（重建+diff，D2 发散检测；计数/字段/孤立格均报）
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from gzx_lock_append import (
    _normalize_dir,
    find_git_bash,
    lock_append,
    lock_append_map_pair,
    lock_truncate,
)

ABSTAIN = "因隐私约束未保存原文"


def _gzx_dir():
    raw = os.environ.get("GZX_DIR") or os.path.expanduser("~/.gan-zhong-xue")
    return _normalize_dir(raw)


def _today():
    return time.strftime("%Y-%m-%d", time.localtime())


def _single_line(val):
    if val is None:
        return ""
    return re.sub(r"[\r\n]+", " ", str(val)).strip()


def _read_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def _parse_jsonl(path):
    rows = []
    bad = 0
    total_nonempty = 0
    for ln in _read_lines(path):
        s = ln.strip()
        if not s:
            continue
        total_nonempty += 1
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                rows.append(obj)
            else:
                bad += 1
        except ValueError:
            bad += 1
    return rows, bad, total_nonempty


def _count_md_cells(path):
    return sum(1 for ln in _read_lines(path) if ln.startswith("## "))


def _bootstrap_writer(gzx_dir):
    os.makedirs(gzx_dir, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(gzx_dir, 0o700)
        except OSError:
            pass
    copied = []
    for name in ("gzx-append.sh", "gzx_lock_append.py"):
        src = os.path.join(SKILL_DIR, name)
        dst = os.path.join(gzx_dir, name)
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)
            copied.append(name)
        if name == "gzx-append.sh" and os.path.exists(dst) and os.name != "nt":
            try:
                mode = os.stat(dst).st_mode
                if not (mode & stat.S_IXUSR):
                    os.chmod(dst, mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
    return copied


def _parse_md_heading_parts(line):
    """「## 概念 · 分层 · 日期」→ [概念, 分层, 日期]（各自 strip）；非格标题 → None。"""
    if not line.startswith("## "):
        return None
    parts = [p.strip() for p in line[3:].strip().split("·")]
    return parts if parts and parts[0] else None


def _parse_md_heading_concept(line):
    parts = _parse_md_heading_parts(line)
    return parts[0] if parts else None


def _extract_md_section(md_path, concept, tier=None):
    """提取概念对应的 map.md 段落（tier 给出时按 (概念,分层) 双键精确命中）。
    按概念名称精确匹配（防止短名如 'Foo' 误抢 'Foo Bar'），
    并倒序查找最后一次出现的段落（与 jsonl 侧取最新行对齐）。"""
    lines = _read_lines(md_path)
    sections = []
    curr = None
    for ln in lines:
        if ln.startswith("## "):
            if curr is not None:
                sections.append(curr)
            curr = [ln]
        elif curr is not None:
            curr.append(ln)
    if curr is not None:
        sections.append(curr)

    for sec in reversed(sections):
        if not sec:
            continue
        parts = _parse_md_heading_parts(sec[0])
        if not parts or parts[0] != concept:
            continue
        if tier is not None and (len(parts) < 2 or parts[1] != tier):
            continue
        return "\n".join(sec).strip()
    return None


def _divergence_cell_check(map_rows, md_sections):
    """重建 + diff（dsh #3055 §5-2）：jsonl 行与 md 格都是 append-only 日志，
    同键 (概念,分层) 按追加顺序**按位配对**（第 k 行配第 k 格——同键多行时
    倒序取最后会把旧行配到新格，实测误报；本注释即 2026-11-28 夹具实证）。
    逐字段比对渲染面：正常格渲染 证据/先猜/顶点原理/底部验证题/用户更懂/状态；
    ⏸ 暂停格渲染 来源/关联靶/关联节点/备注/状态——jsonl 有而渲染面没有的
    字段（如正常格的备注）不比对，不做无据断言。mismatches 上限 10 条。"""
    checked = 0
    mismatches = []
    paired = 0
    truncated = False        # 明细已到上限（Bugbot 4727029068：旧码在此处提前返回，
                             # 把「已配对数」当成第三个返回值即孤立格数写进报告）

    rows_by = {}
    key_order = []
    for row in map_rows:
        k = (row.get("概念"), row.get("分层"))
        if not k[0] or not k[1]:
            continue
        if k not in rows_by:
            key_order.append(k)
        rows_by.setdefault(k, []).append(row)

    secs_by = {}
    for sec in md_sections:
        parts = _parse_md_heading_parts(sec[0]) if sec else None
        if not parts or len(parts) < 2:
            continue          # 无分层的老标题不参与配对（计入 orphan）
        k = (parts[0], parts[1])
        secs_by.setdefault(k, []).append(sec)

    label_normal = [("证据", "证据"), ("先猜", "先猜"), ("顶点原理", "顶点原理"),
                    ("验证题", "底部验证题"), ("用户更懂", "用户更懂"),
                    ("状态", "状态")]
    label_paused = [("来源", "来源"), ("关联靶", "关联靶"),
                    ("关联节点", "关联节点"), ("备注", "备注"),
                    ("状态", "状态")]
    for k in key_order:
        rs = rows_by.get(k, [])
        ss = secs_by.get(k, [])
        if len(ss) < len(rs):
            if len(mismatches) >= 10:
                truncated = True
            else:
                mismatches.append({"格": k[0], "分层": k[1],
                                   "问题": "md 缺 %d 个对应格" % (len(rs) - len(ss))})
        for idx, row in enumerate(rs):
            if idx >= len(ss):
                break
            sec = ss[idx]
            checked += 1
            paired += 1
            is_paused = k[1] == "⏸" or row.get("状态") == "⏸外部前置"
            for key, label in (label_paused if is_paused else label_normal):
                if key not in row:
                    continue
                prefix = "- %s：" % label
                actual = None
                for ln in sec:
                    if ln.startswith(prefix):
                        actual = ln[len(prefix):]
                        break
                if actual is None or actual != str(row[key]):
                    if len(mismatches) >= 10:
                        truncated = True      # 明细封顶：只停止记录，配对继续——孤立格数才算得准
                    else:
                        mismatches.append({"格": k[0], "分层": k[1], "字段": label,
                                           "jsonl": str(row[key]), "md": actual})
    orphan_md = max(0, len(md_sections) - paired)
    return checked, mismatches, orphan_md, truncated


def _divergence_report(gzx_dir):
    md_path = os.path.join(gzx_dir, "map.md")
    jsonl_path = os.path.join(gzx_dir, "map.jsonl")
    md_count = _count_md_cells(md_path)
    map_rows, bad_lines, jsonl_total = _parse_jsonl(jsonl_path)

    # 重建 + diff：把 md 切成格，jsonl 每行按 (概念,分层) 配对逐字段比对
    sections = []
    curr = None
    for ln in _read_lines(md_path):
        if ln.startswith("## "):
            if curr is not None:
                sections.append(curr)
            curr = [ln]
        elif curr is not None:
            curr.append(ln)
    if curr is not None:
        sections.append(curr)

    checked, mismatches, orphan_md, truncated = _divergence_cell_check(map_rows, sections)
    count_diverged = md_count != jsonl_total
    diverged = count_diverged or bool(mismatches) or orphan_md > 0
    rep = {
        "diverged": diverged,
        "md_count": md_count,
        "jsonl_count": jsonl_total,
        "jsonl_bad_lines": bad_lines,
        "cell_check": {
            "checked": checked,
            "mismatches": mismatches,
            "mismatches_truncated": truncated,
            "orphan_md_cells": orphan_md,
        },
    }
    if diverged:
        if jsonl_total == 0 and md_count > 0:
            rep["reason"] = "legacy-md-only"
            rep["hint"] = "检测到遗留 md-only 格（map.jsonl 为空）；无需迁移，新格将同时写入两侧"
        elif mismatches and not count_diverged:
            rep["reason"] = "field-mismatch"
            rep["hint"] = "格数一致但字段比对不符（cell_check.mismatches）；以 map.jsonl 为准修正 map.md 对应格"
        else:
            rep["reason"] = "count-mismatch"
            rep["hint"] = "map.md(%d 格) 与 map.jsonl(%d 行) 不一致；map.jsonl 已包含全部字段(含证据/顶点原理/用户更懂)，可据其无损重建 map.md" % (
                md_count, jsonl_total)
    return rep


def _selftest_writer(gzx_dir):
    """真实直执行写入器自检（badtarget 应回退出码 21 且带 unknown target）。"""
    writer_sh = os.path.join(gzx_dir, "gzx-append.sh")
    if not os.path.exists(writer_sh):
        return False, "writer not found"
    try:
        if os.name == "nt":
            # 共享探测（gzx_lock_append.find_git_bash）：先固定安装位，which 结果
            # 须为 MINGW/MSYS——which("bash") 裸用可能选中 WSL bash（grok #3054 A-7）
            bash_bin = find_git_bash()
            if bash_bin is None:
                return False, "no usable bash (MINGW/MSYS) on Windows"
            cmd = [bash_bin, writer_sh, "badtarget"]
        else:
            cmd = [writer_sh, "badtarget"]
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
        ok = r.returncode == 21 and b"unknown target" in r.stderr
        return ok, r.stderr.decode("utf-8", "replace").strip()
    except Exception as e:
        return False, str(e)


def cmd_preflight(gzx_dir):
    copied = _bootstrap_writer(gzx_dir)
    writer_ok, writer_diag = _selftest_writer(gzx_dir)
    pend_lines = [ln for ln in _read_lines(os.path.join(gzx_dir, "pending.md")) if ln.strip()]
    cand_rows, cand_bad, _ = _parse_jsonl(os.path.join(gzx_dir, "candidates.jsonl"))
    map_rows, map_bad, _ = _parse_jsonl(os.path.join(gzx_dir, "map.jsonl"))

    latest_by_concept = {}
    paused_by_target = {}
    for idx, row in enumerate(map_rows):
        concept = row.get("概念")
        if not concept:
            continue
        is_paused = row.get("分层") == "⏸" or row.get("状态") == "⏸外部前置"
        if is_paused:
            tid = row.get("关联靶") or ""
            paused_by_target.setdefault(tid, []).append({
                "概念": concept,
                "关联节点": row.get("关联节点", ""),
                "首次": row.get("首次", ""),
                "备注": row.get("备注", ""),
            })
        else:
            latest_by_concept[concept] = {
                "seq": idx + 1,
                "概念": concept,
                "分层": row.get("分层", ""),
                "状态": row.get("状态", ""),
                "上次复验": row.get("上次复验") or row.get("首次", ""),
                "有验证题": bool(row.get("验证题")),
            }

    div = _divergence_report(gzx_dir)
    legacy_md = None
    if div.get("reason") == "legacy-md-only":
        legacy_md = "\n".join(_read_lines(os.path.join(gzx_dir, "map.md")))

    out = {
        "gzx_dir": gzx_dir,
        "bootstrapped": copied,
        "writer_ready": writer_ok,
        "writer_selftest": {"ok": writer_ok, "diag": writer_diag},
        "pending": {"count": len(pend_lines), "lines": pend_lines},
        "candidates": {
            "count": len(cand_rows),
            "bad_lines": cand_bad,
            "recent": cand_rows[-10:],
        },
        "map": {
            "total_rows": len(map_rows),
            "bad_lines": map_bad,
            "unique_concepts": len(latest_by_concept),
            "concepts": list(latest_by_concept.values()),
            "paused_by_target": paused_by_target,
        },
        "divergence": div,
    }
    if legacy_md is not None:
        out["legacy_md"] = legacy_md
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_cell(gzx_dir, concept, tier=None):
    """读格。同一概念在 map.md/jsonl 里跨多个分层时（dsh #3055 §5-1 的跨层
    last-wins 取错格问题）不静默返回，报歧义（退出码 25）并附分层候选；
    需要精确取格用 cell <概念> <分层>。"""
    map_rows, _, _ = _parse_jsonl(os.path.join(gzx_dir, "map.jsonl"))
    rows = [r for r in map_rows if r.get("概念") == concept]
    if tier is not None:
        latest = None
        for r in rows:
            if r.get("分层") == tier:
                latest = r
        md_sec = _extract_md_section(os.path.join(gzx_dir, "map.md"), concept, tier)
        print(json.dumps({
            "概念": concept,
            "分层": tier,
            "jsonl_latest": latest,
            "md_section": md_sec,
        }, ensure_ascii=False, indent=2))
        return 0

    tiers = []
    for r in rows:
        t = r.get("分层", "")
        if t not in tiers:
            tiers.append(t)
    if len(tiers) > 1:
        print(json.dumps({
            "概念": concept,
            "ambiguous": True,
            "分层候选": tiers,
            "jsonl_latest": None,
            "md_section": None,
            "hint": "同一概念存在多个分层；用 cell <概念> <分层> 精确取格",
        }, ensure_ascii=False, indent=2))
        return 25
    latest = rows[-1] if rows else None
    md_sec = _extract_md_section(os.path.join(gzx_dir, "map.md"), concept)
    print(json.dumps({
        "概念": concept,
        "jsonl_latest": latest,
        "md_section": md_sec,
    }, ensure_ascii=False, indent=2))
    return 0


def _validate_and_render_cell(obj):
    if not isinstance(obj, dict):
        return None, None, "payload must be a JSON object"
    concept = _single_line(obj.get("概念"))
    tier = _single_line(obj.get("分层"))
    source = _single_line(obj.get("来源"))
    status = _single_line(obj.get("状态"))
    if not concept or not tier or not source or not status:
        return None, None, "missing required fields: 概念 / 分层 / 来源 / 状态"

    today = _today()
    first = _single_line(obj.get("首次")) or today
    last_re = _single_line(obj.get("上次复验")) or first
    note = _single_line(obj.get("备注"))
    is_paused = (tier == "⏸" or status == "⏸外部前置")

    if is_paused:
        target_id = _single_line(obj.get("关联靶"))
        node_id = _single_line(obj.get("关联节点"))
        if not target_id or not node_id:
            return None, None, "paused cell requires 关联靶 and 关联节点"
        row = {
            "概念": concept,
            "分层": "⏸",
            "来源": source,
            "关联靶": target_id,
            "关联节点": node_id,
            "首次": first,
            "状态": "⏸外部前置",
            "备注": note,
        }
        md_lines = [
            "## %s · ⏸ · %s" % (concept, first),
            "- 来源：%s" % source,
            "- 关联靶：%s" % target_id,
            "- 关联节点：%s" % node_id,
            "- 备注：%s" % (note or "外部前置搭桥放行"),
            "- 状态：⏸外部前置",
            "",
        ]
    else:
        guess = _single_line(obj.get("先猜"))
        q = _single_line(obj.get("验证题"))
        if not guess:
            return None, None, "missing required field: 先猜 (use '%s' if needed)" % ABSTAIN
        if not q:
            return None, None, "missing required field: 验证题 (use '%s' if needed)" % ABSTAIN
        evidence = _single_line(obj.get("证据")) or ABSTAIN
        principle = _single_line(obj.get("顶点原理")) or ABSTAIN
        user_knows = _single_line(obj.get("用户更懂")) or "无"
        row = {
            "概念": concept,
            "分层": tier,
            "来源": source,
            "先猜": guess,
            "证据": evidence,
            "顶点原理": principle,
            "验证题": q,
            "用户更懂": user_knows,
            "首次": first,
            "上次复验": last_re,
            "状态": status,
            "备注": note,
        }
        md_lines = [
            "## %s · %s · %s" % (concept, tier, first),
            "- 证据：%s" % evidence,
            "- 先猜：%s" % guess,
            "- 顶点原理：%s" % principle,
            "- 底部验证题：%s" % q,
            "- 用户更懂：%s" % user_knows,
            "- 状态：%s" % status,
            "",
        ]

    jsonl_line = json.dumps(row, ensure_ascii=False) + "\n"
    md_block = "\n".join(md_lines) + "\n"
    return md_block, jsonl_line, None


def cmd_save_cell(gzx_dir):
    # stdin 按字节读 + 显式 UTF-8 解码：Windows 控制台（cp1252 实测）把文本模式的
    # stdin 也按本地编码解码，中文 JSON 的键会被读成乱码导致「缺字段」——
    # 跨平台口径（PR #7 P1）：JSON 必须是 UTF-8。
    raw_bytes = sys.stdin.buffer.read()
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        sys.stderr.write("gzx-cli: stdin is not valid UTF-8: %s\n" % e)
        return 21
    if not raw.strip():
        sys.stderr.write("gzx-cli: empty stdin\n")
        return 21
    try:
        obj = json.loads(raw)
    except ValueError as e:
        sys.stderr.write("gzx-cli: invalid JSON: %s\n" % e)
        return 21
    md_block, jsonl_line, err = _validate_and_render_cell(obj)
    if err:
        sys.stderr.write("gzx-cli: %s\n" % err)
        return 21
    rc, msg = lock_append_map_pair(gzx_dir, md_block, jsonl_line)
    sys.stderr.write("gzx-cli: %s\n" % msg)
    if rc == 0:
        print(json.dumps({"ok": True, "message": msg}, ensure_ascii=False))
    return rc


def cmd_pending_add(gzx_dir):
    data = sys.stdin.buffer.read()
    if not data.strip():
        sys.stderr.write("gzx-cli: empty stdin\n")
        return 21
    if not data.endswith(b"\n"):
        data += b"\n"
    rc, msg = lock_append(gzx_dir, "pending", data)
    sys.stderr.write("gzx-cli: %s\n" % msg)
    return rc


def cmd_pending_clear(gzx_dir):
    rc, msg = lock_truncate(gzx_dir, "pending")
    sys.stderr.write("gzx-cli: %s\n" % msg)
    return rc


def cmd_check_divergence(gzx_dir):
    rep = _divergence_report(gzx_dir)
    print(json.dumps(rep, ensure_ascii=False))
    if rep["diverged"]:
        sys.stderr.write(
            "gzx-cli: divergence detected: md_count=%d jsonl_count=%d — %s\n"
            % (rep["md_count"], rep["jsonl_count"], rep.get("hint", ""))
        )
        return 24
    return 0


def main(argv=None):
    # 输出流强制 UTF-8：preflight/cell 打印中文 JSON，Windows cp1252 控制台打印即崩
    # （与 hooks/test-gzx-*.py 的同款防护一致）。
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = sys.argv[1:] if argv is None else argv
    if not args:
        sys.stderr.write(
            "usage: gzx_cli.py <preflight|cell <concept>|save-cell|pending-add|pending-clear|check-divergence>\n"
        )
        return 21
    gzx_dir = _gzx_dir()
    sub = args[0]
    if sub == "preflight":
        return cmd_preflight(gzx_dir)
    if sub == "cell":
        if len(args) < 2:
            sys.stderr.write("usage: gzx_cli.py cell <概念> [分层]\n")
            return 21
        tier = args[2] if len(args) > 2 else None
        return cmd_cell(gzx_dir, " ".join(args[1:2]), tier)
    if sub == "save-cell":
        return cmd_save_cell(gzx_dir)
    if sub == "pending-add":
        return cmd_pending_add(gzx_dir)
    if sub == "pending-clear":
        return cmd_pending_clear(gzx_dir)
    if sub == "check-divergence":
        return cmd_check_divergence(gzx_dir)
    sys.stderr.write("gzx-cli: unknown subcommand: %s\n" % sub)
    return 21


if __name__ == "__main__":
    raise SystemExit(main())
