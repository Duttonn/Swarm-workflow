"""The coordination tools an agent calls: leases, inbox cursor, team, done.

Runs the real board script the way an agent would, in a temp board, with no model involved.
"""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'adws'))
from adw_modules import coord  # noqa: E402


def run(board, *args):
    done = subprocess.run([sys.executable, str(Path(board) / 'swarm.py'), *args],
                          capture_output=True, text=True, encoding='utf-8', errors='replace')
    return done.returncode, (done.stdout + done.stderr)


class Coord(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='coord-')
        self.board = Path(self.dir) / 'board'
        self.board.mkdir()
        coord.write_tools(self.board, ['alice', 'bob'], 'test-model', 'test-runner')

    def post(self, name, text):
        (self.board / name).write_text(text, encoding='utf-8')

    def test_inbox_delivers_tagged_posts_once(self):
        self.post('alice--note-1.md', '@bob please take the header')
        code, out = run(self.board, 'inbox', '--as', 'bob')
        self.assertEqual(code, 0, out)
        self.assertIn('please take the header', out)
        _, again = run(self.board, 'inbox', '--as', 'bob')
        self.assertIn('no new messages', again)

    def test_inbox_ignores_posts_for_other_agents(self):
        self.post('alice--note-1.md', '@carol a note for carol only')
        _, out = run(self.board, 'inbox', '--as', 'bob')
        self.assertIn('no new messages', out)

    def test_unread_reopens_a_session_only_for_a_direct_question(self):
        # the harness side of the open session: bob is asked directly, carol only broadcast
        self.post('alice--note-1.md', '@bob can your block expose tagsOf(card)?')
        self.post('carol--note-1.md', '@all my block is in')
        self.assertEqual(coord.unread(self.board, 'bob'), ['alice--note-1.md'])
        self.assertEqual(coord.unread(self.board, 'alice'), [])
        run(self.board, 'inbox', '--as', 'bob')          # bob reads it: nothing left to reopen for
        self.assertEqual(coord.unread(self.board, 'bob'), [])

    def test_claim_is_exclusive_then_released(self):
        code, out = run(self.board, 'claim', 'server.js', '60', 'landing', '--as', 'alice')
        self.assertEqual(code, 0, out)
        code, refused = run(self.board, 'claim', 'server.js', '--as', 'bob')
        self.assertEqual(code, 1)
        self.assertIn('REFUSED', refused)
        code, wrong = run(self.board, 'release', 'server.js', '--as', 'bob')
        self.assertEqual(code, 1, wrong)
        self.assertEqual(run(self.board, 'release', 'server.js', '--as', 'alice')[0], 0)
        self.assertEqual(run(self.board, 'claim', 'server.js', '--as', 'bob')[0], 0)

    def test_expired_lease_can_be_taken_over(self):
        run(self.board, 'claim', 'server.js', '1', 'short', '--as', 'alice')
        time.sleep(1.2)
        code, out = run(self.board, 'claim', 'server.js', '--as', 'bob')
        self.assertEqual(code, 0, out)      # a dead holder must never deadlock the swarm

    def test_team_and_done(self):
        run(self.board, 'claim', 'server.js', '--as', 'alice')
        run(self.board, 'done', 'server.js', 'header', 'landed', '--as', 'bob')
        _, out = run(self.board, 'team', '--as', 'alice')
        self.assertIn('@alice', out)
        self.assertIn('server.js', out)
        self.assertIn('DONE', out)
        self.assertEqual(sorted(coord.done_reports(self.board)), ['bob'])
        self.assertEqual(coord.summary(self.board)['claims'], 1)

    def test_history_records_who_held_it(self):
        run(self.board, 'claim', 'server.js', '--as', 'alice')
        run(self.board, 'release', 'server.js', '--as', 'alice')
        _, out = run(self.board, 'history', 'server.js', '--as', 'bob')
        self.assertIn('claim', out)
        self.assertIn('release', out)
        self.assertIn('alice', out)

    def test_render_separates_a_live_page_from_a_dead_one(self):
        # browser() lives inside the agent-facing script, so probe the same paths it probes
        if not any(Path(p).is_file() for p in (
                r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
                r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                '/usr/bin/chromium', '/usr/bin/google-chrome')):
            raise unittest.SkipTest('no headless browser on this machine')
        pages = Path(self.dir)
        (pages / 'good.html').write_text(
            '<html><body><div id=a>hi</div><script>document.body.innerHTML += '
            '"<p>built</p>".repeat(6)</script></body></html>', encoding='utf-8')
        (pages / 'dead.html').write_text(
            '<html><body><script>throw new Error("boom")</script></body></html>', encoding='utf-8')
        code, out = run(self.board, 'render', 'good.html', '--as', 'alice')
        self.assertEqual(code, 0, out)
        self.assertIn('OK', out)
        code, out = run(self.board, 'render', 'dead.html', '--as', 'alice')
        self.assertEqual(code, 1, out)
        self.assertIn('BLANK', out)

    def test_budget_reads_the_shared_file(self):
        (self.board / 'budget.json').write_text(
            json.dumps({'run': 'r', 'spent': 5, 'cap': 10, 'usd': 1.25}), encoding='utf-8')
        _, out = run(self.board, 'budget', '--as', 'bob')
        self.assertIn('50% left', out)
        self.assertIn('$1.2500', out)


if __name__ == '__main__':
    unittest.main()
