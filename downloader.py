import os
import re

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")  # strips ANSI colour codes from yt-dlp speed/ETA strings


def _make_ytdlp_progress_hook(log):
    """Return a yt-dlp progress hook that logs download % every 5 points."""
    last_pct = [-1]
    def hook(d):
        if d.get("status") == "downloading":
            downloaded = d.get("downloaded_bytes", 0)
            total      = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            if total and total > 0:
                pct = int(downloaded / total * 100)
                if pct // 5 > last_pct[0]:
                    last_pct[0] = pct // 5
                    speed = _ANSI_RE.sub("", d.get("_speed_str", "")).strip()
                    eta   = _ANSI_RE.sub("", d.get("_eta_str",   "")).strip()
                    log(f"   {pct}%  {speed}  ETA {eta}")
    return hook


def download_source(url, out_dir, log):
    """Download H.264 ≤480p + best audio. Returns (path, title)."""
    import yt_dlp
    log("📥  Downloading video…")

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
        "progress_hooks": [_make_ytdlp_progress_hook(log)],
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
