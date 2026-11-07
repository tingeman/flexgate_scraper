#!/bin/bash
# Runs as root: cron must run as root to start the jobs. The scraper itself runs as the
# unprivileged scraper user (FLEXGATE_SCRAPER_USER, set in the Dockerfile), never as root.

set -e  # exit on error

SCRAPER_USER="${FLEXGATE_SCRAPER_USER:-dockeruser}"
DOWNLOADS=/app/downloads

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') - $*"; }
# The image's one privilege drop, the same the cron job lines use
as_user() { /usr/local/bin/as-dockeruser "$@"; }

# Check if settings.yml exists and is not empty
if [ ! -s /app/settings.yml ]; then
    echo "Error: /app/settings.yml is missing or empty."
    exit 1
fi
if ! as_user test -r /app/settings.yml; then
    echo "Error: /app/settings.yml is not readable by the scraper user $SCRAPER_USER."
    exit 1
fi

# Refuse to start with settings the scraper would reject (the error is printed above).
# Checked as the scraper user, as the cron job runs it (the scraper refuses root).
if ! as_user python /app/flexgate_scraper.py --check-settings; then
    echo "Error: /app/settings.yml is invalid, see the message above."
    exit 1
fi

# Downloads must belong to the scraper user. Files written by earlier versions (which ran as
# root) are handed over once; later starts find nothing to change and log nothing.
if [ -d "$DOWNLOADS" ]; then
    n="$(find "$DOWNLOADS" \( ! -user "$SCRAPER_USER" -o ! -group "$SCRAPER_USER" \) -printf '.' | wc -c)"
    if [ "$n" -gt 0 ]; then
        if chown -R -h "$SCRAPER_USER:$SCRAPER_USER" "$DOWNLOADS"; then
            log "$DOWNLOADS: owner of $n entries changed to $SCRAPER_USER:$SCRAPER_USER"
        else
            log "WARNING: could not change the owner of $n entries in $DOWNLOADS to $SCRAPER_USER"
        fi
    fi
fi

# A lock left by an earlier start of this container: no run is in progress yet
rm -f /tmp/flexgate_scraper.lock

# For debian based distros, the cron jobs may be in separate files
# All cron files are copied to /etc/cron.d

# Copy cron jobs from /app/cron_jobs to /etc/cron.d/
for cron_file in /app/cron_jobs/*; do
    if [ -f "$cron_file" ]; then
        target="/etc/cron.d/$(basename "$cron_file")"
        cp "$cron_file" "$target"
        # ensure the file has the correct permissions
        chmod 0644 "$target"
        # ensure there is a blank line at the end of the file
        echo "" >> "$target"
    fi
done
log "/etc/cron.d/ updated with custom cron jobs"

# Start cron in the foreground (debian distro)
exec cron -f
