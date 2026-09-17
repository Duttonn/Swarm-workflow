"""Google Antigravity `agy` in stream-json, with the Proposal schema as structured output."""
import json
import os
import subprocess
import threading
import time

from ..agents import _extract_json
from ..agy_swarm import (MODE, SKIP_PERMISSIONS, AgentFailed, AgentRequest, Proposal,
                         agy_path, coerce_proposal, terminate)
from .common import usage


def invoke(req: AgentRequest, messages, cancel):
    # NO --sandbox. Measured across 14 configurations: with --sandbox a headless run hangs on a
    # background task and returns exit 0 / status SUCCESS / no result, so the agent silently
    # produces nothing. Without it, 5/5 runs executed. Containment comes from the per-swarm
    # Docker container instead, which is a real boundary rather than a stalled one.
    argv = [agy_path(), '--input-format', 'stream-json', '--model', req.model, '--mode', MODE,
            '--disable-slash-commands', '--print-timeout', f'{req.timeout}s',
            '--output-format', 'stream-json', '--json-schema', json.dumps(Proposal.model_json_schema())]
    # Escape hatch only. The real HOME's toolPermission=always-proceed already satisfies
    # headless runs, so this stays off unless a machine lacks that setting.
    if SKIP_PERMISSIONS:
        argv.append('--dangerously-skip-permissions')
    # Measured 2026-09-14: without --add-dir agy writes hello.txt into its own scratch
    # (~/.gemini/antigravity-cli/scratch), not the cwd. The board rides along the same way.
    for extra in (req.folder, *req.shared):
        argv += ['--add-dir', str(extra)]
    # HOME is deliberately NOT redirected: doing so hides ~/.gemini/antigravity-cli/settings.json
    # from agy, and every tool call is then silently denied in headless mode. Per-agent
    # separation comes from cwd, and code execution is contained by the per-swarm container.
    env = dict(os.environ)
    with (req.folder/'stderr.log').open('w', encoding='utf-8') as err:
        proc = subprocess.Popen(argv, cwd=req.folder, stdout=subprocess.PIPE, stderr=err,
              stdin=subprocess.PIPE, text=True, encoding='utf-8', env=env,
              creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,
              start_new_session=os.name!='nt')
        messages.put((req.agent, {'event':'process_start', 'pid':proc.pid}))
        deadline = time.monotonic() + req.timeout + 10
        def watchdog():
            while proc.poll() is None:
                if cancel.wait(.25) or time.monotonic() >= deadline:
                    terminate(proc)
                    return
        watcher = threading.Thread(target=watchdog, daemon=True)
        watcher.start()
        proc.stdin.write(json.dumps({'event':'user','message':{'content':req.prompt}}) + '\n')
        proc.stdin.close()
        result = None
        with (req.folder/'events.jsonl').open('w', encoding='utf-8') as raw:
            try:
                for line in proc.stdout:
                    raw.write(line); raw.flush()
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # Retain observable events; private reasoning is not a blueprint input.
                    if event.get('event') == 'step_update':
                        step = event.get('step_update', {})
                        if step.get('step_type') not in ('agent_response','tool','checkpoint','user_input'):
                            continue
                    messages.put((req.agent, event))
                    if event.get('event') == 'result':
                        result = event['result']
                proc.wait()
            finally:
                terminate(proc)
                messages.put((req.agent, {'event':'process_end', 'pid':proc.pid,'exit_code':proc.returncode}))
        # usage.input_tokens is the fresh portion only; cache_read_tokens is separate and
        # NOT in total_tokens (measured: 53300 in + 3849 out = 57149 total, 161482 cached).
        # output_tokens already contains thinking_tokens.
        u = (result or {}).get('usage') or {}
        fresh, cached = u.get('input_tokens'), u.get('cache_read_tokens')
        used = usage('agy', req.model, total=u.get('total_tokens'),
                     input=None if fresh is None else fresh + (cached or 0),
                     cached=cached, output=u.get('output_tokens'))
        (req.folder / 'usage.json').write_text(json.dumps(used, indent=1), encoding='utf-8')
        spent = used['total_tokens'] or 0
        try:
            if proc.returncode or not result or result.get('status') != 'SUCCESS':
                raise RuntimeError(f'AGY failed: exit={proc.returncode}; result={result and result.get("error",result.get("status"))}')
            # Headless mode cannot prompt, so a missing tool permission is auto-denied and the
            # agent returns an empty response. Name that instead of failing on a JSON parse.
            denied = [d.get('action') for d in (result.get('denied_actions') or [])]
            payload = result.get('structured_output')
            if not payload and not result.get('response', '').strip():
                raise RuntimeError(
                    f'{req.agent} produced no proposal; denied tool permissions: {denied or "none"}. '
                    'Agents can write in the sandbox but not execute until the command permission is granted.')
            if not payload:
                # Tool-using agents narrate, so the envelope arrives wrapped in prose or a fenced
                # block rather than as bare JSON. Reuse the factory's tolerant reader instead of
                # a bare json.loads, which is what killed designer_r1 in run 8e5f5634.
                payload = _extract_json(result.get('response', ''))
            if denied:
                proposal_note = f'denied tool permissions during this turn: {denied}'
                payload = {**payload, 'risks': list(payload.get('risks') or []) + [proposal_note]}
            proposal = Proposal.model_validate(coerce_proposal(payload))
            if proposal.status != 'success':
                raise RuntimeError(f'Agent reported failure: {proposal.summary}')
        except Exception as exc:
            raise AgentFailed(str(exc), spent) from exc
        return proposal, {**result, 'usage': used, 'agy_usage': u}
