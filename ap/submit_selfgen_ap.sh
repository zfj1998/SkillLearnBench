#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_DIR="$(cd "${ROOT_DIR}/.." && pwd)"
ENV_FILE="${ENV_FILE:-${WORKSPACE_DIR}/.env}"
MODE="${1:-dry-run}"
FAMILY="${2:-chinese-poem-generator}"
DRY_RUN="${DRY_RUN:-false}"

if [[ -f "${ENV_FILE}" ]]; then
  set +x
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
fi

AP_CLUSTER="${AP_CLUSTER:-hk-test}"
AP_TEMPLATE="${AP_TEMPLATE:-skilllearnbench-selfgen-smoke}"
: "${AP_AGENTHUB_REF:?Set AP_AGENTHUB_REF to the pushed 40-character Agent-Hub SHA}"
: "${BENCHMARK_REVISION:?Set BENCHMARK_REVISION to the pushed 40-character benchmark SHA}"
[[ "${AP_AGENTHUB_REF}" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid Agent-Hub SHA" >&2; exit 2; }
[[ "${BENCHMARK_REVISION}" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid benchmark SHA" >&2; exit 2; }

export AP_API_KEY="${AP_API_KEY:-${AP_KEY:-}}"
: "${AP_API_KEY:?AP_API_KEY or AP_KEY is required}"
unset AP_HEADERS || true

MODEL="${MODEL:-claude-opus-5}"
case "${MODEL}" in
  qwen3.8-max)
    PROVIDER="${PROVIDER:-openai}"
    FORCE_PROXY="${FORCE_PROXY:-true}"
    MODEL_BASE_URL="${MODEL_BASE_URL:-https://dashscope.aliyuncs.com/compatible-mode/v1}"
    MODEL_API_KEY="${MODEL_API_KEY:-${DASHSCOPE_API_KEY_kimi:-}}"
    REASONING_EFFORT="${REASONING_EFFORT:-xhigh}"
    MODEL_MAX_TOKENS_DEFAULT=18000
    ;;
  claude-opus-5)
    PROVIDER="${PROVIDER:-anthropic}"
    FORCE_PROXY="${FORCE_PROXY:-false}"
    MODEL_BASE_URL="${MODEL_BASE_URL:-https://routify-pub.alibaba-inc.com/protocol/anthropic}"
    MODEL_API_KEY="${MODEL_API_KEY:-${ROUTIFY_MY_KEY_0727:-${ROUTIFY_KEY:-}}}"
    REASONING_EFFORT="${REASONING_EFFORT:-max}"
    MODEL_MAX_TOKENS_DEFAULT=128000
    ;;
  *)
    : "${PROVIDER:?PROVIDER is required for model ${MODEL}}"
    : "${FORCE_PROXY:?FORCE_PROXY is required for model ${MODEL}}"
    : "${MODEL_BASE_URL:?MODEL_BASE_URL is required for model ${MODEL}}"
    : "${MODEL_API_KEY:?MODEL_API_KEY is required for model ${MODEL}}"
    : "${REASONING_EFFORT:?REASONING_EFFORT is required for model ${MODEL}}"
    : "${MAX_TOKENS:?MAX_TOKENS is required for model ${MODEL}}"
    ;;
esac
: "${MODEL_API_KEY:?MODEL_API_KEY or the provider-specific protected key is required}"
MAX_TOKENS="${MAX_TOKENS:-${MODEL_MAX_TOKENS_DEFAULT:-}}"
[[ "${MAX_TOKENS}" =~ ^[1-9][0-9]*$ ]] || {
  echo "MAX_TOKENS must be a positive integer" >&2
  exit 2
}
GH_TOKEN="${GH_TOKEN:-}"
if [[ -z "${GH_TOKEN}" ]] && command -v gh >/dev/null; then
  GH_TOKEN="$(gh auth token 2>/dev/null || true)"
fi

case "${REASONING_EFFORT}" in
  xhigh|max) ;;
  *) echo "REASONING_EFFORT must be xhigh or max" >&2; exit 2 ;;
esac
case "${FORCE_PROXY}:${PROVIDER}" in
  true:openai|false:anthropic) ;;
  *) echo "unsupported FORCE_PROXY/PROVIDER combination" >&2; exit 2 ;;
esac
if [[ "${MODEL}" == "qwen3.8-max" && "${REASONING_EFFORT}" != "xhigh" ]]; then
  echo "qwen3.8-max selfgen must use xhigh; max was not stable on this endpoint" >&2
  exit 2
fi
if [[ "${MODEL}" == "claude-opus-5" && "${REASONING_EFFORT}" != "max" ]]; then
  echo "claude-opus-5 selfgen must use max for the matched comparison" >&2
  exit 2
fi
REQUEST_TIMEOUT_SECONDS="${REQUEST_TIMEOUT_SECONDS:-3600}"
RUNTIME_TIMEOUT_SECONDS="${RUNTIME_TIMEOUT_SECONDS:-72000}"
for timeout_value in "${REQUEST_TIMEOUT_SECONDS}" "${RUNTIME_TIMEOUT_SECONDS}"; do
  [[ "${timeout_value}" =~ ^[1-9][0-9]*$ ]] || {
    echo "timeout values must be positive integer seconds" >&2
    exit 2
  }
done
(( REQUEST_TIMEOUT_SECONDS < RUNTIME_TIMEOUT_SECONDS )) || {
  echo "REQUEST_TIMEOUT_SECONDS must be less than RUNTIME_TIMEOUT_SECONDS" >&2
  exit 2
}
case "${MODE}" in
  dry-run|smoke|full) ;;
  *) echo "usage: $0 {dry-run|smoke|full} [family]" >&2; exit 2 ;;
