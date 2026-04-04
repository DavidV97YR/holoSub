"""Resub pipeline — re-process selected cloud chunks or local translation batches."""

import json
import os
import re
import shutil
import tempfile
import threading
import traceback

from config import CHUNK_SECS, OLLAMA_TRANSLATE_BATCH, PROMPT_VERSION, TRANSLATE_BATCH
from ffmpeg_utils import get_duration, split_chunks, reencode_chunk
from gemini_client import GeminiFileUploader, process_chunk
from prompt import make_instruction
from srt import build_srt
from whisper_pipeline import translate_with_gemini, translate_with_ollama, _unload_ollama_model


def _fmt_time(ms):
    """Format milliseconds as H:MM:SS or M:SS for display."""
    s_total = max(0, ms) // 1000
    h, rem = divmod(s_total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def scan_folder(resume_dir):
    """Scan a .resume folder and return available chunks/batches.

    Returns dict:
        mode:      "cloud" | "local" | None
        items:     list of {index, label, start_ms, end_ms, num_lines}
        skip_secs: detected skip seconds
        task:      original task ("translate" / "transcribe")
        title:     original video title
        model:     original model name
    """
    result = {
        "mode": None, "items": [], "skip_secs": 0,
        "task": "", "title": "", "model": "", "translator": "gemini",
    }

    # ── read metadata saved by pipeline.py ──
    meta_path = os.path.join(resume_dir, "_meta.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            result["skip_secs"] = meta.get("skip_secs", 0)
            result["task"] = meta.get("task", "")
            result["title"] = meta.get("title", "")
            result["model"] = meta.get("model", "")
            result["_raw_mode"] = meta.get("mode", "")
        except Exception:
            pass

    # ── cloud chunks ──
    cloud_items = []
    i = 0
    while True:
        path = os.path.join(resume_dir, f"chunk_{i:04d}.json")
        if not os.path.exists(path):
            break
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("entries", [])
            if entries:
                start_ms = min(e["start_ms"] for e in entries)
                end_ms = max(e["end_ms"] for e in entries)
            else:
                start_ms = i * CHUNK_SECS * 1000
                end_ms = (i + 1) * CHUNK_SECS * 1000
            cloud_items.append({
                "index": i,
                "num_lines": len(entries),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "label": (f"Chunk {i+1}:  {_fmt_time(start_ms)} \u2192 "
                          f"{_fmt_time(end_ms)}   ({len(entries)} lines)"),
            })
        except Exception:
            cloud_items.append({
                "index": i, "num_lines": 0,
                "start_ms": i * CHUNK_SECS * 1000,
                "end_ms": (i + 1) * CHUNK_SECS * 1000,
                "label": f"Chunk {i+1}:  (corrupted cache)",
            })
        i += 1

    # ── local whisper transcript → translation batches ──
    whisper_path = os.path.join(resume_dir, "whisper_transcript.json")
    local_items = []
    meta_mode = result.get("_raw_mode", "")
    batch_size = OLLAMA_TRANSLATE_BATCH if meta_mode == "local_only" else TRANSLATE_BATCH

    if os.path.exists(whisper_path):
        try:
            with open(whisper_path, "r", encoding="utf-8") as f:
                wdata = json.load(f)
            if isinstance(wdata, dict):
                segments = wdata.get("segments", [])
                if not result["skip_secs"]:
                    result["skip_secs"] = wdata.get("silence_secs", 0)
            else:
                segments = wdata

            # Apply same skip filter that _resub_local uses so batch
            # indices here match the indices used during re-translation.
            skip = result["skip_secs"]
            if skip > 0:
                skip_ms = int(skip * 1000)
                segments = [s for s in segments if s.get("start_ms", 0) >= skip_ms]

            for batch_idx in range(0, len(segments), batch_size):
                batch = segments[batch_idx:batch_idx + batch_size]
                if not batch:
                    continue
                start_ms = batch[0].get("start_ms", 0)
                end_ms = batch[-1].get("end_ms", 0)
                num = batch_idx // batch_size
                local_items.append({
                    "index": num,
                    "batch_start": batch_idx,
                    "batch_end": min(batch_idx + batch_size, len(segments)),
                    "num_lines": len(batch),
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "label": (f"Batch {num+1}:  {_fmt_time(start_ms)} \u2192 "
                              f"{_fmt_time(end_ms)}   ({len(batch)} segments)"),
                })
        except Exception:
            pass

    # ── pick the mode that has data ──
    # _meta.json stores "gemini", "local", or "local_only"; normalise to "cloud"/"local"
    meta_mode = result.get("_raw_mode", "")
    if meta_mode == "local_only":
        result["translator"] = "ollama"
    if cloud_items and not local_items:
        result["mode"] = "cloud"
        result["items"] = cloud_items
    elif local_items and not cloud_items:
        result["mode"] = "local"
        result["items"] = local_items
    elif cloud_items and local_items:
        # both present — use metadata hint, or default to cloud
        if meta_mode in ("local", "local_only"):
            result["mode"] = "local"
            result["items"] = local_items
        else:
            result["mode"] = "cloud"
            result["items"] = cloud_items

    return result


def find_video_in_folder(done_dir):
    """Try to find a video/audio file in the done folder."""
    for f in os.listdir(done_dir):
        ext = os.path.splitext(f)[1].lower()
        if ext in (".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4a", ".mp3", ".wav"):
            return os.path.join(done_dir, f)
    return None


# ═════════════════════════════════════════════════════════════════════════════
#  Main entry point
# ═════════════════════════════════════════════════════════════════════════════

def run_resub(done_dir, selected_indices, source_path, mode, task, api_key,
              title, skip_secs, model, log, progress_cb, done_cb,
              stop_event=None, translator="gemini"):
    """Re-process selected chunks/batches and rebuild SRT."""
    tmp = None
    try:
        resume_dir = os.path.join(done_dir, ".resume")
        if not os.path.isdir(resume_dir):
            log("\u274c  No .resume folder found in done directory")
            done_cb(None)
            return

        tmp = tempfile.mkdtemp()

        if mode == "cloud":
            entries = _resub_cloud(
                source_path, selected_indices, task, api_key, title,
                skip_secs, resume_dir, tmp, model, log, progress_cb, stop_event,
            )
        else:
            entries = _resub_local(
                source_path, selected_indices, task, api_key, title,
                skip_secs, resume_dir, tmp, model, log, progress_cb, stop_event,
                translator=translator,
            )

        if stop_event and stop_event.is_set():
            log("\U0001f6d1  Cancelled.")
            done_cb(None)
            return

        if not entries:
            log("\u274c  No entries after resub \u2014 check log above.")
            done_cb(None)
            return

        srt_text = build_srt(entries)

        # Overwrite existing SRT or create a new one
        srt_path = None
        for f in os.listdir(done_dir):
            if f.lower().endswith(".srt"):
                srt_path = os.path.join(done_dir, f)
                break
        if not srt_path:
            safe = re.sub(r'[\\/*?:"<>|]', '', title or "resub")
            srt_path = os.path.join(done_dir, f"{safe}.srt")

        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(srt_text)

        log(f"\n\u2728  Resub complete!  {len(entries)} lines \u2192 {srt_path}")
        done_cb(srt_path)

    except Exception as e:
        log(f"\n\u274c  Error: {e}\n{traceback.format_exc()}")
        done_cb(None)
    finally:
        if tmp and os.path.isdir(tmp):
            try:
                shutil.rmtree(tmp)
            except Exception:
                pass


# ═════════════════════════════════════════════════════════════════════════════
#  Cloud resub
# ═════════════════════════════════════════════════════════════════════════════

def _resub_cloud(source_path, selected_indices, task, api_key, title,
                 skip_secs, resume_dir, tmp_dir, model, log, progress_cb,
                 stop_event):
    """Delete selected chunk caches, re-split, re-encode, re-upload, re-process."""
    from google import genai

    selected = set(selected_indices)

    # 1. Delete caches for selected chunks
    for idx in sorted(selected):
        path = os.path.join(resume_dir, f"chunk_{idx:04d}.json")
        if os.path.exists(path):
            os.remove(path)
            log(f"\U0001f5d1\ufe0f  Cleared cache: chunk {idx + 1}")

    # 2. Split video into chunks (fast stream-copy)
    split_dir = os.path.join(tmp_dir, "splits")
    os.makedirs(split_dir, exist_ok=True)
    log("\u2702\ufe0f  Splitting video\u2026")
    chunks = split_chunks(source_path, split_dir, skip_secs, log)
    if not chunks:
        log("\u274c  No chunks produced")
        return []
    n = len(chunks)

    # 3. Identify which chunks actually need work (no cache file)
    needs_work = set()
    for i in range(n):
        if not os.path.exists(os.path.join(resume_dir, f"chunk_{i:04d}.json")):
            needs_work.add(i)

    if not needs_work:
        log("\u2139\ufe0f  All chunks are cached \u2014 loading results")
    else:
        log(f"\U0001f504  Re-processing {len(needs_work)} of {n} chunks")

    # 4. Re-encode only the chunks that need work
    small_chunks = {}
    for i, (cp, off) in enumerate(chunks):
        if i in needs_work:
            log(f"\U0001f504  Re-encoding chunk {i + 1}/{n}\u2026")
            small_chunks[i] = (reencode_chunk(cp, log), off)

    if stop_event and stop_event.is_set():
        return []

    # 5. Upload + process the needed chunks
    if needs_work:
        _rate_limit_until = [0.0]
        _rate_limit_lock = threading.Lock()

        client = genai.Client(api_key=api_key)
        uploader = GeminiFileUploader(client)
        instruction = make_instruction(task, title)

        # Upload
        for i in sorted(needs_work):
            if stop_event and stop_event.is_set():
                break
            cp, off = small_chunks[i]
            label = f"chunk {i + 1}/{n}"
            try:
                uploader.upload(cp, log, label)
            except Exception as e:
                log(f"\u274c  {label}: upload failed \u2014 {e}")

        if stop_event and stop_event.is_set():
            uploader.delete_all(log)
            return []

        # Process
        done_count = [0]
        total_selected = len(needs_work)
        for i in sorted(needs_work):
            if stop_event and stop_event.is_set():
                break
            cp, off = small_chunks[i]
            label = f"chunk {i + 1}/{n}"
            resume_path = os.path.join(resume_dir, f"chunk_{i:04d}.json")
            _dur = get_duration(cp)
            chunk_dur_ms = int(_dur * 1000) if _dur else CHUNK_SECS * 1000

            _, entries = process_chunk(
                i, cp, off, chunk_dur_ms, instruction,
                resume_path, api_key, uploader, model, label, log,
                _rate_limit_until, _rate_limit_lock,
            )
            done_count[0] += 1
            progress_cb(done_count[0], total_selected)
            if entries:
                log(f"\u2705  Chunk {i + 1}: {len(entries)} lines")
            else:
                log(f"\u26a0\ufe0f  Chunk {i + 1}: no subtitles returned")

        log("\U0001f9f9  Cleaning up Files API\u2026")
        uploader.delete_all(log)

    # 6. Collect ALL cached results (old + fresh)
    all_entries = []
    for i in range(n):
        cache_path = os.path.join(resume_dir, f"chunk_{i:04d}.json")
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                all_entries.extend(data.get("entries", []))
            except Exception:
                pass

    return all_entries


# ═════════════════════════════════════════════════════════════════════════════
#  Local resub  (re-translate selected 200-segment batches)
# ═════════════════════════════════════════════════════════════════════════════

def _resub_local(source_path, selected_indices, task, api_key, title,
                 skip_secs, resume_dir, tmp_dir, model, log, progress_cb,
                 stop_event, translator="gemini"):
    """Surgically re-translate selected translation batches."""
    whisper_path = os.path.join(resume_dir, "whisper_transcript.json")
    trans_path = os.path.join(resume_dir, "translation.json")

    if not os.path.exists(whisper_path):
        log("\u274c  No Whisper transcript found \u2014 run Generate first")
        return []

    with open(whisper_path, "r", encoding="utf-8") as f:
        wdata = json.load(f)
    all_segments = wdata.get("segments", []) if isinstance(wdata, dict) else wdata

    if not all_segments:
        log("\u274c  Whisper transcript is empty")
        return []

    # Drop intro segments
    if skip_secs > 0:
        skip_ms = int(skip_secs * 1000)
        all_segments = [s for s in all_segments if s["start_ms"] >= skip_ms]

    if task != "translate":
        log("\u2139\ufe0f  Task is transcribe-only \u2014 returning Whisper segments as-is")
        return all_segments

    # Load existing translation results
    existing = []
    if os.path.exists(trans_path):
        try:
            with open(trans_path, "r", encoding="utf-8") as f:
                tdata = json.load(f)
            existing = tdata.get("entries", [])
        except Exception:
            pass

    if not existing:
        log("\u2139\ufe0f  No existing translations \u2014 translating all segments")
        if translator == "ollama":
            return translate_with_ollama(
                all_segments, title, resume_dir, log, progress_cb,
                stop_event=stop_event,
            )
        return translate_with_gemini(
            all_segments, title, api_key, model, resume_dir, log, progress_cb,
        )

    # Re-translate only the selected batches.
    # Process in descending order so that slice replacements with different
    # lengths don't shift the indices of batches still to be processed.
    batch_size = OLLAMA_TRANSLATE_BATCH if translator == "ollama" else TRANSLATE_BATCH
    selected = set(selected_indices)
    total_to_do = len(selected)
    done_count = 0

    for batch_idx in sorted(selected, reverse=True):
        if stop_event and stop_event.is_set():
            return []

        seg_start = batch_idx * batch_size
        seg_end = min(seg_start + batch_size, len(all_segments))
        batch_segments = all_segments[seg_start:seg_end]

        if not batch_segments:
            continue

        log(f"\U0001f504  Re-translating batch {batch_idx + 1}  "
            f"({len(batch_segments)} segments, "
            f"{_fmt_time(batch_segments[0]['start_ms'])} \u2192 "
            f"{_fmt_time(batch_segments[-1]['end_ms'])})\u2026")

        batch_resume = os.path.join(tmp_dir, f"_resub_batch_{batch_idx}")
        os.makedirs(batch_resume, exist_ok=True)
        if translator == "ollama":
            new_entries = translate_with_ollama(
                batch_segments, title, batch_resume, log,
                lambda d, t: None, stop_event=stop_event,
            )
        else:
            new_entries = translate_with_gemini(
                batch_segments, title, api_key, model, batch_resume, log,
                lambda d, t: None,
            )

        # Replace in the existing list — clamp to actual list bounds
        if new_entries:
            replace_start = min(seg_start, len(existing))
            replace_end = min(seg_end, len(existing))
            existing[replace_start:replace_end] = new_entries

        done_count += 1
        progress_cb(done_count, total_to_do)

    # Persist updated translation cache
    try:
        with open(trans_path, "w", encoding="utf-8") as f:
            json.dump({"prompt_version": PROMPT_VERSION, "entries": existing},
                      f, ensure_ascii=False)
        log("\U0001f4be  Updated translation cache")
    except Exception as e:
        log(f"\u26a0\ufe0f  Could not save translation cache: {e}")

    return existing


