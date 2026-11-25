import json, os
from pathlib import Path
import requests
GEMINI_MODEL=os.getenv("GEMINI_MODEL","gemini-3.8-flash")
GEMINI_URL="https://generativelanguage.googleapis.com/v1beta/models/"+GEMINI_MODEL+":generateContent"
QURAN_API="https://api.alquran.cloud/v1"
def _gemini(prompt,audio_path=None):
    key=os.getenv("GEMINI_API_KEY","").strip()
    if not key: raise RuntimeError("GEMINI_API_KEY is missing; refusing to publish without verified verse captions.")
    parts=[{"text":prompt}]
    if audio_path:
        import base64
        raw=Path(audio_path).read_bytes()
        if len(raw)>18*1024*1024: raise RuntimeError("Audio is too large for inline Gemini analysis.")
        parts.insert(0,{"inline_data":{"mime_type":"audio/mpeg","data":base64.b64encode(raw).decode("ascii")}})
    r=requests.post(GEMINI_URL,params={"key":key},json={"contents":[{"role":"user","parts":parts}],"generationConfig":{"temperature":0.1,"responseMimeType":"application/json"}},timeout=180)
    if r.status_code>=400: raise RuntimeError(f"Gemini API request failed ({r.status_code}): {r.text[:500]}")
    try: return json.loads("".join(p.get("text","") for p in r.json()["candidates"][0]["content"]["parts"]))
    except Exception as e: raise RuntimeError("Gemini returned invalid JSON.") from e
def identify_and_verify_verses(audio_path,duration_seconds,reciter):
    prompt=("استمع إلى الملف الصوتي كاملاً وحدد الآيات القرآنية التي تُتلى كاملة من بدايتها إلى نهايتها. القارئ المتوقع: "+reciter+".\n"
      +'أخرج JSON فقط بالشكل {"verses":[{"surah":1,"ayah":1,"start_seconds":0.0,"end_seconds":4.2}]}\n'
      +"لا تدرج آية يبدأ الصوت من وسطها أو ينتهي قبل اكتمالها. لا تخمّن. إذا لم تجزم فأرجع verses فارغة. كل عنصر آية واحدة، مع رقم السورة والآية وبداية ونهاية زمنيتين بالثواني. الآيات متتابعة من سورة واحدة. مدة الصوت "+str(duration_seconds)+" ثانية. لا تكتب نص القرآن؛ سيؤخذ من مصحف موثوق.")
    data=_gemini(prompt,audio_path); raw=data.get("verses")
    if not isinstance(raw,list) or not raw: raise RuntimeError("Gemini could not confidently identify complete Quran verses.")
    verses=[]; sn=None; prev=None
    for x in raw:
        s,a=int(x["surah"]),int(x["ayah"]); start,end=float(x["start_seconds"]),float(x["end_seconds"])
        if not (1<=s<=114 and a>=1 and 0<=start<end<=duration_seconds+0.75): raise RuntimeError("Invalid verse numbers or timestamps from Gemini.")
        if sn is None: sn=s
        if s!=sn or (prev is not None and a!=prev+1): raise RuntimeError("Verses are not consecutive in one surah.")
        prev=a; verses.append({"surah":s,"ayah":a,"start_seconds":max(0,start),"end_seconds":min(duration_seconds,end)})
    r=requests.get(QURAN_API+"/surah/"+str(sn)+"/quran-uthmani",timeout=45); r.raise_for_status(); d=r.json()["data"]
    canonical={int(x["numberInSurah"]):x["text"] for x in d["ayahs"]}
    for v in verses:
        if v["ayah"] not in canonical: raise RuntimeError("Canonical Quran text missing.")
        v["text"]=canonical[v["ayah"]]
    for i,v in enumerate(verses):
        if v["end_seconds"]-v["start_seconds"]<0.35: raise RuntimeError("Ayah timing too short.")
        if i and v["start_seconds"]<verses[i-1]["end_seconds"]-0.25: raise RuntimeError("Overlapping ayah timestamps.")
    return {"surah":sn,"surah_name":d.get("name") or "السورة "+str(sn),"verses":verses}
def semantic_visual_queries(identified):
    body="\n".join(str(v["ayah"])+". "+v["text"] for v in identified["verses"])
    prompt=("Understand these canonical Quran verses and return JSON only: {\"queries\":[\"English search query\"]}. Give 6-10 concise English YouTube searches for respectful cinematic footage directly reflecting their meaning: nature, sky, landscape, water, architecture, light. Avoid people, faces, religious depictions, text overlays, music videos and unrelated scenery.\n"+identified["surah_name"]+"\n"+body)
    q=_gemini(prompt).get("queries")
    if not isinstance(q,list): raise RuntimeError("Gemini did not return semantic scene queries.")
    q=[str(x).strip() for x in q if str(x).strip()]
    if len(q)<2: raise RuntimeError("Too few semantic scene queries.")
    return q[:10]
def _ass_time(sec):
    cs=max(0,int(round(float(sec)*100))); h,rem=divmod(cs,360000); m,rem=divmod(rem,6000); s,c=divmod(rem,100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"
def write_ayah_subtitles(identified,path):
    header="""[Script Info]
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
    rows=[]
    for v in identified["verses"]:
        t=v["text"].replace("\\",r"\\").replace("{",r"\{").replace("}",r"\}")
        rows.append("Dialogue: 0,"+_ass_time(v["start_seconds"])+","+_ass_time(v["end_seconds"])+",Quran,,0,0,0,,"+t)
    Path(path).write_text(header+"\n".join(rows)+"\n",encoding="utf-8")
