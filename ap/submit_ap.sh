#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_DIR="$(cd "${ROOT_DIR}/.." && pwd)"
ENV_FILE="${ENV_FILE:-${WORKSPACE_DIR}/.env}"
MODE="${1:-}"
DATASET="${DATASET:-skilllearnbench/skilllearnbench-a0da045-phase1-v3}"
AGENTHUB_REF="${AGENTHUB_REF:-25f8c0becb4d17f8edf3a65767629569e4361827}"
AP_CLUSTER="${AP_CLUSTER:-sh-prod-1}"
AP_QUEUE="${AP_QUEUE:-queue-o0cgkmwaegbk7onwxcqz}"
MODEL="${MODEL:-claude-opus-5}"
MODEL_BASE_URL="${MODEL_BASE_URL:-https://routify-pub.alibaba-inc.com/protocol/anthropic}"
AGENT_VERSION="${AGENT_VERSION:-2.1.220}"
HARBOR_IMAGE="${HARBOR_IMAGE:-code-agi-sg-docker-registry-vpc.ap-southeast-1.cr.aliyuncs.com/eflops/harbor-repo:harbor-reasoning-trace-202607071040}"
ARTIFACT_DIR="${ROOT_DIR}/ap/artifacts/submissions"

if [[ -f "${ENV_FILE}" ]]; then
  set +x
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
fi
export AP_CLUSTER
MODEL_API_KEY="${MODEL_API_KEY:-${ROUTIFY_MY_KEY_0727:-}}"
AP_API_KEY="${AP_API_KEY:-${AP_KEY:-}}"
export AP_API_KEY
: "${MODEL_API_KEY:?MODEL_API_KEY or ROUTIFY_MY_KEY_0727 is required}"
: "${AP_API_KEY:?AP_API_KEY is required}"
: "${REASONING_EFFORT:?set REASONING_EFFORT to low, medium, high, xhigh, or max}"
case "${REASONING_EFFORT}" in
  low|medium|high|xhigh|max) ;;
  *) echo "invalid REASONING_EFFORT=${REASONING_EFFORT}" >&2; exit 2 ;;
esac

GH_TOKEN="${GH_TOKEN:-}"
if [[ -z "${GH_TOKEN}" ]] && command -v gh >/dev/null; then
  GH_TOKEN="$(gh auth token 2>/dev/null || true)"
fi
: "${GH_TOKEN:?GH_TOKEN is required for github-repo-analytics}"
mkdir -p "${ARTIFACT_DIR}"
chmod 700 "${ARTIFACT_DIR}"

mapfile -t ALL_INSTANCES < <(
  find "${ROOT_DIR}/tasks" -mindepth 3 -maxdepth 3 -type f -name task.toml \
    -printf '%h\n' | xargs -n1 basename | sort
)
[[ "${#ALL_INSTANCES[@]}" -eq 100 ]] || {
  echo "expected 100 instances, found ${#ALL_INSTANCES[@]}" >&2
  exit 2
}

make_params() {
  local output="$1"
  local condition="$2"
  shift 2
  local instance extra first=true
  printf '[\n' > "${output}"
  chmod 600 "${output}"
  for instance in "$@"; do
    [[ "${first}" == true ]] || printf ',\n' >> "${output}"
    first=false
    # The pinned generic template appends /v1 for native Claude Code, while
    # Claude Code 2.1.220 appends /v1/messages itself. Forward the canonical
    # Routify base directly to the agent process to avoid /v1/v1/messages.
    extra="$(jq -cn --arg base_url "${MODEL_BASE_URL}" \
      '{ANTHROPIC_BASE_URL:$base_url}')"
    if [[ "${instance}" == github-repo-analytics-* ]]; then
      extra="$(jq -cn --arg base_url "${MODEL_BASE_URL}" --arg token "${GH_TOKEN}" \
        '{ANTHROPIC_BASE_URL:$base_url,GH_TOKEN:$token}')"
    fi
    jq -cn --arg id "${instance}" --arg split "${condition}" \
      --argjson agent_extra_env "${extra}" \
      '{instance_id:$id,split:$split,agent_extra_env:$agent_extra_env}' >> "${output}"
  done
  printf '\n]\n' >> "${output}"
}

redact() {
  python3 -c 'import re,sys
s=sys.stdin.read()
s=re.sub(r"(model_api_key|api_key|GH_TOKEN)(.{0,20}?)([A-Za-z0-9_./+\-=]{12,})", r"\1\2<redacted>", s, flags=re.I)
print(s, end="")'
}

submit_condition() {
  local condition="$1"
  local action="$2"
  local concurrency="$3"
  shift 3
  local stamp name params common output
  stamp="$(date -u +%Y%m%d-%H%M%S)"
  name="skilllearnbench-opus5-${condition}-${action}-${stamp}"
  params="${ARTIFACT_DIR}/${name}-params.json"
  output="${ARTIFACT_DIR}/${name}-response.redacted.json"
  make_params "${params}" "${condition}" "$@"
  common="$(jq -cn \
    --arg image "${HARBOR_IMAGE}" \
    --arg dataset "${DATASET}" \
    --arg model "${MODEL}" \
    --arg base_url "${MODEL_BASE_URL}" \
    --arg api_key "${MODEL_API_KEY}" \
    --arg version "${AGENT_VERSION}" \
    --arg effort "${REASONING_EFFORT}" \
    '{docker_image:$image,dataset_type:"local",dataset:$dataset,
      harbor_agent:"claude-code",harbor_env:"docker",provider:"anthropic",
      native_anthropic:"true",force_proxy:"false",model:$model,
      model_base_url:$base_url,model_api_key:$api_key,agent_version:$version,
      reasoning_effort:$effort,max_tokens:128000,max_thinking_tokens:127000,
      n_attempts:1,n_concurrent:1,max_retries:0,max_iterations:200,
      timeout_multiplier:4,agent_timeout_multiplier:4,
      verifier_timeout_multiplier:4,environment_build_timeout_multiplier:4,
      runtime_timeout_sec:30000,override_cpus:8,override_memory_mb:16384,
      override_storage_mb:20480,skip_confirm:"true"}')"

  local -a command=(ap job create skillsbench
    --agenthub-ref "${AGENTHUB_REF}"
    --params-list "${params}"
    --params "${common}"
    --suite-name "${name}"
    --queue "${AP_QUEUE}"
    --concurrency "${concurrency}"
    --priority medium
    --format json)
  [[ "${action}" == dry-run ]] && command+=(--dry-run)
  "${command[@]}" | redact | tee "${output}"
  echo "saved redacted response: ${output}"
}

case "${MODE}" in
  dry-run)
    submit_condition no_skill dry-run 2 "${ALL_INSTANCES[@]}"
    submit_condition human_authored dry-run 2 "${ALL_INSTANCES[@]}"
    ;;
  smoke)
    submit_condition no_skill smoke 1 chinese-poem-generator-1
    submit_condition human_authored smoke 1 chinese-poem-generator-1
    ;;
  full)
    submit_condition no_skill full "${CONCURRENCY:-20}" "${ALL_INSTANCES[@]}"
    submit_condition human_authored full "${CONCURRENCY:-20}" "${ALL_INSTANCES[@]}"
    ;;
  *)
    echo "usage: REASONING_EFFORT=<level> $0 {dry-run|smoke|full}" >&2
    exit 2
    ;;
esac
