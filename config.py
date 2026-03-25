# ── colour palette ────────────────────────────────────────────────────────────
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

# ── processing constants ──────────────────────────────────────────────────────
GEMINI_MODEL        = "gemini-3-flash-preview"
CHUNK_SECS          = 3 * 60    # ~3 min — smaller chunks improve timestamp accuracy
MAX_WORKERS         = 1         # single Gemini worker — prevents cascading rate limit failures
UPLOAD_WORKERS      = 4         # parallel Files API uploads (independent of RPM)
REENCODE_HEIGHT     = 360
REENCODE_FPS        = 1
REENCODE_ABR        = "16000"
REENCODE_BITRATE_KB = 35        # ~35 KB/s video
RETRY_DELAYS        = [30, 60, 90, 120, 150]
PROMPT_VERSION      = 19        # bump to invalidate resume cache on prompt change
MAX_LOG_LINES       = 1200      # trim log box when it exceeds this; keeps GUI responsive
