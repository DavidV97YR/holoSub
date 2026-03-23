"""
holoSub — auto subtitle generator for hololive VODs & YouTube videos
powered by Google Gemini

requirements:
    pip install google-genai yt-dlp static-ffmpeg

No separate FFmpeg install needed — static-ffmpeg bundles its own binary.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import threading
import os
import re
import math
import tempfile
import shutil
import json
import time
import concurrent.futures

# ── colour palette ───────────────────────────────────────────────────────────
BG        = "#0d0d14"
CARD      = "#16161f"
CARD2     = "#1e1e2a"
ACCENT    = "#00b4d8"
ACCENT2   = "#ff6eb4"
TEXT      = "#eef0f7"
SUBTEXT   = "#8888aa"
SUCCESS   = "#4ecca3"
WARN      = "#ffb347"
FONT_B    = ("Segoe UI", 10)
FONT_S    = ("Segoe UI", 9)
FONT_MONO = ("Consolas", 9)

# ── constants ────────────────────────────────────────────────────────────────
GEMINI_MODEL        = "gemini-3-flash-preview"
CHUNK_SECS          = 3 * 60    # ~3 min — smaller chunks improve timestamp accuracy
MAX_WORKERS         = 1         # single Gemini worker — prevents cascading rate limit failures
UPLOAD_WORKERS      = 4         # parallel Files API uploads (independent of RPM)
REENCODE_HEIGHT     = 360
REENCODE_FPS        = 1
REENCODE_ABR        = "16000"
REENCODE_BITRATE_KB = 35        # ~35 KB/s video
RETRY_DELAYS        = [30, 60, 90, 120, 150]

# ── static-ffmpeg ─────────────────────────────────────────────────────────────

def _init_ffmpeg():
    try:
        import static_ffmpeg
        static_ffmpeg.add_paths(weak=False)
        path = shutil.which("ffmpeg") or "ffmpeg"
        print(f"[holoSub] FFmpeg: {path}")
    except ImportError:
        print("[holoSub] static-ffmpeg not found — using system FFmpeg")

_init_ffmpeg()

# ── hardware encoder detection ───────────────────────────────────────────────

def _get_working_encoder():
    import subprocess
    for enc in ["h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox", "h264_mf"]:
        try:
            subprocess.run(
                ["ffmpeg", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.01",
                 "-c:v", enc, "-b:v", "1000k", "-f", "null", "-"],
                check=True, capture_output=True, timeout=10
            )
            return enc
        except Exception:
            continue
    return "libx264"

_ENCODER = _get_working_encoder()
print(f"[holoSub] Encoder: {_ENCODER}")

# ── SRT helpers ───────────────────────────────────────────────────────────────

def ms_to_srt(ms):
    h, rem = divmod(int(ms), 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms2 = divmod(rem, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms2:03}"

def parse_timestamp(ts):
    """Parse MM:SS.mmm / MM:SS:mmm / HH:MM:SS into milliseconds."""
    try:
        ms_part = 0
        if "." in ts:
            left, frag = ts.rsplit(".", 1)
            ms_part = int(frag.ljust(3, "0")[:3])
            parts = left.split(":")
        elif ts.count(":") == 2:
            parts = ts.split(":")
            ms_part = int(parts.pop().ljust(3, "0")[:3])
        else:
            parts = ts.split(":")

        if len(parts) == 2:
            h, m, s = 0, int(parts[0]), int(parts[1])
        elif len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        else:
            return 0
        return h * 3_600_000 + m * 60_000 + s * 1000 + ms_part
    except Exception:
        return 0

def build_srt(entries):
    """Sort, dedupe, fix overlaps, return SRT string."""
    resolved = []
    for e in entries:
        text = e.get("text", "").strip()
        if not text:
            continue
        start = e.get("start_ms", 0)
        end   = e.get("end_ms", start + 2000)
        if end <= start:
            end = start + 2000
        if end - start < 100:
            continue
        resolved.append([start, end, text])

    resolved.sort(key=lambda x: x[0])

    # Remove entries with duplicate start times — keep only the first (longest/best)
    seen_starts = {}
    deduped = []
    for entry in resolved:
        start = entry[0]
        if start not in seen_starts:
            seen_starts[start] = True
            deduped.append(entry)
    resolved = deduped

    # Remove entries that are completely contained within the previous entry's timespan
    filtered = []
    for entry in resolved:
        if filtered and entry[0] >= filtered[-1][0] and entry[1] <= filtered[-1][1]:
            continue  # fully swallowed by previous entry, skip
        filtered.append(entry)
    resolved = filtered

    # Deduplicate consecutive identical or near-identical text
    # (Gemini sometimes repeats the same line multiple times in quick succession)
    deduped_text = []
    for entry in resolved:
        if deduped_text and entry[2].strip().lower() == deduped_text[-1][2].strip().lower():
            continue
        deduped_text.append(entry)
    resolved = deduped_text

    # Fix overlaps — ensure each entry ends before the next begins (min 100ms gap)
    GAP = 100
    for i in range(len(resolved) - 1):
        if resolved[i][1] > resolved[i + 1][0] - GAP:
            resolved[i][1] = max(resolved[i][0] + 200, resolved[i + 1][0] - GAP)

    # Final pass — remove any entries that ended up with invalid durations after overlap fix
    resolved = [e for e in resolved if e[1] > e[0] + 100]

    lines = []
    for idx, (start, end, text) in enumerate(resolved, 1):
        lines += [str(idx), f"{ms_to_srt(start)} --> {ms_to_srt(end)}", text, ""]
    return "\n".join(lines)

# ── ffprobe helpers ───────────────────────────────────────────────────────────

def get_duration(path):
    import subprocess, json as _j
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
            capture_output=True, text=True, timeout=30
        )
        return float(_j.loads(r.stdout)["format"]["duration"])
    except Exception:
        return None

def has_video_stream(path):
    import subprocess, json as _j
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "v", str(path)],
            capture_output=True, text=True, timeout=15
        )
        return len(_j.loads(r.stdout).get("streams", [])) > 0
    except Exception:
        return False

# ── download ──────────────────────────────────────────────────────────────────

def download_source(url, out_dir, log):
    """Download H.264 ≤480p + best audio. Returns (path, title)."""
    import yt_dlp
    log("📥  Downloading video…")

    last_pct = [-1]
    _ansi = re.compile(r'\x1b\[[0-9;]*m')
    def progress_hook(d):
        if d.get("status") == "downloading":
            downloaded = d.get("downloaded_bytes", 0)
            total      = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            speed      = _ansi.sub("", d.get("_speed_str", "")).strip()
            eta        = _ansi.sub("", d.get("_eta_str", "")).strip()
            if total and total > 0:
                pct = int(downloaded / total * 100)
                if pct // 5 > last_pct[0]:
                    last_pct[0] = pct // 5
                    log(f"   {pct}%  {speed}  ETA {eta}")

    ydl_opts = {
        "format": (
            "bestvideo[height<=480][vcodec^=avc]+bestaudio[ext=m4a]"
            "/bestvideo[height<=480][vcodec!*=av01]+bestaudio"
            "/best[height<=480][vcodec!*=av01]"
        ),
        "outtmpl": os.path.join(out_dir, "source.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [progress_hook],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info  = ydl.extract_info(url, download=True)
        title = info.get("title", "video")

    for ext in ("mp4", "mkv", "webm", "m4a"):
        p = os.path.join(out_dir, f"source.{ext}")
        if os.path.exists(p):
            log(f"✅  Downloaded: {title}")
            return p, title
    raise FileNotFoundError("Downloaded file not found")

# ── ffmpeg pipeline ───────────────────────────────────────────────────────────

def split_chunks(source_path, split_dir, skip_secs, log):
    """
    Stream-copy source into CHUNK_SECS segments with -reset_timestamps 1.
    Validates existing chunks by duration. Returns [(path, start_ms)].
    """
    import subprocess

    duration = get_duration(source_path)
    if duration is None:
        log("❌  ffprobe could not read duration")
        return []

    effective = duration - skip_secs
    if effective <= 0:
        log("⚠️  Skip offset exceeds stream duration")
        return []

    n      = math.ceil(effective / CHUNK_SECS)
    chunks = []

    for i in range(n):
        start = skip_secs + i * CHUNK_SECS
        end   = min(start + CHUNK_SECS, duration)
        out   = os.path.join(split_dir, f"chunk_{i:04d}.mp4")

        if os.path.exists(out) and os.path.getsize(out) > 1000:
            existing_dur = get_duration(out)
            expected_dur = end - start
            # Also verify the chunk isn't from a previous run with a different CHUNK_SECS
            # by checking it's not significantly longer than the current CHUNK_SECS
            if existing_dur and abs(existing_dur - expected_dur) < 2.0 and existing_dur <= CHUNK_SECS + 5:
                chunks.append((out, int(start * 1000)))
                continue
            os.remove(out)

        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-ss", str(start), "-to", str(end),
               "-i", str(source_path),
               "-c", "copy", "-map", "0", "-reset_timestamps", "1", out]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=180)
            chunks.append((out, int(start * 1000)))
        except subprocess.CalledProcessError as e:
            log(f"⚠️  Split failed chunk {i+1}: {e.stderr.decode('utf-8','replace')[-200:]}")

    return chunks

def reencode_chunk(chunk_path, log):
    """
    Re-encode to REENCODE_HEIGHT@REENCODE_FPS + 16kHz AAC mono.
    Uses hardware encoder if available.
    Validates output duration before accepting.
    Returns path to small file or original on failure.
    """
    import subprocess

    out = chunk_path.replace(".mp4", "_small.mp4")

    if os.path.exists(out) and os.path.getsize(out) > 500:
        in_dur  = get_duration(chunk_path)
        out_dur = get_duration(out)
        if in_dur and out_dur and abs(in_dur - out_dur) < 1.0:
            return out
        os.remove(out)

    if not has_video_stream(chunk_path):
        log(f"⚠️  {os.path.basename(chunk_path)}: no video stream, using original")
        return chunk_path

    bitrate_bps = REENCODE_BITRATE_KB * 1024 * 8
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-i", chunk_path,
           "-vf", f"fps={REENCODE_FPS},scale=trunc(iw/2)*2:{REENCODE_HEIGHT}",
           "-c:v", _ENCODER,
           "-g", str(REENCODE_FPS * 10),
           "-b:v", str(bitrate_bps),
           "-maxrate", str(bitrate_bps),
           "-bufsize", str(bitrate_bps * 2),
           "-c:a", "aac", "-b:a", "32k", "-ac", "1", "-ar", REENCODE_ABR,
           out]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        # Validate output duration
        in_dur  = get_duration(chunk_path)
        out_dur = get_duration(out)
        if in_dur and out_dur and abs(in_dur - out_dur) > 1.0:
            log(f"⚠️  Re-encode duration mismatch ({in_dur:.1f}s vs {out_dur:.1f}s) — using original")
            return chunk_path
        return out
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode("utf-8", errors="replace")
        log(f"⚠️  Re-encode failed: {stderr[-200:].strip()} — using original")
        return chunk_path

# ── Gemini Files API uploader ─────────────────────────────────────────────────

class GeminiFileUploader:
    """
    Thread-safe Files API uploader with caching and ACTIVE-state polling.
    One upload per file path; retries don't re-upload.
    """
    def __init__(self, client):
        self._client = client
        self._cache  = {}
        self._lock   = threading.Lock()

    def upload(self, local_path, log, label):
        with self._lock:
            if local_path in self._cache:
                return self._cache[local_path]

        from google.genai.types import UploadFileConfig, FileState

        file = self._client.files.upload(
            file=local_path,
            config=UploadFileConfig(display_name=os.path.basename(local_path)),
        )
        for _ in range(60):
            if file.state == FileState.ACTIVE:
                break
            if file.state == FileState.FAILED:
                raise RuntimeError(f"Files API rejected {os.path.basename(local_path)}")
            time.sleep(2)
            file = self._client.files.get(name=file.name)
        else:
            raise RuntimeError(f"Timeout waiting for {os.path.basename(local_path)}")

        with self._lock:
            self._cache[local_path] = file

        log(f"✅  {label}: uploaded ({file.name})")
        return file

    def delete_all(self, log):
        with self._lock:
            files, self._cache = list(self._cache.values()), {}
        for f in files:
            try:
                self._client.files.delete(name=f.name)
            except Exception:
                pass

# ── API key validation ────────────────────────────────────────────────────────

def validate_api_key(api_key, model=GEMINI_MODEL):
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        client.models.generate_content(
            model=model,
            contents=["hi"],
            config=types.GenerateContentConfig(max_output_tokens=1, thinking_config=types.ThinkingConfig(thinking_budget=0)),
        )
        return True, ""
    except Exception as e:
        err = str(e)
        if any(x in err for x in ("API_KEY_INVALID", "401", "403")):
            return False, "Invalid API key — check it and try again."
        if "404" in err or "not found" in err.lower():
            return False, f"Model not available on your account: {model}"
        if "429" in err or "quota" in err.lower() or "exhausted" in err.lower():
            return False, f"API quota exhausted — try again later or switch to a different model.\n\nFull error: {err}"
        return False, f"Could not reach Gemini API: {err}"

# ── prompt ────────────────────────────────────────────────────────────────────

PROMPT_VERSION = 11  # bump to invalidate resume cache on prompt change

def make_instruction(task, title):
    ctx = f"Video title: {title}." if title else ""
    if task == "translate":
        lang_rule = (
            "The streamers speak Japanese. Transcribe every utterance and translate "
            "into natural, colloquial English. Keep VTuber energy — translate 'yabe', "
            "'sugoi', 'kawaii' idiomatically, not literally. Never translate in isolation — "
            "use context from surrounding lines to determine pronouns, tense, and tone."
        )
        text_field = "English translation only"
    else:
        lang_rule = (
            "The streamers speak Japanese. Transcribe every utterance accurately "
            "in Japanese using proper kanji/kana. Preserve names as spoken."
        )
        text_field = "Japanese transcription only"

    return f"""You are an advanced AI expert in audio-visual subtitling. Your specialty is generating audio-synchronised, contextually rich subtitles from multimodal video+audio input using native audio tokenization.

