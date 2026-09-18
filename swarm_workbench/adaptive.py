"""Evidence-based team sizing and bounded, role-specific startup context.

Learning is local policy data, not model training. Only comparable completed runs
can establish a measured winner; a cold-start choice is labelled a heuristic.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from .blueprints import rank, warm_start

ROOT = Path(__file__).resolve().parents[1]

# Roles that judge rather than build. A reduced roster may drop them without losing a region
# of the file, and they are not counted when sizing the build work.
REVIEWERS = ('skeptic', 'referee', 'measurer', 'qa', 'reviewer', 'critic')


def build_roles(spec):
    """The contract's features: every declared role that owns a region rather than judging."""
    return [r for r in (spec.get('agents') or []) if not r.lower().startswith(REVIEWERS)]


def history_path():
    return Path(os.environ.get('SWARM_LEARNING_PATH', ROOT / 'work/adaptive/history.json'))


def fingerprint(spec):
    contract = {k: spec.get(k) for k in ('prompt', 'contract', 'tests', 'output_file', 'ui_scenario')}
    return hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()


def read_history(path=None):
    path = Path(path or history_path())
    if not path.is_file():
        return []
    records = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(records, list):
        raise ValueError('learning history must be a list')
    return records


def remember(record, path=None):
    """Serialize writers with an exclusive lock; never silently lose another run."""
    path = Path(path or history_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix('.lock')
    deadline = time.monotonic() + 5
    while True:
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise RuntimeError('learning store is locked; run evidence remains in trace')
            time.sleep(.05)
    try:
        records = [r for r in read_history(path) if r.get('run_id') != record['run_id']]
        records.append(record)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(records[-500:], indent=2), encoding='utf-8')
        temp.replace(path)
    finally:
        os.close(handle)
        lock.unlink()


