import json
import re

from srt import parse_timestamp

# ── shared prompt constants ───────────────────────────────────────────────────

_HOLOLIVE_NAMES = """\
### HOLOLIVE TALENT NAME REFERENCE
These are the EXACT correct romanisations — never mishear or mis-romanise these names:
- hololive JP Gen 0: Tokino Sora, Robocosan, AZKi, Sakura Miko, Hoshimachi Suisei
- hololive JP Gen 1: Yozora Mel, Aki Rosenthal, Akai Haato, Shirakami Fubuki, Natsuiro Matsuri
- hololive JP Gen 2: Minato Aqua, Murasaki Shion, Nakiri Ayame, Yuzuki Choco, Oozora Subaru
- hololive JP GAMERS: Shirakami Fubuki, Ookami Mio, Nekomata Okayu, Inugami Korone
- hololive JP Gen 3: Usada Pekora, Shiranui Flare, Shirogane Noel, Houshou Marine, Uruha Rushia
- hololive JP Gen 4: Tsunomaki Watame, Tokoyami Towa, Himemori Luna, Amane Kanata, Kiryu Coco
- hololive JP Gen 5: Yukihana Lamy, Momosuzu Nene, Shishiro Botan, Omaru Polka, Mano Aloe
- hololive JP holoX: La+ Darknesss, Takane Lui, Hakui Koyori, Sakamata Chloe, Kazama Iroha
- hololive JP holoAN: Izuki Michiru, Hanazono Sayaka, Kazeshiro Yuki
- hololive ID Gen 1: Ayunda Risu, Moona Hoshinova, Airani Iofifteen
- hololive ID Gen 2: Kureiji Ollie, Anya Melfissa, Pavolia Reine
- hololive ID Gen 3: Vestia Zeta, Kaela Kovalskia, Kobo Kanaeru
- hololive EN Myth: Mori Calliope, Takanashi Kiara, Ninomae Ina'nis, Watson Amelia, Gawr Gura
- hololive EN Promise: IRyS, Ceres Fauna, Ouro Kronii, Hakos Baelz, Tsukumo Sana, Nanashi Mumei
- hololive EN Advent: Shiori Novella, Koseki Bijou, Nerissa Ravencroft, Fuwawa Abyssgard, Mococo Abyssgard
- hololive EN Justice: Elizabeth Rose Bloodflame, Gigi Murin, Cecilia Immergreen, Raora Panthera
- hololive DEV_IS ReGLOSS: Otonose Kanade, Ichijou Ririka, Juufuutei Raden, Todoroki Hajime, Hiodoshi Ao
- hololive DEV_IS FLOW GLOW: Isaki Riona, Koganei Niko, Mizumiya Su, Rindo Chihaya, Kikirara Vivi

### INDIE VTUBER REFERENCE
These indie VTubers may appear as guests or collaborators — use exact romanisations:
- Amagai Ruka (雨海ルカ)
- Kurageu Roa (海月雲ろあ)
- Appare Hinata (天晴ひなた)
Streamers frequently call each other by shortened names. Map these to their correct full names:
- "Chiha" or "Chihaya" → Rindo Chihaya
- "Su-chan" or "Su" → Mizumiya Su
- "Riona" or "Riona-chan" → Isaki Riona
- "Niko" or "Niko-chan" → Koganei Niko
- "Vivi" or "Vivi-chan" → Kikirara Vivi
- "Marine" or "Senchou" → Houshou Marine
- "Noel" or "Danchou" → Shirogane Noel
- "Miko" or "Mikochi" → Sakura Miko
- "Suisei" or "Suichan" → Hoshimachi Suisei
- "Korone" or "Korosan" → Inugami Korone
- "Subaru" or "Suba-chan" → Oozora Subaru
- "Aqua" or "Akutan" → Minato Aqua
- "Towa" or "Towa-sama" → Tokoyami Towa
- "Lamy" or "Lamy-chan" → Yukihana Lamy
- "Botan" or "Shishiron" → Shishiro Botan
- "Nene" or "Nenechi" → Momosuzu Nene"""

# yt-dlp mirrors these unsafe chars with full-width unicode instead of stripping them.
# We replicate that so subtitle filenames match downloaded video filenames automatically.
_YTDLP_REPLACEMENTS = {
    '/':  '\u29f8',  # ⧸
    '\\': '\u29f9',  # ⧹
    '"':  '\uff02',  # ＂
    '*':  '\uff0a',  # ＊
    ':':  '\uff1a',  # ：
    '<':  '\uff1c',  # ＜
    '>':  '\uff1e',  # ＞
    '?':  '\uff1f',  # ？
    '|':  '\uff5c',  # ｜
}

