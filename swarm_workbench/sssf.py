"""Read the actual SSSF schema without changing historical run records."""
import json
import sqlite3
from pathlib import Path
from .blueprints import distill


def load_run(db, run_id):
    con = sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute('BEGIN')
        row = con.execute('SELECT * FROM sessions WHERE adw_id=?', (run_id,)).fetchone()
        if not row:
            raise ValueError('Unknown SSSF session')
        phases = [dict(r) for r in con.execute('SELECT * FROM phases WHERE adw_id=? ORDER BY seq', (run_id,))]
        owners = {p['phase_id']: p['owner'] for p in phases}
        events = []
        for r in con.execute('SELECT * FROM events WHERE adw_id=? ORDER BY rowid', (run_id,)):
            payload = json.loads(r['payload_json'] or '{}')
            kind = r['type']
            if kind in ('gate_pass', 'gate_fail'):
                payload = {**payload, 'passed': kind == 'gate_pass', 'name': r['name']}
                kind = 'gate'
            events.append({'id': r['event_id'], 'run_id': run_id, 'kind': kind,
                           'agent': payload.get('agent', owners.get(r['phase_id'], 'system')),
                           'phase_id': r['phase_id'], 'time': r['started_at'],
                           'payload': payload, 'parent_id': r['parent_id']})
        valid_ids = {e['id'] for e in events}
        for e in events:
            if e['parent_id'] not in valid_ids:
                e['parent_id'] = None
        status = 'running' if not row['ended_at'] else ('done' if row['status']=='success' else 'failed')
        run = {'id': run_id, 'goal': row['request'] or '', 'status': status,
               'definition_of_done': '', 'agents': sorted(set(owners.values())),
               'tokens': row['total_tokens'], 'cost_usd': row['total_cost'],
               'context': {}, 'phases': phases}
        for e in events:
            if e['kind'] == 'run_contract':
                run.update({k:v for k,v in e['payload'].items()
                            if k in ('definition_of_done','context','agents')})
                if e['payload'].get('cost_available') is False:
                    run['cost_usd'] = None
        return run, events
    finally:
        con.close()


def export_blueprint(db, run_id, directory, annotations=None):
    run, events = load_run(db, run_id)
    bp = distill(run, events, annotations)
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    path = root / (bp['id'] + '.json')
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(bp, indent=2), encoding='utf-8')
    temp.replace(path)
    return bp, path
