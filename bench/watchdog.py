"""Kill a swarm that stopped making progress, instead of waiting out its timeout.

A run can be alive and useless: the provider queues every completion, one owner holds the
only claim, or the CLI sits on a dead socket. The harness's own SWARM_RUN_TIMEOUT only fires
at the end of the wall clock, so this watches the trace instead: no new event for --stall
seconds means the run is not working, and its process tree is killed.

The pid comes from the trace (SSSF records the ADW process), so this kills the run it is
watching and nothing else.

Run: .venv\\Scripts\\python.exe bench\\watchdog.py [run_id|latest] [--stall 300] [--max 2400]
"""
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / 'adws' / 'adw_data' / 'sssf.db'


def con():
    return sqlite3.connect('file:%s?mode=ro' % DB.as_posix(), uri=True)


def latest_run():
    row = con().execute("select adw_id from sessions where adw_name='adw_agy_swarm' "
                        "order by started_at desc limit 1").fetchone()
    return row[0] if row else ''


def streamed(run):
    """Newest byte written by any agent of this run.

    The trace only grows on tool calls, and a whole-file prototype can reason for minutes
    between them: watching the trace alone would kill a working run. The agents' own stream
    files move continuously, so they are the honest liveness signal.
    """
    newest = 0.0
    session = ROOT / 'adws' / 'adw_data' / 'sessions' / run
    for stream in session.rglob('events.jsonl'):
        try:
            newest = max(newest, stream.stat().st_mtime)
        except OSError:
            continue
    return newest


def progress(run):
    """How far the trace has got: (events, last phase name, status)."""
    c = con()
    events = c.execute('select count(*) from events where adw_id=?', (run,)).fetchone()[0]
    phase = c.execute('select name from phases where adw_id=? order by seq desc limit 1',
                      (run,)).fetchone()
    status = c.execute('select status from sessions where adw_id=?', (run,)).fetchone()
    return events, (phase or [''])[0], (status or [''])[0]


def live_pids(run):
    return [r[0] for r in con().execute(
        'select pid from processes where adw_id=? and ended_at is null', (run,)).fetchall() if r[0]]


def kill(pid):
    if os.name == 'nt':
        return subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                              capture_output=True, text=True).returncode == 0
    os.kill(pid, 9)
    return True


def main(argv):
    run = (argv[1] if len(argv) > 1 and not argv[1].startswith('--') else 'latest')
    if run == 'latest':
        run = latest_run()
    stall = int(next((a.split('=')[1] for a in argv if a.startswith('--stall=')), 300))
    hard = int(next((a.split('=')[1] for a in argv if a.startswith('--max=')), 2400))
    if not run:
        print('no swarm run to watch')
        return 2
    started = time.monotonic()
    seen, wrote, moved = progress(run)[0], streamed(run), time.monotonic()
    print('watching %s: stall=%ss hard=%ss events=%d' % (run, stall, hard, seen), flush=True)
    while True:
        time.sleep(15)
        events, phase, status = progress(run)
        fresh = streamed(run)
        wall = time.monotonic() - started
        if status and status not in ('running', ''):
            print('%s finished as %s after %.0fs, %d events' % (run, status, wall, events), flush=True)
            return 0
        if events != seen or fresh > wrote:
            seen, wrote, moved = events, fresh, time.monotonic()
            print('%4.0fs %5d events, phase %s' % (wall, events, phase), flush=True)
            continue
        idle = time.monotonic() - moved
        if idle < stall and wall < hard:
            continue
        why = 'stalled %.0fs with no new event' % idle if idle >= stall else 'hit the hard cap'
        pids = live_pids(run)
        print('KILLING %s: %s (phase %s, %d events). pids=%s' % (run, why, phase, events, pids),
              flush=True)
        for pid in pids:
            print('  taskkill %s -> %s' % (pid, kill(pid)), flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
