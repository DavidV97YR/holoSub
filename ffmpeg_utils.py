import json
import os
import shutil
import subprocess
import threading

from config import CHUNK_SECS, REENCODE_HEIGHT, REENCODE_FPS, REENCODE_ABR, REENCODE_BITRATE_KB


# ── static-ffmpeg ──────────────────────────────────────────────────────────────

def _init_ffmpeg():
    try:
        import static_ffmpeg
        static_ffmpeg.add_paths(weak=False)
        path = shutil.which("ffmpeg") or "ffmpeg"
        print(f"[holoSub] FFmpeg: {path}")
    except ImportError:
        print("[holoSub] static-ffmpeg not found — using system FFmpeg")

_init_ffmpeg()


# ── hardware encoder detection ────────────────────────────────────────────────

def _get_working_encoder():
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

_ENCODER      = "libx264"   # safe default shown until probe completes
_encoder_lock = threading.Lock()

def _probe_encoder_async():
    """Run encoder detection in background so GUI opens immediately."""
    global _ENCODER
    result = _get_working_encoder()
    with _encoder_lock:
        _ENCODER = result
    print(f"[holoSub] Encoder: {_ENCODER}")

threading.Thread(target=_probe_encoder_async, daemon=True).start()


# ── ffprobe helpers ───────────────────────────────────────────────────────────

def get_duration(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
            capture_output=True, text=True, timeout=30
        )
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return None


def has_video_stream(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "v", str(path)],
            capture_output=True, text=True, timeout=15
        )
        return len(json.loads(r.stdout).get("streams", [])) > 0
    except Exception:
        return False


# ── ffmpeg pipeline ────────────────────────────────────────────────────────────

def split_chunks(source_path, split_dir, skip_secs, log):
    """
    Stream-copy source into CHUNK_SECS segments with -reset_timestamps 1.
    Validates existing chunks by duration. Returns [(path, start_ms)].
    """
    import math

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


def extract_audio(source_path, out_path, log, start_secs=0):
    """Extract audio from video as 16kHz mono WAV for Whisper.
    start_secs: begin extraction here (fast seek); caller must add this offset
    back to all Whisper timestamps so they align with the original video timeline.
    """
    seek_args = ["-ss", str(start_secs)] if start_secs > 0 else []
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           *seek_args,
           "-i", str(source_path),
           "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", out_path]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
        log(f"🎵  Audio extracted")
        return True
    except subprocess.CalledProcessError as e:
        log(f"❌  Audio extraction failed: {e.stderr.decode('utf-8','replace')[-200:]}")
        return False
