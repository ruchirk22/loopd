# Sandbox for the agentic loop.
#
# The developer agent runs with permissions bypassed INSIDE this container, so its
# blast radius is the container only — never your host or your real credentials.

FROM node:22-bookworm-slim

# Python (orchestrator) + git (ledger)
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app
COPY orchestrator ./orchestrator
COPY prompts ./prompts
COPY run.py ./run.py

ENV DEV_PERMISSION_MODE=bypassPermissions

# Mount your target repo at /work and pass ANTHROPIC_API_KEY at runtime (never bake it in):
#   docker build -t agentic-loop .
#   docker run --rm -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
#     -v "$(pwd)/target-repo:/work" agentic-loop \
#     "Add a /health endpoint returning {status:ok} with a passing test"
ENTRYPOINT ["python3", "run.py", "--repo", "/work"]
