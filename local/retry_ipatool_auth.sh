#!/bin/zsh
# Retry ipatool App Store login, alternating egress (your proxy vs direct) until Apple's edge accepts.
# Usage: zsh local/retry_ipatool_auth.sh [apple-id-email]
# Optional: AUTH_CODE=123456 to pass a 2FA code; MAX_ATTEMPTS=12; SLEEP_SECONDS=180
set -u

EMAIL="${1:-}"
if [[ -z "$EMAIL" ]]; then read -r "EMAIL?Apple ID email: "; fi
read -s "PW?Apple ID password: "; echo
if [[ -n "${AUTH_CODE:-}" ]]; then echo "using auth code $AUTH_CODE"; fi

MAX=${MAX_ATTEMPTS:-12}
SLEEP=${SLEEP_SECONDS:-180}
ARGS=(auth login --email "$EMAIL" --password "$PW" --verbose)
if [[ -n "${AUTH_CODE:-}" ]]; then ARGS+=(--auth-code "$AUTH_CODE"); fi

for ((i = 1; i <= MAX; i++)); do
  for MODE in proxy direct; do
    print -n -- "--- attempt $i/$MAX via $MODE at "
    date +%H:%M:%S
    if [[ "$MODE" == proxy ]]; then
      ipatool "${ARGS[@]}" 2>&1 | grep -E "ERR|DBG error|success=true" | tail -3
    else
      env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY ipatool "${ARGS[@]}" 2>&1 \
        | grep -E "ERR|DBG error|success=true" | tail -3
    fi
    if ipatool auth info >/dev/null 2>&1; then
      echo "SUCCESS: authenticated. Next: ipatool list-versions -b com.kms.worlddaistar --platform ipad"
      exit 0
    fi
    (( i == MAX && MODE == direct )) && break
    sleep "$SLEEP"
  done
done

echo "Gave up after $MAX attempts. Apple is refusing both egress paths."
exit 1
