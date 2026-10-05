#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="$ROOT/.runtime/colibri"
MODEL_DIR="$ROOT/.models/olmoe_i8"
VENV_DIR="$ROOT/.runtime/convert-venv"
UPSTREAM="https://github.com/JustVugg/colibri.git"
MODEL_REPO="allenai/OLMoE-1B-7B-0125-Instruct"
MODE="check"

usage() {
    cat <<EOF
Usage: ./setup_colibri.sh [--check|--runtime|--install-model]
                          [--runtime-dir PATH] [--model-dir PATH]

  --check          Verify runtime and model without changing anything.
  --runtime        Clone/update Colibrì and build the OLMoE engine.
  --install-model  Do --runtime, create an isolated converter venv,
                   download/convert OLMoE, and verify the result.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check) MODE="check"; shift ;;
        --runtime|--auto) MODE="runtime"; shift ;;
        --install|--install-model) MODE="model"; shift ;;
        --runtime-dir) RUNTIME_DIR="${2:?missing value}"; shift 2 ;;
        --model-dir) MODEL_DIR="${2:?missing value}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "[SETUP] Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

need=()
for command in git gcc make python3; do
    command -v "$command" >/dev/null 2>&1 || need+=("$command")
done
if [[ ${#need[@]} -gt 0 ]]; then
    echo "[SETUP] Missing build tools: ${need[*]}" >&2
    echo "[SETUP] Install them with your OS package manager, then rerun." >&2
    exit 2
fi

runtime_ok() {
    [[ -x "$RUNTIME_DIR/c/coli" && -x "$RUNTIME_DIR/c/olmoe" ]]
}

model_ok() {
    [[ -f "$MODEL_DIR/config.json" ]] &&
    compgen -G "$MODEL_DIR/*.safetensors" >/dev/null
}

install_runtime() {
    mkdir -p "$(dirname "$RUNTIME_DIR")"
    if [[ -d "$RUNTIME_DIR/.git" ]]; then
        echo "[SETUP] Updating Colibrì runtime..."
        git -C "$RUNTIME_DIR" pull --ff-only
    else
        echo "[SETUP] Cloning Colibrì runtime..."
        rm -rf "$RUNTIME_DIR"
        git clone --depth 1 "$UPSTREAM" "$RUNTIME_DIR"
    fi
    echo "[SETUP] Building OLMoE engine..."
    make -C "$RUNTIME_DIR/c" olmoe
    chmod +x "$RUNTIME_DIR/c/coli" "$RUNTIME_DIR/c/olmoe"
}

free_gb() {
    df -Pk "$1" | awk 'NR==2 {printf "%.0f", $4/1024/1024}'
}

install_model() {
    mkdir -p "$MODEL_DIR" "$(dirname "$VENV_DIR")"
    local available
    available="$(free_gb "$(dirname "$MODEL_DIR")")"
    if [[ "$available" -lt 25 ]]; then
        echo "[SETUP] Need at least 25 GB free for safe download/conversion; ${available} GB available." >&2
        exit 2
    fi

    if [[ ! -x "$VENV_DIR/bin/python" ]]; then
        echo "[SETUP] Creating isolated conversion environment..."
        python3 -m venv "$VENV_DIR"
    fi

    echo "[SETUP] Installing conversion dependencies in the project venv..."
    "$VENV_DIR/bin/python" -m pip install --upgrade pip wheel
    "$VENV_DIR/bin/python" -m pip install --upgrade numpy safetensors huggingface_hub
    "$VENV_DIR/bin/python" -m pip install --upgrade torch \
        --index-url https://download.pytorch.org/whl/cpu

    echo "[SETUP] Downloading and converting OLMoE (resumable)..."
    "$VENV_DIR/bin/python" \
        "$RUNTIME_DIR/c/tools/convert_olmoe_merged.py" \
        --repo "$MODEL_REPO" \
        --out "$MODEL_DIR" \
        --flush-every 64 \
        --min-free-gb 10
}

if [[ "$MODE" == "runtime" || "$MODE" == "model" ]]; then
    install_runtime
fi

if [[ "$MODE" == "model" ]]; then
    if ! model_ok; then
        install_model
    fi
fi

echo "[CHECK] Runtime: $RUNTIME_DIR"
if runtime_ok; then
    echo "[OK] Colibrì launcher and OLMoE engine are built."
else
    echo "[MISSING] Colibrì runtime. Run: ./setup_colibri.sh --runtime" >&2
    [[ "$MODE" == "check" ]] && exit 3
    exit 2
fi

echo "[CHECK] Model: $MODEL_DIR"
if model_ok; then
    echo "[OK] OLMoE model container is present."
else
    echo "[MISSING] OLMoE model. Run: ./setup_colibri.sh --install-model" >&2
    [[ "$MODE" == "check" ]] && exit 4
    exit 2
fi

echo "[SETUP] Colibrì translator prerequisites are ready."
