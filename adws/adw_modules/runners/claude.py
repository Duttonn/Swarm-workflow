"""Claude Code `-p --output-format stream-json` (paid plan). The brief rides stdin; -p
without a prompt argument reads it from there."""
import os

from ..agy_swarm import AgentRequest
from .common import cli_path, deliver, stream, tool_event, usage


def invoke(req: AgentRequest, messages, cancel):
    argv = [cli_path('claude'), '-p', '--output-format', 'stream-json', '--verbose',
            '--dangerously-skip-permissions']
    if req.model:
        argv += ['--model', req.model]
    for extra in req.shared:
        argv += ['--add-dir', str(extra)]
    # A claude launched from inside a Claude Code session refuses to nest unless these go.
    env = {k: v for k, v in os.environ.items() if k not in ('CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT')}
    texts, done = [], {}

    def on_event(event):
        kind = event.get('type')
        if kind == 'assistant':
            for block in (event.get('message') or {}).get('content') or []:
                if block.get('type') == 'tool_use':
                    tool_event(messages, req, block.get('name'), {'input': block.get('input')})
                elif block.get('type') == 'text' and block.get('text'):
                    texts.append(block['text'])
        elif kind == 'system' and event.get('subtype') == 'init':
            messages.put((req.agent, {'event': 'init', 'init': event}))
        elif kind == 'result':
            done.update(event)

    code, plain = stream(req, argv, messages, cancel, on_event, stdin_text=req.prompt + '\n',
                         env=env)
    u = done.get('usage') or {}
    fresh, created, read = (u.get('input_tokens'), u.get('cache_creation_input_tokens'),
                            u.get('cache_read_input_tokens'))
    model = next(iter(done.get('modelUsage') or {}), None) or req.model
    used = usage('claude', model, cached=read, output=u.get('output_tokens'),
                 input=None if fresh is None else fresh + (created or 0) + (read or 0),
                 cost=done.get('total_cost_usd'))
    error = None
    if code or done.get('is_error') or (done and done.get('subtype', 'success') != 'success'):
        error = 'claude exit=%s %s: %s' % (code, done.get('subtype'),
                                          done.get('result') or plain.strip()[-300:])
    elif not done:
        error = 'no result event: %s' % plain.strip()[-300:]
    final = done.get('result') if isinstance(done.get('result'), str) else None
    return deliver(req, texts + ([final] if final else []), used, error)
