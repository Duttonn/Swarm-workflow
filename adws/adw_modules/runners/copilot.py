"""GitHub Copilot CLI `-p --output-format json` (paid). UNTESTED: this machine is logged out
("No authentication information found"), so the event schema is unknown and the parser is
generic: any assistant-side string field feeds the reply, usage comes from
--usage-output-file with key names guessed by substring. -p takes text on the command
line, so it points at prompt.txt rather than carrying the 30k-char brief.
"""
import json

from ..agy_swarm import AgentRequest
from .common import cli_path, deliver, stream, tool_event, usage
from .cline import POINTER


def _texts(node, out):
    """Every string worth reading from an unknown event, skipping user-side echoes."""
    if isinstance(node, dict):
        if str(node.get('role', '')).lower() == 'user':
            return
        for key in ('text', 'content', 'message', 'result', 'response'):
            if isinstance(node.get(key), str):
                out.append(node[key])
        for value in node.values():
            _texts(value, out)
    elif isinstance(node, list):
        for value in node:
            _texts(value, out)


def _pick(stats, *needles):
    for key, value in (stats or {}).items():
        low = key.lower()
        if all(n in low for n in needles) and isinstance(value, (int, float)):
            return value
    return None


def invoke(req: AgentRequest, messages, cancel):
    stats_file = req.folder / 'usage_raw.json'
    argv = [cli_path('copilot'), '-p', POINTER, '--allow-all', '--output-format', 'json',
            '--model', req.model or 'auto', '--usage-output-file', str(stats_file)]
    for extra in req.shared:
        argv += ['--add-dir', str(extra)]
    texts, errors = [], []

    def on_event(event):
        kind = str(event.get('type') or event.get('event') or '')
        if 'tool' in kind:
            tool_event(messages, req, event.get('name') or event.get('tool') or kind, {'detail': event})
        elif 'error' in kind:
            errors.append(json.dumps(event)[:300])
        else:
            _texts(event, texts)

    code, plain = stream(req, argv, messages, cancel, on_event)
    stats = {}
    if stats_file.is_file():
        try:
            stats = json.loads(stats_file.read_text(encoding='utf-8'))
        except ValueError:
            pass
    # Seen on a logged-out run: {lastCallInputTokens, lastCallOutputTokens, modelMetrics: {},
    # totalPremiumRequestCost, ...}. Per-model totals live under modelMetrics when a model ran;
    # totalPremiumRequestCost counts premium requests, not dollars, so cost stays unknown.
    metrics = stats.get('modelMetrics') or {}
    flat = {}

    def flatten(node, prefix=''):
        if isinstance(node, dict):
            for k, v in node.items():
                flatten(v, prefix + k + '.')
        elif isinstance(node, (int, float)):
            flat[prefix[:-1]] = node
    flatten(metrics)

    def count(needle):
        hits = [v for k, v in flat.items() if needle in k.lower()]
        return sum(hits) if hits else _pick(stats, 'lastcall', needle)
    used = usage('copilot', next(iter(metrics), None) or req.model or 'auto',
                 input=count('input'), cached=count('cache'), output=count('output'))
    error = None
    if code:
        error = 'copilot exit=%s: %s' % (code, (errors or [plain.strip()[-300:]])[-1])
    return deliver(req, texts + [plain], used, error)
