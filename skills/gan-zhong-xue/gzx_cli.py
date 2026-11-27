#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gzx_cli.py — gan-zhong-xue 聚合 CLI（v1.2.0）。

将第 -1 步多条碎命令与第 3 步双写/校验收进确定性 Python 接口：
  preflight         初始化目录与写入器、读取 pending/candidates/map.jsonl 摘要、检测双地图发散
  cell <概念>       读取指定概念的最新 jsonl 记录与 map.md 对应段落
  save-cell         校验 stdin JSON 并同锁原子追加到 map.jsonl 与 map.md
  pending-add       从 stdin 追加到 pending.md（走写入器锁）
  pending-clear     锁内清空 pending.md
  check-divergence  比对 map.md 格数（^## ）与 map.jsonl 行数（D2 发散检测）
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


def _parse_md_heading_concept(line):
    if not line.startswith("## "):
        return None
    rest = line[3:].strip()
    parts = [p.strip() for p in rest.split("·")]
    return parts[0] if parts else None


def _extract_md_section(md_path, concept):
    """提取概念对应的 map.md 段落。
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
        c = _parse_md_heading_concept(sec[0])
        if c == concept:
            return "\n".join(sec).strip()
    return None


def _divergence_report(gzx_dir):
    md_path = os.path.join(gzx_dir, "map.md")
    jsonl_path = os.path.join(gzx_dir, "map.jsonl")
    md_count = _count_md_cells(md_path)
    _, bad_lines, jsonl_total = _parse_jsonl(jsonl_path)
    diverged = md_count != jsonl_total
    rep = {
        "diverged": diverged,
        "md_count": md_count,
        "jsonl_count": jsonl_total,
        "jsonl_bad_lines": bad_lines,
    }
    if diverged:
        if jsonl_total == 0 and md_count > 0:
            rep["reason"] = "legacy-md-only"
            rep["hint"] = "检测到遗留 md-only 格（map.jsonl 为空）；无需迁移，新格将同时写入两侧"
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
            bash_bin = shutil.which("bash") or r"C:\Program Files\Git\bin\bash.exe"
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


def cmd_cell(gzx_dir, concept):
    map_rows, _, _ = _parse_jsonl(os.path.join(gzx_dir, "map.jsonl"))
    latest = None
    for row in map_rows:
        if row.get("概念") == concept:
            latest = row
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
    raw = sys.stdin.read()
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
            sys.stderr.write("usage: gzx_cli.py cell <concept>\n")
            return 21
        return cmd_cell(gzx_dir, " ".join(args[1:]))
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
