"""What every runner does the same way: stream a CLI, normalize its usage, parse the reply."""
import json
import shutil

from ..agents import _extract_json
from ..agy_swarm import AgentFailed, Proposal, coerce_proposal, spawn, watch, terminate


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
            'output_tokens': output, 'model': model, 'cost_usd': cost, 'runner': runner}


def tool_event(messages, req, name, event):
    messages.put((req.agent, {'event': 'step_update', 'step_update': {
        'step_type': 'tool', 'tool_name': name or 'tool', 'agent': req.agent, **event}}))


def stream(req, argv, messages, cancel, on_event, stdin_text=None, env=None):
    """Run argv in req.folder, mirror stdout to events.jsonl, hand every JSON line to
    on_event. Returns (exit_code, text of the lines that were not JSON)."""
    plain = []
    with (req.folder / 'stderr.log').open('w', encoding='utf-8') as err:
        proc = spawn(argv, req, err, stdin=stdin_text is not None, env=env)
        messages.put((req.agent, {'event': 'process_start', 'pid': proc.pid}))
        killed = watch(proc, cancel, req.timeout)
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
                        on_event(event)
                proc.wait()
            finally:
                terminate(proc)
                messages.put((req.agent, {'event': 'process_end', 'pid': proc.pid,
                                          'exit_code': proc.returncode}))
    if not proc.returncode:
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
    return proc.returncode, tail


def deliver(req, reply, used, error=None, **extra):
    """Write usage.json, then either raise AgentFailed (tokens attached) or return the
    parsed Proposal. `reply` is the final assistant text, a list of texts (the last one
    holding a JSON object wins: agents narrate before the envelope), or a parsed dict."""
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
