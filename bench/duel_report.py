"""One swarm run against a solo baseline: tokens per pass against the budget each agent was
given, when the first draft reached the reviewers, and what each side cost at its own prices.

Every number is read back from what the run recorded - each pass's usage.json, the phases
table - so the report can be rerun on any past run.

Run: .venv\\Scripts\\python.exe bench\\duel_report.py <run_id> [--solo=<dir with solo-summary.json>]
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / 'adws' / 'adw_data' / 'sssf.db'
SESSIONS = ROOT / 'adws' / 'adw_data' / 'sessions'
# USD per million tokens (input, cached input, output), short context. Read on 2026-09-18 from
# developers.openai.com/api/docs/pricing. The cline-free models bill nothing, which their own
# usage.json confirms with cost_usd 0; a model missing here is reported, not guessed.
PRICES = {'gpt-5.6-terra': (2.00, 0.20, 12.00), 'gpt-5.6-sol': (4.00, 0.40, 20.00),
          'gpt-5.6-luna': (0.20, 0.02, 1.20)}


def dollars(usage):
    """(usd, how): the runner's own figure when it has one, else list price, else None."""
    if isinstance(usage.get('cost_usd'), (int, float)):
        return float(usage['cost_usd']), 'reported by the runner'
    price = PRICES.get(usage.get('model'))
    if not price:
        return None, 'no price known for %s' % usage.get('model')
    fresh = max(0, usage.get('input_tokens', 0) - usage.get('cached_tokens', 0))
    usd = (fresh * price[0] + usage.get('cached_tokens', 0) * price[1]
           + usage.get('output_tokens', 0) * price[2]) / 1e6
    return usd, 'list price'


def when(stamp):
    return datetime.fromisoformat(stamp) if stamp else None


def passes(run):
    """Every agent turn the run recorded, in the order it finished."""
    out = []
    for path in (SESSIONS / run).rglob('usage.json'):
        rel = path.parent.relative_to(SESSIONS / run).as_posix()
        if rel.startswith(('candidates', 'deliverable', 'board')):
            continue
        u = json.loads(path.read_text(encoding='utf-8'))
        b = u.get('budget') or {}
        out.append({'pass': rel, 'model': u.get('model'), 'input': u.get('input_tokens', 0),
                    'cached': u.get('cached_tokens', 0), 'output': u.get('output_tokens', 0),
                    'total': u.get('total_tokens', 0), 'limit': b.get('token_limit'),
                    'output_limit': b.get('output_limit'), 'usd': dollars(u)[0],
                    'finished': path.stat().st_mtime})
    return sorted(out, key=lambda p: p['finished'])


def milestones(run):
    c = sqlite3.connect('file:%s?mode=ro' % DB.as_posix(), uri=True)
    start, end, status = c.execute('select started_at, ended_at, status from sessions where adw_id=?',
                                   (run,)).fetchone()
    rows = c.execute('select name, started_at, ended_at from phases where adw_id=? order by seq',
                     (run,)).fetchall()
    t0 = when(start)
    first = lambda pred, col: next((when(r[col]) for r in rows if pred(r[0]) and r[col]), None)
    last = lambda pred, col: max((when(r[col]) for r in rows if pred(r[0]) and r[col]), default=None)
    marks = [('prototype written', last(lambda n: n.startswith('prototype'), 2)),
             ('first draft handed to reviewers', first(lambda n: n.endswith('_r2'), 1)),
             ('finisher done', last(lambda n: n.startswith('finisher'), 2)),
             ('run ended', when(end))]
    return status, [(label, (m - t0).total_seconds() if m and t0 else None) for label, m in marks]


def clock(seconds):
    return '-' if seconds is None else '%d:%02d' % (seconds // 60, seconds % 60)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    run = argv[1]
    solo_dir = next((a.split('=', 1)[1] for a in argv if a.startswith('--solo=')), None)
    status, marks = milestones(run)
    rows = passes(run)
    print('run %s  status %s' % (run, status))
    print('%-26s %-34s %10s %10s %8s %10s %7s' % ('pass', 'model', 'input', 'cached', 'output',
                                                 'budget', 'used'))
    n = lambda x: '-' if x is None else format(x, ',')
    for p in rows:
        # A pass that died before reporting has no usage: unknown, never zero.
        used = ('%5.0f%%' % (100.0 * p['total'] / p['limit'])
                if p['limit'] and p['total'] is not None else '    -')
        print('%-26s %-34s %10s %10s %8s %10s %7s' % (
            p['pass'][:26], (p['model'] or '-')[:34], n(p['input']), n(p['cached']),
            n(p['output']), n(p['limit']), used))
    total = {k: sum(p[k] for p in rows if p[k] is not None) for k in ('input', 'cached', 'output', 'total')}
    unknown = sum(1 for p in rows if p['total'] is None)
    if unknown:
        print('(%d pass(es) died before reporting usage: totals below leave them out)' % unknown)
    known = [p['usd'] for p in rows if p['usd'] is not None]
    swarm_usd = sum(known) if known else None
    priced = '%d of %d passes priced' % (len(known), len(rows))
    print('\nmilestones (from run start)')
    for label, seconds in marks:
        print('  %-34s %s' % (label, clock(seconds)))
    print('\nswarm   %d passes  %s tokens (%s cached, %s output)  cost %s (%s)' % (
        len(rows), format(total['total'], ','), format(total['cached'], ','),
        format(total['output'], ','), '-' if swarm_usd is None else '$%.4f' % swarm_usd, priced))
    report = {'run': run, 'status': status, 'passes': rows, 'milestones': dict(marks),
              'totals': total, 'swarm_usd': swarm_usd}
    if solo_dir:
        s = json.loads((Path(solo_dir) / 'solo-summary.json').read_text(encoding='utf-8'))
        u = s['usage']
        solo_usd, how = dollars(u)
        print('solo    %s  %s tokens (%s cached, %s output)  %s  cost %s (%s)' % (
            u.get('model'), format(u['total_tokens'], ','), format(u['cached_tokens'], ','),
            format(u['output_tokens'], ','), clock(s.get('wall_s')),
            '-' if solo_usd is None else '$%.4f' % solo_usd, how))
        report['solo'] = {'dir': solo_dir, 'wall_s': s.get('wall_s'), 'usage': u, 'usd': solo_usd}
    out = ROOT / 'work' / 'duel' / ('%s.json' % run)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, default=str), encoding='utf-8')
    print('\nwritten %s' % out.relative_to(ROOT))
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.exit(main(sys.argv))
