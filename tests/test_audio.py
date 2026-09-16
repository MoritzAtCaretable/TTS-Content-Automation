"""Offline audio tests, including real local FFmpeg encode/decode verification."""
import base64
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pydub import AudioSegment
from pydub.generators import Sine

import sheets_to_elevenlabs_qc_local as pipeline
from tts_audio import AudioProcessingError, export_audio, trim_aligned_word, validate_export, safe_filename

FFMPEG, FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")


def alignment(target="Banane"):
    prefix = "Das Wort heißt: "
    text = prefix + target
    starts = [i / len(prefix) for i in range(len(prefix))]
    ends = [(i + 1) / len(prefix) for i in range(len(prefix))]
    starts += [1.6 + .6 * i / len(target) for i in range(len(target))]
    ends += [1.6 + .6 * (i + 1) / len(target) for i in range(len(target))]
    return {"characters": list(text), "character_start_times_seconds": starts,
            "character_end_times_seconds": ends}


class CutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.raw, self.out = self.root / "raw.wav", self.root / "cut.wav"
        self.tone = Sine(400, sample_rate=24000).to_audio_segment(1000).apply_gain(-15)
        self.silent = lambda n: AudioSegment.silent(n, frame_rate=24000)

    def test_alignment_selects_word_not_trailing_artifact(self):
        (self.tone + self.silent(600) + self.tone[:600] + self.silent(600) + self.tone[:300]).export(self.raw, format="wav").close()
        result = trim_aligned_word(self.raw, self.out, "Banane", "Das Wort heißt:", alignment())
        self.assertEqual(result["target_start_ms"], 1600)
        self.assertEqual(result["target_end_ms"], 2200)
        self.assertGreaterEqual(len(AudioSegment.from_wav(io.BytesIO(self.out.read_bytes()))), 850)
        self.assertLess(result["end_ms"], 2800)

    def test_quiet_suffix_is_preserved_and_boundary_extended(self):
        quiet = Sine(700, sample_rate=24000).to_audio_segment(350).apply_gain(-42)
        (self.tone + self.silent(600) + self.tone[:600] + quiet + self.silent(500)).export(self.raw, format="wav").close()
        result = trim_aligned_word(self.raw, self.out, "Banane", "Das Wort heißt:", alignment())
        self.assertGreaterEqual(result["end_ms"], 2560)

    def test_bad_or_ambiguous_alignment_does_not_write(self):
        (self.tone + self.silent(600) + self.tone).export(self.raw, format="wav").close()
        cases = [None, {}, alignment("Apfel"), alignment("Banane Banane")]
        for mutate in (lambda a: a["character_end_times_seconds"].pop(),
                       lambda a: a["character_start_times_seconds"].__setitem__(-1, float("nan")),
                       lambda a: a["character_end_times_seconds"].__setitem__(-1, 10),
                       lambda a: a["character_start_times_seconds"].__setitem__(-1, -.2)):
            data = alignment(); mutate(data); cases.append(data)
        for data in cases:
            with self.subTest(data=data):
                with self.assertRaises(AudioProcessingError):
                    trim_aligned_word(self.raw, self.out, "Banane", "Das Wort heißt:", data)
                self.assertFalse(self.out.exists())

    def test_active_end_without_safe_boundary_is_rejected(self):
        (self.tone + self.silent(600) + self.tone).export(self.raw, format="wav").close()
        with self.assertRaises(AudioProcessingError):
            trim_aligned_word(self.raw, self.out, "Banane", "Das Wort heißt:", alignment())


