"""The pieces of the NIM runner that fail silently: path confinement, SSE reassembly, the
retry ladder and shared 429 cooldown, the edit/append tools, and the no-tool-call nudge.

No network. A real call is bench/check_runner.py nim <model>.
"""
import os
import queue
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'adws'))
from adw_modules.agy_swarm import AgentRequest  # noqa: E402
from adw_modules.runners import nim  # noqa: E402


def request(folder, shared=()):
    return AgentRequest(agent='probe', prompt='x', folder=Path(folder), runner='nim',
                        model='test', shared=tuple(shared))


class Confinement(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix='nim-'))
        (self.dir / 'mine').mkdir()
        (self.dir / 'theirs').mkdir()
        self.req = request(self.dir / 'mine')

    def test_relative_paths_land_in_the_agent_folder(self):
        out = nim.run_tool(self.req, 'write_file', {'path': 'server.js', 'content': 'hi'})
        self.assertIn('server.js', out)
        self.assertEqual((self.dir / 'mine' / 'server.js').read_text(encoding='utf-8'), 'hi')

    def test_append_builds_a_file_across_calls(self):
        # the 32k-output-cap failure: one turn cannot hold a 40KB file, so parts must add up
        nim.run_tool(self.req, 'write_file', {'path': 'big.js', 'content': 'line1\n'})
        out = nim.run_tool(self.req, 'write_file',
                           {'path': 'big.js', 'content': 'line2\n', 'append': True})
        self.assertEqual((self.dir / 'mine' / 'big.js').read_text(encoding='utf-8'),
                         'line1\nline2\n')
        self.assertIn('12 bytes', out)
        nim.run_tool(self.req, 'write_file', {'path': 'big.js', 'content': 'fresh\n'})
        self.assertEqual((self.dir / 'mine' / 'big.js').read_text(encoding='utf-8'), 'fresh\n')

    def test_overwriting_a_bigger_file_says_so(self):
        # the solo run's 60-step oscillation: 14 KB body, then a 1.3 KB block written over it
        nim.run_tool(self.req, 'write_file', {'path': 's.js', 'content': 'x' * 14000})
        out = nim.run_tool(self.req, 'write_file', {'path': 's.js', 'content': 'y' * 1300})
        self.assertIn('REPLACED', out)
        self.assertIn('14000 bytes are gone', out)
        self.assertIn('edit_file', out)

    def test_edit_file_replaces_one_exact_passage(self):
        nim.run_tool(self.req, 'write_file', {'path': 's.js', 'content': 'a\nfunction f() {}\nb\n'})
        out = nim.run_tool(self.req, 'edit_file', {'path': 's.js', 'old_text': 'function f() {}',
                                                   'new_text': 'function f() { return 1 }'})
        self.assertTrue(out.startswith('edited'), out)
        self.assertEqual((self.dir / 'mine' / 's.js').read_text(encoding='utf-8'),
                         'a\nfunction f() { return 1 }\nb\n')

    def test_edit_file_refuses_an_ambiguous_or_missing_passage(self):
        nim.run_tool(self.req, 'write_file', {'path': 's.js', 'content': 'x x'})
        self.assertIn('appears 2 times', nim.run_tool(
            self.req, 'edit_file', {'path': 's.js', 'old_text': 'x', 'new_text': 'y'}))
        self.assertIn('appears 0 times', nim.run_tool(
            self.req, 'edit_file', {'path': 's.js', 'old_text': 'z', 'new_text': 'y'}))
        self.assertEqual((self.dir / 'mine' / 's.js').read_text(encoding='utf-8'), 'x x')

    def test_read_file_returns_a_whole_deliverable(self):
        nim.run_tool(self.req, 'write_file', {'path': 's.js', 'content': 'q' * 30000})
        self.assertEqual(len(nim.run_tool(self.req, 'read_file', {'path': 's.js'})), 30000)

    def test_read_file_takes_a_numbered_line_range(self):
        # a repair turn re-read a 1,900-line page at every step: 7.2M tokens for one turn
        nim.run_tool(self.req, 'write_file', {'path': 'p.html', 'content': 'l1 l2 l3 l4 l5'.replace(' ', chr(10))})
        out = nim.run_tool(self.req, 'read_file', {'path': 'p.html', 'lines': '2-3'})
        self.assertEqual(out, '2: l2' + chr(10) + '3: l3' + chr(10) + '[lines 2-3 of 5]')
        self.assertTrue(nim.run_tool(self.req, 'read_file', {'path': 'p.html', 'lines': 'x'}).startswith('ERROR'))
        self.assertIn('[lines 4-5 of 5]', nim.run_tool(self.req, 'read_file', {'path': 'p.html', 'lines': '4-99'}))

    def test_escaping_the_folder_is_refused(self):
        for escape in ('../theirs/steal.js', str(self.dir / 'theirs' / 'steal.js')):
            with self.assertRaises(ValueError):
                nim.run_tool(self.req, 'write_file', {'path': escape, 'content': 'x'})
        self.assertEqual(list((self.dir / 'theirs').iterdir()), [])

    def test_a_shared_directory_is_reachable(self):
        board = self.dir / 'board'
        board.mkdir()
        (board / 'note.md').write_text('MISSION', encoding='utf-8')
        req = request(self.dir / 'mine', shared=[board])
        self.assertEqual(nim.run_tool(req, 'read_file', {'path': str(board / 'note.md')}),
                         'MISSION')

    def test_run_command_reports_the_exit_code(self):
        out = nim.run_tool(self.req, 'run_command', {'command': 'python -c "raise SystemExit(3)"'})
        self.assertTrue(out.startswith('exit=3'), out)

    def test_a_foreground_server_is_killed_not_waited_on(self):
        # the size-1 prototype ran `node server.js` in the foreground and hung the run 44 min
        import time
        real, nim.COMMAND_TIMEOUT = nim.COMMAND_TIMEOUT, 2
        try:
            t = time.monotonic()
            out = nim.run_tool(self.req, 'run_command', {
                'command': "python -c \"import sys,time; print(123); sys.stdout.flush(); time.sleep(60)\""})
            elapsed = time.monotonic() - t
        finally:
            nim.COMMAND_TIMEOUT = real
        self.assertLess(elapsed, 15, 'communicate() waited on the orphan pipe')
        self.assertTrue(out.startswith('exit=timeout'), out)
        self.assertIn('killed after 2s', out)
        self.assertIn('123', out)                   # what the pipe held is not lost

    def test_a_grandchild_holding_the_pipe_does_not_block(self):
        # the shell exits, the server it spawned keeps stdout: the reader must be abandoned
        import time
        t = time.monotonic()
        out = nim.run_tool(self.req, 'run_command', {
            'command': 'python -c "import subprocess,sys; print(7); sys.stdout.flush(); '
                       "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\""})
        self.assertLess(time.monotonic() - t, 12)
        self.assertTrue(out.startswith('exit=0'), out)

    def test_missing_file_is_an_error_string_not_an_exception(self):
        self.assertIn('does not exist', nim.run_tool(self.req, 'read_file', {'path': 'nope.js'}))

    def test_a_server_left_in_the_background_dies_with_the_turn(self):
        # F10: sixteen `node server.js` were still running after two cline ladders
        import subprocess
        import time
        from adw_modules.agy_swarm import close_job, new_job
        pidfile = self.dir / 'pid.txt'
        self.req.job = new_job()
        out = nim.run_tool(self.req, 'run_command', {
            'command': 'python -c "import subprocess,sys; p = subprocess.Popen([sys.executable, '
                       "'-c', 'import time; time.sleep(60)']); open(%r, 'w').write(str(p.pid))\""
                       % str(pidfile)})
        self.assertTrue(out.startswith('exit=0'), out)
        pid = int(pidfile.read_text())
        self.assertTrue(alive(pid), 'the background child must outlive its command')
        close_job(self.req.job)                    # what invoke() does when the turn ends
        time.sleep(0.5)
        self.assertFalse(alive(pid), 'the child survived the end of the turn')


