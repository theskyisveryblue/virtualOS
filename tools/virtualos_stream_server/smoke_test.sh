#!/usr/bin/env bash
set -euo pipefail

HOST="${1:-127.0.0.1}"
PORT="${2:-8899}"
SEQ_COUNT="${SEQ_COUNT:-60}"
CON_COUNT="${CON_COUNT:-30}"

BASE_URL="http://${HOST}:${PORT}"
TMP_DIR="${TMPDIR:-/tmp}/virtualos-smoke"
mkdir -p "${TMP_DIR}"
AUTH_TOKEN="${AUTH_TOKEN:-}"
AUTH_ARGS=()
if [[ -n "${AUTH_TOKEN}" ]]; then
  AUTH_ARGS=(-H "Authorization: Bearer ${AUTH_TOKEN}")
fi

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing dependency: $1" >&2
    exit 2
  }
}

need curl
need jq

echo "== health =="
curl -fsS "${BASE_URL}/health" | jq -c .

echo
echo "== discover =="
curl -fsS "${BASE_URL}/discover" | jq -c .

echo
echo "== sequential frame test (${SEQ_COUNT}) =="
ok=0
fail=0
for i in $(seq 1 "${SEQ_COUNT}"); do
  out="${TMP_DIR}/seq_${i}.jpg"
  code="$(curl -sS "${AUTH_ARGS[@]}" -o "${out}" -w '%{http_code}' "${BASE_URL}/frame.jpg?seq=${i}&source=window&app=Codex&max=1400" || true)"
  size="$(wc -c <"${out}" 2>/dev/null || echo 0)"
  if [[ "${code}" == "200" ]] && [[ "${size}" -gt 10000 ]]; then
    ok=$((ok + 1))
  else
    fail=$((fail + 1))
  fi
done
echo "sequential summary: ok=${ok} fail=${fail}"

echo
echo "== concurrent frame test (${CON_COUNT}) =="
rm -f "${TMP_DIR}/con_results.txt"
for i in $(seq 1 "${CON_COUNT}"); do
  (
    out="${TMP_DIR}/con_${i}.jpg"
    code="$(curl -sS "${AUTH_ARGS[@]}" -o "${out}" -w '%{http_code}' "${BASE_URL}/frame.jpg?con=${i}&source=screen&app=Codex&max=1400" || true)"
    size="$(wc -c <"${out}" 2>/dev/null || echo 0)"
    if [[ "${code}" == "200" ]] && [[ "${size}" -gt 10000 ]]; then
      echo ok >>"${TMP_DIR}/con_results.txt"
    else
      echo "fail:${code}:${size}" >>"${TMP_DIR}/con_results.txt"
    fi
  ) &
done
wait
awk '
BEGIN { ok=0; fail=0 }
/^ok$/ { ok++ }
/^fail:/ { fail++; print }
END { printf("concurrent summary: ok=%d fail=%d\n", ok, fail) }
' "${TMP_DIR}/con_results.txt"

echo
echo "== control endpoints =="
curl -fsS "${AUTH_ARGS[@]}" -X POST "${BASE_URL}/control/focus" -H 'content-type: application/json' -d '{"app":"Codex"}' | jq -c .
curl -fsS "${AUTH_ARGS[@]}" -X POST "${BASE_URL}/control/type" -H 'content-type: application/json' -d '{"text":"[smoke-test]"}' | jq -c .
curl -fsS "${AUTH_ARGS[@]}" -X POST "${BASE_URL}/control/key" -H 'content-type: application/json' -d '{"key":"enter"}' | jq -c .
curl -fsS "${AUTH_ARGS[@]}" -X POST "${BASE_URL}/control/click" -H 'content-type: application/json' -d '{"x":300,"y":300}' | jq -c .
curl -fsS "${AUTH_ARGS[@]}" -X POST "${BASE_URL}/control/click" -H 'content-type: application/json' -d '{"nx":0.5,"ny":0.5,"source":"window","app":"Codex"}' | jq -c .

echo
echo "== result =="
if [[ "${fail}" -eq 0 ]] && ! grep -q '^fail:' "${TMP_DIR}/con_results.txt"; then
  echo "PASS"
else
  echo "FAIL"
  exit 1
fi
