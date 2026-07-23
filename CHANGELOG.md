# Changelog

## v1.0.4 — 2026-07-24

Depth: fix "digs too shallow — asks 2-3 questions then wraps up." Field report: a subsystem-level goal ("how is this kernel repo selected & built, which .bb/config files are involved, what's the principle") got compressed into one point and closed after three questions.

- **Root cause (structural, not laziness)**: the lesson had only a *one-point* shape. (a) The cold-read sub-agent returned "one point + one apex principle + one question" — a flat structure that can only grow a ~3-question lesson; the depth ceiling was welded in before digging started. (b) Termination was "user restates the principle" — stops at the first sign of satisfaction, the shallowest layer. (c) Success was defined as "even one point," making the pass-bar the target. (d) No depth floor; "少就是多" was over-applied — the model read "small blocks" (right) as "few blocks" (wrong) and retreated into early wrap-up.
- **Fix (borrows gstack's PLAN≠EXECUTE / anti-skip / push-twice / explicit depth criteria)**:
  - **第1步 sub-agent now returns an agenda graph + per-node descent ladder**, not one point. The graph is a concept-dependency decomposition sized to the target (2-3 nodes for a narrow question, 6-10 for a subsystem goal); each node carries a 3-6 rung "why" ladder down to a **cross-domain transferable law**. A subsystem learning goal is now a legal target that gets a graph, not a single point.
  - **第2步 walks the graph node-by-node**; each node has a bedrock gate (3 red flags: still reciting what-the-code-did / principle only fits this code / user hasn't been surprised) and must push ≥1 layer past the first "got it."
  - **Termination = every agenda node dug-to-bedrock OR explicitly dropped by the user** (no silent skip); "done" = the user can **transfer** the law to a different domain, not restate it.
  - **New iron law 8** (depth floor + anti-early-exit); "少就是多" scoped to *block size*, not *lesson depth*.
  - **Success redefined**: one genuine insight = pass-bar; a transferable law + full agenda coverage = target.
  - **Release valve preserved**: user can tap out → unfinished nodes go to `pending.md`; trust-gate (iron law 7) still prevents over-pushing. Anti-skip targets *AI silently omitting*, never *the user choosing to stop*.
- **No data-safety change**: 第3步 map write path untouched — still append-only (`>>`), never overwrite.
- **Honest ceiling**: depth now depends on (a) the model being strong enough to run the fuller protocol and (b) the sub-agent producing a good graph; still n=1 (author-validated), independent testers remain the missing experiment.

## v1.0.3 — 2026-07-08

Perf: bound 第-1步 read cost — stop catting the bulky `map.md` every run.

- **Problem**: 第-1步 cat'd BOTH `map.md` (rich, bulky) and `map.jsonl` every run. Both grow unboundedly (append-only) → at hundreds of rounds, each skill-run dumped tens of thousands of tokens; `map.md` is the dominant cost.
- **Fix**: 第-1步 now cats only the thin index `map.jsonl` each run (enough for hit-detection + 复验 scheduling). `map.md` is read **on-demand** — grep just the cell being worked (e.g., `grep -A 8 "^## <概念>" map.md`), never wholesale. Cuts per-run variable cost ~60-70% (drops the bulky md).
- **Legacy fallback**: if jsonl is empty but md has content (Sonnet-style md-only case), it cats md each run until the first jsonl line is written. Once jsonl is non-empty, legacy md-only cells aren't auto-surfaced (data not lost — still in md, grep if needed); going forward all cells go to jsonl.
- **No data-safety change**: still append-only; this is a read-strategy change only — writes untouched.
- **Honest ceiling**: thin jsonl still grows linearly; at thousands of rounds it'd need selective read (sample 复验-due + recent) or archival. Deferred (少就是多; revisit when a real map gets huge).

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
