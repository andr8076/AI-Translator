#!/usr/bin/env python3
"""Subtitle translation through a persistent local Colibrì server."""
import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_COLI = ROOT / ".runtime" / "colibri" / "c" / "coli"
DEFAULT_MODEL = ROOT / ".models" / "olmoe_i8"
DEFAULT_MODEL_ID = "olmoe-colibri"
DEFAULT_KEY = "ai-translator-local"

TIME_RE = re.compile(r"^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+")
MARKER_RE = re.compile(
    r"@@\s*(\d{1,3})\s*@@\s*\n(.*?)(?=\n@@\s*\d{1,3}\s*@@|\Z)", re.S
)
DANISH_WORDS = re.compile(
    r"\b(?:og|jeg|du|det|der|ikke|på|han|hun|hvad|hvor|hvordan|skal|har|så|nej)\b",
    re.I,
)


def looks_danish(text):
    letters = re.findall(r"[A-Za-zÆØÅæøå]", text)
    if len(letters) < 20:
        return False
    lower = text.lower()
    words = len(re.findall(r"[A-Za-zÆØÅæøå]+", lower))
    hits = len(DANISH_WORDS.findall(lower))
    special = sum(lower.count(c) for c in ("æ", "ø", "å"))
    return hits >= 3 and (special >= 1 or hits / max(words, 1) >= 0.06)


def subtitle_text(cues):
    return "\n".join(" ".join(c["body"]) for c in cues)


def parse_srt(raw):
    newline = "\r\n" if "\r\n" in raw else "\n"
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n{2,}", text.strip("\n"))
    cues = []
    for block in blocks:
        lines = block.split("\n")
        if len(lines) >= 3 and TIME_RE.match(lines[1]):
            cues.append({"head": lines[:2], "body": lines[2:]})
    return cues, newline


def render_srt(cues, newline):
    blocks = ["\n".join(c["head"] + c["body"]) for c in cues]
    return (("\n\n".join(blocks) + "\n") if blocks else "").replace("\n", newline)


def write_state(path, state):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_zip(path, infos, payloads):
    tmp = Path(str(path) + ".tmp")
    with zipfile.ZipFile(tmp, "w", allowZip64=True) as archive:
        for info, payload in zip(infos, payloads):
            archive.writestr(info, payload)
    os.replace(tmp, path)


