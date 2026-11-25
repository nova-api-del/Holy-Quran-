import os, json, random, hashlib, subprocess, time, base64, uuid
from pathlib import Path
import requests
from PIL import Image, ImageDraw, ImageFont, ImageStat

ROOT=Path(__file__).resolve().parents[1]
CFG=json.loads((ROOT/"config/sources.json").read_text(encoding="utf-8"))
WORK=ROOT/"work"; WORK.mkdir(exist_ok=True)
LEDGER=WORK/"published.json"
PADDING_MS=0

def sh(cmd): subprocess.run(cmd, check=True)

# Explicitly enable yt-dlp's maintained YouTube JavaScript challenge solvers.
# The workflow installs Deno and yt-dlp[default]; this remote fallback keeps
# the EJS solver available when the runner package cache is incomplete.
YTDLP = ["yt-dlp", "--remote-components", "ejs:github"]
def ledger():
    if not LEDGER.exists(): return {}
    return json.loads(LEDGER.read_text(encoding="utf-8"))

def save_ledger(x):
    tmp=LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(x, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(LEDGER)

def download_cloudinary_video(url, out):
    r=requests.get(url, timeout=180)
    r.raise_for_status()
    out.write_bytes(r.content)

def publish_pending_clip(entry, led, path):
    # A Cloudinary copy lets a later run resume a platform that failed,
    # without regenerating the clip or duplicating a successful upload.
    if not path.exists():
        download_cloudinary_video(entry["video_url"], path)
    if CFG["publish"].get("youtube") and not entry.get("youtube_id"):
        entry["youtube_id"]=youtube_upload(path, entry["title"], entry["caption"])
        save_ledger(led)
        print("YouTube published:", entry["youtube_id"])
    if CFG["publish"].get("instagram") and not entry.get("instagram_id"):
        if not entry.get("video_url"):
            raise RuntimeError("Instagram is enabled but this clip has no Cloudinary video URL.")
        entry["instagram_id"]=instagram_reel(entry["video_url"], entry["caption"])
        save_ledger(led)
        print("Instagram published:", entry["instagram_id"])


def yt_cookie_file():
    raw=os.environ.get("YOUTUBE_COOKIES")
    if not raw: return None
    p=WORK/"youtube_cookies.txt"; p.write_text(raw, encoding="utf-8"); return p

def yt_download(url, out, audio=False):
    base=YTDLP+["--no-playlist","--socket-timeout","30","--retries","10",
                "--fragment-retries","10","--extractor-retries","5","--concurrent-fragments","8"]
    c=yt_cookie_file()
    if c: base += ["--cookies",str(c)]
    if audio:
        sh(base+["-f","bestaudio/best","-x","--audio-format","mp3","-o",str(out),url])
        return
    # Avoid a separate metadata request: YouTube may reject it even when
    # the actual media download is available. Download at most the configured
    # clip length; yt-dlp naturally returns the full source if it is shorter.
    limit=min(58,int(CFG["publish"].get("max_seconds",58)))
    end=float(limit)
    out.unlink(missing_ok=True)
    sh(base+["--download-sections",f"*0-{end}","--force-keyframes-at-cuts",
             "-f","bv*[height<=1080]+ba/b[height<=1080]","--merge-output-format","mp4",
             "-o",str(out),url])

def video_metadata(url):
    p=subprocess.run(YTDLP+["--no-playlist","--skip-download","--dump-single-json",url],
                     capture_output=True,text=True,check=True,timeout=120)
    return json.loads(p.stdout)

def discover_recitation(used_ids):
    queries=[("yasser_al_dosari","ياسر الدوسري قرآن آيات قصيرة"),("abdulrahman_al_mossad","عبدالرحمن مسعد قرآن آيات قصيرة")]
    candidates=[]
    for rid,query in queries:
        try:
            p=subprocess.run(YTDLP+["--flat-playlist","--dump-single-json",f"ytsearch100:{query}"],capture_output=True,text=True,check=True,timeout=240)
            data=json.loads(p.stdout)
        except Exception as e:
            print(f"Search failed for {rid}: {e}"); continue
        for item in data.get("entries",[]):
            vid=item.get("id"); dur=item.get("duration"); title=item.get("title") or ""
            if not vid or not dur: continue  # Repeated recitations are allowed; only the extracted clip is capped.
            low=title.lower()
            terms=["ياسر","الدوسري","yasser","dosari"] if rid=="yasser_al_dosari" else ["عبدالرحمن","مسعد","abdurrahman","abdulrahman","mossad"]
            if not any(t in low for t in terms): continue
            views=int(item.get("view_count") or 0)  # No minimum-view threshold.
            source_url=item.get("url") or f"https://www.youtube.com/watch?v={vid}"
            # The user confirms they have direct permission to publish these reciters' recordings.
            # User permission covers the selected recitation sources.
            candidates.append({"id":vid,"url":source_url,"title":title,"views":views,"duration":float(dur),"reciter_id":rid,
                              "credit_title":title,"credit_creator":rid,"credit_url":source_url})
    if not candidates: raise RuntimeError("لم نعثر على مقاطع قصيرة مناسبة للقارئين. جرّب التشغيل لاحقاً.")
    # Popularity is not a selection criterion: either requested reciter is acceptable.
    random.shuffle(candidates)
    selected=candidates[0]
    selected["reciter"]=next(x["name_ar"] for x in CFG["reciters"] if x["id"]==selected["reciter_id"])
    print(f"Selected recitation source: {selected['title']} | reciter={selected['reciter']}")
    return selected

def review_recitation_audio(audio_path):
    # Gemini ranks recitation performance and tajweed impression, not image quality.
    api_key=os.environ.get("GEMINI_API_KEY")
    if not api_key: raise RuntimeError("GEMINI_API_KEY is required for recitation review.")
    prompt=("استمع إلى هذا المقطع من تلاوة القرآن. قيّم جمال الأداء والخشوع وحسن التنغيم "
            "وسلامة النطق والتجويد المسموع بقدر ما تستطيع، ولا تخترع أخطاء تجويدية إن لم تكن متأكداً. "
            "الأهم اختيار الجزء الأكثر تأثيراً وجمالاً، وليس جودة التسجيل فقط. "
            "أعد JSON فقط بهذه المفاتيح: score من 1 إلى 10، start_seconds و end_seconds "
            "لبداية ونهاية مجموعة آيات مكتملة متتابعة داخل الملف، بحيث لا تتجاوز 58 ثانية "
            "ولا تقطع آية من بدايتها أو نهايتها، وnotes وصف عربي قصير. "
            "إذا لم تستطع تحديد حدود آيات مكتملة بثقة، اجعل start_seconds و end_seconds null.")
    response=requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        params={"key":api_key},
        json={"contents":[{"parts":[{"text":prompt},{"inline_data":{"mime_type":"audio/mpeg",
              "data":base64.b64encode(audio_path.read_bytes()).decode("ascii")}}]}],
              "generationConfig":{"temperature":0,"responseMimeType":"application/json"}},
        timeout=180)
    response.raise_for_status()
    raw=response.json()["candidates"][0]["content"]["parts"][0]["text"]
    result=json.loads(raw)
    score=float(result.get("score",0))
    start=result.get("start_seconds"); end=result.get("end_seconds")
    if start is not None and end is not None:
        start=float(start); end=float(end)
        if start < 0 or end <= start or end-start > 58.5: start=end=None
    else: start=end=None
    return {"score":score,"start":start,"end":end,"notes":str(result.get("notes") or "")}

