"""Resub tab — selectively re-process subtitle chunks or translation batches."""

import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from config import (ACCENT, ACCENT2, BG, BORDER, CARD, CARD2, FONT_B,
                    FONT_MONO, FONT_S, GEMINI_MODEL, LOG_BG, MAX_LOG_LINES,
                    SUBTEXT, SUCCESS, TEXT, WARN)
from resub_pipeline import find_video_in_folder, run_resub, scan_folder


class ResubTab(tk.Frame):
    """GUI tab for selectively re-processing subtitle chunks / batches."""

    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        self.app = app              # HoloSubApp — shared API key, model, task
        self._stop_event = threading.Event()
        self._scan_result = None
        self._check_vars = []       # [(index, BooleanVar), ...]
        self._build_ui()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _section(self, text, parent=None):
        tk.Label(parent or self, text=text, font=FONT_S, bg=BG, fg=SUBTEXT
                 ).pack(anchor="w", padx=22, pady=(6, 2))

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
            self.prog_label.config(text=f"{done}/{total}  ({pct:.0f}%)")

    # ── UI construction ─────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Done folder ──
        self._section("Done folder (contains .resume)")
        fcard = tk.Frame(self, bg=CARD, padx=16, pady=10)
        fcard.pack(fill="x", padx=20, pady=(0, 6))
        self.folder_var = tk.StringVar()
        fr = tk.Frame(fcard, bg=CARD)
        fr.pack(fill="x")
        tk.Entry(fr, textvariable=self.folder_var, font=FONT_B,
                 bg=CARD2, fg=TEXT, insertbackground=TEXT,
                 relief="flat", bd=0, highlightthickness=1,
                 highlightcolor=ACCENT, highlightbackground=BORDER
                 ).pack(side="left", fill="x", expand=True, ipady=5)
        tk.Button(fr, text="Browse\u2026", font=FONT_S, bg=CARD, fg=ACCENT,
                  activebackground=CARD, activeforeground=ACCENT2,
                  relief="flat", cursor="hand2", padx=8,
                  command=self._browse_folder).pack(side="left", padx=(8, 0))

        # ── Source video ──
        self._section("Source video (auto-detected from folder)")
        vcard = tk.Frame(self, bg=CARD, padx=16, pady=10)
        vcard.pack(fill="x", padx=20, pady=(0, 6))
        self.source_var = tk.StringVar()
        vr = tk.Frame(vcard, bg=CARD)
        vr.pack(fill="x")
        tk.Entry(vr, textvariable=self.source_var, font=FONT_B,
                 bg=CARD2, fg=TEXT, insertbackground=TEXT,
                 relief="flat", bd=0, highlightthickness=1,
                 highlightcolor=ACCENT, highlightbackground=BORDER
                 ).pack(side="left", fill="x", expand=True, ipady=5)
        tk.Button(vr, text="Browse\u2026", font=FONT_S, bg=CARD, fg=ACCENT,
                  activebackground=CARD, activeforeground=ACCENT2,
                  relief="flat", cursor="hand2", padx=8,
                  command=self._browse_source).pack(side="left", padx=(8, 0))

        # ── Load button + mode label ──
        load_row = tk.Frame(self, bg=BG)
        load_row.pack(fill="x", padx=20, pady=(6, 4))
        self.load_btn = tk.Button(
            load_row, text="\U0001f50d  Load chunks",
            font=("Segoe UI", 10, "bold"),
            bg=CARD2, fg=ACCENT, activebackground=CARD,
            activeforeground=ACCENT2, relief="flat", cursor="hand2", pady=6,
            command=self._load_chunks)
        self.load_btn.pack(side="left")
        self.mode_label = tk.Label(load_row, text="", font=FONT_S,
                                   bg=BG, fg=SUBTEXT)
        self.mode_label.pack(side="left", padx=(12, 0))

        # ── Select-all + scrollable chunk list ──
        check_header = tk.Frame(self, bg=BG)
        check_header.pack(fill="x", padx=22, pady=(4, 0))
        self._select_all_var = tk.BooleanVar(value=False)
        tk.Checkbutton(check_header, text="Select All",
                       variable=self._select_all_var,
                       bg=BG, fg=TEXT, selectcolor=CARD,
                       activebackground=BG, font=FONT_B,
                       command=self._toggle_select_all
                       ).pack(side="left")

        list_frame = tk.Frame(self, bg=CARD, padx=2, pady=2)
        list_frame.pack(fill="both", expand=True, padx=20, pady=(2, 6))

        self._canvas = tk.Canvas(list_frame, bg=CARD,
                                 highlightthickness=0, height=150)
        scrollbar = tk.Scrollbar(list_frame, orient="vertical",
                                 command=self._canvas.yview)
        self._inner = tk.Frame(self._canvas, bg=CARD)
        self._inner.bind(
            "<Configure>",
            lambda _: self._canvas.configure(
                scrollregion=self._canvas.bbox("all")))
        self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._canvas.configure(yscrollcommand=scrollbar.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Mouse-wheel scrolling (only when pointer is over the canvas)
        self._canvas.bind("<Enter>", self._on_enter_canvas)
        self._canvas.bind("<Leave>", self._on_leave_canvas)

        # Placeholder text
        self._placeholder = tk.Label(
            self._inner,
            text="Click  \U0001f50d Load chunks  to scan a done folder.",
            font=FONT_S, bg=CARD, fg=SUBTEXT, pady=20)
        self._placeholder.pack()

        # ── Buttons ──
        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(fill="x", padx=20, pady=(4, 0))
        self.resub_btn = tk.Button(
            btn_row, text="\u2726  Resub selected",
            font=("Segoe UI", 11, "bold"),
            bg=ACCENT, fg=BG, activebackground=ACCENT2, activeforeground=BG,
            relief="flat", cursor="hand2", pady=7, state="disabled",
            command=self._start_resub)
        self.resub_btn.pack(side="left", fill="x", expand=True)
        self.cancel_btn = tk.Button(
            btn_row, text="\u2715  Cancel",
            font=("Segoe UI", 11, "bold"),
            bg=CARD2, fg=WARN, activebackground=CARD, activeforeground=WARN,
            relief="flat", cursor="hand2", pady=7, padx=14,
            state="disabled", command=self._cancel)
        self.cancel_btn.pack(side="left", padx=(6, 0))

        # ── Progress ──
        self.progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(self, variable=self.progress_var, maximum=100,
                        style="Holo.Horizontal.TProgressbar"
                        ).pack(fill="x", padx=20, pady=(6, 0))
        self.prog_label = tk.Label(self, text="", font=FONT_S,
                                   bg=BG, fg=SUBTEXT)
        self.prog_label.pack(anchor="w", padx=22)

        # ── Log ──
        self._section("Log")
        self.log_box = scrolledtext.ScrolledText(
            self, font=FONT_MONO, bg=LOG_BG, fg=TEXT,
            insertbackground=TEXT, relief="flat", bd=0,
            state="disabled", height=8, cursor="arrow")
        self.log_box.pack(fill="both", expand=True, padx=20, pady=(4, 16))

    # ── mouse-wheel helpers ─────────────────────────────────────────────────

    def _on_enter_canvas(self, _event):
        self._canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _on_leave_canvas(self, _event):
        self._canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event):
        self._canvas.yview_scroll(-1 * (event.delta // 120), "units")

    # ── actions ──────────────────────────────────────────────────────────────

    def _browse_folder(self):
        d = filedialog.askdirectory(title="Select done folder")
        if d:
            self.folder_var.set(d)
            vid = find_video_in_folder(d)
            if vid:
                self.source_var.set(vid)

    def _browse_source(self):
        p = filedialog.askopenfilename(
            title="Select source video",
            filetypes=[
                ("Media files",
                 "*.mp4 *.mkv *.webm *.avi *.mov *.m4a *.mp3 *.wav"),
                ("All files", "*.*"),
            ])
        if p:
            self.source_var.set(p)

    def _load_chunks(self):
        folder = self.folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showwarning("No folder",
                                   "Please select a valid done folder.")
            return

        resume_dir = os.path.join(folder, ".resume")
        if not os.path.isdir(resume_dir):
            messagebox.showwarning(
                "No cache",
                "No .resume folder found.\nRun Generate first.")
            return

        result = scan_folder(resume_dir)
        if not result["mode"]:
            messagebox.showwarning("Empty cache",
                                   "No cached chunks or batches found.")
            return

        self._scan_result = result

        # Auto-detect source video if not already set
        if not self.source_var.get().strip():
            vid = find_video_in_folder(folder)
            if vid:
                self.source_var.set(vid)

        # Mode label
        if result["mode"] == "cloud":
            mode_nice = "Cloud (Gemini)"
        elif result.get("translator") == "ollama":
            mode_nice = "Local (Whisper + Qwen3)"
        else:
            mode_nice = "Local/Cloud (Whisper + Gemini)"
        self.mode_label.config(
            text=f"{mode_nice}  \u2022  {len(result['items'])} items found")

        # Clear and repopulate checkboxes
        for w in self._inner.winfo_children():
            w.destroy()
        self._check_vars.clear()

        for item in result["items"]:
            var = tk.BooleanVar(value=False)
            tk.Checkbutton(
                self._inner, text=item["label"], variable=var,
                bg=CARD, fg=TEXT, selectcolor=CARD2,
                activebackground=CARD, font=FONT_B,
                anchor="w", padx=8, pady=2,
            ).pack(fill="x")
            self._check_vars.append((item["index"], var))

        self._select_all_var.set(False)
        self.resub_btn.configure(state="normal")

        info = f"\U0001f50d  Loaded {len(result['items'])} {result['mode']} items"
        if result.get("title"):
            info += f"  \u2014  {result['title']}"
        self._log(info)

    def _toggle_select_all(self):
        val = self._select_all_var.get()
        for _, var in self._check_vars:
            var.set(val)

    def _cancel(self):
        self._stop_event.set()
        self.cancel_btn.configure(state="disabled",
                                  text="\u23f3  Cancelling\u2026")
        self._log("\U0001f6d1  Cancel requested\u2026")

    def _start_resub(self):
        selected = [idx for idx, var in self._check_vars if var.get()]
        if not selected:
            messagebox.showwarning(
                "Nothing selected",
                "Select at least one chunk / batch to resub.")
            return

        folder = self.folder_var.get().strip()
        source = self.source_var.get().strip()
        api_key = self.app.apikey_var.get().strip()
        translator = self._scan_result.get("translator", "gemini") if self._scan_result else "gemini"

        if translator != "ollama" and not api_key:
            messagebox.showwarning(
                "No API key",
                "Enter your Gemini API key in the Generate tab.")
            return

        if not self._scan_result:
            messagebox.showwarning("Not loaded", "Load chunks first.")
            return

        mode = self._scan_result["mode"]
        skip_secs = self._scan_result.get("skip_secs", 0)

        # Cloud resub needs the source video to re-split
        if mode == "cloud" and not source:
            messagebox.showwarning(
                "No source",
                "Cloud resub requires the source video.\n"
                "Please select it above.")
            return

        # Resolve task, model, title from metadata or main tab
        task = (self._scan_result.get("task")
                or self.app.task_var.get())
        model = (self._scan_result.get("model")
                 or self.app._model_map.get(self.app._model_cb.get(),
                                            GEMINI_MODEL))
        title = (self._scan_result.get("title")
                 or os.path.basename(folder))

        self._stop_event.clear()
        self.resub_btn.configure(state="disabled", text="\u23f3  Working\u2026")
        self.cancel_btn.configure(state="normal", text="\u2715  Cancel")
        self.progress_var.set(0)
        self.prog_label.config(text="")

        self._log(f"\u25b6  Resub: mode={mode}  selected={len(selected)}  "
                  f"task={task}  model={model}")

        def do_resub():
            run_resub(
                folder, selected, source, mode, task, api_key,
                title, skip_secs, model,
                lambda m: self.after(0, self._log, m),
                lambda d, t: self.after(0, self._set_progress, d, t),
                self._on_done,
                self._stop_event,
                translator=translator,
            )

        threading.Thread(target=do_resub, daemon=True).start()

    def _on_done(self, out_file):
        self.after(0, self._finish, out_file)

    def _finish(self, out_file):
        self.resub_btn.configure(state="normal",
                                 text="\u2726  Resub selected")
        self.cancel_btn.configure(state="disabled",
                                  text="\u2715  Cancel")
        if self._stop_event.is_set() and not out_file:
            self.prog_label.config(text="Cancelled.", fg=WARN)
            return
        if out_file:
            self.progress_var.set(100)
            self.prog_label.config(text="Complete!", fg=SUCCESS)
            messagebox.showinfo("Resub Done \u2728",
                                f"Updated SRT:\n\n{out_file}")
        else:
            self.prog_label.config(text="Failed \u2014 see log.", fg=WARN)
