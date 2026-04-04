# holoSub — Auto Subtitle Generator

---

## Setup

### 1. Install Python 3.9+
https://python.org — check **"Add Python to PATH"** during install.

### 2. Install FFmpeg
FFmpeg is needed in two ways — `static-ffmpeg` (installed via pip in step 3) handles video processing inside holoSub automatically. A separate system FFmpeg is required by yt-dlp to merge video and audio when downloading.

1. Go to https://github.com/yt-dlp/FFmpeg-Builds/releases/latest
2. Download `ffmpeg-master-latest-win64-gpl.zip`
3. Extract it — you'll get a folder like `ffmpeg-master-latest-win64-gpl`
4. Inside it, open the `bin` folder — you'll see `ffmpeg.exe`, `ffprobe.exe`, `ffplay.exe`
5. Copy those 3 `.exe` files to `C:\Windows\System32` — no PATH editing needed
6. Open a new Command Prompt and run `ffmpeg -version` to confirm it works

### 3. Install packages
```
pip install -r requirements.txt
```

For **Local mode** (optional), install faster-whisper and CUDA Toolkit 12:
```
pip install faster-whisper
```
Then install CUDA Toolkit 12 from https://developer.nvidia.com/cuda-downloads (required for GPU acceleration).

For **fully offline Local mode** (Whisper + Qwen3), also install Ollama:
1. Install from https://ollama.com
2. Start Ollama, then pull the model:
   ```
   ollama pull qwen3:14b
   ```

### 4. Get a Gemini API key
1. Go to https://aistudio.google.com
2. Sign in with a Google account
3. Click **"Get API key"** → **"Create API key"**
4. Copy the key (starts with `AIza...`)

### 5. Run the app
```
python holoSub.py
```

---

## Using holoSub

1. **Paste a URL** — YouTube, Holodex, or any yt-dlp-supported link
   OR click **Browse…** to pick a local `.mp4` / `.mkv` / `.m4a` / `.webm` file

2. **Paste your Gemini API key** into the key field — it's validated before any processing begins

3. **Select a model** — defaults to Gemini 3 Flash Preview. Options:
   - **Gemini 3 Flash Preview** — newest generation, recommended
   - **Gemini 2.5 Flash** — proven, stable fallback
   - **Gemini 2.5 Flash-Lite** — faster and cheaper
   - **Gemini 2.5 Pro** — highest quality, slower and more expensive
   - **Gemini 2.0 Flash** — older generation

4. **Output language:**
   - **English (translate + localise)** — translates JP → EN with natural VTuber-aware phrasing ✅
   - **Japanese (keep original)** — accurate JP transcript

5. **Processing mode:**
   - **Cloud (Gemini)** — sends video chunks to Gemini for transcription and translation. No local setup needed.
   - **Local/Cloud (Whisper + Gemini)** — transcribes audio locally using faster-whisper on your GPU, then sends the Japanese text to Gemini for translation only. No rate limiting, faster on long streams, requires CUDA Toolkit 12 and faster-whisper installed.
   - **Local (Whisper + Qwen3)** — fully offline. Transcribes with faster-whisper and translates with Qwen3 via Ollama — no API key or internet needed. Significantly slower than the other two modes. Requires CUDA Toolkit 12, faster-whisper, and Ollama with `qwen3:14b` pulled. Only tested on an RTX 4070 Super.

   **Local/Cloud mode is recommended** if you have an Nvidia GPU. Whisper produces more accurate timestamps than Gemini's video-based estimates, and sending a text transcript to Gemini is far lighter than uploading video chunks — meaning less API usage, no rate limiting, and faster overall processing. The Local (Whisper + Qwen3) option takes this further by removing the cloud dependency entirely.

6. **VAD filter** — enabled by default. Uncheck this for singing or karaoke streams where Voice Activity Detection may incorrectly filter out vocals.

7. **Skip intro** — optional, defaults to 0. Set the minutes and seconds if you want to skip a waiting room or pre-stream countdown at the start.

8. **Pick your save folder** (defaults to Desktop)

9. Click **✦ Generate subtitles** to create an `.srt` subtitle file
   OR click **⬇ Download video** to download the video in best available quality

holoSub automatically creates a subfolder named after the video, keeping subtitles and downloads organised together. The `.srt` file is named to match the video file so MPC auto-loads it without any manual steps.

---

## Screenshots

holoSub in action — Mizumiya Su & Rindo Chihaya's PC license stream, and Isaki Riona & Natsuiro Matsuri's Phasmophobia VR stream.

