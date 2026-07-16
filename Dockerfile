# Sandbox for the agentic loop.
#
# The developer agent runs with permissions bypassed INSIDE this container, so its
# blast radius is the container only — never your host or your real credentials.
#
# IMPORTANT: Claude Code REFUSES bypassPermissions when running as root, so this
# image runs everything as the non-root `agent` user.

FROM node:22-bookworm-slim

# Python (orchestrator) + git (ledger). Add `docker.io` / gcloud SDK here if your
# verify gates need container builds or emulators (see README: sandbox tradeoffs).
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 git ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI. Session-resume semantics were verified on 2.1.205 — pin and bump
# deliberately rather than riding latest.
RUN npm install -g @anthropic-ai/claude-code@2.1.205

# Non-root user: required for bypassPermissions, and safer anyway.
RUN useradd -m agent
USER agent
WORKDIR /app

# Git identity + trust for the mounted work repo (the ledger also sets repo-local
# identity as a fallback, but the mount's ownership needs marking safe up front).
RUN git config --global user.name "agentic-loop" \
    && git config --global user.email "agentic-loop@container" \
    && git config --global --add safe.directory /work

COPY --chown=agent:agent orchestrator ./orchestrator
COPY --chown=agent:agent prompts ./prompts
COPY --chown=agent:agent run.py ./run.py

ENV DEV_PERMISSION_MODE=bypassPermissions

# Mount your target repo at /work and pass ANTHROPIC_API_KEY at runtime (never bake it in):
#   docker build -t agentic-loop .
#   docker run --rm -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
#     -v "$(pwd)/target-repo:/work" agentic-loop \
#     "Add a /health endpoint returning {status:ok} with a passing test"
# The mounted repo must be writable by uid 1001 (user `agent`), e.g. `chmod -R a+rw`.
ENTRYPOINT ["python3", "run.py", "--repo", "/work"]
