import json
import os
import threading
import time

from config import GEMINI_MODEL, RETRY_DELAYS, PROMPT_VERSION
from ffmpeg_utils import get_duration
from prompt import _GEMINI_RESPONSE_SCHEMA, parse_response


class GeminiFileUploader:
    """
    Thread-safe Files API uploader with caching and ACTIVE-state polling.
    One upload per file path; concurrent callers for the same path wait on an
    Event rather than racing through the cache gap.
    """
    def __init__(self, client):
        self._client  = client
        self._cache   = {}   # path → uploaded file object
        self._events  = {}   # path → threading.Event (upload in progress)
        self._lock    = threading.Lock()

    def upload(self, local_path, log, label):
        from google.genai.types import UploadFileConfig, FileState

        while True:
            with self._lock:
                if local_path in self._cache:
                    return self._cache[local_path]
                if local_path in self._events:
                    event = self._events[local_path]
                else:
                    event = threading.Event()
                    self._events[local_path] = event
                    break
            event.wait()

        try:
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
        finally:
            with self._lock:
                self._events.pop(local_path, None)
            event.set()

    def delete_all(self, log):
        with self._lock:
            files, self._cache = list(self._cache.values()), {}
        for f in files:
            try:
                self._client.files.delete(name=f.name)
            except Exception:
                pass


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
                    response_schema=_GEMINI_RESPONSE_SCHEMA,
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
            if entries and chunk_duration_ms > 0:
                min_ms = offset_ms - 2000
                max_ms = offset_ms + chunk_duration_ms + 30000
                in_range = [e for e in entries if min_ms <= e["start_ms"] <= max_ms]
                out_of_range = len(entries) - len(in_range)
                if out_of_range > 0:
                    log_fn(f"ℹ️  {label}: {out_of_range}/{len(entries)} entries outside expected range [{min_ms/1000:.0f}s-{max_ms/1000:.0f}s], keeping in-range entries")
                if len(in_range) == 0 and attempt < 4:
                    log_fn(f"⚠️  {label}: all entries out of range — retrying")
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
                entries = in_range if in_range else entries

            # Coverage check — if no entries fall in the last 30% of the chunk's
            # time window, Gemini likely stopped early and missed speech at the end.
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
                if is_rate and rate_limit_until is not None and rate_limit_lock is not None:
                    with rate_limit_lock:
                        rate_limit_until[0] = max(rate_limit_until[0], time.time() + wait)
                time.sleep(wait)
                if attempt == 4:
                    log_fn(f"❌  {label}: retries exhausted")
                    return i, []
            else:
                log_fn(f"❌  {label}: {err[:200]}")
                return i, []

    return i, []