def download_complete_short(url, audio_out, source_duration=None):
    max_seconds=min(58,int(CFG["publish"].get("max_seconds",58)))
    total=float(source_duration or max_seconds)
    base=YTDLP+["--no-playlist","--socket-timeout","30","--retries","10","--fragment-retries","10",
                "--extractor-retries","5","--concurrent-fragments","8"]
    cookies=yt_cookie_file()
    if cookies: base += ["--cookies",str(cookies)]
    # For short source recordings, use the entire source so its ending is preserved.
    if total <= max_seconds:
        start=0.0; end=total
        print("Source is already <=58s; preserving the complete recitation.")
    else:
        # Try several positions; Gemini judges performance/tajweed and proposes complete-ayah boundaries.
        sample_len=min(total,78.0)
        max_start=max(0.0,total-sample_len)
        starts=sorted(set(round(max_start*x,2) for x in (0.03,0.30,0.58,0.86)))
        reviewed=[]
        for i,point in enumerate(starts):
            preview=WORK/f"recitation_review_{i}.mp3"
            preview.unlink(missing_ok=True)
            stop=min(total,point+sample_len)
            try:
                sh(base+["--download-sections",f"*{point}-{stop}","-f","bestaudio/best",
                     "-x","--audio-format","mp3","--force-overwrites","-o",str(preview),url])
                verdict=review_recitation_audio(preview)
                if verdict["start"] is not None and verdict["end"] is not None:
                    absolute_start=point+verdict["start"]
                    absolute_end=point+verdict["end"]
                    if (0 <= verdict["start"] < verdict["end"] <= stop-point
                            and absolute_start >= point-0.5
                            and absolute_end-absolute_start <= max_seconds
                            and absolute_end <= total+0.5):
                        reviewed.append((verdict["score"],absolute_start,absolute_end,verdict["notes"]))
                        print(f"Recitation review {i+1}: score={verdict['score']:.1f}/10 | {verdict['notes']}")
                    else: print(f"Recitation review {i+1}: proposed range is outside limits.")
                else: print(f"Recitation review {i+1}: no confident complete-ayah boundaries.")
            except Exception as e:
                print(f"Recitation review {i+1} failed: {e}")
            finally:
                preview.unlink(missing_ok=True)
        if reviewed:
            # Prefer the best performance score; if scores are effectively tied,
            # randomize among verified complete-ayah segments only.
            top=max(x[0] for x in reviewed)
            finalists=[x for x in reviewed if x[0] >= top-0.25]
            score,start,end,notes=random.choice(finalists)
            print(f"Complete-ayah recitation selected: {start:.2f}-{end:.2f}s | score={score:.1f}/10 | {notes}")
        else:
            # No arbitrary 58-second crop: without verified ayah boundaries we cannot
            # guarantee the recitation will not be cut mid-ayah.
            raise RuntimeError("لم أتمكن من تحديد حدود آيات مكتملة بثقة. لم أنشر مقطعاً عشوائياً حتى لا تُقطع آية؛ أعد التشغيل لاختيار تسجيل آخر للقارئ نفسه أو القارئ الآخر.")
    audio_out.unlink(missing_ok=True)
    sh(base+["--download-sections",f"*{start}-{end}","-f","bestaudio/best",
         "-x","--audio-format","mp3","--force-overwrites","-o",str(audio_out),url])

