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

from translategemma_backend import TranslateGemmaServer

ROOT = Path(__file__).resolve().parent
DEFAULT_COLI = ROOT / ".runtime" / "colibri" / "c" / "coli"
DEFAULT_MODEL = ROOT / ".models" / "olmoe_i8"
DEFAULT_LLAMA = ROOT / ".runtime" / "llama" / "bin" / "llama"
DEFAULT_TRANSLATEGEMMA_MODEL = (
    ROOT / ".models" / "translategemma4b_q6" / "translategemma-4b-it.Q6_K.gguf"
)
DEFAULT_MODEL_ID = "olmoe-colibri"
DEFAULT_KEY = "ai-translator-local"

TIME_RE = re.compile(
    r"^\s*\d{1,3}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+"
    r"\d{1,3}:\d{2}:\d{2}[,.]\d{3}(?:\s+.*)?$"
)
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


def load_glossary(path):
    """Read a small project glossary without allowing unbounded prompt growth."""
    if not path:
        return ()
    glossary_path = Path(path).expanduser().resolve()
    if not glossary_path.is_file():
        raise FileNotFoundError("Glossary does not exist: " + str(glossary_path))
    entries = []
    total_chars = 0
    for raw_line in glossary_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if len(line) > 240:
            raise ValueError("Glossary line is too long: " + line[:80])
        entries.append(line)
        total_chars += len(line)
    if len(entries) > 120 or total_chars > 8000:
        raise ValueError("Glossary is too large; use at most 120 lines / 8000 characters")
    return tuple(entries)


def subtitle_text(cues):
    return "\n".join(" ".join(c["body"]) for c in cues)


def parse_srt(raw):
    newline = "\r\n" if "\r\n" in raw else "\n"
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ValueError("Subtitle file is empty")
    blocks = re.split(r"\n{2,}", text.strip("\n"))
    cues = []
    for number, block in enumerate(blocks, 1):
        lines = block.split("\n")
        if len(lines) < 3:
            raise ValueError("Malformed SRT cue %d: expected number, timestamp, and text" % number)
        if not lines[0].strip().isdigit():
            raise ValueError("Malformed SRT cue %d: invalid subtitle number" % number)
        if not TIME_RE.match(lines[1]):
            raise ValueError("Malformed SRT cue %d: invalid timestamp" % number)
        if not any(line.strip() for line in lines[2:]):
            raise ValueError("Malformed SRT cue %d: empty subtitle text" % number)
        cues.append({"head": lines[:2], "body": lines[2:]})
    if not cues:
        raise ValueError("No subtitle cues found")
    return cues, newline


def render_srt(cues, newline):
    blocks = ["\n".join(c["head"] + c["body"]) for c in cues]
    return (("\n\n".join(blocks) + "\n") if blocks else "").replace("\n", newline)


