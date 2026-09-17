"""One table per scaling sweep: swarm size against score, tokens, wall time, coordination.

Run: .venv\\Scripts\\python.exe bench\\sweep_report.py work\\queue-logs\\sweep-nim-control.txt [more sweep logs]
     add --solo work/measured/<spec-model>/<stamp>/solo-summary.json for the one-agent row

The sweep log is what bench/scale_sweep.sh prints: one "=== size N done ... run=<id>" line per
size. Every number comes from measure_run.measure(), so it is the same arithmetic as the
per-run reports; this file only lines the sizes up next to each other, which is the only
view that answers "does adding agents buy anything".
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bench'))
from cost_report import list_rate  # noqa: E402
from measure_run import measure  # noqa: E402

SESSIONS = ROOT / 'adws' / 'adw_data' / 'sessions'


def list_usd(folders):
    """The run at paid list prices, summed over every usage.json under the folders; None when
    a model has no public price (big-pickle)."""
    total, unlisted = 0.0, 0
    for folder in folders:
        for u in Path(folder).rglob('usage.json'):
            d = json.loads(u.read_text(encoding='utf-8'))
            rate = list_rate(d.get('model'))
            if not rate:
                unlisted += 1
                continue
            total += ((d.get('input_tokens') or 0) * rate['input'] + (d.get('output_tokens') or 0) * rate['output']) / 1e6
    return None if unlisted else round(total, 2)

DONE = re.compile(r'=== size (\d+) done exit=(\d+) run=(\w+) accepted=(\w+)')


def sizes(log):
    rows = []
    for line in Path(log).read_text(encoding='utf-8', errors='replace').splitlines():
        m = DONE.match(line.strip())
        if m and m.group(3) != 'none':
            rows.append((int(m.group(1)), m.group(3)))
    return rows


def row(size, run_id):
    try:
        r = measure(run_id)
    except Exception as exc:
        return {'size': size, 'run': run_id, 'error': '%s: %s' % (type(exc).__name__, exc)}
    out, st, c = r['outcome'], r['stages'], r.get('coordination') or {}
    return {'size': size, 'run': run_id, 'tests': '%s/%s' % (out['tests_passed'], out['tests_ran']),
            'accepted': out['accepted'], 'tokens': r['tokens'], 'minutes': r['minutes'],
            'list_usd': list_usd([SESSIONS / run_id]),
            'build_ok': '%s/%s' % (st['build']['ok'], st['build']['agents']) if st.get('build') else '-',
            'posts': c.get('posts', 0), 'coord_calls': c.get('coord_tool_calls', 0),
            'claims': c.get('claims', 0), 'done': c.get('done_declared', 0)}


def solo_row(path):
    s = json.loads(Path(path).read_text(encoding='utf-8'))
    ran = next((l for l in s.get('judge', []) if l.startswith('Ran ')), '')
    n = int(re.search(r'Ran (\d+)', ran).group(1)) if ran else None
    failed = len(s.get('failed_tests') or [])
    return {'size': 'solo', 'run': Path(path).parent.name,
            'tests': '%s/%s' % (max(0, n - failed) if n is not None else '?', n),
            'accepted': failed == 0 and n is not None, 'tokens': (s.get('usage') or {}).get('total_tokens'),
            'minutes': round((s.get('wall_s') or 0) / 60, 1), 'build_ok': '1/1' if not s.get('failure') else '0/1',
            'list_usd': list_usd([Path(path).parent]),
            'posts': 0, 'coord_calls': 0, 'claims': 0, 'done': 0}


def main(argv):
    logs, solos = [], []
    it = iter(argv[1:])
    for a in it:
        if a == '--solo':
            solos.append(next(it))
        else:
            logs.append(a)
    rows = [solo_row(p) for p in solos]
    for log in logs:
        for size, run_id in sizes(log):
            rows.append(row(size, run_id))
    head = ('size', 'run', 'tests', 'ok', 'tokens', 'min', 'list$', 'build', 'posts', 'coord', 'claims', 'done')
    print('%-5s %-9s %-7s %-5s %12s %6s %7s %6s %6s %6s %6s %5s' % head)
    for r in rows:
        if 'error' in r:
            print('%-5s %-9s %s' % (r['size'], r['run'], r['error']))
            continue
        print('%-5s %-9s %-7s %-5s %12s %6s %7s %6s %6s %6s %6s %5s' % (
            r['size'], r['run'], r['tests'], r['accepted'],
            f"{r['tokens']:,}" if isinstance(r['tokens'], int) else '-', r['minutes'],
            '-' if r.get('list_usd') is None else '%.2f' % r['list_usd'],
            r['build_ok'], r['posts'], r['coord_calls'], r['claims'], r['done']))
    print('list$ = the same tokens at the paid list price of the models used (bench/cost_report.py LIST)')
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    raise SystemExit(main(sys.argv))
