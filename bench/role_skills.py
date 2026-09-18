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
                                                     [--roles=3] [--write] [--scout]
                                                     [--scout-runner=cline]
                                                     [--scout-model=cline-free/deepseek-v4.1-flash]
"""
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS_HOME = Path(os.environ.get('SKILLS_HOME') or Path.home() / '.claude' / 'skills')
OUT_DIR = ROOT / 'work' / 'role-skills'
# Roles that judge rather than build: a coding skill in their context is noise. The harness
# splits the roster on the same list, so it lives with the planner.
sys.path.insert(0, str(ROOT))
from swarm_workbench.adaptive import REVIEWERS  # noqa: E402
ENTRY = re.compile(r'([\w.-]+/[\w.-]+@[\w.-]+)\s+([\d.]+)([KM]?)\s+installs')
# Popularity is not relevance. Measured 2026-09-18: "vanilla javascript single file html"
# ranks a 3D-effects skill first (2.1K installs), "html page no framework" a marketing one
# (4.4K), and "Kanban" pulls React, Blazor and MAUI kanban skills - each of which would be
# harmful inside a no-library single file. A skill must at least name the platform.
# ponytail: a name match, not a reading of the skill; an agent that reads the candidates
# against the brief is the upgrade, once a selector turn is worth its cost.
PLATFORM = {'html': ('html', 'web', 'css', 'dom', 'javascript', 'vanilla', 'accessib', 'a11y'),
            'python': ('python', 'py', 'pytest'),
            'javascript': ('javascript', 'js', 'node', 'web', 'vanilla')}
SCOUT_RUNNER = 'cline'
SCOUT_MODEL = 'cline-free/deepseek-v4.1-flash'


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
    # `owner/repo@skill` is what the catalogue prints, and it fails on a repository holding
    # many skills (measured: github/awesome-copilot@web-coder listed the whole repo and exited
    # non-zero). Name the repository and the skill separately instead.
    repo, _, skill = package.partition('@')
    done = subprocess.run(['npx', '--yes', 'skills', 'add', 'https://github.com/' + repo,
                           '--skill', skill, '-g', '-y'],
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


def relevant_terms(spec):
    language = ((spec.get('context') or {}).get('language') or '').lower()
    return PLATFORM.get(language, (language,) if language else ())


def scout_queries(spec, role):
    """The role's job and its execution platform, phrased for catalogue search."""
    context = spec.get('context') or {}
    job = '%s implementation for %s' % (role.replace('-', ' '), subject(spec))
    platform = ' '.join(p for p in (context.get('language', ''), context.get('platform', ''),
                                    'development') if p)
    return job, platform or 'software development'


def scout_candidates(spec, role, maximum=8):
    """Up to ``maximum`` unique catalogue entries, in the two search result orders."""
    found, seen = [], set()
    for query in scout_queries(spec, role):
        print('scout query %-10s %r' % (role, query))
        for package, count in catalog(query):
            if package not in seen:
                found.append((package, count))
                seen.add(package)
            if len(found) == maximum:
                return found
    return found


def scout_prompt(spec, role, candidates):
    """The selection brief, wrapped in the runner's Proposal envelope contract."""
    listed = '\n'.join('- %s (%d installs)' % (package, count) for package, count in candidates)
    return '''You are a skill scout. Choose at most one candidate skill for one builder role.

Brief prompt:
%s

Brief contract:
%s

Role: %s

Candidates:
%s

Judge relevance against the prompt, contract, role, and constraints. Popularity is only a
weak signal. Choose NONE when no candidate fits the constraints - for example, a framework
skill for a no-library single-file page. You must not invent a skill outside the candidates.

End your reply with one fenced json Proposal and nothing after it. Its code field must be a
JSON string containing exactly {"skill": "<owner/repo@name>" or null,
"reason": "one sentence"}. Use status "success", and include decisions and risks as arrays.
''' % (spec.get('prompt', ''), spec.get('contract', ''), role, listed or '(none)')


