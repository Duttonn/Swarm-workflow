"""Zero-token end-to-end run of the parts pipeline.

Only the model call is faked. Prototype, part assignment, mechanical assembly, the budget
gate, reviews, the finisher, candidate grading, materialize, the acceptance gate and (with
DRY_SANDBOX=docker) the swarm container are the real ones.

The fake finisher deliberately returns a BROKEN file: shipping the assembly instead is the
regression guard for run 60dd4052, where the integrator replaced fifteen agents' work with
one copy and nothing checked it.

Run: .venv\\Scripts\\python.exe bench\\check_swarm_dry.py        agents on the host
     DRY_SANDBOX=docker, same command                          agents in the swarm container
"""
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
# factory.ps1 sets PYTHONIOENCODING=utf-8; without it the SSSF console dies printing its glyphs.
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')
# One worker launches agents in order; the stagger gives the owner thread time to book each
# finished agent's tokens before the next launch reads them, so the gate is deterministic.
os.environ['SWARM_MAX_PARALLEL'] = '1'
os.environ['SWARM_STAGGER_SECONDS'] = '0.5'
SANDBOX = os.environ.get('DRY_SANDBOX', 'host')
os.environ['SWARM_AGENT_SANDBOX'] = SANDBOX
if SANDBOX == 'docker':
    os.environ['GEMINI_API_KEY'] = 'not-a-real-key-dry-run'   # lets the container start; unused
sys.path.insert(0, str(ROOT / 'adws'))
sys.path.insert(0, str(ROOT))

from adw_modules import agy_swarm, session
from adw_modules.data_types import ConfigDefaults, ObservabilityConfig, SSSFConfig
from adw_modules.docker_sandbox import docker_env, docker_path

# Sized against the stage ceilings: the build stage stops at 60% of the cap and the reviews at
# 85%, so this scenario runs one review and has the next two refused.
CAP = 2300
SPENT = {'prototype': 400, 'part:a': 400, 'part:b': 1000, 'review:a': 200, 'finisher': 300}
DRAFT = ('"""dry run draft"""\n\n\n'
         '# part:a\n'
         'def answer():\n'
         '    return 0\n'
         '# /part:a\n\n\n'
         '# part:b\n'
         'def helper():\n'
         '    return 1\n'
         '# /part:b\n')
BLOCKS = {'part:a': 'def answer():\n    return 42', 'part:b': 'def helper():\n    return 7'}
SEEN, BOXES = {}, {}


def stage_of(prompt):
    for needle, stage in (('You are the prototype agent', 'prototype'),
                          ('You own exactly ONE block', 'part'),
                          ('REVIEW TURN', 'review'),
                          ('You are the finisher', 'finisher')):
        if needle in prompt:
            return stage
    raise AssertionError('prompt matches no stage:\n' + prompt[-400:])


def fake_invoke(req, messages, cancel):
    req.folder.mkdir(parents=True, exist_ok=True)
    stage = stage_of(req.prompt)
    key = stage if stage in ('prototype', 'finisher') else '%s:%s' % (stage, req.agent)
    SEEN[key] = req.prompt
    BOXES[key] = req.sandbox
    messages.put((req.agent, {'event': 'step_update', 'step_update': {
        'step_type': 'tool', 'tool_name': 'read_file', 'agent': req.agent}}))
    if stage == 'part':
        # verify.py makes agents put their block on disk; b dies right after doing so, which is
        # exactly what the salvage path exists for
        (req.folder / 'block.py').write_text(BLOCKS[key], encoding='utf-8')
    if key == 'part:b':
        raise agy_swarm.AgentFailed('b produced no reply text', SPENT[key])
    body = {'prototype': DRAFT,
            'part:a': BLOCKS['part:a'],
            'review:a': '',
            # the finisher hands back the pre-part draft: broken, and it must not be shipped
            'finisher': DRAFT}[key]
    proposal = agy_swarm.Proposal.model_validate(agy_swarm.coerce_proposal({
        'status': 'success', 'summary': ('ACCEPT ' if stage == 'review' else '') + key,
        'code': body, 'decisions': ['d'],
        'risks': ['part:a - naming could be clearer - rename it'] if stage == 'review' else []}))
    return proposal, {'usage': {'total_tokens': SPENT[key]}}


