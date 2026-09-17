"""Solo first; on a red gate, either the same agent repairs its page or the swarm takes over.

Every ladder measured here ends the same way: one agent scores what twenty do, for a tenth of
the tokens and a twentieth of the time (bench/results/nim-sweep-2026-09-15.md), and the
literature agrees that the single-then-multi policy is the practical one (arXiv 2603.21489).
What a soloist gets wrong is not the tests, it is the page: fourteen empty panels behind a
green suite (bench/results/ui-verdicts-2026-09-16.md). So the gate here is both: the fixed
suite and the browser gate with the spec's click scenario. A red gate hands the brief on:
with --repair to the same soloist with the gate's findings as a defect list, up to
SWARM_REPAIR_ROUNDS (3) turns on a copy of its page, otherwise to the swarm.

Run: .venv\\Scripts\\python.exe bench\\solo_first.py <spec.json> <runner> [model] [--repair] [--resume <folder>]
     Swarm side reads SWARM_RUNNER / SWARM_MODEL / SWARM_WHOLE_FILE_MODEL as usual; the soloist
     uses the runner and model given here. Writes work/measured/<spec>-<model>/<stamp>/solo-first.json.
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bench'))
sys.path.insert(0, str(ROOT / 'adws'))
import solo_run  # noqa: E402
from adw_modules import agy_swarm, ui_gate  # noqa: E402


def gate(folder, spec, out_name):
    """Tests and the browser gate on the deliverable in `folder`."""
    judge = subprocess.run([sys.executable, '-I', '-m', 'unittest', 'discover', '-s', str(folder), '-v'],
                           capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
    tests_green = judge.returncode == 0
    lines = (judge.stdout + judge.stderr).splitlines()
    tail = [l for l in lines if l.startswith(('Ran ', 'OK', 'FAILED'))]
    # The reasons, not only the count: the deepseek repair turn given "FAILED (failures=21)"
    # spent 29 shell calls finding a SyntaxError the suite had already printed.
    reasons = []
    for l in lines:
        if l.startswith(('AssertionError', 'SyntaxError', 'TypeError', 'ReferenceError')):
            short = l[:240]
            if short not in reasons:
                reasons.append(short)
    tail += ['first failure: ' + r for r in reasons[:3]]
    page = folder / out_name
    ui = agy_swarm.page_verdict(page, page.suffix, spec.get('ui_scenario')) if page.is_file() else None
    # A gate that crashed is not a gate that passed: `ok: None` with an error is red (a
    # missing import in ui_gate once let a 5/17 page through as green).
    return {'tests_green': tests_green, 'judge': tail, 'ui': ui, 'shape': shape_defects(page),
            'green': tests_green and (ui is None or ui.get('ok') is True)}


def shape_defects(page):
    """What is wrong with the file as a file, before any test: the deepseek kanban solo stopped
    mid-function after seven appends, and its repair turn then spent 25 tool calls and 600k
    tokens writing inspect scripts to discover that the script tag was never closed. Say it."""
    if not page.is_file():
        return ['the deliverable %s does not exist' % page.name]
    text = page.read_text(encoding='utf-8', errors='replace')
    out = []
    if page.suffix == '.html':
        body = text.rstrip()
        if not body.lower().endswith('</html>'):
            lines = text.count('\n') + 1
            out.append('the file is TRUNCATED: %d lines, it ends with %r and never reaches </html>; the '
                       'last write stopped mid-way. Finish the file from that point with write_file '
                       'append: true (do not rewrite it from the top), then close every open tag.'
                       % (lines, body[-60:]))
        opened = len(re.findall(r'<script\b', text, re.I))
        closed = len(re.findall(r'</script\s*>', text, re.I))
        if opened != closed:
            out.append('%d <script> tags are opened and %d closed' % (opened, closed))
        if not re.search(r'<script[^>]*\bid=["\']app["\']', text, re.I):
            out.append('there is no <script id="app"> block; the acceptance suite extracts that '
                       'block alone and runs it in node, so without it every test fails')
    return out


def score(g):
    """One number for a gate verdict, higher is better: failing tests weigh most, then the
    clicks a person can complete, then panels left empty."""
    failing = 0
    for line in g.get('judge') or []:
        failing += sum(int(n) for n in re.findall(r'(?:failures|errors)=(\d+)', line))
    if not g.get('tests_green') and not failing:
        failing = 1                        # red with no count: the suite did not even run
    ui = g.get('ui') or {}
    steps = ui.get('steps') or 0                    # page_verdict stores the count, judge() the list
    steps = steps if isinstance(steps, int) else len(steps)
    clicks = steps - len(ui.get('failed_steps') or [])
    return -100 * failing + 10 * clicks - 5 * len(ui.get('empty_panels') or []) - (50 if ui.get('errors') else 0)


def repair(folder, spec, out_name, runner, model, defects):
    """One more turn of the same soloist, in the same folder, with the gate's list."""
    task = ('You built %s in this folder. The acceptance suite and a headless browser that clicks '
            'through the page found the defects below: what a person who opens the page cannot '
            'do, and what the suite refuses. Fix ONLY those, in place, with edit calls on the '
            'existing file; keep everything that works. Do not read the whole file back at every '
            'step: locate with grep -n, read the line range you need, edit, run the suite. '
            'Run the suite again before you finish. '
            'Put code = "" in the json block; the file on disk is the delivery.\nDEFECTS:\n%s\n'
            % (out_name, '\n'.join(defects)))
    prompt = solo_run.ENVELOPE % (spec['prompt'], spec['definition_of_done'], spec['contract']) + task
    # its own folder, with a copy of the page and the suite: the runner writes its stream and
    # usage next to the file it works on, and those must not overwrite the solo's
    work = folder / 'repair'
    work.mkdir(exist_ok=True)
    for name in (out_name, 'test_acceptance.py'):
        if (folder / name).is_file():          # a soloist that timed out left no page at all
            (work / name).write_text((folder / name).read_text(encoding='utf-8'), encoding='utf-8')
    req = agy_swarm.AgentRequest('repair', prompt, work, model=model, runner=runner,
                                 timeout=int(os.environ.get('SWARM_SOLO_TIMEOUT', '1800')))
    started = time.monotonic()
    try:
        agy_swarm.invoke(req, queue.Queue(), threading.Event())
        failure = None
    except agy_swarm.AgentFailed as exc:
        failure = str(exc)
    return {'minutes': round((time.monotonic() - started) / 60, 1), 'failure': failure,
            'usage': json.loads((folder / 'repair' / 'usage.json').read_text(encoding='utf-8'))
            if (folder / 'repair' / 'usage.json').is_file() else None}