# Gemini response schema — built once at module level, reused per chunk.
def _build_gemini_schema():
    try:
        from google.genai import types as _t
        _sub = _t.Schema(
            type=_t.Type.OBJECT,
            required=["s", "e", "text"],
            properties={
                "s":    _t.Schema(type=_t.Type.STRING),
                "e":    _t.Schema(type=_t.Type.STRING),
                "text": _t.Schema(type=_t.Type.STRING),
            },
        )
        return _t.Schema(
            type=_t.Type.OBJECT,
            required=["global_analysis", "subs"],
            properties={
                "global_analysis": _t.Schema(type=_t.Type.STRING),
                "subs": _t.Schema(type=_t.Type.ARRAY, items=_sub),
            },
        )
    except ImportError:
        return None  # google-genai not yet installed; will be caught at runtime

_GEMINI_RESPONSE_SCHEMA = _build_gemini_schema()


def make_instruction(task, title):
    ctx = f"Video title: {title}." if title else ""
    if task == "translate":
        lang_rule = (
            "The streamers speak Japanese. Transcribe every utterance and translate "
            "into natural, colloquial English. Keep VTuber energy — translate 'yabe', "
            "'sugoi', 'kawaii' idiomatically, not literally. Never translate in isolation — "
            "use context from surrounding lines to determine pronouns, tense, and tone."
        )
        text_field = "English translation only"
    else:
        lang_rule = (
            "The streamers speak Japanese. Transcribe every utterance accurately "
            "in Japanese using proper kanji/kana. Preserve names as spoken."
        )
        text_field = "Japanese transcription only"

    return f"""You are an advanced AI expert in audio-visual subtitling. Your specialty is generating audio-synchronised, contextually rich subtitles from multimodal video+audio input using native audio tokenization.

{ctx}

### TASK
{lang_rule}

{_HOLOLIVE_NAMES}

### THE GOLDEN RULE: AUDIO DICTATES "WHEN", VIDEO DICTATES "WHAT"
Strictly separate the task of *timing* from the task of *transcription*.

**WHEN (Timing):** The streamer's voice is your absolute ground truth for timestamps. The exact moment a vocal starts dictates `s`. NEVER output a subtitle if no streamer voice is present.

**WHAT (Content):** Use the video visuals, on-screen text, and stream title to determine correct spelling, names, and context. When audio is ambiguous:
1. On-screen text (burnt-in subtitles, overlays) — absolute override.
2. The talent name reference list above — use exact spellings.
3. Visual scene context — what is actively happening on screen.
4. Stream title for broader context.

**Silence rules — ONLY stay silent when:**
- Pure instrumental BGM or ambience with no voice whatsoever.
- Waiting room / pre-stream screen with no one speaking or singing.
- Background game music or sound effects with no streamer voice.

**NEVER stay silent because:**
- Gameplay is on screen — streamers almost always talk during gameplay.
- An ad or sponsor segment is showing — streamers react and comment over them.
- A cutscene is playing — streamers frequently narrate over cutscenes.
- An intro or outro animation is playing — subtitle any singing or speech you hear, whether it is live or pre-recorded.
- If you can hear any voice at all — streamer, singer, or guest — subtitle it.

**No CC tags:** Transcribe speech only. Never add `[applause]`, `(sighs)`, `♪`, or any sound-effect tags.

### PRIORITY 1: PRECISION TIMESTAMPS
Treat every subtitle as a discrete, isolated event tied exclusively to the streamer's voice.

- **Timecode format:** Strictly `MM:SS.mmm` (e.g., `01:05.300`). Always pad with zeros.
- **`s`** = the exact moment the first word begins. **`e`** = the exact moment the last word ends.
- **No stretching for readability:** `e` MUST match when the last word ends, not when a human would want the text to disappear. Never stretch `e` to keep text on screen longer.
- **Prevent the "Traffic Jam" effect:** If you stretch `e` too long, it bleeds into the next subtitle's `s`, pushing everything out of sync. Each subtitle's `s` and `e` must tightly wrap only its own phrase — not bleed into the next one.
- **Unique start times:** Every entry MUST have a unique `s` — no two entries may share the same start time.
- **Strictly sequential:** `s` of entry N+1 MUST always be greater than `e` of entry N.

### PRIORITY 2: SEGMENTATION
- **Pause = split:** If there is a physical pause or breath in the middle of a sentence, SPLIT into a new block. NEVER merge speech across a pause into one entry.
- **Continuous breath split:** If splitting a continuous uninterrupted sentence only to stay under 50 characters, Part 1 `e` MUST EXACTLY EQUAL Part 2 `s` — do not invent a gap that doesn't exist in the audio.
- **Max ~50 characters per line.**
- **No repeated text:** Never output the same text in consecutive entries.

### EXAMPLES

**Example 1: Two streamers in rapid back-and-forth during gameplay**
*Scenario:* Su says "やばい！" (00:02.100–00:02.500), Chihaya immediately says "大丈夫！" (00:02.500–00:03.100).
```json
{{
  "global_analysis": "Two streamers reacting during racing gameplay. Both are speaking continuously with no silent gaps. Timestamps tightly wrap each utterance.",
  "subs": [
    {{"s": "00:02.100", "e": "00:02.500", "text": "Oh no!"}},
    {{"s": "00:02.500", "e": "00:03.100", "text": "You're okay!"}}
  ]
}}
```

**Example 2: Streamer talking over a sponsor segment**
*Scenario:* An ad for NEXTGEAR is on screen. The streamer is narrating over it from 00:05.000.
```json
{{
  "global_analysis": "Sponsor segment visible on screen. Streamer is actively speaking over it — subtitling their speech as normal.",
  "subs": [
    {{"s": "00:05.000", "e": "00:07.200", "text": "So this is the new NEXTGEAR case!"}},
    {{"s": "00:07.400", "e": "00:09.100", "text": "It has a glass panel on two sides."}}
  ]
}}
```

**Example 3: Splitting a continuous breath vs a paused sentence**
*Scenario:* Streamer says one continuous breath "それはちょっと違うんじゃないかな" (00:10.000–00:11.800, no pause). Too long, must split at 50 chars.
```json
{{
  "global_analysis": "Single continuous utterance split for length only — no audio pause exists, so Part 1 e equals Part 2 s exactly.",
  "subs": [
    {{"s": "00:10.000", "e": "00:10.900", "text": "That's a little..."}},
    {{"s": "00:10.900", "e": "00:11.800", "text": "...different, don't you think?"}}
  ]
}}
```

**Example 4: Opening theme song / intro sequence**
*Scenario:* The stream begins with an animated intro playing the streamer's theme song with vocals. Subtitle the singing. The live stream conversation begins at 03:06.
```json
{{
  "global_analysis": "Clip begins with an animated intro sequence containing the streamer's theme song with vocals. Subtitling the singing as normal. Live stream conversation begins at approximately 03:06.",
  "subs": [
    {{"s": "00:02.100", "e": "00:04.500", "text": "Riona-chan!"}},
    {{"s": "00:04.700", "e": "00:07.200", "text": "Yes! Sakisaki Riona!"}},
    {{"s": "03:06.500", "e": "03:08.200", "text": "Okay, let's get started!"}}
  ]
}}
```

### OUTPUT FORMAT
Return ONLY a valid JSON object. No markdown. Output `global_analysis` FIRST.

`global_analysis` is a strict verification step — state: (1) what is in this clip and who is speaking, (2) confirm no gameplay/ad sections were silenced incorrectly, (3) confirm no Traffic Jam stretching was applied.

{{
  "global_analysis": "...",
  "subs": [
    {{
      "s": "MM:SS.mmm",
      "e": "MM:SS.mmm",
      "text": "{text_field}"
    }}
  ]
}}

Rules:
- `text` is a single line — no embedded newlines.
- `text` contains ONLY the {text_field}."""


