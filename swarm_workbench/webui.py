"""Web view of the SSSF trace: swarms, threads, phases, agents.

Server-rendered from the stdlib only: no bundler, no CDN, no framework. The one
external fetch is the JetBrains Mono face from Google Fonts, with a monospace
fallback when it does not load.
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
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .monitor import clip, connect, elapsed, parse_time

# The trailer's palette (brag-output/composition-v2): a dark ground, panels a
# shade lighter with hairline borders, off-white ink, grey labels. Amber is the
# single agent, green the swarm, blue anything running, red anything failed.
GROUND, PANEL, RAISED = '#0d1117', '#10161d', '#161b22'
RULE, INK, DIM, FAINT = '#30363d', '#e6edf3', '#8b949e', '#6e7681'
AMBER, GREEN, BLUE, RED, VIOLET = '#e3b341', '#3fb950', '#58a6ff', '#f85149', '#a371f7'
ACCENT = BLUE

# An unfinished run whose trace has been silent this long is reported stale
# rather than live: the db says "running" long after a crash, and a green dot
# that lies is worse than no dot.
STALE_AFTER = 120.0

# One colour per event type, reused by the ticks, the sparklines and the type
# column, so a colour means the same thing on every page.
TYPE_COLOR = {
    'tool_call': '#8b949e', 'peer_message': '#58a6ff', 'board_post': '#58a6ff',
    'gate_pass': '#3fb950', 'gate_fail': '#f85149', 'error': '#f85149',
    'artifact': '#a371f7', 'agent_start': '#e3b341', 'run_contract': '#6e7681',
    'sandbox_contents': '#a371f7', 'phase_start': '#484f58', 'phase_end': '#484f58',
    'log': '#6e7681',
}
# Twenty, because a swarm of twenty agents is a real roster here and ten would
# put two agents behind the same dot. Bright hues, all legible on the dark ground.
PALETTE = ['#58a6ff', '#3fb950', '#e3b341', '#f78166', '#a371f7',
           '#79c0ff', '#56d364', '#d29922', '#ff7b72', '#d2a8ff',
           '#39c5cf', '#7ee787', '#ffa657', '#ffa198', '#bc8cff',
           '#76e3ea', '#aff5b4', '#ffdf5d', '#ffc2b2', '#e2c5ff']
# A peer_message is the typed proposal an agent files at a round boundary; a
# board_post is a file it wrote to the shared board. Both are the conversation.
POST_TYPES = ('peer_message', 'board_post')
MESSAGE_TYPES = POST_TYPES + ('log', 'error', 'artifact', 'gate_pass', 'gate_fail')
FAIL_TYPES = ('error', 'gate_fail')
TICK_TYPES = POST_TYPES + FAIL_TYPES + ('tool_call', 'gate_pass', 'artifact')
ACTIVE_STATUS = ('RUNNING', 'PENDING', 'STARTED')
FAILED_STATUS = ('FAIL', 'FAILED', 'ERROR')
MENTION = re.compile(r'@([a-z0-9_-]+)')


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
        return f'{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}'
    return f'{total // 60:02d}:{total % 60:02d}'


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


def first_param(payload):
    """The first argument of a tool call, as (key, value). Two runners, two shapes:
    one nests it under tool_info, the other files parameters at the top level."""
    info = payload.get('tool_info') or payload
    params = info.get('parameters') or {}
    for key in params:
        return key, str(params[key])
    return '', ''


def first_line(text):
    return next((line.strip() for line in str(text or '').splitlines() if line.strip()), '')


def mentions(payload):
    """Who a post addresses: the payload's list, else the @tags in its text."""
    listed = payload.get('mentions')
    if isinstance(listed, list):
        return [str(m).lstrip('@') for m in listed if m]
    seen = []
    for name in MENTION.findall(str(payload.get('text') or '')):
        if name not in seen:
            seen.append(name)
    return seen


def thread_key(payload):
    """The board a post lands on. The board layer calls its one thread "main";
    the peer_message stream never named it. Both are the primary board."""
    key = payload.get('thread') or 'primary'
    return 'primary' if key == 'main' else key


def preview(kind, name, payload):
    if kind == 'peer_message':
        return payload.get('summary') or payload.get('notes_for_next_agent') or name
    if kind == 'board_post':
        head = first_line(payload.get('text'))
        title = payload.get('file') or name
        return f'{title}: {head}' if head else title
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
        key, value = first_param(payload)
        return f'{key}={value}' if key else (payload.get('tool_name') or name)
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


def budget(cost, limits, calls, agents, cap_usd, cost_available=True, tokens=0, token_cap=0):
    """Cost bar when the run priced itself, else the token budget it declared,
    else the tool-call cap it agreed to."""
    if cost_available:
        return {'kind': 'cost', 'used': cost, 'cap': cap_usd,
                'label': f'${cost:.4f} of ${cap_usd:g}', 'meter': f'${cost:.4f} COST',
                'left': f'${max(0.0, cap_usd - cost):.2f} left of ${cap_usd:g}',
                'over': cost > cap_usd,
                'pct': min(100.0, 100.0 * cost / cap_usd) if cap_usd else 0.0}
    if token_cap:
        over = tokens > token_cap
        return {'kind': 'tokens', 'used': tokens, 'cap': token_cap, 'over': over,
                'label': f'{num(tokens)} of {num(token_cap)} tokens (cost n/a)',
                'meter': 'COST n/a',
                'left': (f'{short(tokens - token_cap)} tok over budget' if over
                         else f'{short(token_cap - tokens)} tok left of {short(token_cap)}'),
                'pct': min(100.0, 100.0 * tokens / token_cap)}
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
        # A board post is traced when the harness polls the board, not when the
        # agent wrote it; the contract carries the file's mtime as posted_at.
        when = row['started_at']
        if row['type'] == 'board_post' and parse_time(payload.get('posted_at')):
            when = payload['posted_at']
        events.append({
            'id': row['event_id'], 'type': row['type'], 'name': row['name'] or '',
            'phase_id': row['phase_id'] or '', 'time': when, 'at': parse_time(when),
            'agent': payload.get('agent') or owner_of.get(row['phase_id'] or '', '') or '',
            'text': preview(row['type'], row['name'], payload),
            'payload': payload, 'tokens': row['tokens'] or 0,
            'ms': int(duration * 1000) if duration is not None else None,
            'pct': 0.0,
            'mentions': mentions(payload) if row['type'] == 'board_post' else [],
        })
    # Stable, so rows that share a stamp keep their rowid order from the query.
    events.sort(key=lambda e: e['time'] or '')
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
    msgs = sum(1 for e in events if e['type'] in POST_TYPES)
    fails = sum(1 for e in events if e['type'] in FAIL_TYPES)
    last_at = events[-1]['time'] if events else None
    state = liveness(session['ended_at'], last_at, session['status'])

    boards = {}
    for event in events:
        if event['type'] not in POST_TYPES:
            continue
        boards.setdefault(thread_key(event['payload']), []).append(event)
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
                       if e['type'] in POST_TYPES} - {None}),
    }
    run['budget'] = budget(run['cost'], run['limits'], calls, len(people), cap_usd,
                           spec.get('cost_available') is not False,
                           run['tokens'], int(spec.get('budget_tokens') or 0))
    return run


