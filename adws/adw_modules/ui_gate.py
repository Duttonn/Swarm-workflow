"""Does the page a builder shipped have a working control behind every tab?

The fixed suites call pure model functions in node and cannot see that fourteen of sixteen
panels render empty (big-pickle's kanban, 18/18 on the suite) or that the New Note button
throws (big-pickle's notebook, 14/14). This gate loads the page in a headless browser, lets
the mount script run, and reads what a person would find: per panel, how many controls, how
much text, and whether the page threw while mounting.

The CLI is bench/ui_gate.py; the swarm calls judge() on the assembled file before the finisher
and on every candidate. Needs Edge or Chrome; without one the judge is skipped.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

BROWSERS = (r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
            r'C:\Program Files\Google\Chrome\Application\chrome.exe',
            '/usr/bin/chromium', '/usr/bin/google-chrome')
# Injected before the page's own scripts: records uncaught errors on the document so they
# survive into --dump-dom, which has no console; and answers alert/confirm/prompt, since a
# blocking dialog hangs the headless dump until its timeout.
HOOK = ('<script>window.__errs=[];window.alert=function(){};window.confirm=function(){return true};'
        'window.prompt=function(){return "scenario"};window.addEventListener("error",function(e){window.__errs.push('
        'String(e.message).slice(0,200));document.documentElement.setAttribute("data-errs",'
        'JSON.stringify(window.__errs))});</script>')

# `--dump-dom` serializes markup, not the rendered page.  Inspect in the browser after mount
# and any scenario instead: CSS and geometry are the only reliable way to tell whether a person
# can see and use a panel's contents.
INSPECTOR = '''<script>(function(){
var queuedAt = performance.now();
function rendered(el) {
  for (var node = el; node && node.nodeType === 1; node = node.parentElement) {
    var style = getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse' ||
        Number(style.opacity) === 0 || node.hidden || node.getAttribute('aria-hidden') === 'true') return false;
  }
  return el.getClientRects().length > 0;
}
function inViewport(rect) {
  return rect.width > 0 && rect.height > 0 && rect.right > 0 && rect.bottom > 0 &&
    rect.left < window.innerWidth && rect.top < window.innerHeight;
}
function visible(el) {
  return rendered(el) && Array.prototype.some.call(el.getClientRects(), inViewport);
}
function visibleText(panel, heading) {
  var chars = 0;
  var walker = document.createTreeWalker(panel, NodeFilter.SHOW_TEXT);
  for (var text = walker.nextNode(); text; text = walker.nextNode()) {
    if (!text.nodeValue.trim() || (heading && heading.contains(text.parentElement))) continue;
    var parent = text.parentElement;
    if (!parent || parent.closest('button,input,select,textarea,svg,canvas') || !visible(parent)) continue;
    var range = document.createRange(); range.selectNodeContents(text);
    if (Array.prototype.some.call(range.getClientRects(), inViewport)) chars += text.nodeValue.replace(/\\s+/g, ' ').trim().length;
  }
  return chars;
}
function inspect() {
  var startedAt = performance.now();
  var panels = [], seen = {};
  function collectShownPanels() {
    Array.prototype.forEach.call(document.querySelectorAll('section[id^="panel-"]'), function(panel) {
      if (!rendered(panel) || seen[panel.id]) return;
      // A tab panel is evaluated only while its tab has made it visible.
      seen[panel.id] = true;
      var controls = 0;
      Array.prototype.forEach.call(panel.querySelectorAll('button,input,select,textarea,svg,canvas'), function(control) {
        if (!control.matches(':disabled') && visible(control)) controls++;
      });
      var chars = visibleText(panel, panel.querySelector('h1,h2,h3,h4,h5,h6'));
      panels.push({panel: panel.id.slice(6), controls: controls, chars: chars,
                   empty: controls === 0 && chars < 12});
    });
  }
  collectShownPanels();
  // Inactive panels normally have display:none.  Show each tab first so the gate measures
  // its own content rather than treating an intentionally hidden tab as a dead panel.
  Array.prototype.forEach.call(document.querySelectorAll('[data-tab]'), function(tab) {
    if (!visible(tab) || tab.matches(':disabled')) return;
    var target = document.getElementById('panel-' + tab.getAttribute('data-tab'));
    if (!target) return;
    // Let a tab's click handler reveal its panel, but prevent a link's native navigation
    // from replacing the page while it is being inspected.
    var preventDefault = function(event) { event.preventDefault(); };
    tab.addEventListener('click', preventDefault, true);
    try { tab.click(); } finally { tab.removeEventListener('click', preventDefault, true); }
    if (target && rendered(target)) collectShownPanels();
    else if (target && !seen[target.id]) {
      // The tab did not reveal its panel.  Its DOM cannot stand in for a panel a person can see.
      seen[target.id] = true;
      panels.push({panel: target.id.slice(6), controls: 0, chars: 0, empty: true});
    }
  });
  var tabs = 0;
  Array.prototype.forEach.call(document.querySelectorAll('[data-tab]'), function(tab) { if (visible(tab)) tabs++; });
  document.documentElement.setAttribute('data-ui-gate', JSON.stringify({
    panels: panels, tabs: tabs, queued_ms: Math.round(startedAt - queuedAt),
    inspect_ms: Math.round(performance.now() - startedAt)
  }));
}
// Seed starts at 400 ms and a scenario starts at 900 ms.  Waiting just past that startup
// point keeps the inspector out of the virtual-time tail that made a simple page hang.
setTimeout(inspect, 1200);
})();</script>'''


def browser():
    for candidate in BROWSERS:
        if Path(candidate).is_file():
            return candidate
    return None


# A fresh page has no data, and a categories or outline panel with nothing to show is not a
# defect. Each brief's seed goes through the App.store contract every build must honour, a
# beat after mount, so the panels are judged with something on the board.
SEEDS = {
    'budget-ledger': "App.store.set({transactions: [{id: 's1', date: '2026-03-01', description: 'Coffee beans', category: 'Food', amount: -12.4}, {id: 's2', date: '2026-03-05', description: 'Salary', category: 'Income', amount: 2500}, {id: 's3', date: '2026-02-10', description: 'Rent', category: 'Housing', amount: -900}], budgets: {Food: 100}})",
    'markdown-notebook': "App.store.set({notes: [{id: 'n1', title: 'Alpha plan', body: '# Plan' + String.fromCharCode(10, 10) + 'This is **bold**. See [[Beta notes]] #project', created: 1772000000000, updated: 1772000000000, pinned: false}, {id: 'n2', title: 'Beta notes', body: '## Section' + String.fromCharCode(10, 10) + 'Back to [[Alpha plan]] #project', created: 1772100000000, updated: 1772100000000, pinned: true}], ui: {current: 'n1', query: ''}})",
    'spreadsheet': "App.store.set({sheets: [{id: 's1', name: 'Sheet1', cells: {A1: {raw: '10'}, A2: {raw: '20'}, A3: {raw: '=A1+A2'}, B1: {raw: 'ab'}, B2: {raw: '2026-03-01'}, B3: {raw: 'hello world'}, D1: {raw: 'Jan'}, E1: {raw: '3'}, D2: {raw: 'Feb'}, E2: {raw: '5'}, D3: {raw: 'Mar'}, E3: {raw: '2'}}, formats: {}, rules: {}, conditional: []}, {id: 's2', name: 'Sheet2', cells: {A1: {raw: '7'}, A2: {raw: '=Sheet1!A3'}}, formats: {}, rules: {}, conditional: []}], active: 's1', names: {total: 'Sheet1!A1:A2'}})",
    'kanban-board': "App.store.set({columns: [{id: 'todo', name: 'To do', wip: null}, {id: 'doing', name: 'Doing', wip: 2}, {id: 'done', name: 'Done', wip: null}], cards: [{id: 'k1', title: 'Write spec', description: 'draft', tags: ['docs'], due: '2026-03-10', assignee: 'ana', column: 'todo', created: '2026-03-01', done: null, archived: false}, {id: 'k2', title: 'Fix bug', description: '', tags: ['bug'], due: '2026-03-04', assignee: 'ben', column: 'doing', created: '2026-03-02', done: null, archived: false}, {id: 'k3', title: 'Ship', description: '', tags: [], due: null, assignee: null, column: 'done', created: '2026-02-21', done: '2026-02-27', archived: false}, {id: 'k4', title: 'Old', description: '', tags: [], due: null, assignee: 'ana', column: 'done', created: '2026-01-10', done: '2026-01-12', archived: true}]})",
}


def seed_script(page):
    key = next((k for k in SEEDS if k in Path(page).name), None)
    if not key:
        return ''
    return ('<script>setTimeout(function(){try{if(window.App&&App.store){%s}}catch(e){window.__errs.push('
            '"seed: "+String(e.message).slice(0,160));document.documentElement.setAttribute("data-errs",'
            'JSON.stringify(window.__errs))}},400);</script>' % SEEDS[key])


def scenario_script(scenario):
    """A spec's own click scenario (its `ui_scenario`, async JS returning {steps:[{name, ok,
    note}]}), run a beat after the seed. It is the part of a brief the suite cannot state: that
    Undo undoes, that an invalid import is reported. Its result rides on the document too."""
    if not scenario:
        return ''
    # Raced against a timer: a page whose render loops forever must still yield the steps
    # that ran before it (a scenario that pushes to window.__ui_steps keeps them).
    return ('<script>window.__ui_steps=[];setTimeout(function(){var done=false;'
            'var finish=function(r){if(done)return;done=true;document.documentElement.setAttribute("data-ui",JSON.stringify(r||{}))};'
            'setTimeout(function(){finish({steps:(window.__ui_steps||[]).concat([{name:"scenario timed out after 8 s",ok:false,note:""}])})},8000);'
            '(async function(){%s})().then(finish).catch(function(e){finish({steps:(window.__ui_steps||[]).concat('
            '[{name:"scenario threw",ok:false,note:String(e&&e.message||e).slice(0,160)}])})})},900);</script>' % scenario)


def dump(page, scenario=None):
    """The DOM after load, mount, seed and scenario, with the error hook at the top of <head>."""
    raw = Path(page).read_text(encoding='utf-8', errors='replace')
    hooked = re.sub(r'(<head[^>]*>)', r'\1' + HOOK, raw, count=1, flags=re.I) if re.search(r'<head', raw, re.I) \
        else HOOK + raw
    seed = seed_script(page) + scenario_script(scenario) + INSPECTOR
    hooked = hooked.replace('</body>', seed + '</body>', 1) if '</body>' in hooked else hooked + seed
    with tempfile.TemporaryDirectory(prefix='ui-gate-') as tmp:
        copy = Path(tmp) / Path(page).name
        copy.write_text(hooked, encoding='utf-8')
        argv = [browser(), '--headless=new', '--disable-gpu', '--no-sandbox',
                '--virtual-time-budget=12000', '--user-data-dir=' + str(Path(tmp) / 'profile'),
                '--dump-dom', copy.as_uri()]
        # A page is judged once per candidate. What bounds the inspector is its own
        # deadline above, not a tight wall clock here: a cold first launch on Windows
        # takes far longer than a warm one (measured at HEAD, same page, same process:
        # 60s, then 1.8s, then 1.5s), so the first attempt is cut short and the retry
        # gets the real ceiling.
        limit = int(os.environ.get('SWARM_GATE_TIMEOUT', '300'))
        for attempt in (1, 2):
            try:
                # A warm launch judges a page in under two seconds, so a first attempt that has
                # not come back in thirty is a cold browser, not a slow page: cut it short and
                # spend the real ceiling on the attempt that can succeed.
                done = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8',
                                      errors='replace',
                                      timeout=min(limit, 30) if attempt == 1 else limit)
                break
            except subprocess.TimeoutExpired:
                if attempt == 2:
                    raise
                # The first launch on a cold machine warms the browser even when it does not
                # return in time; the second is the one that judges the page.
                print('ui gate: first browser launch timed out after %ss, retrying once' % limit,
                      file=sys.stderr)
    return done.stdout


def unescape(attr):
    return attr.replace('&quot;', '"').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')


def judge(page, scenario=None):
    dom = dump(page, scenario)
    errs = re.search(r'data-errs="([^"]*)"', dom)
    errors = json.loads(unescape(errs.group(1))) if errs else []
    ui = re.search(r'data-ui="([^"]*)"', dom)
    steps = []
    if scenario:
        try:
            steps = (json.loads(unescape(ui.group(1))) if ui else {}).get('steps') or []
        except ValueError:
            steps = []
        if not steps:
            steps = [{'name': 'scenario produced no result', 'ok': False, 'note': ''}]
    failed_steps = [s for s in steps if not s.get('ok')]
    report = re.search(r'data-ui-gate="([^"]*)"', dom)
    try:
        visible = json.loads(unescape(report.group(1))) if report else {}
    except ValueError:
        visible = {}
    panels = visible.get('panels') or []
    tabs = visible.get('tabs', 0)
    return {'page': str(page), 'tabs': tabs, 'panels': panels, 'errors': errors,
            'queued_ms': visible.get('queued_ms'), 'inspect_ms': visible.get('inspect_ms'),
            'empty_panels': [p['panel'] for p in panels if p['empty']],
            'steps': steps, 'failed_steps': failed_steps,
            'ok': bool(panels) and not errors and not any(p['empty'] for p in panels) and not failed_steps}
