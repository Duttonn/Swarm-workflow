import hashlib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from swarm_workbench.blueprints import distill, rank, warm_start


class Blueprints(unittest.TestCase):
    def setUp(self):
        self.run={'id':'a','status':'done','goal':'Merge integer intervals','agents':['builder']}
        self.events=[{'id':'e1','run_id':'a','kind':'gate','payload':{'name':'tests','passed':True}}]

    def test_unverified_without_gate(self):
        events=[{'id':'e1','run_id':'a','kind':'message','payload':{}}]
        self.assertFalse(distill(self.run,events)['verified_success'])

    def test_failed_not_reused_as_plan(self):
        self.run['status']='failed'
        bp=distill(self.run,self.events,{'steps':[{'id':'s','evidence_ids':['e1'],'role':'builder'}]})
        self.assertEqual(warm_start(bp,'new')['suggested_steps'],[])

    def test_unknown_evidence_rejected(self):
        with self.assertRaises(ValueError):
            distill(self.run,self.events,{'lessons':[{'text':'claim','evidence_ids':['invented']}]})

    def test_cycles_rejected(self):
        with self.assertRaises(ValueError):
            distill(self.run,self.events,{'steps':[{'id':'s','depends_on':['s'],'evidence_ids':['e1']}]})

    def test_stale_and_escape_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            file=Path(tmp)/'a.py';file.write_text('old')
            self.events.append({'id':'e2','run_id':'a','kind':'artifact','payload':
                {'path':'a.py','sha256':hashlib.sha256(b'old').hexdigest()}})
            bp=distill(self.run,self.events)
            self.assertEqual(warm_start(bp,'new',tmp)['file_checks'][0]['status'],'unchanged')
            file.write_text('new')
            self.assertEqual(warm_start(bp,'new',tmp)['file_checks'][0]['status'],'changed')
            bp['artifacts'][0]['path']='../outside'
            self.assertEqual(warm_start(bp,'new',tmp)['file_checks'][0]['status'],'outside_workspace')

    def test_matching_explains_fallback(self):
        bp=distill(self.run,self.events,{'concepts':['intervals']})
        self.assertEqual(rank([bp],'merge integer intervals')[0]['method'],'lexical_fallback')
        self.assertEqual(rank([bp],'unrelated',['intervals'])[0]['method'],'semantic_labels')
        self.assertEqual(rank([bp],'unrelated',['graph']),[])

    def test_context_mismatch_excluded(self):
        self.run['context']={'language':'python'}
        bp=distill(self.run,self.events)
        self.assertEqual(rank([bp],'integer intervals',context={'language':'swift'}),[])

    def test_source_hash_changes_and_no_permissions(self):
        bp=distill(self.run,self.events)
        self.events[0]['payload']['passed']=False
        other=distill(self.run,self.events)
        self.assertNotEqual(bp['source_hash'],other['source_hash'])
        warm=warm_start(bp,'new')
        self.assertEqual(warm['permissions'],[])
        self.assertIsNone(warm['budget'])

    def test_failed_gate_can_be_repaired(self):
        self.events[0]['payload']['passed']=False
        self.events.append({'id':'e2','run_id':'a','kind':'gate','payload':{'name':'tests','passed':True}})
        self.assertTrue(distill(self.run,self.events)['verified_success'])

    def test_cross_run_events_rejected(self):
        self.events[0]['run_id']='other'
        with self.assertRaises(ValueError): distill(self.run,self.events)


if __name__=='__main__': unittest.main()
