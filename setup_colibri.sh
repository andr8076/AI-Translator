#!/usr/bin/env bash
set -euo pipefail
MODEL=""
COLI_BIN="coli"
while [[ \$# -gt 0 ]]; do
    case "\$1" in
        --model) MODEL="\${2:?missing value for --model}"; shift 2 ;;
        --coli-bin) COLI_BIN="\${2:?missing value for --coli-bin}"; shift 2 ;;
        -h|--help)
            printf 'Usage: bash setup_colibri.sh --model PATH [--coli-bin PATH]\n'
            exit 0 ;;
        *) printf '[SETUP] Unknown option: %s\n' "\$1" >&2; exit 2 ;;
    esac
done
if ! command -v "\$COLI_BIN" >/dev/null 2>&1 && [[ ! -x "\$COLI_BIN" ]]; then
    printf '[SETUP] Colibrì executable not found: %s\n' "\$COLI_BIN" >&2
    exit 2
fi
if [[ -z "\$MODEL" || ! -e "\$MODEL" ]]; then
    printf '[SETUP] Colibrì model path is missing or does not exist.\n' >&2
    exit 2
fi
printf '[SETUP] Colibrì executable and model are available.\n'
