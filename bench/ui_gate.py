"""Does the page a builder shipped have a working control behind every tab?

CLI over adws/adw_modules/ui_gate.py (the swarm calls the same judge before its finisher).

Run: .venv/Scripts/python.exe bench/ui_gate.py <page.html> [more pages]
Exit 1 when any page has an empty panel or a mount error. Needs Edge or Chrome.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'adws'))
from adw_modules.ui_gate import browser, judge  # noqa: E402


def main(argv):
    if not browser():
        print('no headless browser on this machine')
        return 2
    worst = 0
    for page in argv[1:]:
        r = judge(page)
        rendered = sum(1 for p in r['panels'] if not p['empty'])
        print('%-60s %s  %d/%d panels rendered, %d error(s)%s' % (
            Path(page).name[:60], 'OK  ' if r['ok'] else 'FAIL', rendered, len(r['panels']),
            len(r['errors']), (': empty ' + ', '.join(r['empty_panels'])) if r['empty_panels'] else ''))
        for e in r['errors'][:3]:
            print('    error: %s' % e)
        worst = max(worst, 0 if r['ok'] else 1)
    return worst


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    raise SystemExit(main(sys.argv))
