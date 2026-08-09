# Changelog

## v1.0.6 — 2026-08-10

**真机实证版：修"代码断言可信度"——这是 skill 迄今最严重的问题。**

- **痛点（真机实证，用户当场拆穿）** —— 主 agent 在逐节点下潜时，没有读过对应源文件行，
  凭通用内核知识推演"代码怎么工作"，包装成"基于真实代码"，还写进了文档。用户追问细节时
  兜圈子、换比喻、反复出题，直到用户说"你根本没读过代码？刚才都在乱猜骗我？"才被拆穿。
  浪费大量 token，严重破坏学习体验——**学习体验的根基是"AI 说的代码事实是真的"，这条破了，
  整个 skill 的信任就塌了**。
- **新增铁律10（代码断言铁律）**：任何关于"代码怎么工作"的断言，主 agent 必须在回答前读过
  对应的源文件行，并在回答中给出文件路径和行号。不能从冷读 sub-agent 的梯子里引用代码事实
  而不验证——冷读 sub-agent 只负责画图出题，**代码事实的核实责任在主 agent 身上**。
  没读过就说六个字："让我去读代码"。这是比"倒瀑布"更根本的失败模式：倒瀑布只是信息密度
  问题，代码虚构是**事实层面撒谎**。
- **自检 + 已知局限**：自检清单加"代码断言"项；已知局限诚实交代——铁律10 是文字约束，
  管得住意图、管不住模型的执行（模型天然分不清"通用知识推演"和"读过具体文件"），
  最后防线只剩用户拆穿。不够好，但比没有强。

## v1.0.5 — 2026-08-03

深度 & 心流 + 一条诚实修正。针对两个用户实测痛点，经三轮独立 review 后硬化。

- **痛点1（挖得不够深）** —— v1.0.4 修了结构、没修裁判。v1.0.5：第1步 sub-agent 为每节点多产出一道**底部验证题**（换领域迁移题，答案禁入梯子）；第2步到底判据从"模型读三面红旗"升级为"**用户答底部验证题**"。**诚实上限**：裁判链未真正终结——题是 sub-agent 出的，题软则裁判失灵；这把"裁判可靠性"从"读旗"挪到"出题质量"，没消灭问题、换了位置。
- **痛点2（边挖边冒新问题）** —— v1.0.4 第4步只管"被打断去干活"。v1.0.5 第2步加**接岔路分支**：A 类【前置缺口】→ 插回图当新前置节点（**插一挖一 + 连锁封顶 2/轮** 防发散）；B 类【换场好奇】→ **不丢 pending、当场升级为迁移题考**（这正是痛点1底部验证题、用户自己送上门的版本——统一机制，见 design.md）。B 类补红线：**接住好奇在前、递题在后 + 低信任给脚手架**，防考官腔。
- **新增铁律9（详略得当）**：详略由用户产出质量驱动。**硬约束**：略某级 ≠ 判节点掌握（节点到底仍只由底部验证题决定）；主节点略只准发生在验证题答出后；**略 ≠ 收尾**（防和铁律8打架）。"详"是块厚不是块多——仍一块一停。
- **自指认路径补丁**：用户自指认靶点时，底部验证题**仍强制开 sub-agent 出**（主模型是出题禁区，防漏答案）。
- **增强（验证题入地图）**：底部验证题入地图字段（md + jsonl），第-1步跨天复验复用同一道题。**代价**：破坏了"只动协议不动存储"的承诺——第3步写入格式加了字段。但 **append-only 铁则不破**（仍纯 `>>`，不覆盖/不删）；旧条目无此字段向后兼容（第-1步读到无字段就退回旧行为）。
- **复验取题 + 引号防线（修取题缺陷 + 主路径失效率）**：① 第-1步复验取题定义**三段决策序列**（jsonl 行可解析→取；新格但行坏→md 定位取；旧格/都没有→临时出题）。v1.0.4 无验证题字段、复验时临时出题；v1.0.5 新增字段后，草案期全文件 grep 取题会命中所有格子且 `-A 1` 取错行——定稿禁绝，三段序列一次定义对。② **验证题字段引号防线**：题面单行写、英文双引号转义或换中文引号、**写入前自查**（扫一眼值里有无裸英文双引号——不是写入后 grep，grep 只证行里有字样、证不了可解析，是空转自检，已删）。否则验证题（引号率最高的长文本）写坏 jsonl 行，复验主路径名存实亡。
- **B 类选题顺序 + 两题衔接**：用户场景≈预生成题用预生成题、不一致用用户自己的题（不算主模型现编），但**到底判据仍只认预生成题**（用户题答出 ≠ 到底）。两道都考时，**两题之间措辞自然过渡**（"你问的正是这条定律的另一面"），预生成题答不出回梯子时接住（铁律5），防"答了两道还往下挖"的连环考试感。
- **sub-agent 出题后自核**：题与梯子逐句比对，挡换皮复述题——仍是自证，诚实交代只挡最弱的题。
- **铁律9"详"措辞**：防被读成"一次讲三段"（=倒瀑布），点明"详是块厚不是块多、仍一块一停"。

- **自检**：v1.0.5 新增 12 条（客观到底裁判、接岔路、详略、验证题来源、图膨胀收敛、B类次序、略≠收尾、验证题入地图、B类选题顺序、复验取题三段序列、验证题引号防线、B类两题衔接），自检总条数 27。**新增负担诚实交代**：见已知局限"自检清单膨胀"，趋势上未来分档（必答关键项 vs 可选核对项），这版不修。

- **局限段重写**：把"裁判链未终结"钉为头条；新增图膨胀硬约束是经验值、B 类考官腔是第一翻车点、自检膨胀、自核仍是自证、引号防线压不根治等诚实交代。

**数据安全铁则未变**（map 仍纯 `>>`、append-only）；**存储格式有变**（第3步加"验证题"字段，向后兼容）。**仍是 n=1**：新机制 + 增强全是设计推演、零真人跑过——独立测试更紧迫。

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
