import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import ffmpeg_utils
from config import (ACCENT, ACCENT2, ACCENT3, BG, BORDER, CARD, CARD2, FONT_B,
                    FONT_MONO, FONT_S, GEMINI_MODEL, LOG_BG, MAX_LOG_LINES,
                    SUBTEXT, SUCCESS, TEXT, WARN)
from downloader import _make_ytdlp_progress_hook
from gemini_client import validate_api_key
from pipeline import _check_deps, _check_ffmpeg, run_pipeline, sanitise_folder_name
from whisper_pipeline import check_ollama


def _make_gradient_logo():
    """Render 'holoSub' as a smooth-gradient PNG using Pillow."""
    from PIL import Image, ImageDraw, ImageFont

    font_size = 52
    # Prefer bundled Playfair Display, fall back to Georgia Bold
    assets = os.path.join(os.path.dirname(__file__), "assets")
    playfair = os.path.join(assets, "PlayfairDisplay-Bold.ttf")
    try:
        fnt = ImageFont.truetype(playfair, font_size)
        try:
            fnt.set_variation_by_name("Bold")
        except Exception:
            pass
    except OSError:
        fnt = ImageFont.truetype("C:/Windows/Fonts/georgiab.ttf", font_size)

    # Measure each segment
    tmp = Image.new("RGBA", (1, 1))
    td = ImageDraw.Draw(tmp)
    bb_holo = td.textbbox((0, 0), "holo", font=fnt)
    bb_sub = td.textbbox((0, 0), "Sub", font=fnt)
    w_holo = bb_holo[2] - bb_holo[0]
    w_sub = bb_sub[2] - bb_sub[0]
    total_w = w_holo + w_sub + 4          # small gap
    h = max(bb_holo[3], bb_sub[3]) - min(bb_holo[1], bb_sub[1]) + 4

    # Render white text on transparent background
    img = Image.new("RGBA", (total_w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    y_off = -min(bb_holo[1], bb_sub[1])
    draw.text((0, y_off), "holo", font=fnt, fill=(255, 255, 255, 255))
    draw.text((w_holo + 4, y_off), "Sub", font=fnt, fill=(255, 255, 255, 255))

    # Build a horizontal gradient: pink → purple over "holo", purple → cyan over "Sub"
    pink = (255, 110, 180)
    purple = (168, 85, 247)
    cyan = (34, 211, 238)
    pixels = img.load()
    for x in range(total_w):
        if x <= w_holo:
            t = x / max(w_holo, 1)
            r = int(pink[0] + (purple[0] - pink[0]) * t)
            g = int(pink[1] + (purple[1] - pink[1]) * t)
            b = int(pink[2] + (purple[2] - pink[2]) * t)
        else:
            t = (x - w_holo) / max(w_sub + 4, 1)
            r = int(purple[0] + (cyan[0] - purple[0]) * t)
            g = int(purple[1] + (cyan[1] - purple[1]) * t)
            b = int(purple[2] + (cyan[2] - purple[2]) * t)
        for y in range(h):
            _, _, _, a = pixels[x, y]
            if a > 0:
                pixels[x, y] = (r, g, b, a)

    # Scale down 2x for crispness (rendered at 2x)
    return img


class HoloSubApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("holoSub \u2726 Auto Subtitle Generator")
        self.geometry("1000x860")
        self.resizable(True, True)
        self.configure(bg=BG)
        self._stop_event = threading.Event()
        self._build_ui()

    def _build_ui(self):
        # ── Shared styles ──
        style = ttk.Style()
        style.theme_use("default")
        style.configure("Holo.Horizontal.TProgressbar",
                        troughcolor=CARD, background=ACCENT,
                        lightcolor=ACCENT, darkcolor=ACCENT2, bordercolor=BG)
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=CARD, foreground=SUBTEXT,
                        padding=[16, 7], font=("Segoe UI", 10, "bold"))
        style.map("TNotebook.Tab",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", BG)])

        # ── Notebook ──
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)

        gen_frame = tk.Frame(notebook, bg=BG)
        notebook.add(gen_frame, text="  \u2726 Generate  ")
        self._build_main_tab(gen_frame)

        from resub_tab import ResubTab
        self.resub_tab = ResubTab(notebook, self)
        notebook.add(self.resub_tab, text="  \U0001f504 Resub  ")

    # ── Generate tab ─────────────────────────────────────────────────────────

    def _build_main_tab(self, p):
        # Header
        hdr = tk.Frame(p, bg=BG)
        hdr.pack(fill="x", padx=24, pady=(20, 2))
        from PIL import Image, ImageTk
        logo_pil = _make_gradient_logo()
        # Scale to fit ~38px tall
        scale_h = 38
        scale_w = int(logo_pil.width * scale_h / logo_pil.height)
        logo_pil = logo_pil.resize((scale_w, scale_h), Image.LANCZOS)
        self._logo_img = ImageTk.PhotoImage(logo_pil)
        c = tk.Canvas(hdr, bg=BG, highlightthickness=0, height=40, width=500)
        c.pack(side="left")
        c.create_image(0, 20, image=self._logo_img, anchor="w")
        c.create_text(scale_w + 10, 22, text="\u2726  Auto Subtitle Generator",
                      font=("Segoe UI", 11), fill=SUBTEXT, anchor="w")
        tk.Label(p, text="Paste a YouTube / Holodex URL, or pick a local video/audio file.",
                 font=FONT_S, bg=BG, fg=SUBTEXT).pack(anchor="w", padx=26, pady=(0, 10))

        # API key
        self._section("Gemini API key", p)
        acard = tk.Frame(p, bg=CARD, padx=16, pady=12)
        acard.pack(fill="x", padx=20, pady=(0, 10))
        self.apikey_var = tk.StringVar()
        tk.Entry(acard, textvariable=self.apikey_var, show="\u2022",
                 font=FONT_B, bg=CARD2, fg=TEXT, insertbackground=TEXT,
                 relief="flat", bd=0, highlightthickness=1,
                 highlightcolor=ACCENT, highlightbackground=BORDER
                 ).pack(fill="x", ipady=6)
        self._api_status = tk.Label(
            acard,
            text="Get a free key at aistudio.google.com \u2014 never stored outside this session.",
            font=("Segoe UI", 8), bg=CARD, fg=SUBTEXT)
        self._api_status.pack(anchor="w", pady=(4, 0))

        # Source
        self._section("Source", p)
        scard = tk.Frame(p, bg=CARD, padx=16, pady=12)
        scard.pack(fill="x", padx=20, pady=(0, 10))
        self.source_var = tk.StringVar()
        src_row = tk.Frame(scard, bg=CARD)
        src_row.pack(fill="x")
        tk.Entry(src_row, textvariable=self.source_var, font=FONT_B,
                 bg=CARD2, fg=TEXT, insertbackground=TEXT,
                 relief="flat", bd=0, highlightthickness=1,
                 highlightcolor=ACCENT, highlightbackground=BORDER
                 ).pack(side="left", fill="x", expand=True, ipady=6)
        tk.Button(src_row, text="Browse\u2026", font=FONT_S, bg=CARD, fg=ACCENT,
                  activebackground=CARD, activeforeground=ACCENT2,
                  relief="flat", cursor="hand2", padx=8,
                  command=self._browse_file).pack(side="left", padx=(8, 0))

        # Settings row
        srow = tk.Frame(p, bg=BG)
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
        self.mode_var.trace_add("write", self._on_mode_change)
        tk.Radiobutton(tcard, text="Cloud (Gemini)",
                       variable=self.mode_var, value="gemini",
                       bg=CARD, fg=TEXT, selectcolor=CARD,
                       activebackground=CARD, font=FONT_B).pack(anchor="w")
        tk.Radiobutton(tcard, text="Local/Cloud (Whisper + Gemini)",
                       variable=self.mode_var, value="local",
                       bg=CARD, fg=TEXT, selectcolor=CARD,
                       activebackground=CARD, font=FONT_B).pack(anchor="w")
        tk.Radiobutton(tcard, text="Local (Whisper + Qwen3)",
                       variable=self.mode_var, value="local_only",
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

        vad_card = tk.Frame(rcol, bg=CARD, padx=14, pady=6)
        self.vad_var = tk.BooleanVar(value=True)
        self._vad_check = tk.Checkbutton(
            vad_card, text="VAD filter (uncheck for singing / karaoke streams)",
            variable=self.vad_var, bg=CARD, fg=TEXT, selectcolor=CARD,
            activebackground=CARD, font=FONT_B)
        self._vad_check.pack(anchor="w")
        self._vad_card = vad_card

        ocard = tk.Frame(rcol, bg=CARD, padx=14, pady=10)
        ocard.pack(fill="x")
        tk.Label(ocard, text="Save .srt to", font=FONT_S, bg=CARD, fg=SUBTEXT).pack(anchor="w")
        self.outdir_var = tk.StringVar(value=os.path.expanduser("~/Desktop"))
        od_row = tk.Frame(ocard, bg=CARD)
        od_row.pack(fill="x")
        tk.Entry(od_row, textvariable=self.outdir_var, font=FONT_B,
                 bg=CARD2, fg=TEXT, insertbackground=TEXT,
                 relief="flat", bd=0, highlightthickness=1,
                 highlightcolor=ACCENT, highlightbackground=BORDER
                 ).pack(side="left", fill="x", expand=True, ipady=4)
        tk.Button(od_row, text="Browse\u2026", font=FONT_S, bg=CARD, fg=ACCENT,
                  activebackground=CARD, activeforeground=ACCENT2,
                  relief="flat", cursor="hand2", padx=8,
                  command=self._browse_outdir).pack(side="left", padx=(8, 0))

        # Progress
        self.progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(p, variable=self.progress_var, maximum=100,
                        style="Holo.Horizontal.TProgressbar"
                        ).pack(fill="x", padx=20, pady=(4, 0))
        self.prog_label = tk.Label(p, text="", font=FONT_S, bg=BG, fg=SUBTEXT)
        self.prog_label.pack(anchor="w", padx=22)

        # Buttons
        btn_row = tk.Frame(p, bg=BG)
        btn_row.pack(fill="x", padx=20, pady=(10, 0))
        self.run_btn = tk.Button(
            btn_row, text="\u2726  Generate subtitles",
            font=("Segoe UI", 12, "bold"),
            bg=ACCENT, fg=BG, activebackground=ACCENT2, activeforeground=BG,
            relief="flat", cursor="hand2", pady=9,
            command=self._start)
        self.run_btn.pack(side="left", fill="x", expand=True)
        self.cancel_btn = tk.Button(
            btn_row, text="\u2715  Cancel",
            font=("Segoe UI", 12, "bold"),
            bg=CARD2, fg=WARN, activebackground=CARD, activeforeground=WARN,
            relief="flat", cursor="hand2", pady=9, padx=14,
            state="disabled",
            command=self._cancel)
        self.cancel_btn.pack(side="left", padx=(6, 0))

        self.dl_btn = tk.Button(
            p, text="\u2b07  Download video (best quality)",
            font=("Segoe UI", 10, "bold"),
            bg=CARD2, fg=ACCENT, activebackground=CARD, activeforeground=ACCENT2,
            relief="flat", cursor="hand2", pady=7,
            command=self._download_video)
        self.dl_btn.pack(fill="x", padx=20, pady=(6, 0))

        # Log
        self._section("Log", p)
        self.log_box = scrolledtext.ScrolledText(
            p, font=FONT_MONO, bg=LOG_BG, fg=TEXT,
            insertbackground=TEXT, relief="flat", bd=0,
            state="disabled", height=12,
            cursor="arrow")
        self.log_box.pack(fill="both", expand=True, padx=20, pady=(4, 16))
        self.log_box.bind("<Control-a>", lambda e: (self.log_box.configure(state="normal"),
                                                     self.log_box.tag_add("sel", "1.0", "end"),
                                                     self.log_box.configure(state="disabled"), "break"))
        self.log_box.bind("<Control-c>", lambda e: None)
        self._log(f"Ready. Paste a URL or pick a file, enter your Gemini API key, and go.\n"
                  f"Default model: {GEMINI_MODEL}  |  Encoder: {ffmpeg_utils._ENCODER}\n")

    # ── shared helpers ───────────────────────────────────────────────────────

    def _section(self, text, parent=None):
        tk.Label(parent or self, text=text, font=FONT_S, bg=BG, fg=SUBTEXT
                 ).pack(anchor="w", padx=22, pady=(6, 2))

    def _cancel(self):
        self._stop_event.set()
        self.cancel_btn.configure(state="disabled", text="\u23f3  Cancelling\u2026")
        self._log("\U0001f6d1  Cancel requested \u2014 stopping after current chunk\u2026")

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

    def _on_mode_change(self, *_args):
        mode = self.mode_var.get()
        if mode == "local_only":
            self._model_cb.configure(state="disabled")
            self._api_status.config(
                text="Not needed for fully local mode.", fg=SUBTEXT)
        else:
            self._model_cb.configure(state="readonly")
            if not self.apikey_var.get().strip():
                self._api_status.config(
                    text="Get a free key at aistudio.google.com \u2014 never stored outside this session.",
                    fg=SUBTEXT)
        # VAD toggle only relevant for Whisper modes
        if mode in ("local", "local_only"):
            self._vad_card.pack(fill="x", pady=(0, 6))
        else:
            self._vad_card.pack_forget()

    def _log(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        lines = int(self.log_box.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log_box.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
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
        mode    = self.mode_var.get()

        if not source:
            messagebox.showwarning("No source", "Please enter a URL or choose a file.")
            return
        if mode != "local_only" and not api_key:
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

        is_url     = source.startswith("http://") or source.startswith("https://")
        task       = self.task_var.get()
        skip_mins  = self.skip_min_var.get() + self.skip_sec_var.get() / 60.0
        model      = self._model_map.get(self._model_cb.get(), GEMINI_MODEL)
        vad_filter = self.vad_var.get()

        if mode in ("local", "local_only"):
            try:
                import faster_whisper
            except ImportError:
                messagebox.showerror("Missing package",
                                     "Local mode requires faster-whisper.\n\n"
                                     "Run:\n  pip install faster-whisper\nThen restart holoSub.")
                return

        if mode == "local_only" and task == "translate":
            ok, err_msg = check_ollama()
            if not ok:
                messagebox.showerror("Ollama not ready", err_msg)
                return

        self._stop_event.clear()
        self.cancel_btn.configure(state="disabled")

        if mode == "local_only":
            # Skip Gemini validation — go straight to processing
            self.run_btn.configure(state="disabled", text="\u23f3  Working\u2026")
            self.cancel_btn.configure(state="normal", text="\u2715  Cancel")
            self.progress_var.set(0)
            self.prog_label.config(text="")
            self._log(f"\u25b6 Source={'URL' if is_url else 'file'}  task={task}  mode={mode}  skip={skip_mins:.1f}min  vad={vad_filter}")

            threading.Thread(
                target=run_pipeline,
                args=(source, is_url, task, api_key, outdir, skip_mins, model, mode,
                      lambda m: self.after(0, self._log, m),
                      lambda d, t: self.after(0, self._set_progress, d, t),
                      self._on_done,
                      self._stop_event, vad_filter),
                daemon=True
            ).start()
            return

        self.run_btn.configure(state="disabled", text="\u23f3  Validating key\u2026")
        self._log("\U0001f511  Validating Gemini API key\u2026")

        def validate_and_run():
            ok, err_msg = validate_api_key(api_key, model)
            if not ok:
                short_msg = err_msg.split("\n")[0]
                self.after(0, self._log, f"\u274c  {err_msg}")
                self.after(0, self._api_status.config, {"text": f"\u26a0  {short_msg}", "fg": WARN})
                self.after(0, self.run_btn.configure,
                           {"state": "normal", "text": "\u2726  Generate subtitles"})
                return

            self.after(0, self._api_status.config, {"text": "\u2705  API key valid", "fg": SUCCESS})
            self.after(0, self._log, "\u2705  API key valid")
            self.after(0, self.run_btn.configure, {"text": "\u23f3  Working\u2026"})
            self.after(0, self.cancel_btn.configure, {"state": "normal", "text": "\u2715  Cancel"})
            self.after(0, self.progress_var.set, 0)
            self.after(0, self.prog_label.config, {"text": ""})
            self.after(0, self._log,
                       f"\u25b6 Source={'URL' if is_url else 'file'}  task={task}  mode={mode}  skip={skip_mins:.1f}min  model={model}")

            threading.Thread(
                target=run_pipeline,
                args=(source, is_url, task, api_key, outdir, skip_mins, model, mode,
                      lambda m: self.after(0, self._log, m),
                      lambda d, t: self.after(0, self._set_progress, d, t),
                      self._on_done,
                      self._stop_event, vad_filter),
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

        self.dl_btn.configure(state="disabled", text="\u23f3  Downloading\u2026")
        self._log("\u2b07  Starting download (best quality)\u2026")

        def do_dl():
            try:
                import yt_dlp

                def log_from_thread(msg):
                    self.after(0, self._log, msg)

                with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
                    info  = ydl.extract_info(source, download=False)
                    title = info.get("title", "video")

                safe_title = sanitise_folder_name(title)
                video_dir  = os.path.join(outdir, safe_title)
                os.makedirs(video_dir, exist_ok=True)

                ydl_opts = {
                    "format": "bestvideo+bestaudio/best",
                    "outtmpl": os.path.join(video_dir, "%(title)s.%(ext)s"),
                    "merge_output_format": "mp4",
                    "quiet": True, "no_warnings": True,
                    "progress_hooks": [_make_ytdlp_progress_hook(log_from_thread)],
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([source])
                self.after(0, self._on_download_done, video_dir, True)
            except Exception as e:
                self.after(0, self._log, f"\u274c  Download error: {e}")
                self.after(0, self._on_download_done, None, False)

        threading.Thread(target=do_dl, daemon=True).start()

    def _on_download_done(self, out_path, success):
        self.dl_btn.configure(state="normal", text="\u2b07  Download video (best quality)")
        if success:
            self._log(f"\u2705  Video saved to:\n   {out_path}")
            messagebox.showinfo("Download complete \u2728", f"Video saved to:\n\n{out_path}")
        else:
            messagebox.showerror("Download failed", "Check the log for details.")

    def _on_done(self, out_file):
        self.after(0, self._finish, out_file)

    def _finish(self, out_file):
        self.run_btn.configure(state="normal", text="\u2726  Generate subtitles")
        self.cancel_btn.configure(state="disabled", text="\u2715  Cancel")
        if self._stop_event.is_set() and not out_file:
            self.prog_label.config(text="Cancelled.", fg=WARN)
            return
        if out_file:
            self.progress_var.set(100)
            self.prog_label.config(text="Complete!", fg=SUCCESS)
            messagebox.showinfo("Done \u2728",
                                f"Subtitle file saved:\n\n{out_file}\n\n"
                                "Load in VLC: Subtitle \u2192 Add Subtitle File")
        else:
            self.prog_label.config(text="Failed \u2014 see log above.", fg=WARN)
