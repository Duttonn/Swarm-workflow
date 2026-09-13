"""Score a swarm run against the single-agent baseline for the same spec.

Both artifacts go through the spec's own fixed tests, in one network-less container, so the
comparison is the same judge the run used - not my opinion of the picture.

Run: .venv\\Scripts\\python.exe bench\\compare_to_baseline.py <run_id> [more run ids...]
"""
import contextlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'adws'))
from adw_modules.docker_sandbox import SwarmSandbox, docker_ready  # noqa: E402


def spec_for(out_name):
    for path in sorted((ROOT / 'prompts').glob('*.json')):
        try:
            spec = json.loads(path.read_text(encoding='utf-8'))
        except ValueError:
            continue
        if spec.get('output_file') == out_name:
            return path, spec
    return None, None


def shipped_of(run_id):
    """What the run delivered, found through the trace rather than guessed from the folder."""
    con = sqlite3.connect('file:%s?mode=ro' % (ROOT / 'adws/adw_data/sssf.db').as_posix(), uri=True)
    row = con.execute("select name, total_tokens from events join sessions using (adw_id) "
                      "where adw_id=? and type='artifact' limit 1", (run_id,)).fetchone()
    sess = ROOT / 'adws' / 'adw_data' / 'sessions' / run_id / 'deliverable'
    # Old runs logged the artifact under a fixed name, so trust the event only if it is there.
    if row and (sess / row[0]).exists():
        return sess / row[0], row[1]
    found = next(iter(sorted(sess.glob('*.svg')) + sorted(sess.glob('*.html'))), None)
    return found, row[1] if row else None


def grade(box, spec, path, index, root=None):
    folder = Path(box.workspace if box else root) / ('c%d' % index)
    folder.mkdir(exist_ok=True)
    shutil.copy(path, folder / spec['output_file'])
    (folder / 'test_acceptance.py').write_text(spec['tests'], encoding='utf-8')
    if box is None:
        # No daemon: same verdict, weaker containment. Say so rather than skip the comparison.
        done = subprocess.run([sys.executable, '-I', '-m', 'unittest', 'discover', '-s', '.'],
                              cwd=folder, capture_output=True, text=True, timeout=180)
    else:
        done = box.exec(['python', '-I', '-m', 'unittest', 'discover', '-s', '.'],
                        workdir='/workspace/c%d' % index, timeout=180)
    out = done.stdout + done.stderr
    found = re.search(r'Ran (\d+) test', out)
    ran = int(found.group(1)) if found else 0
    bad = sum(int(n) for n in re.findall(r'(?:failures|errors)=(\d+)', out))
    why = next((l.strip() for l in reversed(out.splitlines()) if re.search(r'Error|assert', l)), '')
    return max(0, ran - bad), ran, why[:90]


def main(run_ids):
    contained = docker_ready()
    if not contained:
        print('docker is down: judging on this machine instead of in a container')
    # The fixed tests are pass/fail on structure; they say nothing about how much was drawn.
    # Element count is the cheap stand-in for "did twenty agents actually add material".
    def richness(path):
        text = path.read_text(encoding='utf-8', errors='replace')
        if path.suffix == '.svg':
            return len(re.findall(r'<[a-zA-Z]', text))
        # a canvas page draws from its script, so count the code, not the handful of tags
        return sum(len(s) for s in re.findall(r'<script[^>]*>(.*?)</script>', text, re.S)) // 100
    print('%-10s %-20s %9s %9s %9s %9s %12s'
          % ('run', 'artifact', 'swarm', 'baseline', 'elem', 'base elem', 'tokens'))
    (ROOT / 'work').mkdir(exist_ok=True)   # scratch, gitignored; docker can mount it
    with tempfile.TemporaryDirectory(dir=ROOT / 'work') as tmp:
        root = Path(tmp)
        with (SwarmSandbox('cmp%d' % int(time.time()), root, network='none') if contained
              else contextlib.nullcontext()) as box:
            for i, run_id in enumerate(run_ids):
                shipped, tokens = shipped_of(run_id)
                if not shipped or not shipped.exists():
                    print('%-10s no deliverable' % run_id)
                    continue
                spec_path, spec = spec_for(shipped.name)
                if not spec:
                    print('%-10s no spec produces %s' % (run_id, shipped.name))
                    continue
                base = ROOT / 'bench' / 'baselines' / ('%s-opus%s' % (shipped.stem, shipped.suffix))
                got, ran, why = grade(box, spec, shipped, i * 2, root)
                if base.exists():
                    bgot, bran, bwhy = grade(box, spec, base, i * 2 + 1, root)
                else:
                    bgot, bran, bwhy = 0, 0, 'no baseline'
                print('%-10s %-20s %9s %9s %9d %9s %12s  %s'
                      % (run_id, shipped.name, '%d/%d' % (got, ran), '%d/%d' % (bgot, bran),
                         richness(shipped), richness(base) if base.exists() else '-',
                         f'{tokens:,}' if tokens else '-', why or bwhy))


if __name__ == '__main__':
    main(sys.argv[1:] or ['60dd4052', 'e202273f'])
