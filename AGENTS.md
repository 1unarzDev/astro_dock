# Agents

astro_dock is a general robotics workspace: keep it abstracted and agnostic to any one team, robot, network or
machine. Team- and machine-specific facts belong in the team's repos and profiles under `src/`, never in astro_dock.

This workspace drives real robots. Logic lives in `.agents/`; `.claude/` and `.codex/` only wire each tool to it.
`.agents/hook.py` loads each workspace package's hook (`src/<repo>/<package>/agent_hook.py`): at session start
it says how to reach the robot, and before each shell command it blocks the commands that bypass the robot's
controls (only commands that actually run: text in a grep, an echo, a commit message or a file is fine).

- Before your first robot command, run `python3 .agents/hook.py --playbook` and follow it. Repo-only work
  needs none of it.
- A blocked command's reason names the command to use instead. Use that command; don't work around the block.
- A team's agent-loop repo is any `src/<repo>` with `hooks/hook.py` and an executable `start`. Before
  orchestrating or picking up new work, run its `start` first: it prints the role, routing, runbooks and
  handoff for a fresh session. The session-start hook names it (`Agent loop: ...`). `.agents/hook.py` runs
  that repo's canonical hook as long as the package hooks guard it like this one.
- Codex runs project hooks only once they are trusted: open Codex in this workspace and approve them with
  `/hooks` (again after `.codex/hooks.json` changes). Until then Codex has no block, only this file.