def plan(spec, runner, model, records=(), requested=None, maximum=4):
    """Choose a roster without calling a model or inferring success from a token count."""
    roster = spec.get('agents') or ['builder', 'reviewer']
    test_count = spec.get('tests', '').count('def test_')
    features = max(test_count, len(roster))
    difficulty = 'small' if features <= 10 else 'medium' if features <= 20 else 'large'
    size = {'small': 1, 'medium': 2, 'large': 3}[difficulty]
    reasons = ['Cold-start heuristic: %s fixed tests, %s declared roles.' % (test_count, len(roster))]
    # Difficulty alone sized run cd0d13f3 at two owners for a sixteen-feature contract, and the
    # measured result was fourteen empty panels: one turn does not build eight regions, whoever
    # is driving. What the roster really divides is features per owner, so size on that and let
    # `maximum` be the ceiling.
    build = build_roles(spec)
    per_owner = int(os.environ.get('SWARM_FEATURES_PER_OWNER', '4'))
    floor = 1
    if build and len(build) > per_owner * size:
        floor = size = min(maximum, -(-len(build) // per_owner))
        reasons.append('%d contract features at %d per owner needs %d owners.'
                       % (len(build), per_owner, size))
    key = fingerprint(spec)
    whole = os.environ.get('SWARM_WHOLE_FILE_MODEL') or spec.get('whole_file_model') or model
    matching = [r for r in records if r.get('spec_hash') == key and r.get('runner') == runner
                and r.get('model') == model and r.get('quality_kind') == 'fixed_tests_and_required_ui'
                and r.get('accepted') is True and r.get('elapsed_seconds') is not None
                and r.get('whole_file_model', whole) == whole]
    sources = []
    if matching:
        # Ranking accepted runs by elapsed time rewards a lucky sample: one 60-second success
        # among ninety-nine failures beats ten reliable 120-second runs, and the failures cost
        # real wall clock and real quota. Rank on what a working artifact actually costs -
        # every attempt's seconds divided by the artifacts that came out acceptable - so an
        # unreliable size pays for its own failures.
        attempts = [r for r in records if r.get('spec_hash') == key and r.get('runner') == runner
                    and r.get('model') == model and r.get('elapsed_seconds') is not None]
        groups = {}
        for r in attempts:
            groups.setdefault(int(r.get('agents') or 1), []).append(r)
        won = {r['run_id'] for r in matching}
        eligible = {n: rs for n, rs in groups.items()
                    if 1 <= n <= maximum and any(r.get('run_id') in won for r in rs)}
        if eligible:
            def paid(rs):
                kept = sum(1 for r in rs if r.get('run_id') in won)
                return sum(r['elapsed_seconds'] for r in rs) / kept
            size = min(eligible, key=lambda n: (paid(eligible[n]), n))
            sources = [r['run_id'] for r in eligible[size] if r.get('run_id') in won]
            reasons = ['Fewest seconds paid per accepted artifact (%.0fs at %d owners), counting '
                       'the failed attempts at each size.' % (paid(eligible[size]), size),
                       '%s of %s attempts at this size were accepted; this is not proof of a '
                       'universal optimum.' % (len(sources), len(eligible[size]))]
    recent_failures = [r for r in records if r.get('spec_hash') == key and r.get('runner') == runner
                       and r.get('model') == model and r.get('accepted') is False]
    if not matching and recent_failures:
        last = recent_failures[-1]
        if last.get('stop_reason'):
            # An infrastructure stop is not an argument for a bigger swarm, but it is not an
            # argument for leaving regions unowned either: the workload floor still holds.
            reasons.append('Previous infrastructure/budget stop: adding agents is not a remedy.')
            size = max(floor, min(size, int(last.get('agents', 1))))
        else:
            size = min(maximum, int(last.get('agents', 1)) + 1)
            reasons.append('Previous quality failure: try one additional owner within the limit.')
    if requested is not None:
        if requested < 1:
            raise ValueError('requested team size must be positive')
        size = requested
        reasons = ['Explicit team size requested for a controlled comparison.']
    size = min(size, maximum, len(roster))
    return {'selected_agents': size, 'difficulty': difficulty, 'reasons': reasons,
            'source_runs': sources, 'runner': runner, 'model': model, 'spec_hash': key,
            'roles': roster[:size], 'skills': [],
            'whole_file_model': whole,
            'token_limit': int(os.environ.get('SWARM_RUN_TOKEN_LIMIT', '180000')),
            'output_limit': int(os.environ.get('SWARM_AGENT_OUTPUT_LIMIT', '12000')),
            'call_timeout_seconds': int(os.environ.get('SWARM_CALL_TIMEOUT', '240')),
            'idle_timeout_seconds': int(os.environ.get('SWARM_IDLE_TIMEOUT', '45'))}


def startup_context(spec, root=ROOT):
    """Reuse the existing provenance-checked blueprint mechanism, not prior output code."""
    candidates = []
    for path in (Path(root) / 'blueprints').glob('bp-*.json'):
        try:
            bp = json.loads(path.read_text(encoding='utf-8'))
            if bp.get('verified_success') and bp.get('lessons'):
                candidates.append(bp)
        except (OSError, ValueError):
            continue
    hits = rank(candidates, spec['prompt'], spec.get('concepts', []), spec.get('context', {}))
    if not hits:
        return None
    context = warm_start(hits[0]['blueprint'], spec['prompt'], root)
    context['suggested_steps'] = context.get('suggested_steps', [])[:4]
    context['lessons'] = context.get('lessons', [])[:4]
    context['file_checks'] = context.get('file_checks', [])[:8]
    # Context remains data, and never replaces current task instructions or grants permissions.
    return context if len(json.dumps(context)) <= 6000 else None


# One ordinary SKILL.md is bigger than the old 5,000-character budget: the only skill the
# chooser found for the kanban brief is 6,625, so every plan shipped `skills: []` while the
# sidecar sat on disk naming it. Two skills is the most a role is given, so 12,000 holds a
# normal one without letting a long document take the agent's context.
def skill_context(spec, role, limit=12000):
    """Inject only explicitly selected local skills, with bounded text and provenance.

    `role_skills` maps a role (or '*') to paths. Vercel discovery happens once
    before launch; agents do not each download a catalog or install arbitrary code.
    """
    mapping = spec.get('role_skills') or {}
    selected = list(mapping.get('*', [])) + list(mapping.get(role, []))
    texts, metadata, left = [], [], limit
    for raw in selected[:2]:
        path = Path(raw).expanduser().resolve()
        if path.name != 'SKILL.md' or not path.is_file():
            raise ValueError('role skill must reference an existing SKILL.md: %s' % path)
        content = path.read_text(encoding='utf-8')
        if len(content) > left:
            # Partial instructions can drop a guardrail. Omit rather than truncate them - but
            # say so, because a silently empty skill list looks like a chooser that found nothing.
            print('role skill %s omitted for %s: %d characters, %d left' %
                  (path.parent.name, role, len(content), left), file=sys.stderr)
            continue
        left -= len(content)
        metadata.append({'name': path.parent.name, 'role': role, 'path': str(path),
                         'sha256': hashlib.sha256(content.encode()).hexdigest()})
        texts.append('ROLE SKILL %s (task constraints take precedence):\n%s' % (path.parent.name, content))
    return '\n'.join(texts), metadata


def import_history(db, specs, path=None):
    """Read old traces without rewriting their status or calling unfinished work free."""
    lookup = {s['prompt']: s for s in specs}
    con = sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    imported = []
    try:
        rows = con.execute("SELECT * FROM sessions WHERE ended_at IS NOT NULL ORDER BY started_at").fetchall()
        for row in rows:
            spec = lookup.get(row['request'])
            if not spec:
                continue
            events = con.execute('SELECT type,name,payload_json,started_at FROM events WHERE adw_id=? ORDER BY rowid',
                                 (row['adw_id'],)).fetchall()
            contracts = [json.loads(e['payload_json']) for e in events if e['type'] == 'run_contract']
            if not contracts:
                continue
            contract = contracts[-1]
            accepted = row['status'] == 'success'
            ui = [json.loads(e['payload_json']) for e in events if e['type'] == 'candidate']
            requires_ui = str(spec.get('output_file', '')).endswith('.html')
            strict = not requires_ui or any(c.get('passed') is True and (c.get('ui') or {}).get('ok') is True for c in ui)
            errors = [json.loads(e['payload_json']).get('error', '') for e in events if e['type'] == 'error']
            def epoch(value):
                return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
            elapsed = max(0, epoch(row['ended_at']) - epoch(row['started_at']))
            init = next((max(0, epoch(e['started_at']) - epoch(row['started_at'])) for e in events
                         if e['type'] == 'parts'), None)
            record = {'run_id': row['adw_id'], 'spec_hash': fingerprint(spec),
                      'runner': contract.get('runner', 'unknown'), 'model': contract.get('model', 'unknown'),
                      'agents': len(contract.get('agents', [])), 'accepted': accepted and strict,
                      'elapsed_seconds': round(elapsed, 2), 'tokens': row['total_tokens'] or None,
                      'init_seconds': init, 'stop_reason': errors[-1][:300] if errors and not accepted else '',
                      'source': 'historical', 'quality_kind': 'fixed_tests_and_required_ui' if strict else 'fixed_tests_only'}
            remember(record, path)
            imported.append(record)
    finally:
        con.close()
    return imported