def duration(p):
    r=subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(p)],capture_output=True,text=True,check=True)
    return float(r.stdout.strip())

class SceneRejected(Exception):
    pass

def scene_has_branding(video_path):
    api_key=os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is required for the visual logo/watermark check.")
    total=duration(video_path)
    timestamps=sorted(set(max(0.1,min(total-0.1,total*x)) for x in (0.05,0.15,0.25,0.35,0.45,0.55,0.65,0.75,0.85,0.95)))
    parts=[{"text":"Inspect these sampled frames from one video. Detect any visible company logo, brand mark, product logo, channel logo, watermark, or corporate emblem, including graphic symbols without readable text. Ignore natural shapes and ordinary scenery. Return only JSON: {\"has_branding\": true or false, \"evidence\": \"short description\"}. If uncertain, set has_branding to true."}]
    frame_files=[]
    try:
        for i,ts in enumerate(timestamps):
            frame=WORK/f"logo_check_{i}.jpg"
            subprocess.run(["ffmpeg","-y","-ss",f"{ts:.3f}","-i",str(video_path),"-frames:v","1","-vf","scale=640:-2","-q:v","4",str(frame)],capture_output=True,check=True)
            if not frame.exists() or frame.stat().st_size==0:
                raise RuntimeError("Could not extract frames for logo detection.")
            frame_files.append(frame)
            parts.append({"inline_data":{"mime_type":"image/jpeg","data":base64.b64encode(frame.read_bytes()).decode("ascii")}})
        endpoint="https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
        response=requests.post(endpoint,params={"key":api_key},json={
            "contents":[{"parts":parts}],
            "generationConfig":{"temperature":0,"responseMimeType":"application/json"}
        },timeout=120)
        response.raise_for_status()
        answer=response.json()["candidates"][0]["content"]["parts"][0]["text"]
        verdict=json.loads(answer)
        if not isinstance(verdict.get("has_branding"),bool):
            raise RuntimeError("Gemini returned an invalid logo-check result.")
        print("Visual branding check:",json.dumps(verdict,ensure_ascii=False))
        return verdict["has_branding"]
    finally:
        for frame in frame_files:
            frame.unlink(missing_ok=True)


