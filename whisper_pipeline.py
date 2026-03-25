import json
import os
import re
import subprocess
import sys
import time

from config import PROMPT_VERSION, RETRY_DELAYS
from prompt import _HOLOLIVE_NAMES
from ffmpeg_utils import extract_audio


def transcribe_with_whisper(audio_path, resume_dir, silence_secs, log, progress_cb):
    """Run faster-whisper in a subprocess. Returns list of {start_ms, end_ms, text}
    with timestamps in original video time (audio is extracted from t=0 with the
    intro silenced, so no offset is needed)."""
    whisper_cache = os.path.join(resume_dir, "whisper_transcript.json")
    if os.path.exists(whisper_cache):
        try:
            with open(whisper_cache, "r", encoding="utf-8") as f:
                data = json.load(f)
            if (isinstance(data, dict) and
                    data.get("whisper_version") == PROMPT_VERSION and
                    abs(data.get("silence_secs", -1) - silence_secs) < 0.5):
                segments = data.get("segments", [])
                log(f"⏭️  Whisper: loaded from cache ({len(segments)} segments)")
                return segments
            elif isinstance(data, dict):
                log("🔄  Whisper cache outdated — re-transcribing…")
                os.remove(whisper_cache)
            elif isinstance(data, list):
                log("🔄  Whisper cache from old version — re-transcribing…")
                os.remove(whisper_cache)
        except Exception:
            pass

    # Write a small helper script that runs Whisper and writes output to JSON.
    # Running in a subprocess isolates CUDA cleanup from the GUI process.
    helper_script = f"""
import sys, json
try:
    from faster_whisper import WhisperModel
except ImportError:
    print(json.dumps({{"error": "faster-whisper not installed"}}))
    sys.exit(1)

audio_path    = {repr(audio_path)}
output_path   = {repr(whisper_cache)}
silence_secs  = {silence_secs}

try:
    model = WhisperModel("large-v3", device="cuda", compute_type="float16")
except Exception:
    try:
        model = WhisperModel("large-v3", device="cpu", compute_type="int8")
    except Exception as e:
        print(json.dumps({{"error": str(e)}}))
        sys.exit(1)

segs, info = model.transcribe(
    audio_path,
    language="ja",
    beam_size=5,
    word_timestamps=True,
    vad_filter=True,
    vad_parameters={{"min_silence_duration_ms": 500}},
)

segments = []
total_duration = info.duration if hasattr(info, "duration") else 0
for seg in segs:
    start_ms = int(seg.start * 1000)
    end_ms   = int(seg.end   * 1000)
    text = seg.text.strip()
    if text:
        segments.append({{"start_ms": start_ms, "end_ms": end_ms, "text": text}})
    if total_duration > 0:
        pct = min(100, int(seg.end / total_duration * 100))
        elapsed_mins = int(seg.end / 60)
        total_mins   = int(total_duration / 60)
        if elapsed_mins % 5 == 0:
            print(f"PROGRESS {{elapsed_mins}} {{total_mins}} {{pct}}", flush=True)

with open(output_path, "w", encoding="utf-8") as f:
    json.dump({{"whisper_version": {PROMPT_VERSION}, "silence_secs": silence_secs, "segments": segments}}, f, ensure_ascii=False)
print(f"DONE {{len(segments)}}", flush=True)
"""

    helper_path = os.path.join(resume_dir, "_whisper_runner.py")
    os.makedirs(resume_dir, exist_ok=True)
    with open(helper_path, "w", encoding="utf-8") as f:
        f.write(helper_script)

    log("🎙️  Loading Whisper large-v3 on CUDA…")

    try:
        proc = subprocess.Popen(
            [sys.executable, helper_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        last_logged = [-1]
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("PROGRESS"):
                parts = line.split()
                if len(parts) == 4:
                    elapsed_mins, total_mins, pct = int(parts[1]), int(parts[2]), int(parts[3])
                    progress_cb(int(elapsed_mins * 60), int(total_mins * 60))
                    if elapsed_mins > last_logged[0]:
                        last_logged[0] = elapsed_mins
                        log(f"🎤  Transcribing… {elapsed_mins}/{total_mins} min ({pct}%)")
            elif line.startswith("DONE"):
                count = line.split()[1] if len(line.split()) > 1 else "?"
                log(f"✅  Whisper: {count} segments transcribed")

        proc.wait()

        if not os.path.exists(whisper_cache):
            stderr = proc.stderr.read()
            log(f"❌  Whisper failed: {stderr[-300:]}")
            return []

    except Exception as e:
        log(f"❌  Whisper subprocess error: {e}")
        return []
    finally:
        try:
            os.remove(helper_path)
        except Exception:
            pass

    try:
        with open(whisper_cache, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            segments = data.get("segments", [])
        else:
            segments = data
        return segments
    except Exception as e:
        log(f"❌  Could not read Whisper output: {e}")
        return []


def translate_with_gemini(segments, title, api_key, model, resume_dir, log, progress_cb):
    """Batch-translate Japanese segments using Gemini text-only API."""
    from google import genai
    from google.genai import types

    if not segments:
        return []

    trans_cache = os.path.join(resume_dir, "translation.json")
    if os.path.exists(trans_cache):
        try:
            with open(trans_cache, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("prompt_version") == PROMPT_VERSION:
                saved = data.get("entries", [])
                log(f"⏭️  Translation: loaded from cache ({len(saved)} entries)")
                return saved
        except Exception:
            pass

    client = genai.Client(api_key=api_key)
    ctx    = f"Video title: {title}." if title else ""

    system_prompt = f"""You are an expert translator for hololive VTuber streams.
{ctx}

{_HOLOLIVE_NAMES}

### TASK
Translate the following Japanese subtitle segments into natural, colloquial English.
Keep VTuber energy — translate 'yabe', 'sugoi', 'kawaii' idiomatically, not literally.
Never translate in isolation — use context from surrounding lines for pronouns, tense, and tone.
If a segment is already in English, keep it as-is.
Return ONLY a valid JSON array with one translated string per input segment, in the same order.
No markdown, no extra keys — just a JSON array of strings."""

    BATCH = 200
    entries = []
    total   = len(segments)

    for batch_start in range(0, total, BATCH):
        batch = segments[batch_start:batch_start + BATCH]

        lines    = []
        line_map = []
        for seg_idx, s in enumerate(batch):
            parts = s["text"].split("\n")
            for line_idx, part in enumerate(parts):
                lines.append(part.strip())
                line_map.append((seg_idx, line_idx))

        user_msg = "Translate these segments:\n" + json.dumps(lines, ensure_ascii=False)

        translations = lines
        for attempt in range(5):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=[user_msg],
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        max_output_tokens=8192,
                        response_mime_type="application/json",
                        thinking_config=types.ThinkingConfig(thinking_budget=0),
                    ),
                )
                raw = response.text.strip()
                raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw).strip()
                translations = json.loads(raw)
                if not isinstance(translations, list):
                    raise ValueError("Expected JSON array")
                break
            except Exception as e:
                err = str(e)
                is_rate = "429" in err or "quota" in err.lower() or "rate" in err.lower()
                if is_rate and attempt < 4:
                    wait = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS)-1)]
                    log(f"⏳  Rate limited on translation batch — waiting {wait}s…")
                    time.sleep(wait)
                elif attempt < 4:
                    time.sleep(10)
                else:
                    log(f"⚠️  Translation batch failed: {err[:100]} — using original Japanese")
                    translations = lines

        seg_parts: list[list[str]] = [[] for _ in range(len(batch))]
        for flat_idx, translated in enumerate(translations):
            if flat_idx < len(line_map):
                seg_idx, _ = line_map[flat_idx]
                seg_parts[seg_idx].append(str(translated).strip())

        for j, seg in enumerate(batch):
            if seg_parts[j]:
                translated_text = "\n".join(seg_parts[j])
            else:
                translated_text = seg["text"]
            entries.append({
                "start_ms": seg["start_ms"],
                "end_ms":   seg["end_ms"],
                "text":     translated_text,
            })

        done = min(batch_start + BATCH, total)
        progress_cb(done, total)
        log(f"📝  Translated {done}/{total} segments…")

    try:
        with open(trans_cache, "w", encoding="utf-8") as f:
            json.dump({"prompt_version": PROMPT_VERSION, "entries": entries}, f, ensure_ascii=False)
    except Exception:
        pass

    return entries


