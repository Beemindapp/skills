# skills

Agent skills, one folder each. A skill is a `SKILL.md` an agent reads, plus whatever files it needs to run.

| Skill | What it does |
|---|---|
| [`timeplan`](timeplan/) | Turns output tokens and tool calls into wall-clock time, measured from real transcripts. Keeps the agent's clock apart from a person's. |

## Install

```
git clone https://github.com/Beemindapp/skills.git
cd skills
ln -s "$PWD/timeplan" ~/.claude/skills/timeplan
ln -s "$PWD/timeplan" ~/.pi/agent/skills/timeplan
```

Claude Code and Pi both pick skills up from those directories. Any other agent that reads a `SKILL.md` can use the
same folder.

## Adding a skill

One directory, one `SKILL.md` with `name` and `description` in the frontmatter. Keep helper files next to it and
refer to them by path, so the whole folder can be linked or copied on its own. Add a row to the table above.

## License

MIT. See [LICENSE](LICENSE).
