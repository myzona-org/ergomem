#!/bin/bash
# Run the prose eval with the litellm normalizer + shared norm cache.
# Sources the ergo-engine key by var name to avoid inline-substitution issues.
set -e
cd "$(dirname "$0")/../.."
source .venv/bin/activate

# Operator-specific paths and hostnames are configuration, not repo content:
# point these at your own proxy and env file. Defaults are placeholders, so a
# missing file fails loudly here rather than surfacing as a confusing auth
# error from the proxy several steps later.
ERGO_LITELLM_ENV="${ERGO_LITELLM_ENV:-$HOME/.secrets/ergo-litellm.env}"
if [ ! -f "$ERGO_LITELLM_ENV" ]; then
    echo "error: $ERGO_LITELLM_ENV not found." >&2
    echo "       Set ERGO_LITELLM_ENV to the env file exporting ERGO_LITELLM_KEY." >&2
    exit 1
fi
source "$ERGO_LITELLM_ENV"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
export ERGO_LLM_PROVIDER=litellm ERGO_LLM_MODEL=claude-haiku
export ERGO_LLM_CACHE=evals/contradiction/.cache/llm_norm_litellm_haiku.json
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://litellm.internal:4000/v1}"
export OPENAI_API_KEY="$ERGO_LITELLM_KEY"
python evals/prose/evaluate.py "$@"
