#!/usr/bin/env bash
# gzx-append.sh — gan-zhong-xue 数据写入器（v1.2.0，append-only 不变量保障）
#
# 用法: gzx-append.sh <target>          内容从 stdin 读入（管道/heredoc 均可）
#   target ∈ {map|jsonl|pending|candidates|audit|pending-clear}
#     pending-clear → 清空 pending.md（锁内核截断、只动 pending；无需 stdin）
#     map        → ~/.gan-zhong-xue/map.md           （永久记录，append-only）
#     jsonl      → ~/.gan-zhong-xue/map.jsonl        （瘦索引，append-only）
#     pending    → ~/.gan-zhong-xue/pending.md       （瞬态待办，追加）
#     candidates → ~/.gan-zhong-xue/candidates.jsonl （提交捕获台账，hook 专用）
#     audit      → ~/.gan-zhong-xue/audit.jsonl      （hook 审计台账，hook 专用）
#
# 四态退出码:
#   0  = 成功写入
#   21 = 目标非法（未知 target / stdin 为空）
#   22 = 锁不可用（拒绝写，不静默降级）
#   23 = 写失败（目录/磁盘/权限）

set -u

GZX_DIR="${GZX_DIR:-$HOME/.gan-zhong-xue}"

fail() {  # fail <exit-code> <message>
  echo "gzx-append: $2" >&2
  exit "$1"
}

[ $# -eq 1 ] || fail 21 "usage: gzx-append.sh <map|jsonl|pending|candidates|audit|pending-clear> (stdin = content; pending-clear 无需 stdin)"

case "$1" in
  map)        TARGET="map";        FILENAME="map.md" ;;
  jsonl)      TARGET="jsonl";      FILENAME="map.jsonl" ;;
  pending)    TARGET="pending";    FILENAME="pending.md" ;;
  candidates) TARGET="candidates"; FILENAME="candidates.jsonl" ;;
  audit)      TARGET="audit";      FILENAME="audit.jsonl" ;;
  pending-clear) TARGET="pending"; FILENAME="pending.md"; CLEAR_PENDING=1 ;;
  *) fail 21 "unknown target: $1" ;;
esac

# ── pending-clear：锁内核截断 pending，绝不碰 map/jsonl ──
if [ "${CLEAR_PENDING:-0}" = "1" ]; then
  [ "$FILENAME" = "pending.md" ] || fail 21 "clear only allowed for pending"
  mkdir -p "$GZX_DIR" 2>/dev/null || fail 23 "cannot create $GZX_DIR"
  LOCKFILE="$GZX_DIR/.lock"
  if command -v flock >/dev/null 2>&1; then
    if flock -x "$LOCKFILE" sh -c ': > "$1"' sh "$GZX_DIR/$FILENAME" 2>/dev/null; then
      echo "gzx-append: ok pending cleared" >&2
      exit 0
    fi
    fail 23 "truncate failed (flock path): pending"
  fi
  SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
  CORE="$SELF_DIR/gzx_lock_append.py"
  [ -f "$CORE" ] || CORE="$GZX_DIR/gzx_lock_append.py"
  for PY in python3 python; do
    if command -v "$PY" >/dev/null 2>&1 && "$PY" -c "import sys" 2>/dev/null; then
      if command -v cygpath >/dev/null 2>&1; then
        CORE="$(cygpath -w "$CORE")"
        GZX_DIR="$(cygpath -w "$GZX_DIR")"
      fi
      GZX_DIR="$GZX_DIR" exec "$PY" "$CORE" --truncate pending
    fi
  done
  fail 22 "no locking mechanism available (flock(1) and python both missing) — refusing to clear pending unprotected"
fi

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

# ── 路径 B：调用 python 核心（Windows Git Bash 无 flock(1)）──────────
# 注意（DEBT D1 修复）：不使用 exec，否则 EXIT trap 不执行导致 $TMP 泄漏。
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
CORE="$SELF_DIR/gzx_lock_append.py"
[ -f "$CORE" ] || CORE="$GZX_DIR/gzx_lock_append.py"

for PY in python3 python; do
  if command -v "$PY" >/dev/null 2>&1 && "$PY" -c "import sys" 2>/dev/null; then
    if command -v cygpath >/dev/null 2>&1; then
      CORE="$(cygpath -w "$CORE")"
      GZX_DIR="$(cygpath -w "$GZX_DIR")"
    fi
    GZX_DIR="$GZX_DIR" "$PY" "$CORE" "$TARGET" < "$TMP"
    rc=$?
    rm -f "$TMP"
    exit "$rc"
  fi
done

# ── 路径 C：flock 与 python 皆不可用 → 拒绝写（不静默降级）───────
fail 22 "no locking mechanism available (flock(1) and python both missing) — refusing to write unprotected: $TARGET"
