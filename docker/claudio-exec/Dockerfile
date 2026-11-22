# claudio/agent-exec — the executor-side Claudio image with the test
# toolchain the evidence gate needs.
#
# The stock claudio/agent image has no pytest, so any task whose `run:`
# verification command invokes pytest fails the gate on environment, not
# on the agent's work (live finding, claudio#58 task 2 attempt 3: the
# code greps passed, `python3 -m pytest` died with "No module named
# pytest"). Claudio itself stays unmodified — this only extends the
# published image.
#
# Build:  docker build -t claudio/agent-exec:latest docker/claudio-exec/
FROM claudio/agent:latest

USER root
RUN pip install --no-cache-dir pytest pytest-asyncio
USER claudio:claudio
