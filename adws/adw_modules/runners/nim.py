"""NVIDIA NIM (integrate.api.nvidia.com), an OpenAI-compatible chat endpoint.

Every other runner here drives a coding-agent CLI that already owns the tool loop. NIM is a
raw chat API, so this module IS the loop: it streams SSE, executes the tool calls the model
asks for inside req.folder, feeds the results back, and stops when a turn arrives with no
tool call left. That is the whole reason it exists - the free CLI gateways (opencode Zen,
kilo, cline) each went dark or hit a daily cap on 2026-09-14, and a swarm cannot be measured
on a provider that answers exit=1 with no text.

Verified 2026-09-15 with bench/check_runner.py. Models: moonshotai/kimi-k3 (default, but it
queues ~320 s and the gateway 504s at ~300 s), nvidia/nemotron-3-super-120b-a12b (18 s, the
workhorse). SWARM_NIM_EFFORT sets reasoning_effort, empty to omit.

What the first night of runs taught this loop (bench/providers-2026-09-14.md):
  - write_file alone makes a model oscillate: it wrote a 14 KB body, then a 1.3 KB block
    over it, then the body again, 60 times. Hence edit_file, append, and a REPLACED notice.
  - the free tier answers an overloaded backend as a 200 whose only data line is an error
    object, and rate-limits at about 40 requests a minute per key. Hence the in-band error
    check, the retry ladder, and one process-wide throttle shared by every agent thread.
  - the endpoint queues a request behind whatever the GPUs are doing, so the first byte can
    be minutes away: CONNECT_TIMEOUT covers that wait, READ_TIMEOUT only trips once the
    stream has gone quiet mid-answer.
  - the model sometimes narrates instead of acting ("wrote server.js", zero tool calls) and
    sometimes starts a server in the foreground. Hence the nudge-then-fail rule and a
    run_command that kills the process tree and never waits on an orphaned pipe.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..agents import _extract_json
from ..agy_swarm import AgentFailed, AgentRequest, assign, close_job, new_job, terminate
from .common import deliver, tool_event, usage


def has_envelope(text):
    """Whether a final text carries the JSON object the harness will parse."""
    try:
        _extract_json(text or '')
        return True
    except Exception:
        return False

BASE = os.environ.get('NVIDIA_BASE_URL', 'https://integrate.api.nvidia.com/v1')
CONNECT_TIMEOUT = int(os.environ.get('SWARM_NIM_CONNECT_TIMEOUT', '900'))
READ_TIMEOUT = int(os.environ.get('SWARM_NIM_READ_TIMEOUT', '300'))
MAX_TOKENS = int(os.environ.get('SWARM_NIM_MAX_TOKENS', '16384'))
MAX_STEPS = int(os.environ.get('SWARM_NIM_MAX_STEPS', '60'))
EFFORT = os.environ.get('SWARM_NIM_EFFORT', 'high')
COMMAND_TIMEOUT = int(os.environ.get('SWARM_NIM_COMMAND_TIMEOUT', '180'))
OUTPUT_LIMIT = 8000          # what one command result may add to the context
READ_LIMIT = 65536           # a whole deliverable: an 8 KB cap on a 20 KB file made the model
                             # believe the file was cut off and rewrite it from scratch, 60 times
# NIM's free tier is 40 requests a minute per account, "dependent on model, use-case and current
# overall traffic" (forums.developer.nvidia.com/t/368420): one throttled agent still met 429s at
# 37 RPM, and five unthrottled owners drew 39 x HTTP 429 and 180 in-band 503 in one night.
MIN_INTERVAL = float(os.environ.get('SWARM_NIM_MIN_INTERVAL', '2.5'))   # 24 RPM: the 40 is nominal, traffic-dependent
NUDGES = 2
NUDGE = ('You answered without calling a single tool, so nothing you describe exists on disk: '
         'no file was read, written or run. Do the work now with write_file, edit_file, '
         'read_file and run_command, then give your final answer again.')
# deepseek-v4.1-flash, kanban solo: seven write_file calls, then the text "Now swimlanes,
# assignees, exporter, importer, metrics." and a stop. The file ended mid-function and the
# turn had no envelope. A narration of the next step is not a final answer: push it back.
CUT_OFF = ('Your last message was cut off at the %d-token output limit and nothing after the cut '
           'reached the disk or the harness. Everything you wrote with tools before it is intact. '
           'Continue from there in smaller steps: one write_file or edit_file of at most about 120 '
           'lines per call, never a whole file in one reply.')
UNFINISHED = ('You stopped without the final JSON answer. If the work is not finished, this is '
              'the moment to continue it with tool calls (the file on disk ends where your last '
              'write ended). When it is finished and the suite passes, answer with ONLY the JSON '
              'object the brief asks for.')

# Measured on nemotron-3-super: asked for a file containing &amp; and &lt;, the tool-call
# arguments arrive as & and <; every escapeHtml it wrote mapped characters to themselves and
# the stored-XSS test failed in every run, solo or swarm. Warned, the same model writes the
# entities intact. So the warning rides with the tools that write.
ENTITIES = ('CAUTION: HTML entities you mean to write literally (&amp; &lt; &gt; &quot; &#39;) '
            'tend to come out already decoded (& < > "). When a file must contain such an '
            'entity, write it with deliberate care and read the file back to confirm, or build '
            'it from pieces in code (e.g. "&" + "amp;").')

TOOLS = [
    {'type': 'function', 'function': {
        'name': 'write_file',
        'description': 'Create a file, or REPLACE the whole file if it exists. To add to a file '
                       'you already wrote, pass append true (adds at the end) or use edit_file; '
                       'calling write_file again without append throws the earlier content away. '
                       + ENTITIES,
        'parameters': {'type': 'object', 'required': ['path', 'content'], 'properties': {
            'path': {'type': 'string', 'description': 'relative to your working directory'},
            'content': {'type': 'string'},
            'append': {'type': 'boolean', 'description': 'add to the end instead of replacing'}}}}},
    {'type': 'function', 'function': {
        'name': 'edit_file',
        'description': 'Replace one exact passage of a file with new text. old_text must appear '
                       'exactly once; the rest of the file is untouched. The right tool for '
                       'fixing a function or inserting a block into a file you already wrote. '
                       + ENTITIES,
        'parameters': {'type': 'object', 'required': ['path', 'old_text', 'new_text'],
                       'properties': {'path': {'type': 'string'},
                                      'old_text': {'type': 'string'},
                                      'new_text': {'type': 'string'}}}}},
    {'type': 'function', 'function': {
        'name': 'read_file',
        'description': 'Read a UTF-8 text file, whole, or a line range with lines "120-180" '
                       '(numbered). Prefer a range on a big file: a whole 60 KB read costs '
                       '16k tokens of your context every time; grep -n first, then the range.',
        'parameters': {'type': 'object', 'required': ['path'], 'properties': {
            'path': {'type': 'string'},
            'lines': {'type': 'string', 'description': 'optional "start-end", 1-based, inclusive'}}}}},
    {'type': 'function', 'function': {
        'name': 'list_dir',
        'description': 'List a directory. Defaults to your working directory.',
        'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}},
    {'type': 'function', 'function': {
        'name': 'run_command',
        'description': 'Run a shell command in your working directory and return its output. '
                       'Use it to actually execute what you wrote.',
        'parameters': {'type': 'object', 'required': ['command'], 'properties': {
            'command': {'type': 'string'}}}}},
]


KEY_VAR = os.environ.get('SWARM_OPENAI_KEY_VAR') or (
    'TOKEN_HARBOR_API_KEY' if 'tokenharbor' in BASE else 'NVIDIA_API_KEY')


def api_key():
    """`just` loads .env for every ADW; bench scripts run python directly, so read it here too.
    The runner is plain OpenAI-compatible: NVIDIA_BASE_URL points it at any such gateway and
    KEY_VAR names the key that goes with it (Token Harbor's by URL, or SWARM_OPENAI_KEY_VAR)."""
    key = os.environ.get(KEY_VAR, '').strip()
    if not key:
        env = Path(__file__).resolve().parents[3] / '.env'
        for line in env.read_text(encoding='utf-8').splitlines() if env.is_file() else []:
            if line.startswith(KEY_VAR + '='):
                key = line.split('=', 1)[1].strip()
    if not key:
        raise RuntimeError('%s is not set: put it in the repo .env (gitignored)' % KEY_VAR)
    return key


def resolve(req, raw):
    """Keep a path inside the agent's own folder or one of its shared dirs.

    A model that asks for ..\\..\\secrets is not hostile here, it is confused, but the whole
    point of per-agent folders is that one agent cannot quietly edit another's work.
    """
    target = (req.folder / str(raw or '.')).resolve() if not Path(str(raw or '.')).is_absolute() \
        else Path(str(raw)).resolve()
    allowed = [req.folder.resolve()] + [Path(p).resolve() for p in req.shared]
    for root in allowed:
        try:
            target.relative_to(root)
            return target
        except ValueError:
            continue
    raise ValueError('%s is outside your working directory' % raw)


def run_host_command(command, folder, job=None):
    """subprocess.run(timeout=...) is not enough here. The size-1 prototype ran
    `node server.js` in the foreground: the timeout killed the shell, node kept the stdout
    pipe open, and communicate() then waited on that pipe; the swarm sat still for an hour.
    So: kill the whole tree on timeout, and never block on a pipe. The pipes are drained by
    daemon threads that are simply abandoned if a grandchild still holds a handle."""
    proc = subprocess.Popen(command, shell=True, cwd=str(folder), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding='utf-8',
                            errors='replace',
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                            start_new_session=os.name != 'nt')
    assign(job, proc)          # the agent's job: a server it leaves running ends with its turn
    captured = {'out': '', 'err': ''}

    def drain(key, pipe):
        try:
            captured[key] = pipe.read()
        except (OSError, ValueError):
            pass
    readers = [threading.Thread(target=drain, args=(k, p), daemon=True)
               for k, p in (('out', proc.stdout), ('err', proc.stderr))]
    for t in readers:
        t.start()
    try:
        proc.wait(timeout=COMMAND_TIMEOUT)
        code, note = proc.returncode, ''
    except subprocess.TimeoutExpired:
        terminate(proc)
        code = 'timeout'
        note = ('\n[killed after %ds: the command did not exit. A server must not be started '
                'in the foreground; run it in the background, or test it with a script that '
                'starts it, calls it and stops it.]' % COMMAND_TIMEOUT)
    deadline = time.monotonic() + 5
    for t in readers:
        t.join(timeout=max(0.0, deadline - time.monotonic()))
    return subprocess.CompletedProcess(command, code, captured['out'], captured['err'] + note)


def run_tool(req, name, args):
    if name == 'write_file':
        path = resolve(req, args.get('path'))
        path.parent.mkdir(parents=True, exist_ok=True)
        text = str(args.get('content') or '')
        before = path.stat().st_size if path.is_file() else 0
        # Appending is what makes a file larger than one turn's output cap possible: the
        # 32k-output runs that died mid-file were the swarm's most common failure.
        with path.open('a' if args.get('append') else 'w', encoding='utf-8', newline='') as out:
            out.write(text)
        after = path.stat().st_size
        if before and not args.get('append') and after < before:
            # The solo run oscillated 60 times between a 14 KB body and a 1.3 KB block, each
            # write_file silently discarding the other. Say it, so the next step is edit_file.
            return ('REPLACED %s: the previous %d bytes are gone, the file is now %d bytes. '
                    'To add to a file, use append true or edit_file.' % (path.name, before, after))
        return 'wrote %s, now %d bytes total' % (path.name, after)
    if name == 'edit_file':
        path = resolve(req, args.get('path'))
        if not path.is_file():
            return 'ERROR: %s does not exist' % args.get('path')
        text = path.read_text(encoding='utf-8', errors='replace')
        old, new = str(args.get('old_text') or ''), str(args.get('new_text') or '')
        hits = text.count(old) if old else 0
        if hits != 1:
            return ('ERROR: old_text appears %d times in %s; it must appear exactly once. '
                    'Read the file and pass a longer, unique passage.' % (hits, path.name))
        path.write_text(text.replace(old, new, 1), encoding='utf-8', newline='')
        return 'edited %s, now %d bytes' % (path.name, path.stat().st_size)
    if name == 'read_file':
        path = resolve(req, args.get('path'))
        if not path.is_file():
            return 'ERROR: %s does not exist' % args.get('path')
        text = path.read_text(encoding='utf-8', errors='replace')
        # A repair turn on a 1,900-line page re-read the whole file at every step: 7.2M tokens
        # for one turn. A line range is what an edit needs; the numbers line up with edit_file.
        if args.get('lines'):
            lines = text.splitlines()
            m = re.fullmatch(r'\s*(\d+)\s*-\s*(\d+)\s*', str(args['lines']))
            if not m:
                return 'ERROR: lines must look like "120-180"'
            a, b = max(1, int(m.group(1))), min(len(lines), int(m.group(2)))
            return ('\n'.join('%d: %s' % (i, lines[i - 1]) for i in range(a, b + 1))
                    + '\n[lines %d-%d of %d]' % (a, b, len(lines)))
        if len(text) > READ_LIMIT:
            return text[:READ_LIMIT] + '\n[... truncated: %d more characters]' % (len(text) - READ_LIMIT)
        return text
    if name == 'list_dir':
        path = resolve(req, args.get('path'))
        if not path.is_dir():
            return 'ERROR: %s is not a directory' % args.get('path')
        return '\n'.join(sorted(p.name + ('/' if p.is_dir() else '') for p in path.iterdir()))
    if name == 'run_command':
        command = str(args.get('command') or '')
        if req.sandbox:
            # The workspace is bind-mounted, so files stay host-side; only execution moves
            # into the per-swarm container, the same path run_acceptance takes.
            done = req.sandbox.exec(['sh', '-c', command], timeout=COMMAND_TIMEOUT,
                                    workdir=req.sandbox.inside(req.folder))
        else:
            done = run_host_command(command, req.folder, getattr(req, 'job', None))
        return ('exit=%s\n%s%s' % (done.returncode, done.stdout, done.stderr))[:OUTPUT_LIMIT]
    return 'ERROR: no such tool %s' % name


def merge_delta(calls, deltas):
    """Tool calls arrive split across chunks: name in one, arguments a few characters at a time."""
    for delta in deltas:
        slot = calls.setdefault(delta.get('index', 0), {'id': '', 'name': '', 'arguments': ''})
        if delta.get('id'):
            slot['id'] = delta['id']
        fn = delta.get('function') or {}
        if fn.get('name'):
            slot['name'] = fn['name']
        if fn.get('arguments'):
            slot['arguments'] += fn['arguments']


class Overloaded(Exception):
    """A transient refusal worth retrying: 429, 5xx, or an in-band 503 data line."""


RETRY_WAITS = (5, 15, 45, 90)   # 155 s in all: the solo run alone met 5 x 503 in ten minutes
RATE_LIMIT_FACTOR = 4        # a 429 is a per-minute budget, not a hiccup: 20 s, 60 s, 180 s
MAX_RETRY_WAIT = int(os.environ.get('SWARM_NIM_MAX_RETRY_WAIT', '600'))   # longer: a dead provider

_gate = threading.Lock()
_next_start = [0.0]
_interval = [MIN_INTERVAL]
MAX_INTERVAL = 20.0


def throttle():
    """Space request starts across every agent thread in this process: the swarm shares one
    API key, so the per-minute budget is per swarm, not per agent. The spacing adapts the
    way TCP does: a refusal widens it by half, every accepted request narrows it a little,
    so the swarm settles at whatever rate the model's bucket sustains right now instead of
    bursting into 180 s holds (57 x 429 in 50 minutes at a fixed 2.5 s, run 8a5fb8b2)."""
    with _gate:
        now = time.monotonic()
        wait = _next_start[0] - now
        _next_start[0] = max(now, _next_start[0]) + _interval[0]
    if wait > 0:
        time.sleep(wait)


def accepted():
    with _gate:
        _interval[0] = max(MIN_INTERVAL, _interval[0] - 0.2)


def cool_down(seconds):
    """A 429 is the key's budget, not this thread's: hold every thread back, or the others
    walk into the same refusal one after the other and each backs off on its own clock."""
    with _gate:
        _next_start[0] = max(_next_start[0], time.monotonic() + seconds)
        _interval[0] = min(MAX_INTERVAL, _interval[0] * 1.5)


def one_turn_with_retries(req, history, raw, cancel):
    """Five owners starting at once are exactly when the free tier says 503; one refused
    turn must not cost the swarm a whole agent. Errors that are not transient (401, 404,
    400) pass straight through."""
    for wait in RETRY_WAITS + (None,):
        try:
            throttle()
            result = one_turn(req, history, raw, cancel)
            accepted()
            return result
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or wait is None:
                raise
            if exc.code == 429:
                retry_after = exc.headers.get('Retry-After') if exc.headers else None
                wait = int(retry_after) if retry_after and retry_after.isdigit() \
                    else min(wait * RATE_LIMIT_FACTOR, 180)
                if wait > MAX_RETRY_WAIT:
                    # Token Harbor answers an exhausted weekly allowance with Retry-After 3600
                    # and a body naming the next period: a dead provider, not a queue
                    try:
                        body = exc.read().decode('utf-8', 'replace')[:300]
                    except Exception:
                        body = ''
                    raise AgentFailed('%s: the provider asks for a %ds wait (%s)'
                                      % (getattr(req, 'agent', '?'), wait, body or 'no body'))
                cool_down(wait)
            raw.write(json.dumps({'retry': 'HTTP %s' % exc.code, 'wait': wait}) + '\n')
        except Overloaded as exc:
            if wait is None:
                raise
            raw.write(json.dumps({'retry': str(exc), 'wait': wait}) + '\n')
        raw.flush()
        if cancel.wait(wait):
            raise RuntimeError('cancelled by the swarm while waiting to retry')


def one_turn(req, history, raw, cancel):
    """One streamed completion. Returns (text, [tool call], finish_reason, usage delta)."""
    body = {'model': req.model, 'messages': history, 'tools': TOOLS, 'max_tokens': MAX_TOKENS,
            'temperature': 1, 'stream': True, 'stream_options': {'include_usage': True}}
    if EFFORT:
        body['reasoning_effort'] = EFFORT
    request = urllib.request.Request(
        BASE.rstrip('/') + '/chat/completions', method='POST',
        data=json.dumps(body).encode('utf-8'),
        headers={'Authorization': 'Bearer ' + api_key(), 'Content-Type': 'application/json',
                 'Accept': 'text/event-stream'})
    texts, calls, finish, used = [], {}, None, {}
    started = time.time()
    with urllib.request.urlopen(request, timeout=CONNECT_TIMEOUT) as response:
        # urlopen's timeout becomes the socket timeout for the whole stream; once the first
        # byte is here a long silence is a dead stream, not a queue.
        try:
            response.fp.raw._sock.settimeout(READ_TIMEOUT)
        except AttributeError:
            pass
        for line in response:
            if cancel.is_set():
                raise RuntimeError('cancelled by the swarm after %.0fs' % (time.time() - started))
            line = line.decode('utf-8', 'replace').strip()
            if not line.startswith('data:'):
                continue
            chunk = line[5:].strip()
            if chunk == '[DONE]':
                break
            try:
                event = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            raw.write(chunk + '\n')
            raw.flush()
            if event.get('error'):
                # NIM reports an overloaded backend as a 200 whose only data line is
                # {"error": {"code": 503, ...}}; silently it would read as an empty reply.
                detail = event['error'] if isinstance(event['error'], dict) else {}
                raise Overloaded('%s %s' % (detail.get('code') or '',
                                            detail.get('message') or event['error']))
            used = event.get('usage') or used
            for choice in event.get('choices') or []:
                delta = choice.get('delta') or {}
                finish = choice.get('finish_reason') or finish
                if delta.get('content'):
                    texts.append(delta['content'])
                if delta.get('tool_calls'):
                    merge_delta(calls, delta['tool_calls'])
    return ''.join(texts), [calls[i] for i in sorted(calls)], finish, used


def invoke(req: AgentRequest, messages, cancel):
    history = [{'role': 'user', 'content': req.prompt}]
    total = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
    texts, error, steps, tool_steps, nudged = [], None, 0, 0, 0
    # The CLI runners get their deadline from watch(); this loop owns its own, so a model that
    # keeps calling tools cannot outlive the call budget the swarm allocated it.
    deadline = time.time() + req.timeout
    req.job = new_job()
    with (req.folder / 'events.jsonl').open('w', encoding='utf-8') as raw:
        while steps < MAX_STEPS:
            if time.time() > deadline:
                error = 'out of time after %d tool steps (%ss budget)' % (steps, req.timeout)
                break
            steps += 1
            try:
                text, calls, finish, used = one_turn_with_retries(req, history, raw, cancel)
            except urllib.error.HTTPError as exc:
                error = 'HTTP %s: %s' % (exc.code, exc.read().decode('utf-8', 'replace')[:300])
                break
            except Overloaded as exc:
                error = 'provider overloaded, gave up after %d retries: %s' % (len(RETRY_WAITS), exc)
                break
            except Exception as exc:                      # socket, TLS, cancel, malformed body
                error = '%s: %s' % (type(exc).__name__, exc)
                break
            for key in total:
                total[key] += (used or {}).get(key) or 0
            if text:
                texts.append(text)
            if not calls:
                if finish == 'length':
                    # nemotron on spec 20: 1,920 lines on disk, then one reply longer than the
                    # cap and the turn was lost. The disk is intact; say so and let it go on.
                    if nudged < NUDGES:
                        nudged += 1
                        history.append({'role': 'assistant', 'content': text or '(cut off)'})
                        history.append({'role': 'user', 'content': CUT_OFF % MAX_TOKENS})
                        raw.write(json.dumps({'nudge': 'reply cut at the output cap'}) + '\n')
                        continue
                    error = ('the reply hit the %d-token output cap; the agent never finished '
                             'its turn' % MAX_TOKENS)
                    break
                if tool_steps == 0:
                    # The size-10 finisher answered "copied the file, applied two fixes,
                    # artifacts: server.js" having called no tool at all; the size-5
                    # prototype did it twice in a row, nudge included. Nothing existed either
                    # time. Every swarm turn needs the disk, so a tool-free answer is pushed
                    # back NUDGES times and is then a failure, never a result. (tool_choice
                    # "required" is not an option: NIM answers it with `[[` and whitespace.)
                    if text:
                        texts.pop()        # a claim with no work behind it is not the reply
                    if nudged >= NUDGES:
                        error = ('answered %d times without calling a single tool; nothing '
                                 'was written' % (nudged + 1))
                        break
                    nudged += 1
                    history.append({'role': 'assistant', 'content': text})
                    history.append({'role': 'user', 'content': NUDGE})
                    raw.write(json.dumps({'nudge': 'no tool call before the final answer'}) + '\n')
                    continue
                if not has_envelope(text) and nudged < NUDGES:
                    nudged += 1
                    history.append({'role': 'assistant', 'content': text})
                    history.append({'role': 'user', 'content': UNFINISHED})
                    raw.write(json.dumps({'nudge': 'stopped without an envelope'}) + '\n')
                    continue
                break
            tool_steps += 1
            history.append({'role': 'assistant', 'content': text or None, 'tool_calls': [
                {'id': c['id'] or ('call_%d' % i), 'type': 'function',
                 'function': {'name': c['name'], 'arguments': c['arguments']}}
                for i, c in enumerate(calls)]})
            for i, call in enumerate(calls):
                try:
                    args = json.loads(call['arguments'] or '{}')
                except json.JSONDecodeError:
                    args, result = {}, 'ERROR: your arguments were not valid JSON'
                else:
                    try:
                        result = run_tool(req, call['name'], args)
                    except Exception as exc:
                        result = 'ERROR: %s: %s' % (type(exc).__name__, exc)
                tool_event(messages, req, call['name'], {'parameters': {
                    k: str(v)[:120] for k, v in args.items() if k in ('path', 'command')}})
                history.append({'role': 'tool', 'tool_call_id': call['id'] or ('call_%d' % i),
                                'content': result})
        else:
            error = 'gave up after %d tool steps without a final answer' % MAX_STEPS
    close_job(req.job)
    req.job = None
    used = usage('nim', req.model, total=total['total_tokens'] or None,
                 input=total['prompt_tokens'] or None, output=total['completion_tokens'] or None,
                 cost=0.0)
    if not error and not any(t.strip() for t in texts):
        error = 'produced no reply text in %d steps' % steps
    return deliver(req, texts, used, error, steps=steps)