def main(argv):
    # --resume <folder>: skip the solo and run the gate + repair loop on a page that exists,
    # for a solo that ended without a verdict (a harness bug, a killed process)
    resume = Path(argv[argv.index('--resume') + 1]) if '--resume' in argv else None
    args = [a for a in argv if a not in ('--repair', '--resume') and (resume is None or a != str(resume))]
    do_repair = '--repair' in argv
    if len(args) < 3:
        print(__doc__)
        return 2
    started = time.monotonic()
    spec = json.loads(Path(args[1]).read_text(encoding='utf-8'))
    if resume is None:
        solo_run.main(args)
    stamp_root = Path(os.environ.get('SWARM_SOLO_ROOT') or ROOT / 'work' / 'measured')
    slug = Path(args[1]).stem.split('-', 1)[-1]
    tag = (args[3] if len(args) > 3 else args[2]).split('/')[-1].replace(':', '-')
    folder = resume or sorted((stamp_root / ('%s-%s' % (slug, tag))).iterdir())[-1]
    summary = json.loads((folder / 'solo-summary.json').read_text(encoding='utf-8')) \
        if (folder / 'solo-summary.json').is_file() else {'resumed': True}
    out_name = spec['output_file']
    first = gate(folder, spec, out_name)
    verdict = {'solo': summary, 'first_gate': first, 'solo_minutes': round((time.monotonic() - started) / 60, 1),
               'repair': None, 'second_gate': None, 'swarm': None}
    ui = first['ui'] or {}
    print('solo gate: %s (tests %s, ui %s)' % ('green' if first['green'] else 'red',
                                              'green' if first['tests_green'] else 'red',
                                              'n/a' if not first['ui'] else ('ok' if ui.get('ok') else
                                              '%d empty, %d failed steps' % (len(ui.get('empty_panels') or []), len(ui.get('failed_steps') or [])))))
    green = first['green']
    last = first
    rounds = int(os.environ.get('SWARM_REPAIR_ROUNDS', '3'))
    verdict['repairs'] = []
    rejected = None
    while not green and do_repair and len(verdict['repairs']) < rounds:
        # the same agent, the gate's current list, one more turn; a verifier loop on one agent
        ui = last['ui'] or {}
        defects = agy_swarm.ui_defects(ui) if last['ui'] else []
        if not last['tests_green']:
            defects.insert(0, 'the acceptance suite fails: ' + ' '.join(last['judge']))
        defects = (last.get('shape') or []) + defects      # the file's own shape comes first
        if rejected:
            defects.insert(0, rejected)
        print('repair turn %d with %d defect(s)' % (len(verdict['repairs']) + 1, len(defects)))
        r = repair(folder, spec, out_name, args[2], args[3] if len(args) > 3 else None, defects)
        made = folder / 'repair' / out_name
        before = (folder / out_name).read_text(encoding='utf-8') if (folder / out_name).is_file() else None
        if made.is_file():
            (folder / out_name).write_text(made.read_text(encoding='utf-8'), encoding='utf-8')
        attempt = gate(folder, spec, out_name)
        # Forced optimisation (ReLook, ACL 2026): a revision is kept only when it scores
        # strictly better than the page it replaces; a base model with feedback otherwise
        # collapses after two or three rounds. The rejected page is not lost: the next turn
        # hears what it broke and starts again from the kept one.
        if score(attempt) > score(last):
            last, rejected = attempt, None
        else:
            if before is None:
                (folder / out_name).unlink(missing_ok=True)
            else:
                (folder / out_name).write_text(before, encoding='utf-8')
            rejected = ('your previous repair attempt was DISCARDED: it scored %d against %d for the page '
                        'before it (tests %s, %d failed steps, %d empty panels). The file is back to the '
                        'version before that attempt; make a smaller, targeted change this time.'
                        % (score(attempt), score(last), 'green' if attempt['tests_green'] else 'red',
                           len((attempt['ui'] or {}).get('failed_steps') or []),
                           len((attempt['ui'] or {}).get('empty_panels') or [])))
            r['rejected'] = True
        r['gate'] = attempt
        r['score'] = score(attempt)
        verdict['repairs'].append(r)
        green = last['green']
        print('after repair %d: %s (%d failed steps)%s' % (len(verdict['repairs']), 'green' if green else 'red',
                                                          len((last['ui'] or {}).get('failed_steps') or []),
                                                          ' - attempt discarded' if r.get('rejected') else ''))
    verdict['repair'] = verdict['repairs'][0] if verdict['repairs'] else None
    verdict['second_gate'] = last if verdict['repairs'] else None
    if not green and not do_repair:
        print('handing the brief to the swarm')
        just = ROOT / 'tools' / 'just' / 'just.exe'
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        env.setdefault('SSSF_DB', str(ROOT / 'adws' / 'adw_data' / 'sssf.db'))
        env.setdefault('SSSF_BLUEPRINTS', str(ROOT / 'blueprints'))
        done = subprocess.run([str(just), 'swarm', args[1]], cwd=ROOT, env=env,
                              capture_output=True, text=True, encoding='utf-8', errors='replace')
        tail = [l for l in (done.stdout + done.stderr).splitlines() if '"run_id"' in l]
        verdict['swarm'] = json.loads(tail[-1]) if tail else {'error': (done.stderr or done.stdout)[-400:]}
    verdict['gate'] = 'green' if green else 'red'
    verdict['total_minutes'] = round((time.monotonic() - started) / 60, 1)
    (folder / 'solo-first.json').write_text(json.dumps(verdict, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in verdict.items() if k != 'solo'}, indent=2))
    return 0 if green or (verdict['swarm'] or {}).get('accepted') else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.exit(main(sys.argv))
