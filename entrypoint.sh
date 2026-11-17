#!/bin/sh
set -e

for file in config.toml curl.txt; do
  if [ ! -f "/data/$file" ]; then
    echo "Missing: /data/$file – please put it into the data folder."
    exit 1
  fi
done

# Migrate an existing setup from the old German names (does nothing if already migrated)
python /app/migrate_config.py /data

# Remove leftovers of a previous run (otherwise Chromium reports "profile in use")
rm -f /tmp/.X99-lock /data/profile/SingletonLock /data/profile/SingletonCookie /data/profile/SingletonSocket

# Start virtual display
Xvfb :99 -screen 0 1280x900x24 -nolisten tcp >/dev/null 2>&1 &
export DISPLAY=:99
sleep 1

# exec: Python receives the signals of "docker stop" directly
exec python /app/location_bridge.py "$@"
