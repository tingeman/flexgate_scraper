# Use the official Python slim image from the Docker Hub
FROM python:3.13-slim


# Install additional packages
RUN apt-get update \
    && apt-get install -y bash cron rsync sudo curl nano wget unzip git\
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

# Install the dependencies
RUN set -ex && \
    python -m pip install --no-cache-dir -r ./requirements.txt

# Copy the rest of the application code into the container
COPY ./app .

# Set the entrypoint
ENTRYPOINT ["/entrypoint.sh"]

# Command to run the application
# CMD ["python", "flexgate_scraper.py"]
