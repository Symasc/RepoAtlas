#!/usr/bin/env bash
set -euo pipefail

source ./env.sh

export SERVICE_HOST="${SERVICE_HOST:-localhost}"

python3 ../python-service/src/app.py
salt-call state.apply service