def board_stream(run, key, msgs):
    """What a board shows: its messages, plus the violations aimed at the swarm.

    A claim violation is filed as its own event type rather than posted, but it
    is addressed to the room, so it belongs in the room's stream. Only the
    primary board adopts them, or a second board would show them twice.
    """
    notices = [e for e in run['events'] if e['type'] in FAIL_TYPES] if key == 'primary' else []
    return sorted(msgs + notices, key=lambda e: (e['time'] or '', str(e['payload'].get('file') or ''),
                                                e['id'] or ''))


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
            'posts': sum(1 for e in events if e['type'] in POST_TYPES),
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
        failed = any((p['status'] or '').upper() in FAILED_STATUS for p in owned)
        out.append({
            **person,
            'live': bool(person['proc'] and run['open'] and running) or (
                person['proc'] and run['live']),
            'running': bool(running and run['open']), 'failed': failed,
            'events': events, 'calls': len(calls),
            'posts': sum(1 for e in events if e['type'] in POST_TYPES),
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
        "SUM(type IN ('peer_message','board_post')) msgs, "
        "SUM(type IN ('error','gate_fail')) fails, "
        "COUNT(DISTINCT CASE WHEN type IN ('peer_message','board_post') THEN "
        "COALESCE(NULLIF(json_extract(payload_json,'$.thread'),'main'),'primary') END) boards, "
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
            'tokens': row['total_tokens'] or 0, 'clock': clock(row['started_at'], row['ended_at']),
            'budget': budget(row['total_cost'] or 0.0, limits, calls, people, cap_usd,
                             spec.get('cost_available') is not False,
                             row['total_tokens'] or 0, int(spec.get('budget_tokens') or 0)),
            'ticks': [],
        })
    # One tick strip per row, from the events that mean something at a glance.
    # A single query for every listed run; thin() keeps the markup flat.
    ids = [r['adw_id'] for r in out]
    marks = {}
    if ids:
        for r in con.execute(
                f"SELECT adw_id, type, started_at FROM events WHERE type IN ({','.join('?' * len(TICK_TYPES))}) "
                f"AND adw_id IN ({','.join('?' * len(ids))}) ORDER BY started_at", TICK_TYPES + tuple(ids)):
            marks.setdefault(r['adw_id'], []).append(r)
    for row in out:
        start = parse_time(row['started_at'])
        end = parse_time(row['ended_at']) or parse_time(row['last_at']) or now()
        row['ticks'] = [{'pct': _pct(parse_time(m['started_at']), start, end), 'type': m['type']}
                        for m in marks.get(row['adw_id'], [])]
    return out


# ── rendering ───────────────────────────────────────────────────────────────

def bar(fill, tint=ACCENT, width='180px'):
    return (f'<span class="bar" style="width:{width}"><i style="width:{fill:.1f}%;'
            f'background:{tint}"></i></span>')


def wide_bar(fill, tint=ACCENT):
    return f'<span class="bar wide"><i style="width:{fill:.1f}%;background:{tint}"></i></span>'


def dot(color, live=False):
    cls = 'dot live' if live else 'dot'
    return f'<span class="{cls}" style="background:{color}"></span>'


# A sparkline is a few hundred pixels wide, so past a few hundred ticks the extra
# marks are invisible and only cost bytes on a 2s poll. Bucket them, and let the
# rarest type win each bucket so a single violation is never hidden by 40 tool calls.
TICK_BUCKETS = 240
TICK_RANK = {'error': 0, 'gate_fail': 0, 'gate_pass': 1, 'peer_message': 2, 'board_post': 2,
             'artifact': 3}


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
        return '<b class="live">&#9679; LIVE</b>'
    if state == 'stale':
        return '<b class="stale">&#9673; STALE</b>'
    return '<b class="done">&#9675; DONE</b>'


def nav(active, run_id=None):
    items = [('SWARMS', '/')]
    if run_id:
        suffix = f'?run={quote(run_id)}'
        items += [('OVERVIEW', '/threads' + suffix), ('BOARD', f'/board{suffix}&board=primary'),
                  ('AGENTS', '/agents' + suffix), ('RAW', '/raw' + suffix)]
    return ''.join(
        f'<a class="nav{" on" if name == active else ""}" href="{escape(href)}">{name}</a>'
        for name, href in items)


def header(state, active, run_id=None, meters=True):
    """The strip that rides on every page: nav, then clock, agents, live, model, cost."""
    b = state['budget']
    right = ''
    if meters:
        right = f'''<div class="meters">
    <span class="clk" data-since="{escape(str(state.get('started_at') or ''))}"
          data-live="{1 if state.get('ticking') else 0}">{escape(state['elapsed'])}</span>
    <span>{state['agent_count']} agents</span>
    {state_tag(state['state'])}
    <span class="chip">{escape(str(state['model']))}</span>
    <span>{escape(state['cost'])}</span>
    <span class="d">{short(state['tokens'])} tok | {num(state['calls'])} calls</span>
    {bar(b['pct'], RED if b.get('over') else ACCENT, '120px')}
    <span class="{'warn' if b.get('over') else 'd'}">{escape(b['left'])}</span>
  </div>'''
    ident = f'<span class="runid">{escape(run_id)}</span>' if run_id else ''
    return f'''<header>
  <div class="crumbs"><a class="brand" href="/">swarm workbench</a>{ident}{nav(active, run_id)}</div>
  {right}
</header>'''


HEAD = '''<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;800&display=swap" rel="stylesheet">
'''

