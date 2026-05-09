FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install the Doppler CLI. The entrypoint (added below) wraps CMD with
# `doppler run` when DOPPLER_TOKEN is set, so all runtime secrets can be
# injected at container start instead of baked into the image or copied
# to the host.
RUN apt-get update && apt-get install -y --no-install-recommends \
        apt-transport-https ca-certificates curl gnupg \
    && curl -sLf --retry 3 --tlsv1.2 --proto "=https" \
        'https://packages.doppler.com/public/cli/gpg.DE2A7741A397C129.key' \
        | gpg --dearmor -o /usr/share/keyrings/doppler-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/doppler-archive-keyring.gpg] https://packages.doppler.com/public/cli/deb/debian any-version main" \
        > /etc/apt/sources.list.d/doppler-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends doppler \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install .

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh

RUN useradd --create-home --shell /bin/bash app \
    && mkdir -p /data \
    && chown -R app:app /data /app
USER app

VOLUME ["/data"]

# `docker exec` / `docker compose exec` bypass ENTRYPOINT, so any management
# command that needs Doppler-injected secrets must invoke the entrypoint
# explicitly, e.g.:
#   docker compose exec sync-to-readwise \
#       /usr/local/bin/docker-entrypoint.sh sync-to-readwise sync-once youtube
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["sync-to-readwise", "run"]
