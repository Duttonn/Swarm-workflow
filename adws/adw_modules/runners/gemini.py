"""Gemini CLI (npm @google/gemini-cli) in stream-json, on the host or inside the swarm container."""
import json
import os
import sys
from pathlib import Path

from ..agy_swarm import (AgentRequest, gemini_path, spawn, terminate, watch,
                         write_gemini_settings)
from .common import deliver, usage


def gemini_env():
    """GEMINI_SANDBOX=docker fails with "Missing sandbox command 'docker'" unless docker's
    own bin directory is on PATH for the child - the same trap the sandbox module hit."""
    env = dict(os.environ)
    if os.environ.get('GEMINI_SANDBOX'):
        try:
            from ..docker_sandbox import docker_path
            bindir = str(Path(docker_path()).parent)
            if bindir not in env.get('PATH', ''):
                env['PATH'] = bindir + os.pathsep + env.get('PATH', '')
        except Exception as exc:
            print('gemini sandbox requested but docker not resolvable: %s' % exc,
                  file=sys.stderr)
    return env


def invoke(req: AgentRequest, messages, cancel):
    """Gemini CLI in stream-json. The result event carries stats but NO text: the reply
    arrives as assistant `message` events, so it has to be accumulated."""
    write_gemini_settings(req.folder)
    box = req.sandbox
    if box:
        from ..docker_sandbox import docker_env, docker_path
        # The key goes by name: docker exec copies it from this process's environment, so it
        # never appears in argv or in `docker inspect`. `timeout` runs inside the container
        # because killing the docker client does not kill what it started in there.
        argv = [docker_path(), 'exec', '-i', '-e', 'GEMINI_API_KEY', '-w', box.inside(req.folder),
                box.name, 'timeout', str(req.timeout), 'gemini']
    else:
        argv = [gemini_path()]
    argv += ['-m', req.model, '--yolo', '--skip-trust', '-o', 'stream-json']
    # Gemini scopes the workspace to cwd. Without this the board sits outside it, glob and
    # read_file are refused, and the agent wastes the turn working around the boundary with
    # run_shell_command (which the boundary does not stop - it is not a sandbox).
    for extra in req.shared:
        argv += ['--include-directories', box.inside(extra) if box else str(extra)]
    # The brief rides stdin; -p only keeps non-interactive mode on. Passing the whole prompt
    # in argv hit "The command line is too long." as soon as round 2 appended the mailbox.
    argv += ['-p', 'Follow the brief above exactly and end with the required json block.']
    with (req.folder / 'stderr.log').open('w', encoding='utf-8') as err:
        proc = spawn(argv, req, err, stdin=True, env=docker_env() if box else gemini_env())
        messages.put((req.agent, {'event': 'process_start', 'pid': proc.pid}))
        watch(proc, cancel, req.timeout)
        try:
            proc.stdin.write(req.prompt + '\n')
            proc.stdin.close()
        except OSError as exc:
            terminate(proc)
            raise RuntimeError('%s: could not send the brief: %s' % (req.agent, exc))
        reply, result = [], None
        with (req.folder / 'events.jsonl').open('w', encoding='utf-8') as raw:
            try:
                for line in proc.stdout:
                    raw.write(line)
                    raw.flush()
                    try:
                        event = json.loads(line.lstrip('\ufeff'))
                    except json.JSONDecodeError:
                        continue
                    kind = event.get('type')
                    if kind == 'message':
                        if event.get('role') != 'user':
                            reply.append(str(event.get('content') or ''))
                    elif kind == 'tool_use':
                        messages.put((req.agent, {'event': 'step_update', 'step_update': {
                            'step_type': 'tool', 'tool_name': event.get('tool_name', 'tool'),
                            'agent': req.agent, **event}}))
                    elif kind == 'init':
                        messages.put((req.agent, {'event': 'init', 'init': event}))
                    elif kind == 'result':
                        result = event
                proc.wait()
            finally:
                terminate(proc)
                messages.put((req.agent, {'event': 'process_end', 'pid': proc.pid,
                                          'exit_code': proc.returncode}))
    stats = (result or {}).get('stats') or {}
    # stats.input_tokens already includes the cached portion; total minus input is what
    # Google bills as output: the answer plus the thinking.
    inp = stats.get('input_tokens')
    total = stats.get('total_tokens')
    used = usage('gemini', req.model, total=total, input=inp, cached=stats.get('cached'),
                 output=None if total is None or inp is None else max(0, total - inp))
    error = None
    if not result or result.get('status') != 'success':
        error = 'gemini exit=%s status=%s' % (proc.returncode, result and result.get('status'))
    billed = list((stats.get('models') or {}).keys())
    # The silent-downgrade trap: fail loudly rather than quietly paying for a weaker model.
    if not error and billed and req.model not in billed:
        error = ('asked for %s but %s was billed - is experimental.dynamicModelConfiguration '
                 'set (and BOM-free)?' % (req.model, billed))
    text = ''.join(reply).strip()
    if not error and not text:
        error = 'produced no reply text'
    return deliver(req, text, used, error, stats=stats)
