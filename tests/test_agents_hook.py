"""The agent hook dispatcher (.agents/hook.py) on sample Claude Code and Codex payloads."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('agents_hook', ROOT / '.agents' / 'hook.py')
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)

# A package hook: denies `rm -rf /`, gives a playbook, records every event it sees.
PACKAGE_HOOK = r'''
import json, os
def agent_event(event):
    with open(os.environ['SEEN'], 'a') as out:
        out.write(json.dumps(event) + '\n')
    if event['event'] == 'start':
        return {'context': 'playbook for ' + event['agent']}
    if event['event'] == 'pre' and 'rm -rf /' in event['command']:
        return {'deny': 'use the trash verb'}
'''
SID = '3f2a9c1e-0000-4000-8000-000000000000'


def claude(name, **fields):
    return dict({'session_id': SID, 'transcript_path': '/t.jsonl', 'cwd': '/ws', 'permission_mode': 'default',
                 'hook_event_name': name, 'tool_name': 'Bash', 'tool_use_id': 'toolu_01',
                 'tool_input': {'command': 'ros2 node list', 'description': 'List nodes'}}, **fields)


def codex(name, **fields):
    return dict({'session_id': SID, 'turn_id': 'turn-1', 'transcript_path': None, 'cwd': '/ws', 'model': 'gpt-5',
                 'hook_event_name': name, 'tool_name': 'Bash', 'tool_use_id': 'call_1',
                 'tool_input': {'command': 'ros2 node list'}}, **fields)


class Dispatcher(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / '.agents').mkdir()
        self.add_package('robot_tools', PACKAGE_HOOK)
        self.seen = self.root / 'seen.jsonl'
        os.environ['SEEN'] = str(self.seen)
        os.environ['XDG_STATE_HOME'] = str(self.root / 'state')

    def run_hook(self, agent, payload):
        out = hook.run(agent, json.dumps(payload), self.root)
        return json.loads(out) if out else None

    def events(self):
        return [json.loads(line) for line in self.seen.read_text().splitlines()] if self.seen.exists() else []

    def test_deny_is_the_same_json_for_claude_and_codex(self):
        for agent, make in (('claude', claude), ('codex', codex)):
            out = self.run_hook(agent, make('PreToolUse', tool_input={'command': 'ssh robot rm -rf /'}))
            self.assertEqual(out, {'hookSpecificOutput': {
                'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                'permissionDecisionReason': 'use the trash verb'}})

    def test_start_returns_the_playbook_as_additional_context(self):
        out = self.run_hook('claude', {'session_id': SID, 'hook_event_name': 'SessionStart', 'source': 'startup'})
        self.assertEqual(out['hookSpecificOutput'], {'hookEventName': 'SessionStart',
                                                      'additionalContext': 'playbook for claude'})
        self.assertEqual(self.events()[0]['source'], 'startup')

    def test_allowed_commands_are_silent_and_reach_the_package_as_neutral_events(self):
        self.assertIsNone(self.run_hook('claude', claude('PreToolUse')))
        self.assertIsNone(self.run_hook('claude', claude('PostToolUse', duration_ms=2100,
                                                         tool_response={'stdout': '', 'interrupted': False})))
        self.assertIsNone(self.run_hook('codex', codex('PostToolUse', tool_response=json.dumps(
            {'output': 'x', 'metadata': {'exit_code': 0, 'duration_seconds': 0.4}}))))
        pre, post, codex_post = self.events()
        self.assertEqual((pre['agent'], pre['event'], pre['tool_id'], pre['command'], pre['exit']),
                         ('claude', 'pre', 'toolu_01', 'ros2 node list', None))
        self.assertEqual((post['event'], post['exit'], post['duration_ms'], post['failed']), ('post', 0, 2100, False))
        self.assertEqual((codex_post['agent'], codex_post['exit'], codex_post['tool_id']), ('codex', 0, 'call_1'))

    def test_failures_carry_the_exit_code(self):
        self.run_hook('claude', claude('PostToolUseFailure', error='Exit code 3\nboom', is_interrupt=False,
                                       duration_ms=4187))
        self.run_hook('claude', claude('PostToolUseFailure', error='spawn failed', is_interrupt=True))
        self.run_hook('codex', codex('PostToolUse', tool_response='Exit code: 2\nWall time: 0.1 seconds\n'))
        self.run_hook('codex', codex('PostToolUse', tool_response={'exit_code': 1}))
        self.run_hook('codex', codex('PostToolUse', tool_response={'stdout': 'no code'}))
        self.assertEqual([(e['exit'], e['failed']) for e in self.events()],
                         [(3, True), (None, True), (2, True), (1, True), (None, False)])
        self.assertEqual(self.events()[0]['duration_ms'], 4187)

    def test_agent_is_detected_when_not_named_and_argv_commands_are_joined(self):
        self.run_hook('', codex('PreToolUse', tool_input={'command': ['bash', '-lc', 'ros2 topic list | head']}))
        self.run_hook('codex', codex('PreToolUse', tool_input={'command': ['ros2', 'topic', 'echo', '/a b']}))
        self.run_hook('codex', codex('PreToolUse', tool_name='exec_command', tool_input={'cmd': 'ros2 node list'}))
        self.assertEqual(self.events()[0]['agent'], 'codex')
        self.assertEqual([e['command'] for e in self.events()],       # Codex's bash -lc script is the command
                         ['ros2 topic list | head', "ros2 topic echo '/a b'", 'ros2 node list'])

    def test_other_tools_and_events_are_ignored(self):
        self.assertIsNone(self.run_hook('claude', claude('PreToolUse', tool_name='Read', tool_input={'file_path': 'x'})))
        self.assertIsNone(self.run_hook('claude', claude('PostToolUse', tool_name='Edit', tool_input={'file_path': 'x'})))
        self.assertIsNone(self.run_hook('codex', codex('PostToolUse', tool_name='apply_patch', tool_input={'command': ''})))
        self.assertIsNone(self.run_hook('claude', claude('Stop')))
        self.assertEqual(self.events(), [])

    def test_edit_tools_reach_the_package_with_their_files(self):
        for tool, field in (('Edit', 'file_path'), ('Write', 'file_path'), ('MultiEdit', 'file_path'),
                            ('NotebookEdit', 'notebook_path')):
            self.run_hook('claude', claude('PreToolUse', tool_name=tool, tool_input={field: 'a/b.py', 'content': 'x'}))
        patch = ('*** Begin Patch\n*** Add File: new.py\n+x\n*** Update File: old.py\n*** Move to: moved.py\n@@\n-a\n+b\n'
                 '*** Delete File: gone.py\n*** End Patch\n')
        self.run_hook('codex', codex('PreToolUse', tool_name='apply_patch', tool_input={'command': patch}))
        self.run_hook('codex', codex('PreToolUse', tool_name='apply_patch', tool_input=patch))          # freeform
        self.run_hook('codex', codex('PreToolUse', tool_input={'command': "apply_patch <<'EOF'\n" + patch + 'EOF'}))
        self.run_hook('claude', claude('PreToolUse', tool_name='Write', tool_input={}))
        seen = self.events()
        self.assertEqual([e['edits'] for e in seen[:4]], [[['write', 'a/b.py']]] * 4)
        self.assertTrue(all(e['command'] == '' and e['event'] == 'pre' for e in seen[:6]))
        whole = [['write', 'new.py'], ['delete', 'old.py'], ['write', 'moved.py'], ['delete', 'gone.py']]
        self.assertEqual([e['edits'] for e in seen[4:7]], [whole] * 3)
        self.assertTrue(seen[6]['command'].startswith('apply_patch'))        # the shell rules still see it
        self.assertEqual(seen[7]['edits'], [['write', '']])                     # no path: the package fails closed
        self.assertNotIn('edits', self.run_hook('claude', claude('PreToolUse')) or {})
        self.assertNotIn('edits', self.events()[-1])

    def test_a_missing_or_broken_package_is_a_no_op(self):
        payload = claude('PreToolUse', tool_input={'command': 'rm -rf /'})
        script = self.root / 'src' / 'robot_tools' / 'robot_tools' / 'agent_hook.py'
        script.write_text('raise RuntimeError("bad")')
        self.assertIsNone(self.run_hook('claude', payload))
        self.assertIn('robot_tools pre', (self.root / 'state' / 'astro' / 'agents-hook.log').read_text())
        script.unlink()
        self.assertIsNone(self.run_hook('claude', payload))
        self.assertEqual(self.events(), [])

    def add_package(self, name, source):
        package = self.root / 'src' / name / name
        package.mkdir(parents=True)
        (package / 'agent_hook.py').write_text(source)

    def test_a_package_exit_skips_only_that_package(self):
        self.add_package('aa_exits', 'import sys\ndef agent_event(event):\n    sys.exit(2)\n')
        out = self.run_hook('claude', claude('PreToolUse', tool_input={'command': 'rm -rf /'}))
        self.assertEqual(out['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIn('aa_exits pre: SystemExit(2)', (self.root / 'state' / 'astro' / 'agents-hook.log').read_text())

    def test_a_spent_budget_skips_the_rest_but_keeps_a_deny(self):
        self.add_package('zz_slow', 'def agent_event(event):\n    raise AssertionError("ran past the budget")\n')
        ticks = iter([0.0, 0.1, 5.0])          # start, before robot_tools, before zz_slow
        deny, _ = hook.dispatch(hook.normalize('claude', claude('PreToolUse', tool_input={'command': 'rm -rf /'})),
                                self.root, budget_s=2.0, clock=lambda: next(ticks))
        self.assertEqual(deny, 'use the trash verb')
        self.assertIn('zz_slow pre: skipped', (self.root / 'state' / 'astro' / 'agents-hook.log').read_text())

    def test_runs_isolated_fast_and_always_exits_0(self):
        env = {'PATH': os.environ['PATH'], 'SEEN': str(self.seen), 'XDG_STATE_HOME': str(self.root / 'state'),
               'HOME': str(self.root)}
        script = self.root / '.agents' / 'hook.py'
        shutil.copy(ROOT / '.agents' / 'hook.py', script)
        cases = [(json.dumps(claude('PreToolUse', tool_input={'command': 'rm -rf /'})), 'deny'),
                 (json.dumps(claude('PreToolUse')), ''), ('not json', '')]
        for text, expect in cases:
            began = time.monotonic()
            done = subprocess.run([sys.executable, '-I', str(script), 'claude'], input=text, capture_output=True,
                                  text=True, env=env, cwd=str(self.root), timeout=10)
            self.assertEqual(done.returncode, 0)
            self.assertLess(time.monotonic() - began, 1.0)
            self.assertEqual(expect in done.stdout, True) if expect else self.assertEqual(done.stdout, '')
        playbook = subprocess.run([sys.executable, '-I', str(script), '--playbook'], capture_output=True,
                                  text=True, env=env, timeout=10)
        self.assertEqual(playbook.stdout.strip(), 'playbook for agent')
        self.assertEqual(self.events()[-1]['source'], 'playbook')     # a package can give the whole text here

    def test_shipped_configs_point_every_cli_at_the_dispatcher(self):
        claude_hooks = json.loads((ROOT / '.claude' / 'settings.json').read_text())['hooks']
        codex_hooks = json.loads((ROOT / '.codex' / 'hooks.json').read_text())['hooks']
        self.assertEqual(set(claude_hooks), {'SessionStart', 'PreToolUse', 'PostToolUse', 'PostToolUseFailure'})
        self.assertEqual(set(codex_hooks), {'SessionStart', 'PreToolUse', 'PostToolUse'})
        self.assertEqual([g['matcher'] for g in claude_hooks['PreToolUse']], ['Bash', 'Edit|MultiEdit|Write|NotebookEdit'])
        self.assertEqual([g['matcher'] for g in codex_hooks['PreToolUse']], ['^Bash$', '^apply_patch$'])
        for hooks, agent in ((claude_hooks, 'claude'), (codex_hooks, 'codex')):
            for groups in hooks.values():
                for handler in (h for g in groups for h in g['hooks']):
                    self.assertIn('.agents/hook.py', handler['command'])
                    self.assertIn('-I', handler['command'])
                    self.assertTrue(handler['command'].split('exit 0')[0].rstrip('; ').endswith(agent))


WORKSPACE_HOOKS = [name for name, _ in hook.packages(ROOT)]
# (blocked?, command): the workspace's own package hooks behind the real dispatcher, as each CLI sends it.
REGRESSION = [
    (True, 'ros2 topic pub /cmd_vel geometry_msgs/msg/Twist "{}"'),
    (True, 'ssh blastoise \'docker exec roboboat_dev bash -c "ros2 topic pub /cmd_vel x y"\''),
    (True, "ssh blastoise <<'EOF'\nros2 service call /mavros/cmd/arming x y\nEOF"),
    (True, 'echo "ros2 topic pub /goal_pose x y" | bash'),
    (True, 'kill -9 $(pgrep -f fence_watchdog)'),
    (True, 'ssh blastoise pkill -f boat_test'),
    (True, 'ssh blastoise docker stop roboboat_dev'),
    (True, 'pkill -P 12345'),                     # kill by exact PID
    (True, 'killall -u me codex'),
    (True, 'setsid -f ros2 topic pub /cmd_vel x y'),        # not a text-only command: whole-text search
    (True, 'ros2 run mavros mavsafety arm'),
    (False, 'kill 12345'),
    (False, "grep -rn 'ros2 topic pub /cmd_vel' src/"),
    (False, "rg -n 'pkill|killall' src | head"),
    (True, "git commit -m \"$(cat <<'EOF'\ndeny ros2 topic pub /cmd_vel and pkill\nEOF\n)\""),   # $(...)
    (True, "cat > test_x.py <<'EOF'\ndef test_kill():\n    proc.kill()  # watchdog\nEOF"),   # writes a file
    (False, "git commit -F - <<'EOF'\ndeny ros2 topic pub /cmd_vel and pkill\nEOF"),
    (False, 'ros2 topic echo /cmd_vel --once'),
    (False, 'git status')]


@unittest.skipUnless('mhseals_testing' in WORKSPACE_HOOKS, 'needs the workspace checkout of src/mhseals_testing')
class WorkspaceHooks(unittest.TestCase):
    """The regression table through ``python3 -I .agents/hook.py`` for Claude's and Codex's payloads
    (Codex may send the command as argv), and the per-call latency the agent waits for."""

    def test_blocks_raw_control_allows_text_and_stays_fast(self):
        state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, state)
        env = dict(os.environ, XDG_STATE_HOME=str(state), XDG_RUNTIME_DIR=str(state), MHSEALS_DATA=str(state))
        env.pop('BOAT_AGENT_TARGET', None)
        took = []
        for blocked, command in REGRESSION:
            for agent, payload in (('claude', claude('PreToolUse', tool_input={'command': command})),
                                   ('codex', codex('PreToolUse', tool_input={'command': command})),
                                   ('codex', codex('PreToolUse', tool_input={'command': ['bash', '-lc', command]}))):
                began = time.monotonic()
                done = subprocess.run([sys.executable, '-I', str(ROOT / '.agents' / 'hook.py'), agent],
                                      input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=10)
                took.append(time.monotonic() - began)
                out = json.loads(done.stdout) if done.stdout.strip() else {}
                decision = out.get('hookSpecificOutput', {}).get('permissionDecision')
                with self.subTest(agent=agent, command=command):
                    self.assertEqual(decision == 'deny', blocked, out)
        took.sort()
        self.assertLess(took[len(took) // 2], 0.15, 'median hook latency per tool call')

    def test_edit_tools_are_denied_on_live_safety_files_only(self):
        """Sample Claude Edit/Write and Codex apply_patch payloads end to end: a live agent.yaml (a main
        checkout), launch file or hold file is denied; the same files in a linked git worktree are allowed."""
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base)
        live, wt = base / 'live', base / 'wt'
        git = ['git', '-c', 'user.name=t', '-c', 'user.email=t@t', '-c', 'commit.gpgsign=false', '-C']
        (live / 'config').mkdir(parents=True)
        (live / 'config' / 'agent.yaml').write_text('hook: {}\n')
        for args in (['init', '-q'], ['add', '.'], ['commit', '-qm', 'x'], ['worktree', 'add', '-q', str(wt)]):
            subprocess.run(git + [str(live)] + args, check=True, capture_output=True)
        env = dict(os.environ, XDG_STATE_HOME=str(base), XDG_RUNTIME_DIR=str(base), MHSEALS_DATA=str(base))
        env.pop('BOAT_AGENT_TARGET', None)

        def patch(path):
            return '*** Begin Patch\n*** Update File: %s\n@@\n-a\n+b\n*** End Patch\n' % path
        for blocked, agent, payload in (
                (True, 'claude', claude('PreToolUse', tool_name='Edit', cwd=str(live), tool_input={
                    'file_path': str(live / 'config' / 'agent.yaml'), 'old_string': 'false', 'new_string': 'true'})),
                (True, 'claude', claude('PreToolUse', tool_name='Write', tool_input={
                    'file_path': str(base / 'run' / 'command_mux_7.json'), 'content': '{}'})),
                (True, 'codex', codex('PreToolUse', tool_name='apply_patch', cwd=str(live),
                                      tool_input={'command': patch('config/agent.yaml')})),
                (True, 'codex', codex('PreToolUse', tool_name='apply_patch', cwd=str(base),
                                      tool_input={'command': patch('share/launch/arm.launch.py')})),
                (False, 'claude', claude('PreToolUse', tool_name='Edit', tool_input={
                    'file_path': str(wt / 'config' / 'agent.yaml'), 'old_string': 'false', 'new_string': 'true'})),
                (False, 'claude', claude('PreToolUse', tool_name='Write', tool_input={
                    'file_path': str(base / 'agent' / 'stop'), 'content': ''})),          # creating the stop file
                (False, 'codex', codex('PreToolUse', tool_name='apply_patch', cwd=str(wt),
                                       tool_input={'command': patch('config/agent.yaml')}))):
            done = subprocess.run([sys.executable, '-I', str(ROOT / '.agents' / 'hook.py'), agent],
                                  input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=10)
            out = json.loads(done.stdout) if done.stdout.strip() else {}
            with self.subTest(agent=agent, payload=payload['tool_input']):
                self.assertEqual(out.get('hookSpecificOutput', {}).get('permissionDecision') == 'deny', blocked, out)


if __name__ == '__main__':
    unittest.main()