@unittest.skipUnless(FFMPEG and FFPROBE, "Local FFmpeg and ffprobe required")
class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.raw = self.root / "source.wav"
        Sine(400, sample_rate=24000).to_audio_segment(1000).apply_gain(-18).export(self.raw, format="wav").close()

    def export(self, ext="opus", **kwargs):
        path = self.root / ("final." + ext)
        result = export_audio(self.raw, path, ffmpeg=FFMPEG, ffprobe=FFPROBE, export_format=ext, **kwargs)
        return path, result

    def test_real_opus_and_mp3_exports_meet_profile(self):
        for ext in ("opus", "mp3"):
            with self.subTest(ext=ext):
                path, result = self.export(ext)
                self.assertTrue(path.exists())
                self.assertEqual(result["codec"], ext)
                self.assertEqual(result["sample_rate"], 48000)
                self.assertEqual(result["channels"], 1)
                self.assertLessEqual(result["loudness"]["input_tp"], -1.5)
                self.assertLessEqual(abs(result["loudness"]["input_i"] + 16), 2)
                self.assertFalse(list(self.root.glob(".export-*")))

    def test_disabled_processing_still_encodes_correct_format(self):
        path, result = self.export(process=False)
        self.assertEqual(path.read_bytes()[:4], b"OggS")
        self.assertEqual(result["normalization"], "disabled")

    def test_failed_encoder_preserves_destination_and_cleans_temp(self):
        path = self.root / "final.opus"; path.write_bytes(b"previous accepted audio")
        original = subprocess.run
        def fail_encoder(cmd, **kw):
            if "libopus" in cmd:
                return SimpleNamespace(returncode=1, stderr="simulated encoder failure", stdout="")
            return original(cmd, **kw)
        with patch("tts_audio.subprocess.run", side_effect=fail_encoder):
            with self.assertRaises(AudioProcessingError): self.export()
        self.assertEqual(path.read_bytes(), b"previous accepted audio")
        self.assertFalse(list(self.root.glob(".export-*")))

    def test_failed_post_encode_validation_preserves_destination(self):
        path = self.root / "final.opus"; path.write_bytes(b"previous")
        with patch("tts_audio.validate_export", side_effect=AudioProcessingError("peak too high")):
            with self.assertRaises(AudioProcessingError): self.export()
        self.assertEqual(path.read_bytes(), b"previous")

    def test_mislabeled_wav_and_truncated_opus_rejected(self):
        fake = self.root / "fake.opus"; shutil.copyfile(self.raw, fake)
        with self.assertRaises(AudioProcessingError):
            validate_export(fake, ffmpeg=FFMPEG, ffprobe=FFPROBE, export_format="opus", sample_rate=48000, expected_duration_ms=1000)
        path, result = self.export()
        path.write_bytes(path.read_bytes()[:len(path.read_bytes()) // 2])
        with self.assertRaises(AudioProcessingError):
            validate_export(path, ffmpeg=FFMPEG, ffprobe=FFPROBE, export_format="opus", sample_rate=48000, expected_duration_ms=result["duration_ms"])

    def test_silent_input_rejected(self):
        AudioSegment.silent(1000).export(self.raw, format="wav").close()
        with self.assertRaises(AudioProcessingError): self.export()


class ProviderAndPipelineTests(unittest.TestCase):
    def test_word_request_uses_timestamps_and_model_specific_pause(self):
        response = Mock(status_code=200)
        response.json.return_value = {"audio_base64": base64.b64encode(b"\x01\x00" * 24000).decode(),
                                     "normalized_alignment": alignment()}
        for model in ("eleven_v3", "eleven_turbo_v2_5"):
            with self.subTest(model=model), tempfile.TemporaryDirectory() as tmp, \
                    patch.object(pipeline, "ELEVENLABS_MODEL", model), \
                    patch.object(pipeline, "_elevenlabs_pcm_failed", False), \
                    patch.object(pipeline.requests, "post", return_value=response) as post:
                raw = pipeline.text_to_speech("Banane", str(Path(tmp) / "raw"), single_word_mode=True)
                self.assertTrue(Path(raw + ".alignment.json").exists())
                args = post.call_args
                self.assertIn("/with-timestamps?", args.args[0])
                self.assertIn("timeout", args.kwargs)
                self.assertEqual("<break" in args.kwargs["json"]["text"], model != "eleven_v3")

    def test_pcm_fallback_keeps_timestamp_endpoint(self):
        refused = Mock(status_code=400, text="output_format pcm not allowed")
        accepted = Mock(status_code=200)
        accepted.json.return_value = {"audio_base64": base64.b64encode(b"mock mp3").decode(), "alignment": alignment()}
        with tempfile.TemporaryDirectory() as tmp, patch.object(pipeline, "_elevenlabs_pcm_failed", False), \
                patch.object(pipeline.requests, "post", side_effect=[refused, accepted]) as post:
            path = pipeline.text_to_speech("Banane", str(Path(tmp) / "raw"), single_word_mode=True)
            self.assertTrue(path.endswith(".mp3"))
            self.assertTrue(all("/with-timestamps?" in c.args[0] for c in post.call_args_list))

    def test_invalid_filenames_rejected(self):
        for name in ("../bad.opus", "/tmp/bad.opus", "C:\\bad.opus", "CON.opus", "bad?.opus", "bad\x00.opus"):
            with self.subTest(name=name), self.assertRaises(AudioProcessingError): safe_filename(name)
        self.assertEqual(safe_filename("Wäsche_1.opus"), "Wäsche_1.opus")

    def test_cut_or_export_failure_preserves_old_final_and_raw(self):
        for word_mode in (True, False):
            with self.subTest(word_mode=word_mode), tempfile.TemporaryDirectory() as tmp, \
                    patch.object(pipeline, "OUTPUT_DIR", tmp), \
                    patch.object(pipeline, "REVIEW_DATA_FILE", str(Path(tmp) / "review.json")), \
                    patch.object(pipeline, "write_back"), \
                    patch.object(pipeline, "text_to_speech") as tts, \
                    patch.object(pipeline, "trim_to_word", side_effect=AudioProcessingError("missing alignment")), \
                    patch.object(pipeline, "postprocess_audio", side_effect=AudioProcessingError("export failed")), \
                    patch.object(pipeline, "quality_check") as qc:
                old = Path(tmp) / "existing.opus"; old.write_bytes(b"previous accepted")
                def generate(text, path, **kw):
                    raw = Path(path + ".wav"); raw.write_bytes(b"RIFF preserved original")
                    return str(raw)
                tts.side_effect = generate
                row = {"_row": 2, "text": "Banane", "filename": old.name, "mode": "Einzelwort" if word_mode else "Normal"}
                counts = dict(passed=0, review=0, failed=0, skipped=0)
                pipeline.process_row(row, None, {}, None, None, counts)
                tts.assert_called_once(); qc.assert_not_called()
                self.assertEqual(old.read_bytes(), b"previous accepted")
                self.assertEqual(row["status"], "review needed")
                self.assertTrue(Path(row["_audio_path"]).exists())
                self.assertTrue(row["_audio_path"].endswith(".wav"))
                self.assertTrue(list((Path(tmp) / ".sources").glob("*/attempts.json")))

    def test_no_new_audio_does_not_show_old_file_in_review(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(pipeline, "OUTPUT_DIR", tmp), \
                patch.object(pipeline, "REVIEW_DATA_FILE", str(Path(tmp) / "review.json")), \
                patch.object(pipeline, "write_back"), patch.object(pipeline, "text_to_speech", return_value=""):
            (Path(tmp) / "old.opus").write_bytes(b"old")
            row = {"_row": 2, "text": "Banane", "filename": "old.opus"}
            pipeline.process_row(row, None, {}, None, None, dict(passed=0, review=0, failed=0, skipped=0))
            page, _ = pipeline.generate_review_html(str(Path(tmp) / "review.html"))
            self.assertIn("Keine Audio-Datei gefunden", Path(page).read_text())
            self.assertNotIn('data-src="file:', Path(page).read_text())


if __name__ == "__main__":
    unittest.main()
