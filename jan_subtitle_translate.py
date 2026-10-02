#!/usr/bin/env python3
"""Resumable SRT/ZIP subtitle translation using Jan's local TranslateGemma model."""
import argparse, json, os, re, signal, shutil, socket, subprocess, time
import urllib.request, zipfile
from pathlib import Path

DEFAULT_DATA = "/mnt/SmollSSD/Natural Stupidity"
DEFAULT_KEY = "jan-local"
TIME_RE = re.compile(r"^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+")
TAG_RE = re.compile(r"(<[^>\n]+>|{\\[^}\n]+})")
LETTER_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿĀ-ž]")
DANISH_WORDS = re.compile(r"\b(?:og|jeg|du|det|der|ikke|på|han|hun|hvad|hvor|hvordan|skal|har|så|nej)\b", re.I)


def looks_danish(text):
    """Conservative heuristic: only skip when several Danish signals agree."""
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

def find_model(folder):
    for yml in (Path(folder) / "llamacpp/models").glob("*/model.yml"):
        text = yml.read_text(errors="replace")
        if "translate" not in (text + str(yml)).lower():
            continue
        path = next((x.split(":", 1)[1].strip() for x in text.splitlines()
                     if x.startswith("model_path:")), "")
        p = Path(path) if Path(path).is_absolute() else Path(folder) / path
        if p.exists():
            return p
    raise FileNotFoundError("TranslateGemma was not found in Jan's data folder")

def find_backend(folder):
    base = Path(folder) / "llamacpp/backends"
    found = sorted(base.glob("*/linux-vulkan-x64/build/bin/llama-server"), reverse=True)
    if not found:
        raise FileNotFoundError("Jan's Vulkan llama-server was not found")
    return found[0]

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
    raise RuntimeError("No free local port")

def start_sleep_inhibitor():
    """Keep the desktop awake only for the lifetime of this translation run."""
    command = shutil.which("systemd-inhibit")
    if not command:
        print("[POWER] systemd-inhibit not found; sleep prevention unavailable",
              flush=True)
        return None
    try:
        inhibitor = subprocess.Popen(
            [
                command,
                "--what=idle:sleep",
                "--mode=block",
                "--who=Jan subtitle translator",
                "--why=Subtitle translation in progress",
                "sleep",
                "infinity",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.1)
        if inhibitor.poll() is not None:
            print("[POWER] Could not enable sleep prevention", flush=True)
            return None
        print("[POWER] Sleep prevention enabled for this run", flush=True)
        return inhibitor
    except OSError as exc:
        print("[POWER] Could not enable sleep prevention:", exc, flush=True)
        return None


def stop_sleep_inhibitor(inhibitor):
    if inhibitor is None or inhibitor.poll() is not None:
        return
    inhibitor.terminate()
    try:
        inhibitor.wait(timeout=3)
    except subprocess.TimeoutExpired:
        inhibitor.kill()


def request(url, payload, key, timeout=180):
    data = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode())

def start_server(folder, model, port, key, ctx, log_path):
    backend = find_backend(folder)
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = str(backend.parent) + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    cmd = [str(backend), "--host", "127.0.0.1", "--port", str(port),
           "--model", str(model), "--alias", "translategemma", "--ctx-size", str(ctx),
           "--n-gpu-layers", "all", "--device", "Vulkan0", "--parallel", "1",
           "--api-key", key, "--no-jinja"]
    log = open(log_path, "a", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                            env=env, start_new_session=True)
    deadline = time.time() + 150
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=3) as res:
                if res.status == 200:
                    return proc, log
        except Exception:
            pass
        if proc.poll() is not None:
            log.close()
            raise RuntimeError(Path(log_path).read_text(errors="replace")[-4000:])
        time.sleep(1)
    proc.terminate()
    log.close()
    raise TimeoutError("Jan did not finish loading the model")

