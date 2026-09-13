"""Measure what a swarm run actually did: startup, division of labour, overlap, outcome.

The three questions this answers, per run:
  1. What did the swarm pay before any real division of labour existed (the startup phase)?
  2. Did the agents end up owning genuinely different work, or the same work N times?
  3. Did the result beat the single-agent baseline on the same fixed tests?

Works on both harness shapes: the old whole-file peer rounds and the current parts pipeline.

Run: .venv\\Scripts\\python.exe bench\\measure_run.py <run_id> [more run ids...]
"""
import difflib
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'adws'))
from adw_modules.agy_swarm import part_spans  # noqa: E402

DB = ROOT / 'adws' / 'adw_data' / 'sssf.db'
SESSIONS = ROOT / 'adws' / 'adw_data' / 'sessions'
OUT = ROOT / 'work' / 'measure'
STOP = set('the a an and or of to in on for with is are be this that it its as at by from not '
           'will must should each per into will i we you they'.split())


def words(text):
    return {w for w in re.findall(r'[a-z]{3,}', (text or '').lower()) if w not in STOP}


def similarity(a, b, cap=4000):
    """Line-level similarity, the way a diff sees two files.

    quick_ratio compares character multisets and scores two different files of the same flavour
    near 1; a character-level matcher is quadratic and has to be truncated, which then only
    compares the beginnings. Lines are the honest unit here, and the sequences stay small.
    """
    return difflib.SequenceMatcher(None, (a or '').splitlines()[:cap],
                                   (b or '').splitlines()[:cap]).ratio()


def jaccard(a, b):
    return len(a & b) / len(a | b) if (a or b) else 0.0


def gini(values):
    """0 = every owner holds an equal share of the artifact, 1 = one owner holds it all."""
    xs = sorted(v for v in values if v >= 0)
    if not xs or sum(xs) == 0:
        return 0.0
    n = len(xs)
    return (2 * sum((i + 1) * x for i, x in enumerate(xs))) / (n * sum(xs)) - (n + 1) / n


def call_tokens(folder):
    ev = folder / 'events.jsonl'
    if not ev.exists():
        return 0
    for line in reversed(ev.read_text(encoding='utf-8', errors='replace').splitlines()):
        try:
            e = json.loads(line.lstrip('﻿'))
        except ValueError:
            continue
        if e.get('type') == 'result':
            return (e.get('stats') or {}).get('total_tokens') or 0
    return 0


def seconds(a, b):
    if not a or not b:
        return 0.0
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()


def load(run_id):
    con = sqlite3.connect('file:%s?mode=ro' % DB.as_posix(), uri=True)
    row = con.execute('select status, total_tokens, started_at, ended_at, request '
                      'from sessions where adw_id=?', (run_id,)).fetchone()
    if not row:
        raise SystemExit('no such run: %s' % run_id)
    phases = con.execute('select name, status, started_at, ended_at from phases where adw_id=? '
                         'order by seq', (run_id,)).fetchall()
    events = con.execute('select type, name, payload_json from events where adw_id=? order by rowid',
                         (run_id,)).fetchall()
    return row, phases, events


