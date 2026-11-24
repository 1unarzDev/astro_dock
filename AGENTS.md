# Agents

astro_dock is a general robotics workspace: keep it abstracted and agnostic to any one team, robot, network or
machine. Team- and machine-specific facts belong in the team's repos and profiles under `src/` and `profiles/`,
never in astro_dock.

This workspace drives real robots. Act on a robot with the same commands a person uses: plain ROS 2
(`ros2 launch`, `ros2 run`, `ros2 param set`, `ros2 topic pub`) or `astro` (`astro help` lists every command).
Log what you do on a robot with `astro mark "text"` so it lands in the day's log and the bag.

`.agents/hook.py` loads each workspace package's hook (`src/<repo>/<package>/agent_hook.py`), if any. Logic
lives in `.agents/`; `.claude/` and `.codex/` only wire each tool to it. Codex runs project hooks only once they
are trusted (`/hooks` in Codex).
