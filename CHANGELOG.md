# Changelog

## v1.0.2 — 2026-07-08

Fix: map data integrity — **append-only, never overwrite**. (Supersedes an earlier "jsonl-single-truth + regenerate md" draft, which risked losing md detail on regeneration and depended on python3.)

- **Root cause of the loss report**: a weaker model (Sonnet 4.6) wrote only `map.md` and skipped `map.jsonl`; the skill judged "empty" by `map.md`, so a cleared `map.md` looked like total loss.
- **New invariant — append-only (scoped to the permanent record)**: every write to `map.md` / `map.jsonl` is `>>`; never `>` / `open('w')` / `rm` / overwrite on those two. They only grow, so the skill itself can never destroy existing map data — an upgrade can't break records via the skill. (`pending.md` is a transient todo: added via `>>`; cleared only on explicit user confirmation, touching only pending — never the map.)
- **Both files first-class & append-only**: `map.md` = rich content (detail lives here, accumulates); `map.jsonl` = structured index (for cross-time 复验 scheduling). Neither is a derived view of the other → no regeneration → **no detail loss**.
- **第-1 步 reads BOTH** (tolerant of missing/empty/malformed; "empty" only when both empty). When `map.jsonl` is empty but `map.md` has content (Sonnet case), it **reads md — does NOT migrate or parse it lossy-ly**; jsonl is built going forward.
- **复验 = append a new round** (md entry + jsonl line); old entries kept as history; 第-1 步 dedups by 概念 (latest 上次复验 wins).
- **Drops the render script + python3 dependency** (was a forward-compat hazard + fragility). Pure bash now.
- **Security**: quoted, collision-resistant heredocs (`<<'GZX_JSONL_EOF'`/`<<'GZX_MD_EOF'`) → model-filled values can't inject shell (only residual risk: a value reproducing the long delimiter → heredoc ends early; near-zero, and affects only the new entry, never existing data); `chmod 700` on the data dir; all-local (no network); privacy 脱敏 rule preserved.
- **Compat**: backward — reads any existing state (md-only / jsonl-only / both / empty) without writing; forward — tolerates unknown jsonl fields (future versions may add), no rigid schema/script to break.

## v1.0.1 — 2026-07-07

Docs: clarify how to trigger the skill (no code/behavior change).

- Three-tier trigger guidance in README (zh/en): `/gan-zhong-xue` (most reliable) > specific phrase > vague.
- Note that `/干中学` is NOT a command — the skill name is ASCII `gan-zhong-xue`; say "干中学", don't type `/干中学`.
- SKILL.md description: add 掌舵 (mid-task) trigger phrases (「等等这步我没懂」/「为什么这么改」) for more reliable auto-triggering.
- docs/design.md: trigger section updated to match.

## v1.0.0 — 2026-07-06

First public release.

- Turn-based "guess → reveal → principle" loop, two modes: 掌舵 (steer mid-task) / 学习 (review after commit).
- Cold-read sub-agent to avoid answer-leak and author blind spots.
- Cross-session knowledge map at `~/.gan-zhong-xue/` (`map.md` + `map.jsonl`) with
  待复验 status — today's 🟢 means "understood today", not "truly understood".
- Privacy-first: no concrete values (chip models, registers, IPs, company names)
  ever written to map/pending/reports; fully local.
- Bilingual-friendly: skill body in Chinese, English trigger aliases, output follows
  the user's language.
- Honest limitation: all "it works" evidence is n=1 (designer-tested). Needs
  independent testers — that's the whole reason for this release.
