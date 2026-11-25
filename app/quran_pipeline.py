import json, os, time, random, re
from pathlib import Path
import requests

# Gemini availability varies by model and region. Prefer the stable 3.6 Flash
# audio path, then fail over across independent model endpoints.
GEMINI_MODELS = [
    x.strip() for x in os.getenv(
        "GEMINI_FALLBACK_MODELS",
        "gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.8-flash"
    ).split(",") if x.strip()
]
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip()
if GEMINI_MODEL:
    GEMINI_MODELS = [GEMINI_MODEL] + [m for m in GEMINI_MODELS if m != GEMINI_MODEL]
QURAN_API = "https://api.alquran.cloud/v1"
RETRYABLE = {408, 429, 500, 502, 503, 504}

def _gemini(prompt, audio_path=None):
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is missing; refusing to publish without verified verse captions.")
    parts = [{"text": prompt}]
    if audio_path:
        import base64
        raw = Path(audio_path).read_bytes()
        if len(raw) > 18 * 1024 * 1024:
            raise RuntimeError("Audio is too large for inline Gemini analysis.")
        parts.insert(0, {"inline_data": {"mime_type": "audio/mpeg", "data": base64.b64encode(raw).decode("ascii")}})

    failures = []
    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        # Two bounded attempts per model; then switch provider endpoint/model.
        for attempt, delay in enumerate((0, 8, 20)):
            if delay:
                time.sleep(delay + random.uniform(0, 3))
            try:
                response = requests.post(
                    url, params={"key": key},
                    json={"contents": [{"role": "user", "parts": parts}],
                          "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}},
                    timeout=(20, 180)
                )
                if response.status_code >= 400:
                    message = f"{model} HTTP {response.status_code}: {response.text[:250]}"
                    failures.append(message)
                    if response.status_code in RETRYABLE:
                        print(f"Gemini temporary error; retry {attempt + 1}/2 on {model}: HTTP {response.status_code}")
                        continue
                    # Model not available or request incompatible: try next model.
                    break
                payload = response.json()
                candidates = payload.get("candidates") or []
                if not candidates:
                    failures.append(f"{model}: no candidates returned")
                    break
                text = "".join(p.get("text", "") for p in candidates[0].get("content", {}).get("parts", []))
                if not text.strip():
                    failures.append(f"{model}: empty response")
                    break
                try:
                    result = json.loads(text)
                    print(f"Gemini succeeded using {model}")
                    return result
                except json.JSONDecodeError:
                    failures.append(f"{model}: invalid JSON response")
                    break
            except (requests.Timeout, requests.ConnectionError) as exc:
                failures.append(f"{model}: {type(exc).__name__}")
                print(f"Gemini connection issue on {model}; retry {attempt + 1}/2")
                continue
            except requests.RequestException as exc:
                failures.append(f"{model}: {type(exc).__name__}: {exc}")
                break
    raise RuntimeError("All Gemini fallback models failed: " + " | ".join(failures[-8:]))

def identify_and_verify_verses(audio_path, duration_seconds, reciter):
    # Quran-specific ASR is the source of truth for verse identity. Gemini is not
    # allowed to guess ayah numbers or caption text.
    from quran_asr import transcribe_and_match

    identified = transcribe_and_match(audio_path, duration_seconds, reciter)
    verses = identified.get("verses") or []
    if not verses:
        raise RuntimeError("Quran ASR could not verify any complete ayahs.")
    previous = None
    for verse in verses:
        surah, ayah = int(verse["surah"]), int(verse["ayah"])
        start, end = float(verse["start_seconds"]), float(verse["end_seconds"])
        if not (1 <= surah <= 114 and ayah >= 1 and 0 <= start < end <= duration_seconds + 0.5):
            raise RuntimeError("Quran ASR returned an invalid verse number or timestamp.")
        if previous and (surah != previous[0] or ayah != previous[1] + 1):
            raise RuntimeError("Quran ASR verses are not consecutive in one surah.")
        if end - start < 0.35:
            raise RuntimeError(f"Quran ASR timing is too short for {surah}:{ayah}.")
        previous = (surah, ayah)
    return identified

def _local_visual_queries(identified):
    # Deterministic offline fallback: map Quranic Arabic themes to matching
    # footage searches; no model/API call is needed for scene selection.
    text = " ".join(v.get("text", "") for v in identified.get("verses", []))
    text = re.sub(r"[\u064B-\u065F\u0670\u06D6-\u06EDـ]", "", text)
    themes = [
        (("ماء", "الماء", "أنهار", "نهر", "بحر", "البحر", "مطر", "المطر", "غيث"), "clear water rivers waterfalls rain aerial nature footage"),
        (("سماء", "السماوات", "نجوم", "النجوم", "كواكب"), "dramatic sky stars galaxy clouds timelapse footage"),
        (("أرض", "الأرض", "جبال", "الجبال", "رواسي"), "mountains vast landscapes earth aerial drone footage"),
        (("ليل", "الليل", "نهار", "النهار", "فجر", "الصبح"), "night turning into dawn sunrise timelapse landscape"),
        (("شمس", "الشمس", "قمر", "القمر"), "sunrise sunset moon sky cinematic nature footage"),
        (("جنة", "جنات", "الجنة", "ثمار", "أنهار"), "lush gardens flowing streams green paradise nature footage"),
        (("نور", "النور", "ضياء", "مشرق"), "golden sunlight rays through clouds cinematic landscape"),
        (("ريح", "الرياح", "الريح", "سحاب", "السحاب"), "moving clouds wind storm clouds aerial landscape"),
        (("نار", "النار", "جحيم"), "volcanic fire glowing embers dramatic landscape footage"),
        (("طير", "الطير", "طيور"), "birds flying over mountains sea slow motion nature footage"),
    ]
    queries = [query for words, query in themes if any(word in text for word in words)]
    defaults = [
        "cinematic mountains and vast valleys aerial nature footage",
        "waterfalls rivers and ocean waves 4k nature footage",
        "clouds sunlight and dramatic skies timelapse footage",
        "quiet forests and landscapes sunrise drone footage",
    ]
    for q in defaults:
        if q not in queries: queries.append(q)
    return queries[:10]

def semantic_visual_queries(identified):
    verses = identified["verses"]
    start = min(float(v["start_seconds"]) for v in verses)
    end = max(float(v["end_seconds"]) for v in verses)
    duration = max(1.0, end - start)
    count = max(4, min(10, round(duration / 17.5)))
    groups = [[] for _ in range(count)]
    for verse in verses:
        midpoint = (float(verse["start_seconds"]) + float(verse["end_seconds"])) / 2
        index = max(0, min(count - 1, int((midpoint - start) / duration * count)))
        groups[index].append(verse)

    descriptions = []
    fallback = []
    for index, group in enumerate(groups, 1):
        if group:
            first, last = group[0]["ayah"], group[-1]["ayah"]
            group_text = " ".join(v["text"] for v in group)
            descriptions.append(f"Scene {index}, ayahs {first}-{last}: {group_text}")
            local = _local_visual_queries({"verses": group})
            fallback.append(local[0] if local else "cinematic natural landscape matching the Quranic theme")
        else:
            descriptions.append(f"Scene {index}: choose a respectful visual continuation of the nearby Quranic meaning.")
            fallback.append("cinematic landscape, sky and light matching the nearby Quranic meaning")

    prompt = (
        'Return JSON only in the form {"queries":["English YouTube search query"]}. '
        f"Return exactly {count} queries in chronological order. Each query must describe footage "
        "that directly illustrates the meaning of its own ayah group, not generic unrelated scenery. "
        "Use concrete visual subjects and actions when present (rain, rivers, mountains, night, dawn, "
        "gardens, ships, stars, wind, etc.). Be respectful: no depiction of Allah, prophets, angels, "
        "religious figures, worshippers, faces, or text. Prefer cinematic 4K/8K landscape or nature footage.\n"
        + identified["surah_name"] + "\n" + "\n".join(descriptions)
    )
    try:
        response = _gemini(prompt)
        queries = response.get("queries") if isinstance(response, dict) else None
        if isinstance(queries, list):
            queries = [str(x).strip() for x in queries if str(x).strip()]
            if len(queries) == count:
                return queries
        print("Gemini scene response did not match the required scene count; using ayah-specific local queries.")
    except Exception as exc:
        print(f"Gemini scene analysis unavailable ({exc}); using ayah-specific local queries.")
    return fallback

def _ass_time(sec):
    cs = max(0, int(round(float(sec) * 100)))
    h, rem = divmod(cs, 360000); m, rem = divmod(rem, 6000); s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"

def write_ayah_subtitles(identified, path):
    header = """[Script Info]
Title: Verified Quran captions
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Quran,Noto Naskh Arabic,48,&H00FFFFFF,&H000000FF,&H00101010,&H78000000,0,0,0,0,100,100,0,0,1,3,1,2,90,90,340,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows = []
    for verse in identified["verses"]:
        text = verse["text"].replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
        rows.append("Dialogue: 0," + _ass_time(verse["start_seconds"]) + "," + _ass_time(verse["end_seconds"]) + ",Quran,,0,0,0,," + text)
    Path(path).write_text(header + "\n".join(rows) + "\n", encoding="utf-8")
