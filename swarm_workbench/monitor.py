"""Live terminal view of the SSSF trace: swarms, phases, agents, tool calls.

Read-only. Never writes to the trace db, so it is safe against a running swarm.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ESC = '\033['
DIM, BOLD, RESET = ESC + '2m', ESC + '1m', ESC + '0m'
RED, GREEN, YELLOW, CYAN = (ESC + c + 'm' for c in ('31', '32', '33', '36'))
STATUS_COLOR = {'success': GREEN, 'running': CYAN, 'fail': RED, 'failed': RED, 'skipped': DIM}
KIND_COLOR = {'tool_call': YELLOW, 'peer_message': CYAN, 'gate_pass': GREEN,
              'gate_fail': RED, 'error': RED, 'artifact': GREEN}


def connect(db):
    """Read-only URI connection: a live run keeps its WAL, we only ever read.

    Always use through contextlib.closing; sqlite3's own context manager commits
    a transaction but leaves the handle open, which leaks one per refresh here.
    """
    con = sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None


def elapsed(started, ended=None):
    start = parse_time(started)
    if not start:
        return '--'
    end = parse_time(ended) or datetime.now(timezone.utc)
    total = int(max(0, (end - start).total_seconds()))
    return f'{total // 60}m{total % 60:02d}s'


def clip(text, width):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= width else text[:width - 1] + '~'


def swarms(con, limit):
    rows = con.execute('SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?', (limit,)).fetchall()
    out = []
    for row in rows:
        counts = con.execute(
            "SELECT COUNT(*) n, SUM(type='tool_call') tools, SUM(type='peer_message') msgs, "
            "COUNT(DISTINCT phase_id) phases "
            'FROM events WHERE adw_id=?', (row['adw_id'],)).fetchone()
        agents = con.execute('SELECT COUNT(*) n FROM agent_sessions WHERE adw_id=?',
                             (row['adw_id'],)).fetchone()['n']
        out.append({'row': row, 'events': counts['n'], 'tools': counts['tools'] or 0,
                    'phases': counts['phases'] or 0, 'msgs': counts['msgs'] or 0,
                    'agents': agents})
    return out


def render_swarms(con, limit, width):
    lines = [f'{BOLD}SWARMS{RESET} {DIM}/ phases / agents{RESET}', '']
    lines.append(f'{DIM}{"run":<10}{"status":<9}{"elapsed":>8}  {"agents":>6}{"msgs":>6}{"ph":>4}'
                 f'{"tools":>7}{"tokens":>11}  goal{RESET}')
    for item in swarms(con, limit):
        row = item['row']
        status = row['status'] or ('running' if not row['ended_at'] else '?')
        color = STATUS_COLOR.get(status, '')
        lines.append(
            f'{BOLD}{row["adw_id"]:<10}{RESET}{color}{status:<9}{RESET}'
            f'{elapsed(row["started_at"], row["ended_at"]):>8}  {item["agents"]:>6}{item["msgs"]:>6}{item["phases"]:>4}'
            f'{item["tools"]:>7}{(row["total_tokens"] or 0):>11,}  '
            f'{clip(row["request"], max(10, width - 60))}')
    if len(lines) == 3:
        lines.append(f'{DIM}no runs recorded yet{RESET}')
    return lines


def render_run(con, run_id, width, tail):
    session = con.execute('SELECT * FROM sessions WHERE adw_id=?', (run_id,)).fetchone()
    if not session:
        return [f'{RED}unknown run {run_id}{RESET}']
    contract = con.execute("SELECT payload_json FROM events WHERE adw_id=? AND type='run_contract'",
                           (run_id,)).fetchone()
    limits = ''
    if contract:
        import json
        payload = json.loads(contract['payload_json'] or '{}')
        cap = payload.get('limits', {})
        limits = (f'{DIM}model {payload.get("model", "?")} | max {cap.get("agents", "?")} agents '
                  f'| {cap.get("peer_rounds", "?")} rounds | '
                  f'{cap.get("call_timeout_seconds", "?")}s/call{RESET}')
    status = session['status'] or ('running' if not session['ended_at'] else '?')
    color = STATUS_COLOR.get(status, '')
    tools = con.execute("SELECT COUNT(*) n FROM events WHERE adw_id=? AND type='tool_call'",
                        (run_id,)).fetchone()['n']
    lines = [f'{BOLD}{run_id}{RESET}  {color}{status}{RESET}  '
             f'{elapsed(session["started_at"], session["ended_at"])}  '
             f'{(session["total_tokens"] or 0):,} tok  {tools} tool calls',
             f'  {clip(session["request"], width - 4)}']
    if limits:
        lines.append('  ' + limits)

    # Phases are execution steps, NOT communication threads. Calling them threads made a
    # 3-agent run read as "11 threads". Real threads are shared message boards agents join;
    # this swarm has none yet - peers exchange a JSON blob at round boundaries instead.
    boards = con.execute("SELECT COUNT(DISTINCT COALESCE(json_extract(payload_json,'$.thread'),"
                         "'primary')) n FROM events WHERE adw_id=? AND type='peer_message'",
                         (run_id,)).fetchone()['n']
    msgs = con.execute("SELECT COUNT(*) n FROM events WHERE adw_id=? AND type='peer_message'",
                       (run_id,)).fetchone()['n']
    lines += ['', f'{BOLD}THREADS{RESET}  {boards} board, {msgs} messages'
                  f'{DIM}   (peers exchange proposals at round boundaries){RESET}']
    lines += ['', f'{BOLD}PHASES{RESET}']
    for ph in con.execute('SELECT * FROM phases WHERE adw_id=? ORDER BY seq', (run_id,)):
        pcolor = STATUS_COLOR.get(ph['status'], '')
        msgs = con.execute('SELECT COUNT(*) n FROM events WHERE phase_id=?',
                           (ph['phase_id'],)).fetchone()['n']
        lines.append(f'  {ph["seq"]:>3} {ph["name"]:<20}{DIM}{ph["kind"]:<9}{ph["owner"]:<12}{RESET}'
                     f'{pcolor}{ph["status"]:<9}{RESET}{msgs:>4} ev  '
                     f'{elapsed(ph["started_at"], ph["ended_at"]):>7}'
                     + (f'  {RED}{clip(ph["error"], 40)}{RESET}' if ph['error'] else ''))

    lines += ['', f'{BOLD}AGENTS{RESET}']
    agents = con.execute('SELECT * FROM agent_sessions WHERE adw_id=? ORDER BY agent',
                         (run_id,)).fetchall()
    for ag in agents:
        acts = con.execute(
            "SELECT COUNT(*) n, SUM(type='tool_call') tools, SUM(type='peer_message') msgs "
            "FROM events WHERE adw_id=? AND json_extract(payload_json,'$.agent')=?",
            (run_id, ag['agent'])).fetchone()
        alive = con.execute('SELECT COUNT(*) n FROM processes WHERE adw_id=? AND name=? '
                            'AND ended_at IS NULL', (run_id, ag['agent'])).fetchone()['n']
        mark = f'{GREEN}live{RESET}' if alive else f'{DIM}idle{RESET}'
        lines.append(f'  {ag["agent"]:<14}{DIM}{ag["coding_agent"]:<6}{clip(ag["model"], 24):<26}{RESET}'
                     f'{mark:<14}{acts["tools"] or 0:>4} tools{acts["msgs"] or 0:>4} msgs'
                     f'{acts["n"]:>5} ev')
    if not agents:
        lines.append(f'  {DIM}no agents registered{RESET}')

    lines += ['', f'{BOLD}TRACE{RESET} {DIM}(latest {tail}){RESET}']
    rows = con.execute('SELECT * FROM events WHERE adw_id=? ORDER BY rowid DESC LIMIT ?',
                       (run_id, tail)).fetchall()
    for ev in reversed(rows):
        kcolor = KIND_COLOR.get(ev['type'], DIM)
        stamp = (str(ev['started_at'] or '')[11:19]) or '--:--:--'
        owner = con.execute('SELECT owner FROM phases WHERE phase_id=?',
                            (ev['phase_id'],)).fetchone()
        who = (owner['owner'] if owner else 'system') or 'system'
        lines.append(f'  {DIM}{stamp}{RESET} {who:<12}{kcolor}{ev["type"]:<14}{RESET}'
                     f'{clip(ev["name"], max(10, width - 45))}')
    return lines


def draw(lines, width):
    sys.stdout.write(ESC + 'H')
    for line in lines:
        sys.stdout.write(line[:width + 200] + ESC + 'K\n')
    sys.stdout.write(ESC + 'J')
    sys.stdout.flush()


def run(db, run_id, interval, limit, tail, once):
    if os.name == 'nt':
        os.system('')  # enables ANSI escape handling on Windows consoles
    if not once:
        sys.stdout.write(ESC + '?25l')
    try:
        while True:
            width = max(60, os.get_terminal_size().columns - 1) if sys.stdout.isatty() else 120
            with closing(connect(db)) as con:
                target = run_id
                if target == 'latest':
                    row = con.execute('SELECT adw_id FROM sessions ORDER BY started_at DESC '
                                      'LIMIT 1').fetchone()
                    target = row['adw_id'] if row else None
                body = render_run(con, target, width, tail) if target else render_swarms(
                    con, limit, width)
            header = (f'{DIM}swarm monitor | {db} | '
                      f'{datetime.now().strftime("%H:%M:%S")}'
                      + ('' if once else f' | refresh {interval}s | ctrl-c to quit') + RESET)
            draw([header, ''] + body, width)
            if once:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        if not once:
            sys.stdout.write(ESC + '?25h\n')
            sys.stdout.flush()


def demo():
    """Self-check: build a trace in memory and assert the renderers read it back."""
    import json
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'demo.db'
        con = sqlite3.connect(path)
        con.executescript("""
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
        """)
        now = '2026-09-10T12:00:00+00:00'
        con.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)',
                    ('run1', 'n', 'merge intervals', 'running', 'eng', now, None, 271318, 0.0, 0))
        con.execute('INSERT INTO phases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('p1', 'run1', 1, 'builder_r1', 'agent', 'builder', 'd', 'running',
                     1, 0, None, now, None))
        con.execute('INSERT INTO agent_sessions VALUES (?,?,?,?,?,?,?,?,?,?)',
                    ('run1', 'builder', 'agy', 'gemini-3.8-flash-medium', '#0', '', 0, 0, now, now))
        con.execute('INSERT INTO processes VALUES (NULL,?,?,?,?,?,?,?)',
                    ('run1', 'agent', 'builder', 42, 'agy', now, None))
        con.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)',
                    ('e1', 'run1', 'p1', None, 'tool_call', 'finish',
                     json.dumps({'agent': 'builder'}), 0, now, None))
        con.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)',
                    ('e2', 'run1', '', None, 'run_contract', 'acceptance',
                     json.dumps({'model': 'gemini-3.8-flash-medium',
                                 'limits': {'agents': 3, 'peer_rounds': 2,
                                            'call_timeout_seconds': 180}}), 0, now, None))
        con.commit()
        con.close()
        with closing(connect(path)) as ro:
            overview = '\n'.join(render_swarms(ro, 10, 120))
            detail = '\n'.join(render_run(ro, 'run1', 120, 25))
            missing = '\n'.join(render_run(ro, 'nope', 120, 25))
        assert 'run1' in overview and 'merge intervals' in overview, overview
        assert '271,318' in overview, overview
        assert 'THREADS' in detail and 'builder_r1' in detail, detail
        assert 'AGENTS' in detail and 'live' in detail, detail
        assert 'TRACE' in detail and 'tool_call' in detail, detail
        assert 'gemini-3.8-flash-medium' in detail, detail
        assert 'unknown run' in missing, missing
        # A read-only handle must refuse writes even if a caller tries.
        try:
            with closing(connect(path)) as ro:
                ro.execute("UPDATE sessions SET status='tampered'")
            raise AssertionError('read-only connection accepted a write')
        except sqlite3.OperationalError:
            pass
    print('monitor self-check ok')


def add_arguments(parser):
    parser.add_argument('run_id', nargs='?', help="run id, or 'latest'; omit for the swarm list")
    parser.add_argument('--db', default=os.environ.get('SSSF_DB', 'adws/adw_data/sssf.db'))
    parser.add_argument('--interval', type=float, default=1.0)
    parser.add_argument('--limit', type=int, default=15, help='swarms listed in the overview')
    parser.add_argument('--tail', type=int, default=25, help='trace lines in the run view')
    parser.add_argument('--once', action='store_true', help='render one frame and exit')
    parser.add_argument('--self-check', action='store_true', help='run the offline self-check')
    return parser


def main(args):
    if args.self_check:
        demo()
        return 0
    return run(args.db, args.run_id, args.interval, args.limit, args.tail, args.once)


if __name__ == '__main__':
    import argparse
    main(add_arguments(argparse.ArgumentParser(description=__doc__)).parse_args())
