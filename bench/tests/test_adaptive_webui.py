"""Read-only adaptive dashboard: missing telemetry, hostile text and comparisons."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from swarm_workbench import webui


class AdaptiveWebUI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'trace.db'
        self.history = self.root / 'history.json'
        self.env = patch.dict('os.environ', {'SWARM_LEARNING_PATH': str(self.history)})
        self.env.start()
        self.addCleanup(self.env.stop)
        webui._fixture(self.db)

    def event(self, name, payload):
        with closing(sqlite3.connect(self.db)) as con:
            con.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (name, 'run1', '', '', 'log', name, json.dumps(payload),
                         None, webui.now().isoformat(), None))
            con.commit()

    def test_unknown_budget_and_escaped_allocation_without_history(self):
        self.event('adaptive_plan', {'runner': 'free-cli', 'model': '<script>bad()</script>',
                                    'selected_agents': 3, 'difficulty': 'hard',
                                    'reasons': ['<img src=x onerror=bad()>'],
                                    'token_limit': 12000, 'output_limit': 1500,
                                    'idle_timeout_seconds': 30, 'call_timeout_seconds': 90,
                                    'source_runs': ['old-run'],
                                    'skills': [{'role': 'reviewer', 'name': '<b>review</b>'}]})
        with closing(webui.connect(self.db)) as con:
            run = webui.load(con, 'run1')
            body, _ = webui.render_threads(con, 'run1', 30)
            listing, _ = webui.render_swarms(con, 25, 30)
        self.assertFalse(run['budget']['known'])
        self.assertIsNone(run['budget']['used'])
        self.assertIn('Token budget remaining unknown', body)
        self.assertIn('Allocation: 3 agents / hard', body)
        self.assertIn('Learning sources: old-run', body)
        self.assertIn('&lt;img src=x onerror=bad()&gt;', body)
        self.assertIn('reviewer: &lt;b&gt;review&lt;/b&gt;', body)
        self.assertNotIn('<script>bad()', body + listing)
        self.assertIn('free-cli', listing)
        self.assertIn('Usage unknown; 12,000 token cap declared', listing)

    def test_known_budget_and_stopped_stalled_run(self):
        self.event('adaptive_plan', {'token_limit': 12000, 'selected_agents': 2})
        self.event('budget_status', {'tokens_known': True, 'tokens_used': 4000,
                                     'tokens_remaining': 8000})
        self.event('swarm_stop', {'stop_reason': 'idle_timeout'})
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("UPDATE events SET started_at='2020-01-01T00:00:00+00:00'")
            con.commit()
        with closing(webui.connect(self.db)) as con:
            run = webui.load(con, 'run1')
            body, _ = webui.render_threads(con, 'run1', 30)
            listing, _ = webui.render_swarms(con, 25, 30)
        self.assertTrue(run['budget']['known'])
        self.assertEqual(run['budget']['used'], 4000)
        self.assertAlmostEqual(run['budget']['pct'], 100 / 3)
        self.assertIn('8.0k tok left of 12.0k', body)
        self.assertIn('Trace silent for over 120s', body)
        self.assertIn('Stop reason: idle_timeout', body)
        self.assertIn('Stopped: idle_timeout', listing)

    def test_history_is_bounded_and_comparisons_require_same_task_and_quality(self):
        records = [dict(run_id=run_id, spec_hash=task, quality_kind=quality,
                        accepted=True, tokens=None, elapsed_seconds=12,
                        init_seconds=2, model='<img src=x>', source='live')
                   for run_id, task, quality in [('run1', 'task-a', 'acceptance'),
                                                  ('peer-run', 'task-a', 'acceptance'),
                                                  ('other-task', 'task-b', 'acceptance'),
                                                  ('weak-quality', 'task-a', 'smoke')]]
        self.history.write_text(json.dumps(records), encoding='utf-8')
        with closing(webui.connect(self.db)) as con:
            body, _ = webui.render_threads(con, 'run1', 30)
            self.assertEqual(len(webui.learning_history(con)), 4)
            self.history.write_text('x' * (2 * 1024 * 1024 + 1), encoding='utf-8')
            self.assertEqual(webui.learning_history(con), [])
            self.history.write_text('{partial', encoding='utf-8')
            self.assertEqual(webui.learning_history(con), [])
        self.assertIn('peer-run', body)
        self.assertNotIn('other-task', body)
        self.assertNotIn('weak-quality', body)
        self.assertIn('Same task hash and quality criterion', body)
        self.assertIn('&lt;img src=x&gt;', body)
        self.assertNotIn('<img src=x>', body)

    def test_compare_renders_grouped_rows_and_absent_values(self):
        with closing(sqlite3.connect(self.db)) as con:
            con.execute('UPDATE sessions SET status=?, ended_at=? WHERE adw_id=?',
                        ('success', '2026-09-10T12:00:12+00:00', 'run1'))
            con.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)',
                        ('run2', 'baseline', 'merge intervals', 'success', 'eng',
                         '2026-09-10T13:00:00+00:00', '2026-09-10T13:00:24+00:00',
                         100, 0.0, 0))
            con.execute('INSERT INTO phases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        ('p4', 'run2', 1, 'acceptance', 'code', 'tests', 'd', 'success',
                         1, 0, None, '2026-09-10T13:00:00+00:00', '2026-09-10T13:00:24+00:00'))
            con.execute('INSERT INTO agent_sessions VALUES (?,?,?,?,?,?,?,?,?,?)',
                        ('run2', 'baseline', 'agy', 'safe-model', '#22d3ee', '', 0, 0,
                         '2026-09-10T13:00:00+00:00', '2026-09-10T13:00:24+00:00'))
            con.execute("UPDATE events SET payload_json=? WHERE event_id='e0'",
                        (json.dumps({'output_file': 'result.html', 'runner': 'safe-runner',
                                     'model': 'safe-model'}),))
            contracts = [('run2-contract', 'run2', {'output_file': 'result.html', 'runner': 'safe-runner',
                                                     'model': 'safe-model'})]
            for event_id, run_id, payload in contracts:
                con.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (event_id, run_id, '', '', 'run_contract', 'acceptance',
                             json.dumps(payload), None, '2026-09-10T13:00:00+00:00', None))
            for event_id, kind, name, payload in (
                    ('run2-plan', 'log', 'adaptive_plan', {'spec_hash': 'same-brief',
                                                            'runner': 'safe-runner', 'model': 'safe-model'}),
                    ('run2-gate', 'gate_pass', 'acceptance', {}),
                    ('run2-ui', 'ui_gate', 'assembled', {'panels': 4, 'empty_panels': ['history']})):
                con.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (event_id, 'run2', '', '', kind, name, json.dumps(payload), None,
                             '2026-09-10T13:00:24+00:00', None))
            con.commit()
        self.history.write_text(json.dumps([
            {'run_id': 'run1', 'spec_hash': 'same-brief', 'elapsed_seconds': 12,
             'agents': 2, 'accepted': True, 'tokens': None, 'stop_reason': '<late>'},
            {'run_id': 'run2', 'spec_hash': 'same-brief', 'elapsed_seconds': 24,
             'agents': 1, 'accepted': True, 'tokens': 100, 'stop_reason': ''},
        ]), encoding='utf-8')
        with closing(webui.connect(self.db)) as con:
            body, _title, live = webui.route(con, '/compare', {}, 30, 25)
        self.assertFalse(live)
        self.assertIn('output: result.html', body)
        self.assertIn('2 runs', body)
        self.assertIn('href="/threads?run=run1"', body)
        self.assertIn('FASTEST', body)
        self.assertIn('1/1', body)
        self.assertIn('3/4', body)
        self.assertIn('unknown', body)
        self.assertIn('<td>-</td>', body)
        self.assertIn('&lt;late&gt;', body)
        self.assertNotIn('<late>', body)


if __name__ == '__main__':
    unittest.main()
