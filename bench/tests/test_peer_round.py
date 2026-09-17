"""A round must end even when an in-process agent never returns.

The nim runner has no child process for watch() to kill; the size-1 prototype of 2026-09-15
sat inside a pipe read for an hour and the whole sweep waited with it.
"""
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'adws'))
from adw_modules import agy_swarm, session  # noqa: E402
from adw_modules.data_types import ConfigDefaults, ObservabilityConfig, SSSFConfig  # noqa: E402


class Abandon(unittest.TestCase):
    def setUp(self):
        data = ROOT / 'work' / 'dryrun-peer'
        data.mkdir(parents=True, exist_ok=True)
        cfg = SSSFConfig(defaults=ConfigDefaults(data_dir='work/dryrun-peer'),
                         observability=ObservabilityConfig(db=str(data / 'sssf.db')))
        self.run = session.ensure(cfg)
        self.release = threading.Event()
        self.real_invoke, self.real_grace = agy_swarm.invoke, agy_swarm.ABANDON_GRACE
        agy_swarm.ABANDON_GRACE = 0

        def fake(req, messages, cancel):
            req.folder.mkdir(parents=True, exist_ok=True)
            if req.agent == 'stuck':
                self.release.wait()          # a pipe nobody closes
                raise agy_swarm.AgentFailed('stuck woke up late', 0)
            proposal = agy_swarm.Proposal.model_validate(agy_swarm.coerce_proposal(
                {'status': 'success', 'summary': 'fine', 'code': 'x', 'decisions': [], 'risks': []}))
            return proposal, {'usage': {'total_tokens': 7}}
        agy_swarm.invoke = fake

    def tearDown(self):
        self.release.set()                   # let the worker thread finish before exit
        agy_swarm.invoke, agy_swarm.ABANDON_GRACE = self.real_invoke, self.real_grace

    def test_an_agent_that_never_returns_is_abandoned_after_its_timeout(self):
        folder = Path(self.run.session_dir)
        reqs = [agy_swarm.AgentRequest(agent=a, prompt='p', folder=folder / a, timeout=1,
                                       runner='nim', model='m') for a in ('fine', 'stuck')]
        t = time.monotonic()
        outputs = agy_swarm.peer_round(self.run, reqs, 1, gate=False)
        self.assertLess(time.monotonic() - t, 15)
        self.assertIn('fine', outputs)
        self.assertNotIn('stuck', outputs)
        # casualties leave run.phases on purpose; the trace keeps them
        rows = self.run.tracer.conn.execute(
            "select payload_json from events where adw_id=? and type='error'",
            (self.run.adw_id,)).fetchall()
        self.assertTrue(any('abandoned' in r[0] for r in rows), rows)


if __name__ == '__main__':
    unittest.main()


class Orphans(unittest.TestCase):
    def test_what_a_cli_leaves_running_dies_when_the_harness_lets_it_go(self):
        # F10 on the CLI path: cline exits normally, the `node server.js` its agent started
        # does not; terminate() after a normal exit must still take the tree down
        import os
        import subprocess
        import tempfile
        folder = Path(tempfile.mkdtemp(prefix='orphan-'))
        pidfile = folder / 'pid.txt'
        req = agy_swarm.AgentRequest('cli', 'x', folder, runner='opencode')
        with (folder / 'err.log').open('w') as err:
            proc = agy_swarm.spawn([sys.executable, '-c',
                                    'import subprocess,sys; p = subprocess.Popen([sys.executable, "-c", '
                                    '"import time; time.sleep(60)"]); open(%r, "w").write(str(p.pid))'
                                    % str(pidfile)], req, err)
            proc.wait(timeout=30)
        pid = int(pidfile.read_text())
        self.assertEqual(proc.returncode, 0)
        agy_swarm.terminate(proc)
        time.sleep(0.5)
        if os.name == 'nt':
            listed = subprocess.run(['tasklist', '/FI', 'PID eq %d' % pid, '/NH'],
                                    capture_output=True, text=True).stdout
            self.assertNotIn(str(pid), listed, 'the orphan survived terminate()')
        else:
            with self.assertRaises(OSError):
                os.kill(pid, 0)


class Skeleton(unittest.TestCase):
    SK = ('<script>\n// part:a\nA();\n// /part:a\n// part:b\nB();\n// /part:b\n'
          '// part:c\nC();\n// /part:c\nmount();\n</script>')

    def test_a_full_roster_keeps_the_spec_blocks(self):
        self.assertEqual(agy_swarm.regroup_skeleton(self.SK, ['a', 'b', 'c']), self.SK)

    def test_a_reduced_roster_gets_contiguous_groups_named_after_its_owners(self):
        # spec 20 at size 5: twenty feature stubs become five blocks of four
        out = agy_swarm.regroup_skeleton(self.SK, ['x', 'y'])
        spans = agy_swarm.part_spans(out)
        self.assertEqual(sorted(spans), ['x', 'y'])
        self.assertIn('A();', out[slice(*spans['x'])])
        self.assertIn('B();', out[slice(*spans['x'])])
        self.assertIn('C();', out[slice(*spans['y'])])
        self.assertNotIn('part:a', out)
        self.assertIn('mount();', out)                  # the frozen tail is untouched
