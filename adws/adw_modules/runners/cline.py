"""Cline CLI `--json`. Verified with deepseek/deepseek-v4-flash on 2026-09-14 (bench/check_runner.py
PASS, 30k tokens, cost 0). Needs a logged-in Cline account (`cline auth cline`).

Tool calls arrive as `agent_event` items with `contentType: "tool"` (content_start / content_update /
content_end), one `toolCallId` each; the first sight of an id is the call.

The brief is NOT on the command line: through the npm .CMD shim cline does not see piped
stdin ("JSON output mode requires a prompt argument or piped stdin", measured), and a
positional 30k-char brief dies on cmd.exe's 8191-char limit. So the prompt points the
agent at prompt.txt, which invoke() has already written into its cwd.
"""
import os

from ..agy_swarm import AgentRequest
from .common import cli_path, deliver, stream, tool_event, usage

PROVIDER = os.environ.get('SWARM_CLINE_PROVIDER', 'cline')
# muse-spark on the 20-feature spreadsheet brief: "The operation timed out." after eleven
# minutes of reasoning, cline's own request deadline, not ours. A lower reasoning effort is
# the only lever the CLI exposes (none|low|medium|high|xhigh); unset keeps the model's default.
THINKING = os.environ.get('SWARM_CLINE_THINKING', '')
POINTER = ('Your brief is in the file prompt.txt in the current working directory. Read it '
           'first, then follow it exactly and end your reply with the required json block.')


def invoke(req: AgentRequest, messages, cancel):
    argv = [cli_path('cline'), '--json', '--auto-approve', 'true', '-c', str(req.folder),
            '-P', PROVIDER, '-m', req.model, '-t', str(req.timeout)]
    if THINKING:
        argv += ['--thinking', THINKING]
    argv.append(POINTER)
    done, calls = {}, set()

    def on_event(event):
        kind = event.get('type')
        if kind == 'run_result':
            done.update(event)
        elif kind == 'agent_event':
            inner = event.get('event') or {}
            call = inner.get('toolCallId')
            if inner.get('contentType') == 'tool' and call and call not in calls:
                calls.add(call)
                update = inner.get('update') or {}
                tool_event(messages, req, inner.get('toolName') or 'tool',
                           {'parameters': {k: v for k, v in update.items()
                                           if k in ('query', 'path', 'command', 'pattern')}})
        elif kind == 'error':
            done.setdefault('errors', []).append(str(event.get('message')))

    code, plain = stream(req, argv, messages, cancel, on_event)
    u = done.get('usage') or {}
    inp, read, write = u.get('inputTokens'), u.get('cacheReadTokens'), u.get('cacheWriteTokens')
    used = usage('cline', (done.get('model') or {}).get('id') or req.model,
                 input=None if inp is None else inp + (read or 0) + (write or 0),
                 cached=read, output=u.get('outputTokens'), cost=u.get('totalCost'))
    error = None
    if code or done.get('finishReason') == 'error' or not done:
        error = 'cline exit=%s finish=%s: %s' % (
            code, done.get('finishReason'),
            (done.get('errors') or [done.get('text') or plain.strip()[-300:]])[-1])
    return deliver(req, done.get('text'), used, error)
