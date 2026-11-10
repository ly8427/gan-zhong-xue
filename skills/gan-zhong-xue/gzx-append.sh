#!/usr/bin/env bash
# gzx-append.sh — gan-zhong-xue 数据写入器（v1.0.9，board thread #80 D2 定案）
#
# 用法: gzx-append.sh <target>          内容从 stdin 读入（管道/heredoc 均可）
#   target ∈ {map|jsonl|pending|candidates|audit}
#     map        → ~/.gan-zhong-xue/map.md           （永久记录，append-only）
#     jsonl      → ~/.gan-zhong-xue/map.jsonl        （瘦索引，append-only）
#     pending    → ~/.gan-zhong-xue/pending.md       （瞬态待办，追加）
#     candidates → ~/.gan-zhong-xue/candidates.jsonl （提交捕获台账，hook 专用）
#     audit      → ~/.gan-zhong-xue/audit.jsonl      （hook 审计台账，hook 专用）
#
# 不变量（由构造保证，不靠模型自觉）:
#   * 只追加: 任何路径都只以追加方式打开——flock 快路径用 `cat >>`，
#             python 核心用 'ab' 模式；代码里不存在截断/覆盖分支。
#   * 写锁:   flock(1) 优先（纯 bash，不依赖 python）；无 flock(1) 时 exec
#             python 核心 gzx_lock_append.py（fcntl/msvcrt，两平台标准库皆在）；
#             flock 与 python 皆不可用 → 拒绝写并报错（退出码 22），绝不静默降级。
#   * 四态退出码（thread #80 D2）:
#       0  = 成功写入
#       21 = 目标非法（未知 target / stdin 为空）
#       22 = 锁不可用（拒绝写，不静默降级）
#       23 = 写失败（目录/磁盘/权限）
#   * stderr 恒带一行 gzx-append: 开头的状态行；stdout 不出内容。
#   * 不变量只有一份实现：flock 快路径之外，一切走 gzx_lock_append.py（两个入口无漂移）。
#
# 隐私（thread #80 D1）: 本脚本不解释内容、不落任何日志——写进文件的就是收到的字节。
#   调用方保证内容已按隐私铁则脱敏（值域={脱敏文本(类型化占位符), 弃权串}）。

set -u

GZX_DIR="${GZX_DIR:-$HOME/.gan-zhong-xue}"

fail() {  # fail <exit-code> <message>
  echo "gzx-append: $2" >&2
  exit "$1"
}

[ $# -eq 1 ] || fail 21 "usage: gzx-append.sh <map|jsonl|pending|candidates|audit> (stdin = content)"

case "$1" in
  map)        TARGET="map";        FILENAME="map.md" ;;
  jsonl)      TARGET="jsonl";      FILENAME="map.jsonl" ;;
  pending)    TARGET="pending";    FILENAME="pending.md" ;;
  candidates) TARGET="candidates"; FILENAME="candidates.jsonl" ;;
  audit)      TARGET="audit";      FILENAME="audit.jsonl" ;;
  *) fail 21 "unknown target: $1" ;;
esac

# stdin 先落临时文件（写锁获取失败时不碰目标文件；空内容=误调用）
TMP="$(mktemp "${TMPDIR:-/tmp}/gzx-append.XXXXXX")" || fail 23 "cannot create temp file"
trap 'rm -f "$TMP"' EXIT
cat > "$TMP" || fail 23 "cannot read stdin"
[ -s "$TMP" ] || fail 21 "empty stdin (nothing to append)"

mkdir -p "$GZX_DIR" 2>/dev/null || fail 23 "cannot create $GZX_DIR"

LOCKFILE="$GZX_DIR/.lock"

# ── 路径 A：flock(1) 可用（Linux/WSL 常见，纯 bash 不依赖 python）────
if command -v flock >/dev/null 2>&1; then
  if cat "$TMP" | flock -x "$LOCKFILE" sh -c 'cat >> "$1"' sh "$GZX_DIR/$FILENAME" 2>/dev/null; then
    echo "gzx-append: ok (+$(wc -c < "$TMP") bytes)" >&2
    exit 0
  fi
  fail 23 "write failed (flock path): $TARGET"
fi

# ── 路径 B：exec python 核心（Windows Git Bash 无 flock(1)）──────────
# bash 找同目录的 gzx_lock_append.py；用到的 python 依次尝试 python3 / python。
# Windows 上 python 是原生程序：路径先经 cygpath 转 Windows 形态再传（MSYS 形态它不认）。
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
CORE="$SELF_DIR/gzx_lock_append.py"
[ -f "$CORE" ] || CORE="$GZX_DIR/gzx_lock_append.py"

for PY in python3 python; do
  if command -v "$PY" >/dev/null 2>&1 && "$PY" -c "import sys" 2>/dev/null; then
    if command -v cygpath >/dev/null 2>&1; then
      CORE="$(cygpath -w "$CORE")"
      GZX_DIR="$(cygpath -w "$GZX_DIR")"
    fi
    GZX_DIR="$GZX_DIR" exec "$PY" "$CORE" "$TARGET" < "$TMP"
  fi
done

# ── 路径 C：flock 与 python 皆不可用 → 拒绝写（D2：不静默降级）───────
fail 22 "no locking mechanism available (flock(1) and python both missing) — refusing to write unprotected: $TARGET"
