# Reference container. This library is defense in depth *inside* a container,
# never the trust boundary itself (see README). The parent may stay root so it
# can chown into bind-mounted artifact dirs; each call then drops to a dedicated
# UID before running code, which is what makes RLIMIT_NPROC bind on Linux, where
# root is exempt. If you instead run as a fixed non-root user (USER 10001, no
# per-call drop), add a container pids cap (docker run --pids-limit=N) so
# process_count is enforced at the container layer — without it the enforcement
# gate refuses to start. Swap the COPY+install for `pip install pyplaypen-sandbox`
# to base your own image on this.
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim AS base

RUN groupadd --gid 10001 sandbox && \
    useradd --no-create-home --uid 10001 --gid 10001 sandbox

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps . && chown -R 10001:10001 /app

# Test stage: run the suite on real Linux, where RLIMIT_AS/NPROC and the
# root-to-UID drop actually apply (they no-op on macOS). CI runs it as root
# (privileged branch) and as uid 10001 with --pids-limit (unprivileged branch +
# container-enforced process_count), plus a gate-rejection check (see ci.yml).
FROM base AS test
RUN pip install --no-cache-dir pytest pytest-asyncio
COPY tests/ ./tests/
RUN chown -R 10001:10001 /app/tests
CMD ["pytest", "-q"]
