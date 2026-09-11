"""Web view of the SSSF trace: swarms, threads, phases, agents.

Server-rendered from the stdlib only: no bundler, no CDN, no external font.
Read-only throughout, so it is safe to point at a db a live swarm is writing.
The SQL is the monitor's SQL; this module only reshapes it for the browser.

Threads and phases are NOT the same thing, and the first pass conflated them.
A phase is an execution step (designer_r1, materialize, acceptance). A thread is
a message board agents post to. This swarm has one implicit board - the
peer_message stream - so a 20-agent run has 1 thread and 21 phases, never
"21 threads". Both are shown, under their own headings, counted from their own
rows.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .monitor import clip, connect, elapsed, parse_time

PAPER, PANEL, INK = '#f4efe4', '#fbf8f2', '#221f1a'
DIM, RULE, ACCENT = '#8b8375', '#ded5c3', '#d1451b'
GREEN, AMBER, RED = '#2f8f3f', '#b4741a', '#c02f1d'

# An unfinished run whose trace has been silent this long is reported stale
# rather than live: the db says "running" long after a crash, and a green dot
# that lies is worse than no dot.
STALE_AFTER = 120.0

# One colour per event type, reused by the ticks, the sparklines and the type
# column, so a colour means the same thing on every page.
TYPE_COLOR = {
    'tool_call': '#b4741a', 'peer_message': '#1f6f8b', 'gate_pass': '#3f7d3f',
    'gate_fail': '#c02f1d', 'error': '#c02f1d', 'artifact': '#5b4b8a',
    'agent_start': '#7a6a4f', 'run_contract': '#8b8375', 'sandbox_contents': '#5b4b8a',
    'phase_start': '#b9b0a0', 'phase_end': '#b9b0a0', 'log': '#a49b8b',
}
# Twenty, because a swarm of twenty agents is a real roster here and ten would
# put two agents behind the same dot. Ink-on-paper hues, all legible on cream.
PALETTE = ['#d1451b', '#1f6f8b', '#3f7d3f', '#8a5a2b', '#5b4b8a',
           '#a8352f', '#2f6f6a', '#7a6a1f', '#8a3d6b', '#356b9a',
           '#b5561f', '#2a5f74', '#55713a', '#6f4b2a', '#463a6f',
           '#8f4a3a', '#3f7a72', '#9a7d2a', '#6f2f55', '#4a5b7a']
MESSAGE_TYPES = ('peer_message', 'log', 'error', 'artifact', 'gate_pass', 'gate_fail')
FAIL_TYPES = ('error', 'gate_fail')
ACTIVE_STATUS = ('RUNNING', 'PENDING', 'STARTED')


# ── shaping ─────────────────────────────────────────────────────────────────

def stable_color(name):
    return PALETTE[sum(ord(c) * (i + 3) for i, c in enumerate(str(name) or '?')) % len(PALETTE)]


def now():
    return datetime.now(timezone.utc)


def clock(started, ended=None):
    start = parse_time(started)
    if not start:
        return '--'
    end = parse_time(ended) or now()
    total = int(max(0, (end - start).total_seconds()))
    if total >= 3600:
        return f'{total // 3600}h {total % 3600 // 60:02d}m'
    return f'{total // 60}m {total % 60:02d}s'


def ago(when):
    moment = parse_time(when)
    if not moment:
        return '--'
    total = int(max(0, (now() - moment).total_seconds()))
    if total < 90:
        return f'{total}s ago'
    if total < 5400:
        return f'{total // 60}m ago'
    return f'{total // 3600}h ago'


def stamp(value):
    return (str(value or '')[11:19]) or '--:--:--'


def day(value):
    return str(value or '')[:19].replace('T', ' ')


def num(value):
    return f'{int(value or 0):,}'


def short(value):
    value = int(value or 0)
    if value >= 1_000_000:
        return f'{value / 1_000_000:.1f}M'
    if value >= 1_000:
        return f'{value / 1_000:.1f}k'
    return str(value)


def preview(kind, name, payload):
    if kind == 'peer_message':
        return payload.get('summary') or payload.get('notes_for_next_agent') or name
    if kind == 'log':
        return payload.get('message') or name
    if kind == 'error':
        return 'ERROR: ' + str(payload.get('error') or name)
    if kind == 'gate_fail':
        return 'CLAIM VIOLATION: ' + str(payload.get('violations') or name)
    if kind == 'gate_pass':
        return 'gate ' + str(name) + ' passed'
    if kind == 'artifact':
        return payload.get('path') or name
    if kind == 'tool_call':
        info = payload.get('tool_info') or {}
        params = info.get('parameters') or {}
        first = next((f'{k}={params[k]}' for k in params), '')
        return first or (info.get('name') or name)
    if kind == 'sandbox_contents':
        return f"{payload.get('file_count', '?')} files in {payload.get('scratch', 'scratch')}"
    if kind == 'run_contract':
        return payload.get('definition_of_done') or name
    if kind in ('phase_start', 'phase_end'):
        return payload.get('description') or payload.get('status') or name
    return name or ''


def liveness(ended_at, last_event_at, status=None):
    """done / live / stale, from the trace rather than from the status column."""
    if ended_at:
        return 'done'
    if (status or '').lower() in ('success', 'fail', 'failed'):
        return 'done'
    last = parse_time(last_event_at)
    if last and (now() - last).total_seconds() > STALE_AFTER:
        return 'stale'
    return 'live'


def contract(con, run_id):
    row = con.execute("SELECT payload_json FROM events WHERE adw_id=? AND type='run_contract'",
                      (run_id,)).fetchone()
    return json.loads(row['payload_json'] or '{}') if row else {}


def budget(cost, limits, calls, agents, cap_usd, cost_available=True):
    """Cost bar when the run priced itself, else the tool-call cap it agreed to."""
    if cost_available:
        return {'kind': 'cost', 'used': cost, 'cap': cap_usd,
                'label': f'${cost:.4f} of ${cap_usd:g}', 'meter': f'${cost:.4f} COST',
                'left': f'${max(0.0, cap_usd - cost):.2f} left of ${cap_usd:g}',
                'over': cost > cap_usd,
                'pct': min(100.0, 100.0 * cost / cap_usd) if cap_usd else 0.0}
    # max_calls is per agent per peer round, so the run's ceiling is the product.
    cap = (limits.get('max_calls') or 0) * max(1, agents) * max(1, limits.get('peer_rounds') or 1)
    over = bool(cap and calls > cap)
    left = (f'{calls - cap:,} calls over cap' if over
            else f'{max(0, cap - calls):,} calls left of {cap:,}')
    return {'kind': 'calls', 'used': calls, 'cap': cap, 'over': over,
            'label': (f'{calls:,} of {cap:,} calls (cost n/a)' if cap
                      else f'{calls:,} calls (no cap declared, cost n/a)'),
            'meter': 'COST n/a', 'left': left if cap else f'{calls:,} calls',
            'pct': min(100.0, 100.0 * calls / cap) if cap else 0.0}


def roster(con, run_id):
    """Agents of a run: the registered sessions, plus any phase owner missing from them."""
    rows = con.execute('SELECT * FROM agent_sessions WHERE adw_id=? ORDER BY rowid',
                       (run_id,)).fetchall()
    names = [r['agent'] for r in rows]
    for owner in con.execute("SELECT DISTINCT owner FROM phases WHERE adw_id=? AND kind='agent'",
                             (run_id,)):
        if owner['owner'] and owner['owner'] not in names:
            names.append(owner['owner'])
    declared = {r['agent']: r['color'] for r in rows}
    distinct = len(set(v for v in declared.values() if v)) > 1
    alive = {r['name'] for r in con.execute('SELECT DISTINCT name FROM processes WHERE adw_id=? '
                                            'AND ended_at IS NULL', (run_id,))}
    out = []
    for index, name in enumerate(names):
        row = next((r for r in rows if r['agent'] == name), None)
        out.append({
            'agent': name,
            # Position in the roster, not a name hash: a hash collides and then two
            # agents on the same swarm wear the same dot.
            'color': declared[name] if distinct else PALETTE[index % len(PALETTE)],
            'model': (row['model'] if row else '') or '',
            'coding_agent': (row['coding_agent'] if row else '') or '',
            'context_tokens': (row['context_tokens'] if row else 0) or 0,
            'context_window': (row['context_window'] if row else 0) or 0,
            'registered': row is not None,
            'proc': name in alive,
            'index': index + 1,
        })
    return out


def _pct(moment, start, end):
    if not moment or not start or not end or end <= start:
        return 0.0
    return max(0.0, min(100.0, 100.0 * (moment - start).total_seconds()
                        / (end - start).total_seconds()))


def load(con, run_id, cap_usd=30.0):
    """One snapshot of a run: session, contract, phases, agents, every event.

    Every page is built from this, so a page costs a fixed handful of queries
    instead of one per phase per agent (the first pass reran the event scan 21
    times for a 21-phase run, on a 2s poll).
    """
    session = con.execute('SELECT * FROM sessions WHERE adw_id=?', (run_id,)).fetchone()
    if not session:
        return None
    spec = contract(con, run_id)
    phases = [dict(r) for r in con.execute('SELECT * FROM phases WHERE adw_id=? ORDER BY seq',
                                           (run_id,))]
    owner_of = {p['phase_id']: p['owner'] for p in phases}
    people = roster(con, run_id)
    colors = {p['agent']: p['color'] for p in people}

    raw = con.execute('SELECT * FROM events WHERE adw_id=? ORDER BY started_at, rowid',
                      (run_id,)).fetchall()
    events = []
    for row in raw:
        payload = json.loads(row['payload_json'] or '{}')
        duration = payload.get('duration_seconds')
        if duration is None and row['ended_at']:
            a, b = parse_time(row['started_at']), parse_time(row['ended_at'])
            duration = (b - a).total_seconds() if a and b else None
        events.append({
            'id': row['event_id'], 'type': row['type'], 'name': row['name'] or '',
            'phase_id': row['phase_id'] or '', 'time': row['started_at'],
            'at': parse_time(row['started_at']),
            'agent': payload.get('agent') or owner_of.get(row['phase_id'] or '', '') or '',
            'text': preview(row['type'], row['name'], payload),
            'payload': payload, 'tokens': row['tokens'] or 0,
            'ms': int(duration * 1000) if duration is not None else None,
            'pct': 0.0,
        })
    moments = [e['at'] for e in events if e['at']]
    start = min(moments) if moments else parse_time(session['started_at'])
    end = max(moments) if moments else None
    end = end if end and start and end > start else (
        parse_time(session['ended_at']) or now())
    for event in events:
        event['pct'] = _pct(event['at'], start, end)

    by_phase, by_agent = {}, {}
    for event in events:
        by_phase.setdefault(event['phase_id'], []).append(event)
        if event['agent']:
            by_agent.setdefault(event['agent'], []).append(event)

    calls = sum(1 for e in events if e['type'] == 'tool_call')
    msgs = sum(1 for e in events if e['type'] == 'peer_message')
    fails = sum(1 for e in events if e['type'] in FAIL_TYPES)
    last_at = events[-1]['time'] if events else None
    state = liveness(session['ended_at'], last_at, session['status'])

    boards = {}
    for event in events:
        if event['type'] != 'peer_message':
            continue
        key = event['payload'].get('thread') or 'primary'
        boards.setdefault(key, []).append(event)
    if not boards:
        boards['primary'] = []

    run = {
        'adw_id': run_id,
        'name': (session['adw_name'] or run_id).upper(),
        'request': session['request'] or '',
        'engineer': session['engineer'] or '',
        'status': (session['status'] or ('running' if state != 'done' else '?')),
        'state': state, 'live': state == 'live', 'open': not session['ended_at'],
        'started_at': session['started_at'], 'ended_at': session['ended_at'],
        'last_at': last_at, 'span': (start, end),
        'elapsed': clock(session['started_at'], session['ended_at']),
        'model': spec.get('model', '?'),
        'definition_of_done': spec.get('definition_of_done', ''),
        'limits': spec.get('limits', {}) or {},
        'agents': people, 'colors': colors,
        'tokens': session['total_tokens'] or 0, 'cost': session['total_cost'] or 0.0,
        'events': events, 'by_phase': by_phase, 'by_agent': by_agent,
        'phases': phases, 'boards': boards,
        'event_count': len(events), 'calls': calls, 'msgs': msgs, 'fails': fails,
        'rounds': len({e['payload'].get('round') for e in events
                       if e['type'] == 'peer_message'} - {None}),
    }
    run['budget'] = budget(run['cost'], run['limits'], calls, len(people), cap_usd,
                           spec.get('cost_available') is not False)
    return run


def board_stream(run, key, msgs):
    """What a board shows: its messages, plus the violations aimed at the swarm.

    A claim violation is filed as its own event type rather than posted, but it
    is addressed to the room, so it belongs in the room's stream. Only the
    primary board adopts them, or a second board would show them twice.
    """
    notices = [e for e in run['events'] if e['type'] in FAIL_TYPES] if key == 'primary' else []
    return sorted(msgs + notices, key=lambda e: (e['time'] or '', e['id'] or ''))


def thread_rows(run):
    """The message boards. One per distinct peer_message thread key, honestly counted."""
    out = []
    for key, msgs in sorted(run['boards'].items()):
        per_agent = {}
        for msg in msgs:
            who = msg['agent'] or 'system'
            per_agent[who] = per_agent.get(who, 0) + 1
        names = [a['agent'] for a in run['agents']]
        names += [w for w in per_agent if w not in names]
        chips = [{'agent': who, 'count': per_agent.get(who, 0),
                  'dormant': not per_agent.get(who),
                  'color': run['colors'].get(who) or stable_color(who)} for who in names]
        stream = board_stream(run, key, msgs)
        latest = stream[-1] if stream else None
        out.append({
            'key': key, 'name': f'{key.upper()} BOARD',
            'messages': len(msgs), 'members': sum(1 for c in chips if not c['dormant']),
            'rounds': len({m['payload'].get('round') for m in msgs} - {None}),
            'status': 'RUNNING' if run['live'] else ('STALE' if run['open'] else 'CLOSED'),
            'ticks': [{'pct': m['pct'], 'type': m['type']} for m in msgs],
            'chips': chips, 'latest': latest,
            'first': msgs[0]['time'] if msgs else None,
            'last': msgs[-1]['time'] if msgs else None,
        })
    return out


def phase_rows(run):
    """The execution steps. Never called threads, never counted as threads."""
    out = []
    for phase in run['phases']:
        events = run['by_phase'].get(phase['phase_id'], [])
        members = sorted({e['agent'] for e in events if e['agent']})
        status = (phase['status'] or '?').upper()
        out.append({
            'phase_id': phase['phase_id'], 'seq': phase['seq'],
            'name': (phase['name'] or '').upper(), 'kind': phase['kind'] or '',
            'owner': phase['owner'] or '', 'status': status,
            'color': run['colors'].get(phase['owner']) or stable_color(phase['owner']),
            'error': phase['error'] or '', 'attempt': phase['attempt'] or 1,
            'retries': phase['retries'] or 0,
            'started_at': phase['started_at'], 'ended_at': phase['ended_at'],
            'elapsed': elapsed(phase['started_at'], phase['ended_at']),
            'events': len(events),
            'posts': sum(1 for e in events if e['type'] == 'peer_message'),
            'calls': sum(1 for e in events if e['type'] == 'tool_call'),
            'fails': sum(1 for e in events if e['type'] in FAIL_TYPES),
            'ticks': [{'pct': e['pct'], 'type': e['type']} for e in events],
            'members': members,
            'latest': next((e for e in reversed(events) if e['type'] in MESSAGE_TYPES), None),
            'active': status in ACTIVE_STATUS,
        })
    return out


def agent_rows(run):
    out = []
    for person in run['agents']:
        events = run['by_agent'].get(person['agent'], [])
        calls = [e for e in events if e['type'] == 'tool_call']
        timed = [e['ms'] for e in calls if e['ms'] is not None]
        owned = [p for p in run['phases'] if p['owner'] == person['agent']]
        running = any((p['status'] or '').lower() in ('running', 'pending') for p in owned)
        out.append({
            **person,
            'live': bool(person['proc'] and run['open'] and running) or (
                person['proc'] and run['live']),
            'events': events, 'calls': len(calls),
            'posts': sum(1 for e in events if e['type'] == 'peer_message'),
            'failures': sum(1 for e in events if e['type'] in FAIL_TYPES),
            'tokens': sum(e['tokens'] for e in events),
            'tokens_recorded': any(e['tokens'] for e in events),
            'ms': sum(timed), 'timed': len(timed),
            'first': events[0]['time'] if events else None,
            'last': events[-1]['time'] if events else None,
            'phases': owned,
            'ticks': [{'pct': e['pct'], 'type': e['type']} for e in events],
        })
    return out


def swarm_list(con, limit=25, cap_usd=30.0):
    """The front page, in five queries: one per table, all grouped by run."""
    counts = {r['adw_id']: r for r in con.execute(
        "SELECT adw_id, COUNT(*) events, SUM(type='tool_call') calls, "
        "SUM(type='peer_message') msgs, SUM(type IN ('error','gate_fail')) fails, "
        "COUNT(DISTINCT CASE WHEN type='peer_message' THEN "
        "COALESCE(json_extract(payload_json,'$.thread'),'primary') END) boards, "
        'MAX(started_at) last_at FROM events GROUP BY adw_id')}
    phases = {r['adw_id']: r['n'] for r in con.execute(
        'SELECT adw_id, COUNT(*) n FROM phases GROUP BY adw_id')}
    agents = {r['adw_id']: r['n'] for r in con.execute(
        'SELECT adw_id, COUNT(*) n FROM agent_sessions GROUP BY adw_id')}
    owners = {r['adw_id']: r['n'] for r in con.execute(
        "SELECT adw_id, COUNT(DISTINCT owner) n FROM phases WHERE kind='agent' GROUP BY adw_id")}
    procs = {r['adw_id']: r['n'] for r in con.execute(
        'SELECT adw_id, COUNT(*) n FROM processes WHERE ended_at IS NULL GROUP BY adw_id')}
    specs = {}
    for row in con.execute("SELECT adw_id, payload_json FROM events WHERE type='run_contract'"):
        specs.setdefault(row['adw_id'], json.loads(row['payload_json'] or '{}'))

    out = []
    for row in con.execute('SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?', (limit,)):
        run_id = row['adw_id']
        count = counts.get(run_id)
        spec = specs.get(run_id, {})
        limits = spec.get('limits', {}) or {}
        calls = (count['calls'] if count else 0) or 0
        msgs = (count['msgs'] if count else 0) or 0
        # An agent may own a phase without ever registering a session, so the
        # roster is the larger of the two counts, never just agent_sessions.
        people = max(agents.get(run_id, 0), owners.get(run_id, 0))
        state = liveness(row['ended_at'], count['last_at'] if count else None, row['status'])
        out.append({
            'adw_id': run_id,
            'name': (row['adw_name'] or run_id).upper(),
            'request': row['request'] or '',
            'status': (row['status'] or 'running').upper(),
            'state': state, 'live': state == 'live', 'open': not row['ended_at'],
            'started_at': row['started_at'], 'ended_at': row['ended_at'],
            'last_at': count['last_at'] if count else None,
            'elapsed': elapsed(row['started_at'], row['ended_at']),
            'model': spec.get('model', '?'),
            'agents': people, 'procs': procs.get(run_id, 0),
            # Boards, not phases. A 20-agent run has one board and 21 phases.
            'threads': (count['boards'] if count else 0) or 0,
            'phases': phases.get(run_id, 0),
            'events': (count['events'] if count else 0) or 0,
            'msgs': msgs, 'calls': calls, 'fails': (count['fails'] if count else 0) or 0,
            'tokens': row['total_tokens'] or 0,
            'budget': budget(row['total_cost'] or 0.0, limits, calls, people, cap_usd,
                             spec.get('cost_available') is not False),
        })
    return out


# ── rendering ───────────────────────────────────────────────────────────────

def bar(fill, tint=ACCENT, width='180px'):
    return (f'<span class="bar" style="width:{width}"><i style="width:{fill:.1f}%;'
            f'background:{tint}"></i></span>')


def dot(color, live=False):
    cls = 'dot live' if live else 'dot'
    return f'<span class="{cls}" style="background:{color}"></span>'


# A sparkline is a few hundred pixels wide, so past a few hundred ticks the extra
# marks are invisible and only cost bytes on a 2s poll. Bucket them, and let the
# rarest type win each bucket so a single violation is never hidden by 40 tool calls.
TICK_BUCKETS = 240
TICK_RANK = {'error': 0, 'gate_fail': 0, 'gate_pass': 1, 'peer_message': 2, 'artifact': 3}


def thin(items, buckets=TICK_BUCKETS):
    if len(items) <= buckets:
        return items
    best = {}
    for item in items:
        key = int(item['pct'] * (buckets - 1) / 100)
        rank = TICK_RANK.get(item['type'], 9)
        if key not in best or rank < TICK_RANK.get(best[key]['type'], 9):
            best[key] = item
    return [best[k] for k in sorted(best)]


def ticks(items, height=18, title=''):
    body = ''.join(f'<i style="left:{t["pct"]:.2f}%;background:'
                   f'{TYPE_COLOR.get(t["type"], DIM)}"></i>' for t in thin(items))
    empty = '' if items else '<u>no events</u>'
    return (f'<span class="spark" style="height:{height}px" title="{escape(title)}">'
            f'{body}{empty}</span>')


def state_tag(state):
    if state == 'live':
        return f'<b class="live">&#9679; LIVE</b>'
    if state == 'stale':
        return f'<b class="stale">&#9673; STALE</b>'
    return '<b class="done">&#9675; DONE</b>'


def nav(active, run_id=None):
    suffix = f'?run={quote(run_id)}' if run_id else ''
    items = [('SWARMS', '/'), ('THREADS', '/threads' + suffix), ('AGENTS', '/agents' + suffix)]
    return ''.join(
        f'<a class="nav{" on" if name == active else ""}" href="{escape(href)}">{name}</a>'
        for name, href in items)


def header(state, active, run_id=None):
    """The strip that rides on every page: clock, agents, live, model, cost, budget."""
    budget_state = state['budget']
    return f'''<header>
  <div class="crumbs">{nav(active, run_id)}</div>
  <div class="meters">
    <span class="clk" data-since="{escape(str(state.get('started_at') or ''))}"
          data-live="{1 if state.get('ticking') else 0}">{escape(state['elapsed'])}</span>
    <span>{state['agent_count']} AGENTS</span>
    {state_tag(state['state'])}
    <span class="chip">{escape(str(state['model']))}</span>
    <span>{escape(state['cost'])}</span>
    <span class="d">{num(state['tokens'])} tok | {num(state['calls'])} calls</span>
    {bar(budget_state['pct'], RED if budget_state.get('over') else ACCENT, '120px')}
    <span class="{'warn' if budget_state.get('over') else 'd'}">{escape(budget_state['left'])}</span>
  </div>
</header>'''


PAGE = '''<title>{title}</title>
<style>
:root{{color-scheme:light}}
*{{box-sizing:border-box}}
body{{margin:0;background:{paper};color:{ink};
 font:12px/1.5 ui-monospace,SFMono-Regular,"Cascadia Mono",Menlo,Consolas,monospace}}
a{{color:inherit;text-decoration:none}}
header{{position:sticky;top:0;z-index:9;display:flex;flex-wrap:wrap;gap:10px 22px;
 justify-content:space-between;align-items:center;padding:9px 18px;background:{panel};
 border-bottom:1px solid {rule}}}
.crumbs{{display:flex;gap:18px}}
.nav{{letter-spacing:.22em;font-size:11px;color:{dim};padding-bottom:2px}}
.nav.on{{color:{accent};border-bottom:2px solid {accent}}}
.meters{{display:flex;flex-wrap:wrap;gap:14px;align-items:center;font-size:11px}}
.meters .d{{color:{dim}}}
.clk{{font-weight:700;letter-spacing:.1em}}
.live{{color:{green}}} .stale{{color:{amber}}} .done{{color:{dim};font-weight:400}}
.chip{{border:1px solid {rule};border-radius:9px;padding:1px 7px;color:{dim};background:{paper}}}
main{{padding:16px 18px 96px}}
h1{{font-size:13px;letter-spacing:.32em;color:{accent};margin:0 0 4px;font-weight:700}}
h2{{font-size:11px;letter-spacing:.28em;color:{dim};margin:24px 0 8px;font-weight:700;
 border-bottom:1px solid {rule};padding-bottom:4px;display:flex;gap:12px;align-items:baseline}}
h2 em{{font-style:normal;color:{ink};letter-spacing:normal;font-weight:400}}
h2 .note{{font-weight:400;letter-spacing:normal;margin-left:auto;color:{dim}}}
.sub{{color:{dim};margin:0 0 14px}}
.bar{{display:inline-block;height:6px;background:{rule};border-radius:3px;
 overflow:hidden;vertical-align:middle}}
.bar i{{display:block;height:100%}}
.dot{{display:inline-block;width:7px;height:7px;border-radius:50%;vertical-align:middle;
 flex:none}}
.dot.live{{box-shadow:0 0 0 3px rgba(47,143,63,.18)}}
.spark{{position:relative;display:block;flex:1;min-width:110px;height:18px;background:{paper};
 border:1px solid {rule};border-radius:2px}}
.spark i{{position:absolute;top:2px;bottom:2px;width:2px;margin-left:-1px;opacity:.85}}
.spark u{{position:absolute;left:5px;top:1px;color:{dim};text-decoration:none;font-size:10px}}
.row{{display:block;border:1px solid {rule};border-left:3px solid {rule};background:{panel};
 padding:9px 12px;margin-bottom:7px;border-radius:2px}}
a.row:hover{{border-color:{accent}}}
.line{{display:flex;flex-wrap:wrap;gap:12px;align-items:center}}
.grow{{flex:1}}
.name{{letter-spacing:.2em;font-weight:700}}
.d{{color:{dim}}}
.tag{{border:1px solid {rule};border-radius:2px;padding:0 5px;font-size:10px;letter-spacing:.14em;
 color:{dim};white-space:nowrap}}
.warn,.tag.warn{{color:{red}}}
.ok,.tag.ok{{color:{green}}}
.run,.tag.run{{color:{amber}}}
.tag.warn{{border-color:#e0b4ae}}
.tag.run{{border-color:#e3cba2}}
.chips{{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px}}
.ag{{border:1px solid {rule};border-radius:10px;padding:1px 8px;font-size:11px;background:{paper};
 display:inline-flex;gap:5px;align-items:center}}
.ag.off{{opacity:.42}}
.tools{{display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin:10px 0}}
input,button,select{{font:inherit;background:{paper};color:{ink};border:1px solid {rule};
 border-radius:2px;padding:3px 7px}}
input:focus{{outline:1px solid {accent}}}
.seg a{{padding:2px 7px;color:{dim};border:1px solid transparent}}
.seg a.on{{color:{accent};border-color:{rule};background:{panel}}}
table{{width:100%;border-collapse:collapse}}
td{{padding:3px 8px 3px 0;vertical-align:top;border-bottom:1px solid rgba(0,0,0,.04)}}
td.t{{color:{dim};white-space:nowrap;width:70px}}
td.k{{white-space:nowrap;width:150px}}
td.w{{white-space:nowrap;width:110px;color:{dim}}}
td.ms{{text-align:right;color:{dim};white-space:nowrap;width:70px}}
.msg{{border:1px solid {rule};border-left:3px solid {rule};background:{panel};padding:10px 12px;
 margin-bottom:8px;border-radius:2px}}
.msg pre{{margin:6px 0 0}}
.msg .kv{{margin-top:6px}}
.msg .kv b{{color:{dim};font-weight:400;letter-spacing:.14em;font-size:10px;margin-right:6px}}
details summary{{cursor:pointer;color:{dim};margin-top:6px}}
details pre{{border-left:2px solid {rule};padding-left:8px;margin-top:6px;max-height:340px;
 overflow:auto}}
footer{{position:fixed;left:0;right:0;bottom:0;background:{panel};border-top:1px solid {rule};
 padding:7px 18px;display:flex;gap:16px;align-items:center;font-size:11px}}
footer .spark{{height:22px}}
.empty{{color:{dim};padding:18px 0}}
pre{{white-space:pre-wrap;word-break:break-word;margin:0;font:inherit}}
.legend{{display:flex;gap:10px;flex-wrap:wrap;color:{dim};font-size:10px;margin-top:6px}}
</style>
<div id="app">{body}</div>
<script>
function wire(){{
  var q=document.getElementById('q');
  if(q){{
    var apply=function(){{
      var v=q.value.toLowerCase(), n=0;
      document.querySelectorAll('[data-hay]').forEach(function(el){{
        var hit=el.dataset.hay.indexOf(v)>=0;
        el.style.display=hit?'':'none'; if(hit)n++;
      }});
      var c=document.getElementById('qn'); if(c)c.textContent=v?n+' match':'';
    }};
    q.oninput=apply; apply();
  }}
  document.querySelectorAll('.clk[data-live="1"]').forEach(function(el){{
    var t0=Date.parse(el.dataset.since); if(isNaN(t0))return;
    el.textContent=fmt((Date.now()-t0)/1000);
  }});
}}
function fmt(s){{s=Math.max(0,Math.floor(s));
  return s>=3600?(s/3600|0)+'h '+(('0'+((s%3600/60)|0)).slice(-2))+'m'
    :((s/60)|0)+'m '+('0'+(s%60)).slice(-2)+'s';}}
document.addEventListener('keydown',function(e){{
  var q=document.getElementById('q');
  if(e.key==='/'&&q&&document.activeElement!==q){{e.preventDefault();q.focus();q.select();}}
  if(e.key==='Escape'&&q&&document.activeElement===q){{q.value='';q.blur();wire();}}
}});
setInterval(wire,1000);
if({poll}){{setInterval(function(){{
  fetch(location.href,{{headers:{{'x-partial':'1'}}}}).then(function(r){{return r.text()}})
   .then(function(t){{
     var q=document.getElementById('q'), keep=q?q.value:null, focus=document.activeElement===q;
     var y=window.scrollY, open=[];
     document.querySelectorAll('details').forEach(function(d,i){{if(d.open)open.push(i)}});
     document.getElementById('app').innerHTML=t;
     document.querySelectorAll('details').forEach(function(d,i){{
       if(open.indexOf(i)>=0)d.open=true;}});
     var n=document.getElementById('q');
     if(n&&keep!==null){{n.value=keep; if(focus)n.focus();}}
     window.scrollTo(0,y); wire();
   }}).catch(function(){{}});
}},2000)}}
wire();
</script>'''


def page(title, body, poll):
    return PAGE.format(title=escape(title), body=body, poll='1' if poll else '0',
                       paper=PAPER, panel=PANEL, ink=INK, dim=DIM, rule=RULE, accent=ACCENT,
                       green=GREEN, amber=AMBER, red=RED)


def search_box(width='260px'):
    return (f'<input id="q" placeholder="find a signal&hellip;  ( / )" '
            f'style="min-width:{width}"><span id="qn" class="d"></span>')


def status_class(status):
    if status in ('SUCCESS', 'PASSED', 'CLOSED'):
        return 'ok'
    if status in ('FAIL', 'FAILED', 'ERROR'):
        return 'warn'
    if status in ACTIVE_STATUS or status == 'STALE':
        return 'run'
    return 'd'


def agent_chip(run_id, chip):
    return (f'<a class="ag{" off" if chip["dormant"] else ""}" '
            f'href="/agent/{quote(chip["agent"])}?run={quote(run_id)}">'
            f'{dot(chip["color"])}{"?" if chip["dormant"] else ""}'
            f'{escape(chip["agent"])} <b>{chip["count"]}</b></a>')


def legend(types=('tool_call', 'peer_message', 'gate_pass', 'gate_fail', 'error', 'artifact')):
    return '<div class="legend">' + ''.join(
        f'{dot(TYPE_COLOR[t])} {t}&nbsp;&nbsp;' for t in types) + '</div>'


def render_swarms(con, limit, cap_usd):
    rows = swarm_list(con, limit, cap_usd)
    live = sum(1 for r in rows if r['live'])
    priced = [r for r in rows if r['budget']['kind'] == 'cost']
    spent = sum(r['budget']['used'] for r in priced)
    newest = rows[0] if rows else None
    state = {'state': 'live' if live else 'done',
             'ticking': bool(newest and newest['live']),
             'elapsed': clock(newest['started_at'], newest['ended_at']) if newest else '--',
             'started_at': newest['started_at'] if newest else '',
             'agent_count': sum(r['agents'] for r in rows),
             'model': newest['model'] if newest else '?',
             'cost': f'${spent:.4f} COST' if priced else 'COST n/a',
             'tokens': sum(r['tokens'] for r in rows), 'calls': sum(r['calls'] for r in rows),
             'budget': {'pct': 0.0, 'left': f'{len(rows)} runs on record'}}
    # The nav needs a run to point at, so the newest swarm stands in for "current".
    out = [header(state, 'SWARMS', newest['adw_id'] if newest else None), '<main>',
           f'<h1>SWARMS</h1><p class="sub">{len(rows)} swarms &nbsp;|&nbsp; {live} live</p>',
           f'<div class="tools">{search_box("300px")}</div>']
    if not rows:
        out.append('<p class="empty">no runs recorded yet</p>')
    for row in rows:
        tint = stable_color(row['adw_id'])
        hay = ' '.join([row['name'], row['adw_id'], row['status'], row['request'],
                        row['model'], row['state']]).lower()
        thread_word = 'thread' if row['threads'] == 1 else 'threads'
        out.append(f'''<a class="row" data-hay="{escape(hay)}"
 href="/threads?run={quote(row['adw_id'])}"
 style="border-left-color:{tint};background:linear-gradient(90deg,{tint}0e,{PANEL} 46%)">
 <div class="line">
  {dot(GREEN if row['live'] else (AMBER if row['open'] else DIM), row['live'])}
  <span class="tag {status_class(row['status'])}">{escape(row['status'])}</span>
  <span class="d">{escape(day(row['started_at']))}</span>
  <span class="name grow">{escape(row['name'])}
   <span class="d">{escape(row['adw_id'])}</span></span>
  <span class="chip">{escape(str(row['model']))}</span>
  <span>{escape(row['elapsed'])}</span>
 </div>
 <div class="line d" style="margin-top:5px">
  <span>{row['agents']} agents &middot; {row['threads']} {thread_word} &middot;
   {row['msgs']} msgs &middot; {row['phases']} phases &middot; {num(row['calls'])} calls
   &middot; {num(row['tokens'])} tok{
   f" &middot; <span class='warn'>{row['fails']} fail</span>" if row['fails'] else ''}</span>
  <span class="grow"></span>
  {bar(row['budget']['pct'], RED if row['budget'].get('over') else tint, '150px')}
  <span class="{'warn' if row['budget'].get('over') else ''}">
   {escape(row['budget']['label'])}</span>
 </div>
 <div class="d" style="margin-top:4px">{escape(clip(row['request'], 150))}</div>
</a>''')
    out.append('</main>')
    return ''.join(out), bool(live)


def scrubber(run):
    start, end = run['span']
    return (f'<footer><span class="d">{stamp(start.isoformat() if start else None)}</span>'
            + ticks(run['events'], 22, f'{run["event_count"]} events')
            + f'<span class="d">{stamp(end.isoformat() if end else None)}</span>'
            f'<span class="d">{run["msgs"]} messages | {num(run["calls"])} tool calls</span>'
            f'<a href="/raw?run={quote(run["adw_id"])}" style="color:{ACCENT}">raw trace '
            f'&rarr;</a></footer>')


def head_state(run):
    return {'state': run['state'], 'ticking': run['live'], 'elapsed': run['elapsed'],
            'started_at': run['started_at'], 'agent_count': len(run['agents']),
            'model': run['model'], 'cost': run['budget']['meter'], 'tokens': run['tokens'],
            'calls': run['calls'], 'budget': run['budget']}


def crumb(run, tail, extra=''):
    return (f'<h1><a href="/">SWARMS</a> &rsaquo; '
            f'<a href="/threads?run={quote(run["adw_id"])}">{escape(run["name"])}</a> '
            f'&rsaquo; {escape(tail)}{extra}</h1>')


def unknown(what, key):
    return (f'<main><h1>UNKNOWN {escape(what)}</h1>'
            f'<p class="sub">{escape(key or "(none given)")}</p>'
            f'<p><a href="/" style="color:{ACCENT}">&larr; every swarm</a></p></main>'), False


SORTS = [('ACTIVITY', 'activity'), ('CREATED', 'created'),
         ('VOLUME', 'volume'), ('MEMBERS', 'members')]
SHOWS = [('ALL', 'all'), ('ACTIVE', 'active'), ('DORMANT', 'dormant')]


def segment(label, options, current, base):
    body = ''.join(f'<a class="{"on" if key == current else ""}" href="{escape(base)}{key}">'
                   f'{name}</a>' for name, key in options)
    return f'<span class="d">{label}:</span><span class="seg">{body}</span>'


def render_threads(con, run_id, cap_usd, order='activity', show='all'):
    run = load(con, run_id, cap_usd)
    if not run:
        return unknown('SWARM', run_id)
    boards = thread_rows(run)
    phases = phase_rows(run)
    if show == 'active':
        phases = [p for p in phases if p['active']]
    elif show == 'dormant':
        phases = [p for p in phases if not p['active']]
    keys = {'activity': lambda p: (p['ticks'][-1]['pct'] if p['ticks'] else -1),
            'created': lambda p: -p['seq'], 'volume': lambda p: p['events'],
            'members': lambda p: len(p['members'])}
    phases.sort(key=keys.get(order, keys['activity']), reverse=True)
    base = f'/threads?run={quote(run_id)}'
    total_msgs = sum(b['messages'] for b in boards)
    board_word = 'board' if len(boards) == 1 else 'boards'

    out = [header(head_state(run), 'THREADS', run_id), '<main>',
           crumb(run, 'THREADS'),
           f'<p class="sub">{escape(clip(run["request"], 200))}</p>',
           f'<div class="tools">{search_box()}'
           + segment('ORDER', SORTS, order, f'{base}&show={show}&order=')
           + segment('SHOW', SHOWS, show, f'{base}&order={order}&show=') + '</div>',
           f'<h2>THREADS <em>{len(boards)} {board_word} &middot; {total_msgs} messages '
           f'&middot; {run["rounds"]} rounds</em>'
           '<span class="note">one implicit board: peers post a typed proposal at each '
           'round boundary, there is no per-topic thread model yet</span></h2>']
    for board in boards:
        hay = ' '.join([board['name']] + [c['agent'] for c in board['chips']]
                       + [board['latest']['text'] if board['latest'] else '']).lower()
        latest = board['latest']
        preview_html = ''
        if latest:
            warn = latest['type'] in FAIL_TYPES
            preview_html = (
                f'<div class="{"warn" if warn else "d"}" style="margin-top:7px">'
                f'{escape(latest["agent"] or "system")}: {"&#9888; " if warn else ""}'
                f'{escape(clip(latest["text"], 200))} '
                f'<span class="d">&middot; {escape(stamp(latest["time"]))}</span></div>')
        out.append(f'''<a class="row" data-hay="{escape(hay)}"
 href="/board?run={quote(run_id)}&board={quote(board['key'])}"
 style="border-left-color:{ACCENT}">
 <div class="line">
  <span class="tag {status_class(board['status'])}">[{escape(board['status'])}]</span>
  <span class="name" style="color:{ACCENT}">&#9670; {escape(board['name'])}</span>
  {ticks(board['ticks'], 18, f"{board['messages']} messages across the run")}
  <span><b>{board['messages']}</b> <span class="d">msgs</span></span>
  <span class="d">{board['members']} of {len(board['chips'])} posted</span>
 </div>
 <div class="chips">{''.join(agent_chip(run_id, c) for c in board['chips'])}</div>
 {preview_html}
</a>''')

    out.append(f'<h2>PHASES <em>{len(phases)} of {len(run["phases"])} steps</em>'
               '<span class="note">execution steps, not threads</span></h2>')
    if not phases:
        out.append('<p class="empty">no phases match</p>')
    for phase in phases:
        hay = ' '.join([phase['name'], phase['owner'], phase['kind'], phase['status'],
                        phase['error'], phase['latest']['text'] if phase['latest'] else '']
                       + phase['members']).lower()
        retry = (f'<span class="tag warn">try {phase["attempt"]}</span>'
                 if phase['attempt'] > 1 else '')
        err = (f'<div class="warn" style="margin-top:6px">&#9888; '
               f'{escape(clip(phase["error"], 190))}</div>' if phase['error'] else '')
        latest = phase['latest']
        note = ''
        if latest and not phase['error']:
            note = (f'<div class="d" style="margin-top:6px">{escape(latest["agent"] or "system")}: '
                    f'{escape(clip(latest["text"], 190))}</div>')
        out.append(f'''<a class="row" data-hay="{escape(hay)}"
 href="/phase?run={quote(run_id)}&phase={quote(phase['phase_id'])}"
 style="border-left-color:{phase['color']}">
 <div class="line">
  <span class="d" style="width:22px;text-align:right">{phase['seq']}</span>
  <span class="name">{escape(phase['name'])}</span>
  <span class="tag {status_class(phase['status'])}">{escape(phase['status'])}</span>{retry}
  <span class="d">{dot(phase['color'])} {escape(phase['kind'])}/{escape(phase['owner'])}</span>
  {ticks(phase['ticks'], 18, f"{phase['events']} events")}
  <span class="d">{escape(phase['elapsed'])}</span>
  <span><b>{num(phase['events'])}</b> <span class="d">events</span></span>
  <span><b>{num(phase['calls'])}</b> <span class="d">calls</span></span>
  <span><b>{phase['posts']}</b> <span class="d">posts</span></span>
 </div>{err}{note}
</a>''')
    out += [legend(), '</main>', scrubber(run)]
    return ''.join(out), run['live']


def message_block(run, event):
    """One peer_message, rendered whole: it is the only place agents talk here."""
    payload = event['payload']
    color = run['colors'].get(event['agent']) or stable_color(event['agent'])
    status = str(payload.get('status') or '').upper()
    parts = [f'''<div class="msg" data-hay="{escape((event['agent'] + ' ' + event['text']).lower())}"
 style="border-left-color:{color}">
 <div class="line">
  {dot(color)}
  <a class="name" href="/agent/{quote(event['agent'])}?run={quote(run['adw_id'])}">
   {escape(event['agent'].upper() or 'SYSTEM')}</a>
  <span class="tag">ROUND {escape(str(payload.get('round', '?')))}</span>
  {f'<span class="tag {status_class(status)}">{escape(status)}</span>' if status else ''}
  <span class="grow"></span>
  <span class="d">{escape(stamp(event['time']))}</span>
 </div>
 <pre>{escape(payload.get('summary') or event['text'])}</pre>''']
    for key, label in (('notes_for_next_agent', 'NOTES'), ('decisions', 'DECISIONS'),
                       ('risks', 'RISKS'), ('artifacts', 'ARTIFACTS')):
        value = payload.get(key)
        if not value:
            continue
        if isinstance(value, list):
            value = '\n'.join(f'- {item}' for item in value)
        parts.append(f'<div class="kv"><b>{label}</b><pre>{escape(str(value))}</pre></div>')
    code = payload.get('code')
    if code:
        parts.append(f'<details><summary>code &middot; {len(str(code)):,} chars</summary>'
                     f'<pre>{escape(str(code))}</pre></details>')
    parts.append('</div>')
    return ''.join(parts)


def render_board(con, run_id, board_key, cap_usd):
    run = load(con, run_id, cap_usd)
    if not run:
        return unknown('SWARM', run_id)
    msgs = run['boards'].get(board_key)
    if msgs is None:
        return unknown('BOARD', board_key)
    board = next(b for b in thread_rows(run) if b['key'] == board_key)
    stream = board_stream(run, board_key, msgs)
    out = [header(head_state(run), 'THREADS', run_id), '<main>',
           crumb(run, 'THREADS', f' &rsaquo; {escape(board["name"])}'),
           f'<p class="sub">{board["messages"]} messages &middot; {board["rounds"]} rounds '
           f'&middot; {board["members"]} of {len(board["chips"])} agents posted '
           f'&middot; {escape(stamp(board["first"]))}-{escape(stamp(board["last"]))}</p>',
           f'<div class="tools">{search_box()}</div>',
           f'<div class="chips">{"".join(agent_chip(run_id, c) for c in board["chips"])}</div>',
           '<h2>MESSAGES</h2>']
    if not stream:
        out.append('<p class="empty">nothing posted to this board yet</p>')
    for event in stream:
        if event['type'] == 'peer_message':
            out.append(message_block(run, event))
        else:
            out.append(f'''<div class="msg" data-hay="{escape(event['text'].lower())}"
 style="border-left-color:{RED}">
 <div class="line"><span class="warn name">&#9888; SYSTEM</span>
  <span class="tag warn">{escape(event['type'].upper())}</span>
  <span class="d">{escape(event['agent'] or 'runtime')}</span>
  <span class="grow"></span><span class="d">{escape(stamp(event['time']))}</span></div>
 <pre class="warn">{escape(clip(event['text'], 600))}</pre></div>''')
    out += ['</main>', scrubber(run)]
    return ''.join(out), run['live']


TABS = [('ALL', 'all'), ('MESSAGES', 'messages'), ('TOOLS', 'tools'),
        ('THINKING', 'thinking'), ('FAILURES', 'failures'), ('SESSION ENDS', 'ends')]
TAB_TYPES = {'messages': MESSAGE_TYPES, 'tools': ('tool_call',),
             'thinking': ('log', 'run_contract', 'agent_start'), 'failures': FAIL_TYPES,
             'ends': ('phase_end',)}


def tab_bar(base, tab):
    return '<span class="seg">' + ''.join(
        f'<a class="{"on" if key == tab else ""}" href="{escape(base)}{key}">{name}</a>'
        for name, key in TABS) + '</span>'


TABLE_CAP = 600


def event_table(events, tab='all', with_agent=False):
    rows = events if tab not in TAB_TYPES else [e for e in events if e['type'] in TAB_TYPES[tab]]
    if not rows:
        return '<p class="empty">nothing in this tab</p>'
    # Newest matter most and the page repolls every 2s, so an old head is dropped
    # rather than shipped: a 10k-event run would otherwise be a megabyte a poll.
    note = ''
    if len(rows) > TABLE_CAP:
        note = (f'<p class="empty">showing the last {TABLE_CAP:,} of {len(rows):,} events '
                f'&middot; the full trace is in the db</p>')
        rows = rows[-TABLE_CAP:]
    body = []
    for event in rows:
        # Tool calls are coloured by tool name, so run_command and view_file read
        # apart at a glance; everything else keeps its event-type colour.
        if event['type'] == 'tool_call':
            label, color = event['name'] or 'tool', stable_color(event['name'])
        else:
            label, color = event['type'], TYPE_COLOR.get(event['type'], DIM)
        who = (f'<td class="w">{escape(clip(event["agent"], 14))}</td>' if with_agent else '')
        body.append(f'<tr data-hay="{escape((label + " " + event["agent"] + " " + event["text"]).lower())}">'
                    f'<td class="t">{stamp(event["time"])}</td>'
                    f'<td class="k" style="color:{color}">{escape(clip(label, 22))}</td>{who}'
                    f'<td><pre>{escape(clip(event["text"], 220))}</pre></td>'
                    f'<td class="ms">{str(event["ms"]) + "ms" if event["ms"] is not None else ""}'
                    f'</td></tr>')
    return f'{note}<table>{"".join(body)}</table>'


def render_phase(con, run_id, phase_id, cap_usd, tab='all'):
    run = load(con, run_id, cap_usd)
    if not run:
        return unknown('SWARM', run_id)
    phase = next((p for p in phase_rows(run) if p['phase_id'] == phase_id), None)
    if not phase:
        return unknown('PHASE', phase_id)
    events = run['by_phase'].get(phase_id, [])
    base = f'/phase?run={quote(run_id)}&phase={quote(phase_id)}&tab='
    members = ' '.join(
        agent_chip(run_id, {'agent': who, 'count': sum(1 for e in events if e['agent'] == who),
                            'dormant': False,
                            'color': run['colors'].get(who) or stable_color(who)})
        for who in phase['members'])
    out = [header(head_state(run), 'THREADS', run_id), '<main>',
           crumb(run, 'PHASES', f' &rsaquo; {escape(phase["name"])}'),
           f'<p class="sub">step {phase["seq"]} of {len(run["phases"])} &middot; '
           f'{escape(phase["kind"])}/{escape(phase["owner"])} &middot; attempt '
           f'{phase["attempt"]} ({phase["retries"]} retries) &middot; '
           f'{escape(stamp(phase["started_at"]))}-{escape(stamp(phase["ended_at"]))} &middot; '
           f'{escape(phase["elapsed"])}</p>',
           f'<div class="line"><span class="tag {status_class(phase["status"])}">'
           f'{escape(phase["status"])}</span>'
           f'<span>{num(phase["events"])} events</span>'
           f'<span>{num(phase["calls"])} calls</span>'
           f'<span>{phase["posts"]} board posts</span>'
           f'<span class="{"warn" if phase["fails"] else "d"}">{phase["fails"]} failures</span>'
           f'{ticks(phase["ticks"], 18, "phase events across the run span")}</div>']
    if phase['error']:
        out.append(f'<div class="msg warn" style="border-left-color:{RED};margin-top:10px">'
                   f'<pre>{escape(phase["error"])}</pre></div>')
    if members:
        out += ['<h2>AGENTS IN THIS PHASE</h2>', f'<div class="chips">{members}</div>']
    out += ['<h2>EVENTS</h2>',
            f'<div class="tools">{search_box()}{tab_bar(base, tab)}</div>',
            event_table(events, tab, with_agent=True), '</main>', scrubber(run)]
    return ''.join(out), run['live']


def render_agents(con, run_id, cap_usd):
    run = load(con, run_id, cap_usd)
    if not run:
        return unknown('SWARM', run_id)
    people = agent_rows(run)
    busiest = max([p['calls'] for p in people] or [0])
    out = [header(head_state(run), 'AGENTS', run_id), '<main>',
           crumb(run, 'AGENTS'),
           f'<p class="sub">{len(people)} agents &middot; '
           f'{sum(1 for p in people if p["live"])} with a live process &middot; '
           f'{sum(p["posts"] for p in people)} board posts</p>',
           f'<div class="tools">{search_box()}</div>']
    if not people:
        out.append('<p class="empty">no agents registered on this swarm</p>')
    for person in people:
        hay = ' '.join([person['agent'], person['model'], person['coding_agent']]).lower()
        out.append(f'''<a class="row" data-hay="{escape(hay)}"
 href="/agent/{quote(person['agent'])}?run={quote(run_id)}"
 style="border-left-color:{person['color']}">
 <div class="line">
  {dot(person['color'], person['live'])}
  <span class="name">{escape(person['agent'].upper())}@SWARM.ORG</span>
  <span class="d">agent-{person['index']}</span>
  <span class="chip">{escape(person['model'] or '?')}</span>
  {ticks(person['ticks'], 18, f"{len(person['events'])} events")}
  <span class="d">{escape(stamp(person['first']))}-{escape(stamp(person['last']))}</span>
 </div>
 <div class="line d" style="margin-top:5px">
  <span>{num(person['calls'])} calls &middot; {person['posts']} posts &middot;
   {len(person['phases'])} phases &middot;
   <span class="{'warn' if person['failures'] else 'd'}">{person['failures']} failures</span>
   </span>
  <span class="grow"></span>
  {bar(100.0 * person['calls'] / busiest if busiest else 0, person['color'], '150px')}
  <span>{(100 * person['calls'] // run['calls']) if run['calls'] else 0}% of swarm calls</span>
 </div>
</a>''')
    out += ['</main>', scrubber(run)]
    return ''.join(out), run['live']


def render_agent(con, run_id, agent, cap_usd, tab='all'):
    run = load(con, run_id, cap_usd)
    if not run:
        return unknown('SWARM', run_id)
    view = next((p for p in agent_rows(run) if p['agent'] == agent), None)
    if not view:
        return unknown('AGENT', agent)
    window, used = view['context_window'], view['context_tokens']
    ctx = (f'{short(used)} of {short(window)} | {100 * used // window if window else 0}%'
           if window else 'not reported by this run')
    token_line = (f'{num(view["tokens"])} tokens' if view['tokens_recorded']
                  else f'{num(run["tokens"])} tokens (swarm total; this run does not split '
                       f'tokens per agent)')
    posted = ''.join(
        f'<div class="line" style="margin:2px 0"><span class="tag">ROUND '
        f'{escape(str(e["payload"].get("round", "?")))}</span>'
        f'<span class="d">{escape(stamp(e["time"]))}</span>'
        f'<span class="grow">{escape(clip(e["text"], 150))}</span></div>'
        for e in view['events'] if e['type'] == 'peer_message')
    phases = ''.join(
        f'<a class="line" style="margin:2px 0" '
        f'href="/phase?run={quote(run_id)}&phase={quote(p["phase_id"])}">'
        f'<span class="name">{escape((p["name"] or "").upper())}</span>'
        f'<span class="tag {status_class((p["status"] or "?").upper())}">'
        f'{escape((p["status"] or "?").upper())}</span>'
        f'<span class="d">{len(run["by_phase"].get(p["phase_id"], []))} events &middot; '
        f'{escape(elapsed(p["started_at"], p["ended_at"]))}</span></a>'
        for p in view['phases'])
    avg = f'{view["ms"] // view["timed"]}ms avg' if view['timed'] else 'no timings'
    chips = ' '.join(f'<span class="chip">{c}</span>' for c in (
        f'{short(view["tokens"]) if view["tokens_recorded"] else "n/a"} read',
        'n/a write', 'n/a cache r', 'n/a cache w',
        f'{short(run["tokens"])} swarm total'))
    base = f'/agent/{quote(agent)}?run={quote(run_id)}&tab='
    out = [header(head_state(run), 'AGENTS', run_id), '<main>',
           f'<h1>{dot(view["color"], view["live"])} {escape(agent.upper())}@SWARM.ORG</h1>',
           f'<p class="sub">agent-{view["index"]} | {escape(view["coding_agent"] or "?")} '
           f'{escape(view["model"] or "?")} | '
           f'{"live process" if view["live"] else "no live process"} | '
           f'active {escape(stamp(view["first"]))}-{escape(stamp(view["last"]))} | '
           f'<a href="/threads?run={quote(run_id)}" style="color:{ACCENT}">'
           f'{escape(run["name"])}</a></p>',
           f'<h2>THREADS <em>{view["posts"]} posts to the '
           f'<a href="/board?run={quote(run_id)}&board=primary" style="color:{ACCENT}">'
           f'primary board</a></em></h2>',
           posted or '<p class="empty">this agent has posted nothing to the board</p>',
           f'<h2>PHASES <em>{len(view["phases"])} owned</em></h2>',
           phases or '<p class="empty">owns no phase on this swarm</p>',
           '<h2>AGENT STATS</h2>',
           f'<div class="line">{num(view["calls"])} calls | {token_line} | '
           f'{escape(run["budget"]["label"])} | {avg} | '
           f'<span class="{"warn" if view["failures"] else "d"}">{view["failures"]} '
           f'failures</span></div>',
           f'<div class="chips" style="margin-top:8px">{chips}</div>',
           '<h2>CONTEXT WINDOW</h2>',
           f'<div class="line">{bar(100 * used / window if window else 0)} '
           f'<span class="d">{escape(ctx)}</span></div>',
           '<h2>SHARE OF THE SWARM</h2>',
           f'<div class="line">{bar(100.0 * view["calls"] / run["calls"] if run["calls"] else 0, view["color"])} '
           f'<span class="d">{num(view["calls"])} of {num(run["calls"])} tool calls</span></div>',
           '<h2>EVENTS</h2>',
           f'<div class="tools">{search_box("240px")}{tab_bar(base, tab)}</div>',
           event_table(view['events'], tab), '</main>', scrubber(run)]
    return ''.join(out), run['live']


def render_raw(con, run_id, cap_usd, tab='all'):
    run = load(con, run_id, cap_usd)
    if not run:
        return unknown('SWARM', run_id)
    base = f'/raw?run={quote(run_id)}&tab='
    out = [header(head_state(run), 'THREADS', run_id), '<main>',
           crumb(run, 'RAW TRACE'),
           f'<p class="sub">{num(run["event_count"])} events &middot; '
           f'{escape(day(run["started_at"]))} &rarr; {escape(day(run["last_at"]))} '
           f'({escape(ago(run["last_at"]))})</p>',
           f'<div class="tools">{search_box("300px")}{tab_bar(base, tab)}</div>',
           event_table(run['events'], tab, with_agent=True),
           legend(tuple(TYPE_COLOR)), '</main>', scrubber(run)]
    return ''.join(out), run['live']


# ── server ──────────────────────────────────────────────────────────────────

def route(con, path, query, cap_usd, limit):
    """Return (body_html, title, live). One place so the self-check can call it."""
    run_id = (query.get('run') or [''])[0]
    tab = (query.get('tab') or ['all'])[0]
    if path.startswith('/agent/'):
        agent = path[len('/agent/'):]
        body, live = render_agent(con, run_id, agent, cap_usd, tab)
        return body, f'{agent}@swarm.org', live
    if path == '/threads':
        body, live = render_threads(con, run_id, cap_usd, (query.get('order') or ['activity'])[0],
                                    (query.get('show') or ['all'])[0])
        return body, f'threads {run_id}', live
    if path == '/board':
        body, live = render_board(con, run_id, (query.get('board') or ['primary'])[0], cap_usd)
        return body, f'board {run_id}', live
    if path == '/phase':
        body, live = render_phase(con, run_id, (query.get('phase') or [''])[0], cap_usd, tab)
        return body, f'phase {run_id}', live
    if path == '/agents':
        body, live = render_agents(con, run_id, cap_usd)
        return body, f'agents {run_id}', live
    if path == '/raw':
        body, live = render_raw(con, run_id, cap_usd, tab)
        return body, f'raw {run_id}', live
    body, live = render_swarms(con, limit, cap_usd)
    return body, 'swarms', live


def handler(db, cap_usd, limit):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip('/') or '/'
            if path == '/favicon.ico':
                return self.send_error(404)
            try:
                with closing(connect(db)) as con:
                    body, title, live = route(con, path, parse_qs(parsed.query), cap_usd, limit)
            except (sqlite3.Error, OSError) as error:
                body, title, live = (f'<main><h1>TRACE UNREADABLE</h1>'
                                     f'<p class="sub">{escape(str(error))}</p></main>',
                                     'error', False)
            partial = self.headers.get('x-partial') == '1'
            text = body if partial else page(title, body, live)
            data = text.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    return Handler


def serve(db, host, port, cap_usd, limit, open_browser):
    if not Path(db).exists():
        print(f'no trace db at {db}', file=sys.stderr)
        return 1
    server = ThreadingHTTPServer((host, port), handler(db, cap_usd, limit))
    url = f'http://{host}:{port}/'
    print(f'swarm ui  {url}  <-  {db}   (read-only, ctrl-c to quit)')
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# ── check ───────────────────────────────────────────────────────────────────

SCHEMA = """
    CREATE TABLE sessions(adw_id TEXT, adw_name TEXT, request TEXT, status TEXT,
      engineer TEXT, started_at TEXT, ended_at TEXT, total_tokens INT, total_cost REAL,
      archived INT);
    CREATE TABLE phases(phase_id TEXT, adw_id TEXT, seq INT, name TEXT, kind TEXT,
      owner TEXT, description TEXT, status TEXT, attempt INT, retries INT, error TEXT,
      started_at TEXT, ended_at TEXT);
    CREATE TABLE events(event_id TEXT, adw_id TEXT, phase_id TEXT, parent_id TEXT,
      type TEXT, name TEXT, payload_json TEXT, tokens INT, started_at TEXT, ended_at TEXT);
    CREATE TABLE processes(id INTEGER PRIMARY KEY, adw_id TEXT, kind TEXT, name TEXT,
      pid INT, command TEXT, started_at TEXT, ended_at TEXT);
    CREATE TABLE agent_sessions(adw_id TEXT, agent TEXT, coding_agent TEXT, model TEXT,
      color TEXT, session_id TEXT, context_tokens INT, context_window INT,
      created_at TEXT, last_used_at TEXT);
    CREATE TABLE gate_results(id INTEGER PRIMARY KEY, adw_id TEXT, phase_id TEXT,
      attempt INT, gate TEXT, passed INT, violations_json TEXT, checks_json TEXT,
      created_at TEXT);
