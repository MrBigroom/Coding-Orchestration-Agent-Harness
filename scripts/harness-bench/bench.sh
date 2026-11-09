#!/usr/bin/env bash
# Measure each coding harness's request overhead: install the CLIs into .work/, point
# them at a local logging mock API (mock_server.py), send one trivial prompt, and count
# the tokens each one sends (count.py). No real API key or network model is used.
#
# usage: scripts/harness-bench/bench.sh [harness ...]
#   harnesses: qwen_code qwen_code_tuned claude_code opencode codex crush pi pi_cache
#              dsh aider mini   (default: all)
#   opt-in:    qoder qoder_cache — need a logged-in Qoder account whose plan allows custom
#              model providers (BYOK). They run with your real HOME so the login is used;
#              the mock provider is added only via --settings for that run.
# needs: node >= 22, npm, uv, git; `claude` on PATH for claude_code.
set -euo pipefail
B="$(cd "$(dirname "$0")" && pwd)"
W="$B/.work"
PORT="${BENCH_PORT:-8765}"
U="http://127.0.0.1:$PORT"
Q="Reply with exactly: OK"
mkdir -p "$W"

# ---------------------------------------------------------------- install (once)
if [ ! -d "$W/node_modules" ]; then
  echo '{"name": "harness-bench", "private": true}' >"$W/package.json"
  (cd "$W" && npm install --no-fund --no-audit \
    @qwen-code/qwen-code opencode-ai @openai/codex @deepseek-ai/dsh @charmland/crush \
    @earendil-works/pi-coding-agent)
fi
if [ ! -x "$W/py/bin/python" ]; then
  uv venv -q "$W/py" && uv pip install -q -p "$W/py/bin/python" dashscope mini-swe-agent mcp
fi
if [ ! -x "$W/py312/bin/aider" ]; then # aider needs Python <= 3.12 (audioop)
  uv venv -q -p 3.12 "$W/py312" && uv pip install -q -p "$W/py312/bin/python" aider-chat
fi
P="$W/node_modules/.bin"

# ---------------------------------------------------------------- per-harness configs
C="$W/cfg"
mkdir -p "$C/opencode/.config/opencode" "$C/codex/.codex" "$C/crush/.config/crush" \
  "$C/pi/.pi/agent" "$C/pi_cache/.pi/agent"
cat >"$C/opencode/.config/opencode/opencode.json" <<EOF
{"autoupdate":false,"share":"disabled","model":"mock/qwen3.8-flash","small_model":"mock/qwen3.8-flash",
 "provider":{"mock":{"npm":"@ai-sdk/openai-compatible","name":"Mock",
   "options":{"baseURL":"$U/v1","apiKey":"x"},"models":{"qwen3.8-flash":{"name":"qwen3.8-flash"}}}}}
EOF
cat >"$C/codex/.codex/config.toml" <<EOF
model = "qwen3.8-flash"
model_provider = "mock"
[model_providers.mock]
name = "mock"
base_url = "$U/v1"
env_key = "MOCK_API_KEY"
wire_api = "responses"
EOF
cat >"$C/crush/.config/crush/crush.json" <<EOF
{"providers":{"mock":{"type":"openai-compat","base_url":"$U/v1","api_key":"x",
  "models":[{"id":"qwen3.8-flash","name":"qwen3.8-flash","context_window":128000,"default_max_tokens":8000}]}},
 "models":{"large":{"model":"qwen3.8-flash","provider":"mock"},"small":{"model":"qwen3.8-flash","provider":"mock"}}}
EOF
cat >"$C/pi/.pi/agent/models.json" <<EOF
{"providers":{"mock":{"baseUrl":"$U/v1","api":"openai-completions","apiKey":"x",
  "models":[{"id":"qwen3.8-flash","name":"qwen3.8-flash"}]}}}
EOF
cat >"$C/pi_cache/.pi/agent/models.json" <<EOF
{"providers":{"mock":{"baseUrl":"$U/v1","api":"openai-completions","apiKey":"x",
  "compat":{"cacheControlFormat":"anthropic"},
  "models":[{"id":"qwen3.8-flash","name":"qwen3.8-flash"}]}}}
