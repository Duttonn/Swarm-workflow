"""Kilo Code CLI: an opencode fork with the same `run --format json` surface and its own free
gateway models (`kilo models | grep :free`; `kilo/kilo-auto/free` rotates through them).
Verified 2026-09-14 with bench/check_runner.py.
"""
from ..agy_swarm import AgentRequest
from . import opencode


def invoke(req: AgentRequest, messages, cancel):
    return opencode.invoke(req, messages, cancel, binary='kilo')
