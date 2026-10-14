#!/usr/bin/env python3
"""Local TranslateGemma server adapter backed by llama.cpp."""

import json
import os
import re
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


LANGUAGE_NAMES = {
    "af": "Afrikaans", "sq": "Albanian", "am": "Amharic",
    "ar": "Arabic", "hy": "Armenian", "as": "Assamese",
    "az": "Azerbaijani", "eu": "Basque", "be": "Belarusian",
    "bn": "Bengali", "bs": "Bosnian", "bg": "Bulgarian",
    "my": "Burmese", "ca": "Catalan", "zh": "Chinese",
    "hr": "Croatian", "cs": "Czech", "da": "Danish",
    "nl": "Dutch", "en": "English", "et": "Estonian",
    "fi": "Finnish", "fil": "Filipino", "fr": "French",
    "gl": "Galician", "ka": "Georgian", "de": "German",
    "el": "Greek", "gu": "Gujarati", "ha": "Hausa",
    "he": "Hebrew", "hi": "Hindi", "hu": "Hungarian",
    "is": "Icelandic", "id": "Indonesian", "ga": "Irish",
    "it": "Italian", "ja": "Japanese", "jv": "Javanese",
    "kn": "Kannada", "kk": "Kazakh", "km": "Khmer",
    "ko": "Korean", "ky": "Kyrgyz", "lo": "Lao",
    "lv": "Latvian", "lt": "Lithuanian", "mk": "Macedonian",
    "ms": "Malay", "ml": "Malayalam", "mr": "Marathi",
    "mn": "Mongolian", "ne": "Nepali", "no": "Norwegian",
    "nb": "Norwegian Bokmål", "nn": "Norwegian Nynorsk",
    "or": "Odia", "ps": "Pashto", "fa": "Persian",
    "pl": "Polish", "pt": "Portuguese", "pa": "Punjabi",
    "ro": "Romanian", "ru": "Russian", "sr": "Serbian",
    "si": "Sinhala", "sk": "Slovak", "sl": "Slovenian",
    "es": "Spanish", "sw": "Swahili", "sv": "Swedish",
    "ta": "Tamil", "te": "Telugu", "th": "Thai",
    "tr": "Turkish", "uk": "Ukrainian", "ur": "Urdu",
    "uz": "Uzbek", "vi": "Vietnamese", "cy": "Welsh",
}


def language_spec(value):
    raw = str(value).strip().replace("_", "-")
    if not raw:
        raise ValueError("Language code/name cannot be empty")
    if re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?", raw):
        parts = raw.split("-")
        code = parts[0].lower()
        if len(parts) > 1:
            code += "-" + parts[1].upper()
    else:
        reverse = {name.casefold(): code for code, name in LANGUAGE_NAMES.items()}
        code = reverse.get(raw.casefold())
        if not code:
            raise ValueError(
                "Unknown TranslateGemma language %r; pass its ISO code, such as en or da"
                % value
            )
    name = LANGUAGE_NAMES.get(code.split("-", 1)[0])
    if not name:
        raise ValueError(
            "TranslateGemma has no configured display name for code %r" % code
        )
    return code, name


def _http_json(url, payload=None, key=None, timeout=5):
    headers = {}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if key:
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("HTTP %d from llama.cpp: %s" % (exc.code, body[-1200:])) from exc


def _free_port(preferred):
    for port in (preferred, 0):
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", port))
            return sock.getsockname()[1]
        except OSError:
            pass
        finally:
            sock.close()
    raise RuntimeError("Could not allocate a local inference-server port")