def parse_response(raw_text, offset_ms, label, log):
    raw_text = re.sub(r"^```[a-zA-Z]*\n?", "", raw_text.strip())
    raw_text = re.sub(r"\n?```$", "", raw_text).strip()
    if not raw_text:
        return None, False  # treat empty as bad JSON — caller will retry

    obj, truncated = None, False

    # Strip trailing junk after the closing } (Extra data error)
    clean = raw_text
    if not clean.endswith("}"):
        last_brace = clean.rfind("}")
        if last_brace != -1:
            clean = clean[:last_brace + 1]

    try:
        obj = json.loads(clean)
    except json.JSONDecodeError:
        # Try to salvage truncated response by finding last complete entry
        salvage = clean
        for marker in ['}\n  ]', '},\n  {', '},\n{', '}, {']:
            pos = salvage.rfind(marker)
            if pos != -1:
                end = pos + 1
                try:
                    candidate = salvage[:end] + "\n  ]\n}"
                    obj = json.loads(candidate)
                    truncated = True
                    break
                except Exception:
                    pass

        if obj is None:
            for suffix in ['\n  ]\n}', ']}']:
                try:
                    obj = json.loads(salvage + suffix)
                    truncated = True
                    break
                except Exception:
                    pass

        if obj is None:
            log(f"⚠️  {label}: unparseable JSON — will retry")
            return None, False

    analysis = obj.get("global_analysis", "")
    if analysis:
        if len(analysis) > 150:
            truncated_analysis = analysis[:150].rsplit(' ', 1)[0] + "…"
        else:
            truncated_analysis = analysis
        log(f"💬  {label}: {truncated_analysis}")

    if truncated:
        log(f"⚠️  {label}: response was truncated, salvaged {len(obj.get('subs', []))} entries — will retry")

    entries = []
    for s in obj.get("subs", []):
        text = str(s.get("text", "")).strip()
        if not text:
            continue
        # Strip embedded newlines Gemini sometimes sneaks in despite instructions
        text = text.replace("\n", " ").strip()
        start_ms = parse_timestamp(s.get("s", "00:00.000")) + offset_ms
        end_ms   = parse_timestamp(s.get("e", "00:00.000")) + offset_ms
        if end_ms <= start_ms:
            end_ms = start_ms + 2000
        entries.append({"start_ms": start_ms, "end_ms": end_ms, "text": text})

    return entries, truncated
