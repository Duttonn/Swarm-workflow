r"""Adversarial pokes against a kanban page, from outside the swarm that built it.

Written blind by a reviewer who was shown only the brief and two unlabeled pages, then kept
unchanged so any later page is judged by the same fifteen pokes: hostile names, broken saved
state, hostile imports, and the board rules under pressure. One line per poke: name,
PASS/FAIL/ERROR, severity, evidence. ERROR means the browser did not return, not a verdict.

Run: .venv\Scripts\python.exe bench\poke_kanban.py <page.html>   (set PYTHONIOENCODING=utf-8)
"""
import argparse
import html
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile


EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

POKES = [
    ("column_html_img", "major", "columnInput", "<img src=x onerror=window.__pwned=1>"),
    ("column_script_tag", "major", "columnInput", "<script>window.__pwned=1</script>"),
    ("column_10000_chars", "minor", "columnInput", "x" * 10000),
    ("column_empty", "major", "columnInput", ""),
    ("column_whitespace", "major", "columnInput", "   \t  "),
    ("column_emoji", "major", "columnInput", "Board " + chr(0x1F9EA)),
    ("persist_invalid_json", "critical", "persistence", "{not json"),
    ("persist_wrong_shape", "critical", "persistence", '{"version":2,"columns":"no","cards":42}'),
    ("import_malformed_json", "major", "importer", "{bad"),
    ("import_huge_payload", "major", "importer", None),
    ("import_proto_key", "critical", "importer", '{"version":2,"columns":[],"cards":[],"__proto__":{"polluted":true}}'),
    ("move_into_full_wip", "major", "moveFull", None),
    ("undo_with_no_history", "major", "undoEmpty", None),
    ("redo_after_new_action", "major", "redoBranch", None),
    ("delete_nonempty_column", "critical", "deleteNonempty", None),
]


