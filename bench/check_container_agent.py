"""Zero-token check that a swarm agent really runs inside the swarm container.

A deliberately invalid GEMINI_API_KEY is forwarded. If Google answers that the key is not
valid, then the container started, Gemini CLI ran inside it at the mapped path, and the key
reached it by name. No real credential is involved, so nothing is billed.

Run: .venv\\Scripts\\python.exe bench\\check_container_agent.py
"""
import os
import queue
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ['GEMINI_API_KEY'] = 'not-a-real-key-container-probe'
sys.path.insert(0, str(ROOT / 'adws'))
sys.path.insert(0, str(ROOT))

from adw_modules import agy_swarm
from adw_modules.docker_sandbox import WORKDIR, SwarmSandbox, docker_ready


def main():
    if not docker_ready():
        print('container agent check SKIPPED (docker unavailable)')
        return
    (ROOT / 'work').mkdir(exist_ok=True)   # scratch, gitignored; docker can mount it
    with tempfile.TemporaryDirectory(dir=ROOT / 'work') as tmp:
        root = Path(tmp)
        board = agy_swarm.board_dir(root)
        folder = root / 'probe' / 'round-1'
        with SwarmSandbox('ctrcheck%d' % time.time(), root) as box:
            assert box.inside(root) == WORKDIR
            assert box.inside(board) == WORKDIR + '/board'
            req = agy_swarm.AgentRequest('probe', 'Reply with the single word ok.', folder,
                                         timeout=120, shared=(board,), sandbox=box)
            try:
                agy_swarm.invoke(req, queue.Queue(), threading.Event())
            except agy_swarm.AgentFailed as exc:
                failure = str(exc)
            else:
                raise AssertionError('an invalid key produced a successful agent')
            seen = ''.join((folder / name).read_text(encoding='utf-8', errors='replace')
                           for name in ('stderr.log', 'events.jsonl') if (folder / name).exists())
            assert 'API key not valid' in seen or 'API_KEY_INVALID' in seen, (failure, seen[-1500:])
            # what an agent sees from its own folder: the swarm workspace, no host drive, no root
            probe = box.exec(['sh', '-c', 'pwd; ls /workspace; '
                              'for d in /c /mnt/c /host; do test -e $d && echo HOSTDRIVE $d; done; '
                              'id -u'], workdir=box.inside(folder))
            lines = probe.stdout.strip().splitlines()
            assert lines[0] == box.inside(folder), probe.stdout
            assert 'board' in lines, probe.stdout
            assert 'HOSTDRIVE' not in probe.stdout, probe.stdout
            assert lines[-1] != '0', 'agent runs as root'
    print('container agent check ok: gemini ran in %s, key forwarded by name, fake key rejected'
          % WORKDIR)


if __name__ == '__main__':
    main()