def transcribe_local(source_path, task, api_key, title,
                     skip_secs, resume_dir, tmp_dir, model, log, progress_cb,
                     stop_event=None):
    """Local pipeline: faster-whisper transcription + Gemini text translation."""
    os.makedirs(resume_dir, exist_ok=True)

    # 1. Extract full audio from t=0 but silence the intro.
    #    This keeps the original timeline intact (no offset math needed) while
    #    preventing the intro music from confusing VAD/Whisper.  The old approach
    #    of seeking past the intro changed the audio context Whisper saw, which
    #    caused systematic timestamp drift.
    audio_path = os.path.join(tmp_dir, "audio.wav")
    log("🎵  Extracting audio…")
    if not extract_audio(source_path, audio_path, log, silence_secs=skip_secs):
        return []

    if stop_event and stop_event.is_set():
        return []

    # 2. Whisper transcription (timestamps are already in video time)
    log("🎤  Starting Whisper transcription…")
    segments = transcribe_with_whisper(audio_path, resume_dir, skip_secs, log,
                                       lambda d, t: progress_cb(int(d/t*50) if t else 0, 100))
    if not segments:
        return []

    # 3. Drop any segments from the silenced intro (safety measure —
    #    VAD should ignore silence, but filter just in case).
    if skip_secs > 0:
        skip_ms = int(skip_secs * 1000)
        before  = len(segments)
        segments = [s for s in segments if s["start_ms"] >= skip_ms]
        dropped = before - len(segments)
        if dropped:
            log(f"⏩  Skipped {dropped} segments before {skip_secs:.0f}s mark")

    if stop_event and stop_event.is_set():
        return []

    # 4. Translate or return as-is
    if task == "translate":
        log(f"🌐  Translating {len(segments)} segments with Gemini…")
        entries = translate_with_gemini(segments, title, api_key, model, resume_dir, log,
                                        lambda d, t: progress_cb(50 + int(d/t*50) if t else 50, 100))
    else:
        entries = segments
        progress_cb(100, 100)

    return entries
