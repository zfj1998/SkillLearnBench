#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_DIR="$(cd "${ROOT_DIR}/.." && pwd)"
ENV_FILE="${ENV_FILE:-${WORKSPACE_DIR}/.env}"
DATASET="${DATASET:-skilllearnbench/skilllearnbench-a0da045-phase1-v1}"
BENCHMARK_REVISION="${BENCHMARK_REVISION:-a0da045a8bf64b8a8ff20730c4d6ef10dc4e2c5b}"
AGENTHUB_REF="${AGENTHUB_REF:-643ef93cb05232d5c259cedd5080cf6bfc7371b2}"
AP_CLUSTER="${AP_CLUSTER:-sh-prod-1}"
AP_QUEUE="${AP_QUEUE:-queue-o0cgkmwaegbk7onwxcqz}"
OUTPUT_DIR="${ROOT_DIR}/ap/artifacts/publisher"

if [[ -f "${ENV_FILE}" ]]; then
  set +x
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
fi
AP_API_KEY="${AP_API_KEY:-${AP_KEY:-}}"
export AP_API_KEY AP_CLUSTER
: "${AP_API_KEY:?AP_API_KEY is required}"
mkdir -p "${OUTPUT_DIR}"

name="skilllearnbench-phase1-publish-$(date -u +%Y%m%d-%H%M%S)"
response="${OUTPUT_DIR}/${name}.json"
params="$(jq -cn \
  --arg revision "${BENCHMARK_REVISION}" \
  --arg dataset "${DATASET}" \
  '{benchmark_revision:$revision,dataset:$dataset}')"

ap job create skilllearnbench-dataset-publish \
  --agenthub-ref "${AGENTHUB_REF}" \
  --instance-id publish \
  --params "${params}" \
  --suite-name "${name}" \
  --queue "${AP_QUEUE}" \
  --priority medium \
  --format json | tee "${response}"
echo "submitted publisher; response saved to ${response}"
