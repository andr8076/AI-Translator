#!/usr/bin/env bash
set -euo pipefail

DATA_FOLDER="${JAN_DATA_FOLDER:-/mnt/SmollSSD/Natural Stupidity}"
MODE="check"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --auto) MODE="install"; shift ;;
        --install) MODE="install"; shift ;;
        --data-folder) DATA_FOLDER="${2:?missing value for --data-folder}"; shift 2 ;;
        --check) MODE="check"; shift ;;
        -h|--help)
            printf 'Usage: bash setup.sh [--check|--auto|--install] [--data-folder PATH]\n'
            exit 0 ;;
        *) printf '[SETUP] Unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done

missing=()
for command in python3 curl unzip; do
    command -v "$command" >/dev/null 2>&1 || missing+=("$command")
done

if [[ ${#missing[@]} -gt 0 && "$MODE" == "install" ]]; then
    if command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --needed --noconfirm python curl unzip vulkan-tools
    elif command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update
        sudo apt-get install -y python3 curl unzip vulkan-tools
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y python3 curl unzip vulkan-tools
    else
        printf '[SETUP] No supported package manager found. Install: %s\n' "${missing[*]}" >&2
        exit 2
    fi
fi

missing=()
for command in python3 curl unzip; do
    command -v "$command" >/dev/null 2>&1 || missing+=("$command")
done
if [[ ${#missing[@]} -gt 0 ]]; then
    printf '[SETUP] Missing required commands: %s\n' "${missing[*]}" >&2
    exit 2
fi

if [[ ! -d "$DATA_FOLDER/llamacpp/models" ]]; then
    printf '[SETUP] Jan model directory not found: %s\n' "$DATA_FOLDER/llamacpp/models" >&2
    printf "[SETUP] Set JAN_DATA_FOLDER to Jan's data directory.\n" >&2
    exit 2
fi

backend_count=$(find "$DATA_FOLDER/llamacpp/backends" -type f -name llama-server 2>/dev/null | wc -l)
if [[ "$backend_count" -eq 0 ]]; then
    printf '[SETUP] No Jan llama-server backend found in %s\n' "$DATA_FOLDER/llamacpp/backends" >&2
    printf '[SETUP] Open Jan once and install its llama.cpp backend.\n' >&2
    exit 2
fi

printf '[SETUP] Runtime checks passed. Found %s Jan backend(s).\n' "$backend_count"
