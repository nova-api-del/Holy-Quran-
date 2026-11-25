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
    prompt = ("استمع إلى الملف الصوتي كاملاً وحدد الآيات القرآنية التي تُتلى كاملة من بدايتها إلى نهايتها. القارئ المتوقع: " + reciter + ".\\n"
      +'أخرج JSON فقط بالشكل {"verses":[{"surah":1,"ayah":1,"start_seconds":0.0,"end_seconds":4.2}]}\\n'
      +"لا تدرج آية يبدأ الصوت من وسطها أو ينتهي قبل اكتمالها. لا تخمّن. إذا لم تجزم فأرجع verses فارغة. كل عنصر آية واحدة، مع رقم السورة والآية وبداية ونهاية زمنيتين بالثواني. الآيات متتابعة من سورة واحدة. مدة الصوت "+str(duration_seconds)+" ثانية. لا تكتب نص القرآن؛ سيؤخذ من مصحف موثوق.")
    data = _gemini(prompt, audio_path)
    raw = data.get("verses")
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("Gemini could not confidently identify complete Quran verses.")
    verses = []; sn = None; prev = None
    for x in raw:
        s, a = int(x["surah"]), int(x["ayah"])
        start, end = float(x["start_seconds"]), float(x["end_seconds"])
        if not (1 <= s <= 114 and a >= 1 and 0 <= start < end <= duration_seconds + 0.75):
            raise RuntimeError("Invalid verse numbers or timestamps from Gemini.")
        if sn is None: sn = s
        if s != sn or (prev is not None and a != prev + 1):
            raise RuntimeError("Verses are not consecutive in one surah.")
        prev = a
        verses.append({"surah": s, "ayah": a, "start_seconds": max(0, start), "end_seconds": min(duration_seconds, end)})
    response = requests.get(QURAN_API + "/surah/" + str(sn) + "/quran-uthmani", timeout=45)
    response.raise_for_status()
    data = response.json()["data"]
    canonical = {int(x["numberInSurah"]): x["text"] for x in data["ayahs"]}
    for verse in verses:
        if verse["ayah"] not in canonical:
            raise RuntimeError("Canonical Quran text missing.")
        verse["text"] = canonical[verse["ayah"]]
    for i, verse in enumerate(verses):
        if verse["end_seconds"] - verse["start_seconds"] < 0.35:
            raise RuntimeError("Ayah timing too short.")
        if i and verse["start_seconds"] < verses[i-1]["end_seconds"] - 0.25:
            raise RuntimeError("Overlapping ayah timestamps.")
    return {"surah": sn, "surah_name": data.get("name") or "السورة " + str(sn), "verses": verses}

def _local_visual_queries(identified):
    # Deterministic offline fallback: map Quranic Arabic themes to matching
    # footage searches; no model/API call is needed for scene selection.
    text = " ".join(v.get("text", "") for v in identified.get("verses", []))
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
    body = "\\n".join(str(v["ayah"]) + ". " + v["text"] for v in identified["verses"])
    prompt = ("Understand these canonical Quran verses and return JSON only: {\"queries\":[\"English search query\"]}. Give 6-10 concise English YouTube searches for respectful cinematic footage directly reflecting their meaning: nature, sky, landscape, water, architecture, light. Avoid people, faces, religious depictions, text overlays, music videos and unrelated scenery.\\n" + identified["surah_name"] + "\\n" + body)
    try:
        queries = _gemini(prompt).get("queries")
        if isinstance(queries, list):
            queries = [str(x).strip() for x in queries if str(x).strip()]
            if len(queries) >= 2:
                return queries[:10]
        print("Gemini scene response unusable; using offline Quran-theme scene matching.")
    except Exception as exc:
        print(f"Gemini scene analysis unavailable ({exc}); using offline Quran-theme scene matching.")
    return _local_visual_queries(identified)

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
        text = verse["text"].replace("\\\\", r"\\\\").replace("{", r"\\{").replace("}", r"\\}")
        rows.append("Dialogue: 0," + _ass_time(verse["start_seconds"]) + "," + _ass_time(verse["end_seconds"]) + ",Quran,,0,0,0,," + text)
    Path(path).write_text(header + "\\n".join(rows) + "\\n", encoding="utf-8")
