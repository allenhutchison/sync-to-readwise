#!/bin/sh
set -e

# If DOPPLER_TOKEN is set, wrap the command with `doppler run` so secrets
# from Doppler are injected into the process environment. Otherwise run
# the command directly — lets the same image work for CI, local dev with
# a plain .env, and production with Doppler without any image changes.
if [ -n "$DOPPLER_TOKEN" ]; then
    exec doppler run -- "$@"
else
    exec "$@"
fi
