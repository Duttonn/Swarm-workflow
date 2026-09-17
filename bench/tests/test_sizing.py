"""Roster sizing and the reduced-roster split.

Run cd0d13f3 is the case these cover: a sixteen-feature contract sized at two owners, whose
prototype cut two blocks and shipped fourteen empty panels because nothing owned the rest.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'adws'))
from swarm_workbench import adaptive  # noqa: E402
from adw_modules.agy_swarm import assign_groups  # noqa: E402

SPEC = {'prompt': 'Build a board', 'contract': 'c', 'tests': 'def test_a():\n' * 18,
        'output_file': 'x.html', 'ui_scenario': 's',
        'agents': ['columns', 'cards', 'move', 'wiplimit', 'labels', 'search', 'due', 'sort',
                   'archive', 'history', 'lanes', 'people', 'export', 'import', 'metrics',
                   'persist', 'skeptic', 'referee', 'measurer', 'qa']}


class Sizing(unittest.TestCase):
    def plan(self, spec=None, records=(), maximum=4):
        return adaptive.plan(spec or SPEC, 'cline', 'free', records, None, maximum)

    def test_reviewers_are_not_counted_as_features(self):
        self.assertEqual(len(adaptive.build_roles(SPEC)), 16)

    def test_sixteen_features_do_not_ship_as_two_owners(self):
        plan = self.plan()
        self.assertEqual(plan['selected_agents'], 4)
        self.assertTrue(any('16 contract features' in r for r in plan['reasons']))

    def test_the_cap_still_wins(self):
        self.assertEqual(self.plan(maximum=3)['selected_agents'], 3)

    def test_a_budget_stop_does_not_drop_below_the_workload_floor(self):
        """"Adding agents is not a remedy" is about growth, not about unowning regions."""
        history = [{'spec_hash': adaptive.fingerprint(SPEC), 'runner': 'cline', 'model': 'free',
                    'accepted': False, 'agents': 2, 'stop_reason': 'token budget exceeded'}]
        plan = self.plan(records=history)
        self.assertEqual(plan['selected_agents'], 4)

    def test_a_small_contract_stays_small(self):
        spec = dict(SPEC, agents=['builder', 'skeptic'], tests='def test_a():\n')
        self.assertEqual(self.plan(spec)['selected_agents'], 1)


class ReducedRoster(unittest.TestCase):
    def test_every_feature_lands_in_exactly_one_group(self):
        kept, groups, judges = assign_groups(list(SPEC['agents']), 4)
        self.assertEqual(kept, ['columns', 'cards', 'move', 'wiplimit'])
        self.assertEqual(judges, ['skeptic', 'referee', 'measurer', 'qa'])
        owned = [name for _, features in groups for name in features]
        self.assertEqual(sorted(owned), sorted(adaptive.build_roles(SPEC)))
        self.assertEqual(len(owned), len(set(owned)), 'no feature may have two owners')

    def test_each_group_leads_with_its_owner(self):
        _, groups, _ = assign_groups(list(SPEC['agents']), 4)
        self.assertTrue(all(features[0] == owner for owner, features in groups))

    def test_groups_are_contiguous_and_even(self):
        _, groups, _ = assign_groups(list(SPEC['agents']), 4)
        self.assertEqual([len(f) for _, f in groups], [4, 4, 4, 4])

    def test_one_owner_takes_everything(self):
        _, groups, _ = assign_groups(list(SPEC['agents']), 1)
        self.assertEqual(len(groups[0][1]), 16)


if __name__ == '__main__':
    unittest.main()