def choose_complete_ayah_block(rec, used):
    verses=rec.get("verses") or []
    if not verses:
        raise RuntimeError(f"No verified verse timestamps for {rec['name_ar']}; refusing to cut or publish.")
    verses=sorted(verses,key=lambda v:(int(v["verse_key"].split(":")[0]),int(v["verse_key"].split(":")[1])))
    chapter=int(verses[0]["verse_key"].split(":")[0])
    for v in verses:
        if int(v["verse_key"].split(":")[0]) != chapter:
            raise RuntimeError("A source entry cannot mix surahs.")
        if int(v["start_ms"]) < 0 or int(v["end_ms"]) <= int(v["start_ms"]):
            raise RuntimeError(f"Invalid timestamp range for {v['verse_key']}.")
    max_ms=int(CFG["publish"]["max_seconds"]*1000)
    candidates=[]
    for i,first in enumerate(verses):
        block=[first]
        for nxt in verses[i+1:]:
            expected=int(block[-1]["verse_key"].split(":")[1])+1
            current=int(nxt["verse_key"].split(":")[1])
            if current != expected: break
            if int(nxt["end_ms"])-int(first["start_ms"]) > max_ms: break
            block.append(nxt)
        # One long ayah is allowed in full; never trim it to fit a target duration.
        key=",".join(v["verse_key"] for v in block)
        if key not in used: candidates.append(block)
    if not candidates:
        raise RuntimeError("No unused complete consecutive ayah block is available.")
    block=random.choice(candidates)
    # Use the verified ayah boundaries exactly; padding can leak adjacent recitation.
    start=int(block[0]["start_ms"])
    end=int(block[-1]["end_ms"])
    return block,start,end

def extract_audio(source_audio, start_ms, end_ms, out):
    if end_ms <= start_ms: raise RuntimeError("Invalid audio clip range.")
    sh(["ffmpeg","-y","-ss",f"{start_ms/1000:.3f}","-i",str(source_audio),
        "-t",f"{(end_ms-start_ms)/1000:.3f}","-vn","-c:a","libmp3lame","-q:a","2",str(out)])

