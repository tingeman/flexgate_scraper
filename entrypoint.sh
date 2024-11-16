#!/bin/bash

set -e  # exit on error

# Check if settings.yml exists and is not empty
if [ ! -s /app/settings.yml ]; then
    echo "Error: /app/settings.yml is missing or empty."
    exit 1
fi

# For debian based distros, the cron jobs may be in separate files
# All cron files are copied to /etc/cron.d

# Copy cron jobs from /app/cron_jobs to /etc/cron.d/
for cron_file in /app/cron_jobs/*; do
    if [ -f "$cron_file" ]; then
        cp "$cron_file" /etc/cron.d
        # ensure the file has the correct permissions
        chmod 0644 /etc/cron.d/$(basename "$cron_file")
        # ensure there is a blank line at the end of the file
        echo "" >> /etc/cron.d/$(basename "$cron_file")
    fi
done
echo "$(date '+%Y-%m-%d %H:%M:%S') - /etc/cron.d/ updated with custom cron jobs"

# Start cron in the foreground (debian distro)
exec cron -f