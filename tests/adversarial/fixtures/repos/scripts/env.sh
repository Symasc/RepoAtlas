#!/usr/bin/env bash
# GROUND TRUTH: this script EXPORTS SERVICE_HOST and DB_URL.
export SERVICE_HOST=scripts.internal.example
export DB_URL="postgres://localhost/app"
LOCAL_ONLY=not-exported
echo "$LOCAL_ONLY"
