# 干中学 (Learning by Doing)

> Cures **vibe-coding**: your shipped output keeps growing, but your understanding doesn't keep up.

You ship fast with Claude Code. Some of what you shipped, you let the AI do hands-off and never truly understood — even though it explained while working. This skill doesn't re-explain. It takes **a real change you were present for** and walks you, turn by turn, from "I recognize it" to "I actually understand it, can restate it, and can transfer it."

It's a **cognitive gym, not an answer bot.** It makes you guess first, pushes you to think, and doesn't hand you the answer — **the effort is the feature, not a bug.**

> The skill body is in Chinese (its triggers, its philosophy anchors like 绝知此事要躬行 / "to truly know it, you must do it yourself"). Claude follows those rules in any language — **talk to it in English, it answers in English.**

---

## Try it (30 seconds)

```
/plugin marketplace add ly8427/gan-zhong-xue
/plugin install gan-zhong-xue@gan-zhong-xue
```

Then, mid-task or after a commit — **three ways to trigger** (most → least reliable):

- **Most reliable**: type `/gan-zhong-xue` (slash commands match the ASCII name only; `/干中学` won't fire)
- **Natural**: say something specific: "干中学" / "help me actually understand what you just did" / "I can't explain this commit"; mid-task works too: "wait, I didn't follow that step" / "why did you change it this way"
- ⚠️ Vague "I don't get it, explain" may not trigger — this skill needs the intent "understand a real change I was part of", not a generic code question

**Use a strong model** (Opus-class). Weaker models dump answers and ask stiffly.

⚠️ **Be honest when you don't know** — that's the signal it catches you with. Faking it just corrupts your own map.

---

## What makes it different from "open another session and ask AI to explain the repo"

**The product is a *map*, not a *lesson*.** Every round writes one cell to a cross-session, persistent map (`~/.gan-zhong-xue/`), with a timestamp and a "needs re-verification" status. 🟢 only means "understood today" — real understanding is earned later, when the map catches you on the same concept and you pass a transfer question. Without the map, this degrades into a one-off smart chat.

**Two modes**: *Steering* (invoke mid-task to steer direction) — payoff is immediate, most natural. *Learning* (review after a commit).

**Cold-read sub-agent**: when picking what to dig into, it spawns an outsider sub-agent that only sees the final diff — not the dev conversation — to avoid author blind spots and leaking the answer into the question.

---

## Privacy (hard gate)

Runs on your **real working code** (possibly company code). Hard rules:
- The map, pending queue, and any summary **never contain concrete values** (chip models, register addresses, IPs, keys, company/product names, internal paths) — only abstracted concepts.
- **Everything stays local at `~/.gan-zhong-xue/`. Never sent anywhere.**

---

## Honest limitations (please read)

- **All evidence that "it works" is n=1 (designer-tested).** To actually falsify it, independent testers are needed — **that's the one experiment missing before public release, and the gate is real humans, not code.** This is published precisely to find those testers. You.
- Judgment-call abysses ("which `if` to change", "will this architecture rot in 3 months") have no in-the-moment judge — truth comes from time and production consequences. It doesn't pretend to grade them; it only trains you to honestly face what you do and don't understand.

**Your feedback is the only fuel for improving it.** See below.

---

## Feedback

After a round, please open an issue using the [feedback template](.github/ISSUE_TEMPLATE/feedback.md) and tell me three things — especially the sentence "what I didn't see coming about this change was ___." That's the only success signal it cares about.

- [中文 README（主入口）](README.md)
- [A real round (example)](examples/real-round-1.md)
- [Design notes](docs/design.md)

---

## License

MIT.