def scout_decision(spec, role, candidates, runner=SCOUT_RUNNER, model=SCOUT_MODEL):
    """Ask one runner-backed model, returning (candidate package or None, audit reason)."""
    sys.path.insert(0, str(ROOT / 'adws'))
    # Outside the ADW nothing scales the runner limits, so a coding CLI meets the 60,000-token
    # default sized for a raw endpoint. Measured: all five scout turns on the kanban brief failed
    # "token budget exceeded", while one probe turn through cline alone accounts ~50,000 tokens.
    # Same scale the ADW sets for a CLI runner (adws/adw_agy_swarm.py).
    if runner != 'nim':
        os.environ.setdefault('SWARM_AGENT_TOKEN_LIMIT', '6000000')
        os.environ.setdefault('SWARM_AGENT_OUTPUT_LIMIT', '60000')
        os.environ.setdefault('SWARM_FIRST_OUTPUT_TIMEOUT', '240')
        os.environ.setdefault('SWARM_IDLE_TIMEOUT', '180')
    from adw_modules import agy_swarm
    with tempfile.TemporaryDirectory(prefix='role-skills-scout-') as scratch:
        request = agy_swarm.AgentRequest('skill-scout-%s' % role, scout_prompt(spec, role, candidates),
                                         Path(scratch), model=model, runner=runner, timeout=300)
        try:
            proposal, _ = agy_swarm.invoke(request, queue.Queue(), threading.Event())
        except (agy_swarm.AgentFailed, OSError, RuntimeError) as exc:
            return None, 'Scout call failed: %s' % str(exc)[:200]
    try:
        answer = json.loads(proposal.code)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, 'Scout response was not valid JSON.'
    if not isinstance(answer, dict) or not isinstance(answer.get('reason'), str):
        return None, 'Scout response did not contain a skill decision and reason.'
    package = answer.get('skill')
    if package is None:
        return None, answer['reason']
    if not isinstance(package, str) or package not in {entry[0] for entry in candidates}:
        return None, 'Scout chose a skill outside the catalogue candidates.'
    return package, answer['reason']


def pick(results, floor, have, do_install, terms=()):
    """First result above the floor whose skill is on disk, installing it only if allowed."""
    for package, count in results:
        if count < floor:
            continue
        name = package.split('@')[-1]
        if terms and not any(term in name.lower() for term in terms):
            continue
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
    do_install, write_spec, scout = '--install' in argv, '--write' in argv, '--scout' in argv
    scout_runner, scout_model = flag('scout-runner', SCOUT_RUNNER), flag('scout-model', SCOUT_MODEL)
    have = installed()
    roles = [r for r in (spec.get('agents') or []) if not r.lower().startswith(REVIEWERS)]

    mapping, chosen = {}, []
    if scout:
        for role in roles[:limit]:
            candidates = scout_candidates(spec, role)
            package, reason = scout_decision(spec, role, candidates, scout_runner, scout_model)
            counts = dict(candidates)
            if package is None:
                print('scout %-10s -> NONE  %s' % (role, reason))
                chosen.append({'key': role, 'skill': None, 'package': None, 'installs': None,
                               'reason': reason, 'by': 'scout'})
                continue
            name, count = package.split('@')[-1], counts[package]
            print('scout %-10s -> %-28s %8d installs  %s' % (role, name, count, reason))
            chosen.append({'key': role, 'skill': name, 'package': package, 'installs': count,
                           'reason': reason, 'by': 'scout'})
            if name not in have and do_install and install(package):
                have.update(installed())
            if name in have:
                mapping[role] = [str(have[name])]
            else:
                print('  chosen skill is not installed; rerun with --install to map it')
    else:
        terms = relevant_terms(spec)
        language = (spec.get('context') or {}).get('language', '')
        queries = [('*', 'web design accessibility %s' % language), ('*', domain_query(spec))]
        theme = '%s %s' % (subject(spec), (spec.get('context') or {}).get('language', ''))
        queries += [(role, '%s %s' % (role, theme)) for role in roles[:limit]]
        for key, query in queries:
            print('query %-10s %r' % (key, query))
            if key in mapping:
                continue
            hit = pick(catalog(query), floor, have, do_install, terms)
            if not hit:
                print('  nothing above %d installs that is on this machine' % floor)
                continue
            name, path, count, package = hit
            print('  -> %-28s %8d installs  %s' % (name, count, path))
            mapping.setdefault(key, []).append(str(path))
            chosen.append({'key': key, 'skill': name, 'package': package, 'installs': count})

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sidecar = OUT_DIR / (spec_path.stem + '.json')
    # A scout that answered NONE made a decision worth recording; a scout whose call failed did
    # not, and must not erase the last good sidecar (measured twice on this brief).
    decided = any(not str(c.get('reason', '')).startswith('Scout call failed') for c in chosen)
    if not mapping and sidecar.is_file() and not (scout and decided):
        # Measured: one empty catalogue pass overwrote a working cards -> html mapping.
        print('nothing chosen this time; keeping %s as it was' % sidecar)
        return 0
    if scout and sidecar.is_file():
        # The scout decides per role; the platform skill every role shares ('*') is not its
        # call, so carry it forward instead of erasing it (measured: a scout pass dropped html).
        previous = json.loads(sidecar.read_text(encoding='utf-8'))
        if '*' in (previous.get('role_skills') or {}) and '*' not in mapping:
            mapping['*'] = previous['role_skills']['*']
            chosen += [c for c in previous.get('chosen', []) if c.get('key') == '*']
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