def scene_url(excluded_urls=None):

    excluded_urls=excluded_urls or set()
    configured=[x for x in CFG.get("visual_sources",[]) if x.get("enabled") and x.get("url") and x.get("url") not in excluded_urls]
    if configured:
        fresh_configured=[x for x in configured if x.get("url") not in excluded_urls]
        if fresh_configured:
            source=random.choice(fresh_configured)
            return ("youtube",source["url"],{"title":source.get("title") or "YouTube visual footage",
                                            "creator":source.get("creator") or "غير محدد",
                                            "url":source["url"]})
    queries=[
        "cinematic nature scenery 4k mountains clouds river",
        "peaceful ocean waves sunset cinematic 4k",
        "beautiful forest waterfall nature cinematic footage",
        "night sky stars clouds cinematic landscape",
        "mosque architecture peaceful cinematic footage"
    ]
    query=random.choice(queries)
    p=subprocess.run(
        YTDLP+["--flat-playlist","--dump-single-json","--socket-timeout","30",
               "--retries","5","--extractor-retries","5",f"ytsearch30:{query}"],
        capture_output=True,text=True,check=True,timeout=180
    )
    data=json.loads(p.stdout)
    candidates=[]
    for item in data.get("entries",[]):
        vid=item.get("id")
        dur=item.get("duration")
        if not vid or not dur: continue
        source_url=item.get("url") or f"https://www.youtube.com/watch?v={vid}"
        if source_url in excluded_urls: continue
        candidates.append({"url":source_url,"title":item.get("title") or "YouTube visual footage",
                           "creator":item.get("channel") or item.get("uploader") or "غير محدد"})
    if not candidates:
        # Reuse a previous scene only when no fresh visual candidate is available.
        previous=[e for e in ledger().get("clips",{}).values() if e.get("scene_url")]
        if previous:
            fallback=random.choice(previous)
            reused_url=fallback["scene_url"]
            print("No new visual scenes found; reusing a previous scene as fallback.")
            return ("youtube",reused_url,{"title":"مشهد سابق من فيديو منشور",
                                          "creator":"مصدر مستخدم سابقاً",
                                          "url":reused_url})
        raise RuntimeError("No visual footage candidates were found and no previous scene is available.")
    chosen=random.choice(candidates[:min(15,len(candidates))])
    credit={"title":chosen["title"],"creator":chosen["creator"],"url":chosen["url"]}
    return ("youtube",chosen["url"],credit)

def font_path():
    for p in ["/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf","/usr/share/fonts/opentype/noto/NotoNaskhArabic-Regular.ttf"]:
        if os.path.exists(p): return p
    return "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

def make_video(audio,scene,out,reciter,verse_label):
    scene_path=WORK/"scene.mp4"
    if scene[0]=="youtube":
        yt_download(scene[1],scene_path,audio=False)
    else:
        r=requests.get(scene[1],timeout=(15,120)); r.raise_for_status(); scene_path.write_bytes(r.content)
    # Branding/logo screening disabled as requested.
    dur=duration(audio)
    img=Image.new("RGBA",(1080,1920),(0,0,0,0)); d=ImageDraw.Draw(img)
    f=ImageFont.truetype(font_path(),CFG["visual_style"]["font_size"])
    text="القرآن الكريم"; b=d.textbbox((0,0),text,font=f); x=(1080-(b[2]-b[0]))//2; y=CFG["visual_style"]["subtitle_y"]
    d.text((x+3,y+5),text,font=f,fill=(0,0,0,190),stroke_width=3,stroke_fill=(0,0,0,160))
    d.text((x,y),text,font=f,fill="white",stroke_width=1,stroke_fill="white")
    sf=ImageFont.truetype(font_path(),30)
    d.text((540,CFG["visual_style"]["reciter_y"]),f"{reciter}  •  {verse_label}",font=sf,anchor="ma",fill=(245,235,205,235))
    overlay=WORK/"overlay.png"; img.save(overlay)
    # One video encode only: scale/crop, darken, overlay text, and mux recitation.
    sh(["ffmpeg","-y","-threads","2","-stream_loop","-1","-i",str(scene_path),"-i",str(audio),
        "-loop","1","-i",str(overlay),
        "-filter_complex",
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,eq=brightness=-0.10:saturation=0.80[bg];[2:v]format=rgba[ov];[bg][ov]overlay=0:0:format=auto[v]",
        "-map","[v]","-map","1:a:0","-t",f"{dur:.3f}","-c:v","libx264","-preset","veryfast",
        "-crf","19","-threads","2","-c:a","aac","-b:a","192k","-shortest","-movflags","+faststart",str(out)])