def translate(api, key, text, source, target):
    if not text.strip() or not LETTER_RE.search(text):
        return text
    if re.fullmatch(r"\s*(?:\[.*\]|\(.*\))\s*", text, re.S):
        return text
    lead = re.match(r"^\s*(?:<[^>]+>\s*)+", text)
    tail = re.search(r"(?:\s*</?[^>]+>\s*)+$", text)
    prefix = lead.group(0) if lead else ""
    suffix = tail.group(0) if tail and tail.start() >= len(prefix) else ""
    safe = TAG_RE.sub("", text)
    prompt = (
        "<start_of_turn>user\n"
        "You are a professional %s translator producing natural, idiomatic %s subtitles.\n"
        "Translate only the SOURCE text. Output only the translation: no explanation, labels, "
        "quotation marks, or commentary.\n"
        "Keep meaning, tone, names, punctuation, and line breaks where possible.\n\nSOURCE:\n%s"
        "<end_of_turn>\n<start_of_turn>model\n"
    ) % (source, target, safe)
    payload = {"prompt": prompt, "n_predict": max(160, len(safe) * 4),
               "temperature": 0.15, "top_p": 0.9,
               "stop": ["<end_of_turn>", "<start_of_turn>"]}
    last = ""
    for attempt in range(3):
        try:
            out = request(api + "/completion", payload, key)["content"].strip()
            out = out.replace(chr(96) * 3 + "text", "").replace(chr(96) * 3, "").strip()
            out = re.sub(r"TAGTOKEN\d*", "", out).strip()
            if out:
                return prefix + out + suffix
            last = "empty response"
        except Exception as exc:
            last = str(exc)
        payload["temperature"] = 0.05
        time.sleep(attempt + 1)
    raise RuntimeError("Translation failed: " + last)

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

