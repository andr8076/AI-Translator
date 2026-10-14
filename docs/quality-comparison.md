# Colibrì validation — block-size and quality comparison

Date: 2026-10-14  
Host: Farm4, Linux Mint, Xeon E3-1505L v5 (4 cores/8 threads), approximately 8 GB RAM  
Source: `Little House On the Prairie - S09E10 (Love).srt` (698 cues)  
Reference: matching `_Da.srt`  
Model: TranslateGemma 4B IT, Q6_K GGUF, CPU-only, llama.cpp build 11429, context 2048, 4 generation threads

## Controlled comparison

The first 32 source cues were translated with identical model/runtime settings. Only the request block size changed.

| Run | Block size | Cues | Wall time | Structural result |
| --- | ---: | ---: | ---: | --- |
| Confirmed baseline | 16 | 16 | 79.45 s | valid SRT |
| Controlled comparison | 16 | 32 | 148.13 s | valid SRT |
| Existing comparison | 8 | 32 | 164.81 s | valid SRT |
| Existing comparison | 32 | 32 | not recorded | valid SRT |

The 16-cue configuration remains the recommended default. A 32-cue block did not provide a measured, reliable quality or speed benefit and was not adopted.

## Memory/cache finding

The original backend always enabled llama.cpp prompt KV caching. On the 8 GB Farm4 profile, a full pilot with that setting reached cue 352/698, filled the 2 GiB swap, and was stopped cleanly to avoid an uncontrolled thrash. The stable path now leaves caching off by default and exposes `--cache-prompt` as an explicit opt-in for hosts with enough RAM. Long runs also restart llama.cpp every eight 16-cue blocks (128 cues) by default, releasing accumulated allocator/KV memory without changing the prompt or model.

A cache-off smoke run translated the same 32-cue/16-block workload in 149.88 s. A forced-restart variant took 150.72 s and produced a byte-identical SRT, so the restart overhead was about 0.6% in that check. During the final pilot, the preserved checkpoint was resumed from cue 481; cues 481-698 completed in 1044.62 s (218 cues, 4.79 s/cue), with restarts at cues 512 and 640. The final output reached checkpoint `next_cue=698`, and host memory returned to about 6.8 GiB available after shutdown.

## Quality observations

All compared files retained 32 cues, original numbering, timestamps, and cue order. No extra or missing dialogue cues were introduced.

Representative differences:

- The 32-cue run corrected the age question in cue 3 (`hvor gammel`) but changed cue 1 from a “what” question into a “who” question.
- The 16-cue run preserved cue 1's “what” meaning, but the Danish was wordier than the reference and cue 3 sometimes used the comparative `ældre`.
- Both larger-block runs retained awkward or literal phrasing such as `Spørg ikke spørgsmål`, `undskyld ikke`, and `Nøddeplantage` for the named place Walnut Grove.
- The 8-cue run had a malformed cue 9 and a less natural description of the blind school.
- Names such as Jane, Laura, and Almanzo were retained in the compared outputs, but proper-name/place-name consistency still needs review.

The Danish reference is a quality guide, not a required wording match. The remaining errors are meaning/naturalness issues rather than SRT corruption.

## Engineering changes retained

- Strict SRT parsing now rejects empty or malformed cue blocks.
- Final output is checked for unchanged numbering and timestamps.
- Duplicate structured-response markers are rejected; a single-cue response can still use the existing plain-text fallback.
- Colibrì hot-pin patch application is idempotent and setup preserves an already modified local runtime.
- A dependency-free regression suite covers parser/rendering, layout preservation, marker contracts, malformed input, Danish detection, checkpoint/resume, explicit prompt-cache behavior, and restart scheduling.
- TranslateGemma prompt caching is explicit (`--cache-prompt`); the default low-RAM path restarts the server every eight blocks and preserves the checkpoint before each restart.

Two prompt variants were tested and discarded. A verbose output-contract prompt caused max-token responses; a shorter semantic-hints variant caused recursive structured-response retries. Neither is part of the final code.

## Quality gate

The implementation is structurally reliable and the 16-cue CPU baseline is reproducible. The quality gate for unattended full-season translation is **not passed**: the Q6 model still needs human review for pronouns, idioms, and proper names. The final 698-cue pilot output is complete and structurally valid, but it is not evidence that those semantic issues are solved.