def cloudinary_upload(path):
    import cloudinary, cloudinary.uploader
    cloudinary.config(cloud_name=os.environ["CLOUDINARY_CLOUD_NAME"],api_key=os.environ["CLOUDINARY_API_KEY"],api_secret=os.environ["CLOUDINARY_API_SECRET"])
    return cloudinary.uploader.upload_large(str(path),resource_type="video",folder="holy-quran")["secure_url"]

def youtube_upload(path,title,description):
    import re
    from google.auth.transport.requests import Request
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    names=("YOUTUBE_CLIENT_ID","YOUTUBE_CLIENT_SECRET","YOUTUBE_REFRESH_TOKEN")
    values={name:os.environ.get(name,"") for name in names}
    metadata={
        name: {
            "present":bool(value),
            "length":len(value),
            "has_outer_whitespace":bool(value) and value != value.strip(),
            "fingerprint_sha256_12":hashlib.sha256(value.encode("utf-8")).hexdigest()[:12] if value else None
        } for name,value in values.items()
    }
    report_path=WORK/"diagnostics.json"
    report=json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    diagnostic_secrets=report.get("secrets",{})
    comparisons={}
    for name,item in metadata.items():
        previous=diagnostic_secrets.get(name,{})
        comparisons[name]={
            "fingerprint_matches_diagnostics":bool(item["fingerprint_sha256_12"]) and item["fingerprint_sha256_12"] == previous.get("fingerprint_sha256_12"),
            "length_matches_diagnostics":item["length"] == previous.get("length"),
            "present_in_diagnostics":bool(previous.get("present"))
        }
    report["upload_process"]={
        "checked_at_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
        "secrets":metadata,
        "comparison_with_diagnostics":comparisons,
        "oauth_refresh_tested_in_upload_process":False,
        "oauth_refresh_ok":False
    }
    report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print("YouTube OAuth secret fingerprint comparison:",
          json.dumps({k:v["fingerprint_matches_diagnostics"] for k,v in comparisons.items()}))

    if any(not value for value in values.values()):
        raise RuntimeError("YouTube OAuth refresh failed: missing_required_secret")
    c=Credentials(token=None,refresh_token=values["YOUTUBE_REFRESH_TOKEN"],token_uri="https://oauth2.googleapis.com/token",client_id=values["YOUTUBE_CLIENT_ID"],client_secret=values["YOUTUBE_CLIENT_SECRET"],scopes=["https://www.googleapis.com/auth/youtube.upload"])
    try:
        c.refresh(Request())
        report["upload_process"]["oauth_refresh_tested_in_upload_process"]=True
        report["upload_process"]["oauth_refresh_ok"]=True
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    except RefreshError as exc:
        error_text=" ".join(str(part) for part in exc.args)
        match=re.search(r"invalid_grant|invalid_client|unauthorized_client|access_denied",error_text)
        error_code=match.group(0) if match else "refresh_error"
        report["upload_process"]["oauth_refresh_tested_in_upload_process"]=True
        report["upload_process"]["oauth_refresh_ok"]=False
        report["upload_process"]["oauth_error_code"]=error_code
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        raise RuntimeError("YouTube OAuth refresh failed: "+error_code) from None
    yt=build("youtube","v3",credentials=c)
    body={"snippet":{"title":title,"description":description,"categoryId":"22"},"status":{"privacyStatus":"public","selfDeclaredMadeForKids":False}}
    req=yt.videos().insert(part="snippet,status",body=body,media_body=MediaFileUpload(str(path),chunksize=8*1024*1024,resumable=True))
    return req.execute()["id"]

