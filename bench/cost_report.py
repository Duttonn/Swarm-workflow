"""Dollar cost of every Gemini swarm run and every Opus agent job, from measured tokens.

Gemini side: the result stats each Gemini CLI call wrote to its events.jsonl (input, cached,
total), so thinking tokens are billed as output the way Google bills them.
Opus side: the usage block of every API response in the subagent transcripts, with cache writes
split into 5-minute and 1-hour where the transcript says which.

Prices are list prices, read from the official pages on 2026-09-12:
  https://docs.anthropic.com/en/docs/about-claude/pricing
  https://ai.google.dev/gemini-api/docs/latest-model  (introductory until 2026-12-31)

Run: .venv\\Scripts\\python.exe bench\\cost_report.py [--standard]
"""
import glob
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS = ROOT / 'adws' / 'adw_data' / 'sessions'
# Claude Code names a project's transcript folder after its path with separators as dashes.
TRANSCRIPTS = Path(os.environ.get('CLAUDE_TRANSCRIPTS') or
                   Path.home() / '.claude' / 'projects' / re.sub(r'[:\\/]', '-', str(ROOT)))

# USD per million tokens
OPUS = {'input': 5.00, 'write_5m': 6.25, 'write_1h': 10.00, 'read': 0.50, 'output': 25.00}
GEMINI_INTRO = {'input': 0.75, 'cached': 0.075, 'output': 3.75}
GEMINI_STANDARD = {'input': 1.50, 'cached': 0.15, 'output': 7.50}


def gemini_run(run_id, price):
    """Every Gemini call of one run, whatever stage it belonged to."""
    fresh = cached = output = calls = 0
    for ev in (SESSIONS / run_id).rglob('events.jsonl'):
        result = None
        for line in ev.read_text(encoding='utf-8', errors='replace').splitlines():
            try:
                e = json.loads(line.lstrip('﻿'))
            except ValueError:
                continue
            if e.get('type') == 'result':
                result = e
        stats = (result or {}).get('stats') or {}
        if not stats:
            continue
        calls += 1
        inp = stats.get('input_tokens') or 0
        hit = stats.get('cached') or 0
        cached += hit
        fresh += max(0, inp - hit)
        # total minus input is what Google bills as output: the answer plus the thinking
        output += max(0, (stats.get('total_tokens') or 0) - inp)
    cost = (fresh * price['input'] + cached * price['cached'] + output * price['output']) / 1e6
    return {'calls': calls, 'fresh': fresh, 'cached': cached, 'output': output, 'usd': cost}


def opus_jobs():
    """Opus usage per job, keyed by the file the agent was hired to deliver."""
    jobs = defaultdict(lambda: {'agents': 0, 'calls': 0, 'input': 0, 'write_5m': 0,
                                'write_1h': 0, 'read': 0, 'output': 0})
    for path in sorted(TRANSCRIPTS.glob('*/subagents/agent-*.jsonl')):
        first, msgs = None, {}
        for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            msg = e.get('message') if isinstance(e, dict) else None
            if not isinstance(msg, dict):
                continue
            if first is None and e.get('type') == 'user':
                c = msg.get('content')
                first = c if isinstance(c, str) else ' '.join(
                    x.get('text', '') for x in c if isinstance(x, dict))
            u, mid = msg.get('usage'), msg.get('id')
            if not u or not mid or msg.get('model') == '<synthetic>':
                continue
            split = u.get('cache_creation') or {}
            cur = msgs.setdefault(mid, dict.fromkeys(('input', 'write_5m', 'write_1h', 'read', 'output'), 0))
            # streaming repeats a message id with a growing usage block: keep the largest
            cur['input'] = max(cur['input'], u.get('input_tokens') or 0)
            written = u.get('cache_creation_input_tokens') or 0
            one_hour = split.get('ephemeral_1h_input_tokens')
            if one_hour is None:
                cur['write_1h'] = max(cur['write_1h'], written)   # unknown split: price it high
            else:
                cur['write_1h'] = max(cur['write_1h'], one_hour)
                cur['write_5m'] = max(cur['write_5m'], split.get('ephemeral_5m_input_tokens') or 0)
            cur['read'] = max(cur['read'], u.get('cache_read_input_tokens') or 0)
            cur['output'] = max(cur['output'], u.get('output_tokens') or 0)
        target = (re.search(r'work/measured/([a-z-]+)-opus', first or '')
                  or re.search(r'work/baseline/([a-z-]+)-opus', first or '')
                  or re.search(r'prompts/(?:0[6-9]|1[0-5])-([a-z-]+)\.json', first or ''))
        if not target or not msgs:
            continue
        key = target.group(1) + (' (measured, artifact only)' if 'work/measured/' in first else
                                 ' (spec + tests + artifact)')
        job = jobs[key]
        job['agents'] += 1
        job['calls'] += len(msgs)
        for m in msgs.values():
            for k, v in m.items():
                job[k] += v
    for job in jobs.values():
        job['usd'] = sum(job[k] * OPUS[k] for k in OPUS) / 1e6
    return jobs


def swarm_runs():
    con = sqlite3.connect('file:%s?mode=ro' % (ROOT / 'adws/adw_data/sssf.db').as_posix(), uri=True)
    rows = con.execute("select adw_id, status, substr(request, 1, 40) from sessions "
                       "where adw_name='adw_agy_swarm' order by started_at").fetchall()
    return [(r, s, q) for r, s, q in rows if (SESSIONS / r).is_dir()]


def main():
    price = GEMINI_STANDARD if '--standard' in sys.argv else GEMINI_INTRO
    label = 'standard' if price is GEMINI_STANDARD else 'introductory'
    print('GEMINI 3.8 FLASH SWARMS (%s price: $%s in / $%s cached / $%s out per MTok)'
          % (label, price['input'], price['cached'], price['output']))
    print('%-10s %-8s %-32s %5s %12s %12s %10s %9s' % ('run', 'status', 'brief', 'calls', 'fresh in',
                                                       'cached in', 'output', 'USD'))
    total = 0.0
    for run, status, brief in swarm_runs():
        g = gemini_run(run, price)
        if not g['calls']:
            continue
        total += g['usd']
        print('%-10s %-8s %-32s %5d %12s %12s %10s %9.2f' % (run, status, (brief or '')[:32], g['calls'],
              f"{g['fresh']:,}", f"{g['cached']:,}", f"{g['output']:,}", g['usd']))
    print('%-10s %-8s %-32s %5s %12s %12s %10s %9.2f' % ('TOTAL', '', '', '', '', '', '', total))
    print()
    print('OPUS 5 AGENTS ($%s in / $%s 5m write / $%s 1h write / $%s read / $%s out per MTok)'
          % (OPUS['input'], OPUS['write_5m'], OPUS['write_1h'], OPUS['read'], OPUS['output']))
    print('%-44s %5s %10s %11s %11s %9s %8s' % ('job', 'calls', 'cache write', 'cache read', 'input',
                                                 'output', 'USD'))
    total = 0.0
    for name, job in sorted(opus_jobs().items()):
        total += job['usd']
        print('%-44s %5d %11s %11s %11s %9s %8.2f' % (name, job['calls'],
              f"{job['write_5m'] + job['write_1h']:,}", f"{job['read']:,}", f"{job['input']:,}",
              f"{job['output']:,}", job['usd']))
    print('%-44s %5s %11s %11s %11s %9s %8.2f' % ('TOTAL', '', '', '', '', '', total))


if __name__ == '__main__':
    main()
