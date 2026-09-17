"""Coordination substrate: the tools an agent CALLS, instead of context the harness pushes.

Measured gap against IndyDevDan's simple swarm system (video S2sjyokoxeE, 13:35-14:25): his
agents call `inbox`, `list_team`, `claims`, `file_history`, `claim_file(path, reason, seconds)`,
`release_file`, `done`. Ours only received a mailbox frozen into the prompt at build time, so an
agent could not learn anything during its own turn, and two agents could never negotiate a file.

Everything lives under <board>/coord/ so the container and the host see the same state:
  claims/<slug>.json   one file per live lease - O_EXCL create IS the lock, TTL in the payload
  cursor-<agent>.json  {post filename: mtime} for every post that agent has already read
  history.jsonl        append-only log of inbox/claim/release/done events
  team.json            the roster, written by the harness at swarm start
  done/<agent>.json    an agent declaring itself finished

The agent-facing surface is ONE script (board/swarm.py) with subcommands, mirroring the existing
board/budget.py pattern: stdlib only, no imports from this package, so it runs unchanged inside
the swarm container.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

MENTION = re.compile(r'@([a-z0-9_-]+)')     # the same two patterns the tool script carries
HARNESS = re.compile(r'^\d{3}--')

# Off by default: the runs measured before it existed are the control group, and a spec that
# never mentions the tools should behave exactly as it did. Set SWARM_COORD=1 to enable.
ENABLED = os.environ.get('SWARM_COORD', '') not in ('', '0', 'false')
CLAIM_SECONDS = int(os.environ.get('SWARM_CLAIM_SECONDS', '120'))

TOOL = r'''#!/usr/bin/env python3
"""Swarm coordination tools. Run me: python swarm.py <command> --as <your-agent-name>

  inbox                       posts addressed to you (@you or @all) since your last inbox call
  team                        every agent, what it holds, whether it is done
  claims                      every live file claim
  claim <path> [seconds]      take a lease on a file before you edit it
  release <path>              give the lease back as soon as you are done
  history <path>              who claimed or released this file, newest first
  render <file>               load the page in a headless browser and say if it really renders
  done <output_file> <reason> declare your work finished
  budget                      spend against the cap (same figure as budget.py)

A claim is a LEASE: it expires on its own, so a dead agent cannot deadlock the swarm. Renew by
calling claim again. Editing a file someone else holds is a claim violation and shows in history.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

BOARD = Path(__file__).resolve().parent
COORD = BOARD / 'coord'
DEFAULT_SECONDS = 120
MENTION = re.compile(r'@([a-z0-9_-]+)')
HARNESS = re.compile(r'^\d{3}--')


def agent_name(argv):
    for i, a in enumerate(argv):
        if a == '--as' and i + 1 < len(argv):
            return argv[i + 1]
    return os.environ.get('SWARM_AGENT') or ''


def slug(path):
    return re.sub(r'[^a-z0-9]+', '-', str(path).lower()).strip('-')[:120] or 'file'


def now():
    return time.time()


