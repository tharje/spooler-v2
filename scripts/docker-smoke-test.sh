#!/usr/bin/env bash
# Builds the real Docker image and checks that it actually works.
#
#   scripts/docker-smoke-test.sh [image tag]
#
# pytest runs on plain Python, so it cannot catch what only breaks inside the
# image: a module missing from the COPY, a different websockets version than
# the one the code was tested with, a server that crashes on start. This does
# a build, starts the container with no printers and an empty data volume, and
# checks that it answers /api/health with the version the changelog names.
set -euo pipefail

cd "$(dirname "$0")/.."
image="${1:-spooler-smoke:test}"
name="spooler-smoke-$$"
fail() { echo "SMOKE TEST FAILED: $*" >&2; echo "--- container log ---" >&2; docker logs "$name" 2>&1 | tail -40 >&2 || true; exit 1; }
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker build -q -t "$image" .

docker run -d --name "$name" -p 127.0.0.1::8080 "$image" >/dev/null
port="$(docker port "$name" 8080/tcp | head -1 | sed 's/.*://')"

# Wait for the server to answer (up to 60 s), and stop at once if it has crashed.
body=""
for _ in $(seq 1 60); do
  [ "$(docker inspect -f '{{.State.Running}}' "$name")" = "true" ] || fail "the container stopped"
  if body="$(curl -fsS "http://127.0.0.1:${port}/api/health" 2>/dev/null)"; then break; fi
  body=""; sleep 1
done
[ -n "$body" ] || fail "/api/health did not answer within 60 s"

expected="$(python3 -c "import json; print(json.load(open('public/changelog.json'))[0]['version'])")"
python3 - "$body" "$expected" <<'PY' || fail "unexpected /api/health answer"
import json, sys
d = json.loads(sys.argv[1])
assert d.get("status") == "ok", f"status is {d.get('status')!r}"
assert d.get("version") == sys.argv[2], f"version is {d.get('version')!r}, the changelog says {sys.argv[2]!r}"
PY

# The websockets version in the image must be the one pinned in requirements.txt
# (the Dockerfile pins it a second time, and the two have drifted before).
pinned="$(sed -n 's/^websockets==\(.*\)$/\1/p' requirements.txt)"
actual="$(docker exec "$name" python3 -c "import websockets; print(websockets.__version__)")"
[ "$pinned" = "$actual" ] || fail "websockets is $actual in the image but requirements.txt pins $pinned"

echo "Smoke test passed: $image answers /api/health, version $expected, websockets $actual"
