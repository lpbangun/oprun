# oprun

Hermes skill for long-horizon software missions.

This chat is the **conductor**. Product work goes to **Pi** (default writer) or a bounded **Hermes coder** session. **Codevisor** advises or judges. No Beads, OMP, Droid, Codex, or `spawn-agent`.

## Install

User-local Hermes skill:

```bash
mkdir -p ~/.hermes/skills/autonomous-ai-agents
git clone https://github.com/lpbangun/oprun.git ~/.hermes/skills/autonomous-ai-agents/oprun
```

On a named profile (example: coder):

```bash
git clone https://github.com/lpbangun/oprun.git \
  ~/.hermes/profiles/coder/skills/autonomous-ai-agents/oprun
```

Then `/oprun <mission>` or natural language. First turn is intake unless already told to start. See `SKILL.md`.

## Layout

- `SKILL.md` — policy
- `scripts/dispatch.py` — one Hermes coder/codevisor worker, wait until exit
- `templates/` — `state.json`, `worker-brief.md`
