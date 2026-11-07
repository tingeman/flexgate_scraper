# Base image: python:3.13-slim, pinned by digest (index digest, multi-arch), as of 2026-11-07.
# To update: docker buildx imagetools inspect python:3.13-slim, then regenerate the
# lock file app/requirements.txt inside the same base image (see README).
FROM python:3.13-slim@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc

# Only what the container uses: bash for the entrypoint, cron to schedule the scraper.
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash cron \
    && rm -rf /var/lib/apt/lists/*

# Copy the entrypoint script and set permissions
COPY ./entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN mkdir -p /app \
    && chmod -R 755 /app \
    && mkdir -p /var/log \
    && touch /var/log/cron.log

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY app/requirements.txt .

# Install the dependencies from the hash lock
RUN set -ex && \
    python -m pip install --no-cache-dir --require-hashes -r ./requirements.txt

# Copy the rest of the application code into the container
COPY ./app .

# Set the entrypoint
ENTRYPOINT ["/entrypoint.sh"]

# Command to run the application
# CMD ["python", "flexgate_scraper.py"]
