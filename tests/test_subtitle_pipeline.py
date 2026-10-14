#!/usr/bin/env python3
import argparse
import json
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import colibri_subtitle_translate as app
import translategemma_backend as translategemma


RAW = (
    "1\n"
    "00:00:01,000 --> 00:00:02,000\n"
    "Hello there.\n\n"
    "2\n"
    "00:00:03,000 --> 00:00:04,000\n"
    "Where are you?\n"
)


class MarkerServer:
    def __init__(self, duplicate=False):
        self.duplicate = duplicate
        self.calls = []

    def chat(self, prompt, max_tokens=0, system=None):
        self.calls.append((prompt, max_tokens, system))
        numbers = [int(n) for n in re.findall(r"@@(\d+)@@", prompt)]
        if self.duplicate and numbers:
            numbers.append(numbers[-1])
        return "\n".join("@@%d@@\nDanish %d" % (n, n) for n in numbers)


class TranslateGemmaMarkerServer:
    def __init__(self):
        self.calls = []

    def translate(self, prompt, max_tokens, source, target):
        self.calls.append((prompt, max_tokens, source, target))
        numbers = [int(n) for n in re.findall(r"@@(\d+)@@", prompt)]
        return "\n".join("@@%d@@\nOversat %d" % (n, n) for n in sorted(set(numbers)))


class SubtitlePipelineTests(unittest.TestCase):
    def test_parse_render_and_layout(self):
        cues, newline = app.parse_srt(RAW)
        self.assertEqual(newline, "\n")
        self.assertEqual(len(cues), 2)
        self.assertEqual(app.render_srt(cues, newline), RAW)
        parsed, _ = app.parse_srt(app.render_srt(cues, newline))
        app.validate_srt_layout(cues, parsed)

    def test_malformed_srt_is_rejected(self):
        with self.assertRaises(ValueError):
            app.parse_srt("")
        with self.assertRaises(ValueError):
            app.parse_srt("1\nnot a timestamp\ntext\n")
        with self.assertRaises(ValueError):
            app.parse_srt("1\n00:00:01,000 --> 00:00:02,000\n")

    def test_colibri_marker_contract_and_duplicate_rejection(self):
        server = MarkerServer()
        translator = app.SubtitleTranslator(server)
        cues, _ = app.parse_srt(RAW)
        result = translator.translate_chunk(cues, "English", "Danish")
        self.assertEqual(result[0]["body"], ["Danish 0"])
        self.assertEqual(result[1]["body"], ["Danish 1"])
        self.assertEqual(len(server.calls), 1)

        duplicate = app.SubtitleTranslator(MarkerServer(duplicate=True))
        with self.assertRaises(RuntimeError):
            duplicate.translate_chunk(cues, "English", "Danish")

    def test_translategemma_prompt_keeps_context_outside_targets(self):
        server = TranslateGemmaMarkerServer()
        translator = app.SubtitleTranslator(server)
        cues, _ = app.parse_srt(RAW)
        result = translator.translate_chunk(
            cues[:1], "English", "Danish", before=(), after=cues[1:]
        )
        self.assertEqual(result[0]["body"], ["Oversat 0"])
        prompt, _, source, target = server.calls[0]
        self.assertIn("Context after", prompt)
        self.assertIn("Where are you?", prompt)
        self.assertIn("@@0@@", prompt)
        self.assertEqual((source, target), ("English", "Danish"))

    def test_translategemma_prompt_cache_is_explicit(self):
        for enabled in (False, True):
            server = translategemma.TranslateGemmaServer.__new__(
                translategemma.TranslateGemmaServer
            )
            server.api = "http://127.0.0.1:8768"
            server.key = "test-key"
            server.cache_prompt = enabled
            with mock.patch.object(
                translategemma, "_http_json", return_value={"content": "Hej"}
            ) as request:
                self.assertEqual(
                    server.translate("Hello", 32, "English", "Danish"), "Hej"
                )
            payload = request.call_args.kwargs["payload"]
            self.assertIs(payload["cache_prompt"], enabled)

    def test_checkpoint_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            output = root / "translated.srt"
            source.write_text(RAW, encoding="utf-8")
            args = argparse.Namespace(
                fresh=False,
                limit=1,
                block_cues=1,
                source_language="English",
                target_language="Danish",
                quiet=True,
            )
            translator = app.SubtitleTranslator(MarkerServer())
            first = app.process_srt_file(source, output, args, translator)
            self.assertEqual(first["next_cue"], 1)
            self.assertEqual(first["translated"], 1)
            saved = json.loads(
                Path(str(output) + ".progress.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("partial_output", saved)

            args.limit = 0
            second = app.process_srt_file(
                source, output, args, app.SubtitleTranslator(MarkerServer())
            )
            self.assertEqual(second["next_cue"], 2)
            self.assertEqual(second["translated"], 2)
            final, _ = app.parse_srt(output.read_text(encoding="utf-8"))
            self.assertEqual(final[0]["body"], ["Danish 0"])
            self.assertEqual(final[1]["body"], ["Danish 0"])

            restarted_output = root / "restarted.srt"
            restart_server = MarkerServer()
            restart_server.restart = mock.Mock()
            args.fresh = True
            args.restart_every = 1
            app.process_srt_file(
                source, restarted_output, args,
                app.SubtitleTranslator(restart_server)
            )
            self.assertEqual(restart_server.restart.call_count, 1)

    def test_zip_checkpoint_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_zip = root / "source.zip"
            output_zip = root / "translated.zip"
            with zipfile.ZipFile(source_zip, "w") as archive:
                archive.writestr("episode.srt", RAW)
                archive.writestr("notes.txt", "keep me")
            args = argparse.Namespace(
                fresh=False,
                limit=1,
                block_cues=1,
                source_language="English",
                target_language="Danish",
                quiet=True,
            )
            app.process_zip(
                source_zip, output_zip, args,
                app.SubtitleTranslator(MarkerServer())
            )
            saved = json.loads(
                Path(str(output_zip) + ".progress.json").read_text(encoding="utf-8")
            )
            self.assertEqual(saved["entry"], 0)
            self.assertEqual(saved["next_cue"], 1)
            self.assertIn("partial_output", saved)

            args.limit = 0
            app.process_zip(
                source_zip, output_zip, args,
                app.SubtitleTranslator(MarkerServer())
            )
            with zipfile.ZipFile(output_zip) as archive:
                final, _ = app.parse_srt(
                    archive.read("episode.srt").decode("utf-8")
                )
                self.assertEqual(archive.read("notes.txt"), b"keep me")
            self.assertEqual(len(final), 2)
            self.assertEqual(final[1]["body"], ["Danish 0"])

    def test_danish_detection(self):
        self.assertTrue(app.looks_danish(
            "Jeg går hjem, og du kommer med mig. Det er ikke langt."
        ))
        self.assertFalse(app.looks_danish("This is an English subtitle sentence."))


if __name__ == "__main__":
    unittest.main()