{ctx}

### TASK
{lang_rule}

### HOLOLIVE TALENT NAME REFERENCE
These are the EXACT correct romanisations — never mishear or mis-romanise these names:
- hololive JP Gen 0: Tokino Sora, Robocosan, AZKi, Sakura Miko, Hoshimachi Suisei
- hololive JP Gen 1: Yozora Mel, Aki Rosenthal, Akai Haato, Shirakami Fubuki, Natsuiro Matsuri
- hololive JP Gen 2: Minato Aqua, Murasaki Shion, Nakiri Ayame, Yuzuki Choco, Oozora Subaru
- hololive JP GAMERS: Shirakami Fubuki, Ookami Mio, Nekomata Okayu, Inugami Korone
- hololive JP Gen 3: Usada Pekora, Shiranui Flare, Shirogane Noel, Houshou Marine, Uruha Rushia
- hololive JP Gen 4: Tsunomaki Watame, Tokoyami Towa, Himemori Luna, Amane Kanata, Kiryu Coco
- hololive JP Gen 5: Yukihana Lamy, Momosuzu Nene, Shishiro Botan, Omaru Polka, Mano Aloe
- hololive JP holoX: La+ Darknesss, Takane Lui, Hakui Koyori, Sakamata Chloe, Kazama Iroha
- hololive JP holoAN: Izuki Michiru, Hanazono Sayaka, Kazeshiro Yuki
- hololive ID Gen 1: Ayunda Risu, Moona Hoshinova, Airani Iofifteen
- hololive ID Gen 2: Kureiji Ollie, Anya Melfissa, Pavolia Reine
- hololive ID Gen 3: Vestia Zeta, Kaela Kovalskia, Kobo Kanaeru
- hololive EN Myth: Mori Calliope, Takanashi Kiara, Ninomae Ina'nis, Watson Amelia, Gawr Gura
- hololive EN Promise: IRyS, Ceres Fauna, Ouro Kronii, Hakos Baelz, Tsukumo Sana, Nanashi Mumei
- hololive EN Advent: Shiori Novella, Koseki Bijou, Nerissa Ravencroft, Fuwawa Abyssgard, Mococo Abyssgard
- hololive EN Justice: Elizabeth Rose Bloodflame, Gigi Murin, Cecilia Immergreen, Raora Panthera
- hololive DEV_IS ReGLOSS: Otonose Kanade, Ichijou Ririka, Juufuutei Raden, Todoroki Hajime, Hiodoshi Ao
- hololive DEV_IS FLOW GLOW: Isaki Riona, Koganei Niko, Mizumiya Su, Rindo Chihaya, Kikirara Vivi

