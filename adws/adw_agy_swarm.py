"""Phases: request -> 3 AGY peers x 2 rounds -> integration -> fixed tests -> blueprint.

Usage: python adws/adw_agy_swarm.py prompts/01-intervals.json [--warm bp-file.json]
This workflow has no commit, merge, push or remote-publishing phase.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adw_modules import session
from adw_modules.data_types import SSSFConfig, PhaseParams
from adw_modules.agy_swarm import execute_swarm, MODEL
from swarm_workbench.blueprints import warm_start
from swarm_workbench.sssf import export_blueprint, load_run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('spec')
    parser.add_argument('--warm')
    args = parser.parse_args()
    spec = json.loads(Path(args.spec).read_text(encoding='utf-8'))
    for key in ('prompt','definition_of_done','contract','tests'):
        if not spec.get(key):
            raise ValueError(f'{key} is required before launching agents')
    cfg = SSSFConfig()
    run = session.ensure(cfg)
    warm = warm_start(json.loads(Path(args.warm).read_text()), spec['prompt'], run.repo_root) if args.warm else None
    accepted, final, error = False, {}, ''
    try:
        with run.phase(PhaseParams(name='request',kind='engineer',owner=run.engineer,
                       description='Record the exact task and its predefined acceptance contract')) as ph:
            ph.log(input=spec['prompt'], definition_of_done=spec['definition_of_done'],
                   model=MODEL, warm_source=warm and warm['source_blueprint'])
        accepted, final = execute_swarm(run,spec,warm)
    except Exception as exc:
        error = str(exc)
        print(error, file=sys.stderr)
    code = run.finish(accepted=accepted, reason=error or 'Fixed acceptance tests failed')
    native, events = load_run(cfg.observability.db,run.adw_id)
    messages = [e for e in events if e['kind']=='peer_message']
    annotations = {'summary':final.get('summary',''), 'concepts':spec.get('concepts',[]),
                   'lessons':[], 'steps':[]}
    for item in final.get('decisions',[]):
        annotations['lessons'].append({'text':item, 'evidence_ids':[final['evidence_id']],
                                      'kind':'agent_reported_decision'})
    for item in final.get('risks',[]):
        annotations['lessons'].append({'text':item, 'evidence_ids':[final['evidence_id']],
                                      'kind':'remaining_risk'})
    for e in messages:
        annotations['steps'].append({'id':e['id'], 'role':e['agent'],
            'purpose':e['payload'].get('summary',''), 'evidence_ids':[e['id']],
            'depends_on':[m['id'] for m in messages
                          if m['payload'].get('round',0)==e['payload'].get('round',0)-1]})
    bp,path = export_blueprint(cfg.observability.db,run.adw_id,'blueprints',annotations)
    print(json.dumps({'run_id':run.adw_id,'accepted':accepted,'blueprint':str(path),
                      'semantic_status':bp['semantic_status'],'model':MODEL}))
    return code


if __name__ == '__main__':
    sys.exit(main())
