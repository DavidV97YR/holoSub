# ── colour palette (matches holoIndex / holoVault theme) ─────────────────────
BG        = "#0a0612"
CARD      = "#110d1f"
CARD2     = "#1a1430"
ACCENT    = "#a855f7"       # purple primary
ACCENT2   = "#ff6eb4"       # pink secondary
ACCENT3   = "#22d3ee"       # cyan tertiary
TEXT      = "#e8d5f5"
SUBTEXT   = "#9070b0"
SUCCESS   = "#4ecca3"
WARN      = "#ffb347"
BORDER    = "#2a1f45"       # purple-tinted border for inputs
LOG_BG    = "#07050e"       # log box background
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
REENCODE_ABR        = 16000
REENCODE_BITRATE_KB = 35        # ~35 KB/s video
WHISPER_MODEL       = "large-v3"
TRANSLATE_BATCH     = 200       # segments per Gemini translation request
RETRY_DELAYS        = [30, 60, 90, 120, 150]
PROMPT_VERSION      = 25        # bump to invalidate resume cache on prompt change
MAX_LOG_LINES       = 1200      # trim log box when it exceeds this; keeps GUI responsive

# ── Ollama (fully local translation) ─────────────────────────────────────────
OLLAMA_URL              = "http://localhost:11434"
OLLAMA_MODEL            = "qwen3:14b"
OLLAMA_TRANSLATE_BATCH  = 50    # smaller batches for local model responsiveness