### COMMON NICKNAMES & ALTERNATE READINGS
Streamers frequently call each other by shortened names. Map these to their correct full names:
- "Chiha" or "Chihaya" → Rindo Chihaya
- "Su-chan" or "Su" → Mizumiya Su
- "Riona" or "Riona-chan" → Isaki Riona
- "Niko" or "Niko-chan" → Koganei Niko
- "Vivi" or "Vivi-chan" → Kikirara Vivi
- "Marine" or "Senchou" → Houshou Marine
- "Noel" or "Danchou" → Shirogane Noel
- "Miko" or "Mikochi" → Sakura Miko
- "Suisei" or "Suichan" → Hoshimachi Suisei
- "Korone" or "Korosan" → Inugami Korone
- "Subaru" or "Suba-chan" → Oozora Subaru
- "Aqua" or "Akutan" → Minato Aqua
- "Towa" or "Towa-sama" → Tokoyami Towa
- "Lamy" or "Lamy-chan" → Yukihana Lamy
- "Botan" or "Shishiron" → Shishiro Botan
- "Nene" or "Nenechi" → Momosuzu Nene

### THE GOLDEN RULE: AUDIO DICTATES "WHEN", VIDEO DICTATES "WHAT"
Strictly separate the task of *timing* from the task of *transcription*.

**WHEN (Timing):** The streamer's voice is your absolute ground truth for timestamps. The exact moment a vocal starts dictates `s`. NEVER output a subtitle if no streamer voice is present.

**WHAT (Content):** Use the video visuals, on-screen text, and stream title to determine correct spelling, names, and context. When audio is ambiguous:
1. On-screen text (burnt-in subtitles, overlays) — absolute override.
2. The talent name reference list above — use exact spellings.
3. Visual scene context — what is actively happening on screen.
4. Stream title for broader context.

**Silence rules — ONLY stay silent when:**
- Pure instrumental BGM or ambience with no voice whatsoever.
- Waiting room / pre-stream screen with no one speaking or singing.
- Background game music or sound effects with no streamer voice.

**NEVER stay silent because:**
- Gameplay is on screen — streamers almost always talk during gameplay.
- An ad or sponsor segment is showing — streamers react and comment over them.
- A cutscene is playing — streamers frequently narrate over cutscenes.
- An intro or outro animation is playing — subtitle any singing or speech you hear, whether it is live or pre-recorded.
- If you can hear any voice at all — streamer, singer, or guest — subtitle it.

**No CC tags:** Transcribe speech only. Never add `[applause]`, `(sighs)`, `♪`, or any sound-effect tags.

### PRIORITY 1: PRECISION TIMESTAMPS
Treat every subtitle as a discrete, isolated event tied exclusively to the streamer's voice.

- **Timecode format:** Strictly `MM:SS.mmm` (e.g., `01:05.300`). Always pad with zeros.
- **`s`** = the exact moment the first word begins. **`e`** = the exact moment the last word ends.
- **No stretching for readability:** `e` MUST match when the last word ends, not when a human would want the text to disappear. Never stretch `e` to keep text on screen longer.
- **Prevent the "Traffic Jam" effect:** If you stretch `e` too long, it bleeds into the next subtitle's `s`, pushing everything out of sync. Each subtitle's `s` and `e` must tightly wrap only its own phrase — not bleed into the next one.
- **Unique start times:** Every entry MUST have a unique `s` — no two entries may share the same start time.
- **Strictly sequential:** `s` of entry N+1 MUST always be greater than `e` of entry N.

### PRIORITY 2: SEGMENTATION
- **Pause = split:** If there is a physical pause or breath in the middle of a sentence, SPLIT into a new block. NEVER merge speech across a pause into one entry.
- **Continuous breath split:** If splitting a continuous uninterrupted sentence only to stay under 50 characters, Part 1 `e` MUST EXACTLY EQUAL Part 2 `s` — do not invent a gap that doesn't exist in the audio.
- **Max ~50 characters per line.**
- **No repeated text:** Never output the same text in consecutive entries.

### EXAMPLES

**Example 1: Two streamers in rapid back-and-forth during gameplay**
*Scenario:* Su says "やばい！" (00:02.100–00:02.500), Chihaya immediately says "大丈夫！" (00:02.500–00:03.100).
```json
{{
  "global_analysis": "Two streamers reacting during racing gameplay. Both are speaking continuously with no silent gaps. Timestamps tightly wrap each utterance.",
  "subs": [
    {{"s": "00:02.100", "e": "00:02.500", "text": "Oh no!"}},
    {{"s": "00:02.500", "e": "00:03.100", "text": "You're okay!"}}
  ]
}}
```

**Example 2: Streamer talking over a sponsor segment**
*Scenario:* An ad for NEXTGEAR is on screen. The streamer is narrating over it from 00:05.000.
```json
{{
  "global_analysis": "Sponsor segment visible on screen. Streamer is actively speaking over it — subtitling their speech as normal.",
  "subs": [
    {{"s": "00:05.000", "e": "00:07.200", "text": "So this is the new NEXTGEAR case!"}},
    {{"s": "00:07.400", "e": "00:09.100", "text": "It has a glass panel on two sides."}}
  ]
}}
```

**Example 3: Splitting a continuous breath vs a paused sentence**
*Scenario:* Streamer says one continuous breath "それはちょっと違うんじゃないかな" (00:10.000–00:11.800, no pause). Too long, must split at 50 chars.
```json
{{
  "global_analysis": "Single continuous utterance split for length only — no audio pause exists, so Part 1 e equals Part 2 s exactly.",
  "subs": [
    {{"s": "00:10.000", "e": "00:10.900", "text": "That's a little..."}},
    {{"s": "00:10.900", "e": "00:11.800", "text": "...different, don't you think?"}}
  ]
}}
```

**Example 4: Opening theme song / intro sequence**
*Scenario:* The stream begins with an animated intro playing the streamer's theme song with vocals. Subtitle the singing. The live stream conversation begins at 03:06.
```json
{{
  "global_analysis": "Clip begins with an animated intro sequence containing the streamer's theme song with vocals. Subtitling the singing as normal. Live stream conversation begins at approximately 03:06.",
  "subs": [
    {{"s": "00:02.100", "e": "00:04.500", "text": "Riona-chan!"}},
    {{"s": "00:04.700", "e": "00:07.200", "text": "Yes! Sakisaki Riona!"}},
    {{"s": "03:06.500", "e": "03:08.200", "text": "Okay, let's get started!"}}
  ]
}}
```

### OUTPUT FORMAT
Return ONLY a valid JSON object. No markdown. Output `global_analysis` FIRST.

`global_analysis` is a strict verification step — state: (1) what is in this clip and who is speaking, (2) confirm no gameplay/ad sections were silenced incorrectly, (3) confirm no Traffic Jam stretching was applied.

{{
  "global_analysis": "...",
  "subs": [
    {{
      "s": "MM:SS.mmm",
      "e": "MM:SS.mmm",
      "text": "{text_field}"
    }}
  ]
}}