def main():
    data = ROOT / 'work' / 'dryrun'
    data.mkdir(parents=True, exist_ok=True)
    db = data / 'sssf.db'
    cfg = SSSFConfig(defaults=ConfigDefaults(data_dir='work/dryrun'),
                     observability=ObservabilityConfig(db=str(db)))
    agy_swarm.invoke = fake_invoke
    run = session.ensure(cfg)
    spec = {'prompt': 'return 42', 'definition_of_done': 'answer() returns 42',
            'contract': 'def answer() -> int', 'agents': ['a', 'loser', 'b'],
            'budget': {'tokens': CAP},
            'tests': ('import unittest\nfrom solution import answer\n\n\n'
                      'class T(unittest.TestCase):\n    def test_answer(self):\n'
                      '        self.assertEqual(answer(), 42)\n')}
    accepted, final = agy_swarm.execute_swarm(run, spec)
    run.finish(accepted=accepted, reason='dry run')

    # the prototype cut the blocks, a filled its own, b died after spending, the reviews were
    # gated at the cap except the first, and the finisher ran anyway
    assert set(SEEN) == {'prototype', 'part:a', 'part:b', 'review:a', 'finisher'}, sorted(SEEN)
    assert run.tokens == sum(SPENT.values()), run.tokens
    assert accepted, 'acceptance gate failed on a correct assembly'

    session_dir = Path(run.session_dir)
    shipped = (session_dir / 'deliverable' / 'solution.py').read_text(encoding='utf-8')
    assert 'return 42' in shipped, 'the fragment never reached the deliverable'
    assert '# part:a' in shipped, 'the markers must survive for the next run to cut the same way'
    assert 'return 0' not in shipped, 'the broken finisher file was shipped'

    board = session_dir / 'board'
    for name in ('010--prototype.py', '020--assembled.py', 'a--part.py', 'budget.json', 'budget.py'):
        assert (board / name).exists(), 'missing on the board: %s' % name
    assert (board / 'a--part.py').read_text(encoding='utf-8').strip() == BLOCKS['part:a']

    # the tool every agent is told to run must really judge their block, both ways
    verify = session_dir / 'a' / 'part' / 'verify.py'
    assert verify.exists(), 'no verify.py in the agent workspace'
    good = subprocess.run([sys.executable, 'verify.py'], cwd=verify.parent, capture_output=True,
                          text=True, timeout=120)
    assert 'the file passes' in good.stdout, good.stdout[-400:] + good.stderr[-200:]
    (verify.parent / 'block.py').write_text('def answer():\n    return 41', encoding='utf-8')
    bad = subprocess.run([sys.executable, 'verify.py'], cwd=verify.parent, capture_output=True,
                         text=True, timeout=120)
    assert 'FAILING' in bad.stdout, bad.stdout[-400:] + bad.stderr[-200:]

    # every prompt names the board and the budget tool as the agent sees them; the part prompt
    # names the block it owns and nothing else
    if SANDBOX == 'docker':
        seen_board, seen_draft = '/workspace/board', '/workspace/board/010--prototype.py'
    else:
        seen_board, seen_draft = str(board), str(board / '010--prototype.py')
    assert all(seen_board + '/budget.py' in p for p in SEEN.values()), 'budget tool path'
    assert 'part:a' in SEEN['part:a'] and seen_draft in SEEN['part:a']
    assert all((b is not None) == (SANDBOX == 'docker') for b in BOXES.values()), BOXES
    if SANDBOX == 'docker':
        left = subprocess.run([docker_path(), 'ps', '-a', '--format', '{{.Names}}'],
                              capture_output=True, text=True, env=docker_env()).stdout
        assert 'swarm-%s-agents' % run.adw_id not in left, 'agent container outlived the swarm'

    with closing(sqlite3.connect(db)) as con:
        rows = con.execute('SELECT type, name, payload_json FROM events WHERE adw_id=?',
                           (run.adw_id,)).fetchall()
        total, status = con.execute('SELECT total_tokens, status FROM sessions WHERE adw_id=?',
                                    (run.adw_id,)).fetchone()
    # losing agents is tolerated, so an accepted swarm must be recorded as a success
    assert status == 'success', 'accepted swarm recorded as %s' % status
    assert total == sum(SPENT.values()), 'trace undercounts failed agents: %s' % total
    payload = {(t, n): json.loads(p) for t, n, p in rows}
    assert payload[('parts', 'assignment')]['owners'] == ['a', 'b'], payload[('parts', 'assignment')]
    assert payload[('parts', 'assignment')]['reviewers'] == ['loser']
    assert payload[('assembled', 'parts')]['filled'] == ['a', 'b'], payload[('assembled', 'parts')]
    assert payload[('assembled', 'parts')]['missing'] == []
    assert ('salvaged', 'b') in payload, 'the block b left on disk before dying was not salvaged'
    assert 'return 7' in shipped, 'the salvaged block never reached the deliverable'
    graded = payload[('artifact', 'solution.py')]
    assert graded['candidate'] == 'assembled', graded
    assert graded['candidates'] == {'assembled': True, 'finished': False}, graded
    errors = [json.loads(p)['error'] for t, _, p in rows if t == 'error']
    assert any('not started' in e for e in errors), errors
    assert any('no reply text' in e for e in errors), errors
    assert payload[('review', 'verdicts')]['defects'] == 1, payload[('review', 'verdicts')]

    budget = json.loads((board / 'budget.json').read_text(encoding='utf-8'))
    assert budget['cap'] == CAP and budget['spent'] == sum(SPENT.values()), budget
    shown = subprocess.run([sys.executable, str(board / 'budget.py')],
                           capture_output=True, text=True, timeout=30)
    assert 'low' in shown.stdout, shown.stdout

    print('swarm dry run ok (%s): run %s, %s tokens, %d blocks cut, shipped the %s candidate'
          % (SANDBOX, run.adw_id, f'{run.tokens:,}',
             len(payload[('parts', 'assignment')]['owners']), graded['candidate']))


if __name__ == '__main__':
    main()
