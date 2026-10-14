# AI-Translator — Colibrì

Colibrì is the CPU-focused local subtitle translator in this repository. It supports the existing Colibrì/OLMoE backend and the recommended local TranslateGemma Q6 backend.

## Prerequisites

- Linux x86_64 (the setup script selects the available CPU runtime).
- About 8 GB RAM is workable for the Q6 CPU model; keep swap enabled.
- Python 3, Git, GCC, and Make.
- Enough disk space for the runtime and model. TranslateGemma weights are subject to Google's Gemma terms; review and accept the model terms before downloading.

## Install and check

For the recommended TranslateGemma backend:

```bash
./setup_translategemma.sh --install-model
./setup_translategemma.sh --check
```

To prepare the original Colibrì/OLMoE backend instead:

```bash
./setup_colibri.sh --install-model
./setup_colibri.sh --check
```

The setup scripts keep runtime/model files under `.runtime/` and `.models/`; those directories are intentionally ignored by Git.

## Recommended translation command

The validated CPU baseline is TranslateGemma Q6 with 16-cue requests:

```bash
python3 colibri_subtitle_translate.py \
  "/path/to/input.srt" \
  --backend translategemma \
  --threads 4 \
  --context-size 2048 \
  --block-cues 16
```

The default TranslateGemma settings already select Q6, CPU, a 2048-token context, and 16-cue blocks. Use `--device auto` only when a supported llama.cpp accelerator has been independently verified; CPU is the reproducible Farm4 setting. Prompt KV caching is disabled by default on the 8 GB profile because a long cache-enabled run grew into swap pressure; use `--cache-prompt` only when extra RAM is available and the trade-off has been benchmarked. Long runs restart the inference server every 8 blocks (128 cues) by default to release accumulated allocator/KV memory; use `--restart-every 0` only when you have independently verified enough RAM.

Translate a directory (the output is a sibling directory) or a ZIP:

```bash
python3 colibri_subtitle_translate.py "/path/to/season" --backend translategemma
python3 colibri_subtitle_translate.py "/path/to/subtitles.zip" --backend translategemma
```

Use `--output` for an explicit destination. The default suffix is `_translategemma_da`; use `--suffix` to change it. Pass `--backend colibri` to use the original OLMoE engine and its `_colibri_da` default suffix.

## Checkpointing and resume

Each SRT output has a sibling `.progress.json`. A checkpoint is written after every completed block, including the translated partial output. Re-running the same command resumes from the saved cue; `--fresh` deliberately starts over. By default, generated output is written beside the source; choose a separate `--output` path when preserving an existing output is important.

The parser rejects empty or malformed SRT blocks. Final output is checked for unchanged cue numbering and timestamps, and structured model responses with duplicate/missing markers are rejected (single-cue responses have a plain-text fallback).

## Performance and quality

On Farm4 (Xeon E3-1505L v5, 4 cores/8 threads, CPU-only), the measured Q6 baseline was:

| Configuration | Result |
| --- | ---: |
| 16-cue blocks, 16 cues | 79.45 s |
| 16-cue blocks, 32 cues | 148.13 s |
| 8-cue blocks, 8 cues | about 44 s |
| Resumed final pilot, cues 481-698, 16-cue blocks | 1044.62 s |

The 32-cue comparison produced valid SRT but no meaningful quality advantage; it changed question interpretation in one cue and retained awkward wording elsewhere. Keep 16-cue blocks unless a new model or prompt is benchmarked with the same quality checks. The completed 698-cue pilot passed the strict SRT/layout gate; the resumed segment took 1044.62 s with cache off and the default eight-block server restart cadence.

The current Q6 model can still make occasional Danish pronoun, idiom, and proper-name mistakes. The pilot output therefore requires human review before publishing or using it as a full-season batch. Valid SRT structure is not a substitute for translation-quality review.

## Tests

Run the dependency-free regression suite:

```bash
python3 -m unittest discover -s tests -v
```

The tests cover SRT parsing/rendering, timestamp preservation, malformed input, marker contracts, duplicate-marker rejection, Danish detection, and checkpoint/resume behavior.
