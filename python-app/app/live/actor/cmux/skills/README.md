# Claude Code skills for the cmux agent

Skill files Claude Code reads from `~/.claude/skills/`, vendored here so a checkout carries them. `cmux-browser` is
cmux's own skill for its built-in browser (upstream: https://github.com/manaflow-ai/cmux/tree/main/skills/cmux-browser),
which the cmux agent's instructions point Claude at for web pages.

Install (copies them into `~/.claude/skills/` and creates the browser profile the agent uses):

    pnpm cmux:skills

Refresh from upstream:

    D=python-app/app/live/actor/cmux/skills/cmux-browser
    curl -fsSL https://raw.githubusercontent.com/manaflow-ai/cmux/main/skills/cmux-browser/SKILL.md -o $D/SKILL.md
    curl -fsSL https://raw.githubusercontent.com/manaflow-ai/cmux/main/skills/cmux-browser/references/commands.md -o $D/references/commands.md