STYLE = '''<style>
:root{color-scheme:dark;--bg:#0d1117;--panel:#10161d;--raised:#161b22;--rule:#30363d;
 --ink:#e6edf3;--dim:#8b949e;--faint:#6e7681;--amber:#e3b341;--green:#3fb950;--blue:#58a6ff;
 --red:#f85149;--violet:#a371f7}
*{box-sizing:border-box}
html{background:var(--bg)}
body{margin:0;background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased;
 font:13px/1.5 "JetBrains Mono",ui-monospace,SFMono-Regular,"Cascadia Mono",Menlo,Consolas,monospace}
a{color:inherit;text-decoration:none}
header{position:sticky;top:0;z-index:9;display:flex;flex-wrap:wrap;gap:8px 24px;
 justify-content:space-between;align-items:center;padding:10px 24px;
 background:rgba(13,17,23,.94);backdrop-filter:blur(8px);border-bottom:1px solid var(--rule)}
.crumbs{display:flex;gap:6px;align-items:center}
.brand{color:var(--dim);margin-right:14px;font-weight:600}
.brand:hover{color:var(--ink)}
.runid{color:var(--amber);font-weight:600;margin-right:10px}
.nav{padding:3px 10px;border-radius:6px;color:var(--dim);font-weight:600;font-size:12px;
 letter-spacing:.06em;border:1px solid transparent}
.nav:hover{color:var(--ink)}
.nav.on{color:var(--ink);background:var(--raised);border-color:var(--rule)}
.meters{display:flex;flex-wrap:wrap;gap:14px;align-items:center;color:var(--dim);font-size:12px}
.meters .clk{color:var(--ink);font-weight:600;font-size:13px}
.live{color:var(--green)} .stale{color:var(--amber)} .done{color:var(--dim);font-weight:400}
.chip{border:1px solid var(--rule);border-radius:999px;padding:1px 9px;color:var(--dim);
 background:var(--raised);font-size:12px;white-space:nowrap}
main{padding:20px 24px 84px;max-width:1900px;margin:0 auto}
h1{font-size:22px;font-weight:600;margin:0;letter-spacing:0;display:flex;flex-wrap:wrap;
 gap:10px;align-items:baseline}
h1 small{font-size:13px;font-weight:400;color:var(--dim)}
h1 .sep{color:var(--faint);font-weight:400}
h2{font-size:12px;font-weight:400;color:var(--dim);margin:22px 0 8px;display:flex;gap:10px;
 align-items:baseline;letter-spacing:.06em}
h2 em{font-style:normal;color:var(--ink)}
h2 .note{margin-left:auto;color:var(--faint);letter-spacing:0;text-align:right}
a.note:hover{color:var(--blue)}
.sub{color:var(--dim);margin:6px 0 12px}
.panel{background:var(--panel);border:1px solid var(--rule);border-radius:14px;padding:16px 20px}
.hero .brief{color:var(--ink);margin:8px 0 14px;max-width:1400px}
.stats{display:flex;gap:40px;flex-wrap:wrap;align-items:flex-end;margin:4px 0 16px}
.stat .v{font-size:46px;font-weight:800;line-height:1.05;white-space:nowrap;letter-spacing:-.02em}
.stat .k{font-size:12px;color:var(--dim);margin-top:4px}
.stat.small .v{font-size:26px;font-weight:600;line-height:1.05;padding-bottom:5px}
.bar{display:inline-block;height:6px;background:var(--rule);border-radius:3px;overflow:hidden;
 vertical-align:middle;flex:none}
.bar i{display:block;height:100%;border-radius:3px}
.bar.wide{display:block;width:100%;height:8px;margin:6px 0 8px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;vertical-align:middle;flex:none}
.dot.live{box-shadow:0 0 0 3px rgba(63,185,80,.25);animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{50%{box-shadow:0 0 0 6px rgba(63,185,80,.06)}}
.spark{position:relative;display:block;flex:1;min-width:110px;height:18px;background:var(--bg);
 border:1px solid var(--rule);border-radius:4px;overflow:hidden}
.spark i{position:absolute;top:2px;bottom:2px;width:2px;margin-left:-1px;opacity:.9}
.spark u{position:absolute;left:6px;top:0;color:var(--faint);text-decoration:none;font-size:10px}
.row{display:block;border:1px solid var(--rule);border-left:3px solid var(--rule);
 background:var(--panel);padding:12px 16px;margin-bottom:8px;border-radius:10px}
a.row:hover{background:var(--raised)}
.line{display:flex;flex-wrap:wrap;gap:12px;align-items:center}
.grow{flex:1}
.name{font-weight:600}
.d{color:var(--dim)} .f{color:var(--faint)}
.n b{color:var(--ink);font-weight:600}
.tag{border:1px solid var(--rule);border-radius:6px;padding:0 7px;font-size:11px;font-weight:600;
 letter-spacing:.05em;color:var(--dim);white-space:nowrap;line-height:20px}
.warn,.tag.warn{color:var(--red)} .tag.warn{border-color:rgba(248,81,73,.45)}
.ok,.tag.ok{color:var(--green)} .tag.ok{border-color:rgba(63,185,80,.45)}
.run,.tag.run{color:var(--blue)} .tag.run{border-color:rgba(88,166,255,.45)}
.hold,.tag.hold{color:var(--amber)} .tag.hold{border-color:rgba(227,179,65,.45)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.ag{border:1px solid var(--rule);border-radius:999px;padding:1px 9px;background:var(--raised);
 display:inline-flex;gap:6px;align-items:center;font-size:12px;white-space:nowrap}
.ag b{color:var(--ink)}
.ag.off{opacity:.4}
.ag.at{color:var(--amber);border-color:rgba(227,179,65,.4)}
.tools{display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin:12px 0}
input,button,select{font:inherit;background:var(--bg);color:var(--ink);border:1px solid var(--rule);
 border-radius:6px;padding:4px 10px}
input::placeholder{color:var(--faint)}
input:focus{outline:none;border-color:var(--blue)}
.seg{display:inline-flex;border:1px solid var(--rule);border-radius:6px;overflow:hidden}
.seg a{padding:3px 10px;color:var(--dim);border-right:1px solid var(--rule);font-size:12px}
.seg a:last-child{border-right:0}
.seg a.on{color:var(--ink);background:var(--raised)}
.cols{display:grid;grid-template-columns:minmax(0,1fr) minmax(380px,36%);gap:20px;
 align-items:start}
@media(max-width:1100px){.cols{grid-template-columns:1fr}}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.tile{display:block;border:2px solid var(--rule);border-radius:8px;padding:5px 10px;
 color:var(--dim);white-space:nowrap;overflow:hidden;background:var(--panel)}
.tile b{display:block;font-weight:600;overflow:hidden;text-overflow:ellipsis}
.tile small{display:block;font-size:11px;color:var(--faint);overflow:hidden;text-overflow:ellipsis}
.tile:hover{background:var(--raised)}
.tile.run{border-color:var(--blue)} .tile.run b{color:var(--ink)}
.tile.ok{border-color:var(--green)} .tile.ok b{color:#57c266}
.tile.fail{border-color:var(--red)} .tile.fail b{color:#f97069}
.mon{font-size:12.5px;line-height:22px;max-height:400px;overflow:auto;color:var(--dim);
 padding:10px 16px}
.mon div{display:flex;gap:14px;white-space:nowrap}
.mon .t{color:var(--faint);flex:none}
.mon .who{color:#57c266;width:150px;flex:none;overflow:hidden;text-overflow:ellipsis}
.mon .tool{color:var(--ink);width:170px;flex:none;overflow:hidden;text-overflow:ellipsis}
.mon .arg{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis}
.mon .ev .who{color:var(--amber)} .mon .ev .tool{color:var(--amber)}
.mon .bad .who,.mon .bad .tool{color:var(--red)}
.mon .good .who,.mon .good .tool{color:var(--green)}
.feed{max-height:calc(100vh - 330px);min-height:360px;overflow:auto;padding-right:2px}
.msg{border:1px solid var(--rule);border-left:3px solid var(--rule);background:var(--panel);
 padding:10px 14px;margin-bottom:8px;border-radius:10px}
.msg pre{margin:6px 0 0;color:var(--ink)}
.msg pre.d{color:var(--dim)}
.msg .kv{margin-top:8px}
.msg .kv b{color:var(--dim);font-weight:400;letter-spacing:.08em;font-size:11px;margin-right:6px}
details summary{cursor:pointer;color:var(--dim);margin-top:6px}
details pre{border-left:2px solid var(--rule);padding-left:8px;margin-top:6px;max-height:340px;
 overflow:auto}
table{width:100%;border-collapse:collapse}
td{padding:4px 10px 4px 0;vertical-align:top;border-bottom:1px solid rgba(48,54,61,.5)}
td.t{color:var(--faint);white-space:nowrap;width:76px}
td.k{white-space:nowrap;width:160px}
td.w{white-space:nowrap;width:130px;color:#57c266}
td.ms{text-align:right;color:var(--faint);white-space:nowrap;width:70px}
td pre{color:var(--dim)}
footer{position:fixed;left:0;right:0;bottom:0;background:rgba(13,17,23,.94);
 backdrop-filter:blur(8px);border-top:1px solid var(--rule);padding:8px 24px;display:flex;
 gap:16px;align-items:center;font-size:12px;color:var(--dim)}
footer .spark{height:22px}
footer a{color:var(--blue)}
.empty{color:var(--dim);padding:18px 0}
pre{white-space:pre-wrap;word-break:break-word;margin:0;font:inherit}
.legend{display:flex;gap:12px;flex-wrap:wrap;color:var(--dim);font-size:11px;margin-top:10px}
</style>'''

