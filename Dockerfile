# Container image for the abaqus-mcp server.
#
# IMPORTANT: this image deliberately does NOT contain Abaqus. Abaqus is licensed
# commercial software and cannot be redistributed, so the image ships the agent
# only. What you get out of the box is a server that starts, advertises its 13
# tools, validates simulation specs and parses solver output -- everything that
# does not require the solver itself.
#
# To actually run jobs, mount your host's Abaqus installation and point the
# agent at it:
#
#   docker run --rm -i \
#     -v /opt/SIMULIA:/opt/SIMULIA:ro \
#     -v "$PWD/runs:/work/runs" \
#     -e ABAQUS_AGENT_COMMAND=/opt/SIMULIA/Commands/abaqus \
#     -e ABAQUS_AGENT_RUNS_DIR=/work/runs \
#     abaqus-mcp
#
# The licence server must also be reachable from inside the container. Call the
# check_environment tool first -- it reports exactly what was found.

FROM python:3.12-slim

# Keep Python from buffering stdout: the MCP transport is line-oriented over
# stdio, and a buffered reply looks like a hung server to the client.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copy only what the build needs before installing, so the dependency layer is
# cached across source edits.
COPY pyproject.toml README.md LICENSE ./
COPY abaqus_mcp ./abaqus_mcp

RUN pip install --no-cache-dir .

# Job output goes here; mount a volume over it to keep results on the host.
ENV ABAQUS_AGENT_RUNS_DIR=/work/runs
RUN mkdir -p /work/runs
WORKDIR /work

# Run as a non-root user -- the server needs no privileges of its own.
RUN useradd --create-home --uid 1000 agent && chown -R agent /work
USER agent

# stdio transport: the MCP client speaks JSON-RPC over stdin/stdout, so the
# container must be run with -i and must not print anything else to stdout.
ENTRYPOINT ["abaqus-mcp"]