EOF
cat >"$W/dsh-patch.yml" <<EOF
- id: llm-pi-ai
  config:
    providers:
      mock: {displayName: Mock, apiKeyEnv: MOCK_API_KEY, api: openai-completions, baseURL: "$U/v1",
             models: [{id: qwen3.8-flash, name: qwen3.8-flash}]}
- id: agent-default-model
  config: {provider: mock, model: qwen3.8-flash}
EOF
echo '{"memory":{"enableManagedAutoMemory":false,"enableManagedAutoDream":false}}' >"$W/qwen-settings.json"

# A tiny git repo for the harnesses to run in.
if [ ! -d "$W/proj/.git" ]; then
  mkdir -p "$W/proj" && printf 'def add(a, b):\n    return a + b\n' >"$W/proj/calc.py"
  git -C "$W/proj" init -q && git -C "$W/proj" add -A &&
    git -C "$W/proj" -c user.email=bench@local -c user.name=bench commit -qm init
fi

# ---------------------------------------------------------------- mock server
LOG_DIR="$W/logs/_" "$W/py/bin/python" "$B/mock_server.py" "$PORT" &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT
sleep 1

run() { # run <name> <cmd...>: fresh HOME + scrubbed env, logs moved to .work/logs/<name>
  local name="$1"
  shift
  rm -rf "$W/logs/_" "$W/logs/$name" "$W/homes/$name" "$W/runs/$name"
  mkdir -p "$W/logs/_" "$W/homes/$name" "$W/runs"
  cp -r "$W/proj" "$W/runs/$name"
  [ -d "$C/$name" ] && cp -r "$C/$name/." "$W/homes/$name/"
  local status=0
  (cd "$W/runs/$name" && env -i PATH="$PATH" TERM=dumb LANG=C.UTF-8 \
    HOME="$W/homes/$name" XDG_CONFIG_HOME="$W/homes/$name/.config" \
    XDG_DATA_HOME="$W/homes/$name/.local/share" XDG_CACHE_HOME="$W/homes/$name/.cache" \
    NO_PROXY="127.0.0.1,localhost" no_proxy="127.0.0.1,localhost" \
    HTTPS_PROXY="${HTTPS_PROXY:-}" HTTP_PROXY="${HTTP_PROXY:-}" \
    NODE_EXTRA_CA_CERTS="${NODE_EXTRA_CA_CERTS:-}" SSL_CERT_FILE="${SSL_CERT_FILE:-}" \
    timeout 180 "$@" </dev/null >"$W/logs/$name.out" 2>&1) || status=$?
  sleep 1
  mv "$W/logs/_" "$W/logs/$name"
  printf '%-16s exit=%s\n' "$name" "$status"
}

run_logged_in() { # like run, but keeps the real HOME so the harness's own login is used
  local name="$1"
  shift
  rm -rf "$W/logs/_" "$W/logs/$name" "$W/runs/$name"
  mkdir -p "$W/logs/_" "$W/runs"
  cp -r "$W/proj" "$W/runs/$name"
  local status=0
  (cd "$W/runs/$name" && env -i PATH="$PATH" TERM=dumb LANG=C.UTF-8 HOME="$HOME" \
    ${XDG_CONFIG_HOME:+XDG_CONFIG_HOME="$XDG_CONFIG_HOME"} \
    ${QODER_PERSONAL_ACCESS_TOKEN:+QODER_PERSONAL_ACCESS_TOKEN="$QODER_PERSONAL_ACCESS_TOKEN"} \
    NO_PROXY="127.0.0.1,localhost" no_proxy="127.0.0.1,localhost" \
    HTTPS_PROXY="${HTTPS_PROXY:-}" HTTP_PROXY="${HTTP_PROXY:-}" \
    NODE_EXTRA_CA_CERTS="${NODE_EXTRA_CA_CERTS:-}" SSL_CERT_FILE="${SSL_CERT_FILE:-}" \
    timeout 180 "$@" </dev/null >"$W/logs/$name.out" 2>&1) || status=$?
  sleep 1
  mv "$W/logs/_" "$W/logs/$name"
  printf '%-16s exit=%s\n' "$name" "$status"
  [ "$status" = 0 ] || tail -5 "$W/logs/$name.out" | sed 's/^/    /'
}

