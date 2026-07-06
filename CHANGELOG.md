# Changelog

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
