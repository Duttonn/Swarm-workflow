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
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS = ROOT / 'adws' / 'adw_data' / 'sessions'
# Opus jobs were dispatched from more than one Claude Code project folder, so scan them all:
# only transcripts whose brief names a baseline or a spec of this repo are counted (see opus_jobs).
TRANSCRIPTS = [p for p in sorted((Path(os.environ.get('CLAUDE_TRANSCRIPTS')
                                       or Path.home() / '.claude' / 'projects')).glob('*'))
               if p.is_dir()]

# USD per million tokens
OPUS = {'input': 5.00, 'write_5m': 6.25, 'write_1h': 10.00, 'read': 0.50, 'output': 25.00}
GEMINI_INTRO = {'input': 0.75, 'cached': 0.075, 'output': 3.75}
GEMINI_STANDARD = {'input': 1.50, 'cached': 0.15, 'output': 7.50}
FREE = {'input': 0.0, 'cached': 0.0, 'output': 0.0}
# What the same tokens would cost on a paid endpoint: the ratio the user optimises is
# score per dollar per minute, and a free gateway hides the dollar. USD per million, from
# Token Harbor's catalogue (tokenharbor.ai/v1/models, 2026-09-16) for the models it lists,
# NVIDIA's for nemotron. Cached input is billed as fresh input here: an upper bound, since
# none of these gateways publishes a cache price. Matched by substring on the model id, so
# the same model is priced the same through opencode, cline, NIM or Token Harbor.
LIST = [
    ('deepseek-v4.1-flash', {'input': 0.30, 'output': 1.20}),
    ('deepseek-v4-flash', {'input': 0.44, 'output': 1.32}),
    ('deepseek-v4-pro', {'input': 1.32, 'output': 3.96}),
    ('glm-5.3-flash', {'input': 0.15, 'output': 0.50}),
    ('muse-spark', {'input': 1.25, 'output': 4.25}),
    ('kimi-k3', {'input': 3.00, 'output': 15.00}),
    ('kimi-k2.6', {'input': 0.95, 'output': 4.00}),
    ('qwen3.8-flash', {'input': 0.15, 'output': 0.47}),
    ('nemotron-3-super-120b', {'input': 0.20, 'output': 0.60}),   # NVIDIA build catalogue, non-free tier
    ('gemini-3.8-flash', {'input': 0.75, 'output': 3.75}),
    ('haiku', {'input': 1.00, 'output': 5.00}),
]


def list_rate(model):
    """Paid list price for a model id from any gateway, or None when nobody publishes one
    (big-pickle is a stealth model with no public price)."""
    m = (model or '').lower()
    for key, rate in LIST:
        if key in m:
            return rate
    return None
# Per-model list prices for the usage.json runners (bench/providers-2026-09-14.md). Gemini
# and agy models take the --standard/introductory choice; anything absent is unpriced.
PRICES = {
    'opencode/big-pickle': FREE, 'opencode/muse-spark-1.3-contributor-free': FREE,
    'opencode/ling-3.0-flash-fin-free': FREE, 'opencode/nemotron-3.5-lightning-free': FREE,
    'opencode/nemotron-3-ultra-free': FREE, 'opencode/mimo-v2.5-free': FREE,
    'cline-free/muse-spark-1.3-contributor': FREE,
    'claude-haiku-4-5-20251001': {'input': 1.00, 'cached': 0.10, 'output': 5.00},
    'haiku': {'input': 1.00, 'cached': 0.10, 'output': 5.00},
}


