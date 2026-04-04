import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

from config import (OLLAMA_MODEL, OLLAMA_TRANSLATE_BATCH, OLLAMA_URL,
                    PROMPT_VERSION, RETRY_DELAYS, TRANSLATE_BATCH, WHISPER_MODEL)
from prompt import _HOLOLIVE_NAMES
from ffmpeg_utils import extract_audio


def transcribe_with_whisper(audio_path, resume_dir, silence_secs, log, progress_cb,
                            vad_filter=True):
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
                    abs(data.get("silence_secs", -1) - silence_secs) < 0.5 and
                    data.get("vad_filter", True) == vad_filter):
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
whisper_model = {repr(WHISPER_MODEL)}

try:
    model = WhisperModel(whisper_model, device="cuda", compute_type="float16")
except Exception:
    try:
        model = WhisperModel(whisper_model, device="cpu", compute_type="int8")
    except Exception as e:
        print(json.dumps({{"error": str(e)}}))
        sys.exit(1)

segs, info = model.transcribe(
    audio_path,
    language="ja",
    beam_size=5,
    word_timestamps=True,
    vad_filter={vad_filter},
    vad_parameters={{"min_silence_duration_ms": 500}},
)

segments = []
total_duration = info.duration if hasattr(info, "duration") else 0
_last_reported_min = -1
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
        if elapsed_mins % 5 == 0 and elapsed_mins != _last_reported_min:
            _last_reported_min = elapsed_mins
            print(f"PROGRESS {{elapsed_mins}} {{total_mins}} {{pct}}", flush=True)

with open(output_path, "w", encoding="utf-8") as f:
    json.dump({{"whisper_version": {PROMPT_VERSION}, "silence_secs": silence_secs, "vad_filter": {vad_filter}, "segments": segments}}, f, ensure_ascii=False)
print(f"DONE {{len(segments)}}", flush=True)
"""

    helper_path = os.path.join(resume_dir, "_whisper_runner.py")
    os.makedirs(resume_dir, exist_ok=True)
    with open(helper_path, "w", encoding="utf-8") as f:
        f.write(helper_script)

    log(f"🎙️  Loading Whisper {WHISPER_MODEL} on CUDA…")

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

    entries = []
    total   = len(segments)

    for batch_start in range(0, total, TRANSLATE_BATCH):
        batch = segments[batch_start:batch_start + TRANSLATE_BATCH]

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

        done = min(batch_start + TRANSLATE_BATCH, total)
        progress_cb(done, total)
        log(f"📝  Translated {done}/{total} segments…")

    try:
        with open(trans_cache, "w", encoding="utf-8") as f:
            json.dump({"prompt_version": PROMPT_VERSION, "entries": entries}, f, ensure_ascii=False)
    except Exception:
        pass

    return entries


def check_ollama():
    """Check if Ollama is running and the model is available.
    Returns (ok, error_message)."""
    try:
        resp = urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=5)
        data = json.loads(resp.read().decode("utf-8"))
        models = [m.get("name", "") for m in data.get("models", [])]
        # Match with or without :latest tag
        found = any(OLLAMA_MODEL in m for m in models)
        if not found:
            return False, (f"Model '{OLLAMA_MODEL}' not found in Ollama.\n\n"
                           f"Run:\n  ollama pull {OLLAMA_MODEL}\nThen try again.")
        return True, ""
    except urllib.error.URLError:
        return False, ("Ollama is not running.\n\n"
                       "Install from ollama.com, start it, then run:\n"
                       f"  ollama pull {OLLAMA_MODEL}")
    except Exception as e:
        return False, f"Cannot connect to Ollama: {e}"


def _ollama_request(endpoint, payload, timeout=300):
    """Make a POST request to the Ollama API."""
    url = f"{OLLAMA_URL}{endpoint}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(resp.read().decode("utf-8"))


def _unload_ollama_model(log):
    """Tell Ollama to unload the model immediately to free VRAM."""
    try:
        _ollama_request("/api/generate", {
            "model": OLLAMA_MODEL,
            "keep_alive": 0,
        }, timeout=10)
        log(f"🧹  Unloaded {OLLAMA_MODEL} from VRAM")
    except Exception:
        pass  # non-critical


def translate_with_ollama(segments, title, resume_dir, log, progress_cb,
                          stop_event=None):
    """Batch-translate Japanese segments using Qwen3 via Ollama (fully local)."""
    if not segments:
        return []

    trans_cache = os.path.join(resume_dir, "translation.json")
    if os.path.exists(trans_cache):
        try:
            with open(trans_cache, "r", encoding="utf-8") as f:
                data = json.load(f)
            if (data.get("prompt_version") == PROMPT_VERSION
                    and data.get("translator") == "ollama"):
                saved = data.get("entries", [])
                log(f"⏭️  Translation: loaded from cache ({len(saved)} entries)")
                return saved
        except Exception:
            pass

    ctx = f"Video title: {title}." if title else ""

    system_prompt = f"""You are an expert translator for hololive VTuber streams.
{ctx}

