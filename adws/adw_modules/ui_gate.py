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
    seed = seed_script(page) + scenario_script(scenario)
    hooked = hooked.replace('</body>', seed + '</body>', 1) if '</body>' in hooked else hooked + seed
    with tempfile.TemporaryDirectory(prefix='ui-gate-') as tmp:
        copy = Path(tmp) / Path(page).name
        copy.write_text(hooked, encoding='utf-8')
        argv = [browser(), '--headless=new', '--disable-gpu', '--no-sandbox',
                '--virtual-time-budget=12000', '--user-data-dir=' + str(Path(tmp) / 'profile'),
                '--dump-dom', copy.as_uri()]
        done = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=int(os.environ.get('SWARM_GATE_TIMEOUT', '300')))
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
    panels = []
    for m in re.finditer(r'<section[^>]*id="panel-([\w-]+)"[^>]*>(.*?)</section>', dom, re.S | re.I):
        name, body = m.group(1), m.group(2)
        controls = len(re.findall(r'<(button|input|select|textarea|svg|canvas)\b', body, re.I))
        # a panel is empty when, past its own heading, a person finds neither a control nor
        # a dozen characters of content ("Tags #project (2)" is a rendered tag list)
        headless = re.sub(r'<h[1-6][^>]*>.*?</h[1-6]>', ' ', body, count=1, flags=re.S | re.I)
        text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', headless)).strip()
        panels.append({'panel': name, 'controls': controls, 'chars': len(text),
                       'empty': controls == 0 and len(text) < 12})
    tabs = len(re.findall(r'data-tab="', dom))
    return {'page': str(page), 'tabs': tabs, 'panels': panels, 'errors': errors,
            'empty_panels': [p['panel'] for p in panels if p['empty']],
            'steps': steps, 'failed_steps': failed_steps,
            'ok': bool(panels) and not errors and not any(p['empty'] for p in panels) and not failed_steps}
