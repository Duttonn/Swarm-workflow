"""One Docker container per swarm; every agent execs inside it.

Per-swarm rather than per-agent is deliberate: agents share one filesystem, so two of
them writing the same file is a real collision the harness must arbitrate, not a
hypothetical. That is the condition file claims exist for.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

IMAGE = os.environ.get('SWARM_SANDBOX_IMAGE', 'swarm-workbench-sandbox:latest')
WORKDIR = '/workspace'
# The agent process calls a model API from inside the container, so the network cannot be
# 'none' here. Containment is the filesystem and user boundary, not egress.
NETWORK = os.environ.get('SWARM_SANDBOX_NETWORK', 'bridge')
MEMORY = os.environ.get('SWARM_SANDBOX_MEMORY', '2g')
CPUS = os.environ.get('SWARM_SANDBOX_CPUS', '2')


class SandboxUnavailable(RuntimeError):
    """Docker is absent or not running; the caller decides whether that is fatal."""


def docker_path():
    found = shutil.which('docker')
    if not found and os.name == 'nt':
        candidate = Path(r'C:\Program Files\Docker\Docker\resources\bin\docker.exe')
        found = str(candidate) if candidate.is_file() else None
    if not found:
        raise SandboxUnavailable('docker not found on PATH')
    return found


def docker_env():
    """Docker shells out to sibling helpers such as docker-credential-desktop, so calling
    docker.exe by absolute path is not enough: its own bin directory must be on PATH."""
    env = dict(os.environ)
    bindir = str(Path(docker_path()).parent)
    if bindir not in env.get('PATH', ''):
        env['PATH'] = bindir + os.pathsep + env.get('PATH', '')
    return env


def docker_ready():
    """True only if the daemon actually answers, not merely if the client exists."""
    try:
        probe = subprocess.run([docker_path(), 'info', '--format', '{{.ServerVersion}}'],
                               capture_output=True, text=True, timeout=30, env=docker_env())
    except (SandboxUnavailable, subprocess.SubprocessError, OSError):
        return False
    return probe.returncode == 0


def build_image(dockerfile, context, image=IMAGE, timeout=1800):
    argv = [docker_path(), 'build', '-f', str(dockerfile), '-t', image, str(context)]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=docker_env())
    if done.returncode:
        raise SandboxUnavailable(f'image build failed: {done.stderr[-1500:]}')
    return image


@dataclass
class SwarmSandbox:
    run_id: str
    workspace: Path
    image: str = IMAGE
    network: str = NETWORK
    mounts: list = field(default_factory=list)   # (host, container, mode) triples
    env: dict = field(default_factory=dict)
    container: str = ''
    # Explicit keep-alive so the box stays up whatever the image's own CMD would do.
    command: list = field(default_factory=lambda: ['sleep', 'infinity'])
    # Enforced at runtime, not left to the image: a root shell defeats the sandbox, and
    # base images such as python:3.12-slim default to root.
    user: str = os.environ.get('SWARM_SANDBOX_USER', '1000:1000')

    @property
    def name(self):
        return f'swarm-{self.run_id}'

    def start(self):
        self.workspace.mkdir(parents=True, exist_ok=True)
        # A leftover container from a killed run would otherwise block the name.
        subprocess.run([docker_path(), 'rm', '-f', self.name],
                       capture_output=True, text=True, timeout=60, env=docker_env())
        argv = [docker_path(), 'run', '-d', '--name', self.name,
                '--network', self.network, '--memory', MEMORY, '--cpus', CPUS,
                '--pids-limit', '512', '--security-opt', 'no-new-privileges',
                '-v', f'{self.workspace.resolve()}:{WORKDIR}:rw', '-w', WORKDIR]
        if self.user:
            argv += ['--user', self.user]
        for host, target, mode in self.mounts:
            argv += ['-v', f'{Path(host).resolve()}:{target}:{mode}']
        for key, value in self.env.items():
            argv += ['-e', f'{key}={value}']
        argv += [self.image, *self.command]
        done = subprocess.run(argv, capture_output=True, text=True, timeout=300, env=docker_env())
        if done.returncode:
            raise SandboxUnavailable(f'container start failed: {done.stderr[-1000:]}')
        self.container = done.stdout.strip()
        alive = subprocess.run([docker_path(), 'inspect', '-f', '{{.State.Running}}', self.name],
                               capture_output=True, text=True, timeout=60, env=docker_env())
        if alive.stdout.strip() != 'true':
            detail = self.logs(50)
            self.stop()
            raise SandboxUnavailable(f'container exited immediately: {detail[-500:]}')
        return self

    def inside(self, host):
        """Where a host path under the workspace appears inside the container."""
        rel = Path(host).resolve().relative_to(self.workspace.resolve()).as_posix()
        return WORKDIR if rel == '.' else f'{WORKDIR}/{rel}'

    def exec(self, argv, timeout=300, workdir=None, stdin=None):
        if not self.container:
            raise SandboxUnavailable('sandbox not started')
        cmd = [docker_path(), 'exec', '-i']
        if workdir:
            cmd += ['-w', workdir]
        cmd += [self.name, *argv]
        return subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                              encoding='utf-8', timeout=timeout, env=docker_env())

    def logs(self, tail=200):
        done = subprocess.run([docker_path(), 'logs', '--tail', str(tail), self.name],
                              capture_output=True, text=True, timeout=60, env=docker_env())
        return done.stdout + done.stderr

    def stop(self):
        """The sandbox is the last line of defence, so tearing it down must not be optional."""
        if not self.container:
            return
        subprocess.run([docker_path(), 'rm', '-f', self.name],
                       capture_output=True, text=True, timeout=120, env=docker_env())
        self.container = ''

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


def demo():
    """Self-check. Skips loudly when Docker is unavailable rather than passing silently."""
    if not docker_ready():
        print('docker sandbox self-check SKIPPED (daemon unavailable)')
        return
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / 'ws'
        workspace.mkdir()
        (workspace / 'seed.txt').write_text('seed', encoding='utf-8')
        run_id = f'selfcheck{int(time.time())}'
        with SwarmSandbox(run_id, workspace, image='python:3.12-slim') as box:
            seen = box.exec(['cat', f'{WORKDIR}/seed.txt'])
            assert seen.stdout.strip() == 'seed', seen
            assert box.inside(workspace) == WORKDIR
            assert box.inside(workspace / 'a' / 'b.txt') == f'{WORKDIR}/a/b.txt'
            wrote = box.exec(['python', '-c',
                              f'open("{WORKDIR}/made.txt","w").write("from-container")'])
            assert wrote.returncode == 0, wrote.stderr
            assert (workspace / 'made.txt').read_text() == 'from-container'
            # Two "agents" in one container really do share a filesystem.
            box.exec(['python', '-c', f'open("{WORKDIR}/shared.txt","w").write("a")'])
            both = box.exec(['cat', f'{WORKDIR}/shared.txt'])
            assert both.stdout.strip() == 'a', both
            # Nothing above /workspace is writable, enforced by --user regardless of image.
            escaped = box.exec(['python', '-c', 'open("/etc/escape","w").write("x")'])
            assert escaped.returncode != 0, 'sandbox let an agent write outside /workspace'
            whoami = box.exec(['id', '-u'])
            assert whoami.stdout.strip() != '0', f'sandbox is running as root: {whoami.stdout}'
            name = box.name
        gone = subprocess.run([docker_path(), 'ps', '-a', '--filter', f'name={name}',
                               '--format', '{{.Names}}'], capture_output=True, text=True,
                              env=docker_env())
        assert name not in gone.stdout, f'container survived teardown: {gone.stdout}'
    print('docker sandbox self-check ok (non-root, writes outside /workspace blocked)')


if __name__ == '__main__':
    demo()
