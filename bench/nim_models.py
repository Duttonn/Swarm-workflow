"""Which NVIDIA NIM models actually answer, how fast, and whether they can call a tool.

Run: .venv\\Scripts\\python.exe bench\\nim_models.py [model ...]     (default: the shortlist)
     NIM_PROBE_TIMEOUT=seconds per model (default 240), NIM_PROBE_PARALLEL=threads (default 6)

The free tier queues a request behind whatever the GPUs are doing and answers HTTP 504 when
that wait runs out, which is indistinguishable from a bad key until you look. One small
tool-calling request per model, all at once, gives the table that picks the workhorse:
a swarm runner needs a model that (a) replies at all and (b) emits tool_calls, since the
nim runner's whole loop is built on them.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get('NVIDIA_BASE_URL', 'https://integrate.api.nvidia.com/v1')
TIMEOUT = int(os.environ.get('NIM_PROBE_TIMEOUT', '240'))
PARALLEL = int(os.environ.get('NIM_PROBE_PARALLEL', '6'))

SHORTLIST = ['moonshotai/kimi-k3', 'deepseek-ai/deepseek-v4-flash-0731', 'moonshotai/kimi-k2.6',
             'z-ai/glm-5.3-flash', 'openai/gpt-oss-20b', 'nvidia/nemotron-3-super-120b-a12b',
             'nvidia/nemotron-nano-3-30b-a3b', 'nvidia/nemotron-3.5-lightning-30b-a3b',
             'poolside/laguna-xs-2.1', 'mistralai/mistral-nemotron']

TOOL = [{'type': 'function', 'function': {
    'name': 'write_file', 'description': 'Create a file.',
    'parameters': {'type': 'object', 'required': ['path', 'content'], 'properties': {
        'path': {'type': 'string'}, 'content': {'type': 'string'}}}}}]
ASK = ('Create a file named hello.txt whose entire content is the word ok. '
       'Call the write_file tool to do it.')


KEY_VAR = os.environ.get('SWARM_OPENAI_KEY_VAR') or (
    'TOKEN_HARBOR_API_KEY' if 'tokenharbor' in BASE else 'NVIDIA_API_KEY')


def api_key():
    if os.environ.get(KEY_VAR):
        return os.environ[KEY_VAR].strip()
    for line in (ROOT / '.env').read_text(encoding='utf-8').splitlines():
        if line.startswith(KEY_VAR + '='):
            return line.split('=', 1)[1].strip()
    raise RuntimeError('%s not in the environment or .env' % KEY_VAR)


def probe(model, key):
    body = {'model': model, 'messages': [{'role': 'user', 'content': ASK}], 'tools': TOOL,
            'max_tokens': 2048, 'temperature': 1, 'stream': True,
            'stream_options': {'include_usage': True}}
    request = urllib.request.Request(
        BASE.rstrip('/') + '/chat/completions', method='POST',
        data=json.dumps(body).encode('utf-8'),
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
                 'Accept': 'text/event-stream'})
    started, first, names, text, used = time.time(), None, [], [], None
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            for raw in response:
                line = raw.decode('utf-8', 'replace').strip()
                if not line.startswith('data:'):
                    continue
                chunk = line[5:].strip()
                if chunk == '[DONE]':
                    break
                try:
                    event = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                first = first if first is not None else time.time() - started
                used = event.get('usage') or used
                for choice in event.get('choices') or []:
                    delta = choice.get('delta') or {}
                    if delta.get('content'):
                        text.append(delta['content'])
                    for call in delta.get('tool_calls') or []:
                        name = (call.get('function') or {}).get('name')
                        if name:
                            names.append(name)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', 'replace')[:120].replace('\n', ' ').strip()
        return model, 'HTTP %s' % exc.code, time.time() - started, first, detail
    except Exception as exc:
        return model, type(exc).__name__, time.time() - started, first, str(exc)[:120]
    tokens = (used or {}).get('total_tokens')
    if names:
        return model, 'TOOL', time.time() - started, first, 'calls=%s tokens=%s' % (names, tokens)
    if any(t.strip() for t in text):
        return model, 'TEXT', time.time() - started, first, \
            'no tool call, said %r' % ''.join(text)[:60]
    return model, 'EMPTY', time.time() - started, first, 'tokens=%s' % tokens


def main(argv):
    models = argv[1:] or SHORTLIST
    key = api_key()
    print('%-42s %-10s %8s %8s  %s' % ('model', 'verdict', 'wall', 'first', 'detail'))
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        rows = list(pool.map(lambda m: probe(m, key), models))
    for model, verdict, wall, first, detail in rows:
        print('%-42s %-10s %7.1fs %8s  %s' % (
            model, verdict, wall, '%.1fs' % first if first else '-', detail))
    return 0 if any(r[1] == 'TOOL' for r in rows) else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    raise SystemExit(main(sys.argv))
