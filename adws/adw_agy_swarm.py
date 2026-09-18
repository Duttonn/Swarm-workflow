"""Phases: request -> 3 AGY peers x 2 rounds -> integration -> fixed tests -> blueprint.

Usage: python adws/adw_agy_swarm.py prompts/01-intervals.json [--warm bp-file.json]
This workflow has no commit, merge, push or remote-publishing phase.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adw_modules import session
from adw_modules.data_types import SSSFConfig, PhaseParams
from swarm_workbench.blueprints import warm_start
from swarm_workbench.sssf import export_blueprint, load_run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('spec')
    parser.add_argument('--warm')
    parser.add_argument('--auto', action='store_true', help='learned bounded roster and startup context')
    parser.add_argument('--agents', type=int, help='explicit roster size for a controlled comparison')
    # 5, not an arbitrary 4: the size sweep on the same UI brief measured 2 -> 11/14 panels,
    # 5 -> 14/14, 10 -> 8/14, 20 -> 9/14 (bench/results/ui-verdicts-2026-09-16.md). More
    # owners is more integration surface for the finisher, whose one turn is the bottleneck.
    parser.add_argument('--max-agents', type=int, default=5)
    args = parser.parse_args()
    spec = json.loads(Path(args.spec).read_text(encoding='utf-8'))
    # Role skills are chosen once, before launch, by bench/role_skills.py; the swarm only reads
    # that decision. Nothing was reading it, so every plan so far shipped `skills: []`.
    sidecar = Path('work/role-skills') / (Path(args.spec).stem + '.json')
    if 'role_skills' not in spec and sidecar.is_file():
        spec['role_skills'] = json.loads(sidecar.read_text(encoding='utf-8')).get('role_skills') or {}
    from swarm_workbench import adaptive
    if args.auto:
        # Seconds are monotonic wall time; token budgets are cumulative input+output.
        # 240s was sized on NIM, where a call is one completion. A free coding CLI writing the
        # whole prototype streams for longer than that: measured on cline + deepseek-v4.1-flash,
        # the kanban prototype was still emitting at 215s, so a 240s cap only buys two dead
        # attempts. The run cap, not the call cap, is what keeps a bad run bounded.
        # Token limits depend on what is driving, and the gap is two orders of magnitude. A raw
        # endpoint bills one completion per call; a coding CLI resends the whole conversation at
        # every step, so its accounted total is enormous. Measured on this kanban brief: the
        # cline prototype turn reported 3,819,499 tokens (1.6M of them cached) for 30,901 tokens
        # of actual output, against a 60,000 limit meant for NIM. The limit did not save a cent -
        # enforcement is telemetry after the turn - it only failed the phase that was going to
        # fill the page. Wall clock and the watchdog are the real brakes.
        cli = (os.environ.get('SWARM_RUNNER') or spec.get('runner') or 'gemini') != 'nim'
        for key, value in {'SWARM_CALL_TIMEOUT': '900', 'SWARM_RUN_TIMEOUT': '2400',
                           'SWARM_RUN_TOKEN_LIMIT': '40000000' if cli else '180000',
                           'SWARM_AGENT_TOKEN_LIMIT': '6000000' if cli else '60000',
                           'SWARM_AGENT_OUTPUT_LIMIT': '60000' if cli else '12000',
                           'SWARM_MAX_PARALLEL': '2',
                           # 45s of silence means a dead socket on a raw endpoint. On a free
                           # coding CLI it means the queue: measured on run a3f176c3, the
                           # prototype was killed at 45s idle having already written a complete
                           # four-block file to disk, and the finisher was killed at 45s before
                           # its first token - which is why a 5/18 assembly shipped with nobody
                           # left to repair it.
                           'SWARM_FIRST_OUTPUT_TIMEOUT': '240' if cli else '45',
                           'SWARM_IDLE_TIMEOUT': '180' if cli else '45',
                           'SWARM_STAGGER_SECONDS': '0', 'SWARM_COORD': '1'}.items():
            os.environ.setdefault(key, value)
    from adw_modules.agy_swarm import execute_swarm, choose, trace
    runner, model = choose(spec)
    allocation = None
    if args.auto or args.agents:
        allocation = adaptive.plan(spec, runner, model, adaptive.read_history(), args.agents, args.max_agents)
        os.environ['SWARM_ROSTER_SIZE'] = str(allocation['selected_agents'])
        spec['budget'] = {'tokens': allocation['token_limit']}
        spec['_adaptive'] = allocation
    for key in ('prompt','definition_of_done','contract','tests'):
        if not spec.get(key):
            raise ValueError(f'{key} is required before launching agents')
    cfg = SSSFConfig()
    run = session.ensure(cfg)
    warm = warm_start(json.loads(Path(args.warm).read_text()), spec['prompt'], run.repo_root) if args.warm else (
        adaptive.startup_context(spec, run.repo_root) if args.auto else None)
    started = time.monotonic()
    if allocation:
        for role in allocation['roles'] + ['prototype', 'finisher']:
            _, skills = adaptive.skill_context(spec, role)
            allocation['skills'].extend(skills)
        trace(run, '', 'log', 'adaptive_plan', allocation)
    accepted, final, error = False, {}, ''
    try:
        with run.phase(PhaseParams(name='request',kind='engineer',owner=run.engineer,
                       description='Record the exact task and its predefined acceptance contract')) as ph:
            ph.log(input=spec['prompt'], definition_of_done=spec['definition_of_done'],
                   model=model, warm_source=warm and warm['source_blueprint'])
        accepted, final = execute_swarm(run,spec,warm)
    except Exception as exc:
        error = str(exc)
        print(error, file=sys.stderr)
    code = run.finish(accepted=accepted, reason=error or 'Fixed acceptance tests failed')
    if allocation:
        adaptive.remember({'run_id': run.adw_id, 'spec_hash': adaptive.fingerprint(spec),
                          'runner': runner, 'model': model, 'agents': allocation['selected_agents'],
                          'whole_file_model': allocation['whole_file_model'],
                          'accepted': accepted and code == 0, 'elapsed_seconds': round(time.monotonic() - started, 2),
                          'tokens': None if run.__dict__.get('usage_unknown') else run.tokens,
                          'init_seconds': run.__dict__.get('init_seconds'),
                          'stop_reason': run.__dict__.get('swarm_stop', error), 'source': 'live',
                          'quality_kind': 'fixed_tests_and_required_ui'})
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
                      'semantic_status':bp['semantic_status'],'model':model}))
    return code


if __name__ == '__main__':
    sys.exit(main())