SCRIPT = '''<script>
function wire(){
  var q=document.getElementById('q');
  if(q){
    var apply=function(){
      var v=q.value.toLowerCase(), n=0;
      document.querySelectorAll('[data-hay]').forEach(function(el){
        var hit=el.dataset.hay.indexOf(v)>=0;
        el.style.display=hit?'':'none'; if(hit)n++;
      });
      var c=document.getElementById('qn'); if(c)c.textContent=v?n+' match':'';
    };
    q.oninput=apply; apply();
  }
  document.querySelectorAll('.clk[data-live="1"]').forEach(function(el){
    var t0=Date.parse(el.dataset.since); if(isNaN(t0))return;
    el.textContent=fmt((Date.now()-t0)/1000);
  });
}
function fmt(s){s=Math.max(0,Math.floor(s));
  var m=('0'+((s%3600/60)|0)).slice(-2), c=('0'+(s%60)).slice(-2);
  return s>=3600?(s/3600|0)+':'+m+':'+c:m+':'+c;}
function pinned(){var out=[];document.querySelectorAll('.tail').forEach(function(el,i){
  if(el.scrollHeight-el.scrollTop-el.clientHeight<14)out.push(i)});return out;}
function tail(keep){document.querySelectorAll('.tail').forEach(function(el,i){
  if(!keep||keep.indexOf(i)>=0)el.scrollTop=el.scrollHeight});}
document.addEventListener('keydown',function(e){
  var q=document.getElementById('q');
  if(e.key==='/'&&q&&document.activeElement!==q){e.preventDefault();q.focus();q.select();}
  if(e.key==='Escape'&&q&&document.activeElement===q){q.value='';q.blur();wire();}
});
setInterval(wire,1000);
if(POLL){setInterval(function(){
  fetch(location.href,{headers:{'x-partial':'1'}}).then(function(r){return r.text()})
   .then(function(t){
     var q=document.getElementById('q'), keep=q?q.value:null, focus=document.activeElement===q;
     var y=window.scrollY, open=[], stuck=pinned();
     document.querySelectorAll('details').forEach(function(d,i){if(d.open)open.push(i)});
     document.getElementById('app').innerHTML=t;
     document.querySelectorAll('details').forEach(function(d,i){
       if(open.indexOf(i)>=0)d.open=true;});
     var n=document.getElementById('q');
     if(n&&keep!==null){n.value=keep; if(focus)n.focus();}
     window.scrollTo(0,y); wire(); tail(stuck);
   }).catch(function(){});
},2000)}
wire(); tail();
</script>'''


def page(title, body, poll):
    return (HEAD + f'<title>{escape(title)}</title>' + STYLE + f'<div id="app">{body}</div>'
            + SCRIPT.replace('POLL', '1' if poll else '0'))


def search_box(width='260px'):
    return (f'<input id="q" placeholder="find a signal&hellip;  ( / )" '
            f'style="min-width:{width}"><span id="qn" class="d"></span>')


def status_class(status):
    if status in ('SUCCESS', 'PASSED', 'CLOSED'):
        return 'ok'
    if status in FAILED_STATUS:
        return 'warn'
    if status in ACTIVE_STATUS:
        return 'run'
    if status == 'STALE':
        return 'hold'
    return 'd'


def agent_chip(run_id, chip):
    return (f'<a class="ag{" off" if chip["dormant"] else ""}" '
            f'href="/agent/{quote(chip["agent"])}?run={quote(run_id)}">'
            f'{dot(chip["color"])}{"?" if chip["dormant"] else ""}'
            f'{escape(chip["agent"])} <b>{chip["count"]}</b></a>')


def mention_chips(run_id, names):
    """One amber chip per tagged agent; @all is the room, so it links nowhere."""
    return ''.join(
        f'<span class="ag at">@all</span>' if m == 'all' else
        f'<a class="ag at" href="/agent/{quote(m)}?run={quote(run_id)}">@{escape(m)}</a>'
        for m in names)


def legend(types=('tool_call', 'peer_message', 'board_post', 'gate_pass', 'gate_fail', 'error',
                  'artifact')):
    return '<div class="legend">' + ''.join(
        f'{dot(TYPE_COLOR[t])} {t}&nbsp;&nbsp;' for t in types) + '</div>'


def stat(value, label, tone=None, small=False, cls='', attrs=''):
    style = f' style="color:{tone}"' if tone else ''
    return (f'<div class="stat{" small" if small else ""}">'
            f'<div class="v{" " + cls if cls else ""}"{style}{attrs}>'
            f'{value}</div><div class="k">{label}</div></div>')


def render_swarms(con, limit, cap_usd):
    rows = swarm_list(con, limit, cap_usd)
    live = sum(1 for r in rows if r['live'])
    priced = [r for r in rows if r['budget']['kind'] == 'cost']
    spent = sum(r['budget']['used'] for r in priced)
    newest = rows[0] if rows else None
    state = {'state': 'live' if live else 'done',
             'ticking': bool(newest and newest['live']),
             'elapsed': newest['clock'] if newest else '--',
             'started_at': newest['started_at'] if newest else '',
             'agent_count': sum(r['agents'] for r in rows),
             'model': newest['model'] if newest else '?',
             'cost': f'${spent:.4f} COST' if priced else 'COST n/a',
             'tokens': sum(r['tokens'] for r in rows), 'calls': sum(r['calls'] for r in rows),
             'budget': {'pct': 0.0, 'left': f'{len(rows)} runs on record'}}
    out = [header(state, 'SWARMS'), '<main>',
           f'<h1>swarms <small>{len(rows)} on record &middot; {live} live</small></h1>',
           f'<div class="tools">{search_box("300px")}</div>']
    if not rows:
        out.append('<p class="empty">no runs recorded yet</p>')
    for row in rows:
        b = row['budget']
        tone = GREEN if row['live'] else (AMBER if row['open'] else DIM)
        edge = {'ok': GREEN, 'warn': RED, 'run': BLUE, 'hold': AMBER}.get(
            status_class(row['status'] if not row['live'] else 'RUNNING'), RULE)
        hay = ' '.join([row['name'], row['adw_id'], row['status'], row['request'],
                        row['model'], row['state']]).lower()
        thread_word = 'thread' if row['threads'] == 1 else 'threads'
        spent = f'${b["used"]:.2f}' if b['kind'] == 'cost' else 'n/a'
        fails = f' <span class="warn">&middot; {row["fails"]} fail</span>' if row['fails'] else ''
        out.append(f'''<a class="row" data-hay="{escape(hay)}"
 href="/threads?run={quote(row['adw_id'])}" style="border-left-color:{edge}">
 <div class="line">
  {dot(tone, row['live'])}
  <span class="tag {status_class(row['status'])}">{escape(row['status'])}</span>
  <span class="name">{escape(row['adw_id'])}</span>
  <span class="d">{escape(row['name'])} &middot; {escape(day(row['started_at']))}</span>
  <span class="grow"></span>
  <span class="chip">{escape(str(row['model']))}</span>
  <span class="clk" data-since="{escape(str(row['started_at'] or ''))}"
        data-live="{1 if row['live'] else 0}"><b>{escape(row['clock'])}</b></span>
 </div>
 <div style="margin-top:6px">{escape(clip(row['request'], 170))}</div>
 <div class="line d" style="margin-top:8px">
  <span class="n"><b>{row['agents']}</b> agents &middot; <b>{row['threads']}</b> {thread_word}
   &middot; <b>{row['msgs']}</b> posts &middot; <b>{row['phases']}</b> phases
   &middot; <b>{num(row['calls'])}</b> calls
   &middot; <b title="{num(row['tokens'])} tokens">{short(row['tokens'])}</b> tok
   &middot; <b>{spent}</b> cost{fails}</span>
  {ticks(row['ticks'], 18, f"{row['events']} events")}
  {bar(b['pct'], RED if b.get('over') else edge, '150px')}
  <span class="{'warn' if b.get('over') else ''}">{escape(b['label'])}</span>
 </div>
</a>''')
    out.append('</main>')
    return ''.join(out), bool(live)


def scrubber(run):
    start, end = run['span']
    return (f'<footer><span>{stamp(start.isoformat() if start else None)}</span>'
            + ticks(run['events'], 22, f'{run["event_count"]} events')
            + f'<span>{stamp(end.isoformat() if end else None)}</span>'
            f'<span>{run["msgs"]} posts | {num(run["calls"])} tool calls</span>'
            f'<a href="/raw?run={quote(run["adw_id"])}">raw trace &rarr;</a></footer>')


def head_state(run):
    return {'state': run['state'], 'ticking': run['live'], 'elapsed': run['elapsed'],
            'started_at': run['started_at'], 'agent_count': len(run['agents']),
            'model': run['model'], 'cost': run['budget']['meter'], 'tokens': run['tokens'],
            'calls': run['calls'], 'budget': run['budget']}


def crumb(run, tail, extra=''):
    return (f'<h1><a href="/">swarms</a><span class="sep">/</span>'
            f'<a href="/threads?run={quote(run["adw_id"])}">{escape(run["adw_id"])}</a>'
            f'<span class="sep">/</span>{escape(tail)}{extra}</h1>')


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