def validate_srt_layout(expected, actual):
    if len(expected) != len(actual):
        raise RuntimeError(
            "SRT cue count changed: expected %d, got %d" %
            (len(expected), len(actual))
        )
    for index, (before, after) in enumerate(zip(expected, actual), 1):
        if before["head"] != after["head"]:
            raise RuntimeError(
                "SRT numbering/timestamp changed at cue %d" % index
            )


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
            "--auto-tier",
            "--policy", "quality",
            "--temp", "0.05",
        ]
        if self.gpu and self.gpu.lower() not in ("none", "cpu", "off"):
            cmd += ["--gpu", self.gpu]
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

    def chat(self, prompt, max_tokens=1024, system=None):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self.model_id,
            "messages": messages,
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
    def __init__(self, server, glossary=()):
        self.server = server
        self.glossary = tuple(glossary)
        self.exact_overrides = []
        for entry in self.glossary:
            if not entry.startswith("EXACT:"):
                continue
            mapping = entry[len("EXACT:"):].strip()
            if "=>" not in mapping:
                raise ValueError(
                    "EXACT glossary entry must use 'source => target': " + entry
                )
            source_text, target_text = mapping.split("=>", 1)
            source_text = " ".join(source_text.split()).strip()
            target_text = target_text.replace("\\n", "\n").strip()
            if not source_text or not target_text:
                raise ValueError("EXACT glossary entry cannot be empty: " + entry)
            self.exact_overrides.append(
                (" ".join(source_text.casefold().split()), target_text)
            )

    def apply_exact_overrides(self, cues, translated):
        for index in range(len(cues)):
            source_key = " ".join(
                " ".join(cues[index]["body"]).casefold().split()
            )
            for exact_source, exact_target in self.exact_overrides:
                if source_key == exact_source:
                    translated[index] = exact_target
                    break
        return translated

    @staticmethod
    def passthrough(cue):
        text = "\n".join(cue["body"]).strip()
        # Bracketed/parenthetical cues are usually stage directions and must be
        # translated (for example, "[Birds chirping]" -> "[Fugle kvidrer]").
        # Keep only genuinely non-linguistic markers such as "[♪♪]" as-is.
        return bool(
            re.fullmatch(r"(?:\[[^\n]*\]|\([^\n]*\))", text)
            and not re.search(r"[A-Za-zÆØÅæøå]", text)
        )

    @staticmethod
    def prompt(cues, source, target, before=(), after=()):
        target_text = "\n".join(
            "@@%d@@\n%s" % (i, "\n".join(cue["body"]))
            for i, cue in enumerate(cues)
            if not SubtitleTranslator.passthrough(cue)
        )
        # OLMoE is deliberately given only the text it must translate.
        # Supplying surrounding dialogue caused this small model to translate
        # reference context instead of the marked target cues.
        return target_text

    def translate_chunk(self, cues, source, target, before=(), after=()):
        active = {
            i for i, cue in enumerate(cues)
            if not self.passthrough(cue)
        }
        if not active:
            return [dict(cue, body=list(cue["body"])) for cue in cues]

        exact = {}
        for index in active:
            source_key = " ".join(
                " ".join(cues[index]["body"]).casefold().split()
            )
            for exact_source, exact_target in self.exact_overrides:
                if source_key == exact_source:
                    exact[index] = exact_target
                    break
        # If every active cue is covered by an exact project mapping, avoid
        # spending a model request on text that is already quality-approved.
        if exact and len(exact) == len(active):
            return [
                dict(
                    cue,
                    body=exact[i].split("\n") if i in exact
                    else list(cue["body"]),
                )
                for i, cue in enumerate(cues)
            ]

        prompt = self.prompt(cues, source, target, before, after)
        glossary = "\n".join(self.glossary).strip()
        glossary_block = (
            "Project glossary and translation notes (use only when relevant):\n"
            + glossary + "\n\n"
            if glossary else ""
        )
        quality_rules = (
            "Use natural, idiomatic %s rather than word-for-word calques. "
            "Keep question words, negation, tense, speaker roles, and names exact. "
            "Translate ordinary words inside bracketed or parenthetical stage "
            "directions, while preserving the brackets. "
            "For Danish, use normal forms such as 'hvor gammel er du' for "
            "'how old are you'; do not invent comparative forms such as "
            "'hvor ældre er du'. Interpret idioms by meaning, not by translating "
            "individual words. "
        ) % target
        target_prompt = prompt
        if not hasattr(self.server, "translate"):
            target_prompt = glossary_block + prompt
        if hasattr(self.server, "translate"):
            context_before = "\n".join(
                " ".join(cue["body"]) for cue in before
            )
            context_after = "\n".join(
                " ".join(cue["body"]) for cue in after
            )
            prompt = (
                "Translate only the target subtitle cues from %s into natural %s. "
                "%s"
                "Use the surrounding dialogue to understand meaning, speakers, "
                "and pronouns. The context is for reference only; do not translate it. "
                "Preserve every @@number@@ marker exactly and return only the "
                "marked target translations.\n\n"
                "%s"
                "Context before (reference only):\n%s\n\n"
                "Context after (reference only):\n%s\n\n"
                "Target cues (translate only):\n%s"
            ) % (
                source, target, quality_rules, glossary_block,
                context_before or "[none]", context_after or "[none]",
                target_prompt,
            )
        cue_text = " ".join(
            " ".join(cues[i]["body"]) for i in sorted(active)
        )
        source_words = len(re.findall(r"\S+", cue_text))
        if hasattr(self.server, "translate"):
            max_tokens = min(512, max(64, source_words * 6 + 32))
        else:
            max_tokens = min(512, max(32, source_words * 3 + 16))
        system = (
            "You are a strict subtitle translation engine. "
            "Translate from %s to natural, idiomatic %s. "
            "Translate every ordinary %s word; do not leave source-language words "
            "unchanged when a normal %s translation exists. Keep proper names. "
            "Style examples: 'Mary went home because she was tired.' becomes "
            "'Mary gik hjem, fordi hun var træt.' Keep character names unchanged; "
            "for example, 'Where is Charles?' -> 'Hvor er Charles?'. When 'you' "
            "means one person, use 'dig', as in 'I will see you tomorrow.' -> "
            "'Jeg vil se dig i morgen.' These examples are guidance only; "
            "do not output them. "
            "Return ONLY the @@number@@ blocks from the user message, in the same "
            "order, with translated subtitle text. Never output context, headings, "
            "instructions, explanations, END CUES, or REFERENCE text."
        ) % (source, target, source, target)
        if hasattr(self.server, "translate"):
            output = self.server.translate(
                prompt, max_tokens, source, target
            )
        else:
            output = self.server.chat(
                prompt, max_tokens=max_tokens, system=system
            )
        forbidden = (
            "END CUES", "REFERENCE BEFORE", "REFERENCE AFTER",
            "TRANSLATE THESE CUES", "Context before:", "Context after:",
        )
        if any(token.lower() in output.lower() for token in forbidden):
            raise RuntimeError("Model echoed prompt scaffolding")
        matches = MARKER_RE.findall(output)
        marker_numbers = [int(number) for number, _ in matches]
        duplicate_markers = len(marker_numbers) != len(set(marker_numbers))
        translated = {int(number): body.strip("\n ") for number, body in matches}
        if duplicate_markers:
            translated = {}
        if (
            (duplicate_markers or set(translated) != active or
             any(not translated[i].strip() for i in active))
            and hasattr(self.server, "translate")
            and len(cues) == 1
            and len(active) == 1
        ):
            only = next(iter(active))
            plain_text = "\n".join(cues[only]["body"])
            fallback = self.server.translate(
                plain_text, max_tokens, source, target
            ).strip()
            if fallback:
                translated = {only: fallback}
        if set(translated) != active or any(not translated[i].strip() for i in active):
            missing = sorted(active - set(translated))[:12]
            raise RuntimeError(
                "Malformed structured translation: returned %d/%d cues; missing %s"
                % (len(translated), len(active), missing)
            )
        self.apply_exact_overrides(cues, translated)
        result = []
        for i, cue in enumerate(cues):
            if i in active:
                result.append(dict(cue, body=translated[i].split("\n")))
            else:
                result.append(dict(cue, body=list(cue["body"])))
        return result


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
                  label, block_cues, quiet=False, checkpoint=None,
                  restart_every=0):
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
    base_translated = state.get("translated", 0)
    block_count = start // block_cues
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
        state["translated"] = base_translated + done
        if checkpoint is not None:
            checkpoint(render_srt(cues, newline).encode("utf-8"), state)
        if not quiet:
            elapsed = max(time.time() - started, 0.001)
            print(
                "  [BLOCK] %s cues %d-%d / %d | %.2f cues/s"
                % (label, pos - len(translated) + 1, pos, len(cues), done / elapsed),
                flush=True,
            )
        block_count += 1
        restart = getattr(getattr(translator, "server", None), "restart", None)
        if (
            restart_every and pos < max_end and
            block_count % restart_every == 0 and restart is not None
        ):
            restart()
    rendered = render_srt(cues, newline).encode("utf-8")
    rendered_cues, _ = parse_srt(rendered.decode("utf-8"))
    validate_srt_layout(cues, rendered_cues)
    return rendered, done