def translate_chunk(api, key, cues, source, target, before=(), after=()):
    """Translate target cues while showing nearby dialogue as read-only context."""
    target_text = "\n".join(
        "@@%d@@\n%s" % (i, "\n".join(c["body"]))
        for i, c in enumerate(cues)
    )
    before_text = "\n".join("\n".join(c["body"]) for c in before)
    after_text = "\n".join("\n".join(c["body"]) for c in after)
    source_text = (
        "REFERENCE BEFORE - do not output:\n" + before_text +
        "\nEND REFERENCE BEFORE\n\n"
        "TRANSLATE THESE CUES:\n" + target_text +
        "\nEND CUES\n\n"
        "REFERENCE AFTER - do not output:\n" + after_text +
        "\nEND REFERENCE AFTER"
    )
    prompt = (
        "<start_of_turn>user\n"
        "You are translating one continuous subtitle section from %s to natural, idiomatic %s.\n"
        "Use the surrounding dialogue for context and consistency.\n"
        "Translate only the dialogue text. Keep every @@number@@ marker exactly unchanged and in the same order.\n"
        "Do not add explanations, labels, quotation marks, or commentary. Preserve HTML tags, sound markers, names, tone, and line breaks where possible.\n\n"
        "%s"
        "<end_of_turn>\n<start_of_turn>model\n"
    ) % (source, target, source_text)
    payload = {"prompt": prompt, "n_predict": min(4096, max(512, len(source_text) // 2)),
               "temperature": 0.15, "top_p": 0.9,
               "stop": ["<end_of_turn>", "<start_of_turn>"]}
    last = ""
    for attempt in range(3):
        try:
            out = request(api + "/completion", payload, key)["content"].strip()
            out = out.replace(chr(96) * 3 + "text", "").replace(chr(96) * 3, "").strip()
            matches = re.findall(r"@@\s*(\d{1,3})\s*@@\s*\n(.*?)(?=\n@@\s*\d{1,3}\s*@@|\Z)", out, re.S)
            translated = {int(number): body.strip("\n ") for number, body in matches}
            expected = {i for i in range(len(cues))}
            if set(translated) == expected and all(translated[i].strip() for i in expected):
                return [dict(c, body=translated[i].split("\n")) for i, c in enumerate(cues)]
            missing = sorted(expected - set(translated))[:12]
            last = "missing or reordered cue markers; returned %d/%d; missing %s" % (len(translated), len(expected), missing)
        except Exception as exc:
            last = str(exc)
        payload["temperature"] = 0.05
        time.sleep(attempt + 1)
    raise RuntimeError("Context translation failed: " + last)


def translate_resilient(api, key, cues, source, target, before=(), after=()):
    """Retry a failed context block at smaller sizes without losing the file."""
    try:
        return translate_chunk(api, key, cues, source, target, before, after)
    except RuntimeError:
        if len(cues) <= 1:
            raise
        mid = max(1, len(cues) // 2)
        left = translate_resilient(
            api, key, cues[:mid], source, target,
            list(before)[-8:], list(cues[mid:])[:8] + list(after)[:8]
        )
        right = translate_resilient(
            api, key, cues[mid:], source, target,
            list(before)[-8:] + left[-8:], list(after)[:8]
        )
        return left + right


def translate_srt(raw, start, limit, api, source, target, state,
                label="subtitle", verbose=True, block_cues=32):
    cues, newline = parse_srt(raw.decode("utf-8-sig"))
    state["cue_total"] = len(cues)
    if verbose:
        print("[FILE]", label, "-", len(cues), "cues", flush=True)
    if looks_danish(subtitle_text(cues)):
        state["next_cue"] = len(cues)
        state["skipped_danish"] = True
        print("[SKIP] Already Danish:", label, flush=True)
        return raw, 0
    state.pop("skipped_danish", None)
    done = 0
    max_end = len(cues)
    if limit:
        max_end = min(max_end, start + limit)
    pos = start
    while pos < max_end:
        end = pos
        chars = 0
        while end < max_end and (
            end == pos or (
                end - pos < block_cues and
                chars + len("\n".join(cues[end]["body"])) <= 4000
            )
        ):
            chars += len("\n".join(cues[end]["body"]))
            end += 1
        block_start = pos
        before = cues[max(0, pos - 8):pos]
        after = cues[end:min(len(cues), end + 8)]
        translated = translate_resilient(
            api, state["key"], cues[pos:end], source, target, before, after
        )
        cues[pos:end] = translated
        done += end - pos
        pos = end
        state["next_cue"] = pos
        if verbose:
            print("  [BLOCK]", label, "cues", block_start + 1, "-", pos,
                  "/", len(cues), "| translated", done, flush=True)
    return render_srt(cues, newline).encode("utf-8"), done

def write_state(path, state):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)

def write_zip(path, infos, payloads):
    tmp = Path(str(path) + ".tmp")
    with zipfile.ZipFile(tmp, "w", allowZip64=True) as out:
        for info, payload in zip(infos, payloads):
            out.writestr(info, payload)
    os.replace(tmp, path)

def process_srt_file(src, out, args, api, key, label=None):
    state_file = Path(str(out) + ".progress.json")
    state = {
        "key": key, "entry": 0, "next_cue": 0,
        "translated": 0, "source": str(src)
    }
    raw = src.read_bytes()
    if not raw:
        raise ValueError("Input subtitle file is empty")
    if out.exists() and state_file.exists() and not args.fresh:
        try:
            old = json.loads(state_file.read_text(encoding="utf-8"))
            if old.get("source") == str(src):
                raw = out.read_bytes()
                state.update(old)
                print("[RESUME]", label or src, "from cue",
                      state.get("next_cue", 0) + 1, flush=True)
        except Exception as exc:
            print("[WARN] Resume data unusable:", exc, flush=True)
    translated, count = translate_srt(
        raw, state["next_cue"], args.limit, api,
        args.source_language, args.target_language, state,
        label or str(src), not args.quiet, args.block_cues
    )
    state["translated"] += count
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(out) + ".tmp")
    tmp.write_bytes(translated)
    os.replace(tmp, out)
    write_state(state_file, state)
    skipped = bool(state.get("skipped_danish"))
    status = "skipped Danish" if skipped else "translated"
    print("[DONE]", label or src, "-", status, "| added", count,
          "cues | output:", out, flush=True)
    return {"translated": count, "skipped": skipped, "cues": state["cue_total"]}


def process_folder(src, out_root, args, api, key):
    out_root = out_root.resolve()
    files = [
        p for p in sorted(src.rglob("*.srt"))
        if p.is_file() and not (out_root == p or out_root in p.parents)
    ]
    if not files:
        raise FileNotFoundError("No .srt files found in folder")
    print("[FOLDER]", src, "-", len(files), "SRT files found", flush=True)
    summary = {"files": 0, "translated": 0, "skipped": 0, "errors": 0}
    for number, file_path in enumerate(files, 1):
        relative = file_path.relative_to(src)
        destination = out_root / relative
        destination = destination.with_name(
            destination.stem + args.suffix + destination.suffix
        )
        label = "[%d/%d] %s" % (number, len(files), relative)
        try:
            result = process_srt_file(
                file_path, destination, args, api, key, label
            )
            summary["files"] += 1
            summary["translated"] += result["translated"]
            summary["skipped"] += int(result["skipped"])
        except Exception as exc:
            summary["errors"] += 1
            print("[ERROR]", label, "-", exc, flush=True)
    print("[SUMMARY] files:", summary["files"],
          "| translated cues:", summary["translated"],
          "| skipped Danish:", summary["skipped"],
          "| errors:", summary["errors"], flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Translate SRT files, folders, or subtitle ZIP files with Jan."
    )
    parser.add_argument("input", help="An .srt file, folder of .srt files, or ZIP")
    parser.add_argument("-o", "--output",
                        help="Output file or output folder when input is a folder")
    parser.add_argument("--source-language", default="English")
    parser.add_argument("--target-language", default="Danish")
    parser.add_argument("--data-folder",
                        default=os.environ.get("JAN_DATA_FOLDER", DEFAULT_DATA))
    parser.add_argument("--limit", type=int, default=0,
                        help="Maximum cues per file (0 = all)")
    parser.add_argument("--block-cues", type=int, default=32,
                        help="Target cues per model request (default: 32; lower if needed)")
    parser.add_argument("--fresh", action="store_true",
                        help="Ignore existing progress and output")
    parser.add_argument("--quiet", action="store_true",
                        help="Only show file results and errors")
    parser.add_argument("--allow-sleep", action="store_true",
                        help="Do not prevent the PC from sleeping during translation")
    parser.add_argument("--suffix", default="_jan_da",
                        help="Suffix for automatic output names; e.g. DK or _Danish")
    parser.add_argument("--keep-server", action="store_true")
    parser.add_argument("--port", type=int, default=6768)
    parser.add_argument("--context-size", type=int, default=16384)
    args = parser.parse_args()
    if args.block_cues < 1:
        parser.error("--block-cues must be at least 1")

    if args.suffix and not args.suffix.startswith(("_", "-", ".")):
        args.suffix = "_" + args.suffix

    src = Path(args.input).expanduser().resolve()
    if not src.exists():
        parser.error("Input does not exist: " + str(src))

    if src.is_dir():
        out = (Path(args.output).expanduser().resolve()
               if args.output else src.with_name(src.name + args.suffix))
        if out.exists() and out.is_file():
            parser.error("Folder output must be a directory: " + str(out))
    elif args.output:
        out = Path(args.output).expanduser().resolve()
    elif src.suffix.lower() == ".zip":
        out = src.with_name(src.stem + args.suffix + src.suffix)
    else:
        out = src.with_name(src.stem + args.suffix + src.suffix)

    state_file = Path(str(out) + ".progress.json")
    model = find_model(args.data_folder)
    key = os.environ.get("JAN_SUBTITLE_API_KEY", DEFAULT_KEY)
    port = free_port(args.port)
    log_path = Path(args.data_folder) / "logs/subtitle-translator.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    sleep_inhibitor = None
    proc = None
    log = None
    if not args.allow_sleep:
        sleep_inhibitor = start_sleep_inhibitor()

    try:
        print("Jan model:", model, flush=True)
        print("GPU: Vulkan0 / Radeon RX 7800 XT", flush=True)
        print("Loading model; first load can take about a minute...", flush=True)
        proc, log = start_server(
            args.data_folder, model, port, key, args.context_size, log_path
        )
        api = "http://127.0.0.1:%d" % port
        state = {
            "key": key, "entry": 0, "next_cue": 0,
            "translated": 0, "source": str(src)
        }
        if src.is_dir():
            process_folder(src, out, args, api, key)
        elif src.suffix.lower() == ".zip":
            with zipfile.ZipFile(src) as archive:
                infos = archive.infolist()
                source_payloads = [archive.read(i) for i in infos]
            payloads = list(source_payloads)
            if out.exists() and state_file.exists() and not args.fresh:
                try:
                    old = json.loads(state_file.read_text(encoding="utf-8"))
                    if (old.get("source") == str(src)
                            and old.get("entry_count") == len(infos)):
                        with zipfile.ZipFile(out) as archive:
                            if len(archive.infolist()) == len(infos):
                                payloads = [archive.read(i)
                                            for i in archive.infolist()]
                                state.update(old)
                                print("[RESUME] ZIP from entry",
                                      state.get("entry", 0) + 1, flush=True)
                except Exception as exc:
                    print("[WARN] Resume data unusable:", exc, flush=True)
            state["entry_count"] = len(infos)
            for entry in range(state["entry"], len(infos)):
                if not infos[entry].filename.lower().endswith(".srt"):
                    state["entry"], state["next_cue"] = entry + 1, 0
                    continue
                before = state["translated"]
                payloads[entry], count = translate_srt(
                    payloads[entry], state["next_cue"], args.limit, api,
                    args.source_language, args.target_language, state,
                    infos[entry].filename, not args.quiet, args.block_cues
                )
                state["translated"] += count
                complete = state["next_cue"] >= state["cue_total"]
                state["entry"] = entry + 1 if complete else entry
                if complete:
                    state["next_cue"] = 0
                write_zip(out, infos, payloads)
                write_state(state_file, state)
                print("[CHECKPOINT]", infos[entry].filename,
                      "added", state["translated"] - before, "cues", flush=True)
                if args.limit and state["translated"] >= args.limit:
                    break
            print("[DONE] ZIP output:", out, flush=True)
        else:
            process_srt_file(src, out, args, api, key)
    finally:
        if log is not None:
            log.close()
        if proc is not None and not args.keep_server:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        stop_sleep_inhibitor(sleep_inhibitor)


if __name__ == "__main__":
    main()