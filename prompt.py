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
            required=["i", "o", "text"],
            properties={
                "i": _t.Schema(type=_t.Type.STRING),
                "o": _t.Schema(type=_t.Type.STRING),
                "text": _t.Schema(type=_t.Type.STRING),
            },
        )
        return _t.Schema(
            type=_t.Type.OBJECT,
            required=["preflight", "cues"],
            properties={
                "preflight": _t.Schema(type=_t.Type.STRING),
                "cues": _t.Schema(type=_t.Type.ARRAY, items=_sub),
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
            "'sugoi', 'kawaii' idiomatically, not literally. Use surrounding lines "
            "for pronouns, tense, and tone — never translate in isolation."
        )
        text_field = "English translation only"
    else:
        lang_rule = (
            "The streamers speak Japanese. Transcribe every utterance accurately "
            "in Japanese using proper kanji/kana. Preserve names as spoken."
        )
        text_field = "Japanese transcription only"

    return f"""You are a specialist subtitle generator for hololive VTuber streams. You produce tightly-synchronised subtitles from raw video+audio using native audio tokenization.

{ctx}

### TASK
{lang_rule}

{_HOLOLIVE_NAMES}

### THE GOLDEN RULE: VOICE-LOCKED TIMING, CONTEXT-INFORMED CONTENT
Strictly separate the task of *timing* from the task of *transcription*.

**WHEN (Timing):** The voice waveform is absolute ground truth. `i` = first syllable onset. `o` = last syllable offset. No voice = no cue.

**WHAT (Content):** Resolve what was said using this priority order:
1. Burnt-in on-screen text / hardcoded subtitles — overrides everything.
2. The hololive name list above — always use exact romanisation.
3. Visual context — actions, expressions, environment.
4. Stream title — broader topical context.

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

- **Format:** Strictly `MM:SS.mmm` (e.g., `02:14.070`). Always pad with zeros.
- **`i`** = the exact moment the first word begins. **`o`** = the exact moment the last word ends.
- **No stretching for readability:** `o` MUST match when the last word ends, not when a human would want the text to disappear. Never stretch `o` to keep text on screen longer. If you stretch `o` too long, it bleeds into the next entry's `i`, pushing everything out of sync. Each entry's `i` and `o` must tightly wrap only its own phrase.
- **Unique starts:** No two entries share the same `i`.
- **Strict ordering:** Entry N+1's `i` > entry N's `o`.

### PRIORITY 2: SEGMENTATION
- **Pause = split:** If there is a physical pause or breath in the middle of a sentence, SPLIT into a new block. NEVER merge speech across a pause into one entry.
- **Length-only splits:** If splitting a continuous uninterrupted sentence only to stay under 50 characters, block 1's `o` MUST EXACTLY EQUAL block 2's `i` — do not invent a gap that doesn't exist in the audio.
- **Max ~50 characters per entry.**
- **Keep short reactions as their own entries:** Exclamations like "yabai!", "eh?", "uso!" should be individual entries — do not merge them into adjacent speech.
- **No repeated text:** Never output the same text in consecutive entries.

### KNOWN FAILURE MODES — AVOID THESE

**"Hallucinated Speech":**
Never invent cues for audio gaps. If there is a 5-second silence between utterances, there must be a 5-second gap in your output. However, rapid back-to-back entries ARE correct when the speaker is actually talking fast — VTubers frequently produce many short utterances in quick succession. Match the real pace of speech.

**"Early Cutoff" (incomplete coverage):**
Subtitle the ENTIRE clip from start to finish. Do not stop generating cues partway through. If the speaker is still talking at the end of the video, your last cue should cover that final utterance.

### EXAMPLES

**Rapid back-and-forth during gameplay:**
Su says "やばい！" (00:02.100–00:02.500), Chihaya immediately says "大丈夫！" (00:02.500–00:03.100).
```json
{{
  "preflight": "SCENE: Two streamers reacting during racing gameplay. SILENCE CHECK: Both speaking — no incorrect silencing. TIMING CHECK: Each cue tightly wraps its utterance, no padding.",
  "cues": [
    {{"i": "00:02.100", "o": "00:02.500", "text": "Oh no!"}},
    {{"i": "00:02.500", "o": "00:03.100", "text": "You're okay!"}}
  ]
}}
```

**Simultaneous speakers in a collab:**
Riona says "やったー！" (00:15.200–00:15.600) while Chihaya says "すごい！" starting at (00:15.300–00:15.800). Both are audible.
```json
{{
  "preflight": "SCENE: Two streamers celebrating at the same time. SILENCE CHECK: Both voices audible — subtitling both. TIMING CHECK: Entries are short and tightly wrapped to each voice.",
  "cues": [
    {{"i": "00:15.200", "o": "00:15.600", "text": "We did it!"}},
    {{"i": "00:15.300", "o": "00:15.800", "text": "Amazing!"}}
  ]
}}
```

**Length-only split (continuous breath, no pause):**
Streamer says "それはちょっと違うんじゃないかな" (00:10.000–00:11.800) in one breath. Over 50 chars, must split.
```json
{{
  "preflight": "SCENE: Single streamer, casual conversation. SILENCE CHECK: N/A — continuous speech. TIMING CHECK: No audio gap exists so block 1 `o` equals block 2 `i`.",
  "cues": [
    {{"i": "00:10.000", "o": "00:10.900", "text": "That's a little..."}},
    {{"i": "00:10.900", "o": "00:11.800", "text": "...different, don't you think?"}}
  ]
}}
```

**Intro theme song then live conversation:**
Animated intro with vocal theme. Live stream starts at 03:06.
```json
{{
  "preflight": "SCENE: Animated intro with theme song vocals, then live conversation. SILENCE CHECK: Subtitled intro vocals — not silenced. TIMING CHECK: Gap between 00:07.200 and 03:06.500 is real silence.",
  "cues": [
    {{"i": "00:02.100", "o": "00:04.500", "text": "Riona-chan!"}},
    {{"i": "00:04.700", "o": "00:07.200", "text": "Yes! Sakisaki Riona!"}},
    {{"i": "03:06.500", "o": "03:08.200", "text": "Okay, let's get started!"}}
  ]
}}
```

### OUTPUT FORMAT
Return ONLY valid JSON. No markdown. Output `preflight` FIRST.

`preflight` is a structured verification checklist with three mandatory checks:
- **SCENE:** Who is speaking and what is happening.
- **SILENCE CHECK:** Confirm you did not incorrectly silence any gameplay/ad/cutscene/intro sections.
- **TIMING CHECK:** Confirm every `o` stops at the last syllable with no padding, and no cascade drift.

{{
  "preflight": "SCENE: ... SILENCE CHECK: ... TIMING CHECK: ...",
  "cues": [
    {{
      "i": "MM:SS.mmm",
      "o": "MM:SS.mmm",
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

    analysis = obj.get("preflight", "")
    if analysis:
        if len(analysis) > 150:
            truncated_analysis = analysis[:150].rsplit(' ', 1)[0] + "…"
        else:
            truncated_analysis = analysis
        log(f"💬  {label}: {truncated_analysis}")

    if truncated:
        log(f"⚠️  {label}: response was truncated, salvaged {len(obj.get('cues', []))} entries — will retry")

    entries = []
    for cue in obj.get("cues", []):
        text = str(cue.get("text", "")).strip()
        if not text:
            continue
        # Strip embedded newlines Gemini sometimes sneaks in despite instructions
        text = text.replace("\n", " ").strip()
        start_ms = parse_timestamp(cue.get("i", "00:00.000")) + offset_ms
        end_ms   = parse_timestamp(cue.get("o", "00:00.000")) + offset_ms
        if end_ms <= start_ms:
            end_ms = start_ms + 2000
        entries.append({"start_ms": start_ms, "end_ms": end_ms, "text": text})

    return entries, truncated