def measure(run_id):
    (status, total, started, ended, request), phases, events = load(run_id)
    sess = SESSIONS / run_id
    board = sess / 'board'
    payload = {}
    for kind, name, blob in events:
        try:
            payload.setdefault(kind, {})[name] = json.loads(blob)
        except ValueError:
            continue
    parts = payload.get('parts', {}).get('assignment')
    shape = 'parts' if parts else 'whole-file'

    # --- stage costs -------------------------------------------------------------------
    stage_of = {'_r0': 'startup', '_r1': 'build', '_r2': 'review', '_r3': 'finish'}
    stages = {}
    for name, st, began, done in phases:
        suffix = name[-3:]
        stage = stage_of.get(suffix)
        if not stage:
            continue
        agent = name[:-3]
        folder = (sess / 'prototype' / 'try-1' if stage == 'startup' and shape == 'parts' else
                  sess / 'finisher' if stage == 'finish' and shape == 'parts' else
                  sess / agent / ('part' if stage == 'build' and shape == 'parts'
                                  else 'review' if stage == 'review' and shape == 'parts'
                                  else 'round-%s' % suffix[-1]))
        if stage == 'finish' and shape != 'parts':
            folder = sess / 'integrator'
        entry = stages.setdefault(stage, {'agents': 0, 'ok': 0, 'tokens': 0, 'seconds': 0.0})
        entry['agents'] += 1
        entry['ok'] += st == 'success'
        entry['tokens'] += call_tokens(folder)
        entry['seconds'] = max(entry['seconds'], seconds(began, done))

    # In the old shape the startup is round 1 up to the first board post; in the parts shape
    # it is the prototype, the only call made before anyone owns anything.
    startup = stages.get('startup', {'tokens': 0, 'seconds': 0.0, 'agents': 0, 'ok': 0})
    if shape != 'parts':
        startup = dict(stages.get('build', {}), note='no dedicated startup: round 1 is both')

    # --- division of labour ------------------------------------------------------------
    owners = parts['owners'] if parts else []
    reviewers = parts['reviewers'] if parts else []
    suffix = None
    for candidate in board.glob('020--assembled.*'):
        suffix = candidate.suffix
    blocks = {}
    if parts and suffix:
        for agent in owners:
            frag = board / ('%s--part%s' % (agent, suffix))
            blocks[agent] = len(frag.read_text(encoding='utf-8', errors='replace')) if frag.exists() else 0

    plans, raw = {}, {}
    for pattern in ('*--plan.md', '*--claim.md'):
        for path in board.glob(pattern):
            agent = path.name.split('--')[0]
            if agent in plans:
                continue
            text = path.read_text(encoding='utf-8', errors='replace')
            raw[agent], plans[agent] = text.lower(), words(text)
    pairs = [(a, b, jaccard(plans[a], plans[b]))
             for i, a in enumerate(sorted(plans)) for b in sorted(plans)[i + 1:]]
    # Did each agent write about its own slice, or about the whole artifact? Count mentions of
    # its own name against every other owner's name in its own post. 1.0 = perfectly on its slice.
    focus = []
    for agent, text in raw.items():
        own = text.count(agent.lower())
        other = sum(text.count(o.lower()) for o in (owners or plans) if o != agent)
        if own + other:
            focus.append(own / (own + other))
    plan_sim = {'pairs': len(pairs),
                'mean': round(sum(p[2] for p in pairs) / len(pairs), 3) if pairs else None,
                'worst': max(pairs, key=lambda p: p[2])[:3] if pairs else None,
                'focus': round(sum(focus) / len(focus), 2) if focus else None}

    # --- overlap -----------------------------------------------------------------------
    texts = {}
    if parts and suffix:
        for agent in owners:
            frag = board / ('%s--part%s' % (agent, suffix))
            if frag.exists():
                texts[agent] = frag.read_text(encoding='utf-8', errors='replace')
    else:
        for frag in board.glob('*--r1-code.*'):
            texts[frag.name.split('--')[0]] = frag.read_text(encoding='utf-8', errors='replace')
    # What survived of the startup: how close each finished block still is to the prototype's
    # version of it. Near 1 means the prototype did the work and the owner only polished; near 0
    # means the startup call was thrown away and paid for twice.
    kept = []
    draft_file = board / ('010--prototype' + (suffix or ''))
    if parts and suffix and draft_file.exists():
        draft = draft_file.read_text(encoding='utf-8', errors='replace')
        spans = part_spans(draft)
        for agent, frag in (texts or {}).items():
            if agent in spans:
                start, end = spans[agent]
                kept.append(round(similarity(draft[start:end], frag), 2))

    # Two agents doing the same work is a question of SCOPE, not of wording: it shows up as the
    # same named things appearing in both their outputs. Text similarity was the wrong proxy -
    # fifteen agents each drawing a whole pelican wrote fifteen different-looking files.
    handles = {a: set(re.findall(r'id="([^"]+)"', t)) | set(re.findall(r'function\s+([A-Za-z_]\w*)', t))
               for a, t in texts.items()}
    sims = [(a, b, round(jaccard(handles[a], handles[b]), 2))
            for i, a in enumerate(sorted(handles)) for b in sorted(handles)[i + 1:]
            if handles[a] and handles[b]]
    worst = max(sims, key=lambda s: s[2]) if sims else None
    duplicated = [s for s in sims if s[2] >= 0.5]

    # --- outcome -----------------------------------------------------------------------
    gate = payload.get('gate_pass', {}).get('acceptance') or payload.get('gate_fail', {}).get('acceptance') or {}
    ran = re.search(r'Ran (\d+) test', gate.get('output', '') or '')
    bad = sum(int(n) for n in re.findall(r'(?:failures|errors)=(\d+)', gate.get('output', '') or ''))
    artifact = next(iter(payload.get('artifact', {}).values()), {})
    report = {
        'run': run_id, 'shape': shape, 'task': (request or '')[:60], 'status': status, 'tokens': total,
        'minutes': round(seconds(started, ended) / 60, 1),
        'startup': {'kept_in_final': round(sum(kept) / len(kept), 2) if kept else None,
                    'tokens': startup.get('tokens', 0),
                    'share': round(100.0 * startup.get('tokens', 0) / total, 1) if total else 0,
                    'seconds': round(startup.get('seconds', 0.0)),
                    'agents': startup.get('agents', 0)},
        'stages': {k: {'agents': v['agents'], 'ok': v['ok'], 'tokens': v['tokens'],
                       'seconds': round(v['seconds'])} for k, v in stages.items()},
        'division': {'roster': len(owners) + len(reviewers) or None, 'owners': owners,
                     'reviewers': reviewers,
                     'block_bytes': blocks,
                     'block_gini': round(gini(list(blocks.values())), 2) if blocks else None,
                     'plan_similarity': plan_sim},
        'overlap': {'compared': len(sims), 'worst_pair': worst,
                    'near_duplicates': duplicated[:5],
                    'duplicate_count': len(duplicated)},
        'outcome': {'accepted': bool(payload.get('gate_pass', {}).get('acceptance')),
                    'tests_passed': (int(ran.group(1)) - bad) if ran else None,
                    'tests_ran': int(ran.group(1)) if ran else None,
                    'shipped_candidate': artifact.get('candidate'),
                    'candidates': artifact.get('candidates')},
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / ('%s.json' % run_id)).write_text(json.dumps(report, indent=1), encoding='utf-8')
    return report