def log(event, **fields):
    COORD.mkdir(parents=True, exist_ok=True)
    line = json.dumps({'at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'event': event, **fields})
    with (COORD / 'history.jsonl').open('a', encoding='utf-8') as fh:
        fh.write(line + '\n')


def live_claims():
    out = []
    for path in sorted((COORD / 'claims').glob('*.json')) if (COORD / 'claims').is_dir() else []:
        try:
            item = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if item.get('expires_at', 0) < now():
            try:
                path.unlink()          # the lease ran out: the file is free again
            except OSError:
                pass
            continue
        out.append(item)
    return out


def cmd_inbox(me, argv):
    # Per-post cursor, not a single high-water mtime: a float mtime does not survive being
    # formatted and read back at the filesystem's resolution, and one lost digit redelivers
    # a message the agent has already acted on. Filenames are exact and also catch an edit.
    cursor = COORD / ('cursor-%s.json' % me)
    try:
        seen = json.loads(cursor.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        seen = {}
    hits = []
    for path in BOARD.glob('*.md'):
        if HARNESS.match(path.name) or path.name == 'thread.md':
            continue
        try:
            mtime = path.stat().st_mtime
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        author = path.name.split('--')[0]
        if author == me or seen.get(path.name) == mtime:
            seen[path.name] = mtime
            continue
        seen[path.name] = mtime
        tags = set(MENTION.findall(text.lower()))
        if me in tags or 'all' in tags:
            hits.append((mtime, path.name, author, text))
    COORD.mkdir(parents=True, exist_ok=True)
    cursor.write_text(json.dumps(seen), encoding='utf-8')
    # Logged like claims: the CLI runners' tool events do not carry the command text, so
    # this line is the only provider-independent count of inbox reads a run has.
    log('inbox', agent=me, new=len(hits))
    if not hits:
        print('inbox: no new messages for @%s' % me)
        return 0
    print('inbox: %d new message(s) for @%s' % (len(hits), me))
    for mtime, name, author, text in sorted(hits):
        print('--- %s from @%s' % (name, author))
        print(text.strip()[:1200])
    return 0


def cmd_team(me, argv):
    try:
        team = json.loads((COORD / 'team.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        team = {'agents': []}
    held = {}
    for item in live_claims():
        held.setdefault(item['agent'], []).append(item['path'])
    done = {}
    if (COORD / 'done').is_dir():
        for path in (COORD / 'done').glob('*.json'):
            done[path.stem] = True
    posts = {}
    for path in BOARD.glob('*.md'):
        if HARNESS.match(path.name) or path.name == 'thread.md':
            continue
        who = path.name.split('--')[0]
        try:
            posts[who] = max(posts.get(who, 0), path.stat().st_mtime)
        except OSError:
            pass
    print('team of %d (you are @%s), model %s' % (len(team.get('agents', [])), me,
                                                  team.get('model', '?')))
    for name in team.get('agents', []):
        last = posts.get(name)
        age = '%.0fs ago' % (now() - last) if last else 'never posted'
        print('  @%-18s %-12s holds=%s%s' % (
            name, 'DONE' if done.get(name) else 'working',
            ','.join(held.get(name, [])) or '-',
            '  last post %s' % age))
    return 0


def cmd_claims(me, argv):
    items = live_claims()
    if not items:
        print('claims: none live')
        return 0
    for item in sorted(items, key=lambda i: i['path']):
        print('%-40s @%-16s %4.0fs left  %s' % (item['path'], item['agent'],
                                                item['expires_at'] - now(),
                                                item.get('reason', '')))
    return 0


def cmd_claim(me, argv):
    if not argv:
        print('usage: claim <path> [seconds] [reason...]', file=sys.stderr)
        return 2
    target = argv[0]
    seconds = DEFAULT_SECONDS
    rest = argv[1:]
    if rest and rest[0].isdigit():
        seconds, rest = int(rest[0]), rest[1:]
    reason = ' '.join(rest)
    (COORD / 'claims').mkdir(parents=True, exist_ok=True)
    path = COORD / 'claims' / (slug(target) + '.json')
    payload = {'path': target, 'agent': me, 'reason': reason,
               'claimed_at': now(), 'expires_at': now() + seconds}
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                held = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                held = {}
            if held.get('expires_at', 0) > now() and held.get('agent') != me:
                print('REFUSED: %s is held by @%s for another %.0fs (%s). Post a note asking for '
                      'it, or work on something else.' % (target, held.get('agent'),
                                                          held['expires_at'] - now(),
                                                          held.get('reason', '')))
                return 1
            try:
                path.unlink()   # expired, or already ours: retake it
            except OSError:
                pass
            continue
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh)
        log('claim', agent=me, path=target, seconds=seconds, reason=reason)
        print('Claimed %r for %ds. Make your edit, then release %r. Re-call claim to renew.'
              % (target, seconds, target))
        return 0
    print('REFUSED: could not take %s' % target)
    return 1


def cmd_release(me, argv):
    if not argv:
        print('usage: release <path>', file=sys.stderr)
        return 2
    target = argv[0]
    path = COORD / 'claims' / (slug(target) + '.json')
    try:
        held = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        print('release: %s was not claimed' % target)
        return 0
    if held.get('agent') != me:
        print('release: %s is held by @%s, not you' % (target, held.get('agent')))
        return 1
    try:
        path.unlink()
    except OSError:
        pass
    log('release', agent=me, path=target)
    print('Released %r.' % target)
    return 0


def cmd_history(me, argv):
    if not argv:
        print('usage: history <path>', file=sys.stderr)
        return 2
    target = argv[0]
    rows = []
    try:
        for line in (COORD / 'history.jsonl').read_text(encoding='utf-8').splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if item.get('path') == target:
                rows.append(item)
    except OSError:
        pass
    disk = BOARD.parent / target
    if disk.is_file():
        st = disk.stat()
        print('%s on disk: %d bytes, modified %.0fs ago' % (target, st.st_size,
                                                            now() - st.st_mtime))
    for item in reversed(rows[-20:]):
        print('%s  %-8s @%s %s' % (item['at'], item['event'], item.get('agent'),
                                   item.get('reason', '')))
    if not rows:
        print('history: no claim or release recorded for %s' % target)
    return 0


def cmd_done(me, argv):
    if len(argv) < 1:
        print('usage: done <output_file> <reason...>', file=sys.stderr)
        return 2
    (COORD / 'done').mkdir(parents=True, exist_ok=True)
    payload = {'agent': me, 'output_file': argv[0], 'reason': ' '.join(argv[1:]), 'at': now()}
    (COORD / 'done' / ('%s.json' % me)).write_text(json.dumps(payload), encoding='utf-8')
    log('done', agent=me, path=argv[0], reason=payload['reason'])
    print('Recorded: @%s is done with %s. Finish your reply now.' % (me, argv[0]))
    return 0


def browser():
    for candidate in (os.environ.get('SWARM_BROWSER'),
                      r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
                      r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                      '/usr/bin/chromium', '/usr/bin/google-chrome'):
        if candidate and Path(candidate).is_file():
            return candidate
    return ''


def cmd_render(me, argv):
    """Does the page actually come up? The tests are numeric; this is the eye.

    Loads the file in a headless browser and reports the DOM AFTER scripts ran, so a page whose
    script throws (and leaves an empty body) is caught even though the source looks complete.
    """
    if not argv:
        print('usage: render <file.html|file.svg>', file=sys.stderr)
        return 2
    target = Path(argv[0])
    if not target.is_absolute():
        target = (BOARD.parent / argv[0]).resolve()
    if not target.is_file():
        print('render: no such file %s' % target)
        return 1
    exe = browser()
    if not exe:
        print('render: no headless browser found; skipping (set SWARM_BROWSER)')
        return 0
    import subprocess
    import tempfile
    profile = tempfile.mkdtemp(prefix='swarm-render-')
    argvv = [exe, '--headless=new', '--disable-gpu', '--no-sandbox', '--virtual-time-budget=4000',
             '--user-data-dir=' + profile, '--dump-dom', target.as_uri()]
    try:
        done = subprocess.run(argvv, capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=90)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print('render: browser failed: %s' % exc)
        return 1
    dom = done.stdout or ''
    source = target.read_text(encoding='utf-8', errors='replace')
    rendered = len(re.findall(r'<[a-zA-Z][^>]*>', dom))
    authored = len(re.findall(r'<[a-zA-Z][^>]*>', source))
    text = re.sub(r'<[^>]+>', ' ', dom)
    print('render: %d elements after scripts ran (%d in the source), %d characters of text'
          % (rendered, authored, len(text.split() and ' '.join(text.split()) or '')))
    if rendered < 5:
        print('BLANK: the page produced almost nothing. Your script probably threw before it '
              'built the DOM; open the file and check the console path.')
        return 1
    if authored > 40 and rendered < authored // 3:
        print('THIN: the rendered DOM is much smaller than the source. Something in the script '
              'stopped early.')
        return 1
    print('OK: the page renders.')
    return 0


def cmd_budget(me, argv):
    try:
        data = json.loads((BOARD / 'budget.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        print('budget: unknown')
        return 0
    cap, spent = data.get('cap') or 0, data.get('spent') or 0
    left = 'uncapped' if not cap else '%.0f%% left' % (100.0 * max(0, cap - spent) / cap)
    usd = data.get('usd')
    print('budget: %s of %s tokens spent, %s%s' % (
        format(spent, ','), format(cap, ',') if cap else 'no cap', left,
        '' if usd is None else ', $%.4f spent' % usd))
    return 0


COMMANDS = {'inbox': cmd_inbox, 'team': cmd_team, 'claims': cmd_claims, 'claim': cmd_claim,
            'release': cmd_release, 'history': cmd_history, 'done': cmd_done,
            'render': cmd_render, 'budget': cmd_budget}


def main(argv):
    if len(argv) < 2 or argv[1] in ('-h', '--help'):
        print(__doc__)
        return 0
    command = argv[1]
    if command not in COMMANDS:
        print('unknown command %r; one of %s' % (command, ', '.join(COMMANDS)), file=sys.stderr)
        return 2
    me = agent_name(argv)
    if not me:
        print('who are you? pass --as <your-agent-name>', file=sys.stderr)
        return 2
    rest = [a for i, a in enumerate(argv[2:], 2)
            if a != '--as' and argv[i - 1] != '--as']
    return COMMANDS[command](me, rest)


if __name__ == '__main__':
    sys.exit(main(sys.argv))
'''


def write_tools(board, roster, model, runner):
    """Put the tool and the roster on the board, where every agent can reach them."""
    board = Path(board)
    (board / 'coord').mkdir(parents=True, exist_ok=True)
    (board / 'swarm.py').write_text(TOOL, encoding='utf-8')
    (board / 'coord' / 'team.json').write_text(
        json.dumps({'agents': list(roster), 'model': model, 'runner': runner}), encoding='utf-8')


def protocol(board_shown, agent):
    """The brief section that teaches one agent the tools. Terse on purpose: every line here is
    paid for by 20 agents on every turn."""
    return (
        'COORDINATION TOOLS - call them, they are how you see and are seen. Prefix every call\n'
        'with `python %s/swarm.py` and end it with `--as %s`:\n'
        '  inbox                      messages addressed to @%s or @all since you last looked.\n'
        '                             Call it when you start AND again before you finish: the\n'
        '                             board moves while you work.\n'
        '  team                       who exists, what they hold, who is done.\n'
        '  claims                     which files are locked right now.\n'
        '  claim <path> [seconds]     TAKE A LEASE BEFORE YOU EDIT ANY SHARED FILE. It expires\n'
        '                             on its own, so nothing deadlocks; call it again to renew.\n'
        '  release <path>             the moment your edit is written.\n'
        '  history <path>             who claimed or released it, and how old it is on disk.\n'
        '  done <output_file> <why>   declare yourself finished, then end your reply.\n'
        '  budget                     spend against the cap.\n'
        'Editing a file another agent holds is a claim violation and it shows in history. If a\n'
        'claim is REFUSED, do not wait: post a note tagging the holder and work on something\n'
        'else.\n' % (board_shown, agent, agent))


def done_reports(board):
    """{agent: payload} for every agent that declared itself finished."""
    out = {}
    folder = Path(board) / 'coord' / 'done'
    if not folder.is_dir():
        return out
    for path in folder.glob('*.json'):
        try:
            out[path.stem] = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
    return out


def unread(board, agent):
    """Posts by other agents that name @agent directly and that its inbox has not delivered:
    the reason to give an owner another turn after its block has landed (a question it has
    not answered). @all broadcasts do not count, or every owner would get every turn."""
    board = Path(board)
    cursor = board / 'coord' / ('cursor-%s.json' % agent)
    try:
        seen = json.loads(cursor.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        seen = {}
    hits = []
    for path in board.glob('*.md'):
        if HARNESS.match(path.name) or path.name == 'thread.md' or path.name.split('--')[0] == agent:
            continue
        try:
            if seen.get(path.name) == path.stat().st_mtime:
                continue
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        if agent in set(MENTION.findall(text.lower())):
            hits.append(path.name)
    return sorted(hits)


def events(board):
    """Every claim/release/done event, oldest first."""
    out = []
    path = Path(board) / 'coord' / 'history.jsonl'
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def summary(board):
    """What the coordination layer actually did, for the trace and for measure_run."""
    log = events(board)
    claims = [e for e in log if e['event'] == 'claim']
    return {'claims': len(claims), 'releases': len([e for e in log if e['event'] == 'release']),
            'inbox_reads': len([e for e in log if e['event'] == 'inbox']),
            'inbox_readers': sorted({e['agent'] for e in log if e['event'] == 'inbox'}),
            'done': len(done_reports(board)),
            'claimers': sorted({e['agent'] for e in claims}),
            'files_claimed': sorted({e['path'] for e in claims})}
