# Agents

This workspace drives real robots. Logic lives in `.agents/`; `.claude/` and `.codex/` only wire each tool to it.
`.agents/hook.py` loads each workspace package's hook (`src/<repo>/<package>/agent_hook.py`): at session start
it says how to reach the robot, and before each shell command it blocks the commands that bypass the robot's
controls (only commands that actually run: text in a grep, an echo, a commit message or a file is fine).

- Before your first robot command, run `python3 .agents/hook.py --playbook` and follow it. Repo-only work
  needs none of it.
- A blocked command's reason names the command to use instead. Use that command; don't work around the block.
- Codex runs project hooks only once they are trusted: open Codex in this workspace and approve them with
  `/hooks` (again after `.codex/hooks.json` changes). Until then Codex has no block, only this file.