esac
case "${DRY_RUN}" in
  true|false) ;;
  *) echo "DRY_RUN must be true or false" >&2; exit 2 ;;
esac
SCOREABLE="${SCOREABLE:-$([[ "${MODE}" == "full" ]] && printf true || printf false)}"
case "${SCOREABLE}" in
  true|false) ;;
  *) echo "SCOREABLE must be true or false" >&2; exit 2 ;;
esac

mapfile -t ALL_FAMILIES < <(
  find "${ROOT_DIR}/tasks" -mindepth 2 -maxdepth 2 -type d -name '*-1' \
    -printf '%h\n' | xargs -n1 basename | sort
)
[[ "${#ALL_FAMILIES[@]}" -eq 20 ]] || {
  echo "expected 20 families, found ${#ALL_FAMILIES[@]}" >&2
  exit 2
}

if [[ "${MODE}" == "full" ]]; then
  FAMILIES=("${ALL_FAMILIES[@]}")
else
  printf '%s\n' "${ALL_FAMILIES[@]}" | grep -Fqx -- "${FAMILY}" || {
    echo "unknown family: ${FAMILY}" >&2
    exit 2
  }
  FAMILIES=("${FAMILY}")
fi
if printf '%s\n' "${FAMILIES[@]}" | grep -Fqx github-repo-analytics; then
  : "${GH_TOKEN:?Existing gh authentication or GH_TOKEN is required for github-repo-analytics}"
fi

umask 077
params_file="$(mktemp /tmp/skilllearnbench-selfgen-params.XXXXXX.json)"
private_response="$(mktemp /tmp/skilllearnbench-selfgen-response.XXXXXX.json)"
trap 'rm -f "${params_file}" "${private_response}"' EXIT
jq -n --argjson families "$(printf '%s\n' "${FAMILIES[@]}" | jq -Rsc 'split("\n") | map(select(length > 0))')" \
  --arg github_token "${GH_TOKEN}" \
  '$families | map(
    {instance_id: .} +
    (if . == "github-repo-analytics" then {github_token: $github_token} else {} end)
  )' > "${params_file}"

common="$(jq -cn \
  --arg revision "${BENCHMARK_REVISION}" \
  --arg model "${MODEL}" \
  --arg base "${MODEL_BASE_URL}" \
  --arg api_key "${MODEL_API_KEY}" \
  --arg provider "${PROVIDER}" \
  --arg force_proxy "${FORCE_PROXY}" \
  --arg effort "${REASONING_EFFORT}" \
  --arg scoreable "${SCOREABLE}" \
  --argjson max_tokens "${MAX_TOKENS}" \
  --argjson request_timeout "${REQUEST_TIMEOUT_SECONDS}" \
  --argjson runtime_timeout_sec "${RUNTIME_TIMEOUT_SECONDS}" \
  '{benchmark_revision:$revision,model:$model,model_base_url:$base,
    model_api_key:$api_key,provider:$provider,harbor_agent:"claude-code",
    force_proxy:$force_proxy,reasoning_effort:$effort,max_iterations:200,
    max_tokens:$max_tokens,request_timeout:$request_timeout,runtime_timeout_sec:$runtime_timeout_sec,
    claude_code_version:"2.1.220",scoreable:$scoreable}')"

stamp="$(date -u +%Y%m%d-%H%M%S)"
model_slug="$(printf '%s' "${MODEL}" | tr -cs '[:alnum:]' '-')"
suite="skilllearnbench-${model_slug}-${REASONING_EFFORT}-selfgen-${MODE}-${stamp}"
if [[ -z "${CONCURRENCY:-}" ]]; then
  [[ "${MODE}" == "full" ]] && CONCURRENCY=20 || CONCURRENCY=1
fi
[[ "${CONCURRENCY}" =~ ^[1-9][0-9]*$ ]] || {
  echo "CONCURRENCY must be a positive integer" >&2
  exit 2
}
command=(ap --cluster "${AP_CLUSTER}" job create "${AP_TEMPLATE}"
  --agenthub-ref "${AP_AGENTHUB_REF}"
  --params-list "${params_file}" --params "${common}"
  --suite-name "${suite}" --concurrency "${CONCURRENCY}"
  --priority medium --idempotency --format json)
[[ "${MODE}" == "full" ]] && command+=(--enable-post-process)
[[ "${MODE}" == "dry-run" || "${DRY_RUN}" == "true" ]] && command+=(--dry-run)
"${command[@]}" > "${private_response}"

artifact_dir="${ROOT_DIR}/ap/artifacts/submissions"
mkdir -p "${artifact_dir}"
redacted_response="${artifact_dir}/${suite}-response.redacted.json"
jq 'walk(
  if type == "object" then
    with_entries(
      if (.key | ascii_downcase | test("(^|[-_])(api[-_]?key|authorization|github[-_]?token|gh[-_]?token)$"))
      then .value = "<redacted>" else . end
    )
  else . end
)' "${private_response}" > "${redacted_response}"
chmod 600 "${redacted_response}"

python3 - "${private_response}" "${redacted_response}" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1]))
submission = payload.get("submission", payload)
safe = {
    key: submission.get(key)
    for key in ("group_id", "queue_id", "total", "submitted", "failed")
    if key in submission
}
safe["redacted_response"] = sys.argv[2]
print(json.dumps(safe, ensure_ascii=False, indent=2))
PY