"""


def _fixture(path):
    """A two-agent, three-phase run with one board, one violation and a live process."""
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    live = (now().replace(microsecond=0)).isoformat()
    t0, t1 = '2026-09-10T12:00:00+00:00', '2026-09-10T12:04:00+00:00'
    con.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)',
                ('run1', 'raytracer', 'merge intervals', 'running', 'eng', t0, None,
                 271318, 0.0, 0))
    for seq, (pid, name, kind, owner, status) in enumerate((
            ('p1', 'stitch_r1', 'agent', 'stitch', 'success'),
            ('p2', 'cynic_r1', 'agent', 'cynic', 'running'),
            ('p3', 'acceptance', 'code', 'tests', 'fail')), start=1):
        con.execute('INSERT INTO phases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (pid, 'run1', seq, name, kind, owner, 'd', status, 2 if pid == 'p3' else 1,
                     1 if pid == 'p3' else 0, 'assertion failed' if pid == 'p3' else None,
                     t0, t1 if status != 'running' else None))
    for agent in ('stitch', 'cynic'):
        con.execute('INSERT INTO agent_sessions VALUES (?,?,?,?,?,?,?,?,?,?)',
                    ('run1', agent, 'agy', 'gemini-3.8-flash-medium', '#22d3ee', '',
                     55300, 1000000, t0, t0))
    con.execute('INSERT INTO processes VALUES (NULL,?,?,?,?,?,?,?)',
                ('run1', 'agent', 'cynic', 42, 'agy', t0, None))
    rows = [
        ('e0', '', 'run_contract', 'acceptance',
         {'model': 'gemini-3.8-flash-medium', 'cost_available': False,
          'definition_of_done': 'tests pass',
          'limits': {'agents': 2, 'peer_rounds': 2, 'max_calls': 7,
                     'call_timeout_seconds': 180}}, t0),
        ('e1', 'p1', 'tool_call', 'run_command',
         {'agent': 'stitch', 'duration_seconds': 1.25,
          'tool_info': {'name': 'run_command', 'parameters': {'cmd': 'pytest -q'}}}, t0),
        ('e2', 'p1', 'peer_message', 'stitch',
         {'agent': 'stitch', 'round': 1, 'status': 'success',
          'summary': 'merged the intervals', 'notes_for_next_agent': 'check the empty case',
          'decisions': ['D1: half-open ranges'], 'risks': ['none'],
          'code': 'def merge(x):\n    return x\n'}, t1),
        ('e3', 'p3', 'gate_fail', 'claims',
         {'agent': 'stitch', 'violations': 'claimed a test it never ran'}, t1),
        ('e4', 'p2', 'tool_call', 'view_file',
         {'agent': 'cynic', 'tool_info': {'name': 'view_file',
                                          'parameters': {'AbsolutePath': 'solution.py'}}}, live),
    ]
    for event_id, phase_id, kind, name, payload, when in rows:
        con.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (event_id, 'run1', phase_id, '', kind, name, json.dumps(payload),
                     None, when, None))
    con.commit()
    con.close()


def demo():
    """Self-check: build a trace on disk and assert every page renders real rows."""
    import tempfile
    from .monitor import demo as _  # noqa: F401  (keeps the two checks side by side)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'demo.db'
        _fixture(path)
        with closing(connect(path)) as ro:
            page_of = lambda p, q: route(ro, p, q, 30.0, 25)[0]
            swarms, _title, live = route(ro, '/', {}, 30.0, 25)
            threads = page_of('/threads', {'run': ['run1']})
            board = page_of('/board', {'run': ['run1'], 'board': ['primary']})
            phase = page_of('/phase', {'run': ['run1'], 'phase': ['p1']})
            phase_fail = page_of('/phase', {'run': ['run1'], 'phase': ['p3']})
            agents = page_of('/agents', {'run': ['run1']})
            detail = page_of('/agent/stitch', {'run': ['run1']})
            tools = page_of('/agent/stitch', {'run': ['run1'], 'tab': ['tools']})
            fails = page_of('/agent/stitch', {'run': ['run1'], 'tab': ['failures']})
            raw = page_of('/raw', {'run': ['run1']})
            only_active = page_of('/threads', {'run': ['run1'], 'show': ['active']})
            no_swarm = page_of('/threads', {'run': ['nope']})
            no_agent = page_of('/agent/ghost', {'run': ['run1']})
            no_phase = page_of('/phase', {'run': ['run1'], 'phase': ['p9']})
            no_board = page_of('/board', {'run': ['run1'], 'board': ['side']})
            listed = swarm_list(ro, 25, 30.0)
            run = load(ro, 'run1', 30.0)
            boards = thread_rows(run)
            steps = phase_rows(run)
            people = agent_rows(run)

        # The data layer, not just the markup.
        assert listed[0]['adw_id'] == 'run1' and listed[0]['live'], listed
        assert listed[0]['calls'] == 2 and listed[0]['msgs'] == 1, listed
        # The correction this rewrite exists for: 3 phases, 1 board, never 3 threads.
        assert listed[0]['phases'] == 3 and listed[0]['threads'] == 1, listed[0]
        assert listed[0]['agents'] == 2, listed[0]
        assert listed[0]['budget']['kind'] == 'calls', listed[0]['budget']
        assert listed[0]['budget']['cap'] == 28, listed[0]['budget']  # 7 calls x 2 agents x 2 rounds
        assert run['model'] == 'gemini-3.8-flash-medium' and len(run['agents']) == 2, run['model']
        assert run['rounds'] == 1 and run['msgs'] == 1 and run['fails'] == 1, run
        assert len(boards) == 1 and boards[0]['messages'] == 1, boards
        assert boards[0]['members'] == 1 and len(boards[0]['chips']) == 2, boards[0]
        assert [c['dormant'] for c in boards[0]['chips']] == [False, True], boards[0]['chips']
        assert len({c['color'] for c in boards[0]['chips']}) == 2, boards[0]['chips']
        assert [s['name'] for s in steps] == ['STITCH_R1', 'CYNIC_R1', 'ACCEPTANCE'], steps
        assert steps[0]['calls'] == 1 and steps[0]['posts'] == 1, steps[0]
        assert steps[2]['error'] == 'assertion failed' and steps[2]['attempt'] == 2, steps[2]
        assert [s['active'] for s in steps] == [False, True, False], steps
        assert all(0.0 <= t['pct'] <= 100.0 for t in steps[0]['ticks']), steps[0]['ticks']
        stitch = next(p for p in people if p['agent'] == 'stitch')
        cynic = next(p for p in people if p['agent'] == 'cynic')
        assert stitch['calls'] == 1 and stitch['failures'] == 1 and stitch['posts'] == 1, stitch
        assert not stitch['live'] and cynic['live'], (stitch['live'], cynic['live'])
        assert stitch['context_window'] == 1000000, stitch
        assert stitch['color'] != cynic['color'], people
        assert len(set(PALETTE)) == len(PALETTE) >= 20, 'a 20-agent swarm needs 20 distinct dots'
        # An idle unfinished run is reported stale, not live.
        assert liveness(None, '2020-01-01T00:00:00+00:00', 'running') == 'stale'
        assert liveness(None, now().isoformat(), 'running') == 'live'
        assert liveness('2026-01-01T00:00:00+00:00', None, 'success') == 'done'
        assert budget(0, {'max_calls': 1, 'peer_rounds': 1}, 9, 1, 30, False)['over'], 'over-cap'
        # No run in the sample trace prices itself, so the dollar bar is checked here.
        priced = budget(11.6551, {}, 0, 3, 30.0, True)
        assert priced['label'] == '$11.6551 of $30' and 38.8 < priced['pct'] < 38.9, priced
        assert priced['meter'] == '$11.6551 COST' and not priced['over'], priced
        # A sparkline thins to a fixed width, and a lone violation survives it.
        dense = [{'pct': i / 20.0, 'type': 'tool_call'} for i in range(2000)]
        dense[900] = {'pct': 45.0, 'type': 'gate_fail'}
        assert len(thin(dense)) <= TICK_BUCKETS, len(thin(dense))
        assert any(t['type'] == 'gate_fail' for t in thin(dense)), 'thinning ate the violation'

        # The pages.
        assert live and 'RAYTRACER' in swarms and 'merge intervals' in swarms, swarms[:400]
        assert '271,318' in swarms and 'gemini-3.8-flash-medium' in swarms, swarms[:400]
        assert '1 thread ' in swarms and '3 phases' in swarms, swarms
        assert 'href="/threads?run=run1"' in swarms, swarms[:400]
        assert 'LIVE' in swarms, swarms[:400]
        # Threads and phases are two lists with two counts, under two headings.
        assert '>THREADS <em>1 board' in threads, threads
        assert '>PHASES <em>3 of 3 steps' in threads, threads
        assert 'STITCH_R1' in threads and 'ACCEPTANCE' in threads, threads
        assert 'execution steps, not threads' in threads, threads
        assert 'find a signal' in threads and 'ORDER' in threads and 'SHOW' in threads, threads
        assert 'stitch <b>1</b>' in threads and '?cynic <b>0</b>' in threads, threads
        assert 'CLAIM VIOLATION' in threads and 'warn' in threads, threads
        assert 'assertion failed' in threads, threads
        assert 'raw trace' in threads and '1 messages | 2 tool calls' in threads, threads[-600:]
        assert threads.count('class="row"') == 4, 'one board row plus three phase rows'
        assert only_active.count('class="row"') == 2, 'one board row plus the running phase'
        assert 'CYNIC_R1' in only_active and 'ACCEPTANCE' not in only_active, only_active
        # The board carries the message body, not just a preview.
        assert 'merged the intervals' in board and 'check the empty case' in board, board
        assert 'D1: half-open ranges' in board and 'ROUND 1' in board, board
        assert 'def merge' in board and '<details>' in board, board
        assert 'claimed a test it never ran' in board, board
        assert 'run_command' in phase and 'pytest -q' in phase and '1250ms' in phase, phase
        assert 'STITCH_R1' in phase and 'stitch <b>2</b>' in phase, phase
        assert 'assertion failed' in phase_fail, phase_fail
        assert 'STITCH@SWARM.ORG' in agents and 'CYNIC@SWARM.ORG' in agents, agents[:400]
        assert "% of swarm calls" in agents, agents
        assert 'STITCH@SWARM.ORG' in detail and 'CONTEXT WINDOW' in detail, detail[:400]
        assert '55.3k of 1.0M | 5%' in detail, detail
        assert 'run_command' in detail and '1250ms' in detail, detail
        assert 'primary board' in detail and 'merged the intervals' in detail, detail
        assert '>PHASES <em>1 owned' in detail, detail
        assert 'not split\n' not in detail and 'tokens per agent' in detail, detail
        # The tab filters the event table; the sections above it always stand.
        assert 'peer_message</td>' in detail, detail
        assert 'run_command' in tools and 'peer_message</td>' not in tools, tools
        assert 'claimed a test it never ran' in fails, fails
        assert 'run_command' in raw and 'peer_message' in raw and 'view_file' in raw, raw[:600]
        for miss, what in ((no_swarm, 'SWARM'), (no_agent, 'AGENT'),
                           (no_phase, 'PHASE'), (no_board, 'BOARD')):
            assert f'UNKNOWN {what}' in miss, miss
        assert '<script' not in swarms, 'partial responses must carry no script tag'
        assert '<script' in page('t', swarms, True), 'full pages need the poller'
        assert 'cdn' not in page('t', threads, True).lower(), 'no external fetches'
        assert '//fonts.' not in page('t', threads, True), 'no external font'

        # A read-only handle must refuse writes even if a caller tries.
        try:
            with closing(connect(path)) as ro:
                ro.execute("UPDATE sessions SET status='tampered'")
            raise AssertionError('read-only connection accepted a write')
        except sqlite3.OperationalError:
            pass
    print('webui self-check ok')


def add_arguments(parser):
    parser.add_argument('--db', default=os.environ.get('SSSF_DB', 'adws/adw_data/sssf.db'))
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5178)
    parser.add_argument('--budget', type=float, default=float(os.environ.get('SSSF_BUDGET', 30)),
                        help='dollar cap for the budget bar when a run reports cost')
    parser.add_argument('--limit', type=int, default=25, help='swarms listed on the front page')
    parser.add_argument('--open', action='store_true', help='open a browser on start')
    parser.add_argument('--self-check', action='store_true', help='run the offline self-check')
    return parser


def main(args):
    if args.self_check:
        demo()
        return 0
    return serve(args.db, args.host, args.port, args.budget, args.limit, args.open)


if __name__ == '__main__':
    import argparse
    sys.exit(main(add_arguments(argparse.ArgumentParser(description=__doc__)).parse_args()) or 0)