class TranslateGemmaServer:
    def __init__(
        self, llama_bin, model, key, port, context, threads,
        log_path, startup_timeout, device="auto", gpu_layers="all",
    ):
        self.llama_bin = Path(llama_bin)
        self.model = Path(model)
        self.key = key
        self.port = _free_port(port)
        self.context = context
        self.threads = threads
        self.device_option = device
        self.gpu_layers = gpu_layers
        self.device = None
        self.force_cpu = False
        self.log_path = Path(log_path)
        self.startup_timeout = startup_timeout
        self.proc = None
        self.log = None
        self.api = "http://127.0.0.1:%d" % self.port

    def start(self):
        if self.context > 2048:
            raise ValueError(
                "TranslateGemma supports up to 2048 context tokens; lower --context-size"
            )
        if not self.llama_bin.is_file() or not os.access(self.llama_bin, os.X_OK):
            raise RuntimeError(
                "llama.cpp runtime is missing. Run ./setup_translategemma.sh --runtime"
            )
        if not self.model.is_file():
            raise RuntimeError(
                "TranslateGemma model is missing. Run ./setup_translategemma.sh --install-model"
            )

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = self.log_path.open("a", encoding="utf-8")
        if self.device_option.lower() in ("cpu", "none", "off"):
            self.device = None
            self.force_cpu = True
        elif self.device_option == "auto":
            try:
                probe = subprocess.run(
                    [str(self.llama_bin), "serve", "--list-devices"],
                    capture_output=True, text=True, timeout=15, check=False,
                )
                match = re.search(
                    r"^\s*((?:Vulkan|CUDA|ROCm|Metal|SYCL)\d+):",
                    probe.stdout, re.M,
                )
                self.device = match.group(1) if match else None
            except (OSError, subprocess.TimeoutExpired):
                self.device = None
        else:
            self.device = self.device_option

        command = [
            str(self.llama_bin), "serve",
            "--model", str(self.model),
            "--no-jinja",
            "--ctx-size", str(self.context),
            "--threads", str(self.threads),
            "--threads-batch", str(self.threads),
            "--batch-size", "128",
            "--ubatch-size", "64",
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "--parallel", "1",
            "--api-key", self.key,
        ]
        if self.device:
            command += [
                "--device", self.device,
                "--n-gpu-layers", self.gpu_layers,
            ]
            print("[TRANSLATEGEMMA] GPU offload:", self.device, flush=True)
        elif self.force_cpu:
            command += ["--device", "none"]
            print("[TRANSLATEGEMMA] CPU-only inference", flush=True)
        else:
            print("[TRANSLATEGEMMA] CPU-only inference", flush=True)
        self.proc = subprocess.Popen(
            command,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.time() + self.startup_timeout
        last_error = ""
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    "llama.cpp exited during startup:\n" + self._log_tail()
                )
            try:
                models = _http_json(
                    self.api + "/v1/models", key=self.key, timeout=3
                )
                if models.get("data"):
                    print("[TRANSLATEGEMMA] Server ready on port", self.port, flush=True)
                    return
            except Exception as exc:
                last_error = str(exc)
            time.sleep(1)
        self.stop()
        raise TimeoutError(
            "TranslateGemma server did not become ready: " + last_error
        )

    def translate(self, text, max_tokens, source_language, target_language):
        source_code, source_name = language_spec(source_language)
        target_code, target_name = language_spec(target_language)
        prompt = (
            "<start_of_turn>user\n"
            "You are a professional %s (%s) to %s (%s) translator. "
            "Your goal is to accurately convey the meaning and nuances of the "
            "original %s text while adhering to %s grammar, vocabulary, and "
            "cultural sensitivities.\n"
            "Produce only the %s translation, without any additional explanations "
            "or commentary. Please translate the following %s text into %s:\n\n\n"
            "%s<end_of_turn>\n<start_of_turn>model\n"
        ) % (
            source_name, source_code, target_name, target_code,
            source_name, target_name, target_name, source_name, target_name, text,
        )
        payload = {
            "prompt": prompt,
            "n_predict": min(512, max(32, int(max_tokens))),
            "temperature": 0.0,
            "stop": ["<end_of_turn>", "<eos>"],
            "cache_prompt": True,
        }
        result = _http_json(
            self.api + "/completion",
            payload=payload,
            key=self.key,
            timeout=900,
        )
        return (result.get("content") or "").strip()

    def _log_tail(self):
        try:
            return self.log_path.read_text(errors="replace")[-5000:]
        except OSError:
            return "(no llama.cpp log available)"

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=8)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                    self.proc.wait(timeout=3)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass
        if self.log is not None:
            self.log.close()
            self.log = None
