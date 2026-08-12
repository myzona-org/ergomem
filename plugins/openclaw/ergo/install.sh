#!/bin/bash
# Prints a ready-to-paste OpenClaw install command + config block for the
# Ergo memory plugin. Read-only: probes the engine's /health, writes nothing.
set -u

ERGO_API_URL="${ERGO_API_URL:-http://127.0.0.1:8788}"
PROJECT="${ERGO_PROJECT:-}"

if [ -z "$PROJECT" ]; then
  echo "ERGO_PROJECT is not set." >&2
  echo "Set it to the project name you want this OpenClaw install scoped to," >&2
  echo "e.g.: ERGO_PROJECT=my-project ./install.sh" >&2
  echo "(a guessed default here would silently mis-scope your memory)." >&2
  exit 1
fi

json_safe_or_die() {
  case "$2" in
    *'"'*|*'\'*|*$'\t'*|*$'\n'*|*$'\r'*)
      echo "$1 contains a quote, backslash, or control character," >&2
      echo "which would break the printed JSON config block." >&2
      exit 1
      ;;
  esac
}
json_safe_or_die ERGO_PROJECT "$PROJECT"
json_safe_or_die ERGO_API_URL "$ERGO_API_URL"

if ! curl -fsS --max-time 4 "$ERGO_API_URL/health" >/dev/null 2>&1; then
  echo "Ergo engine not reachable at $ERGO_API_URL/health." >&2
  echo "Start it first, e.g.: python -m ergo.contradiction.server" >&2
  echo "Then re-run this script, or set ERGO_API_URL if it runs elsewhere." >&2
  exit 1
fi

cat <<EOF
git clone https://github.com/myzona-org/ergomem.git
cd ergomem/plugins/openclaw/ergo
npm install
openclaw plugins install --link "\$(pwd)"

Then add this to your OpenClaw config:

{
  "plugins": {
    "slots": { "memory": "memory-ergo" },
    "entries": {
      "memory-ergo": {
        "config": {
          "baseUrl": "$ERGO_API_URL",
          "project": "$PROJECT"
        }
      }
    }
  }
}
EOF
