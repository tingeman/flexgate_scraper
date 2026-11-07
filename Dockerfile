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

# The scraper runs as this unprivileged user (same convention as the other images: uid = gid
# 30000). The container's main process stays root, because cron must start the jobs; cron
# job lines and the entrypoint drop to this user with /usr/local/bin/as-dockeruser, and the
# scraper refuses to run as root. No password, no sudo.
ARG USERNAME=dockeruser
ARG USER_UID=30000
ARG USER_GID=$USER_UID
ENV FLEXGATE_SCRAPER_USER=$USERNAME
RUN groupadd --gid "$USER_GID" "$USERNAME" \
    && useradd --uid "$USER_UID" --gid "$USER_GID" --create-home --home-dir "/home/$USERNAME" "$USERNAME"

# The privilege drop (setpriv from util-linux, part of the base image), with the user name
# filled in: cron jobs do not see the container's environment
COPY ./as-dockeruser.sh /usr/local/bin/as-dockeruser
RUN sed -i "s/@USERNAME@/$USERNAME/g" /usr/local/bin/as-dockeruser \
    && chmod 0755 /usr/local/bin/as-dockeruser

# /app/downloads is owned by the user, so a new empty volume mounted there inherits that
# owner. /var/lib/flexgate_scraper holds the scraper's state (status file).
RUN mkdir -p /app \
    && chmod -R 755 /app \
    && install -d -o "$USERNAME" -g "$USERNAME" -m 0755 /app/downloads /var/lib/flexgate_scraper

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
