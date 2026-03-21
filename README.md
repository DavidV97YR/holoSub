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
pip install google-genai yt-dlp static-ffmpeg
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

5. **Skip intro** — optional, defaults to 0. Set the minutes and seconds if you want to skip a waiting room or pre-stream countdown at the start.

6. **Pick your save folder** (defaults to Desktop)

7. Click **✦ Generate subtitles** to create an `.srt` subtitle file  
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

holoSub processes video in 3-minute chunks and sends each one to Gemini. Gemini uses both the visuals and audio to generate accurate subtitles — this means it can read on-screen text, understand context from what's happening in the video, and correctly handle speech during gameplay, ads, cutscenes, intros, and outros.

The 3-minute chunk size was arrived at through testing. We started at 20-minute chunks, then tried 10 minutes and 5 minutes, and found that the smaller the chunks, the better Gemini is at placing timestamps accurately. Shorter chunks give Gemini a tighter window to work within, reducing the chance of subtitles drifting out of sync.

Subtitles are generated for any voice that is heard — live speech, singing during an intro or outro, or commentary over any type of content. The only time holoSub stays silent is during pure instrumental music or ambience with no voice at all.

A `.resume` folder is saved inside the video's output folder during processing. If the run is interrupted you can re-run on the same URL and it will skip already-completed chunks. Delete the `.resume` folder to force a full reprocess.

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