def start_sleep_inhibitor():
    command = shutil.which("systemd-inhibit")
    if not command:
        print("[POWER] systemd-inhibit unavailable", flush=True)
        return None
    try:
        proc = subprocess.Popen(
            [
                command,
                "--what=idle:sleep",
                "--mode=block",
                "--who=AI Translator (Colibri)",
                "--why=Subtitle translation in progress",
                "sleep",
                "infinity",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.1)
        if proc.poll() is not None:
            return None
        print("[POWER] Sleep prevention enabled", flush=True)
        return proc
    except OSError:
        return None


def stop_sleep_inhibitor(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


def free_port(preferred):
    for port in (preferred, 0):
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", port))
            value = sock.getsockname()[1]
            sock.close()
            return value
        except OSError:
            sock.close()
    raise RuntimeError("Could not allocate a local port")


def http_json(url, payload=None, key=None, timeout=60):
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("HTTP %d from Colibri: %s" % (exc.code, body[-1200:])) from exc


class ColibriServer:
    def __init__(
        self, coli_bin, model, model_id, key, port, context, ram_gb, cap, gpu,
        log_path, startup_timeout
    ):
        self.coli_bin = Path(coli_bin)
        self.model = Path(model)
        self.model_id = model_id
        self.key = key
        self.port = free_port(port)
        self.context = context
        self.ram_gb = ram_gb
        self.cap = cap
        self.gpu = gpu
        self.log_path = Path(log_path)
        self.startup_timeout = startup_timeout
        self.proc = None
        self.log = None
        self.api = "http://127.0.0.1:%d/v1" % self.port

    def start(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = open(self.log_path, "a", encoding="utf-8")
        cmd = [
            str(self.coli_bin), "serve",
            "--model", str(self.model),
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "--model-id", self.model_id,
            "--api-key", self.key,
            "--ctx", str(self.context),
            "--gpu", self.gpu,
            "--policy", "quality",
            "--temp", "0.05",
        ]
        if self.ram_gb > 0:
            cmd += ["--ram", str(self.ram_gb)]
        if self.cap > 0:
            cmd += ["--cap", str(self.cap)]
        print("[COLIBRI] Starting persistent server...", flush=True)
        print("[COLIBRI] Model:", self.model, flush=True)
        self.proc = subprocess.Popen(
            cmd,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.time() + self.startup_timeout
        last_error = ""
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("Colibri exited during startup:\n" + self._log_tail())
            try:
                models = http_json(self.api + "/models", key=self.key, timeout=3)
                if models.get("data"):
                    print("[COLIBRI] Server ready on port", self.port, flush=True)
                    return
            except Exception as exc:
                last_error = str(exc)
            time.sleep(1)
        self.stop()
        raise TimeoutError("Colibri server did not become ready: " + last_error)

    def _log_tail(self):
        try:
            return self.log_path.read_text(errors="replace")[-5000:]
        except OSError:
            return "(no Colibri log available)"

    def chat(self, prompt, max_tokens=1024):
        payload = {
            "model": self.model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.05,
            "top_p": 0.9,
            "max_tokens": max_tokens,
            "stream": False,
        }
        result = http_json(
            self.api + "/chat/completions",
            payload=payload,
            key=self.key,
            timeout=900,
        )
        try:
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Unexpected Colibri response: %r" % result) from exc
        return (content or "").strip()

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=8)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if self.log is not None and not self.log.closed:
            self.log.close()


class SubtitleTranslator:
    def __init__(self, server):
        self.server = server

    @staticmethod
    def prompt(cues, source, target, before=(), after=()):
        target_text = "\n".join(
            "@@%d@@\n%s" % (i, "\n".join(cue["body"]))
            for i, cue in enumerate(cues)
        )
        before_text = "\n".join("\n".join(c["body"]) for c in before)
        after_text = "\n".join("\n".join(c["body"]) for c in after)
        return (
            "You are a professional subtitle translator.\n"
            "Translate the marked dialogue from %s to natural, idiomatic %s.\n"
            "Output ONLY the translated marked cues. Do not answer the dialogue, "
            "explain anything, or add commentary.\n"
            "Keep every @@number@@ marker exactly unchanged and in the same order.\n"
            "Preserve names, meaning, tone, HTML tags, bracketed sound/music cues, "
            "punctuation, and line breaks where practical.\n"
            "Use REFERENCE BEFORE/AFTER only for context; never output them.\n\n"
            "REFERENCE BEFORE:\n%s\nEND REFERENCE BEFORE\n\n"
            "TRANSLATE THESE CUES:\n%s\nEND CUES\n\n"
            "REFERENCE AFTER:\n%s\nEND REFERENCE AFTER\n"
        ) % (source, target, before_text, target_text, after_text)

    def translate_chunk(self, cues, source, target, before=(), after=()):
        prompt = self.prompt(cues, source, target, before, after)
        max_tokens = min(1800, max(256, len(prompt) // 2))
        output = self.server.chat(prompt, max_tokens=max_tokens)
        matches = MARKER_RE.findall(output)
        translated = {int(number): body.strip("\n ") for number, body in matches}
        expected = set(range(len(cues)))
        if set(translated) != expected or any(not translated[i].strip() for i in expected):
            missing = sorted(expected - set(translated))[:12]
            raise RuntimeError(
                "Malformed structured translation: returned %d/%d cues; missing %s"
                % (len(translated), len(expected), missing)
            )
        return [
            dict(cue, body=translated[i].split("\n"))
            for i, cue in enumerate(cues)
        ]

    def translate_resilient(self, cues, source, target, before=(), after=()):
        try:
            return self.translate_chunk(cues, source, target, before, after)
        except RuntimeError:
            if len(cues) <= 1:
                raise
            mid = max(1, len(cues) // 2)
            left = self.translate_resilient(
                cues[:mid], source, target,
                list(before)[-8:],
                list(cues[mid:])[:8] + list(after)[:8],
            )
            right = self.translate_resilient(
                cues[mid:], source, target,
                list(before)[-8:] + left[-8:],
                list(after)[:8],
            )
            return left + right


def translate_srt(raw, start, limit, translator, source, target, state,
                  label, block_cues, quiet=False):
    cues, newline = parse_srt(raw.decode("utf-8-sig"))
    state["cue_total"] = len(cues)
    if not quiet:
        print("[FILE]", label, "-", len(cues), "cues", flush=True)
    if start == 0 and looks_danish(subtitle_text(cues)):
        state["next_cue"] = len(cues)
        state["skipped_danish"] = True
        print("[SKIP] Already Danish:", label, flush=True)
        return raw, 0
    state.pop("skipped_danish", None)
    max_end = len(cues)
    if limit:
        max_end = min(max_end, start + limit)
    pos = start
    done = 0
    started = time.time()
    while pos < max_end:
        end = min(max_end, pos + block_cues)
        block = cues[pos:end]
        before = cues[max(0, pos - 8):pos]
        after = cues[end:min(len(cues), end + 8)]
        translated = translator.translate_resilient(
            block, source, target, before, after
        )
        cues[pos:end] = translated
        done += end - pos
        pos = end
        state["next_cue"] = pos
        if not quiet:
            elapsed = max(time.time() - started, 0.001)
            print(
                "  [BLOCK] %s cues %d-%d / %d | %.2f cues/s"
                % (label, pos - len(translated) + 1, pos, len(cues), done / elapsed),
                flush=True,
            )
    return render_srt(cues, newline).encode("utf-8"), done


def process_srt_file(src, out, args, translator, label=None):
    state_path = Path(str(out) + ".progress.json")
    state = {"source": str(src), "next_cue": 0, "translated": 0}
    raw = src.read_bytes()
    if not raw:
        raise ValueError("Input subtitle file is empty")
    if out.exists() and state_path.exists() and not args.fresh:
        try:
            old = json.loads(state_path.read_text(encoding="utf-8"))
            if old.get("source") == str(src):
                raw = out.read_bytes()
                state.update(old)
                print("[RESUME]", label or src, "from cue", state["next_cue"] + 1, flush=True)
        except Exception as exc:
            print("[WARN] Ignoring invalid resume data:", exc, flush=True)
    translated, count = translate_srt(
        raw, state["next_cue"], args.limit, translator,
        args.source_language, args.target_language, state,
        label or str(src), args.block_cues, args.quiet,
    )
    state["translated"] = state.get("translated", 0) + count
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(out) + ".tmp")
    tmp.write_bytes(translated)
    os.replace(tmp, out)
    write_state(state_path, state)
    print("[DONE]", label or src, "| added", count, "cues |", out, flush=True)
    return state


def process_folder(src, out_root, args, translator):
    out_root = out_root.resolve()
    files = [
        p for p in sorted(src.rglob("*.srt"))
        if p.is_file() and not (out_root == p or out_root in p.parents)
    ]
    if not files:
        raise FileNotFoundError("No .srt files found")
    print("[FOLDER]", len(files), "subtitle files", flush=True)
    errors = 0
    for number, file_path in enumerate(files, 1):
        relative = file_path.relative_to(src)
        target = (out_root / relative).with_name(
            relative.stem + args.suffix + relative.suffix
        )
        label = "[%d/%d] %s" % (number, len(files), relative)
        try:
            process_srt_file(file_path, target, args, translator, label)
        except Exception as exc:
            errors += 1
            print("[ERROR]", label, "-", exc, flush=True)
    print("[SUMMARY]", len(files) - errors, "completed |", errors, "errors", flush=True)


def process_zip(src, out, args, translator):
    state_path = Path(str(out) + ".progress.json")
    with zipfile.ZipFile(src) as archive:
        infos = archive.infolist()
        payloads = [archive.read(info) for info in infos]
    state = {
        "source": str(src), "entry_count": len(infos),
        "entry": 0, "next_cue": 0, "translated": 0,
    }
    if out.exists() and state_path.exists() and not args.fresh:
        try:
            old = json.loads(state_path.read_text(encoding="utf-8"))
            if old.get("source") == str(src) and old.get("entry_count") == len(infos):
                with zipfile.ZipFile(out) as archive:
                    saved_infos = archive.infolist()
                    if len(saved_infos) == len(infos):
                        payloads = [archive.read(info) for info in saved_infos]
                        state.update(old)
                        print("[RESUME] ZIP entry", state["entry"] + 1, flush=True)
        except Exception as exc:
            print("[WARN] Ignoring invalid ZIP resume data:", exc, flush=True)
    for index in range(state["entry"], len(infos)):
        info = infos[index]
        if not info.filename.lower().endswith(".srt"):
            state["entry"], state["next_cue"] = index + 1, 0
            continue
        payloads[index], count = translate_srt(
            payloads[index], state["next_cue"], args.limit, translator,
            args.source_language, args.target_language, state,
            info.filename, args.block_cues, args.quiet,
        )
        state["translated"] += count
        complete = state["next_cue"] >= state["cue_total"]
        state["entry"] = index + 1 if complete else index
        if complete:
            state["next_cue"] = 0
        out.parent.mkdir(parents=True, exist_ok=True)
        write_zip(out, infos, payloads)
        write_state(state_path, state)
        print("[CHECKPOINT]", info.filename, "| added", count, "cues", flush=True)
        if args.limit and count >= args.limit:
            break
    print("[DONE] ZIP:", out, flush=True)


def runtime_ok(coli_bin, model):
    return (
        Path(coli_bin).is_file()
        and os.access(coli_bin, os.X_OK)
        and Path(model).is_dir()
        and (Path(model) / "config.json").is_file()
        and any(Path(model).glob("*.safetensors"))
    )


def maybe_setup(args):
    if runtime_ok(args.coli_bin, args.model):
        return
    setup = ROOT / "setup_colibri.sh"
    if not setup.is_file():
        return
    print("[SETUP] Colibri runtime/model incomplete; running setup check...", flush=True)
    subprocess.run(
        [
            "bash", str(setup), "--check",
            "--runtime-dir", str(ROOT / ".runtime" / "colibri"),
            "--model-dir", str(args.model),
        ],
        check=False,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Translate SRT files, folders, or ZIPs with Colibrì."
    )
    parser.add_argument("input")
    parser.add_argument("-o", "--output")
    parser.add_argument("--model", default=str(DEFAULT_MODEL))
    parser.add_argument("--coli-bin", default=str(DEFAULT_COLI))
    parser.add_argument("--source-language", default="English")
    parser.add_argument("--target-language", default="Danish")
    parser.add_argument("--block-cues", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--suffix", default="_colibri_da")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--allow-sleep", action="store_true")
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--context-size", type=int, default=4096)
    parser.add_argument("--ram-gb", type=float, default=0.0)
    parser.add_argument("--cap", type=int, default=4)
    parser.add_argument("--gpu", default="none")
    parser.add_argument("--startup-timeout", type=int, default=300)
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    args = parser.parse_args()

    if args.block_cues < 1:
        parser.error("--block-cues must be at least 1")
    if args.cap < 0:
        parser.error("--cap cannot be negative")
    if args.suffix and not args.suffix.startswith(("_", "-", ".")):
        args.suffix = "_" + args.suffix

    args.coli_bin = str(Path(args.coli_bin).expanduser().resolve())
    args.model = str(Path(args.model).expanduser().resolve())
    maybe_setup(args)
    if not Path(args.coli_bin).is_file():
        parser.error(
            "Colibri runtime is missing. Run: ./setup_colibri.sh --runtime"
        )
    if not (
        Path(args.model).is_dir()
        and (Path(args.model) / "config.json").is_file()
        and any(Path(args.model).glob("*.safetensors"))
    ):
        parser.error(
            "Colibri model is missing. Run: ./setup_colibri.sh --install-model"
        )

    src = Path(args.input).expanduser().resolve()
    if not src.exists():
        parser.error("Input does not exist: " + str(src))
    if src.is_dir():
        out = (
            Path(args.output).expanduser().resolve()
            if args.output else src.with_name(src.name + args.suffix)
        )
    elif args.output:
        out = Path(args.output).expanduser().resolve()
    elif src.suffix.lower() == ".zip":
        out = src.with_name(src.stem + args.suffix + src.suffix)
    else:
        out = src.with_name(src.stem + args.suffix + src.suffix)

    sleep_inhibitor = None
    if not args.allow_sleep:
        sleep_inhibitor = start_sleep_inhibitor()
    key = os.environ.get("COLIBRI_TRANSLATOR_API_KEY", DEFAULT_KEY)
    server = ColibriServer(
        args.coli_bin, args.model, args.model_id, key, args.port,
        args.context_size, args.ram_gb, args.cap, args.gpu,
        ROOT / ".runtime" / "colibri-translator.log",
        args.startup_timeout,
    )
    try:
        server.start()
        translator = SubtitleTranslator(server)
        if src.is_dir():
            process_folder(src, out, args, translator)
        elif src.suffix.lower() == ".zip":
            process_zip(src, out, args, translator)
        else:
            process_srt_file(src, out, args, translator)
    finally:
        server.stop()
        stop_sleep_inhibitor(sleep_inhibitor)


if __name__ == "__main__":
    main()
