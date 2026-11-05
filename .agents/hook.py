#!/usr/bin/env python3
"""One hook for every coding agent: Claude Code and Codex call this; workspace packages decide.

    python3 -I .agents/hook.py claude|codex      # a hook: the agent CLI's event JSON on stdin
    python3 -I .agents/hook.py --playbook [claude|codex]   # print the full playbooks as text

A package opts in with an ``agent_hook.py`` module at ``src/<repo>/<package>/agent_hook.py``
that defines ``agent_event(event)``. Each one gets the same neutral event, whichever CLI sent it:

    {agent: claude|codex, event: start|pre|post, session_id, agent_id, cwd, source,
     tool, tool_id, command, exit, duration_ms, failed}

plus, before a file edit (Claude Edit, MultiEdit, Write, NotebookEdit; Codex apply_patch, or a shell
call that carries an apply_patch text), ``edits``: ``[[write|delete, path as given]]`` (relative to
``cwd``). An edit tool's event has an empty ``command``; a patch no file header names gives ``[]``.
Only ``pre`` is sent for edit tools.

``agent_id`` is the payload's (a Claude subagent's own id; its ``session_id`` is the parent's), else ''.

``source`` at start is the CLI's (startup, resume, clear, compact) or ``playbook`` for
``--playbook``: a package can keep its session-start text short and give the whole of it on demand.

``exit`` is 0 on success, N for ``Exit code N``, else None (interrupted, or not reported).
It returns None, ``{"context": text}`` (start) or ``{"deny": reason}`` (pre). The first deny
blocks the call; contexts are joined. Stdlib only, Python 3.8, no ROS: well under 50 ms. Exits 0
always (a deny is JSON, not an exit code): a missing, broken or slow package never blocks a call;
its error goes to ``$XDG_STATE_HOME/astro/agents-hook.log``.

The canonical copy lives in the team's agent-loop repo (``src/<repo>/hooks/hook.py``); when the
workspace has exactly one, ``main`` hands the call to it (``canonical``). This file's own logic is the
fallback: no checkout, a broken one, or one the package hooks don't guard the way they guard
this file (delegating to an unguarded file would let an agent edit the rules away). Whichever
answers, a session start's context leads with the agent-loop line (``loop_entry``): every
``src/<repo>`` with that hook and an executable ``start`` is named, with its ``start`` to run first.
"""

import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = 'src/*/hooks/hook.py'      # the agent-loop repo's copy of this hook
LOOP_START = 'start'                   # its executable entry point for a fresh session
EVENTS = {'SessionStart': 'start', 'PreToolUse': 'pre', 'PostToolUse': 'post', 'PostToolUseFailure': 'post'}
SHELL_TOOLS = ('Bash', 'shell', 'local_shell', 'exec_command')   # Codex reports Bash; older builds the rest
SHELLS = ('bash', 'sh', 'zsh')
EDIT_TOOLS = {'Edit': 'file_path', 'MultiEdit': 'file_path', 'Write': 'file_path', 'NotebookEdit': 'notebook_path'}
PATCH_TOOLS = ('apply_patch',)      # Codex: tool_input.command is the patch text (matched as apply_patch|Edit|Write)
PATCH = re.compile(r'^\*\*\* (Add File|Update File|Delete File|Move to): *([^\r\n]+?)\s*$', re.M)   # CRLF too
EXIT = re.compile(r'^Exit code:?\s*(-?\d+)', re.M)
BUDGET_S = 2.0      # all packages together; the CLIs' hook timeout is 5 s


def log(text):
    base = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state') / 'astro'
    try:
        base.mkdir(parents=True, exist_ok=True)
        with open(base / 'agents-hook.log', 'a') as handle:
            handle.write('%s %s\n' % (time.strftime('%Y-%m-%dT%H:%M:%S'), text))
    except OSError:
        pass


def packages(root=ROOT):
    """``[(package name, agent_hook.py path)]`` of every package hook, in path order."""
    return [(path.parent.name, path) for path in sorted(root.glob('src/*/*/agent_hook.py'))]


def _exit_of(response):
    """The exit code a Codex PostToolUse ``tool_response`` reports, or None."""
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            match = EXIT.search(response)
            return int(match.group(1)) if match else None
    if isinstance(response, dict):
        for holder in (response, response.get('metadata') or {}):
            for key in ('exit_code', 'exitCode'):
                if isinstance(holder.get(key), int):
                    return holder[key]
        return _exit_of(response.get('output')) if isinstance(response.get('output'), str) else None
    return None


