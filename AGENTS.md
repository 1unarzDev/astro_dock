# Agents

This workspace drives real robots. Claude Code (`.claude/settings.json`) and Codex
(`.codex/hooks.json`) run `.agents/hook.py`, which loads each workspace package's hook
(`src/<repo>/<package>/agent_hook.py`). At session start the hook gives you that package's playbook, and it blocks
commands that bypass the robot's controls.

- If no playbook arrived at session start (for example, Codex hooks not yet trusted with `/hooks`),
  run `python3 .agents/hook.py --playbook` and follow it before you touch a robot.
- A blocked command's reason names the command to use instead. Use that command; don't work
  around the block.