qoder_settings() { # qoder_settings <cacheControl true|false>: custom provider -> mock
  cat >"$W/qoder-settings-$1.json" <<EOF
{"providers":{"benchmock":{"type":"openai-compatible","protocol":"openai","authType":"bearer",
  "baseUrl":"$U/v1","apiKey":"x","displayName":"Bench Mock","model":"qwen3.8-flash",
  "models":[{"model":"qwen3.8-flash","contextWindow":128000,
             "capabilities":{"tools":true,"cacheControl":$1}}]}}}
EOF
  echo "$W/qoder-settings-$1.json"
}

if printf '%s\n' "$@" | grep -qx 'qoder\|qoder_cache' && [ ! -x "$P/qodercli" ]; then
  (cd "$W" && npm install --no-fund --no-audit @qoder-ai/qodercli)
fi

ALL=(qwen_code qwen_code_tuned claude_code opencode codex crush pi pi_cache dsh aider mini)
for h in "${@:-${ALL[@]}}"; do
  case "$h" in
  qwen_code) run "$h" env OPENAI_API_KEY=x OPENAI_BASE_URL="$U/v1" OPENAI_MODEL=qwen3.8-flash \
    "$P/qwen" --approval-mode auto-edit --model qwen3.8-flash -p "$Q" ;;
  qwen_code_tuned) run "$h" env OPENAI_API_KEY=x OPENAI_BASE_URL="$U/v1" \
    QWEN_CODE_SYSTEM_SETTINGS_PATH="$W/qwen-settings.json" "$P/qwen" --auth-type openai \
    --approval-mode auto-edit --allowed-tools run_shell_command --exclude-tools skill notebook_edit \
    -o json --model qwen3.8-flash -p "$Q" ;;
  claude_code) run "$h" env ANTHROPIC_BASE_URL="$U" ANTHROPIC_AUTH_TOKEN=x \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 claude --model qwen3.8-flash \
    --permission-mode acceptEdits -p "$Q" ;;
  opencode) run "$h" env OPENCODE_DISABLE_MODELS_FETCH=1 OPENCODE_DISABLE_AUTOUPDATE=1 \
    "$P/opencode" run -m mock/qwen3.8-flash "$Q" ;;
  codex) run "$h" env MOCK_API_KEY=x CODEX_HOME="$W/homes/codex/.codex" \
    "$P/codex" exec --skip-git-repo-check "$Q" ;;
  crush) run "$h" env CRUSH_DISABLE_PROVIDER_AUTO_UPDATE=1 "$P/crush" run "$Q" ;;
  pi | pi_cache) run "$h" "$P/pi" --provider mock --model qwen3.8-flash -p "$Q" ;;
  dsh) run "$h" env MOCK_API_KEY=x DSH_HOME="$W/homes/dsh/.dsh" \
    "$P/dsh" --profile headless --patch "$W/dsh-patch.yml" "$Q" ;;
  aider) run "$h" "$W/py312/bin/aider" --model openai/qwen3.8-flash --openai-api-base "$U/v1" \
    --openai-api-key x --yes-always --no-check-update --no-show-model-warnings \
    --analytics-disable --no-auto-commits --message "$Q" ;;
  mini) run "$h" env OPENAI_API_KEY=x OPENAI_API_BASE="$U/v1" MSWEA_COST_TRACKING=ignore_errors \
    MSWEA_CONFIGURED=true "$W/py/bin/mini" -m openai/qwen3.8-flash -y --exit-immediately \
    --agent-class default -t "$Q" ;;
  qoder | qoder_cache)
    [ "$h" = qoder_cache ] && cc=true || cc=false
    run_logged_in "$h" "$P/qodercli" --settings "$(qoder_settings $cc)" -m qwen3.8-flash \
      --permission-mode accept_edits --no-session-persistence -p "$Q" ;;
  *) echo "unknown harness: $h" >&2 ;;
  esac
done

echo
"$W/py/bin/python" "$B/count.py" $(for h in "${@:-${ALL[@]}}"; do echo "$W/logs/$h"; done) 2>/dev/null
