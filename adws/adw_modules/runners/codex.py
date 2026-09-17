"""OpenAI Codex CLI `exec --json` (paid ChatGPT plan). The brief rides stdin: a bare `-`
prompt makes codex read the instructions from there. No --full-auto in 0.154; the
workspace-write sandbox plus --skip-git-repo-check is what runs unattended in a scratch dir.
"""
from ..agy_swarm import AgentRequest
from .common import cli_path, deliver, stream, tool_event, usage


def invoke(req: AgentRequest, messages, cancel):
    argv = [cli_path('codex'), 'exec', '--json', '-s', 'workspace-write', '--skip-git-repo-check',
            '-C', str(req.folder), '-o', str(req.folder / 'last_message.txt')]
    if req.model:
        argv += ['-m', req.model]
    for extra in req.shared:
        argv += ['--add-dir', str(extra)]
    argv.append('-')
    texts, done, errors = [], {}, []

    def on_event(event):
        kind, item = event.get('type'), event.get('item') or {}
        if kind == 'item.completed' and item.get('type') == 'agent_message':
            texts.append(str(item.get('text') or ''))
        elif kind == 'item.started' and item.get('type') in ('command_execution', 'file_change',
                                                             'mcp_tool_call', 'web_search'):
            tool_event(messages, req, item.get('type'), {'item': item})
        elif kind == 'turn.completed':
            done.update(event.get('usage') or {})
        elif kind in ('turn.failed', 'error'):
            errors.append(str(event.get('error') or event.get('message') or event))

    code, plain = stream(req, argv, messages, cancel, on_event, stdin_text=req.prompt + '\n')
    last = req.folder / 'last_message.txt'
    if not texts and last.is_file():
        texts.append(last.read_text(encoding='utf-8', errors='replace'))
    # input_tokens already includes the cached portion
    used = usage('codex', req.model or 'default', input=done.get('input_tokens'),
                 cached=done.get('cached_input_tokens'), output=done.get('output_tokens')) \
        if done else usage('codex', req.model or 'default')
    error = None
    if code or errors:
        error = 'codex exit=%s: %s' % (code, (errors or [plain.strip()[-300:]])[-1])
    elif not any(t.strip() for t in texts):
        error = 'produced no reply text'
    return deliver(req, texts, used, error)
