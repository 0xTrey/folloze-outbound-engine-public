#!/usr/bin/env bash
set -euo pipefail

# Load .env first so cron (where macOS Keychain is locked) can still load
# credentials. Do not shell-source the file directly; some secrets contain
# characters like `&` that are valid dotenv values but invalid bare shell.
_ENV_FILE="$HOME/.config/folloze-outbound/.env"

load_env_file() {
  local env_file="$1"
  [[ -f "$env_file" ]] || return 0

  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" == export\ * ]] && line="${line#export }"
    [[ "$line" != *=* ]] && continue

    local key="${line%%=*}"
    local value="${line#*=}"

    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"

    if [[ "$value" == \"*\" && "$value" == *\" ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
      value="${value:1:${#value}-2}"
    fi

    export "$key=$value"
  done < "$env_file"
}

load_env_file "$_ENV_FILE"

security_value() {
  local service="$1"
  security find-generic-password -s "$service" -w 2>/dev/null || true
}

export FOLLOZE_DATABASE_URL="${FOLLOZE_DATABASE_URL:-$(security_value "folloze-outbound-db")}"
export FOLLOZE_SMARTLEAD_API_KEY="${FOLLOZE_SMARTLEAD_API_KEY:-$(security_value "smartlead-api")}"
export FOLLOZE_APOLLO_API_KEY="${FOLLOZE_APOLLO_API_KEY:-$(security_value "apollo-folloze-api")}"
export FOLLOZE_PERPLEXITY_API_KEY="${FOLLOZE_PERPLEXITY_API_KEY:-$(security_value "perplexity-api")}"
export FOLLOZE_BRAVE_API_KEY="${FOLLOZE_BRAVE_API_KEY:-$(security_value "brave-search-api")}"
export FOLLOZE_X_BEARER_TOKEN="${FOLLOZE_X_BEARER_TOKEN:-$(security_value "x-api-bearer-token")}"

if [[ -z "${FOLLOZE_DATABASE_URL}" || -z "${FOLLOZE_SMARTLEAD_API_KEY}" || -z "${FOLLOZE_APOLLO_API_KEY}" ]]; then
  echo "Missing one or more Folloze outbound credentials" >&2
  return 1 2>/dev/null || exit 1
fi
