"""opencode `run --format json`: the free Zen models (opencode/big-pickle and friends) need no
login. The brief rides stdin (verified: opencode appends piped stdin to the message).

Two opencode processes starting in the same instant once hit "database is locked" on its
local SQLite; peer_round's STAGGER already spaces agents out, so no serve/attach here.
"""
from ..agy_swarm import AgentRequest
from .common import cli_path, deliver, stream, tool_event, usage


def invoke(req: AgentRequest, messages, cancel, binary='opencode'):
    """`binary` lets forks with the same CLI surface (kilo) reuse the whole parser."""
    argv = [cli_path(binary), 'run', '--format', 'json', '-m', req.model,
            '--dir', str(req.folder), '--auto']
    texts, tokens, errors = [], [], []
    total = {'input': 0, 'output': 0, 'reasoning': 0, 'read': 0, 'write': 0, 'total': 0,
             'cost': 0.0}

    def on_event(event):
        kind, part = event.get('type'), event.get('part') or {}
        if kind == 'text':
            texts.append(str(part.get('text') or ''))
        elif kind == 'tool_use':
            tool_event(messages, req, part.get('tool'), {'state': part.get('state')})
        elif kind == 'step_finish':
            t = part.get('tokens') or {}
            cache = t.get('cache') or {}
            for key in ('input', 'output', 'reasoning', 'total'):
                total[key] += t.get(key) or 0
            total['read'] += cache.get('read') or 0
            total['write'] += cache.get('write') or 0
            total['cost'] += part.get('cost') or 0
            tokens.append(t)
        elif kind == 'error':
            errors.append(str(event.get('error') or event))

    code, plain = stream(req, argv, messages, cancel, on_event, stdin_text=req.prompt + '\n')
    # tokens.input excludes the cached part: total = input + output + cache.read
    used = usage(binary, req.model, total=total['total'] if tokens else None,
                 input=total['input'] + total['read'] + total['write'] if tokens else None,
                 cached=total['read'] if tokens else None,
                 output=total['output'] + total['reasoning'] if tokens else None,
                 cost=total['cost'] if tokens else None)
    error = None
    if code:
        error = '%s exit=%s %s' % (binary, code, (errors or [plain.strip()[-300:]])[-1])
    elif not any(t.strip() for t in texts):
        error = 'produced no reply text%s' % (': ' + errors[-1][:300] if errors else '')
    return deliver(req, texts, used, error)
