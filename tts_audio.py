"""Conservative alignment-based cuts and verified, atomic audio exports."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile

from pydub import AudioSegment
from pydub.silence import detect_leading_silence
from tts_quality import normalize_for_compare


class AudioProcessingError(RuntimeError):
    pass


def _run(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioProcessingError(f"Audiowerkzeug nicht ausführbar: {exc}") from exc
    if result.returncode:
        raise AudioProcessingError(f"Audiowerkzeug fehlgeschlagen: {result.stderr[-1200:]}")
    return result


def silence_threshold(audio):
    return min(-55.0, audio.dBFS - 35) if math.isfinite(audio.dBFS) else -75.0


def load_audio(path):
    with open(path, "rb") as source:
        return AudioSegment.from_file(source)


def trim_aligned_word(input_path, output_path, target, lead_in, alignment,
                      pad_start_ms=80, pad_end_ms=180):
    """Require a complete, ordered alignment; never guess the last pause."""
    audio = load_audio(input_path)
    if not len(audio) or not math.isfinite(audio.dBFS):
        raise AudioProcessingError("Leeres/stummes Audio kann nicht geschnitten werden")
    if not isinstance(alignment, dict):
        raise AudioProcessingError("Keine Wortzeitstempel vorhanden; Original zur Prüfung behalten")
    chars = alignment.get("characters")
    starts = alignment.get("character_start_times_seconds")
    ends = alignment.get("character_end_times_seconds")
    if (not isinstance(chars, list) or not isinstance(starts, list) or not isinstance(ends, list)
            or not chars or len(chars) != len(starts) or len(chars) != len(ends)
            or any(not isinstance(c, str) or len(c) != 1 for c in chars)):
        raise AudioProcessingError("Unvollständiges Zeichen-Alignment")
    text = "".join(chars)
    # Mask non-spoken provider tags without changing character offsets.
    clean = re.sub(r"<[^>]*>|\[[^\]]*\]", lambda m: " " * len(m.group()), text)
    words = list(re.finditer(r"\w+", clean))
    prefix_count = len(re.findall(r"\w+", lead_in))
    if len(words) <= prefix_count:
        raise AudioProcessingError("Zielwort fehlt im Alignment")
    prefix_end = words[prefix_count - 1].end()
    target_start = words[prefix_count].start()
    target_end = words[-1].end()
    if (normalize_for_compare(clean[:prefix_end]) != normalize_for_compare(lead_in)
            or normalize_for_compare(clean[target_start:target_end]) != normalize_for_compare(target)):
        raise AudioProcessingError("Alignment enthält nicht eindeutig Einleitung und Zieltext")
    previous_start = previous_end = -1.0
    for word in words:
        for i in range(word.start(), word.end()):
            start, end = starts[i], ends[i]
            if (type(start) not in (float, int) or type(end) not in (float, int)
                    or not math.isfinite(start) or not math.isfinite(end)
                    or start < 0 or end < start or start < previous_start - .005
                    or end < previous_end - .005 or end > len(audio) / 1000 + .05):
                raise AudioProcessingError("Unplausible oder ungeordnete Wortzeitstempel")
            previous_start, previous_end = start, end
    onset = round(starts[target_start] * 1000)
    offset = round(ends[target_end - 1] * 1000)
    prefix_offset = round(ends[prefix_end - 1] * 1000)
    if offset <= onset or onset - prefix_offset < 30:
        raise AudioProcessingError("Keine sichere Trennung zwischen Einleitung und Zielwort")
    # Search outward for quiet boundaries, constrained by the known lead-in.
    threshold = silence_threshold(audio)
    start = max(prefix_offset + 10, onset - pad_start_ms)
    end = min(len(audio), offset + pad_end_ms)
    while start > max(prefix_offset + 10, onset - 300) and audio[start:start + 10].dBFS > threshold:
        start -= 5
    while end < min(len(audio), offset + 400) and audio[end - 10:end].dBFS > threshold:
        end = min(len(audio), end + 5)
    if (start >= onset or end <= onset or audio[start:start + 10].dBFS > threshold
            or audio[end - 10:end].dBFS > threshold):
        raise AudioProcessingError("Schnittgrenze liegt in aktivem Signal; manuelle Prüfung erforderlich")
    audio[start:end].export(output_path, format="wav").close()
    return {"method": "provider_alignment", "start_ms": start, "end_ms": end,
            "target_start_ms": onset, "target_end_ms": offset,
            "input_duration_ms": len(audio), "output_duration_ms": end - start}


def measure_loudness(path, ffmpeg):
    result = _run([ffmpeg, "-nostdin", "-hide_banner", "-i", str(path),
                   "-af", "loudnorm=I=-16:TP=-2:LRA=11:print_format=json",
                   "-f", "null", "-"])
    match = re.search(r'\{\s*"input_i".*?\}', result.stderr, re.S)
    if not match:
        raise AudioProcessingError("FFmpeg lieferte keine Lautheitsmessung")
    try:
        data = json.loads(match.group())
        result = {key: float(data[key]) for key in
                  ("input_i", "input_tp", "input_lra", "input_thresh")}
    except (ValueError, KeyError, TypeError) as exc:
        raise AudioProcessingError("Ungültige Lautheitsmessung") from exc
    if not all(math.isfinite(n) for n in result.values()):
        raise AudioProcessingError("Lautheit nicht messbar (zu kurz oder stumm); manuelle Prüfung erforderlich")
    return result


def validate_export(path, *, ffmpeg, ffprobe, export_format, sample_rate,
                    expected_duration_ms, target_i=-16, target_tp=-1.5,
                    loudness_tolerance=2.0, check_loudness=True):
    """Probe container/codec, decode the WHOLE file, then measure decoded peaks."""
    if export_format not in {"opus", "mp3"} or Path(path).suffix.lower() != "." + export_format:
        raise AudioProcessingError("Dateiendung und Exportformat stimmen nicht überein")
    if not Path(path).is_file() or Path(path).stat().st_size == 0:
        raise AudioProcessingError("Exportdatei fehlt oder ist leer")
    probe = _run([ffprobe, "-v", "error", "-show_entries",
                  "stream=codec_name,sample_rate,channels,codec_type:format=format_name,duration",
                  "-of", "json", str(path)])
    try:
        data = json.loads(probe.stdout)
        streams = data["streams"]
        stream = streams[0]
        expected_container = "ogg" if export_format == "opus" else "mp3"
        if (len(streams) != 1 or stream["codec_type"] != "audio"
                or stream["codec_name"] != export_format or int(stream["channels"]) != 1
                or int(stream["sample_rate"]) != sample_rate
                or expected_container not in data["format"]["format_name"].split(",")):
            raise AudioProcessingError("Codec, Container, Kanalzahl oder Samplerate entsprechen nicht dem Exportprofil")
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise AudioProcessingError("Export-Metadaten nicht prüfbar") from exc
    # PCM decoding also detects damaged packets beyond the initial probe window.
    with tempfile.TemporaryDirectory(prefix="tts-verify-") as folder:
        decoded = Path(folder) / "decoded.wav"
        _run([ffmpeg, "-nostdin", "-v", "error", "-xerror", "-i", str(path),
              "-map", "0:a:0", "-c:a", "pcm_s16le", str(decoded)])
        audio = load_audio(decoded)
        if not len(audio) or not math.isfinite(audio.dBFS):
            raise AudioProcessingError("Export enthält kein hörbares Signal")
        if abs(len(audio) - expected_duration_ms) > 80:
            raise AudioProcessingError("Exportdauer weicht vom bearbeiteten Audio ab")
    metrics = measure_loudness(path, ffmpeg) if check_loudness else None
    if metrics:
        if metrics["input_tp"] > target_tp:
            raise AudioProcessingError(f"Export überschreitet Peak-Limit: {metrics['input_tp']:.2f} dBTP")
        if abs(metrics["input_i"] - target_i) > loudness_tolerance:
            raise AudioProcessingError(f"Export verfehlt Lautheitsziel: {metrics['input_i']:.2f} LUFS")
    return {"codec": stream["codec_name"], "container": expected_container,
            "sample_rate": sample_rate, "channels": 1, "duration_ms": len(audio),
            "loudness": metrics}


def export_audio(input_path, output_path, *, ffmpeg, ffprobe, export_format="opus",
                 sample_rate=48000, bitrate="64k", target_i=-16, target_tp=-1.5,
                 process=True, keep_start_ms=100, keep_end_ms=250,
                 min_edge_silence_ms=300, max_edge_trim_ms=2500,
                 fade_in_ms=20, fade_out_ms=60):
    """Prepare PCM, normalize in two passes, verify encoded output, atomic replace.

    No fallback without normalization. On failure an existing destination survives.
    process=False still encodes and validates the actual target format.
    """
    if export_format not in {"opus", "mp3"} or Path(output_path).suffix != "." + export_format:
        raise AudioProcessingError("Nicht unterstütztes oder widersprüchliches Exportformat")
    audio = load_audio(input_path).set_channels(1)
    if not len(audio) or not math.isfinite(audio.dBFS):
        raise AudioProcessingError("Leeres/stummes Audio wird nicht exportiert")
    original_ms = len(audio)
    cut_start = cut_end = 0
    if process:
        threshold = silence_threshold(audio)
        leading = detect_leading_silence(audio, threshold, chunk_size=10)
        trailing = detect_leading_silence(audio.reverse(), threshold, chunk_size=10)
        cut_start = min(max_edge_trim_ms, max(0, leading - keep_start_ms)) if leading >= min_edge_silence_ms else 0
        cut_end = min(max_edge_trim_ms, max(0, trailing - keep_end_ms)) if trailing >= min_edge_silence_ms else 0
        audio = audio[cut_start:len(audio) - cut_end]
        # Add missing quiet padding instead of fading phonemes at active edges.
        add_start = max(0, keep_start_ms - (leading - cut_start))
        add_end = max(0, keep_end_ms - (trailing - cut_end))
        audio = (AudioSegment.silent(add_start, frame_rate=audio.frame_rate) + audio
                 + AudioSegment.silent(add_end, frame_rate=audio.frame_rate))
        audio = audio.fade_in(min(fade_in_ms, keep_start_ms)).fade_out(min(fade_out_ms, keep_end_ms))
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".export-", dir=output.parent) as folder:
        wav = Path(folder) / "prepared.wav"
        candidate = Path(folder) / ("encoded." + export_format)
        audio.export(wav, format="wav").close()
        filters, measured = [], None
        if process:
            measured = measure_loudness(wav, ffmpeg)
            # Codec headroom; final true peak is checked after lossy encoding.
            filters = ["-af", f"loudnorm=I={target_i}:TP={target_tp - .8}:LRA=11:"
                       f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
                       f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:linear=true"]
        codec = ["-c:a", "libopus", "-b:a", bitrate, "-vbr", "on"] if export_format == "opus" else ["-c:a", "libmp3lame", "-b:a", "128k"]
        _run([ffmpeg, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
              "-i", str(wav), "-map", "0:a:0", "-map_metadata", "-1", *filters,
              "-ar", str(sample_rate), "-ac", "1", *codec, str(candidate)])
        verified = validate_export(candidate, ffmpeg=ffmpeg, ffprobe=ffprobe,
                                   export_format=export_format, sample_rate=sample_rate,
                                   expected_duration_ms=len(audio), target_i=target_i,
                                   target_tp=target_tp, check_loudness=process)
        os.replace(candidate, output)
    return {**verified, "input_duration_ms": original_ms, "cut_start_ms": cut_start,
            "cut_end_ms": cut_end, "normalization": "two_pass" if process else "disabled",
            "input_loudness": measured}


def safe_filename(filename):
    """One portable basename; no paths or Windows reserved device names."""
    if (not filename or filename != filename.strip() or filename in {".", ".."}
            or re.search(r'[\\/:*?"<>|\x00-\x1f]', filename)
            or filename.endswith((".", " "))
            or re.match(r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", filename, re.I)):
        raise AudioProcessingError("Ungültiger Dateiname: nur ein einfacher Dateiname ohne Pfad ist erlaubt")
    return filename
