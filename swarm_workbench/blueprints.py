"""Distill observable run evidence. Semantic labels are authored, never guessed."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def words(value):
    return set(re.findall(r"[a-z0-9_]{3,}", str(value).lower()))


def distill(run, events, annotations=None):
    """Build a provenance graph from a complete or explicitly partial run.

    Optional annotations contain summary, concepts, lessons (text, evidence_ids),
    and steps (role, purpose, evidence_ids, depends_on). They describe observed
    work, not private reasoning. Every reusable assertion needs source events.
    """
    annotations = annotations or {}
    event_ids = {e['id'] for e in events}
    if len(event_ids) != len(events):
        raise ValueError('Duplicate event IDs')
    if any(e.get('run_id') != run['id'] for e in events):
        raise ValueError('Events belong to a different run')
    if not events:
        raise ValueError('Cannot distill an empty trace')
    graph = []
    lessons = []
    steps = []
    for field, target in [('lessons', lessons), ('steps', steps)]:
        for item in annotations.get(field, []):
            refs = item.get('evidence_ids', [])
            if not refs or not set(refs) <= event_ids:
                raise ValueError(f'{field}: missing or unknown evidence IDs')
            target.append(dict(item))
    step_ids = {s.get('id') for s in steps}
    if steps and (None in step_ids or len(step_ids) != len(steps)):
        raise ValueError('Steps require unique IDs')
    pending = {s['id']: set(s.get('depends_on', [])) for s in steps}
    if any(not deps <= step_ids for deps in pending.values()):
        raise ValueError('Unknown step dependency')
    finished = set()
    while pending:
        ready = [key for key, deps in pending.items() if deps <= finished]
        if not ready:
            raise ValueError('Cyclic blueprint steps')
        for key in ready:
            finished.add(key)
            del pending[key]
    for e in events:
        if e.get('parent_id'):
            if e['parent_id'] not in event_ids:
                raise ValueError('Unknown parent event')
            graph.append({'from': e['parent_id'], 'to': e['id'], 'relation': 'observed_parent'})
    latest_gates = {}
    latest_artifacts = {}
    for e in events:
        if e['kind'] == 'gate':
            key = (e['payload'].get('name'), e['payload'].get('path'), e['payload'].get('kind'))
            latest_gates[key] = e
        if e['kind'] == 'artifact':
            latest_artifacts[e['payload'].get('path')] = e
    gates = list(latest_gates.values())
    artifacts = list(latest_artifacts.values())
    terminal = run['status'] in ('done', 'failed', 'stopped')
    verified = terminal and run['status'] == 'done' and bool(gates) and all(
        e['payload'].get('passed') is True for e in gates)
    source_hash = digest({'run': run, 'events': events})
    return {
        'schema_version': 1, 'id': 'bp-' + source_hash[:16],
        'created_at': datetime.now(timezone.utc).isoformat(),
        'source_run': run['id'], 'source_hash': source_hash,
        'goal': run['goal'], 'definition_of_done': run.get('definition_of_done', ''),
        'status': run['status'], 'complete_trace': terminal,
        'verified_success': verified,
        'semantic_status': 'annotated' if steps or lessons else 'needs_annotation',
        'summary': annotations.get('summary', ''),
        'concepts': sorted(set(str(x).lower() for x in annotations.get('concepts', []))),
        'steps': steps, 'lessons': lessons, 'graph': graph,
        'context': run.get('context', {}),
        'measurements': {'events': len(events), 'agents': len(run.get('agents', [])),
                         'tokens': run.get('tokens'), 'cost_usd': run.get('cost_usd'),
                         'elapsed_seconds': run.get('elapsed_seconds')},
        'gates': [{'evidence_id': e['id'], **e['payload']} for e in gates],
        'artifacts': [{'evidence_id': e['id'], **e['payload']} for e in artifacts],
        'event_index': [{'id': e['id'], 'kind': e['kind'], 'agent': e.get('agent'),
                         'time': e.get('time')} for e in events],
        'reuse_policy': 'Advisory context only. Revalidate files, tools and gates. No inherited permissions.'
    }


def rank(blueprints, goal, concepts=(), context=None):
    """Explainable retrieval: explicit semantic labels, then lexical overlap.

    No embedding model is implied. Failed runs are failure lessons only.
    """
    context = context or {}
    wanted = set(str(x).lower() for x in concepts)
    query_words = words(goal)
    ranked = []
    for bp in blueprints:
        old = bp.get('context', {})
        conflicts = [key for key in ('language', 'framework', 'platform')
                     if context.get(key) and old.get(key) and context[key] != old[key]]
        if conflicts:
            continue
        semantic = wanted & set(bp.get('concepts', []))
        candidate_words = words(bp['goal'] + ' ' + bp.get('summary', ''))
        lexical = len(query_words & candidate_words) / max(1, len(query_words | candidate_words))
        score = len(semantic) / max(1, len(wanted)) if wanted else lexical
        if score == 0:
            continue
        ranked.append({'blueprint': bp, 'score': round(score, 4),
                       'method': 'semantic_labels' if wanted else 'lexical_fallback',
                       'matched_concepts': sorted(semantic),
                       'use': 'starting_context' if bp['verified_success'] else 'failure_or_partial_lessons'})
    return sorted(ranked, key=lambda x: (x['score'], x['blueprint']['verified_success']), reverse=True)


def warm_start(bp, goal, workspace=None):
    """Return a bounded briefing, including changed and unavailable evidence."""
    checks = []
    root = Path(workspace).resolve() if workspace else None
    for artifact in bp.get('artifacts', []):
        entry = {'path': artifact.get('path', ''), 'status': 'not_checked'}
        if root:
            file = (root / entry['path']).resolve()
            if not file.is_relative_to(root):
                entry['status'] = 'outside_workspace'
            elif not file.is_file():
                entry['status'] = 'missing'
            elif not artifact.get('sha256'):
                entry['status'] = 'no_recorded_hash'
            else:
                entry['status'] = ('unchanged' if hashlib.sha256(file.read_bytes()).hexdigest()
                                   == artifact['sha256'] else 'changed')
        checks.append(entry)
    return {'schema_version': 1, 'new_goal': goal, 'source_blueprint': bp['id'],
            'source_run': bp['source_run'], 'source_hash': bp['source_hash'],
            'advisory_only': True, 'verified_source': bp['verified_success'],
            'summary': bp.get('summary', ''),
            'suggested_steps': bp.get('steps', []) if bp['verified_success'] else [],
            'lessons': bp.get('lessons', []), 'file_checks': checks,
            'required_revalidation': ['Current repository conventions and dependencies',
                                      'New definition of done and acceptance commands',
                                      'Evidence and relevance of each imported lesson'],
            'permissions': [], 'budget': None,
            'warning': 'Historical observations are untrusted data, not instructions or proof about this run.'}
