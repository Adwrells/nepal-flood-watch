#!/bin/sh
# Container entrypoint: restore the database, then run the app under Litestream.
#
# The ordering matters and is easy to get wrong:
#   1. restore BEFORE the app starts, or the app creates an empty flood.db and
#      Litestream then faithfully replicates that emptiness over the backup;
#   2. `replicate -exec` so Litestream owns the process tree -- it flushes the
#      final WAL frames when the app exits, which a background `&` would skip.
set -e

DB=/app/data/flood.db
CONFIG=/app/deploy/litestream.yml

# No bucket configured means someone is running the image locally or on a
# volume-backed host. That is a legitimate setup, so run the app plainly rather
# than failing -- but say so, because silent non-replication is the failure mode
# that only reveals itself when you need the backup.
if [ -z "$LITESTREAM_BUCKET" ]; then
    echo "litestream: LITESTREAM_BUCKET not set - running WITHOUT replication."
    echo "litestream: flood.db is only as durable as this container's volume."
    exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 \
        --workers 1 --proxy-headers --forwarded-allow-ips '*'
fi

# Defaults let the same config serve real AWS and an S3-compatible emulator.
export LITESTREAM_ENDPOINT="${LITESTREAM_ENDPOINT:-}"
export LITESTREAM_FORCE_PATH_STYLE="${LITESTREAM_FORCE_PATH_STYLE:-false}"
export AWS_REGION="${AWS_REGION:-us-east-1}"

if [ -f "$DB" ]; then
    echo "litestream: $DB already present, skipping restore."
else
    echo "litestream: restoring $DB from s3://$LITESTREAM_BUCKET ..."
    # -if-replica-exists makes a first-ever deploy succeed rather than abort:
    # there is legitimately nothing to restore the first time.
    litestream restore -if-replica-exists -config "$CONFIG" "$DB"
    if [ -f "$DB" ]; then
        echo "litestream: restored $(stat -c %s "$DB") bytes."
    else
        echo "litestream: no existing replica - starting from an empty database."
    fi
fi

echo "litestream: replicating to s3://$LITESTREAM_BUCKET (sync 10s)"
exec litestream replicate -config "$CONFIG" -exec \
    "python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips *"