def show(r):
    d, o, s = r['division'], r['overlap'], r['startup']
    print('== %s  (%s shape)  %s  %s tokens  %s min'
          % (r['run'], r['shape'], r['status'], f"{r['tokens']:,}", r['minutes']))
    print('   startup     %s tokens (%s%% of the run) over %ss, %d call(s), %s of it still in '
          'the final blocks' % (f"{s['tokens']:,}", s['share'], s['seconds'], s['agents'],
                                s.get('kept_in_final')))
    for stage in ('build', 'review', 'finish'):
        v = r['stages'].get(stage)
        if v:
            print('   %-11s %s tokens, %d/%d agents ok, %ss wall'
                  % (stage, f"{v['tokens']:,}", v['ok'], v['agents'], v['seconds']))
    if d['owners']:
        print('   division    %d owners, %d reviewers, block gini %s (0 = even split)'
              % (len(d['owners']), len(d['reviewers']), d['block_gini']))
    sim = d['plan_similarity']
    if sim['mean'] is not None:
        print('   task focus  own-slice focus %s (1 = only its own), plan overlap mean %s, worst %s'
              % (sim.get('focus'), sim['mean'], sim['worst']))
    print('   work overlap %d pairs compared on the names they produce, worst %s, pairs sharing '
          'half their scope: %d' % (o['compared'], o['worst_pair'], o['duplicate_count']))
    out = r['outcome']
    print('   outcome     accepted=%s tests %s/%s shipped=%s %s'
          % (out['accepted'], out['tests_passed'], out['tests_ran'],
             out['shipped_candidate'], out['candidates'] or ''))


def table():
    """Every run measured so far, oldest first: the point is the trend, not one number."""
    rows = [json.loads(p.read_text(encoding='utf-8')) for p in sorted(OUT.glob('*.json'))]
    rows.sort(key=lambda r: r.get('minutes', 0) and r['run'])
    head = ('run', 'shape', 'tokens', 'min', 'startup%', 'owners', 'dup pairs', 'tests', 'ok')
    print('%-10s %-11s %12s %5s %8s %7s %10s %7s %4s' % head)
    for r in rows:
        d, o, out = r['division'], r['overlap'], r['outcome']
        print('%-10s %-11s %12s %5s %7s%% %7s %10s %7s %4s'
              % (r['run'], r['shape'], f"{r['tokens']:,}", r['minutes'], r['startup']['share'],
                 len(d['owners']) or '-', '%d/%d' % (o['duplicate_count'], o['compared']),
                 '%s/%s' % (out['tests_passed'], out['tests_ran']), out['accepted']))


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if a != '--table']
    for rid in args:
        show(measure(rid))
    if '--table' in sys.argv[1:] or not args:
        table()