def alive(pid):
    import subprocess
    if os.name == 'nt':
        out = subprocess.run(['tasklist', '/FI', 'PID eq %d' % pid, '/NH'], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class Reassembly(unittest.TestCase):
    def test_tool_call_split_across_chunks_is_rejoined(self):
        calls = {}
        nim.merge_delta(calls, [{'index': 0, 'id': 'call_1',
                                 'function': {'name': 'write_file', 'arguments': '{"pa'}}])
        nim.merge_delta(calls, [{'index': 0, 'function': {'arguments': 'th": "a.js",'}}])
        nim.merge_delta(calls, [{'index': 0, 'function': {'arguments': ' "content": "x"}'}}])
        self.assertEqual(calls[0]['name'], 'write_file')
        self.assertEqual(calls[0]['arguments'], '{"path": "a.js", "content": "x"}')

    def test_two_parallel_calls_stay_separate(self):
        calls = {}
        nim.merge_delta(calls, [{'index': 0, 'id': 'a', 'function': {'name': 'read_file'}},
                                {'index': 1, 'id': 'b', 'function': {'name': 'write_file'}}])
        nim.merge_delta(calls, [{'index': 1, 'function': {'arguments': '{}'}}])
        self.assertEqual([calls[i]['name'] for i in sorted(calls)], ['read_file', 'write_file'])
        self.assertEqual(calls[0]['arguments'], '')


class Retry(unittest.TestCase):
    """The overloaded free tier: an in-band 503 must be retried, a 401 must not."""

    def setUp(self):
        self.calls = 0
        self.waits = []
        nim.RETRY_WAITS, self._waits = (0, 0, 0), nim.RETRY_WAITS   # no real sleeping
        nim.MIN_INTERVAL, self._interval = 0.0, nim.MIN_INTERVAL
        nim._interval[0] = 0.0

    def tearDown(self):
        nim.RETRY_WAITS, nim.MIN_INTERVAL = self._waits, self._interval
        nim._next_start[0] = 0.0                 # a test's 429 cooldown must not slow the next test
        nim._interval[0] = nim.MIN_INTERVAL

    def drive(self, outcomes):
        import io
        import threading
        script = iter(outcomes)

        def fake(req, history, raw, cancel):
            self.calls += 1
            result = next(script)
            if isinstance(result, Exception):
                raise result
            return result
        real, nim.one_turn = nim.one_turn, fake
        try:
            return nim.one_turn_with_retries(None, [], io.StringIO(), threading.Event())
        finally:
            nim.one_turn = real

    def test_in_band_503_is_retried_then_succeeds(self):
        out = self.drive([nim.Overloaded('503 Service temporarily overloaded'),
                          nim.Overloaded('503 again'), ('ok', [], 'stop', {})])
        self.assertEqual(out[0], 'ok')
        self.assertEqual(self.calls, 3)

    def test_gives_up_after_the_last_wait(self):
        with self.assertRaises(nim.Overloaded):
            self.drive([nim.Overloaded('503')] * 4)
        self.assertEqual(self.calls, 4)     # one try per wait, plus the final one

    def test_a_429_waits_longer_than_a_503(self):
        import io
        import threading
        import urllib.error
        waits = []
        real_wait, real_cool = threading.Event.wait, nim.cool_down
        threading.Event.wait = lambda self, t=None: waits.append(t) or False
        held = []
        nim.cool_down = held.append                # the shared hold is tested on its own below
        nim.RETRY_WAITS = (5, 15, 45)              # real ladder; the patched wait never sleeps
        try:
            self.drive([urllib.error.HTTPError('u', 429, 'Too Many', {}, None),
                        nim.Overloaded('503'), ('ok', [], 'stop', {})])
        finally:
            threading.Event.wait, nim.cool_down = real_wait, real_cool
        self.assertEqual(self.calls, 3)
        self.assertEqual(waits, [5 * nim.RATE_LIMIT_FACTOR, 15])
        self.assertEqual(held, [5 * nim.RATE_LIMIT_FACTOR])   # only the 429 holds the others

    def test_a_retry_after_beyond_the_budget_fails_the_call_at_once(self):
        # Token Harbor, weekly allowance gone: 429 with Retry-After 3600; the runner sat on it
        import email.message
        import io
        import urllib.error
        headers = email.message.Message()
        headers['Retry-After'] = '3600'
        body = io.BytesIO(b'{"error": {"code": "free_tier_limit_reached"}}')
        with self.assertRaises(nim.AgentFailed) as caught:
            self.drive([urllib.error.HTTPError('u', 429, 'Too Many', headers, body)])
        self.assertEqual(self.calls, 1)
        self.assertIn('3600s wait', str(caught.exception))
        self.assertIn('free_tier_limit_reached', str(caught.exception))

    def test_a_429_holds_every_thread_back(self):
        import time
        nim._next_start[0] = 0.0
        nim.cool_down(30)
        self.assertGreaterEqual(nim._next_start[0], time.monotonic() + 29)
        nim.cool_down(5)                       # a shorter hold never releases a longer one
        self.assertGreaterEqual(nim._next_start[0], time.monotonic() + 29)
        nim._next_start[0] = 0.0

    def test_spacing_widens_on_429_and_narrows_on_success(self):
        nim.MIN_INTERVAL = 2.5                    # setUp zeroes it; the floor matters here
        nim._interval[0] = nim.MIN_INTERVAL
        nim.cool_down(0)
        self.assertAlmostEqual(nim._interval[0], nim.MIN_INTERVAL * 1.5)
        for _ in range(40):
            nim.cool_down(0)
        self.assertEqual(nim._interval[0], nim.MAX_INTERVAL)       # capped
        for _ in range(200):
            nim.accepted()
        self.assertEqual(nim._interval[0], nim.MIN_INTERVAL)       # floored
        nim._next_start[0] = 0.0

    def test_throttle_spaces_request_starts(self):
        real = nim.MIN_INTERVAL
        nim.MIN_INTERVAL, nim._interval[0], nim._next_start[0] = 0.05, 0.05, 0.0
        try:
            import time
            t = time.monotonic()
            for _ in range(4):
                nim.throttle()
            self.assertGreaterEqual(time.monotonic() - t, 0.14)
        finally:
            nim.MIN_INTERVAL, nim._next_start[0] = real, 0.0

    def test_a_bad_key_is_not_retried(self):
        import urllib.error
        err = urllib.error.HTTPError('u', 401, 'Unauthorized', {}, None)
        with self.assertRaises(urllib.error.HTTPError):
            self.drive([err])
        self.assertEqual(self.calls, 1)

    def test_an_error_data_line_raises_overloaded(self):
        import io
        import threading

        class Response:
            status = 200
            fp = None

            def __init__(self):
                self.lines = [b'data: {"error":{"message":"Service temporarily overloaded",'
                              b'"type":"service_unavailable","code":503}}\n']

            def __iter__(self):
                return iter(self.lines)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        real, nim.urllib.request.urlopen = nim.urllib.request.urlopen, lambda *a, **k: Response()
        real_key, nim.api_key = nim.api_key, lambda: 'k'
        try:
            with self.assertRaises(nim.Overloaded) as caught:
                nim.one_turn(request(tempfile.mkdtemp(prefix='nim-')), [], io.StringIO(),
                             threading.Event())
        finally:
            nim.urllib.request.urlopen, nim.api_key = real, real_key
        self.assertIn('overloaded', str(caught.exception))


class Nudge(unittest.TestCase):
    """A final answer with no tool call behind it is pushed back once, then taken as given."""

    ENVELOPE = ('done\n```json\n{"status": "success", "summary": "%s", "artifacts": ["server.js"], '
                '"notes_for_next_agent": "n", "code": "x", "decisions": [], "risks": []}\n```')

    def drive(self, turns):
        import queue
        import threading
        script = iter(turns)
        seen = []

        def fake(req, history, raw, cancel):
            seen.append([m['role'] for m in history])
            return next(script)
        real, nim.one_turn = nim.one_turn, fake
        nim.MIN_INTERVAL, interval = 0.0, nim.MIN_INTERVAL
        nim._interval[0], nim._next_start[0] = 0.0, 0.0
        try:
            proposal, extra = nim.invoke(request(tempfile.mkdtemp(prefix='nim-')), queue.Queue(),
                                         threading.Event())
        finally:
            nim.one_turn, nim.MIN_INTERVAL = real, interval
            nim._interval[0] = interval
        return proposal, seen

    def test_tool_free_claim_is_pushed_back_once(self):
        lie = self.ENVELOPE % 'applied two fixes'
        truth = self.ENVELOPE % 'really wrote it'
        write = {'id': 'c1', 'name': 'write_file',
                 'arguments': '{"path": "server.js", "content": "ok"}'}
        proposal, seen = self.drive([(lie, [], 'stop', {}), ('', [write], 'tool_calls', {}),
                                     (truth, [], 'stop', {})])
        self.assertEqual(proposal.summary, 'really wrote it')
        self.assertEqual(seen[1][-1], 'user')          # the nudge went in as a user turn
        self.assertEqual(len(seen), 3)

    def test_repeated_tool_free_answers_are_a_failure_not_a_result(self):
        # the size-5 prototype lied twice, nudge included; the harness must see a failure
        lie = self.ENVELOPE % 'still no tools'
        with self.assertRaises(nim.AgentFailed) as caught:
            self.drive([(lie, [], 'stop', {})] * (nim.NUDGES + 1))
        self.assertIn('without calling a single tool', str(caught.exception))

    def test_a_reply_cut_at_the_output_cap_is_pushed_back_not_fatal(self):
        # nemotron on spec 20: 1,920 lines on disk, one over-long reply, the whole turn lost
        write = {'id': 'c1', 'name': 'write_file', 'arguments': '{"path": "a.html", "content": "<p>"}'}
        proposal, seen = self.drive([('', [write], 'tool_calls', {}),
                                     ('half a file...', [], 'length', {}),
                                     (self.ENVELOPE % 'finished in pieces', [], 'stop', {})])
        self.assertEqual(proposal.summary, 'finished in pieces')
        self.assertEqual(seen[2][-1], 'user', 'the cut-off note went in as a user turn')

    def test_a_narrated_next_step_after_tool_work_is_pushed_back(self):
        # deepseek wrote seven parts, said "Now swimlanes, assignees, ..." and stopped
        write = {'id': 'c1', 'name': 'write_file', 'arguments': '{"path": "a.html", "content": "<p>"}'}
        more = {'id': 'c2', 'name': 'write_file', 'arguments': '{"path": "a.html", "content": "</p>", "append": true}'}
        proposal, seen = self.drive([('', [write], 'tool_calls', {}),
                                     ('Now swimlanes, assignees, exporter.', [], 'stop', {}),
                                     ('', [more], 'tool_calls', {}),
                                     (self.ENVELOPE % 'finished', [], 'stop', {})])
        self.assertEqual(proposal.summary, 'finished')
        self.assertEqual(len(seen), 4)
        self.assertEqual(seen[2][-1], 'user', 'the push-back went in as a user turn')


class Envelope(unittest.TestCase):
    def test_a_bad_tool_name_comes_back_as_text_the_model_can_read(self):
        req = request(tempfile.mkdtemp(prefix='nim-'))
        self.assertIn('no such tool', nim.run_tool(req, 'delete_everything', {}))

    def test_tool_events_reach_the_gui_queue(self):
        from adw_modules.runners.common import tool_event
        q = queue.Queue()
        tool_event(q, request(tempfile.mkdtemp(prefix='nim-')), 'write_file', {})
        agent, event = q.get_nowait()
        self.assertEqual(agent, 'probe')
        self.assertEqual(event['step_update']['tool_name'], 'write_file')

    def test_an_envelope_with_an_unescaped_quote_keeps_its_status_and_summary(self):
        # F9: deepseek and glm quote a test name inside the summary, one owner in five
        from adw_modules.runners.common import deliver
        req = request(tempfile.mkdtemp(prefix='nim-'))
        reply = ('Done.\n```json\n{"status": "success", "summary": "the block passes "test_move" and '
                 '"test_wip"", "code": "", "decisions": ["kept"], "risks": []}\n```')
        proposal, _ = deliver(req, reply, {'total_tokens': 1})
        self.assertEqual(proposal.status, 'success')
        self.assertIn('test_move', proposal.summary)
        self.assertEqual(proposal.code, '')


if __name__ == '__main__':
    unittest.main()