Rules:
- `text` is a single line — no embedded newlines.
- `text` contains ONLY the {text_field}."""

# ── response parser ───────────────────────────────────────────────────────────

def parse_response(raw_text, offset_ms, label, log):
    raw_text = re.sub(r"^```[a-zA-Z]*\n?", "", raw_text.strip())
    raw_text = re.sub(r"\n?```$", "", raw_text).strip()
    if not raw_text:
        return None, False  # treat empty as bad JSON — caller will retry

    obj, truncated = None, False

    # Strip trailing junk after the closing } (Extra data error)
    clean = raw_text
    if not clean.endswith("}"):
        last_brace = clean.rfind("}")
        if last_brace != -1:
            clean = clean[:last_brace + 1]

    try:
        obj = json.loads(clean)
    except json.JSONDecodeError:
        # Try to salvage truncated response by finding last complete entry
        # Look for the last complete "},\n  {" or "}]" boundary
        salvage = clean
        for marker in ['}\n  ]', '},\n  {', '},\n{', '}, {']:
            pos = salvage.rfind(marker)
            if pos != -1:
                # Cut to just after the last complete entry, close the array and object
                end = pos + 1  # include the closing }
                try:
                    candidate = salvage[:end] + "\n  ]\n}"
                    obj = json.loads(candidate)
                    truncated = True
                    break
                except Exception:
                    pass

        if obj is None:
            # Last resort: try closing off with minimal suffix
            for suffix in ['\n  ]\n}', ']}']:
                try:
                    obj = json.loads(salvage + suffix)
                    truncated = True
                    break
                except Exception:
                    pass

        if obj is None:
            log(f"⚠️  {label}: unparseable JSON — will retry")
            return None, False

    analysis = obj.get("global_analysis", "")
    if analysis:
        if len(analysis) > 150:
            truncated_analysis = analysis[:150].rsplit(' ', 1)[0] + "…"
        else:
            truncated_analysis = analysis
        log(f"💬  {label}: {truncated_analysis}")

    if truncated:
        log(f"⚠️  {label}: response was truncated, salvaged {len(obj.get('subs', []))} entries — will retry")

    entries = []
    for s in obj.get("subs", []):
        text = str(s.get("text", "")).strip()
        if not text:
            continue
        # Strip embedded newlines Gemini sometimes sneaks in despite instructions
        text = text.replace("\n", " ").strip()
        start_ms = parse_timestamp(s.get("s", "00:00.000")) + offset_ms
        end_ms   = parse_timestamp(s.get("e", "00:00.000")) + offset_ms
        if end_ms <= start_ms:
            end_ms = start_ms + 2000
        entries.append({"start_ms": start_ms, "end_ms": end_ms, "text": text})

    return entries, truncated

# ── chunk worker ──────────────────────────────────────────────────────────────

def process_chunk(i, chunk_path, offset_ms, chunk_duration_ms, instruction, resume_path,
                  api_key, uploader, model, label, log_fn,
                  rate_limit_until=None, rate_limit_lock=None):
    """Upload → Gemini → parse → cache. Returns (i, entries)."""
    from google import genai
    from google.genai import types

    # Resumption: check version-tagged cache
    if os.path.exists(resume_path):
        try:
            with open(resume_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("prompt_version") == PROMPT_VERSION:
                saved = data.get("entries", [])
                log_fn(f"⏭️  {label}: loaded from cache ({len(saved)} lines)")
                return i, saved
        except Exception:
            pass

    client = genai.Client(api_key=api_key)

    try:
        gemini_file = uploader.upload(chunk_path, log_fn, label)
    except Exception as e:
        log_fn(f"❌  {label}: upload failed — {e}")
        return i, []

    strict_prefix = (
        'Return ONLY a valid JSON object with keys "global_analysis" and "subs". '
        'No markdown, no text outside JSON.\n\n'
    )

    # Response schema — tells Gemini the exact structure to return,
    # eliminating truncation and unparseable JSON at the API level.
    from google.genai import types as _types
    _sub_schema = _types.Schema(
        type=_types.Type.OBJECT,
        required=["s", "e", "text"],
        properties={
            "s":    _types.Schema(type=_types.Type.STRING),
            "e":    _types.Schema(type=_types.Type.STRING),
            "text": _types.Schema(type=_types.Type.STRING),
        },
    )
    _response_schema = _types.Schema(
        type=_types.Type.OBJECT,
        required=["global_analysis", "subs"],
        properties={
            "global_analysis": _types.Schema(type=_types.Type.STRING),
            "subs": _types.Schema(
                type=_types.Type.ARRAY,
                items=_sub_schema,
            ),
        },
    )

    for attempt in range(5):
        # Respect global rate limit pause set by any worker
        if rate_limit_until is not None and rate_limit_lock is not None:
            with rate_limit_lock:
                wait_until = rate_limit_until[0]
            now = time.time()
            if wait_until > now:
                sleep_secs = wait_until - now
                log_fn(f"⏸️  {label}: waiting {sleep_secs:.0f}s for global rate limit cooldown…")
                time.sleep(sleep_secs)
        prompt = (strict_prefix + instruction) if attempt > 0 else instruction
        try:
            response = client.models.generate_content(
                model=model,
                contents=[gemini_file, prompt],
                config=types.GenerateContentConfig(
                    max_output_tokens=65536,
                    response_mime_type="application/json",
                    response_schema=_response_schema,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )
            try:
                raw_text = response.text.strip()
            except Exception:
                log_fn(f"⚠️  {label}: empty/blocked response")
                if attempt < 4:
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
                return i, []

            entries, truncated = parse_response(raw_text, offset_ms, label, log_fn)

            if entries is None:
                if attempt < 4:
                    log_fn(f"🔄  {label}: bad JSON, retry {attempt+2}/5…")
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
                return i, []

            if truncated and attempt < 4:
                log_fn(f"🔄  {label}: truncated, retry {attempt+2}/5…")
                time.sleep(RETRY_DELAYS[attempt])
                continue

            # Validate timestamps are within expected range for this chunk.
            # Gemini sometimes returns timestamps relative to the full video
            # instead of the clip, causing all entries to land at the wrong time.
            if entries and chunk_duration_ms > 0:
                # Expected range: offset_ms to offset_ms + chunk_duration_ms + 30s tolerance
                min_ms = offset_ms - 2000   # 2s before chunk start
                max_ms = offset_ms + chunk_duration_ms + 30000  # 30s after chunk end
                in_range = [e for e in entries if min_ms <= e["start_ms"] <= max_ms]
                out_of_range = len(entries) - len(in_range)
                if out_of_range > 0:
                    log_fn(f"ℹ️  {label}: {out_of_range}/{len(entries)} entries outside expected range [{min_ms/1000:.0f}s-{max_ms/1000:.0f}s], keeping in-range entries")
                if len(in_range) == 0 and attempt < 4:
                    # All entries are out of range — Gemini used completely wrong timestamps
                    log_fn(f"⚠️  {label}: all entries out of range — retrying")
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
                entries = in_range if in_range else entries

            # Coverage check — if no entries fall in the last 30% of the chunk's
            # time window, Gemini likely stopped early and missed speech at the end.
            # Only trigger if there are enough entries to suggest active speech was present
            # (avoids false positives on chunks that legitimately end in silence).
            if entries and len(entries) >= 5 and chunk_duration_ms > 0 and attempt < 4:
                coverage_threshold = offset_ms + (chunk_duration_ms * 0.7)
                last_entry_ms = max(e["start_ms"] for e in entries)
                if last_entry_ms < coverage_threshold:
                    log_fn(f"⚠️  {label}: last entry at {last_entry_ms/1000:.1f}s, chunk ends at {(offset_ms+chunk_duration_ms)/1000:.1f}s — possible early cutoff, retrying")
                    time.sleep(RETRY_DELAYS[attempt])
                    continue

            result = entries or []
            try:
                with open(resume_path, "w", encoding="utf-8") as f:
                    json.dump({"prompt_version": PROMPT_VERSION, "entries": result}, f)
            except Exception:
                pass
            return i, result

        except Exception as e:
            err = str(e)
            is_rate      = "429" in err or "quota" in err.lower() or "rate" in err.lower()
            is_transient = "503" in err or "unavailable" in err.lower() or "overloaded" in err.lower()
            is_timeout   = any(x in err.lower() for x in ("deadline", "timeout", "timed out"))

            if is_rate or is_transient or is_timeout:
                wait   = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                reason = "Rate limited" if is_rate else ("Timeout" if is_timeout else "Server unavailable")
                log_fn(f"⏳  {reason} on {label} — waiting {wait}s ({attempt+1}/5)…")
                # Set global rate limit pause so other workers also back off
                if is_rate and rate_limit_until is not None and rate_limit_lock is not None:
                    with rate_limit_lock:
                        rate_limit_until[0] = max(rate_limit_until[0], time.time() + wait)
                else:
                    time.sleep(wait)
                if attempt == 4:
                    log_fn(f"❌  {label}: retries exhausted")
                    return i, []
            else:
                log_fn(f"❌  {label}: {err[:200]}")
                return i, []

    return i, []

# ── pipeline orchestrator ─────────────────────────────────────────────────────

def transcribe_with_gemini(source_path, task, api_key, title,
                            skip_secs, resume_dir, tmp_dir, model, log, progress_cb):
    from google import genai

    os.makedirs(resume_dir, exist_ok=True)
    # All FFmpeg work in ASCII tmp_dir — avoids Windows Unicode path issues
    split_dir = os.path.join(tmp_dir, "splits")
    os.makedirs(split_dir, exist_ok=True)

    # Global rate limit pause — when any worker hits a 429, all workers wait
    _rate_limit_until = [0.0]
    _rate_limit_lock  = threading.Lock()

    mb = os.path.getsize(source_path) / 1_048_576
    log(f"🎙️  Source: {mb:.1f} MB")
    if skip_secs > 0:
        log(f"⏩  Skipping first {skip_secs//60:.0f}m {skip_secs%60:.0f}s")

    # 1. Split
    chunks = split_chunks(source_path, split_dir, skip_secs, log)
    if not chunks:
        log("❌  No chunks produced")
        return []
    n = len(chunks)
    log(f"📊  {n} chunks  ({MAX_WORKERS} subtitle workers  {UPLOAD_WORKERS} upload workers)")

    # 2. Re-encode in parallel (2 threads — CPU-bound)
    log("🔄  Re-encoding chunks…")
    small_chunks = [None] * n
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(reencode_chunk, cp, log): (idx, off)
                for idx, (cp, off) in enumerate(chunks)}
        for fut in concurrent.futures.as_completed(futs):
            idx, off = futs[fut]
            small_chunks[idx] = (fut.result(), off)

    # 3. Upload all chunks first (UPLOAD_WORKERS parallel, independent of Gemini RPM)
    log(f"☁️   Uploading {n} chunks to Files API…")
    client   = genai.Client(api_key=api_key)
    uploader = GeminiFileUploader(client)

    def do_upload(idx, chunk_path, offset_ms):
        label = f"chunk {idx+1}/{n}"
        try:
            uploader.upload(chunk_path, log, label)
        except Exception as e:
            log(f"❌  {label}: upload failed — {e}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as ex:
        futs = [ex.submit(do_upload, idx, cp, off)
                for idx, (cp, off) in enumerate(small_chunks)]
        concurrent.futures.wait(futs)

    # 4. Generate subtitles (MAX_WORKERS parallel — respects RPM)
    instruction = make_instruction(task, title)
    results     = {}
    completed   = [0]
    lock        = threading.Lock()

    def do_subtitle(i, chunk_path, offset_ms):
        label       = f"chunk {i+1}/{n}"
        resume_path = os.path.join(resume_dir, f"chunk_{i:04d}.json")
        chunk_dur_ms = int(get_duration(chunk_path) * 1000) if get_duration(chunk_path) else CHUNK_SECS * 1000
        idx, entries = process_chunk(
            i, chunk_path, offset_ms, chunk_dur_ms, instruction,
            resume_path, api_key, uploader, model, label, log,
            _rate_limit_until, _rate_limit_lock
        )
        with lock:
            completed[0] += 1
            progress_cb(completed[0], n)
            if entries:
                log(f"✅  Chunk {i+1}: {len(entries)} lines")
            else:
                log(f"⚠️  Chunk {i+1}: no subtitles returned")
        return idx, entries

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(do_subtitle, idx, cp, off): idx
                for idx, (cp, off) in enumerate(small_chunks)}
        for fut in concurrent.futures.as_completed(futs):
            try:
                idx, entries = fut.result()
                results[idx] = entries
            except Exception as e:
                log(f"❌  Worker exception: {e}")

    # 5. Clean up Files API
    log("🧹  Cleaning up Files API…")
    uploader.delete_all(log)

    # 6. Reassemble in order
    all_entries = []
    for i in range(n):
        all_entries.extend(results.get(i, []))
    return all_entries

# ── folder helpers ────────────────────────────────────────────────────────────

def sanitise_folder_name(title):
    title = re.sub(r'[【】「」『』〔〕]', ' ', title)
    title = re.sub(r'#\S+', '', title)
    title = re.sub(r'[\\/*?:"<>|]', '', title)
    title = re.sub(r'\s+', ' ', title).strip()
    if len(title) > 60:
        title = title[:60].rsplit(' ', 1)[0].strip()
    return title or "video"

# ── local pipeline (Whisper + Gemini translation) ────────────────────────────

def extract_audio(source_path, out_path, skip_secs, log):
    """Extract audio from video as 16kHz mono WAV for Whisper."""
    import subprocess
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-ss", str(skip_secs), "-i", str(source_path),
           "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", out_path]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
        log(f"🎵  Audio extracted")
        return True
    except subprocess.CalledProcessError as e:
        log(f"❌  Audio extraction failed: {e.stderr.decode('utf-8','replace')[-200:]}")
        return False

def transcribe_with_whisper(audio_path, resume_dir, skip_secs, log, progress_cb):
    """Run faster-whisper in a subprocess. Returns list of {start_ms, end_ms, text}."""
    import json as _json
    import subprocess
    import sys

    whisper_cache = os.path.join(resume_dir, "whisper_transcript.json")
    if os.path.exists(whisper_cache):
        try:
            with open(whisper_cache, "r", encoding="utf-8") as f:
                segments = _json.load(f)
            log(f"⏭️  Whisper: loaded from cache ({len(segments)} segments)")
            return segments
        except Exception:
            pass

    # Write a small helper script that runs Whisper and writes output to JSON
    # Running in a subprocess isolates CUDA cleanup from the GUI process
    helper_script = f"""