![Mizumiya Su & Rindo Chihaya's PC license stream - Screenshot 1](https://raw.githubusercontent.com/DavidV97YR/holoSub/main/Screenshots/mpc-hc64_YzKjFIIJxZ.jpg)

![Mizumiya Su & Rindo Chihaya's PC license stream - Screenshot 2](https://raw.githubusercontent.com/DavidV97YR/holoSub/main/Screenshots/mpc-hc64_uy97cFOBaF.jpg)

![Isaki Riona & Natsuiro Matsuri's Phasmophobia VR stream](https://raw.githubusercontent.com/DavidV97YR/holoSub/main/Screenshots/mpc-hc64_O3KzXUni9G.png)

---

## How It Works

### Cloud (Gemini) mode
holoSub processes video in 3-minute chunks and sends each one to Gemini. Gemini uses both the visuals and audio to generate accurate subtitles — this means it can read on-screen text, understand context from what's happening in the video, and correctly handle speech during gameplay, ads, cutscenes, intros, and outros.

The 3-minute chunk size was arrived at through testing. We started at 20-minute chunks, then tried 10 minutes and 5 minutes, and found that the smaller the chunks, the better Gemini is at placing timestamps accurately. Shorter chunks give Gemini a tighter window to work within, reducing the chance of subtitles drifting out of sync.

Subtitles are generated for any voice that is heard — live speech, singing during an intro or outro, or commentary over any type of content. The only time holoSub stays silent is during pure instrumental music or ambience with no voice at all.

### Local/Cloud (Whisper + Gemini) mode
holoSub extracts the full audio and silences the intro period (if skip is set) so that VAD ignores it while keeping the original timeline intact — no timestamp offsets needed. The audio is then run through faster-whisper large-v3 on your GPU locally, producing a precise Japanese transcript with segment-level timestamps. The transcript is sent to Gemini in batches for translation into English — no video is uploaded, so there are no rate limits and processing is significantly faster on long streams.

Timestamp accuracy in Local mode relies on Whisper's segment-level timestamps with Silero VAD filtering (`min_silence_duration_ms=500`, default threshold). Short subtitle segments are given a minimum display duration and, when back-to-back in rapid dialog, overlapping entries are merged into multi-line windows by a sweep-line resolver that supports up to 4 simultaneous speakers.

This mode requires an Nvidia GPU with CUDA Toolkit 12 installed. The large-v3 model (~3GB) is downloaded automatically on first use and cached locally.

### Local (Whisper + Qwen3) mode
Same as Local (Whisper + Gemini) above, but translation is handled entirely on your machine by Qwen3 running through Ollama instead of calling the Gemini API. No API key or internet connection is needed once the models are downloaded. Segments are sent in smaller batches (50 at a time) with rolling context from the previous 10 lines to keep translations consistent. After processing, the Qwen3 model is automatically unloaded from VRAM.

This mode is significantly slower than the other two since translation runs entirely on your GPU. It has only been tested on an RTX 4070 Super. Requires Ollama running locally with the `qwen3:14b` model pulled, in addition to the same CUDA and faster-whisper requirements as Local/Cloud mode.

### Resume system
A `.resume` folder is saved inside the video's output folder during processing. If the run is interrupted you can re-run on the same URL and it will skip already-completed work. Delete the `.resume` folder to force a full reprocess. The Resub tab (see below) lets you surgically re-process individual chunks or batches from this cache without redoing the whole video.

---

## Resub Tab

The Resub tab lets you fix specific parts of a subtitle file without re-processing the entire video. It reads from the `.resume` cache created by a previous run and lets you select individual chunks (cloud mode) or translation batches (local mode) to redo.

### When to use it
- A specific section of the subtitles is wrong, mistimed, or missing
- A chunk failed or returned no subtitles during the original run
- You want to re-translate a particular segment with different context

### How to use it

1. **Done folder** — browse to the video's output folder (the one containing the `.resume` subfolder). The source video inside it is auto-detected.

2. **Source video** — for cloud mode this is required so holoSub can re-split the video. For local mode it is not needed since the Whisper transcript is already cached.

3. Click **🔍 Load chunks** — holoSub scans the `.resume` folder and lists every cached chunk or translation batch with its time range and line count. The mode (Cloud or Local) is auto-detected from the original run's metadata.

4. **Select** the chunks or batches you want to redo. Use **Select All** to tick everything, or pick individual entries.

5. Click **✦ Resub selected** — holoSub deletes the cache for the selected items, re-processes them using the API key and model from the Generate tab, then rebuilds and overwrites the existing `.srt` file in the output folder.

> **Note:** The API key and model are read from the Generate tab — make sure they are set before running a resub.

---

## Loading Subtitles in VLC

1. Open the video in VLC
2. **Subtitle → Add Subtitle File…**
3. Pick the `.srt` file holoSub created

---

## Loading Subtitles in Media Player Classic (MPC-HC / MPC-BE)

**Auto-load** — holoSub names the `.srt` to match the downloaded video file exactly, so MPC loads it automatically when you open the video. No manual steps needed as long as both files are in the same folder.

**Drag and drop** — drag the `.srt` file onto the MPC window while the video is playing.

**Menu** — File → Load Subtitle, then pick the `.srt` file.

> **Encoding note:** holoSub saves subtitle files as UTF-8. If MPC shows garbled characters, go to View → Options → Subtitles and set the default charset to UTF-8.

---

## Tips

- **Download first, then subtitle** — use the Download video button before generating subtitles so the `.srt` gets named to match the video for MPC auto-load.
- **Members streams** — yt-dlp supports cookie-based auth. See the yt-dlp docs for `--cookies-from-browser`.
- **Pricing** — Gemini 3 Flash Preview costs roughly a few cents per hour of video. Check current rates at https://ai.google.dev/pricing.
- **AV1 playback** — if downloaded videos won't play in MPC, install LAV Filters 0.81+ from https://github.com/Nevcairiel/LAVFilters/releases. Uninstall any older version first and reboot before installing.
