"""Choose the role skills once, before launch, and write them where the swarm reads them.

adaptive.skill_context() injects spec['role_skills'], and nothing ever filled it: every run so
far planned with `skills: []`. Discovery belongs here rather than inside an agent's turn, for
two reasons measured earlier: a catalogue query costs 10-20 s of wall clock that twenty agents
would each pay, and a skill is instructions an agent will follow, so what gets installed is a
decision to take once, in the open, with the install count printed.

Popularity is the only quality signal the catalogue exposes, so there is a floor, and nothing
is installed unless --install says so: by default only skills already on this machine are
mapped.

Run: .venv\\Scripts\\python.exe bench\\role_skills.py <spec.json> [--install] [--floor=1000]
                                                     [--roles=3] [--write]
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS_HOME = Path(os.environ.get('SKILLS_HOME') or Path.home() / '.claude' / 'skills')
OUT_DIR = ROOT / 'work' / 'role-skills'
# Roles that judge rather than build: a coding skill in their context is noise. The harness
# splits the roster on the same list, so it lives with the planner.
sys.path.insert(0, str(ROOT))
from swarm_workbench.adaptive import REVIEWERS  # noqa: E402
ENTRY = re.compile(r'([\w.-]+/[\w.-]+@[\w.-]+)\s+([\d.]+)([KM]?)\s+installs')


def catalog(query, timeout=180):
    """`npx skills find` results as (package, installs), best first. Network, so it can fail."""
    try:
        done = subprocess.run(['npx', '--yes', 'skills', 'find', query], capture_output=True,
                              text=True, timeout=timeout, shell=os.name == 'nt',
                              encoding='utf-8', errors='replace')
    except (subprocess.SubprocessError, OSError, UnicodeDecodeError) as exc:
        print('  catalogue unreachable for %r: %s' % (query, exc))
        return []
    out = []
    for package, count, unit in ENTRY.findall(re.sub(r'\x1b\[[0-9;]*m', '', done.stdout)):
        scale = {'': 1, 'K': 1000, 'M': 1000000}[unit]
        out.append((package, int(float(count) * scale)))
    return out


def installed():
    """name -> SKILL.md for everything already on this machine."""
    found = {}
    if SKILLS_HOME.is_dir():
        for entry in sorted(SKILLS_HOME.iterdir()):
            skill = entry / 'SKILL.md'
            if skill.is_file():
                found[entry.name] = skill
    return found


def install(package):
    print('  installing %s' % package)
    done = subprocess.run(['npx', '--yes', 'skills', 'add', package, '-g', '-y'],
                          capture_output=True, text=True, timeout=600, shell=os.name == 'nt',
                          encoding='utf-8', errors='replace')
    if done.returncode:
        print('  install failed: %s' % (done.stderr or done.stdout)[-200:])
    return done.returncode == 0


def subject(spec):
    """The two or three words that name the thing, e.g. "Kanban Board" out of a paragraph."""
    words = [w.strip(',.:;') for w in (spec.get('prompt') or '').split()]
    named = [w for w in words[:6] if w[:1].isupper() and w.lower() not in ('build', 'create', 'make')]
    return ' '.join(named[:3]) or ' '.join(words[1:3])


def domain_query(spec):
    """Short and concrete: the catalogue ranks a phrase, and a whole paragraph ranks nothing.

    Measured: "Build Kanban Board, a personal task board that html browser" returned no
    eligible skill; "kanban board html single file app" is what a human would type.
    """
    context = spec.get('context') or {}
    parts = [subject(spec), context.get('language', ''), context.get('platform', ''),
             'single file app',
             'no libraries' if 'no libraries' in (spec.get('contract', '') or '') else '']
    return ' '.join(p for p in parts if p).strip()


def pick(results, floor, have, do_install):
    """First result above the floor whose skill is on disk, installing it only if allowed."""
    for package, count in results:
        if count < floor:
            continue
        name = package.split('@')[-1]
        if name in have:
            return name, have[name], count, package
        if do_install and install(package):
            have.update(installed())
            if name in have:
                return name, have[name], count, package
    return None


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    spec_path = Path(argv[1])
    spec = json.loads(spec_path.read_text(encoding='utf-8'))
    flag = lambda name, default: next((a.split('=')[1] for a in argv if a.startswith('--%s=' % name)), default)
    floor, limit = int(flag('floor', 1000)), int(flag('roles', 3))
    do_install, write_spec = '--install' in argv, '--write' in argv
    have = installed()
    roles = [r for r in (spec.get('agents') or []) if not r.lower().startswith(REVIEWERS)]

    mapping, chosen = {}, []
    queries = [('*', domain_query(spec))]
    theme = '%s %s' % (subject(spec), (spec.get('context') or {}).get('language', ''))
    queries += [(role, '%s %s' % (role, theme)) for role in roles[:limit]]
    for key, query in queries:
        print('query %-10s %r' % (key, query))
        hit = pick(catalog(query), floor, have, do_install)
        if not hit:
            print('  nothing above %d installs that is on this machine' % floor)
            continue
        name, path, count, package = hit
        print('  -> %-28s %8d installs  %s' % (name, count, path))
        mapping.setdefault(key, []).append(str(path))
        chosen.append({'key': key, 'skill': name, 'package': package, 'installs': count})

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sidecar = OUT_DIR / (spec_path.stem + '.json')
    sidecar.write_text(json.dumps({'spec': spec_path.name, 'role_skills': mapping,
                                   'chosen': chosen}, indent=1), encoding='utf-8')
    print('wrote %s (%d role entries)' % (sidecar, len(mapping)))
    if write_spec and mapping:
        spec['role_skills'] = mapping
        spec_path.write_text(json.dumps(spec, indent=2), encoding='utf-8')
        print('merged role_skills into %s' % spec_path)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