import sys, json
try:
    from faster_whisper import WhisperModel
except ImportError:
    print(json.dumps({{"error": "faster-whisper not installed"}}))
    sys.exit(1)

audio_path   = {repr(audio_path)}
output_path  = {repr(whisper_cache)}
skip_secs    = {skip_secs}

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
    start_ms = int((seg.start + skip_secs) * 1000)
    end_ms   = int((seg.end   + skip_secs) * 1000)
    text     = seg.text.strip()
    if text:
        segments.append({{"start_ms": start_ms, "end_ms": end_ms, "text": text}})
    if total_duration > 0:
        pct = min(100, int(seg.end / total_duration * 100))
        elapsed_mins = int(seg.end / 60)
        total_mins   = int(total_duration / 60)
        if elapsed_mins % 5 == 0:
            print(f"PROGRESS {{elapsed_mins}} {{total_mins}} {{pct}}", flush=True)

with open(output_path, "w", encoding="utf-8") as f:
    json.dump(segments, f, ensure_ascii=False)
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

        # Check if output was written successfully — return code isn't reliable
        # since faster-whisper can exit non-zero due to CUDA cleanup warnings
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

    # Load results from cache written by subprocess
    try:
        with open(whisper_cache, "r", encoding="utf-8") as f:
            segments = _json.load(f)
        return segments
    except Exception as e:
        log(f"❌  Could not read Whisper output: {e}")
        return []