JS_COMMON = r"""
const pokeName = __POKE_NAME__;
const kind = __KIND__;
const value = __VALUE__;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const norm = s => String(s || '').replace(/\s+/g, ' ').trim().toLowerCase();
let uncaught = [];
window.addEventListener('error', e => uncaught.push(String(e.message || e.error || 'error')));
window.addEventListener('unhandledrejection', e => uncaught.push(String(e.reason || 'unhandled rejection')));
function finish(pass, evidence, severity) {
  if (uncaught.length) { pass = false; severity = severity === 'critical' ? severity : 'major'; evidence += '; uncaught: ' + uncaught.join(' | '); }
  document.documentElement.setAttribute('data-poke-result', JSON.stringify({name: pokeName, pass, severity, evidence: String(evidence).slice(0, 1200)}));
}
function state() { return window.App && App.store && App.store.get ? App.store.get() : null; }
function panel(name) {
  const tab = [...document.querySelectorAll('[role=tab], #tabs button, button[data-tab]')].find(x => norm(x.dataset.tab || x.textContent) === norm(name));
  if (!tab) throw new Error('tab not found: ' + name);
  tab.click();
  return document.querySelector('#panel-' + name) || document.querySelector('[data-panel="' + name + '"]');
}
function labelled(root, words, selector='input,textarea,select') {
  const wanted = words.map(norm);
  const nodes = [...root.querySelectorAll(selector)];
  return nodes.find(el => {
    const labels = [];
    if (el.labels) labels.push(...[...el.labels].map(x => x.textContent));
    labels.push(el.getAttribute('aria-label'), el.getAttribute('placeholder'), el.name, el.id);
    const text = norm(labels.filter(Boolean).join(' '));
    return wanted.some(w => text.includes(w));
  });
}
function button(root, words) {
  const wanted = words.map(norm);
  return [...root.querySelectorAll('button,input[type=button],input[type=submit]')].find(el => wanted.some(w => norm(el.textContent || el.value || el.getAttribute('aria-label')).includes(w)));
}
function setControl(el, val) {
  if (!el) throw new Error('control not found');
  if (el.tagName === 'SELECT') {
    const opt = [...el.options].find(o => norm(o.textContent) === norm(val) || String(o.value) === String(val) || norm(o.textContent).includes(norm(val)));
    if (!opt) throw new Error('select option not found: ' + val);
    el.value = opt.value;
  } else el.value = val;
  el.dispatchEvent(new Event('input', {bubbles:true}));
  el.dispatchEvent(new Event('change', {bubbles:true}));
}
async function addColumn(name) {
  const root = panel('columns');
  const input = labelled(root, ['column name','name'], 'input,textarea');
  const add = button(root, ['add column','create column','add','create']);
  setControl(input, name); add.click(); await sleep(40);
}
async function importDoc(text) {
  const root = panel('importer');
  const area = labelled(root, ['json','import'], 'textarea,input') || root.querySelector('textarea');
  const run = button(root, ['import','add']);
  setControl(area, text); run.click(); await sleep(80);
  return root;
}
function seedDoc() {
  return {version:2, columns:[{id:'todo',name:'Todo',wip:null},{id:'full',name:'Full',wip:1}], cards:[
    {id:'c1',title:'Existing',description:'',tags:[],due:null,assignee:null,column:'full',created:'2026-01-01',done:null,archived:false},
    {id:'c2',title:'Moving',description:'',tags:[],due:null,assignee:null,column:'todo',created:'2026-01-02',done:null,archived:false}
  ]};
}
(async () => {
try {
  await sleep(80);
  if (!window.App || !App.store || !App.features) throw new Error('App did not mount');
  if (kind === 'columnInput') {
    const before = state().columns.length;
    await addColumn(value);
    const s = state(); const after = s.columns.length;
    const blank = value.trim() === '';
    const acceptedExact = s.columns.some(c => c.name === value);
    const escaped = !window.__pwned;
    const pass = blank ? after === before : acceptedExact && after === before + 1 && escaped;
    finish(pass, `columns ${before}->${after}; exact=${acceptedExact}; codeExecuted=${!!window.__pwned}`, blank ? 'major' : (value.length > 1000 ? 'minor' : 'major'));
  } else if (kind === 'persistence') {
    const key = App.features.persist && App.features.persist.model && App.features.persist.model.KEY;
    if (!key) throw new Error('persist key unavailable');
    const stageKey = 'poke-stage-' + pokeName;
    if (!sessionStorage.getItem(stageKey)) {
      sessionStorage.setItem(stageKey, 'reload'); localStorage.setItem(key, value); location.reload(); return;
    }
    sessionStorage.removeItem(stageKey);
    const s = state();
    const alive = !!document.querySelector('#tabs button, button[data-tab]') && s && Array.isArray(s.columns) && Array.isArray(s.cards);
    finish(alive, `reloaded with ${value.slice(0,80)}; alive=${!!alive}; columnsArray=${!!(s&&Array.isArray(s.columns))}; cardsArray=${!!(s&&Array.isArray(s.cards))}`, 'critical');
  } else if (kind === 'importer') {
    let text = value;
    if (pokeName === 'import_huge_payload') {
      const stageKey = 'poke-stage-' + pokeName;
      if (sessionStorage.getItem(stageKey)) {
        sessionStorage.removeItem(stageKey);
        const restored = state(); const kept = restored && Array.isArray(restored.cards) ? restored.cards.length : -1;
        finish(kept >= 8000, `after reload persistedCards=${kept}`, 'major');
        return;
      }
      const doc = {version:2, columns:[{id:'bulk',name:'Bulk',wip:null}], cards:[]};
      for (let i=0;i<8000;i++) doc.cards.push({id:'b'+i,title:'Card '+i,description:'y'.repeat(700),tags:['bulk'],due:null,assignee:null,column:'bulk',created:'2026-01-01',done:null,archived:false});
      text = JSON.stringify(doc);
    }
    const root = await importDoc(text);
    const s = state(); const alive = !!s && Array.isArray(s.columns) && Array.isArray(s.cards);
    const polluted = ({}).polluted === true;
    let pass = alive && !polluted;
    if (pokeName === 'import_malformed_json') pass = pass && /error|invalid|not valid|not json|malformed/.test(norm(root.textContent));
    if (pokeName === 'import_huge_payload') {
      if (!(pass && s.cards.length >= 8000)) { finish(false, `alive=${alive}; cards=${alive?s.cards.length:'n/a'}`, 'major'); return; }
      sessionStorage.setItem('poke-stage-' + pokeName, 'reload'); location.reload(); return;
    }
    finish(pass, `alive=${alive}; cards=${alive?s.cards.length:'n/a'}; prototypePolluted=${polluted}; panelText=${norm(root.textContent).slice(0,180)}`, pokeName === 'import_proto_key' ? 'critical' : 'major');
  } else if (kind === 'moveFull') {
    await importDoc(JSON.stringify(seedDoc()));
    const root = panel('move');
    const cardRow = [...root.querySelectorAll('article,.card,[role=row],li')].find(x => norm(x.textContent).includes('moving'));
    if (!cardRow) throw new Error('card move controls for Moving not found');
    const colSel = labelled(cardRow, ['column','destination','target','to'], 'select') || cardRow.querySelector('select');
    setControl(colSel, 'Full');
    const go = button(cardRow, ['move']); if (!go) throw new Error('move button not found'); go.click(); await sleep(60);
    const s = state(); const card = s.cards.find(c=>c.id==='c2');
    const refused = card && card.column === 'todo';
    const reason = /wip|full|limit|cannot|refus/.test(norm(root.textContent));
    finish(refused && reason, `column=${card&&card.column}; refusalMessage=${reason}; panelText=${norm(root.textContent).slice(0,180)}`, 'major');
  } else if (kind === 'undoEmpty') {
    const root = panel('history'); const before = JSON.stringify(state());
    const undo = button(root, ['undo']); if (!undo) throw new Error('undo button not found'); undo.click(); await sleep(40);
    finish(JSON.stringify(state()) === before, `stateUnchanged=${JSON.stringify(state())===before}; disabled=${undo.disabled}`, 'major');
  } else if (kind === 'redoBranch') {
    await addColumn('First');
    let root = panel('history'); let undo = button(root,['undo']); if(!undo) throw new Error('undo button not found'); undo.click(); await sleep(40);
    const undoWasDisabled = undo.disabled;
    const historyText = norm(root.textContent).slice(0,160);
    const afterUndo = state().columns.map(c=>c.name);
    await addColumn('Second');
    root = panel('history'); const redo = button(root,['redo']); if(!redo) throw new Error('redo button not found'); redo.click(); await sleep(40);
    const names = state().columns.map(c=>c.name);
    finish(!afterUndo.includes('First') && names.includes('Second') && !names.includes('First'), `undoDisabled=${undoWasDisabled}; history=${historyText}; afterUndo=${JSON.stringify(afterUndo)}; afterNewThenRedo=${JSON.stringify(names)}; redoDisabled=${redo.disabled}`, 'major');
  } else if (kind === 'deleteNonempty') {
    await importDoc(JSON.stringify(seedDoc()));
    const root = panel('columns');
    const row = [...root.querySelectorAll('article,.column,.col,[role=row],li')].find(x => norm(x.textContent).includes('full'));
    if (!row) throw new Error('column row for Full not found');
    const del = button(row,['delete column','remove column','delete','remove']); if(!del) throw new Error('delete button not found'); del.click(); await sleep(50);
    const s = state(); const colExists=s.columns.some(c=>c.id==='full'); const card=s.cards.find(c=>c.id==='c1'); const cardExists=!!card; const orphan=cardExists&&!s.columns.some(c=>c.id===card.column);
    const explained=/card|empty|move|cannot|refus/.test(norm(root.textContent));
    finish(!orphan && (colExists ? explained : !cardExists || card.column !== 'full'), `columnExists=${colExists}; cardExists=${cardExists}; cardColumn=${card&&card.column}; orphan=${orphan}; explanation=${explained}`, orphan ? 'critical' : 'major');
  }
} catch (e) { finish(false, 'exception: ' + (e && e.stack || e), kind === 'persistence' || pokeName === 'import_proto_key' || kind === 'deleteNonempty' ? 'critical' : 'major'); }
})();
"""


