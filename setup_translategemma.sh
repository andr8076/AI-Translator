#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="$ROOT/.runtime/llama"
MODEL_DIR="$ROOT/.models/translategemma4b_q6"
MODEL_REPO="mradermacher/translategemma-4b-it-GGUF"
MODEL_FILE="translategemma-4b-it.Q6_K.gguf"
LLAMA_VERSION="b11429"
MODE="check"

usage() {
    cat <<EOF
Usage: ./setup_translategemma.sh [--check|--runtime|--install-model]
                                  [--runtime-dir PATH] [--model-dir PATH]

  --check          Verify the local llama.cpp runtime and TranslateGemma model.
  --runtime        Install the pinned, CPU-optimized llama.cpp runtime locally.
  --install-model  Install the runtime and download the Q6_K TranslateGemma model.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check) MODE="check"; shift ;;
        --runtime) MODE="runtime"; shift ;;
        --install-model|--install) MODE="model"; shift ;;
        --runtime-dir) RUNTIME_DIR="${2:?missing value for --runtime-dir}"; shift 2 ;;
        --model-dir) MODEL_DIR="${2:?missing value for --model-dir}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "[SETUP] Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

LLAMA_BIN="$RUNTIME_DIR/bin/llama"
MODEL_PATH="$MODEL_DIR/$MODEL_FILE"
BASE="https://huggingface.co/buckets/ggml-org/install.sh/resolve/$LLAMA_VERSION/x86_64/linux"

runtime_ok() {
    [[ -x "$LLAMA_BIN" ]] || return 1
    local version
    version="$("$LLAMA_BIN" version 2>&1)" || return 1
    grep -q "build ${LLAMA_VERSION#b}" <<<"$version"
}

model_ok() {
    [[ -s "$MODEL_PATH" ]]
}

install_runtime() {
    for command in curl; do
        command -v "$command" >/dev/null 2>&1 || {
            echo "[SETUP] Missing required command: $command" >&2
            exit 2
        }
    done

    local temp_dir profile version
    mkdir -p "$RUNTIME_DIR/bin"
    temp_dir="$(mktemp -d "$RUNTIME_DIR/.setup.XXXXXX")"
    trap 'rm -rf "$temp_dir"' RETURN

    echo "[SETUP] Probing this CPU for an optimized llama.cpp build..."
    curl -fsSL --retry 2 "$BASE/featcode" -o "$temp_dir/featcode"
    chmod +x "$temp_dir/featcode"
    profile="$("$temp_dir/featcode")"
    if [[ -z "$profile" ]]; then
        echo "[SETUP] Could not detect a supported CPU profile." >&2
        exit 2
    fi

    echo "[SETUP] Downloading CPU profile: $profile"
    curl -fL --retry 3 "$BASE/cpu/$profile/llama-app.zst" -o "$temp_dir/llama.zst"
    if command -v zstd >/dev/null 2>&1; then
        zstd -d -q "$temp_dir/llama.zst" -o "$temp_dir/llama"
    else
        curl -fsSL --retry 2 "$BASE/unzstd" -o "$temp_dir/unzstd"
        chmod +x "$temp_dir/unzstd"
        "$temp_dir/unzstd" < "$temp_dir/llama.zst" > "$temp_dir/llama"
    fi
    chmod +x "$temp_dir/llama"

    version="$("$temp_dir/llama" version 2>&1)"
    if ! grep -q "build ${LLAMA_VERSION#b}" <<<"$version"; then
        echo "[SETUP] Runtime version check failed: $version" >&2
        exit 2
    fi
    mv -f "$temp_dir/llama" "$LLAMA_BIN"
    echo "[OK] Installed $version at $LLAMA_BIN"
}

install_model() {
    mkdir -p "$MODEL_DIR"
    local hf
    hf="$ROOT/.runtime/convert-venv/bin/hf"
    if [[ ! -x "$hf" ]]; then
        hf="$ROOT/.runtime/translategemma-download-venv/bin/hf"
        if [[ ! -x "$hf" ]]; then
            command -v python3 >/dev/null 2>&1 || {
                echo "[SETUP] python3 is required to prepare the local model downloader." >&2
                exit 2
            }
            python3 -m venv "$ROOT/.runtime/translategemma-download-venv"
            "$ROOT/.runtime/translategemma-download-venv/bin/python" -m pip install --upgrade huggingface_hub
        fi
    fi

    echo "[LICENSE] TranslateGemma weights follow Google's Gemma terms."
    echo "[LICENSE] Review and accept the terms on the official model page before use."
    echo "[SETUP] Downloading $MODEL_FILE (resumable)..."
    "$hf" download "$MODEL_REPO" "$MODEL_FILE" --local-dir "$MODEL_DIR"
    model_ok || {
        echo "[SETUP] Model download did not produce $MODEL_PATH" >&2
        exit 2
    }
    echo "[OK] Model downloaded to $MODEL_PATH"
}

if [[ "$MODE" == "runtime" || "$MODE" == "model" ]]; then
    if ! runtime_ok; then
        install_runtime
    else
        echo "[OK] llama.cpp runtime is already ready."
    fi
fi

if [[ "$MODE" == "model" ]] && ! model_ok; then
    install_model
fi

echo "[CHECK] Runtime: $LLAMA_BIN"
if runtime_ok; then
    echo "[OK] llama.cpp runtime is ready."
else
    echo "[MISSING] Runtime. Run: ./setup_translategemma.sh --runtime" >&2
    [[ "$MODE" == "check" ]] && exit 3
    exit 2
fi

echo "[CHECK] Model: $MODEL_PATH"
if model_ok; then
    echo "[OK] TranslateGemma Q6_K model is ready."
else
    echo "[MISSING] Model. Run: ./setup_translategemma.sh --install-model" >&2
    [[ "$MODE" == "check" ]] && exit 4
fi

echo "[SETUP] TranslateGemma prerequisites are ready."