def process_srt_file(src, out, args, translator, label=None):
    state_path = Path(str(out) + ".progress.json")
    state = {"source": str(src), "next_cue": 0, "translated": 0}
    raw = src.read_bytes()
    if not raw:
        raise ValueError("Input subtitle file is empty")
    if state_path.exists() and not args.fresh:
        try:
            old = json.loads(state_path.read_text(encoding="utf-8"))
            if old.get("source") == str(src):
                partial = old.get("partial_output")
                if isinstance(partial, str):
                    raw = partial.encode("utf-8")
                    state.update(old)
                    print("[RESUME]", label or src, "from cue", state["next_cue"] + 1, flush=True)
                elif out.exists():
                    raw = out.read_bytes()
                    state.update(old)
                    print("[RESUME]", label or src, "from cue", state["next_cue"] + 1, flush=True)
        except Exception as exc:
            print("[WARN] Ignoring invalid resume data:", exc, flush=True)
    out.parent.mkdir(parents=True, exist_ok=True)

    def save_checkpoint(partial_output, current_state):
        current_state["partial_output"] = partial_output.decode("utf-8")
        write_state(state_path, current_state)

    translated, count = translate_srt(
        raw, state["next_cue"], args.limit, translator,
        args.source_language, args.target_language, state,
        label or str(src), args.block_cues, args.quiet,
        checkpoint=save_checkpoint,
        restart_every=getattr(args, "restart_every", 0),
    )
    tmp = Path(str(out) + ".tmp")
    tmp.write_bytes(translated)
    os.replace(tmp, out)
    state.pop("partial_output", None)
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
                        partial = state.get("partial_output")
                        if isinstance(partial, str):
                            entry = state.get("entry", 0)
                            if 0 <= entry < len(payloads):
                                payloads[entry] = partial.encode("utf-8")
                        print("[RESUME] ZIP entry", state["entry"] + 1, flush=True)
        except Exception as exc:
            print("[WARN] Ignoring invalid ZIP resume data:", exc, flush=True)
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        write_zip(out, infos, payloads)
    for index in range(state["entry"], len(infos)):
        info = infos[index]
        if not info.filename.lower().endswith(".srt"):
            state["entry"], state["next_cue"] = index + 1, 0
            write_state(state_path, state)
            continue

        def save_zip_checkpoint(partial_output, current_state):
            current_state["partial_output"] = partial_output.decode("utf-8")
            write_state(state_path, current_state)

        payloads[index], count = translate_srt(
            payloads[index], state["next_cue"], args.limit, translator,
            args.source_language, args.target_language, state,
            info.filename, args.block_cues, args.quiet,
            checkpoint=save_zip_checkpoint,
            restart_every=getattr(args, "restart_every", 0),
        )
        complete = state["next_cue"] >= state["cue_total"]
        state["entry"] = index + 1 if complete else index
        if complete:
            state["next_cue"] = 0
            state.pop("partial_output", None)
        out.parent.mkdir(parents=True, exist_ok=True)
        write_zip(out, infos, payloads)
        write_state(state_path, state)
        print("[CHECKPOINT]", info.filename, "| added", count, "cues", flush=True)
        if args.limit and count >= args.limit:
            break
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        write_zip(out, infos, payloads)
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
    setup = ROOT / "setup_colibri.sh"
    runtime_ready = Path(args.coli_bin).is_file() and os.access(args.coli_bin, os.X_OK)
    if not runtime_ready and setup.is_file():
        print("[SETUP] Colibri runtime missing; building it automatically...", flush=True)
        subprocess.run(
            [
                "bash", str(setup), "--runtime",
                "--runtime-dir", str(ROOT / ".runtime" / "colibri"),
                "--model-dir", str(args.model),
            ],
            check=False,
        )
    if runtime_ok(args.coli_bin, args.model):
        return
    if setup.is_file():
        print("[SETUP] Running Colibri readiness check...", flush=True)
        subprocess.run(
            [
                "bash", str(setup), "--check",
                "--runtime-dir", str(ROOT / ".runtime" / "colibri"),
                "--model-dir", str(args.model),
            ],
            check=False,
        )


