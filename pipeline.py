import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import traceback

from config import CHUNK_SECS, MAX_WORKERS, UPLOAD_WORKERS
from downloader import download_source
from ffmpeg_utils import get_duration, split_chunks, reencode_chunk
from gemini_client import GeminiFileUploader, process_chunk
from prompt import make_instruction, _YTDLP_REPLACEMENTS
from srt import build_srt
from whisper_pipeline import transcribe_local


def sanitise_folder_name(title):
    title = re.sub(r'[【】「」『』〔〕]', ' ', title)
    title = re.sub(r'#\S+', '', title)
    title = re.sub(r'[\\/*?:"<>|]', '', title)
    title = re.sub(r'\s+', ' ', title).strip()
    if len(title) > 60:
        title = title[:60].rsplit(' ', 1)[0].strip()
    return title or "video"


def transcribe_with_gemini(source_path, task, api_key, title,
                            skip_secs, resume_dir, tmp_dir, model, log, progress_cb,
                            stop_event=None):
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

    if stop_event and stop_event.is_set():
        return []

    # 3. Upload all chunks first (UPLOAD_WORKERS parallel, independent of Gemini RPM)
    log(f"☁️   Uploading {n} chunks to Files API…")
    client   = genai.Client(api_key=api_key)
    uploader = GeminiFileUploader(client)

    def do_upload(idx, chunk_path, offset_ms):
        if stop_event and stop_event.is_set():
            return
        label = f"chunk {idx+1}/{n}"
        try:
            uploader.upload(chunk_path, log, label)
        except Exception as e:
            log(f"❌  {label}: upload failed — {e}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as ex:
        futs = [ex.submit(do_upload, idx, cp, off)
                for idx, (cp, off) in enumerate(small_chunks)]
        concurrent.futures.wait(futs)

    if stop_event and stop_event.is_set():
        uploader.delete_all(log)
        return []

    # 4. Generate subtitles (MAX_WORKERS parallel — respects RPM)
    instruction = make_instruction(task, title)
    results     = {}
    completed   = [0]
    lock        = threading.Lock()

    def do_subtitle(i, chunk_path, offset_ms):
        if stop_event and stop_event.is_set():
            return i, []
        label        = f"chunk {i+1}/{n}"
        resume_path  = os.path.join(resume_dir, f"chunk_{i:04d}.json")
        _dur         = get_duration(chunk_path)
        chunk_dur_ms = int(_dur * 1000) if _dur else CHUNK_SECS * 1000
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


def run_pipeline(source, is_url, task, api_key, outdir, skip_mins, model, mode,
                 log, progress_cb, done_cb, stop_event=None, vad_filter=True):
    tmp = None
    try:
        tmp = tempfile.mkdtemp()

        if is_url:
            source_path, title = download_source(source, tmp, log)
        else:
            source_path = source
            title = os.path.splitext(os.path.basename(source))[0]

        if stop_event and stop_event.is_set():
            log("🛑  Cancelled before processing.")
            done_cb(None)
            return

        safe_title = sanitise_folder_name(title)
        video_dir  = os.path.join(outdir, safe_title)
        resume_dir = os.path.join(video_dir, ".resume")
        os.makedirs(video_dir, exist_ok=True)
        log(f"📁  Output: {video_dir}")

        # Save processing metadata so the Resub tab can auto-detect settings
        os.makedirs(resume_dir, exist_ok=True)
        meta_path = os.path.join(resume_dir, "_meta.json")
        try:
            with open(meta_path, "w", encoding="utf-8") as mf:
                json.dump({
                    "skip_secs": int(skip_mins * 60),
                    "mode": mode,
                    "task": task,
                    "title": title,
                    "model": model,
                    "vad_filter": vad_filter,
                }, mf)
        except Exception:
            pass

        if mode in ("local", "local_only"):
            translator = "ollama" if mode == "local_only" else "gemini"
            entries = transcribe_local(
                source_path, task, api_key, title,
                int(skip_mins * 60), resume_dir, tmp, model, log, progress_cb,
                stop_event=stop_event, translator=translator,
                vad_filter=vad_filter,
            )
        else:
            entries = transcribe_with_gemini(
                source_path, task, api_key, title,
                int(skip_mins * 60), resume_dir, tmp, model, log, progress_cb,
                stop_event=stop_event
            )

        if stop_event and stop_event.is_set():
            log("🛑  Cancelled — partial subtitles will not be saved.")
            done_cb(None)
            return

        if not entries:
            log("❌  No subtitles returned — check log above.")
            done_cb(None)
            return

        srt = build_srt(entries)

        # Try to match the .srt filename to any existing video file in video_dir
        # so MPC-HC auto-loads it without manual selection.
        srt_name = safe_title
        for f in os.listdir(video_dir):
            ext = os.path.splitext(f)[1].lower()
            if ext in (".mp4", ".mkv", ".webm", ".avi", ".mov"):
                srt_name = os.path.splitext(f)[0]
                log(f"🔗  Matching .srt name to video: {f}")
                break
        else:
            # No video in folder yet — mirror yt-dlp's full-width unicode replacements
            srt_name = ''.join(_YTDLP_REPLACEMENTS.get(c, c) for c in title).strip() or safe_title

        out_file = os.path.join(video_dir, f"{srt_name}.srt")
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(srt)

        log(f"\n✨  Done! {len(entries)} lines → {out_file}")
        done_cb(out_file)

    except Exception as e:
        log(f"\n❌  Error: {e}\n{traceback.format_exc()}")
        done_cb(None)
    finally:
        if tmp and os.path.isdir(tmp):
            try:
                shutil.rmtree(tmp)
            except Exception:
                pass


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
    ok_ff = ok_fp = False
    for name in ("ffmpeg", "ffprobe"):
        try:
            subprocess.run([name, "-version"], capture_output=True, timeout=5)
            if name == "ffmpeg":
                ok_ff = True
            else:
                ok_fp = True
        except Exception:
            pass
    return ok_ff, ok_fp