def translate_with_gemini(segments, title, api_key, model, resume_dir, log, progress_cb):
    """Batch-translate Japanese segments using Gemini text-only API."""
    import json as _json
    from google import genai
    from google.genai import types

    if not segments:
        return []

    trans_cache = os.path.join(resume_dir, "translation.json")
    if os.path.exists(trans_cache):
        try:
            with open(trans_cache, "r", encoding="utf-8") as f:
                data = _json.load(f)
            if data.get("prompt_version") == PROMPT_VERSION:
                saved = data.get("entries", [])
                log(f"⏭️  Translation: loaded from cache ({len(saved)} entries)")
                return saved
        except Exception:
            pass

    client = genai.Client(api_key=api_key)
    ctx    = f"Video title: {title}." if title else ""

    # Build a translation-only prompt — no video, just text
    system_prompt = f"""You are an expert translator for hololive VTuber streams.
{ctx}

### HOLOLIVE TALENT NAME REFERENCE
These are the EXACT correct romanisations — never mis-romanise these names:
- hololive JP Gen 0: Tokino Sora, Robocosan, AZKi, Sakura Miko, Hoshimachi Suisei
- hololive JP Gen 1: Yozora Mel, Aki Rosenthal, Akai Haato, Shirakami Fubuki, Natsuiro Matsuri
- hololive JP Gen 2: Minato Aqua, Murasaki Shion, Nakiri Ayame, Yuzuki Choco, Oozora Subaru
- hololive JP GAMERS: Shirakami Fubuki, Ookami Mio, Nekomata Okayu, Inugami Korone
- hololive JP Gen 3: Usada Pekora, Shiranui Flare, Shirogane Noel, Houshou Marine, Uruha Rushia
- hololive JP Gen 4: Tsunomaki Watame, Tokoyami Towa, Himemori Luna, Amane Kanata, Kiryu Coco
- hololive JP Gen 5: Yukihana Lamy, Momosuzu Nene, Shishiro Botan, Omaru Polka, Mano Aloe
- hololive JP holoX: La+ Darknesss, Takane Lui, Hakui Koyori, Sakamata Chloe, Kazama Iroha
- hololive JP holoAN: Izuki Michiru, Hanazono Sayaka, Kazeshiro Yuki
- hololive ID Gen 1: Ayunda Risu, Moona Hoshinova, Airani Iofifteen
- hololive ID Gen 2: Kureiji Ollie, Anya Melfissa, Pavolia Reine
- hololive ID Gen 3: Vestia Zeta, Kaela Kovalskia, Kobo Kanaeru
- hololive EN Myth: Mori Calliope, Takanashi Kiara, Ninomae Ina'nis, Watson Amelia, Gawr Gura
- hololive EN Promise: IRyS, Ceres Fauna, Ouro Kronii, Hakos Baelz, Tsukumo Sana, Nanashi Mumei
- hololive EN Advent: Shiori Novella, Koseki Bijou, Nerissa Ravencroft, Fuwawa Abyssgard, Mococo Abyssgard
- hololive EN Justice: Elizabeth Rose Bloodflame, Gigi Murin, Cecilia Immergreen, Raora Panthera
- hololive DEV_IS ReGLOSS: Otonose Kanade, Ichijou Ririka, Juufuutei Raden, Todoroki Hajime, Hiodoshi Ao
- hololive DEV_IS FLOW GLOW: Isaki Riona, Koganei Niko, Mizumiya Su, Rindo Chihaya, Kikirara Vivi

### COMMON NICKNAMES
- "Chiha" or "Chihaya" → Rindo Chihaya
- "Su-chan" or "Su" → Mizumiya Su
- "Marine" or "Senchou" → Houshou Marine
- "Noel" or "Danchou" → Shirogane Noel
- "Miko" or "Mikochi" → Sakura Miko
- "Suisei" or "Suichan" → Hoshimachi Suisei
- "Korone" or "Korosan" → Inugami Korone
- "Subaru" or "Suba-chan" → Oozora Subaru
- "Aqua" or "Akutan" → Minato Aqua
- "Towa" or "Towa-sama" → Tokoyami Towa
- "Lamy" or "Lamy-chan" → Yukihana Lamy
- "Botan" or "Shishiron" → Shishiro Botan
- "Nene" or "Nenechi" → Momosuzu Nene

### TASK
Translate the following Japanese subtitle segments into natural, colloquial English.
Keep VTuber energy — translate 'yabe', 'sugoi', 'kawaii' idiomatically, not literally.
Never translate in isolation — use context from surrounding lines for pronouns, tense, and tone.
If a segment is already in English, keep it as-is.
Return ONLY a valid JSON array with one translated string per input segment, in the same order.
No markdown, no extra keys — just a JSON array of strings."""

    BATCH = 60  # segments per Gemini call
    entries = []
    total   = len(segments)

    for batch_start in range(0, total, BATCH):
        batch = segments[batch_start:batch_start + BATCH]
        lines = [s["text"] for s in batch]
        user_msg = "Translate these segments:\n" + _json.dumps(lines, ensure_ascii=False)

        translations = lines  # default fallback — keeps Japanese if all attempts fail
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
                translations = _json.loads(raw)
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

        for j, seg in enumerate(batch):
            translated = translations[j] if j < len(translations) else seg["text"]
            entries.append({
                "start_ms": seg["start_ms"],
                "end_ms":   seg["end_ms"],
                "text":     str(translated).strip(),
            })

        done = min(batch_start + BATCH, total)
        progress_cb(done, total)
        log(f"📝  Translated {done}/{total} segments…")

    try:
        with open(trans_cache, "w", encoding="utf-8") as f:
            _json.dump({"prompt_version": PROMPT_VERSION, "entries": entries}, f, ensure_ascii=False)
    except Exception:
        pass

    return entries

def transcribe_local(source_path, task, api_key, title,
                     skip_secs, resume_dir, tmp_dir, model, log, progress_cb):
    """Local pipeline: faster-whisper transcription + Gemini text translation."""
    os.makedirs(resume_dir, exist_ok=True)

    # 1. Extract audio
    audio_path = os.path.join(tmp_dir, "audio.wav")
    log("🎵  Extracting audio…")
    if not extract_audio(source_path, audio_path, skip_secs, log):
        return []

    # 2. Whisper transcription
    log("🎤  Starting Whisper transcription…")
    segments = transcribe_with_whisper(audio_path, resume_dir, skip_secs, log,
                                       lambda d, t: progress_cb(int(d/t*50) if t else 0, 100))
    if not segments:
        return []

    # 3. Translate or return as-is
    if task == "translate":
        log(f"🌐  Translating {len(segments)} segments with Gemini…")
        entries = translate_with_gemini(segments, title, api_key, model, resume_dir, log,
                                        lambda d, t: progress_cb(50 + int(d/t*50) if t else 50, 100))
    else:
        entries = segments
        progress_cb(100, 100)

    return entries

def run_pipeline(source, is_url, task, api_key, outdir, skip_mins, model, mode,
                 log, progress_cb, done_cb):
    tmp = None
    try:
        tmp = tempfile.mkdtemp()

        if is_url:
            source_path, title = download_source(source, tmp, log)
        else:
            source_path = source
            title = os.path.splitext(os.path.basename(source))[0]

        safe_title = sanitise_folder_name(title)
        video_dir  = os.path.join(outdir, safe_title)
        resume_dir = os.path.join(video_dir, ".resume")
        os.makedirs(video_dir, exist_ok=True)
        log(f"📁  Output: {video_dir}")

        if mode == "local":
            entries = transcribe_local(
                source_path, task, api_key, title,
                int(skip_mins * 60), resume_dir, tmp, model, log, progress_cb
            )
        else:
            entries = transcribe_with_gemini(
                source_path, task, api_key, title,
                int(skip_mins * 60), resume_dir, tmp, model, log, progress_cb
            )

        if not entries:
            log("❌  No subtitles returned — check log above.")
            done_cb(None)
            return

        srt = build_srt(entries)

        # Try to match the .srt filename to any existing video file in video_dir
        # so MPC-HC auto-loads it without manual selection
        srt_name = safe_title  # fallback
        for f in os.listdir(video_dir):
            ext = os.path.splitext(f)[1].lower()
            if ext in (".mp4", ".mkv", ".webm", ".avi", ".mov"):
                srt_name = os.path.splitext(f)[0]
                log(f"🔗  Matching .srt name to video: {f}")
                break

        out_file = os.path.join(video_dir, f"{srt_name}.srt")
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(srt)

        log(f"\n✨  Done! {len(entries)} lines → {out_file}")
        done_cb(out_file)

    except Exception as e:
        import traceback
        log(f"\n❌  Error: {e}\n{traceback.format_exc()}")
        done_cb(None)
    finally:
        if tmp and os.path.isdir(tmp):
            try:
                shutil.rmtree(tmp)
            except Exception:
                pass

# ── dependency checks ─────────────────────────────────────────────────────────

def _check_deps():
    missing = []
    for pkg, imp in [("google-genai", "google.genai"),
                     ("yt-dlp", "yt_dlp"),
                     ("static-ffmpeg", "static_ffmpeg")]:
        try:
            __import__(imp)
        except ImportError:
            missing.append(pkg)
    return missing

def _check_ffmpeg():
    import subprocess
    ok_ff = ok_fp = False
    for name, flag in [("ffmpeg", True), ("ffprobe", False)]:
        try:
            subprocess.run([name, "-version"], capture_output=True, timeout=5)
            if flag:
                ok_ff = True
            else:
                ok_fp = True
        except Exception:
            pass
    return ok_ff, ok_fp

# ── GUI ───────────────────────────────────────────────────────────────────────

class HoloSubApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("holoSub ✦ Auto Subtitle Generator")
        self.geometry("780x790")
        self.resizable(True, True)
        self.configure(bg=BG)
        self._build_ui()

    def _build_ui(self):
        # Header
        hdr = tk.Frame(self, bg=BG)
        hdr.pack(fill="x", padx=24, pady=(20, 2))
        logo_font = ("Segoe UI", 26, "bold")
        c = tk.Canvas(hdr, bg=BG, highlightthickness=0, height=38, width=500)
        c.pack(side="left")
        c.create_text(0,   19, text="holo", font=logo_font, fill=ACCENT,  anchor="w")
        c.create_text(72,  19, text="Sub",  font=logo_font, fill=ACCENT2, anchor="w")
        c.create_text(140, 22, text="✦  Auto Subtitle Generator",
                      font=("Segoe UI", 13), fill=SUBTEXT, anchor="w")
        tk.Label(self, text="Paste a YouTube / Holodex URL, or pick a local video/audio file.",
                 font=FONT_S, bg=BG, fg=SUBTEXT).pack(anchor="w", padx=26, pady=(0, 10))

        # API key
        self._section("Gemini API key")
        acard = tk.Frame(self, bg=CARD, padx=16, pady=12)
        acard.pack(fill="x", padx=20, pady=(0, 10))
        self.apikey_var = tk.StringVar()
        tk.Entry(acard, textvariable=self.apikey_var, show="•",
                 font=FONT_B, bg=CARD2, fg=TEXT, insertbackground=TEXT,
                 relief="flat", bd=0, highlightthickness=1,
                 highlightcolor=ACCENT, highlightbackground="#333"
                 ).pack(fill="x", ipady=6)
        self._api_status = tk.Label(
            acard,
            text="Get a free key at aistudio.google.com — never stored outside this session.",
            font=("Segoe UI", 8), bg=CARD, fg=SUBTEXT)
        self._api_status.pack(anchor="w", pady=(4, 0))

        # Source
        self._section("Source")
        scard = tk.Frame(self, bg=CARD, padx=16, pady=12)
        scard.pack(fill="x", padx=20, pady=(0, 10))
        self.source_var = tk.StringVar()
        src_row = tk.Frame(scard, bg=CARD)
        src_row.pack(fill="x")
        tk.Entry(src_row, textvariable=self.source_var, font=FONT_B,
                 bg=CARD2, fg=TEXT, insertbackground=TEXT,
                 relief="flat", bd=0, highlightthickness=1,
                 highlightcolor=ACCENT, highlightbackground="#333"
                 ).pack(side="left", fill="x", expand=True, ipady=6)
        tk.Button(src_row, text="Browse…", font=FONT_S, bg=CARD, fg=ACCENT,
                  activebackground=CARD, activeforeground=ACCENT2,
                  relief="flat", cursor="hand2", padx=8,
                  command=self._browse_file).pack(side="left", padx=(8, 0))

        # Settings row
        srow = tk.Frame(self, bg=BG)
        srow.pack(fill="x", padx=20, pady=(0, 10))

        tcard = tk.Frame(srow, bg=CARD, padx=14, pady=10)
        tcard.pack(side="left", fill="y")
        tk.Label(tcard, text="Output language", font=FONT_S, bg=CARD, fg=SUBTEXT).pack(anchor="w")
        self.task_var = tk.StringVar(value="translate")
        tk.Radiobutton(tcard, text="English (translate + localise)",
                       variable=self.task_var, value="translate",
                       bg=CARD, fg=TEXT, selectcolor=CARD,
                       activebackground=CARD, font=FONT_B).pack(anchor="w")
        tk.Radiobutton(tcard, text="Japanese (keep original)",
                       variable=self.task_var, value="transcribe",
                       bg=CARD, fg=TEXT, selectcolor=CARD,
                       activebackground=CARD, font=FONT_B).pack(anchor="w")

        tk.Label(tcard, text="", bg=CARD).pack()  # spacer
        tk.Label(tcard, text="Processing mode", font=FONT_S, bg=CARD, fg=SUBTEXT).pack(anchor="w")
        self.mode_var = tk.StringVar(value="gemini")
        tk.Radiobutton(tcard, text="Gemini (cloud)",
                       variable=self.mode_var, value="gemini",
                       bg=CARD, fg=TEXT, selectcolor=CARD,
                       activebackground=CARD, font=FONT_B).pack(anchor="w")
        tk.Radiobutton(tcard, text="Local (Whisper + Gemini)",
                       variable=self.mode_var, value="local",
                       bg=CARD, fg=TEXT, selectcolor=CARD,
                       activebackground=CARD, font=FONT_B).pack(anchor="w")

        rcol = tk.Frame(srow, bg=BG)
        rcol.pack(side="left", fill="both", expand=True, padx=(8, 0))

        skip_card = tk.Frame(rcol, bg=CARD, padx=14, pady=10)
        skip_card.pack(fill="x", pady=(0, 6))

        # Model selector + skip intro on same row
        top_row = tk.Frame(skip_card, bg=CARD)
        top_row.pack(fill="x")

        model_col = tk.Frame(top_row, bg=CARD)
        model_col.pack(side="left", fill="y", padx=(0, 16))
        tk.Label(model_col, text="Model", font=FONT_S, bg=CARD, fg=SUBTEXT).pack(anchor="w")
        self.model_var = tk.StringVar(value="gemini-3-flash-preview")
        MODELS = [
            ("gemini-3-flash-preview",      "Gemini 3 Flash Preview"),
            ("gemini-2.5-flash",            "Gemini 2.5 Flash"),
            ("gemini-2.5-flash-lite",       "Gemini 2.5 Flash-Lite"),
            ("gemini-2.5-pro",              "Gemini 2.5 Pro"),
            ("gemini-2.0-flash",            "Gemini 2.0 Flash"),
        ]
        model_display = [label for _, label in MODELS]
        self._model_map = {label: key for key, label in MODELS}
        self._model_cb = ttk.Combobox(model_col, values=model_display,
                                      state="readonly", width=22, font=FONT_B)
        self._model_cb.set("Gemini 3 Flash Preview")
        self._model_cb.pack(anchor="w")

        skip_col = tk.Frame(top_row, bg=CARD)
        skip_col.pack(side="left", fill="y")
        tk.Label(skip_col, text="Skip intro", font=FONT_S,
                 bg=CARD, fg=SUBTEXT).pack(anchor="w")
        skip_row = tk.Frame(skip_col, bg=CARD)
        skip_row.pack(fill="x")
        self.skip_min_var = tk.IntVar(value=0)
        self.skip_sec_var = tk.IntVar(value=0)
        tk.Spinbox(skip_row, textvariable=self.skip_min_var, from_=0, to=120,
                   increment=1, width=4, font=FONT_B, bg=CARD2, fg=TEXT,
                   insertbackground=TEXT, relief="flat", buttonbackground=CARD2
                   ).pack(side="left", ipady=3)
        tk.Label(skip_row, text="m", font=FONT_S, bg=CARD, fg=SUBTEXT).pack(side="left")
        tk.Spinbox(skip_row, textvariable=self.skip_sec_var, from_=0, to=59,
                   increment=1, width=4, font=FONT_B, bg=CARD2, fg=TEXT,
                   insertbackground=TEXT, relief="flat", buttonbackground=CARD2
                   ).pack(side="left", ipady=3, padx=(4, 0))
        tk.Label(skip_row, text="s", font=FONT_S, bg=CARD, fg=SUBTEXT).pack(side="left")

        ocard = tk.Frame(rcol, bg=CARD, padx=14, pady=10)
        ocard.pack(fill="x")
        tk.Label(ocard, text="Save .srt to", font=FONT_S, bg=CARD, fg=SUBTEXT).pack(anchor="w")
        self.outdir_var = tk.StringVar(value=os.path.expanduser("~/Desktop"))
        od_row = tk.Frame(ocard, bg=CARD)
        od_row.pack(fill="x")
        tk.Entry(od_row, textvariable=self.outdir_var, font=FONT_B,
                 bg=CARD2, fg=TEXT, insertbackground=TEXT,
                 relief="flat", bd=0, highlightthickness=1,
                 highlightcolor=ACCENT, highlightbackground="#333"
                 ).pack(side="left", fill="x", expand=True, ipady=4)
        tk.Button(od_row, text="Browse…", font=FONT_S, bg=CARD, fg=ACCENT,
                  activebackground=CARD, activeforeground=ACCENT2,
                  relief="flat", cursor="hand2", padx=8,
                  command=self._browse_outdir).pack(side="left", padx=(8, 0))

        # Progress
        self.progress_var = tk.DoubleVar(value=0)
        style = ttk.Style()
        style.theme_use("default")
        style.configure("Holo.Horizontal.TProgressbar",
                        troughcolor=CARD, background=ACCENT,
                        lightcolor=ACCENT, darkcolor=ACCENT2, bordercolor=BG)
        ttk.Progressbar(self, variable=self.progress_var, maximum=100,
                        style="Holo.Horizontal.TProgressbar"
                        ).pack(fill="x", padx=20, pady=(4, 0))
        self.prog_label = tk.Label(self, text="", font=FONT_S, bg=BG, fg=SUBTEXT)
        self.prog_label.pack(anchor="w", padx=22)

        # Buttons
        self.run_btn = tk.Button(
            self, text="✦  Generate subtitles",
            font=("Segoe UI", 12, "bold"),
            bg=ACCENT, fg=BG, activebackground=ACCENT2, activeforeground=BG,
            relief="flat", cursor="hand2", pady=9,
            command=self._start)
        self.run_btn.pack(fill="x", padx=20, pady=(10, 0))

        self.dl_btn = tk.Button(
            self, text="⬇  Download video (best quality)",
            font=("Segoe UI", 10, "bold"),
            bg=CARD2, fg=ACCENT, activebackground=CARD, activeforeground=ACCENT2,
            relief="flat", cursor="hand2", pady=7,
            command=self._download_video)
        self.dl_btn.pack(fill="x", padx=20, pady=(6, 0))

        # Log
        self._section("Log")
        self.log_box = scrolledtext.ScrolledText(
            self, font=FONT_MONO, bg="#08080f", fg=TEXT,
            insertbackground=TEXT, relief="flat", bd=0,
            state="disabled", height=12,
            cursor="arrow")
        self.log_box.pack(fill="both", expand=True, padx=20, pady=(4, 16))
        # Allow selecting and copying log text
        self.log_box.bind("<Control-a>", lambda e: (self.log_box.configure(state="normal"),
                                                     self.log_box.tag_add("sel", "1.0", "end"),
                                                     self.log_box.configure(state="disabled"), "break"))
        self.log_box.bind("<Control-c>", lambda e: None)
        self._log(f"Ready. Paste a URL or pick a file, enter your Gemini API key, and go.\n"
                  f"Default model: {GEMINI_MODEL}  |  Encoder: {_ENCODER}\n")

    def _section(self, text):
        tk.Label(self, text=text, font=FONT_S, bg=BG, fg=SUBTEXT
                 ).pack(anchor="w", padx=22, pady=(6, 2))

    def _browse_file(self):
        p = filedialog.askopenfilename(
            title="Select video or audio file",
            filetypes=[("Media files", "*.mp4 *.mkv *.webm *.avi *.mov *.m4a *.mp3 *.wav *.flac *.ogg"),
                       ("All files", "*.*")])
        if p:
            self.source_var.set(p)

    def _browse_outdir(self):
        d = filedialog.askdirectory(title="Select output folder")
        if d:
            self.outdir_var.set(d)

    def _log(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _set_progress(self, done, total):
        if total:
            pct = min(100.0, done / total * 100)
            self.progress_var.set(pct)
            if total == 100:
                self.prog_label.config(text=f"{pct:.0f}%")
            else:
                self.prog_label.config(text=f"Chunk {done}/{total}  ({pct:.0f}%)")

    def _start(self):
        source  = self.source_var.get().strip()
        api_key = self.apikey_var.get().strip()
        outdir  = self.outdir_var.get().strip()

        if not source:
            messagebox.showwarning("No source", "Please enter a URL or choose a file.")
            return
        if not api_key:
            messagebox.showwarning("No API key", "Please enter your Gemini API key.")
            return
        if not os.path.isdir(outdir):
            messagebox.showwarning("Bad output folder", f"Folder not found:\n{outdir}")
            return

        missing = _check_deps()
        if missing:
            messagebox.showerror("Missing packages",
                                 f"Run:\n  pip install {' '.join(missing)}\nThen restart holoSub.")
            return

        ok_ff, ok_fp = _check_ffmpeg()
        if not ok_ff or not ok_fp:
            messagebox.showerror("FFmpeg missing",
                                 "FFmpeg / FFprobe not found.\n\n"
                                 "Install:\n  pip install static-ffmpeg\nThen restart holoSub.")
            return

        is_url    = source.startswith("http://") or source.startswith("https://")
        task      = self.task_var.get()
        mode      = self.mode_var.get()
        skip_mins = self.skip_min_var.get() + self.skip_sec_var.get() / 60.0
        model     = self._model_map.get(self._model_cb.get(), GEMINI_MODEL)

        # Check faster-whisper is installed if local mode selected
        if mode == "local":
            try:
                import faster_whisper
            except ImportError:
                messagebox.showerror("Missing package",
                                     "Local mode requires faster-whisper.\n\n"
                                     "Run:\n  pip install faster-whisper\nThen restart holoSub.")
                return

        self.run_btn.configure(state="disabled", text="⏳  Validating key…")
        self._log("🔑  Validating Gemini API key…")

        def validate_and_run():
            ok, err_msg = validate_api_key(api_key, model)
            if not ok:
                short_msg = err_msg.split("\n")[0]  # first line only for the status label
                self.after(0, self._log, f"❌  {err_msg}")
                self.after(0, self._api_status.config, {"text": f"⚠  {short_msg}", "fg": WARN})
                self.after(0, self.run_btn.configure,
                           {"state": "normal", "text": "✦  Generate subtitles"})
                return

            self.after(0, self._api_status.config, {"text": "✅  API key valid", "fg": SUCCESS})
            self.after(0, self._log, "✅  API key valid")
            self.after(0, self.run_btn.configure, {"text": "⏳  Working…"})
            self.after(0, self.progress_var.set, 0)
            self.after(0, self.prog_label.config, {"text": ""})
            self.after(0, self._log,
                       f"▶ Source={'URL' if is_url else 'file'}  task={task}  mode={mode}  skip={skip_mins:.1f}min  model={model}")

            threading.Thread(
                target=run_pipeline,
                args=(source, is_url, task, api_key, outdir, skip_mins, model, mode,
                      lambda m: self.after(0, self._log, m),
                      lambda d, t: self.after(0, self._set_progress, d, t),
                      self._on_done),
                daemon=True
            ).start()

        threading.Thread(target=validate_and_run, daemon=True).start()

    def _download_video(self):
        source = self.source_var.get().strip()
        outdir = self.outdir_var.get().strip()

        if not source:
            messagebox.showwarning("No source", "Please enter a URL to download.")
            return
        if not source.startswith("http://") and not source.startswith("https://"):
            messagebox.showwarning("URLs only", "Video download only works with URLs.")
            return
        if not os.path.isdir(outdir):
            messagebox.showwarning("Bad output folder", f"Folder not found:\n{outdir}")
            return

        self.dl_btn.configure(state="disabled", text="⏳  Downloading…")
        self._log("⬇  Starting download (best quality)…")

        def do_dl():
            try:
                import yt_dlp

                def progress_hook(d):
                    if d.get("status") == "downloading":
                        msg = f"   {d.get('_percent_str','').strip()}  {d.get('_speed_str','').strip()}"
                        self.after(0, self._log, msg)

                with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
                    info = ydl.extract_info(source, download=False)
                    title = info.get("title", "video")

                safe_title = sanitise_folder_name(title)
                video_dir  = os.path.join(outdir, safe_title)
                os.makedirs(video_dir, exist_ok=True)

                ydl_opts = {
                    "format": "bestvideo+bestaudio/best",
                    "outtmpl": os.path.join(video_dir, "%(title)s.%(ext)s"),
                    "merge_output_format": "mp4",
                    "quiet": True, "no_warnings": True,
                    "progress_hooks": [progress_hook],
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([source])
                self.after(0, self._on_download_done, video_dir, True)
            except Exception as e:
                self.after(0, self._log, f"❌  Download error: {e}")
                self.after(0, self._on_download_done, None, False)

        threading.Thread(target=do_dl, daemon=True).start()

    def _on_download_done(self, out_path, success):
        self.dl_btn.configure(state="normal", text="⬇  Download video (best quality)")
        if success:
            self._log(f"✅  Video saved to:\n   {out_path}")
            messagebox.showinfo("Download complete ✨", f"Video saved to:\n\n{out_path}")
        else:
            messagebox.showerror("Download failed", "Check the log for details.")

    def _on_done(self, out_file):
        self.after(0, self._finish, out_file)

    def _finish(self, out_file):
        self.run_btn.configure(state="normal", text="✦  Generate subtitles")
        if out_file:
            self.progress_var.set(100)
            self.prog_label.config(text="Complete!", fg=SUCCESS)
            messagebox.showinfo("Done ✨",
                                f"Subtitle file saved:\n\n{out_file}\n\n"
                                "Load in VLC: Subtitle → Add Subtitle File")
        else:
            self.prog_label.config(text="Failed — see log above.", fg=WARN)


if __name__ == "__main__":
    app = HoloSubApp()
    app.mainloop()