def gemini_run(run_id, price):
    """Every Gemini call of one run, whatever stage it belonged to.

    Two on-disk schemas exist, one per runner (SWARM_RUNNER):
    - 'gemini' runner (Gemini CLI, stream-json): {"type": "result", "stats": {...}},
      where stats.input_tokens already includes the cached portion.
    - 'agy' runner (Antigravity, gemini-3.8-flash-medium), used for every swarm run
      before 2026-09-11: {"event": "result", "result": {"usage": {...}}}, where
      usage.input_tokens is the fresh portion only and cache_read_tokens is separate.
    Mixing them up double-counts or drops cache tokens, so each is priced by its own layout.
    """
    fresh = cached = output = calls = 0
    lf = lc = lo = 0   # tokens from the two legacy stream layouts, priced at `price`
    usd, models, unknown = 0.0, set(), 0
    shadow, unlisted = 0.0, 0   # the run at paid list prices, and calls no list price covers
    for ev in (SESSIONS / run_id).rglob('events.jsonl'):
        result_new = result_old = None
        # Since 2026-09-14 every runner writes a normalized usage.json next to the stream:
        # input counts every prompt token, cached the subset served from cache.
        norm = ev.with_name('usage.json')
        if norm.is_file():
            u = json.loads(norm.read_text(encoding='utf-8'))
            calls += 1
            models.add('%s/%s' % (u.get('runner'), u.get('model')))
            inp, hit, out = u.get('input_tokens') or 0, u.get('cached_tokens') or 0, u.get('output_tokens') or 0
            fresh += max(0, inp - hit)
            cached += hit
            output += out
            rate = PRICES.get(u.get('model')) or (price if u.get('runner') in ('gemini', 'agy') else None)
            lr = list_rate(u.get('model'))
            if lr:
                shadow += (inp * lr['input'] + out * lr['output']) / 1e6
            else:
                unlisted += 1
            if u.get('cost_usd') is not None:
                usd += u['cost_usd']
            elif rate:
                usd += (max(0, inp - hit) * rate['input'] + hit * rate['cached'] + out * rate['output']) / 1e6
            else:
                unknown += 1
            continue
        for line in ev.read_text(encoding='utf-8', errors='replace').splitlines():
            try:
                e = json.loads(line.lstrip('﻿'))
            except ValueError:
                continue
            if e.get('type') == 'result':
                result_new = e
            elif e.get('event') == 'result':
                result_old = e
        stats = (result_new or {}).get('stats') or {}
        if stats:
            calls += 1
            inp = stats.get('input_tokens') or 0
            hit = stats.get('cached') or 0
            cached += hit
            fresh += max(0, inp - hit)
            # total minus input is what Google bills as output: the answer plus the thinking
            output += max(0, (stats.get('total_tokens') or 0) - inp)
            lf += max(0, inp - hit); lc += hit; lo += max(0, (stats.get('total_tokens') or 0) - inp)
            continue
        usage = ((result_old or {}).get('result') or {}).get('usage') or {}
        if usage:
            calls += 1
            fresh += usage.get('input_tokens') or 0
            cached += usage.get('cache_read_tokens') or 0
            output += (usage.get('output_tokens') or 0) + (usage.get('thinking_tokens') or 0)
            lf += usage.get('input_tokens') or 0; lc += usage.get('cache_read_tokens') or 0
            lo += (usage.get('output_tokens') or 0) + (usage.get('thinking_tokens') or 0)
    legacy = (lf * price['input'] + lc * price['cached'] + lo * price['output']) / 1e6
    cost = usd + legacy
    return {'calls': calls, 'fresh': fresh, 'cached': cached, 'output': output, 'usd': cost,
            'shadow': shadow + legacy, 'unlisted': unlisted,
            'models': ', '.join(sorted(models)) or 'gemini (legacy stream)', 'unknown': unknown}


def opus_jobs():
    """Opus usage per job, keyed by the file the agent was hired to deliver."""
    jobs = defaultdict(lambda: {'agents': 0, 'calls': 0, 'input': 0, 'write_5m': 0,
                                'write_1h': 0, 'read': 0, 'output': 0})
    for path in sorted(p for root in TRANSCRIPTS for p in root.glob('*/subagents/agent-*.jsonl')):
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
    print('SWARM RUNS (gemini at %s price: $%s in / $%s cached / $%s out per MTok; free models 0)'
          % (label, price['input'], price['cached'], price['output']))
    print('%-10s %-8s %-24s %-30s %5s %12s %12s %10s %8s %8s' % ('run', 'status', 'brief', 'runner/model',
                                                                  'calls', 'fresh in', 'cached in', 'output', 'USD', 'list$'))
    total = shadow = 0.0
    for run, status, brief in swarm_runs():
        g = gemini_run(run, price)
        if not g['calls']:
            continue
        total += g['usd']
        shadow += g['shadow']
        print('%-10s %-8s %-24s %-30s %5d %12s %12s %10s %8.2f %8s%s' % (run, status, (brief or '')[:24],
              g['models'][:30], g['calls'], f"{g['fresh']:,}", f"{g['cached']:,}", f"{g['output']:,}",
              g['usd'], ('%.2f' % g['shadow']) if not g['unlisted'] else '%.2f+?' % g['shadow'],
              ' (+%d calls unpriced)' % g['unknown'] if g['unknown'] else ''))
    print('%-10s %-8s %-24s %-30s %5s %12s %12s %10s %8.2f %8.2f' % ('TOTAL', '', '', '', '', '', '', '', total, shadow))
    print('list$ = the same tokens at the paid list price of the model (LIST); +? marks a model with no public price')
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