def patch_edits(text):
    """``[[write|delete, path]]`` of an apply_patch text: Add and Update write, Delete deletes, and a
    Move deletes the updated file and writes the new one."""
    edits = []
    for verb, path in PATCH.findall(text):
        if verb == 'Move to':
            edits[-1:] = [['delete', edits[-1][1]]] if edits else []
        edits.append(['delete' if verb == 'Delete File' else 'write', path])
    return edits


def normalize(agent, raw):
    """The neutral event of one CLI hook payload, or None when no package cares about it."""
    kind = EVENTS.get(raw.get('hook_event_name'))
    if kind is None:
        return None
    event = {'agent': agent, 'event': kind, 'session_id': str(raw.get('session_id') or ''),
             'agent_id': str(raw.get('agent_id') or ''),
             'cwd': raw.get('cwd'), 'source': raw.get('source'), 'tool': raw.get('tool_name'),
             'tool_id': raw.get('tool_use_id'), 'command': '', 'exit': None, 'duration_ms': None,
             'failed': False}
    if kind == 'start':
        return event
    given = raw.get('tool_input')
    if raw.get('tool_name') in EDIT_TOOLS or raw.get('tool_name') in PATCH_TOOLS:
        if kind != 'pre':
            return None
        if raw['tool_name'] in EDIT_TOOLS:
            event['edits'] = [['write', str((given or {}).get(EDIT_TOOLS[raw['tool_name']]) or '')]]
        else:       # a dict with command (or patch/input), or the freeform patch text itself
            text = given if isinstance(given, str) else next(
                (str(given[k]) for k in ('command', 'patch', 'input') if isinstance(given, dict) and given.get(k)), '')
            event['edits'] = patch_edits(text)
        return event
    if raw.get('tool_name') not in SHELL_TOOLS:
        return None
    command = (raw.get('tool_input') or {}).get('command') or (raw.get('tool_input') or {}).get('cmd')
    if isinstance(command, list) and len(command) == 3 and os.path.basename(str(command[0])) in SHELLS \
            and command[1] in ('-c', '-lc'):
        command = command[2]            # Codex's ['bash', '-lc', script]: the script is the command
    event['command'] = shlex.join(map(str, command)) if isinstance(command, list) else str(command or '')
    if kind == 'pre' and '*** Begin Patch' in event['command']:     # Codex's apply_patch through the shell
        event['edits'] = patch_edits(event['command'])
    if kind == 'post':
        took = raw.get('duration_ms')
        event['duration_ms'] = took if isinstance(took, (int, float)) else None
        if raw.get('hook_event_name') == 'PostToolUseFailure':      # Claude: the call failed
            match = EXIT.match(str(raw.get('error') or ''))
            event.update(failed=True, exit=int(match.group(1)) if match else None)
        elif agent == 'claude':                                     # Claude: PostToolUse is success
            event['exit'] = 0
        else:                                                       # Codex: one event, read the code
            event['exit'] = _exit_of(raw.get('tool_response'))
            event['failed'] = event['exit'] not in (0, None)
    return event


def _load(name, script):
    spec = importlib.util.spec_from_file_location('agents_hook_' + re.sub(r'\W', '_', name), script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.agent_event


def dispatch(event, root=ROOT, budget_s=BUDGET_S, clock=time.monotonic):
    """Every package's answer to ``event``: ``(deny reason or None, [contexts])``.

    Packages run in order until ``budget_s`` is spent; the rest are skipped (and logged), and a
    deny already collected still stands. A package's error, even ``sys.exit``, skips only it.
    """
    deny, contexts = None, []
    ends = clock() + budget_s
    for name, script in packages(root):
        if clock() > ends:
            log('%s %s: skipped, the %.1f s budget is spent' % (name, event['event'], budget_s))
            continue
        try:
            answer = _load(name, script)(dict(event)) or {}
            answer = answer if isinstance(answer, dict) else {}
        except BaseException as error:      # a package hook never breaks the agent's tool call
            log('%s %s: %r' % (name, event['event'], error))
            continue
        if answer.get('deny') and deny is None and event['event'] == 'pre':
            deny = str(answer['deny'])
        if answer.get('context') and event['event'] == 'start':
            contexts.append(str(answer['context']))
    return deny, contexts


def render(event, deny, contexts):
    """The stdout both CLIs read: the same hookSpecificOutput schema."""
    if deny:
        return json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                                  'permissionDecisionReason': deny}})
    if contexts:
        return json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart',
                                                  'additionalContext': '\n\n'.join(contexts)}})
    return ''


