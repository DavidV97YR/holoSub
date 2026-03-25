"""
holoSub — auto subtitle generator for hololive VODs & YouTube videos
powered by Google Gemini

requirements:
    pip install google-genai yt-dlp static-ffmpeg

No separate FFmpeg install needed — static-ffmpeg bundles its own binary.
"""

from gui import HoloSubApp

if __name__ == "__main__":
    app = HoloSubApp()
    app.mainloop()
