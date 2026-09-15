# Gym Tracker application image.
#
# Logs go to stdout, never to a file inside the container. The Docker json-file
# driver captures stdout to /var/lib/docker/containers/<id>/<id>-json.log, which
# is the file Filebeat tails — so writing a log file here would produce a second
# copy nothing reads, growing until the disk filled.

FROM python:3.11-slim

# PYTHONUNBUFFERED is not optional here. Python buffers stdout when it is a pipe
# rather than a terminal, which is exactly the case in a container, so without
# this a log line can sit in the process buffer for minutes. Filebeat would then
# report a gap that looks like downtime and is really just buffering.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# psycopg2-binary ships wheels, so no compiler is needed; curl is here for the
# container healthcheck below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first, as their own layer: application code changes on every
# commit, requirements.txt rarely does, so this keeps the slow layer cached.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user. A container that does not need to write to its own
# filesystem should not be able to.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

# One worker, deliberately.
#
# prometheus_client keeps counters in process memory. With several workers each
# would hold its own copy, and Prometheus — scraping through one port — would be
# load-balanced across them, so every scrape would return a different worker's
# partial totals and the counters would appear to jump backwards. Running
# multi-worker needs PROMETHEUS_MULTIPROC_DIR and the MultiProcessCollector;
# for this workload one worker is simpler and enough. See docs/REPORT.md.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