{_HOLOLIVE_NAMES}

### TASK
Translate the following Japanese subtitle segments into natural, colloquial English.
Keep VTuber energy — translate 'yabe', 'sugoi', 'kawaii' idiomatically, not literally.
Never translate in isolation — use context from surrounding lines for pronouns, tense, and tone.
If a segment is already in English, keep it as-is.
Return ONLY valid JSON: an object with a single key "translations" containing an array of translated strings, one per input segment, in the same order.
No markdown, no extra keys — just {{"translations": ["...", "...", ...]}}"""

    entries = []
    total = len(segments)
    batch_size = OLLAMA_TRANSLATE_BATCH
    prev_context = []  # last ~10 translated lines for cross-batch continuity

    for batch_start in range(0, total, batch_size):
        if stop_event and stop_event.is_set():
            _unload_ollama_model(log)
            return []

        batch = segments[batch_start:batch_start + batch_size]

        lines = []
        line_map = []
        for seg_idx, s in enumerate(batch):
            parts = s["text"].split("\n")
            for line_idx, part in enumerate(parts):
                lines.append(part.strip())
                line_map.append((seg_idx, line_idx))

        # Build user message with rolling context from previous batch
        parts_msg = []
        if prev_context:
            parts_msg.append(
                "For context, here are the last lines from the previous batch "
                "(already translated — do NOT include these in your output):\n"
                + json.dumps(prev_context, ensure_ascii=False))
        parts_msg.append("Translate these segments:\n"
                         + json.dumps(lines, ensure_ascii=False))
        user_msg = "\n\n".join(parts_msg) + "\n/no_think"

        translations = lines  # fallback to original
        for attempt in range(3):
            try:
                resp = _ollama_request("/api/chat", {
                    "model": OLLAMA_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": 0.4},
                }, timeout=600)

                content = resp.get("message", {}).get("content", "")
                raw = content.strip()
                raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw).strip()
                parsed = json.loads(raw)

                if isinstance(parsed, dict) and "translations" in parsed:
                    translations = parsed["translations"]
                elif isinstance(parsed, list):
                    translations = parsed
                else:
                    raise ValueError("Expected JSON with 'translations' array")

                if not isinstance(translations, list):
                    raise ValueError("translations is not a list")
                break
            except Exception as e:
                if attempt < 2:
                    log(f"⚠️  Translation batch retry {attempt+1}: {str(e)[:80]}")
                    time.sleep(5)
                else:
                    log(f"⚠️  Translation batch failed: {str(e)[:100]} — using original Japanese")
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
                "end_ms": seg["end_ms"],
                "text": translated_text,
            })

        # Keep last 10 translated lines as rolling context for the next batch
        recent = [e["text"] for e in entries[-10:]]
        prev_context = recent

        done = min(batch_start + batch_size, total)
        progress_cb(done, total)
        log(f"📝  Translated {done}/{total} segments…")

    # Unload model to free VRAM
    _unload_ollama_model(log)

    try:
        with open(trans_cache, "w", encoding="utf-8") as f:
            json.dump({"prompt_version": PROMPT_VERSION,
                       "translator": "ollama",
                       "entries": entries}, f, ensure_ascii=False)
    except Exception:
        pass

    return entries


def transcribe_local(source_path, task, api_key, title,
                     skip_secs, resume_dir, tmp_dir, model, log, progress_cb,
                     stop_event=None, translator="gemini", vad_filter=True):
    """Local pipeline: faster-whisper transcription + Gemini or Ollama translation."""
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
                                       lambda d, t: progress_cb(int(d/t*50) if t > 0 else 0, 100),
                                       vad_filter=vad_filter)
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
        if translator == "ollama":
            log(f"🌐  Translating {len(segments)} segments with {OLLAMA_MODEL}…")
            entries = translate_with_ollama(
                segments, title, resume_dir, log,
                lambda d, t: progress_cb(50 + int(d/t*50) if t > 0 else 50, 100),
                stop_event=stop_event)
        else:
            log(f"🌐  Translating {len(segments)} segments with Gemini…")
            entries = translate_with_gemini(
                segments, title, api_key, model, resume_dir, log,
                lambda d, t: progress_cb(50 + int(d/t*50) if t > 0 else 50, 100))
    else:
        entries = segments
        progress_cb(100, 100)

    return entries
