#!/usr/bin/env python3
"""Colibrì-backed subtitle translator using a fixed Colibrì backend."""
import argparse
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from jan_subtitle_translate import looks_danish, parse_srt, render_srt, subtitle_text

MARKER_RE = re.compile(r"@@\s*(\d{1,3})\s*@@\s*\n(.*?)(?=\n@@\s*\d{1,3}\s*@@|\Z)", re.S)


class ColibriTranslator:
    def __init__(self, binary, model, timeout=900):
        self.binary = str(binary)
        self.model = str(model)
        self.timeout = timeout

    def translate_chunk(self, cues, source, target, before=(), after=()):
        target_text = "\n".join("@@%d@@\n%s" % (i, "\n".join(c["body"])) for i, c in enumerate(cues))
        before_text = "\n".join("\n".join(c["body"]) for c in before)
        after_text = "\n".join("\n".join(c["body"]) for c in after)
        prompt = (
            "Translate one continuous subtitle section from %s to natural, idiomatic %s. "
            "Use the reference dialogue for context.\n"
            "Keep every @@number@@ marker exactly unchanged and in order. Output only translated cues. "
            "Preserve tags, sound markers, names, tone, and line breaks.\n\n"
            "REFERENCE BEFORE - do not output:\n%s\n"
            "TRANSLATE THESE CUES:\n%s\n"
            "REFERENCE AFTER - do not output:\n%s\n"
        ) % (source, target, before_text, target_text, after_text)
        result = subprocess.run(
            [self.binary, "chat", "--model", self.model],
            input=prompt, text=True, encoding="utf-8", errors="replace",
            capture_output=True, timeout=self.timeout,
        )
        if result.returncode:
            raise RuntimeError("Colibrì failed (%d): %s" % (result.returncode, result.stderr[-1000:]))
        translated = {int(n): body.strip("\n ") for n, body in MARKER_RE.findall(result.stdout)}
        expected = set(range(len(cues)))
        if set(translated) != expected:
            raise RuntimeError("Colibrì returned %d/%d cues; missing %s" % (
                len(translated), len(expected), sorted(expected - set(translated))[:12]))
        if any(not translated[i].strip() for i in expected):
            raise RuntimeError("Colibrì returned an empty subtitle cue")
        return [dict(cue, body=translated[i].split("\n")) for i, cue in enumerate(cues)]

    def translate_resilient(self, cues, source, target, before=(), after=()):
        try:
            return self.translate_chunk(cues, source, target, before, after)
        except RuntimeError:
            if len(cues) <= 1:
                raise
            mid = max(1, len(cues) // 2)
            left = self.translate_resilient(cues[:mid], source, target, list(before)[-8:], list(cues[mid:])[:8] + list(after)[:8])
            right = self.translate_resilient(cues[mid:], source, target, list(before)[-8:] + left[-8:], list(after)[:8])
            return left + right


def translate_srt(raw, translator, source, target, block_cues, limit, label):
    cues, newline = parse_srt(raw.decode("utf-8-sig"))
    if looks_danish(subtitle_text(cues)):
        print("[SKIP] Already Danish:", label, flush=True)
        return raw, 0
    maximum = min(len(cues), limit) if limit else len(cues)
    output = list(cues)
    pos = 0
    while pos < maximum:
        end = min(maximum, pos + block_cues)
        output[pos:end] = translator.translate_resilient(
            cues[pos:end], source, target, cues[max(0, pos - 8):pos], cues[end:min(len(cues), end + 8)]
        )
        pos = end
        print("[BLOCK]", label, "cues", pos, "/", len(cues), flush=True)
    return render_srt(output, newline).encode("utf-8"), maximum


def process_srt(src, dst, translator, args):
    translated, count = translate_srt(src.read_bytes(), translator, args.source_language, args.target_language, args.block_cues, args.limit, str(src))
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(translated)
    print("[DONE]", dst, "| translated", count, "cues", flush=True)


def process_folder(src, dst, translator, args):
    files = sorted(src.rglob("*.srt"))
    if not files:
        raise FileNotFoundError("No .srt files found")
    for number, file_path in enumerate(files, 1):
        relative = file_path.relative_to(src)
        target = dst / relative
        target = target.with_name(target.stem + args.suffix + target.suffix)
        print("[FILE]", number, "/", len(files), relative, flush=True)
        try:
            process_srt(file_path, target, translator, args)
        except Exception as exc:
            print("[ERROR]", relative, "-", exc, flush=True)


def process_zip(src, dst, translator, args):
    with zipfile.ZipFile(src) as archive:
        infos = archive.infolist()
        payloads = [archive.read(info) for info in infos]
    for index, info in enumerate(infos):
        if info.filename.lower().endswith(".srt"):
            try:
                payloads[index], _ = translate_srt(payloads[index], translator, args.source_language, args.target_language, args.block_cues, args.limit, info.filename)
            except Exception as exc:
                print("[ERROR]", info.filename, "-", exc, flush=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dst, "w", allowZip64=True) as archive:
        for info, payload in zip(infos, payloads):
            archive.writestr(info, payload)
    print("[DONE]", dst, flush=True)


def main():
    parser = argparse.ArgumentParser(description="Translate SRT files, folders, or ZIP files through Colibrì.")
    parser.add_argument("input")
    parser.add_argument("-o", "--output")
    parser.add_argument("--model", required=True)
    parser.add_argument("--coli-bin", default="coli")
    parser.add_argument("--source-language", default="English")
    parser.add_argument("--target-language", default="Danish")
    parser.add_argument("--block-cues", type=int, default=16)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--suffix", default="_colibri_da")
    args = parser.parse_args()
    if not args.suffix.startswith(("_", "-", ".")):
        args.suffix = "_" + args.suffix
    if args.block_cues < 1:
        parser.error("--block-cues must be at least 1")
    if not shutil.which(args.coli_bin) and not Path(args.coli_bin).exists():
        parser.error("Colibrì executable not found: " + args.coli_bin)
    src = Path(args.input).expanduser().resolve()
    if not src.exists():
        parser.error("Input does not exist: " + str(src))
    translator = ColibriTranslator(args.coli_bin, args.model)
    if src.is_dir():
        dst = Path(args.output).expanduser().resolve() if args.output else src.with_name(src.name + args.suffix)
        process_folder(src, dst, translator, args)
    elif src.suffix.lower() == ".zip":
        process_zip(src, Path(args.output).expanduser().resolve() if args.output else src.with_name(src.stem + args.suffix + src.suffix), translator, args)
    else:
        process_srt(src, Path(args.output).expanduser().resolve() if args.output else src.with_name(src.stem + args.suffix + src.suffix), translator, args)


if __name__ == "__main__":
    main()
