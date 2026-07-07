# Changelog

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
