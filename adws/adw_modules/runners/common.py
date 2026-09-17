"""What every runner does the same way: stream a CLI, normalize its usage, parse the reply."""
import json
import os
import shutil
import threading
import time

from ..agents import _extract_json
from ..agy_swarm import AgentFailed, Proposal, coerce_proposal, spawn, terminate


def estimate_tokens(value):
    """A planning estimate, not a tokenizer or an upper bound (hidden reasoning is absent)."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return max(1, (len(text.encode('utf-8')) + 2) // 3)


class AgentBudget:
    """Per-invocation accounting; provider quota and model context are separate quantities."""
    def __init__(self, req, enforcement='estimated_api_preflight'):
        self.req = req
        self.limit = int(os.environ.get('SWARM_AGENT_TOKEN_LIMIT', '60000'))
        self.output_limit = int(os.environ.get('SWARM_AGENT_OUTPUT_LIMIT', '12000'))
        if min(self.limit, self.output_limit) <= 0:
            raise ValueError('agent token/output limits must be positive')
        self.total = self.output = 0
        self.status = 'unknown'
        self.enforcement = enforcement

    def snapshot(self):
        return {'token_limit': self.limit, 'output_limit': self.output_limit,
                'accounted_tokens': self.total, 'accounted_output_tokens': self.output,
                'remaining_tokens': max(0, self.limit - self.total),
                'remaining_output_tokens': max(0, self.output_limit - self.output),
                'usage_status': self.status, 'enforcement': self.enforcement,
                'provider_quota_remaining': None}

    def publish(self, messages=None):
        data = self.snapshot()
        (self.req.folder / 'budget.json').write_text(json.dumps(data, indent=1), encoding='utf-8')
        if messages is not None:
            messages.put((self.req.agent, {'event': 'budget_update', 'budget': data}))

    def note(self):
        return ('\nAgent budget (tokens, all repeated input plus output): %s. '
                'Read budget.json for the latest accounting. Remaining values are an allowance '
                'against accounted usage, not a provider quota or a guarantee. Finish within '
                'this allowance; do not spawn extra agents.\n' % json.dumps(self.snapshot()))

    def allocate(self, history, maximum):
        allowance = min(maximum, self.output_limit - self.output,
                        self.limit - self.total - estimate_tokens(history) - 256)
        if allowance <= 0:
            raise RuntimeError('token budget exhausted before the next API request')
        return allowance

    def record(self, used, history, output):
        used = used or {}
        known = all(isinstance(used.get(k), (int, float)) for k in
                    ('prompt_tokens', 'completion_tokens'))
        inp = used.get('prompt_tokens') if known else estimate_tokens(history)
        out = used.get('completion_tokens') if known else estimate_tokens(output)
        self.total += int(used.get('total_tokens') or inp + out) if known else inp + out
        self.output += int(out)
        self.status = 'known' if known and self.status != 'estimated' else 'estimated'


def meaningful(event):
    """Only model/tool work counts; startup, heartbeat and accounting events do not."""
    kind = event.get('type') or event.get('event') or ''
    if kind in ('heartbeat', 'ping', 'process_start', 'session_start', 'thread.started',
                'turn.started', 'init', 'system', 'step_start', 'step_finish'):
        return False
    if kind.startswith('item.'):
        item = event.get('item') or {}
        return item.get('type') in ('agent_message', 'reasoning', 'command_execution',
                                    'file_change', 'mcp_tool_call', 'web_search')
    if kind == 'agent_event':
        inner = event.get('event') or {}
        return inner.get('contentType') in ('text', 'tool', 'reasoning')
    return kind in ('text', 'tool_use', 'message', 'content_block_delta',
                    'content_block_start', 'assistant', 'step_update')


def watch_progress(proc, cancel, timeout, progress):
    started = time.monotonic()
    first = float(os.environ.get('SWARM_FIRST_OUTPUT_TIMEOUT', '45'))
    idle = float(os.environ.get('SWARM_IDLE_TIMEOUT', '45'))
    killed, stop = {'reason': None}, threading.Event()

    def run():
        while not stop.wait(.05) and proc.poll() is None:
            now = time.monotonic()
            reason = ('cancelled by the swarm' if cancel.is_set() else
                      'wall timeout after %ss' % timeout if now - started >= timeout else
                      'no meaningful output after %ss' % first if not progress[0] and
                      now - started >= first else
                      'idle without meaningful output for %ss' % idle if progress[0] and
                      now - progress[0] >= idle else None)
            if reason:
                killed['reason'] = reason
                terminate(proc)
                return
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return killed, stop, thread


def cli_path(*names):
    """First of `names` on PATH (Windows npm shims are .cmd files)."""
    for name in names:
        found = shutil.which(name) or shutil.which(name + '.cmd')
        if found:
            return found
    raise RuntimeError('%s CLI missing from PATH' % names[0])


def usage(runner, model, total=None, input=None, cached=None, output=None, cost=None):
    """Normalized usage. input counts every prompt token (cached included), cached the
    subset served from cache, output the answer plus any thinking. None means unknown."""
    if total is None and (input is not None or output is not None):
        total = (input or 0) + (output or 0)
    return {'total_tokens': total, 'input_tokens': input, 'cached_tokens': cached,
            'output_tokens': output, 'model': model, 'cost_usd': cost, 'runner': runner,
            'usage_status': 'known' if total is not None else 'unknown'}


def tool_event(messages, req, name, event):
    messages.put((req.agent, {'event': 'step_update', 'step_update': {
        'step_type': 'tool', 'tool_name': name or 'tool', 'agent': req.agent, **event}}))


def stream(req, argv, messages, cancel, on_event, stdin_text=None, env=None):
    """Run argv in req.folder, mirror stdout to events.jsonl, hand every JSON line to
    on_event. Returns (exit_code, text of the lines that were not JSON)."""
    plain = []
    budget = AgentBudget(req, 'cli_turn_final_telemetry_only')
    req._agent_budget = budget
    budget.publish(messages)
    if stdin_text is not None:
        stdin_text += budget.note()
    elif (req.folder / 'prompt.txt').is_file():
        with (req.folder / 'prompt.txt').open('a', encoding='utf-8') as prompt:
            prompt.write(budget.note())
    with (req.folder / 'stderr.log').open('w', encoding='utf-8') as err:
        proc = spawn(argv, req, err, stdin=stdin_text is not None, env=env)
        messages.put((req.agent, {'event': 'process_start', 'pid': proc.pid}))
        progress = [None]
        killed, stop, watcher = watch_progress(proc, cancel, req.timeout, progress)
        if stdin_text is not None:
            try:
                proc.stdin.write(stdin_text)
                proc.stdin.close()
            except OSError as exc:
                terminate(proc)
                raise RuntimeError('%s: could not send the brief: %s' % (req.agent, exc))
        with (req.folder / 'events.jsonl').open('w', encoding='utf-8') as raw:
            try:
                for line in proc.stdout:
                    raw.write(line)
                    raw.flush()
                    try:
                        event = json.loads(line.lstrip('\ufeff'))
                    except json.JSONDecodeError:
                        plain.append(line)
                        continue
                    if isinstance(event, dict):
                        if meaningful(event):
                            progress[0] = time.monotonic()
                        on_event(event)
                proc.wait()
            finally:
                stop.set()
                watcher.join(timeout=12)
                terminate(proc)
                messages.put((req.agent, {'event': 'process_end', 'pid': proc.pid,
                                          'exit_code': proc.returncode}))
    if not proc.returncode and not killed['reason']:
        return 0, ''.join(plain)
    # A failure must never come back as a bare exit code: name the reason or say there was
    # none. `plain` can be a blank line, which is why the test is on the stripped text.
    tail = ''.join(plain).strip()
    if not tail:
        # CLIs put their login and quota errors on stderr; keep them in the failure message.
        tail = (req.folder / 'stderr.log').read_text(
            encoding='utf-8', errors='replace')[-600:].strip()
    if killed['reason']:
        tail = '%s%s' % (killed['reason'], ' (%s)' % tail if tail else '')
    elif not tail:
        tail = 'the CLI exited with no output on either stream (provider unreachable?)'
    return proc.returncode or 1, tail


def deliver(req, reply, used, error=None, **extra):
    """Write usage.json, then either raise AgentFailed (tokens attached) or return the
    parsed Proposal. `reply` is the final assistant text, a list of texts (the last one
    holding a JSON object wins: agents narrate before the envelope), or a parsed dict."""
    budget = getattr(req, '_agent_budget', None)
    if budget is not None:
        if budget.enforcement == 'cli_turn_final_telemetry_only':
            budget.total = used.get('total_tokens') or 0
            budget.output = used.get('output_tokens') or 0
            budget.status = used.get('usage_status', 'unknown')
            if budget.total > budget.limit or budget.output > budget.output_limit:
                error = error or 'token budget exceeded (CLI reports usage only after the turn)'
        budget.publish()
        used['budget'] = budget.snapshot()
    (req.folder / 'usage.json').write_text(json.dumps(used, indent=1), encoding='utf-8')
    spent = used.get('total_tokens') or 0
    if error:
        raise AgentFailed('%s: %s' % (req.agent, error), spent)
    try:
        if isinstance(reply, dict):
            payload = reply
        else:
            texts = [t for t in (reply if isinstance(reply, list) else [reply]) if t]
            payload, failure = None, ValueError('no reply text')
            for text in reversed(texts):
                try:
                    payload = _extract_json(text)
                    break
                except Exception as exc:
                    failure = exc
            if payload is None:
                raise failure
        proposal = Proposal.model_validate(coerce_proposal(payload))
        if proposal.status != 'success':
            raise RuntimeError('Agent reported failure: %s' % proposal.summary)
    except Exception as exc:
        raise AgentFailed('%s: %s' % (req.agent, exc), spent) from exc
    return proposal, {'usage': used, **extra}