def run_tone(run):
    """The colour of the run's big clock: blue while it runs, then the verdict."""
    if run['state'] == 'live':
        return BLUE
    if run['state'] == 'stale':
        return AMBER
    return RED if run['status'].upper() in FAILED_STATUS else GREEN


def hero(run):
    """The block people screenshot: id, brief, verdict, and the big counters."""
    b = run['budget']
    tone = run_tone(run)
    status = run['status'].upper()
    priced = b['kind'] == 'cost'
    done = sum(1 for p in run['phases'] if (p['status'] or '').upper() == 'SUCCESS')
    clk = (f' data-since="{escape(str(run["started_at"] or ""))}" '
           f'data-live="{1 if run["live"] else 0}"')
    about = (f'{escape(run["name"].lower())} &middot; {escape(day(run["started_at"]))} &middot; '
             f'{len(run["agents"])} agents &middot; {escape(str(run["model"]))}')
    return f'''<section class="hero panel">
 <div class="line">
  <h1>{escape(run['adw_id'])} <small>{about}</small></h1>
  <span class="grow"></span>
  <span class="tag {status_class(status)}">{escape(status)}</span>{state_tag(run['state'])}
 </div>
 <p class="brief">{escape(clip(run['request'], 260))}</p>
 <div class="stats">
  {stat(escape(run['elapsed']), 'elapsed', tone, cls='clk', attrs=clk)}
  {stat(f'${run["cost"]:.2f}' if priced else 'n/a', 'spent', None if priced else FAINT)}
  {stat(short(run['tokens']), 'tokens')}
  {stat(num(run['calls']), 'tool calls', small=True)}
  {stat(run['msgs'], 'posts', small=True)}
  {stat(f'{done}/{len(run["phases"])}', 'phases', small=True)}
  {stat(run['fails'], 'failures', RED if run['fails'] else None, small=True)}
 </div>
 {wide_bar(b['pct'], RED if b.get('over') else tone)}
 <div class="line d">
  <span class="{'warn' if b.get('over') else ''}">{escape(b['label'])}</span>
  <span class="grow"></span>
  <span>{escape(clip(run['definition_of_done'], 140))}</span>
 </div>
</section>'''


def tile_state(person):
    if person['live'] or person['running']:
        return 'run'
    if person['failures'] or person['failed']:
        return 'fail'
    if person['calls'] or person['posts']:
        return 'ok'
    return ''


def tile(run_id, person):
    facts = [f'{num(person["calls"])} calls', f'{person["posts"]} posts']
    if person['tokens_recorded']:
        facts.append(f'{short(person["tokens"])} tok')
    return (f'<a class="tile {tile_state(person)}" data-hay="{escape(person["agent"].lower())}" '
            f'href="/agent/{quote(person["agent"])}?run={quote(run_id)}" '
            f'title="{escape(person["model"] or "")}">'
            f'<b>{escape(person["agent"])}</b><small>{" &middot; ".join(facts)}</small></a>')


MONITOR_TAIL = 40
FEED_TAIL = 30
MONITOR_TYPES = ('tool_call',) + FAIL_TYPES + ('gate_pass', 'artifact')


def inside_run(text, run_id):
    """A path relative to the run's session folder: the absolute prefix is the
    same on every line and says nothing, board/sky--plan.md says everything."""
    text = str(text)
    for sep in ('\\', '/'):
        head = f'{sep}{run_id}{sep}'
        cut = text.find(head)
        if cut < 0:
            continue
        # The prefix starts at the last whitespace or quote before the run id, so
        # a command line keeps its verb and only loses the folder walk.
        start = max(text.rfind(c, 0, cut) + 1 for c in ' "\'')
        return text.replace(text[start:cut + len(head)], '').replace('\\', '/')
    return text


def monitor_line(event, run_id):
    kind = event['type']
    if kind == 'tool_call':
        cls, who, tool = '', event['agent'] or 'system', event['name'] or 'tool'
        arg = inside_run(first_param(event['payload'])[1], run_id)
    else:
        cls = 'ev bad' if kind in FAIL_TYPES else 'ev good'
        who, tool, arg = kind, event['name'], event['text']
    return (f'<div class="{cls}" data-hay="{escape((who + " " + tool + " " + arg).lower())}">'
            f'<span class="t">{stamp(event["time"])}</span><span class="who">{escape(who)}</span>'
            f'<span class="tool">{escape(clip(tool, 24))}</span>'
            f'<span class="arg">{escape(clip(arg, 180))}</span></div>')


def monitor(run, tail=MONITOR_TAIL):
    rows = [e for e in run['events'] if e['type'] in MONITOR_TYPES][-tail:]
    body = (''.join(monitor_line(e, run['adw_id']) for e in rows)
            or '<div class="d">no tool calls yet</div>')
    return f'<div class="panel mon tail">{body}</div>'


def post_block(run, event, full=False):
    """One board_post: who wrote it, which file, whom it tags, and the text."""
    payload = event['payload']
    color = run['colors'].get(event['agent']) or stable_color(event['agent'])
    text = str(payload.get('text') or event['text'])
    file = str(payload.get('file') or '')
    hay = ' '.join([event['agent'], file] + event['mentions'] + [clip(text, 400)]).lower()
    body = escape(text) if full else escape(clip(text, 320))
    return f'''<div class="msg" data-hay="{escape(hay)}" style="border-left-color:{color}">
 <div class="line">
  {dot(color)}
  <a class="name" href="/agent/{quote(event['agent'])}?run={quote(run['adw_id'])}">
   {escape(event['agent'] or 'system')}</a>
  {f'<span class="chip">{escape(file)}</span>' if file else ''}
  {mention_chips(run['adw_id'], event['mentions'])}
  <span class="grow"></span>
  <span class="d">{escape(stamp(event['time']))}</span>
 </div>
 <pre{'' if full else ' class="d"'}>{body}</pre>
</div>'''


def message_block(run, event, full=True):
    """One peer_message, rendered whole: the typed proposal an agent files per round."""
    payload = event['payload']
    color = run['colors'].get(event['agent']) or stable_color(event['agent'])
    status = str(payload.get('status') or '').upper()
    summary = str(payload.get('summary') or event['text'])
    parts = [f'''<div class="msg" data-hay="{escape((event['agent'] + ' ' + event['text']).lower())}"
 style="border-left-color:{color}">
 <div class="line">
  {dot(color)}
  <a class="name" href="/agent/{quote(event['agent'])}?run={quote(run['adw_id'])}">
   {escape(event['agent'] or 'system')}</a>
  <span class="tag">ROUND {escape(str(payload.get('round', '?')))}</span>
  {f'<span class="tag {status_class(status)}">{escape(status)}</span>' if status else ''}
  <span class="grow"></span>
  <span class="d">{escape(stamp(event['time']))}</span>
 </div>
 <pre{'' if full else ' class="d"'}>{escape(summary if full else clip(summary, 320))}</pre>''']
    if full:
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


def notice_block(event):
    return f'''<div class="msg" data-hay="{escape(event['text'].lower())}"
 style="border-left-color:{RED}">
 <div class="line"><span class="warn name">&#9888; SYSTEM</span>
  <span class="tag warn">{escape(event['type'].upper())}</span>
  <span class="d">{escape(event['agent'] or 'runtime')}</span>
  <span class="grow"></span><span class="d">{escape(stamp(event['time']))}</span></div>
 <pre class="warn">{escape(clip(event['text'], 600))}</pre></div>'''


def conversation(run, stream, full=False):
    """The board as one column: posts, proposals and violations in time order."""
    out = []
    for event in stream:
        if event['type'] == 'peer_message':
            out.append(message_block(run, event, full))
        elif event['type'] == 'board_post':
            out.append(post_block(run, event, full))
        else:
            out.append(notice_block(event))
    return ''.join(out) or '<p class="empty">nothing posted to this board yet</p>'