def run(agent, text, root=ROOT):
    """The hook's stdout for one payload (may be empty)."""
    raw = json.loads(text or '{}')
    if agent not in ('claude', 'codex'):
        agent = 'codex' if 'turn_id' in raw else 'claude'
    event = normalize(agent, raw) if isinstance(raw, dict) else None
    return render(event, *dispatch(event, root)) if event else ''


def _edit_denied(path, root):
    probe = normalize('claude', {'hook_event_name': 'PreToolUse', 'tool_name': 'Edit', 'cwd': str(root),
                                 'tool_input': {'file_path': str(path)}})
    return dispatch(probe, root)[0] is not None


def canonical(root=ROOT):
    """The agent-loop repo's hook module to hand calls to, or None to use this file's logic.

    Only for exactly one copy, and only when the package hooks deny editing it, or deny editing
    neither it nor this file."""
    found = sorted(root.glob(CANONICAL))
    if len(found) > 1:
        log('canonical hook: %d copies (%s), using the built-in logic' % (len(found), ', '.join(map(str, found))))
    if len(found) != 1 or found[0].resolve() == Path(__file__).resolve():
        return None
    path = found[0]
    if not _edit_denied(path, root) and _edit_denied(Path(__file__).resolve(), root):
        log('canonical hook %s: not guarded like .agents/hook.py, using the built-in logic' % path)
        return None
    spec = importlib.util.spec_from_file_location('agents_hook_canonical', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def respond(args, text, root=ROOT):
    """The hook's stdout for command-line ``args`` and stdin ``text``."""
    if args[:1] == ['--playbook']:
        start = normalize(args[1] if len(args) > 1 else 'agent',
                          {'hook_event_name': 'SessionStart', 'source': 'playbook'})
        return '\n\n'.join(dispatch(start, root)[1])
    return run(args[0] if args else '', text, root)


def loops(root=ROOT):
    """Agent-loop repos: ``src/<repo>`` with the canonical hook and an executable ``LOOP_START``."""
    return sorted(path.parent.parent for path in root.glob(CANONICAL)
                  if os.access(path.parent.parent / LOOP_START, os.X_OK))


def loop_entry(root=ROOT):
    """The session-start line naming where orchestration starts, or '' with no agent-loop repo."""
    found = [str(repo.relative_to(root)) for repo in loops(root)]
    if not found:
        return ''
    starts = ' or '.join('`%s/%s`' % (repo, LOOP_START) for repo in found)
    return ('Agent loop: %s. Before orchestrating or picking up new work, run %s (it prints the role, '
            'routing, runbooks and handoff for a fresh session); repo-only edits need none of it.'
            % (', '.join(found), starts))


def with_loop_entry(args, text, out, root=ROOT):
    """``out`` with the agent-loop line first in a session start's context (hook JSON or --playbook)."""
    entry = loop_entry(root)
    if not entry:
        return out
    if args[:1] == ['--playbook']:
        return '\n\n'.join(part for part in (entry, out) if part)
    raw = json.loads(text or '{}')
    if not isinstance(raw, dict) or raw.get('hook_event_name') != 'SessionStart':
        return out
    answer = json.loads(out) if out else {}
    spec = answer.get('hookSpecificOutput') or {}
    context = '\n\n'.join(part for part in (entry, spec.get('additionalContext')) if part)
    return json.dumps({'hookSpecificOutput': {**spec, 'hookEventName': 'SessionStart',
                                              'additionalContext': context}})


def main(argv=None, root=ROOT):
    args = sys.argv[1:] if argv is None else argv
    out = None
    try:
        text = '' if args[:1] == ['--playbook'] else sys.stdin.read()
        try:
            module = canonical(root)
            out = module.respond(args, text, root) if module else None
        except BaseException as error:      # a broken canonical copy falls back to this file
            log('canonical hook: %r' % (error,))
        if out is None:
            out = respond(args, text, root)
        try:
            out = with_loop_entry(args, text, out, root)
        except BaseException as error:      # the pointer never costs the hook its answer
            log('loop entry: %r' % (error,))
    except BaseException as error:
        log('dispatch: %r' % (error,))
        out = ''
    if out:
        print(out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