def inject(page_text, name, kind, value):
    encoded_value = json.dumps(value).replace("</", "<\\/")
    script = JS_COMMON.replace("__POKE_NAME__", json.dumps(name)).replace("__KIND__", json.dumps(kind)).replace("__VALUE__", encoded_value)
    # A page that asks confirm() before deleting a populated column is doing the right thing for
    # a person, but a modal blocks a headless browser until the timeout, which read as "no verdict"
    # on the one page that protected its data. Accept every dialog, as the swarm's own UI gate
    # does, and judge the state the page is left in afterwards.
    dialogs = "window.confirm=function(){return true};window.alert=function(){};window.prompt=function(){return ''};"
    tag = "\n<script>\n" + dialogs + "\n" + script + "\n</script>\n"
    pos = page_text.lower().rfind("</body>")
    if pos < 0:
        raise ValueError("page has no </body>")
    return page_text[:pos] + tag + page_text[pos:]


def run_edge(temp_page, profile):
    uri = pathlib.Path(temp_page).resolve().as_uri()
    cmd = [EDGE, "--headless=new", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=8000", f"--user-data-dir={profile}", "--dump-dom", uri]
    # A cold or busy machine can hold the first headless launch past the limit. That says
    # nothing about the page, so one retry before giving up.
    for attempt in (1, 2):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=30 if attempt == 1 else 90)
        except subprocess.TimeoutExpired:
            if attempt == 2:
                raise


