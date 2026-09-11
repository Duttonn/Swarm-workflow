# One container per swarm. Every agent in the swarm execs inside this same box, so they
# share a filesystem and can genuinely collide - which is what makes file claims real
# rather than decorative.
FROM python:3.12-slim

# Node is here for the Gemini CLI; git because agents reach for it constantly.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates git \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

# Same version as the host CLI: older builds do not know gemini-3.8-flash and silently
# fall back to another model.
RUN npm install -g @google/gemini-cli@0.59.0

# Unprivileged by default: a root shell in the sandbox defeats the point of having one.
RUN useradd --create-home --uid 1000 swarm
WORKDIR /workspace
USER swarm

# The harness keeps the container alive and drives it with docker exec.
CMD ["sleep", "infinity"]
