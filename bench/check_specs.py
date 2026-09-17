"""Validate every swarm spec and its baseline before a swarm ever runs on it.

A spec is only worth 30M tokens if its tests actually decide something: they must pass on a
real implementation and fail on an empty file. Both are checked here, in the network-less
container, the same place the acceptance gate runs.

Run: .venv\\Scripts\\python.exe bench\\check_specs.py [prompts/06-metro-map.json ...]
"""
import contextlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'adws'))
from adw_modules.docker_sandbox import SwarmSandbox, docker_ready  # noqa: E402

KEYS = ('prompt', 'definition_of_done', 'contract', 'context', 'agents', 'output_file', 'tests',
        'budget')


def flaws(spec, path):
    out = []
    for key in KEYS:
        if not spec.get(key):
            out.append('missing %s' % key)
    if out:
        return out
    agents = spec['agents']
    # 16 owners + 4 reviewers is the ladder's shape; spec 20 has twenty owners, so 24
    if not 20 <= len(agents) <= 24:
        out.append('roster is %d agents, expected 20 to 24' % len(agents))
    if len(set(agents)) != len(agents):
        out.append('duplicate agent names')
    if any(not re.fullmatch(r'[a-z][a-z0-9_-]*', a) for a in agents):
        out.append('agent names must be lowercase slugs')
    tests = len(re.findall(r'def test_', spec['tests']))
    if tests < 10:
        out.append('only %d tests' % tests)
    if 'import unittest' not in spec['tests']:
        out.append('tests are not a unittest module')
    for banned in ('requests', 'urllib', 'http'):
        if re.search(r'import %s' % banned, spec['tests']):
            out.append('tests reach the network (%s)' % banned)
    # .js joined the list for server specs: their suite boots the file over HTTP on a local
    # port instead of parsing it, so urllib in the tests is the client, not a network reach.
    if Path(spec['output_file']).suffix not in ('.svg', '.html', '.js'):
        out.append('output_file must be .svg, .html or .js')
    if (spec.get('budget') or {}).get('tokens') != 30000000:
        out.append('budget is %s' % (spec.get('budget'),))
    return out


def grade(box, spec, artifact, label, root, index):
    folder = root / ('c%d' % index)
    folder.mkdir()
    if artifact:
        shutil.copy(artifact, folder / spec['output_file'])
    else:
        (folder / spec['output_file']).write_text('', encoding='utf-8')
    (folder / 'test_acceptance.py').write_text(spec['tests'], encoding='utf-8')
    if box is None:
        # No daemon: still judge the spec, on this machine. Weaker containment, same verdict.
        done = subprocess.run([sys.executable, '-I', '-m', 'unittest', 'discover', '-s', '.'],
                              cwd=folder, capture_output=True, text=True, timeout=180)
    else:
        done = box.exec(['python', '-I', '-m', 'unittest', 'discover', '-s', '.'],
                        workdir='/workspace/c%d' % index, timeout=180)
    out = done.stdout + done.stderr
    found = re.search(r'Ran (\d+) test', out)
    ran = int(found.group(1)) if found else 0
    bad = sum(int(n) for n in re.findall(r'(?:failures|errors)=(\d+)', out))
    return max(0, ran - bad), ran, done.returncode == 0


def main():
    paths = [Path(p) for p in sys.argv[1:]] or sorted((ROOT / 'prompts').glob('*.json'))
    contained = docker_ready()
    if not contained:
        print('docker is down: judging the specs on this machine instead of in a container')
    rows, bad = [], 0
    (ROOT / 'work').mkdir(exist_ok=True)   # scratch, gitignored; docker can mount it
    with tempfile.TemporaryDirectory(dir=ROOT / 'work') as tmp:
        root = Path(tmp)
        with (SwarmSandbox('speccheck%d' % int(time.time()), root, network='none') if contained
              else contextlib.nullcontext()) as box:
            for i, path in enumerate(paths):
                spec = json.loads(path.read_text(encoding='utf-8'))
                problems = flaws(spec, path)
                # the baseline is named after the artifact, not after the spec file
                stem = Path(spec['output_file']).stem
                art = ROOT / 'bench' / 'baselines' / ('%s-opus%s' % (stem, Path(spec['output_file']).suffix))
                passed = ran = 0
                empty_ok = None
                if art and art.exists():
                    passed, ran, _ = grade(box, spec, art, 'baseline', root, i * 2)
                    _, _, empty_green = grade(box, spec, None, 'empty', root, i * 2 + 1)
                    empty_ok = not empty_green
                    if passed != ran or ran == 0:
                        problems.append('baseline scores %d/%d' % (passed, ran))
                    if empty_green:
                        problems.append('the suite passes on an EMPTY file')
                else:
                    problems.append('no baseline at work/baseline/%s' % art.name)
                rows.append((path.name, len(spec.get('agents') or []), ran, passed, empty_ok,
                             problems))
                bad += bool(problems)
    print('%-26s %6s %6s %8s %7s  %s' % ('spec', 'agents', 'tests', 'baseline', 'empty', 'problems'))
    for name, agents, ran, passed, empty_ok, problems in rows:
        print('%-26s %6d %6d %8s %7s  %s'
              % (name, agents, ran, '%d/%d' % (passed, ran),
                 'fails' if empty_ok else ('PASSES' if empty_ok is False else '-'),
                 '; '.join(problems) or 'ok'))
    print('%d spec(s) checked, %d with problems' % (len(rows), bad))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main() or 0)
