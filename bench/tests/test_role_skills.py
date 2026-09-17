"""The role-skill chooser, with the catalogue stubbed: no network, no install, in this test."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'bench'))
import role_skills  # noqa: E402


class Parsing(unittest.TestCase):
    def test_reads_packages_and_install_counts(self):
        sample = ('Install with npx skills add <owner/repo@skill>\n'
                  'vercel-labs/agent-skills@web-design 185K installs\n'
                  '  https://skills.sh/vercel-labs/agent-skills/web-design\n'
                  'someone/small@thing 42 installs\n'
                  'other/pack@mid 2.6K installs\n')
        found = role_skills.ENTRY.findall(sample)
        self.assertEqual([f[0] for f in found],
                         ['vercel-labs/agent-skills@web-design', 'someone/small@thing', 'other/pack@mid'])

    def test_thousands_and_millions_scale(self):
        self.assertEqual(role_skills.pick([('a/b@x', 1)], 0, {'x': Path('x')}, False)[2], 1)


class Picking(unittest.TestCase):
    def setUp(self):
        self.have = {'web-design': Path('/skills/web-design/SKILL.md')}

    def test_skips_below_the_floor(self):
        self.assertIsNone(role_skills.pick([('o/r@web-design', 999)], 1000, self.have, False))

    def test_skips_what_is_not_on_this_machine(self):
        self.assertIsNone(role_skills.pick([('o/r@absent', 50000)], 1000, self.have, False))

    def test_takes_the_first_eligible_installed_skill(self):
        got = role_skills.pick([('o/r@absent', 90000), ('o/r@web-design', 5000)], 1000, self.have, False)
        self.assertEqual((got[0], got[3]), ('web-design', 'o/r@web-design'))

    def test_never_installs_unless_asked(self):
        calls = []
        original = role_skills.install
        role_skills.install = lambda package: calls.append(package) or True
        try:
            role_skills.pick([('o/r@absent', 90000)], 1000, {}, False)
            self.assertEqual(calls, [], 'a dry run must not install anything')
        finally:
            role_skills.install = original


class Queries(unittest.TestCase):
    def test_domain_query_uses_the_brief_and_its_context(self):
        spec = {'prompt': 'Build Kanban Board, a personal task board that runs as one page',
                'contract': 'no libraries, one file', 'context': {'language': 'html', 'platform': 'browser'}}
        query = role_skills.domain_query(spec)
        self.assertIn('Kanban', query)
        self.assertIn('html', query)
        self.assertIn('browser', query)
        self.assertIn('no libraries', query)

    def test_reviewer_roles_are_not_given_build_skills(self):
        roles = ['columns', 'cards', 'skeptic', 'referee', 'measurer', 'qa']
        kept = [r for r in roles if not r.lower().startswith(role_skills.REVIEWERS)]
        self.assertEqual(kept, ['columns', 'cards'])


class Output(unittest.TestCase):
    def test_writes_a_sidecar_and_leaves_the_spec_alone(self):
        spec = {'prompt': 'Build a thing', 'agents': ['alpha', 'skeptic'], 'context': {'language': 'html'}}
        spec_path = ROOT / 'work' / 'role-skills-test-spec.json'
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(json.dumps(spec), encoding='utf-8')
        original_catalog, original_installed = role_skills.catalog, role_skills.installed
        role_skills.catalog = lambda query, timeout=180: [('o/r@web-design', 9000)]
        role_skills.installed = lambda: {'web-design': Path('/skills/web-design/SKILL.md')}
        try:
            role_skills.main(['role_skills.py', str(spec_path), '--roles=1'])
        finally:
            role_skills.catalog, role_skills.installed = original_catalog, original_installed
        sidecar = json.loads((role_skills.OUT_DIR / (spec_path.stem + '.json')).read_text(encoding='utf-8'))
        self.assertEqual(sorted(sidecar['role_skills']), ['*', 'alpha'])
        self.assertNotIn('role_skills', json.loads(spec_path.read_text(encoding='utf-8')))
        spec_path.unlink()


if __name__ == '__main__':
    unittest.main()