def main():
    parser = argparse.ArgumentParser(description="Run repeatable adversarial UI pokes against a Kanban HTML page.")
    parser.add_argument("page")
    args = parser.parse_args()
    page = pathlib.Path(args.page).resolve()
    original = page.read_text(encoding="utf-8")
    if not os.path.isfile(EDGE):
        raise SystemExit(f"Edge not found: {EDGE}")
    overall = 0
    for name, default_severity, kind, value in POKES:
        run_dir = tempfile.mkdtemp(prefix="kanban-poke-")
        try:
            temp_page = pathlib.Path(run_dir) / page.name
            temp_page.write_text(inject(original, name, kind, value), encoding="utf-8")
            profile = pathlib.Path(run_dir) / "profile"
            proc = run_edge(temp_page, profile)
            match = re.search(r'<html[^>]*\sdata-poke-result="([^"]*)"', proc.stdout, re.I)
            if not match:
                evidence = f"no result attribute; edge exit={proc.returncode}; stderr={proc.stderr.strip()[:300]}"
                result = {"name": name, "pass": False, "severity": "critical" if kind == "persistence" else default_severity, "evidence": evidence}
            else:
                try:
                    result = json.loads(html.unescape(match.group(1)))
                except Exception as exc:
                    result = {"name": name, "pass": False, "severity": default_severity, "evidence": f"invalid result attribute: {exc}"}
            status = "PASS" if result.get("pass") else "FAIL"
            if status == "FAIL": overall = 1
            clean = str(result.get("evidence", "")).replace("\r", " ").replace("\n", " ")
            print(f"{name}\t{status}\t{result.get('severity', default_severity)}\t{clean}")
        except subprocess.TimeoutExpired:
            overall = 1
            print(f"{name}\tERROR\t{default_severity}\tbrowser did not return twice (30s, 90s): no verdict on the page")
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)
    return overall


if __name__ == "__main__":
    sys.exit(main())
