"""The browser gate: an empty panel and a mount error are found, a drawn page passes."""
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'adws'))
from adw_modules import agy_swarm, ui_gate  # noqa: E402

PAGE = '''<!doctype html><html><head><title>t</title></head><body>
<nav><button data-tab="a">A</button><button data-tab="b">B</button></nav>
<section id="panel-a"><h2>A</h2></section><section id="panel-b"><h2>B</h2></section>
<script>%s</script></body></html>'''


class Gate(unittest.TestCase):
    def setUp(self):
        if not ui_gate.browser():
            raise unittest.SkipTest('no headless browser on this machine')
        self.dir = Path(tempfile.mkdtemp(prefix='ui-'))

    def page(self, script):
        p = self.dir / 'x.html'
        p.write_text(PAGE % script, encoding='utf-8')
        return p

    def test_a_drawn_page_passes(self):
        started = time.perf_counter()
        v = ui_gate.judge(self.page(
            'document.getElementById("panel-a").innerHTML += "<button>go</button>";'
            'document.getElementById("panel-b").innerHTML += "<p>some content to read here</p>";'))
        self.assertLess(time.perf_counter() - started, 10)
        self.assertTrue(v['ok'], v)
        self.assertEqual(v['empty_panels'], [])

    def test_an_empty_panel_and_a_throw_are_named(self):
        v = ui_gate.judge(self.page(
            'document.getElementById("panel-a").innerHTML += "<button>go</button>";'
            'throw new Error("mount broke");'))
        self.assertFalse(v['ok'])
        self.assertEqual(v['empty_panels'], ['b'])
        self.assertTrue(any('mount broke' in e for e in v['errors']), v['errors'])
        defects = agy_swarm.ui_defects(agy_swarm.page_verdict(self.dir / 'x.html', '.html'))
        self.assertTrue(any(d.startswith('ui: page throws') for d in defects), defects)
        self.assertTrue(any(d.startswith('part:b - the panel renders EMPTY') for d in defects), defects)

    def test_a_hidden_application_is_rejected(self):
        v = ui_gate.judge(self.page(
            'document.body.style.display = "none";'))
        self.assertFalse(v['ok'], v)

    def test_a_disabled_control_does_not_fill_a_panel(self):
        v = ui_gate.judge(self.page(
            'document.getElementById("panel-a").innerHTML += "<button disabled>go</button>";'
            'document.getElementById("panel-b").innerHTML += "<p>some content to read here</p>";'))
        self.assertFalse(v['ok'], v)
        self.assertIn('a', v['empty_panels'])

    def test_a_visible_empty_panel_is_rejected(self):
        v = ui_gate.judge(self.page(
            'document.getElementById("panel-b").innerHTML += "<p>some content to read here</p>";'))
        self.assertFalse(v['ok'], v)
        self.assertIn('a', v['empty_panels'])

    def test_an_offscreen_control_does_not_fill_a_panel(self):
        v = ui_gate.judge(self.page(
            'document.getElementById("panel-a").innerHTML += '
            '"<button style=\\"position:fixed;left:-10000px\\">go</button>";'
            'document.getElementById("panel-b").innerHTML += "<p>some content to read here</p>";'))
        self.assertFalse(v['ok'], v)
        self.assertIn('a', v['empty_panels'])

    def test_hidden_inactive_tabs_do_not_fail_the_current_panel(self):
        page = self.page(
            'document.getElementById("panel-a").innerHTML += "<button>go</button>";'
            'document.getElementById("panel-b").innerHTML += "<p>some content to read here</p>";'
            'document.getElementById("panel-b").hidden = true;'
            'document.querySelectorAll("[data-tab]").forEach(tab => tab.onclick = () => {'
            'document.getElementById("panel-a").hidden = tab.dataset.tab !== "a";'
            'document.getElementById("panel-b").hidden = tab.dataset.tab !== "b";});')
        scenario = ('document.querySelector("[data-tab=b]").click(); '
                    'await new Promise(r => setTimeout(r, 50)); '
                    'return {steps: [{name: "switch tabs", ok: !document.getElementById("panel-b").hidden}]};')
        v = ui_gate.judge(page, scenario)
        self.assertTrue(v['ok'], v)
        self.assertEqual([p['panel'] for p in v['panels']], ['b', 'a'])

    def test_a_scenario_step_that_fails_is_a_defect(self):
        page = self.page('document.getElementById("panel-a").innerHTML += "<button id=go>go</button>";'
                         'document.getElementById("panel-b").innerHTML += "<p>content to read here</p>";'
                         'window.hits = 0; document.getElementById("go").onclick = () => window.hits++;')
        scenario = ('document.getElementById("go").click(); await new Promise(r => setTimeout(r, 50));'
                    'return {steps: [{name: "go counts a click", ok: window.hits === 1},'
                    '{name: "go counts twice", ok: window.hits === 2, note: "only " + window.hits}]};')
        v = ui_gate.judge(page, scenario)
        self.assertFalse(v['ok'])
        self.assertEqual([s['name'] for s in v['failed_steps']], ['go counts twice'])
        defects = agy_swarm.ui_defects(agy_swarm.page_verdict(page, '.html', scenario))
        self.assertTrue(any('cannot "go counts twice" (only 1)' in d for d in defects), defects)

    def test_not_a_page_is_not_judged(self):
        self.assertIsNone(agy_swarm.page_verdict(self.dir / 'x.py', '.py'))


if __name__ == '__main__':
    unittest.main()
