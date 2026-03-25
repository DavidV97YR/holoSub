def ms_to_srt(ms):
    h, rem = divmod(int(ms), 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms2 = divmod(rem, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms2:03}"


def parse_timestamp(ts):
    """Parse MM:SS.mmm / MM:SS:mmm / HH:MM:SS into milliseconds."""
    try:
        ms_part = 0
        if "." in ts:
            left, frag = ts.rsplit(".", 1)
            ms_part = int(frag.ljust(3, "0")[:3])
            parts = left.split(":")
        elif ts.count(":") == 2:
            parts = ts.split(":")
            ms_part = int(parts.pop().ljust(3, "0")[:3])
        else:
            parts = ts.split(":")

        if len(parts) == 2:
            h, m, s = 0, int(parts[0]), int(parts[1])
        elif len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        else:
            return 0
        return h * 3_600_000 + m * 60_000 + s * 1000 + ms_part
    except Exception:
        return 0


def build_srt(entries):
    """Sort, dedupe, fix overlaps, return SRT string."""
    resolved = []
    for e in entries:
        text = e.get("text", "").strip()
        if not text:
            continue
        start = e.get("start_ms", 0)
        end   = e.get("end_ms", start + 2000)
        if end <= start:
            end = start + 2000
        if end - start < 100:
            continue
        resolved.append([start, end, text])

    resolved.sort(key=lambda x: x[0])

    # Enforce minimum display duration — extend short subs so they're readable.
    # Very short entries (under OVERLAP_THRESHOLD) may extend past the next
    # entry's start by up to OVERLAP_BUDGET ms, creating controlled overlaps
    # that the sweep resolver turns into multi-line windows.  Longer entries
    # are capped at the next entry's start to avoid cascading.
    MIN_DISPLAY_MS      = 1500
    OVERLAP_THRESHOLD   = 800   # entries shorter than this get overlap budget
    OVERLAP_BUDGET      = 500   # max ms an entry may push into the next one
    for i in range(len(resolved)):
        start, end, _text = resolved[i]
        if end - start < MIN_DISPLAY_MS:
            new_end = start + MIN_DISPLAY_MS
            if i + 1 < len(resolved):
                next_start = resolved[i + 1][0]
                if end - start < OVERLAP_THRESHOLD:
                    # Very short — allow controlled overlap for multi-line
                    new_end = min(new_end, next_start + OVERLAP_BUDGET)
                else:
                    # Moderate length — cap at next entry's start
                    new_end = min(new_end, next_start)
            if new_end > end:
                resolved[i][1] = new_end

    # Remove entries with duplicate start times — keep only the first (longest/best)
    seen_starts = {}
    deduped = []
    for entry in resolved:
        start = entry[0]
        if start not in seen_starts:
            seen_starts[start] = True
            deduped.append(entry)
    resolved = deduped

    # Deduplicate only consecutive identical text — do NOT filter fully-contained
    # entries here, as those are legitimate overlapping speakers that the sweep
    # resolver below needs to see in order to produce multi-line subtitles.
    deduped_text = []
    for entry in resolved:
        if deduped_text and entry[2].strip().lower() == deduped_text[-1][2].strip().lower():
            continue  # consecutive duplicate text — skip
        deduped_text.append(entry)
    resolved = deduped_text

    # ── overlap resolver: sweep-line interval merge ───────────────────────────
    # Instead of pairwise fixes, sweep all entry boundaries to find every
    # distinct time window and render active speakers as a combined subtitle.
    # Handles up to 4 simultaneous speakers (e.g. FLOW GLOW collabs) cleanly.
    # No entries are shifted or dropped — overlapping speakers share a window.

    MAX_SPEAKERS   = 4
    OVERLAP_MIN_MS = 300   # minimum overlap window duration to render as dual+ line
    GAP            = 80    # ms gap enforced between non-overlapping entries

    # Collect all boundary points from every entry
    boundaries = set()
    for entry in resolved:
        boundaries.add(entry[0])
        boundaries.add(entry[1])
    boundaries = sorted(boundaries)

    sweep = []
    for j in range(len(boundaries) - 1):
        win_start = boundaries[j]
        win_end   = boundaries[j + 1]
        if win_end - win_start < 100:
            continue  # too short to be worth rendering

        # Find all entries active during this window
        active = [e for e in resolved if e[0] <= win_start and e[1] >= win_end]
        if not active:
            continue

        # Cap at MAX_SPEAKERS — if somehow more are active, keep the longest ones
        if len(active) > MAX_SPEAKERS:
            active = sorted(active, key=lambda e: e[1] - e[0], reverse=True)[:MAX_SPEAKERS]

        # Build combined text — deduplicate in case of near-identical lines
        texts = []
        seen  = set()
        for e in active:
            t = e[2].strip()
            if t and t not in seen:
                seen.add(t)
                texts.append(t)

        combined = "\n".join(texts)

        # Multi-speaker window too short to read — collapse to first speaker only
        if len(texts) > 1 and (win_end - win_start) < OVERLAP_MIN_MS:
            combined = texts[0]

        sweep.append([win_start, win_end, combined])

    # Merge consecutive windows with identical text and no meaningful gap between them
    merged = []
    for entry in sweep:
        if merged and merged[-1][2] == entry[2] and entry[0] <= merged[-1][1] + GAP:
            merged[-1][1] = entry[1]  # extend previous entry
        else:
            merged.append(entry)

    resolved = [e for e in merged if e[1] > e[0] + 100]
    # ── end overlap resolver ──────────────────────────────────────────────────

    lines = []
    for idx, (start, end, text) in enumerate(resolved, 1):
        lines += [str(idx), f"{ms_to_srt(start)} --> {ms_to_srt(end)}", text, ""]
    return "\n".join(lines)
