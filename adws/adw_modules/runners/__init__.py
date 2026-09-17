"""One module per coding-agent CLI, all behind the same contract:

    invoke(req: AgentRequest, messages, cancel) -> (Proposal, result)

result['usage'] is the normalized dict `common.usage()` builds and is also written to
req.folder/usage.json, next to prompt.txt, stderr.log and events.jsonl (the raw stream).
Modules are imported on first use because each one imports helpers from agy_swarm, which
imports this package back.
"""
import importlib

RUNNERS = {name: '.' + name
           for name in ('gemini', 'agy', 'opencode', 'kilo', 'cline', 'codex', 'claude',
                        'copilot', 'nim')}


def get_runner(name):
    if name not in RUNNERS:
        raise KeyError('unknown SWARM_RUNNER %r; one of %s' % (name, ', '.join(RUNNERS)))
    return importlib.import_module(RUNNERS[name], __package__).invoke