def colibri_doctor(coli_bin, model, context, ram_gb, cap, gpu):
    """Validate the same resource plan that the server will use."""
    cmd = [
        str(coli_bin), "doctor", "--model", str(model),
        "--ctx", str(context), "--auto-tier", "--policy", "quality",
    ]
    if gpu and gpu.lower() not in ("none", "cpu", "off"):
        cmd += ["--gpu", gpu]
    if ram_gb > 0:
        cmd += ["--ram", str(ram_gb)]
    if cap > 0:
        cmd += ["--cap", str(cap)]
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, check=False,
    )
    output = result.stdout.strip()
    if output:
        print(output, flush=True)
    if result.returncode != 0:
        raise RuntimeError(
            "Colibri doctor reported that this model/runtime is not ready."
        )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Translate SRT files, folders, or ZIPs with local TranslateGemma "
            "(default) or Colibrì."
        )
    )
    parser.add_argument("input")
    parser.add_argument("-o", "--output")
    parser.add_argument(
        "--backend", choices=("translategemma", "colibri"),
        default="translategemma",
        help="Translation engine (default: translategemma)",
    )
    parser.add_argument("--llama-bin", default=str(DEFAULT_LLAMA))
    parser.add_argument(
        "--gguf-model", default=str(DEFAULT_TRANSLATEGEMMA_MODEL)
    )
    parser.add_argument(
        "--device", default="cpu",
        help="cpu (default), auto, or an available llama.cpp device such as Vulkan0",
    )
    parser.add_argument("--gpu-layers", default="all")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument(
        "--cache-prompt", action="store_true",
        help="Reuse llama.cpp prompt KV cache (faster, but uses more RAM)",
    )
    parser.add_argument(
        "--restart-every", type=int, default=8,
        help="Restart TranslateGemma after this many blocks (0 disables; default: 8)",
    )
    parser.add_argument("--model", default=str(DEFAULT_MODEL))
    parser.add_argument("--coli-bin", default=str(DEFAULT_COLI))
    parser.add_argument("--source-language", default="English")
    parser.add_argument("--target-language", default="Danish")
    parser.add_argument(
        "--glossary",
        help="Optional UTF-8 glossary/notes file included in every translation prompt",
    )
    parser.add_argument("--block-cues", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--suffix")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--allow-sleep", action="store_true")
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--context-size", type=int, default=0)
    parser.add_argument("--ram-gb", type=int, default=0)
    parser.add_argument("--cap", type=int, default=0)
    parser.add_argument("--gpu", default="none")
    parser.add_argument("--startup-timeout", type=int, default=300)
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    args = parser.parse_args()

    try:
        args.glossary_entries = load_glossary(args.glossary)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    if args.context_size == 0:
        args.context_size = 2048 if args.backend == "translategemma" else 4096
    if args.block_cues == 0:
        args.block_cues = 16 if args.backend == "translategemma" else 8
    if args.suffix is None:
        args.suffix = (
            "_translategemma_da"
            if args.backend == "translategemma"
            else "_colibri_da"
        )
    if args.block_cues < 1:
        parser.error("--block-cues must be at least 1")
    if args.context_size < 1:
        parser.error("--context-size must be at least 1")
    if args.backend == "translategemma" and args.context_size > 2048:
        parser.error("TranslateGemma supports at most 2048 context tokens")
    if args.threads < 1:
        parser.error("--threads must be at least 1")
    if args.restart_every < 0:
        parser.error("--restart-every cannot be negative")
    if args.cap < 0:
        parser.error("--cap cannot be negative")
    if args.suffix and not args.suffix.startswith(("_", "-", ".")):
        args.suffix = "_" + args.suffix

    args.coli_bin = str(Path(args.coli_bin).expanduser().resolve())
    args.model = str(Path(args.model).expanduser().resolve())
    args.llama_bin = str(Path(args.llama_bin).expanduser().resolve())
    args.gguf_model = str(Path(args.gguf_model).expanduser().resolve())

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
    server = None
    if not args.allow_sleep:
        sleep_inhibitor = start_sleep_inhibitor()

    try:
        if args.backend == "translategemma":
            setup = ROOT / "setup_translategemma.sh"
            if (
                not Path(args.llama_bin).is_file()
                or not os.access(args.llama_bin, os.X_OK)
            ) and setup.is_file():
                print(
                    "[SETUP] llama.cpp runtime missing; installing it locally...",
                    flush=True,
                )
                subprocess.run(["bash", str(setup), "--runtime"], check=True)
            if (
                not Path(args.llama_bin).is_file()
                or not os.access(args.llama_bin, os.X_OK)
            ):
                parser.error(
                    "llama.cpp runtime is missing. Run: "
                    "./setup_translategemma.sh --runtime"
                )
            if not Path(args.gguf_model).is_file():
                parser.error(
                    "TranslateGemma model is missing. Run: "
                    "./setup_translategemma.sh --install-model"
                )
            key = os.environ.get(
                "TRANSLATEGEMMA_API_KEY",
                os.environ.get("COLIBRI_TRANSLATOR_API_KEY", DEFAULT_KEY),
            )
            print(
                "[TRANSLATEGEMMA] Preflight check | model:",
                args.gguf_model,
                "| threads:", args.threads,
                "| context:", args.context_size,
                "| prompt-cache:", "on" if args.cache_prompt else "off",
                "| restart-every:", args.restart_every,
                flush=True,
            )
            server = TranslateGemmaServer(
                args.llama_bin, args.gguf_model, key, args.port,
                args.context_size, args.threads,
                ROOT / ".runtime" / "translategemma.log",
                args.startup_timeout, device=args.device,
                gpu_layers=args.gpu_layers,
                cache_prompt=args.cache_prompt,
            )
        else:
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
                    "Colibri model is missing. Run: "
                    "./setup_colibri.sh --install-model"
                )
            key = os.environ.get("COLIBRI_TRANSLATOR_API_KEY", DEFAULT_KEY)
            print("[COLIBRI] Preflight check...", flush=True)
            colibri_doctor(
                args.coli_bin, args.model, args.context_size, args.ram_gb,
                args.cap, args.gpu,
            )
            server = ColibriServer(
                args.coli_bin, args.model, args.model_id, key, args.port,
                args.context_size, args.ram_gb, args.cap, args.gpu,
                ROOT / ".runtime" / "colibri-translator.log",
                args.startup_timeout,
            )

        server.start()
        translator = SubtitleTranslator(server, glossary=args.glossary_entries)
        if src.is_dir():
            process_folder(src, out, args, translator)
        elif src.suffix.lower() == ".zip":
            process_zip(src, out, args, translator)
        else:
            process_srt_file(src, out, args, translator)
    finally:
        if server is not None:
            server.stop()
        stop_sleep_inhibitor(sleep_inhibitor)


if __name__ == "__main__":
    main()
