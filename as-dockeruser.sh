#!/bin/sh
# Runs a command as the scraper user: the one privilege drop of the image, used by the cron
# job lines (user field root, so the root shell can open /proc/1/fd/1) and by the entrypoint.
#   as-dockeruser <command> [args...]
# The command runs in /app (the image's working directory, as with docker exec) with the
# user's HOME: a root cron job starts in /root, which the user cannot read.
cd /app || exit 1
HOME=/home/@USERNAME@
export HOME
exec setpriv --reuid=@USERNAME@ --regid=@USERNAME@ --init-groups "$@"
