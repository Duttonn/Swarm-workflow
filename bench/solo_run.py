"""One agent, alone, on a whole spec: the single-agent side of every comparison.

Run: .venv\\Scripts\\python.exe bench\\solo_run.py <spec.json> <runner> [model]
     (SWARM_SOLO_TIMEOUT=seconds, default 1800; SWARM_SOLO_ROOT overrides work/measured)

The agent gets exactly what the swarm gets: goal, definition of done, contract, and the fixed
test suite on disk to run as often as it likes. The deliverable is judged by that suite, and
the folder keeps events.jsonl + usage.json so bench/cost_report.py can price it.
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'adws'))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from adw_modules import agy_swarm  # noqa: E402
from check_runner import ENVELOPE  # noqa: E402


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    spec = json.loads(Path(argv[1]).read_text(encoding='utf-8'))
    runner = argv[2]
    model = argv[3] if len(argv) > 3 else agy_swarm.MODELS.get(runner, '')
    slug = Path(argv[1]).stem.split('-', 1)[-1]
    tag = (model or runner).split('/')[-1].replace(':', '-')
    root = Path(os.environ.get('SWARM_SOLO_ROOT') or ROOT / 'work' / 'measured') / ('%s-%s' % (slug, tag))
    folder = root / time.strftime('%Y%m%d-%H%M%S')
    folder.mkdir(parents=True, exist_ok=True)
    out_name = spec['output_file']
    tests = folder / 'test_acceptance.py'
    tests.write_text(spec['tests'], encoding='utf-8')
    task = ('You are one agent building one deliverable alone. Write %s in your current working '
            'directory: the complete, working file that satisfies the whole contract. The '
            'acceptance suite the judge will run is already next to you at %s: run it with '
            '`python -I -m unittest discover -s . -v` as often as you like and fix until it '
            'passes. WRITE THE FILE IN PIECES: the skeleton first, then each section with a '
            'separate edit call, never more than about 120 lines per tool call (a model with an '
            'output cap loses the whole turn on one long write). Put code = "" in the json '
            'block; the file on disk is the delivery.\n' % (out_name, tests.name))
    prompt = ENVELOPE % (spec['prompt'], spec['definition_of_done'], spec['contract']) + task
    req = agy_swarm.AgentRequest('solo', prompt, folder, model=model, runner=runner,
                                 timeout=int(os.environ.get('SWARM_SOLO_TIMEOUT', '1800')))
    events, cancel = queue.Queue(), threading.Event()
    print('solo runner=%s model=%s folder=%s' % (runner, model or '(cli default)', folder))
    started = time.monotonic()
    proposal, result, failure = None, {}, None
    try:
        proposal, result = agy_swarm.invoke(req, events, cancel)
    except agy_swarm.AgentFailed as exc:
        failure = exc
    wall = time.monotonic() - started
    tools = 0
    while not events.empty():
        _agent, event = events.get()
        if event.get('event') == 'step_update' and event['step_update'].get('step_type') == 'tool':
            tools += 1
    # the file on disk is the deliverable; the envelope's code field is the fallback
    target = folder / out_name
    if not target.exists() and proposal and proposal.code:
        target.write_text(proposal.code, encoding='utf-8')
    judge = subprocess.run([sys.executable, '-I', '-m', 'unittest', 'discover', '-s', str(folder), '-v'],
                           capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
    verdict = (judge.stdout + judge.stderr).strip().splitlines()
    tail = [l for l in verdict if l.startswith(('Ran ', 'OK', 'FAILED'))]
    failed = [l.split()[1] for l in verdict if l.startswith('FAIL:') or l.startswith('ERROR:')]
    usage = (result or {}).get('usage')
    if usage is None and (folder / 'usage.json').is_file():
        # deliver() writes usage.json before raising AgentFailed: a failed turn still spent
        usage = json.loads((folder / 'usage.json').read_text(encoding='utf-8'))
    summary = {'runner': runner, 'model': model, 'wall_s': round(wall, 1), 'tool_calls': tools,
               'usage': usage, 'failure': str(failure) if failure else None,
               'deliverable': str(target), 'bytes': target.stat().st_size if target.exists() else 0,
               'judge': tail, 'failed_tests': failed}
    (folder / 'solo-summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))
    return 0 if judge.returncode == 0 else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
