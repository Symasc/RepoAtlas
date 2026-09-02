#!/usr/bin/env bash
set -euo pipefail

# GROUND TRUTH: these three targets are static and MUST resolve.
source ./env.sh
python3 ../python-service/src/app.py
salt-call state.apply service

deploy_helper() {
    echo "${SERVICE_HOST}"
}

export DEPLOY_STAGE=production
echo "$DB_URL"
env LOG_LEVEL=debug ./notify.sh