def instagram_reel(url,caption):
    token=os.environ["META_ACCESS_TOKEN"]; ig=os.environ["IG_USER_ID"]
    r=requests.post(f"https://graph.facebook.com/v27.0/{ig}/media",data={"media_type":"REELS","video_url":url,"caption":caption,"access_token":token},timeout=60)
    r.raise_for_status(); cid=r.json()["id"]
    for _ in range(30):
        s=requests.get(f"https://graph.facebook.com/v27.0/{cid}",params={"fields":"status_code","access_token":token},timeout=30)
        s.raise_for_status()
        if s.json().get("status_code")=="FINISHED": break
        time.sleep(10)
    p=requests.post(f"https://graph.facebook.com/v27.0/{ig}/media_publish",data={"creation_id":cid,"access_token":token},timeout=60)
    p.raise_for_status(); return p.json()["id"]

def main():
    led=ledger()
    clips=led.setdefault("clips",{})
    youtube_on=bool(CFG["publish"].get("youtube"))
    instagram_requested=bool(CFG["publish"].get("instagram"))
    instagram_secrets=["CLOUDINARY_CLOUD_NAME","CLOUDINARY_API_KEY","CLOUDINARY_API_SECRET","META_ACCESS_TOKEN","IG_USER_ID"]
    instagram_on=instagram_requested and all(os.environ.get(k) for k in instagram_secrets)
    if instagram_requested and not instagram_on:
        print("Instagram is configured but its Cloudinary/Meta secrets are incomplete; YouTube can still publish.")
    if not youtube_on and not instagram_on: raise SystemExit("Both publishing platforms are disabled.")
    for clip_key,entry in clips.items():
        yt_done=(not youtube_on) or bool(entry.get("youtube_id"))
        ig_done=(not instagram_on) or bool(entry.get("instagram_id"))
        if entry.get("video_url") and not (yt_done and ig_done):
            publish_pending_clip(entry,led,WORK/(clip_key+".mp4")); return
    # Reuse of a recitation is allowed. Give every generated video its own key
    # so the ledger/cache never blocks a previously published source recording.
    selected=discover_recitation(set())
    clip_key=uuid.uuid4().hex[:12]
    audio=WORK/(clip_key+".mp3")
    if not audio.exists(): download_complete_short(selected["url"],audio,selected["duration"])
    out=WORK/(clip_key+".mp4")
    previous_scenes={e.get("scene_url") for e in clips.values() if e.get("scene_url")}
    scene=scene_url(previous_scenes)
    make_video(audio,scene,out,selected["reciter"],"تلاوة كاملة")
    public_url=cloudinary_upload(out) if instagram_on else None
    title=f"تلاوة قرآنية خاشعة | {selected['reciter']} #Shorts"
    audio_credit=(f"الصوت: {selected['credit_title']} — {selected['credit_creator']}\n"
                  f"المصدر: {selected['credit_url']}")
    visual_credit=(f"المشاهد: {scene[2]['title']} — {scene[2]['creator']}\n"
                   f"المصدر: {scene[2]['url']}")
    caption=(f"تلاوة قرآنية بصوت {selected['reciter']}\nاللهم اجعل القرآن ربيع قلوبنا.\n"
             "#القرآن #قرآن #تلاوة #ياسر_الدوسري #عبدالرحمن_مسعد #quran #shorts\n\n"
             f"{audio_credit}\n\n{visual_credit}\n"
             "التعديلات: استخراج الصوت، قصّ المشاهد ودمجها مع التلاوة والنص.")
    entry={"source_video_id":selected["id"],"source_title":selected["title"],"source_views":selected["views"],
           "reciter":selected["reciter"],"title":title,"caption":caption,"video_url":public_url,"scene_url":scene[1],
           "youtube_id":None,"instagram_id":None,"created_at":int(time.time())}
    clips[clip_key]=entry; save_ledger(led)
    publish_pending_clip(entry,led,out)

if __name__=="__main__": main()