def phase_row(run_id, phase):
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
    return f'''<a class="row" data-hay="{escape(hay)}"
 href="/phase?run={quote(run_id)}&phase={quote(phase['phase_id'])}"
 style="border-left-color:{phase['color']}">
 <div class="line">
  <span class="f" style="width:22px;text-align:right">{phase['seq']}</span>
  <span class="name">{escape(phase['name'])}</span>
  <span class="tag {status_class(phase['status'])}">{escape(phase['status'])}</span>{retry}
  <span class="d">{dot(phase['color'])} {escape(phase['kind'])}/{escape(phase['owner'])}</span>
  {ticks(phase['ticks'], 18, f"{phase['events']} events")}
  <span class="d">{escape(phase['elapsed'])}</span>
  <span class="n"><b>{num(phase['events'])}</b> <span class="d">events</span></span>
  <span class="n"><b>{num(phase['calls'])}</b> <span class="d">calls</span></span>
  <span class="n"><b>{phase['posts']}</b> <span class="d">posts</span></span>
 </div>{err}{note}
</a>'''


def render_threads(con, run_id, cap_usd, order='activity', show='all'):
    """The run overview: hero, agent tiles, monitor stream, the board, then the phases."""
    run = load(con, run_id, cap_usd)
    if not run:
        return unknown('SWARM', run_id)
    boards = thread_rows(run)
    phases = phase_rows(run)
    people = agent_rows(run)
    if show == 'active':
        phases = [p for p in phases if p['active']]
    elif show == 'dormant':
        phases = [p for p in phases if not p['active']]
    keys = {'activity': lambda p: (p['ticks'][-1]['pct'] if p['ticks'] else -1),
            'created': lambda p: -p['seq'], 'volume': lambda p: p['events'],
            'members': lambda p: len(p['members'])}
    phases.sort(key=keys.get(order, keys['activity']), reverse=True)
    base = f'/threads?run={quote(run_id)}'
    board = next((b for b in boards if b['key'] == 'primary'), boards[0])
    stream = board_stream(run, board['key'], run['boards'][board['key']])
    total_posts = sum(b['messages'] for b in boards)
    board_word = 'board' if len(boards) == 1 else 'boards'
    states = [tile_state(p) for p in people]
    roll = (f'{states.count("run")} running &middot; {states.count("ok")} done &middot; '
            f'{states.count("fail")} failed &middot; {states.count("")} idle')
    shown = min(MONITOR_TAIL, sum(1 for e in run['events'] if e['type'] in MONITOR_TYPES))

    out = [header(head_state(run), 'OVERVIEW', run_id, meters=False), '<main>', hero(run),
           f'<div class="tools">{search_box()}'
           + segment('ORDER', SORTS, order, f'{base}&show={show}&order=')
           + segment('SHOW', SHOWS, show, f'{base}&order={order}&show=') + '</div>',
           '<div class="cols"><div>',
           f'<h2>agents <em>{len(people)}</em><span class="note">{roll}</span></h2>',
           f'<div class="tiles">{"".join(tile(run_id, p) for p in people)}</div>'
           if people else '<p class="empty">no agents registered on this swarm</p>',
           f'<h2>monitor <em>last {shown} of {num(run["calls"])} tool calls</em>'
           f'<a class="note" href="/raw?run={quote(run_id)}&tab=tools">full stream &rarr;</a></h2>',
           monitor(run),
           '</div><div>',
           f'<h2>board <em>{len(boards)} {board_word} &middot; {total_posts} posts &middot; '
           f'{run["rounds"]} rounds</em><span class="note">{board["members"]}/'
           f'{len(board["chips"])} posted &middot; '
           f'<a href="/board?run={quote(run_id)}&board={quote(board["key"])}">open &rarr;</a>'
           '</span></h2>',
           f'<div class="feed tail">{conversation(run, stream[-FEED_TAIL:])}</div>',
           '</div></div>',
           f'<h2>phases <em>{len(phases)} of {len(run["phases"])} steps</em>'
           '<span class="note">execution steps, not threads</span></h2>']
    if not phases:
        out.append('<p class="empty">no phases match</p>')
    out += [phase_row(run_id, p) for p in phases]
    out += [legend(), '</main>', scrubber(run)]
    return ''.join(out), run['live']


def render_board(con, run_id, board_key, cap_usd):
    run = load(con, run_id, cap_usd)
    if not run:
        return unknown('SWARM', run_id)
    msgs = run['boards'].get(board_key)
    if msgs is None:
        return unknown('BOARD', board_key)
    board = next(b for b in thread_rows(run) if b['key'] == board_key)
    stream = board_stream(run, board_key, msgs)
    out = [header(head_state(run), 'BOARD', run_id), '<main>',
           crumb(run, board['name'].lower()),
           f'<p class="sub">{board["messages"]} posts &middot; {board["rounds"]} rounds '
           f'&middot; {board["members"]} of {len(board["chips"])} agents posted '
           f'&middot; {escape(stamp(board["first"]))}-{escape(stamp(board["last"]))}</p>',
           f'<div class="tools">{search_box()}</div>',
           f'<div class="chips">{"".join(agent_chip(run_id, c) for c in board["chips"])}</div>',
           f'<h2>conversation <em>{len(stream)} entries</em>'
           '<span class="note">posts, round proposals and violations, oldest first</span></h2>',
           conversation(run, stream, full=True),
           '</main>', scrubber(run)]
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


def event_table(events, tab='all', with_agent=False, run_id=''):
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
        # A tool name reads in ink, like the monitor stream; red and green are
        # kept for what failed and what passed, so event types keep their colour.
        if event['type'] == 'tool_call':
            label, color = event['name'] or 'tool', INK
        else:
            label, color = event['type'], TYPE_COLOR.get(event['type'], DIM)
        text = inside_run(event['text'], run_id) if run_id else event['text']
        who = (f'<td class="w">{escape(clip(event["agent"], 16))}</td>' if with_agent else '')
        body.append(f'<tr data-hay="{escape((label + " " + event["agent"] + " " + text).lower())}">'
                    f'<td class="t">{stamp(event["time"])}</td>'
                    f'<td class="k" style="color:{color}">{escape(clip(label, 22))}</td>{who}'
                    f'<td><pre>{escape(clip(text, 220))}</pre></td>'
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
    tone = {'ok': GREEN, 'warn': RED, 'run': BLUE}.get(status_class(phase['status']), DIM)
    out = [header(head_state(run), 'OVERVIEW', run_id), '<main>',
           crumb(run, 'phases', f'<span class="sep">/</span>{escape(phase["name"])}'
                 f'<span class="tag {status_class(phase["status"])}">{escape(phase["status"])}</span>'),
           f'<p class="sub">step {phase["seq"]} of {len(run["phases"])} &middot; '
           f'{escape(phase["kind"])}/{escape(phase["owner"])} &middot; attempt '
           f'{phase["attempt"]} ({phase["retries"]} retries) &middot; '
           f'{escape(stamp(phase["started_at"]))}-{escape(stamp(phase["ended_at"]))}</p>',
           '<section class="panel"><div class="stats">',
           stat(escape(phase['elapsed']), 'elapsed', tone),
           stat(num(phase['events']), 'events', small=True),
           stat(num(phase['calls']), 'tool calls', small=True),
           stat(phase['posts'], 'board posts', small=True),
           stat(phase['fails'], 'failures', RED if phase['fails'] else None, small=True),
           '</div>',
           f'<div class="line">{ticks(phase["ticks"], 18, "phase events across the run span")}</div>',
           '</section>']
    if phase['error']:
        out.append(f'<div class="msg warn" style="border-left-color:{RED};margin-top:12px">'
                   f'<pre class="warn">{escape(phase["error"])}</pre></div>')
    if members:
        out += ['<h2>agents in this phase</h2>', f'<div class="chips">{members}</div>']
    out += ['<h2>events</h2>',
            f'<div class="tools">{search_box()}{tab_bar(base, tab)}</div>',
            event_table(events, tab, with_agent=True, run_id=run_id), '</main>', scrubber(run)]
    return ''.join(out), run['live']


def render_agents(con, run_id, cap_usd):
    run = load(con, run_id, cap_usd)
    if not run:
        return unknown('SWARM', run_id)
    people = agent_rows(run)
    busiest = max([p['calls'] for p in people] or [0])
    out = [header(head_state(run), 'AGENTS', run_id), '<main>',
           crumb(run, 'agents'),
           f'<p class="sub">{len(people)} agents &middot; '
           f'{sum(1 for p in people if p["live"])} with a live process &middot; '
           f'{sum(p["posts"] for p in people)} board posts</p>',
           f'<div class="tools">{search_box()}</div>']
    if not people:
        out.append('<p class="empty">no agents registered on this swarm</p>')
    for person in people:
        hay = ' '.join([person['agent'], person['model'], person['coding_agent']]).lower()
        state = tile_state(person)
        label = {'run': 'RUNNING', 'ok': 'DONE', 'fail': 'FAILED'}.get(state, 'IDLE')
        tone = {'run': 'run', 'ok': 'ok', 'fail': 'warn'}.get(state, 'd')
        out.append(f'''<a class="row" data-hay="{escape(hay)}"
 href="/agent/{quote(person['agent'])}?run={quote(run_id)}"
 style="border-left-color:{person['color']}">
 <div class="line">
  {dot(person['color'], person['live'])}
  <span class="name">{escape(person['agent'])}</span>
  <span class="tag {tone}">{label}</span>
  <span class="f">agent-{person['index']}</span>
  <span class="chip">{escape(person['model'] or '?')}</span>
  {ticks(person['ticks'], 18, f"{len(person['events'])} events")}
  <span class="d">{escape(stamp(person['first']))}-{escape(stamp(person['last']))}</span>
 </div>
 <div class="line d" style="margin-top:6px">
  <span class="n"><b>{num(person['calls'])}</b> calls &middot; <b>{person['posts']}</b> posts
   &middot; <b>{len(person['phases'])}</b> phases &middot;
   <span class="{'warn' if person['failures'] else ''}"><b>{person['failures']}</b> failures</span>
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
        f'<div class="line" style="margin:3px 0">'
        + (f'<span class="chip">{escape(str(e["payload"].get("file")))}</span>'
           if e['type'] == 'board_post' else
           f'<span class="tag">ROUND {escape(str(e["payload"].get("round", "?")))}</span>')
        + f'{mention_chips(run_id, e["mentions"])}'
        f'<span class="f">{escape(stamp(e["time"]))}</span>'
        f'<span class="grow d">{escape(clip(first_line(e["payload"].get("text")) or e["text"], 150))}'
        '</span></div>'
        for e in view['events'] if e['type'] in POST_TYPES)
    phases = ''.join(
        f'<a class="line" style="margin:3px 0" '
        f'href="/phase?run={quote(run_id)}&phase={quote(p["phase_id"])}">'
        f'<span class="name">{escape((p["name"] or "").upper())}</span>'
        f'<span class="tag {status_class((p["status"] or "?").upper())}">'
        f'{escape((p["status"] or "?").upper())}</span>'
        f'<span class="d">{len(run["by_phase"].get(p["phase_id"], []))} events &middot; '
        f'{escape(elapsed(p["started_at"], p["ended_at"]))}</span></a>'
        for p in view['phases'])
    avg = f'{view["ms"] // view["timed"]}ms' if view['timed'] else 'n/a'
    state = tile_state(view)
    tone = {'run': BLUE, 'ok': GREEN, 'fail': RED}.get(state, DIM)
    label = {'run': 'RUNNING', 'ok': 'DONE', 'fail': 'FAILED'}.get(state, 'IDLE')
    base = f'/agent/{quote(agent)}?run={quote(run_id)}&tab='
    out = [header(head_state(run), 'AGENTS', run_id), '<main>',
           f'<h1>{dot(view["color"], view["live"])} {escape(agent)} <small>@swarm.org &middot; '
           f'agent-{view["index"]} &middot; {escape(view["coding_agent"] or "?")} '
           f'{escape(view["model"] or "?")} &middot; '
           f'{"live process" if view["live"] else "no live process"} &middot; '
           f'active {escape(stamp(view["first"]))}-{escape(stamp(view["last"]))}</small>'
           f'<span class="tag {status_class(label)}">{label}</span></h1>',
           '<section class="panel" style="margin-top:14px"><div class="stats">',
           stat(num(view['calls']), 'tool calls', tone),
           stat(view['posts'], 'posts'),
           stat(short(view['tokens']) if view['tokens_recorded'] else 'n/a', 'tokens',
                None if view['tokens_recorded'] else FAINT),
           stat(view['failures'], 'failures', RED if view['failures'] else None, small=True),
           stat(avg, 'avg call', small=True),
           stat(f'{(100 * view["calls"] // run["calls"]) if run["calls"] else 0}%', 'of swarm calls',
                small=True),
           '</div>',
           f'<div class="line d"><span>{token_line}</span><span class="grow"></span>'
           f'<span>{escape(run["budget"]["label"])}</span></div>',
           '</section>',
           '<h2>context window</h2>',
           f'<div class="line">{bar(100 * used / window if window else 0)} '
           f'<span class="d">{escape(ctx)}</span></div>',
           '<h2>share of the swarm</h2>',
           f'<div class="line">{bar(100.0 * view["calls"] / run["calls"] if run["calls"] else 0, view["color"])} '
           f'<span class="d">{num(view["calls"])} of {num(run["calls"])} tool calls</span></div>',
           f'<h2>board <em>{view["posts"]} posts to the '
           f'<a href="/board?run={quote(run_id)}&board=primary" style="color:{ACCENT}">'
           f'primary board</a></em></h2>',
           posted or '<p class="empty">this agent has posted nothing to the board</p>',
           f'<h2>phases <em>{len(view["phases"])} owned</em></h2>',
           phases or '<p class="empty">owns no phase on this swarm</p>',
           '<h2>events</h2>',
           f'<div class="tools">{search_box("240px")}{tab_bar(base, tab)}</div>',
           event_table(view['events'], tab, run_id=run_id), '</main>', scrubber(run)]
    return ''.join(out), run['live']


def render_raw(con, run_id, cap_usd, tab='all'):
    run = load(con, run_id, cap_usd)
    if not run:
        return unknown('SWARM', run_id)
    base = f'/raw?run={quote(run_id)}&tab='
    out = [header(head_state(run), 'RAW', run_id), '<main>',
           crumb(run, 'raw trace'),
           f'<p class="sub">{num(run["event_count"])} events &middot; '
           f'{escape(day(run["started_at"]))} &rarr; {escape(day(run["last_at"]))} '
           f'({escape(ago(run["last_at"]))})</p>',
           f'<div class="tools">{search_box("300px")}{tab_bar(base, tab)}</div>',
           event_table(run['events'], tab, with_agent=True, run_id=run_id),
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
        # A board post on the "main" thread with no mentions list: the tags come
        # from its text, and it lands on the primary board with the proposals.
        # Traced at t1 but written half a minute earlier: posted_at orders it.
        ('e5', 'p1', 'board_post', 'stitch',
         {'agent': 'stitch', 'thread': 'main', 'file': 'stitch--note-1.md',
          'posted_at': '2026-09-10T12:03:30+00:00',
          'text': '@cynic @judge the empty case is still open, see @cynic above'}, t1),
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
        assert listed[0]['calls'] == 2 and listed[0]['msgs'] == 2, listed
        # The correction this rewrite exists for: 3 phases, 1 board, never 3 threads.
        # The board post's "main" thread is that same primary board, not a second one.
        assert listed[0]['phases'] == 3 and listed[0]['threads'] == 1, listed[0]
        assert listed[0]['agents'] == 2, listed[0]
        assert listed[0]['budget']['kind'] == 'calls', listed[0]['budget']
        assert listed[0]['budget']['cap'] == 28, listed[0]['budget']  # 7 calls x 2 agents x 2 rounds
        assert len(listed[0]['ticks']) == 5 and listed[0]['clock'].count(':') >= 1, listed[0]
        assert run['model'] == 'gemini-3.8-flash-medium' and len(run['agents']) == 2, run['model']
        assert run['rounds'] == 1 and run['msgs'] == 2 and run['fails'] == 1, run
        assert len(boards) == 1 and boards[0]['messages'] == 2, boards
        assert boards[0]['members'] == 1 and len(boards[0]['chips']) == 2, boards[0]
        assert [c['dormant'] for c in boards[0]['chips']] == [False, True], boards[0]['chips']
        assert len({c['color'] for c in boards[0]['chips']}) == 2, boards[0]['chips']
        assert [s['name'] for s in steps] == ['STITCH_R1', 'CYNIC_R1', 'ACCEPTANCE'], steps
        assert steps[0]['calls'] == 1 and steps[0]['posts'] == 2, steps[0]
        assert steps[2]['error'] == 'assertion failed' and steps[2]['attempt'] == 2, steps[2]
        assert [s['active'] for s in steps] == [False, True, False], steps
        assert all(0.0 <= t['pct'] <= 100.0 for t in steps[0]['ticks']), steps[0]['ticks']
        stitch = next(p for p in people if p['agent'] == 'stitch')
        cynic = next(p for p in people if p['agent'] == 'cynic')
        assert stitch['calls'] == 1 and stitch['failures'] == 1 and stitch['posts'] == 2, stitch
        assert not stitch['live'] and cynic['live'], (stitch['live'], cynic['live'])
        assert tile_state(cynic) == 'run' and tile_state(stitch) == 'fail', (cynic, stitch)
        assert stitch['context_window'] == 1000000, stitch
        assert stitch['color'] != cynic['color'], people
        assert len(set(PALETTE)) == len(PALETTE) >= 20, 'a 20-agent swarm needs 20 distinct dots'
        # Mentions: the payload's list when it has one, else the @tags in the text, once each.
        post = next(e for e in run['events'] if e['type'] == 'board_post')
        assert post['mentions'] == ['cynic', 'judge'], post['mentions']
        assert mentions({'mentions': ['@sky', 'hills'], 'text': '@x'}) == ['sky', 'hills']
        assert thread_key({'thread': 'main'}) == 'primary' and thread_key({}) == 'primary'
        assert thread_key({'thread': 'side'}) == 'side'
        # Tool arguments read from either runner's payload shape.
        assert first_param({'tool_info': {'parameters': {'cmd': 'ls'}}}) == ('cmd', 'ls')
        assert first_param({'parameters': {'dir_path': '/x'}}) == ('dir_path', '/x')
        # The monitor strips the session folder walk and keeps the command's verb.
        assert inside_run('python "C:\\a\\run1\\board\\x.py"', 'run1') == 'python "board/x.py"'
        assert inside_run('/tmp/run1/board/x.md', 'run1') == 'board/x.md'
        assert inside_run('ls -la', 'run1') == 'ls -la'
        # A declared token budget drives the bar when no cost is priced.
        toks = budget(0, {}, 0, 1, 30, False, 20_000_000, 30_000_000)
        assert toks['kind'] == 'tokens' and 66 < toks['pct'] < 67 and not toks['over'], toks
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
        assert '271.3k' in swarms and '271,318 tokens' in swarms, swarms[:400]
        assert 'gemini-3.8-flash-medium' in swarms, swarms[:400]
        assert '<b>1</b> thread' in swarms and '<b>3</b> phases' in swarms, swarms
        assert 'href="/threads?run=run1"' in swarms, swarms[:400]
        assert 'LIVE' in swarms and 'class="spark"' in swarms, swarms[:400]
        # The overview: hero counters, one tile per agent, the monitor, the board
        # as one column, then the phases under their own heading.
        assert 'class="hero panel"' in threads and '>elapsed<' in threads, threads[:1200]
        assert '>tokens<' in threads and '>tool calls<' in threads, threads[:2000]
        assert threads.count('class="tile ') == 2, 'one tile per agent'
        assert '<a class="tile run"' in threads and '<a class="tile fail"' in threads, threads
        assert '<b>stitch</b>' in threads and '<b>cynic</b>' in threads, threads
        assert 'class="panel mon tail"' in threads and 'solution.py' in threads, threads
        assert '<span class="tool">view_file</span>' in threads, threads
        assert 'board <em>1 board' in threads and '1/2 posted' in threads, threads
        assert threads.count('class="feed tail"') == 1, 'the board is one column'
        assert 'stitch--note-1.md' in threads and '>@cynic</a>' in threads, threads
        assert '>@judge</a>' in threads and threads.count('>@cynic</a>') == 1, threads
        assert 'phases <em>3 of 3 steps' in threads, threads
        assert 'STITCH_R1' in threads and 'ACCEPTANCE' in threads, threads
        assert 'execution steps, not threads' in threads, threads
        assert 'find a signal' in threads and 'ORDER' in threads and 'SHOW' in threads, threads
        assert 'CLAIM VIOLATION' in threads and 'warn' in threads, threads
        assert 'assertion failed' in threads, threads
        assert 'raw trace' in threads and '2 posts | 2 tool calls' in threads, threads[-600:]
        assert threads.count('class="row"') == 3, 'three phase rows'
        assert only_active.count('class="row"') == 1, 'the running phase only'
        assert 'CYNIC_R1' in only_active and 'ACCEPTANCE' not in only_active, only_active
        # The board carries the message body, not just a preview.
        assert 'merged the intervals' in board and 'check the empty case' in board, board
        assert 'D1: half-open ranges' in board and 'ROUND 1' in board, board
        assert 'def merge' in board and '<details>' in board, board
        assert 'claimed a test it never ran' in board, board
        assert 'the empty case is still open' in board and '>@judge</a>' in board, board
        # posted_at, not the trace time, orders the post: it precedes the 12:04 proposal.
        assert board.index('stitch--note-1.md') < board.index('merged the intervals'), board
        assert '12:03:30' in board, board
        assert '<span class="ag at">@all</span>' in mention_chips('run1', ['all', 'sky']), 'room tag'
        assert 'run_command' in phase and 'pytest -q' in phase and '1250ms' in phase, phase
        assert 'STITCH_R1' in phase and 'stitch <b>3</b>' in phase, phase
        assert 'assertion failed' in phase_fail, phase_fail
        assert '>stitch</span>' in agents and '>cynic</span>' in agents, agents[:400]
        assert "% of swarm calls" in agents and '>RUNNING<' in agents, agents
        assert 'stitch <small>@swarm.org' in detail and 'context window' in detail, detail[:600]
        assert '55.3k of 1.0M | 5%' in detail, detail
        assert 'run_command' in detail and '1250ms' in detail, detail
        assert 'primary board' in detail and 'merged the intervals' in detail, detail
        assert 'stitch--note-1.md' in detail and '>@cynic</a>' in detail, detail
        assert 'phases <em>1 owned' in detail, detail
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
        full = page('t', threads, True)
        assert '<script' in page('t', swarms, True), 'full pages need the poller'
        assert 'cdn' not in full.lower(), 'no external fetches'
        # The one external fetch is the font; everything else is inline.
        hosts = set(re.findall(r'https?://([^/"\'\s]+)', full))
        assert hosts == {'fonts.googleapis.com', 'fonts.gstatic.com'}, hosts
        assert 'JetBrains Mono' in full and 'monospace' in full, 'font with a fallback'
        assert 'e6edf3' in full and '0d1117' in full, 'the palette of the trailer'

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
