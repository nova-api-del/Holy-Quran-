import os, json, random, hashlib, subprocess, time, tempfile, shutil
from pathlib import Path
import requests
from PIL import Image, ImageDraw, ImageFont
from quran_pipeline import identify_and_verify_verses, semantic_visual_queries, write_ayah_subtitles

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


COOKIE_PATH=Path(tempfile.gettempdir())/"quran_yt_cookies.txt"

def yt_cookie_file():
    # Cookies live outside the repository so the workflow can never commit them.
    raw=os.environ.get("YOUTUBE_COOKIES")
    if not raw: return None
    COOKIE_PATH.write_text(raw, encoding="utf-8")
    os.chmod(COOKIE_PATH,0o600)
    return COOKIE_PATH

def cookie_args():
    c=yt_cookie_file()
    return ["--cookies",str(c)] if c else []

def discover_recitation(used_ids):
    candidates=[]
    for rec in CFG["reciters"]:
        if not rec.get("enabled",True): continue
        rid=rec["id"]; query=rec.get("search_query") or (rec["name_ar"]+" قرآن تلاوة قصيرة")
        try:
            p=subprocess.run(YTDLP+["--flat-playlist","--dump-single-json",f"ytsearch30:{query}"],capture_output=True,text=True,check=True,timeout=180)
            data=json.loads(p.stdout)
        except Exception as e:
            print(f"Search failed for {rid}: {e}"); continue
        terms=[str(t).lower() for t in rec.get("title_terms",[])]
        for item in data.get("entries",[]):
            vid=item.get("id"); dur=item.get("duration"); title=item.get("title") or ""
            if not vid or vid in used_ids or not dur or float(dur)>int(CFG["publish"].get("max_seconds",58)): continue
            if terms and not any(t in title.lower() for t in terms): continue
            views=int(item.get("view_count") or 0)
            if views<500: continue
            candidates.append({"id":vid,"url":item.get("url") or f"https://www.youtube.com/watch?v={vid}","title":title,"views":views,"duration":float(dur),"reciter_id":rid,"reciter":rec["name_ar"]})
    if not candidates: raise RuntimeError("لم نعثر على مقاطع قصيرة مناسبة للقراء المحددين.")
    random.shuffle(candidates); candidates.sort(key=lambda x:x["views"],reverse=True)
    return candidates

def pick_and_download(used_ids, max_tries=8):
    tries=0
    for selected in discover_recitation(used_ids):
        if tries>=max_tries: break
        tries+=1
        clip_key=hashlib.sha256(selected["id"].encode()).hexdigest()[:12]
        source_video=WORK/(clip_key+"_source.mp4"); audio=WORK/(clip_key+".mp3")
        print(f"Trying {selected['title'][:70]} | views={selected['views']} | {selected['reciter']}")
        try:
            if not source_video.exists() or not audio.exists():
                download_complete_short(selected["url"],source_video,audio)
        except (subprocess.CalledProcessError, OSError) as e:
            print(f"Skipping {selected['id']} (unavailable or download failed): {e}")
            continue
        return selected,clip_key,source_video,audio
    raise RuntimeError(f"No recitation could be downloaded after {tries} attempts.")

def download_complete_short(url, video_out, audio_out):
    cmd=YTDLP+["--no-playlist","--retries","3","--fragment-retries","3","-f","bv*[height<=1080]+ba/b[height<=1080]","--merge-output-format","mp4","-o",str(video_out),url]
    cookies=yt_cookie_file()
    if cookies: cmd[1:1]=["--cookies",str(cookies)]
    sh(cmd)
    sh(["ffmpeg","-y","-i",str(video_out),"-vn","-acodec","libmp3lame","-q:a","2",str(audio_out)])

def duration(p):
    r=subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(p)],capture_output=True,text=True,check=True)
    return float(r.stdout.strip())

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

VISUAL_DEFAULTS={
    "queries":[
        "4k nature drone footage mountains",
        "4k waterfall forest nature cinematic",
        "4k ocean waves sunset drone",
        "4k night sky stars milky way timelapse",
        "4k clouds timelapse sky",
        "4k desert dunes aerial drone",
        "4k snow mountains aerial drone",
        "4k misty forest morning cinematic",
        "4k lake reflection sunrise landscape",
        "4k aurora northern lights",
        "4k mosque architecture cinematic",
        "4k river valley aerial autumn"
    ],
    "blocked_words":[
        "lyrics","music video","song","nasheed","recitation","quran","reaction","gameplay",
        "tutorial","review","vlog","news","tiktok","shorts",
        "girl","woman","women","model","dance","bikini","wedding","people","crowd"
    ],
    "min_source_seconds":40,
    "max_source_seconds":3600,
    "segment_seconds":5.0,
    "min_segments":4,
    "max_segments":10,
    "crossfade_seconds":0.5,
    "min_height":1080,
    "preferred_height":1440,
    "queries_per_run":5,
    "results_per_query":25,
    "max_attempt_factor":3
}
QUALITY_HINTS=("4k","uhd","2160","8k","5k","hdr")

def visual_cfg():
    v=dict(VISUAL_DEFAULTS); v.update(CFG.get("visuals") or {}); return v

def segment_format(v):
    # Prefer 1440p-2160p so the 9:16 centre crop is downscaled (sharp) rather than
    # upscaled; fall back to 1080p+. AV1 is skipped for decoder compatibility.
    lo=int(v["min_height"]); pref=int(v["preferred_height"])
    return (f"bv*[height>={pref}][height<=2160][vcodec!^=av01]/"
            f"bv*[height>={lo}][height<=2160][vcodec!^=av01]")

def video_height(p):
    r=subprocess.run(["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=height",
                      "-of","default=noprint_wrappers=1:nokey=1",str(p)],capture_output=True,text=True,check=True)
    return int(r.stdout.strip().splitlines()[0])

def search_visual_candidates(v, used, semantic_queries=None):
    # Picture-only B-roll found directly on YouTube; its soundtrack is never used.
    queries=list(semantic_queries or v["queries"])
    picked=random.sample(queries,k=min(int(v["queries_per_run"]),len(queries)))
    pool={}
    for q in picked:
        try:
            p=subprocess.run(YTDLP+["--flat-playlist","--dump-single-json",f"ytsearch{int(v['results_per_query'])}:{q}"],
                             capture_output=True,text=True,check=True,timeout=180)
            data=json.loads(p.stdout)
        except Exception as e:
            print(f"Visual search failed for '{q}': {e}"); continue
        for item in data.get("entries") or []:
            vid=item.get("id"); dur=item.get("duration"); title=item.get("title") or ""
            if not vid or vid in used or vid in pool or not dur: continue
            dur=float(dur)
            if dur<float(v["min_source_seconds"]) or dur>float(v["max_source_seconds"]): continue
            low=title.lower()
            if any(w in low for w in v["blocked_words"]): continue
            pool[vid]={"id":vid,"url":item.get("url") or f"https://www.youtube.com/watch?v={vid}",
                       "title":title,"duration":dur,"hinted":any(h in low for h in QUALITY_HINTS)}
    cands=list(pool.values())
    random.shuffle(cands)
    cands.sort(key=lambda c: not c["hinted"])  # titles that advertise 4K/UHD first
    print(f"Visual candidates: {len(cands)} ({sum(c['hinted'] for c in cands)} advertise 4K/UHD)")
    return cands

def fetch_scene_segment(cand, seg_len, tmp, idx, v):
    # Download only a short window from the middle of the video (skips intros/outros).
    total=cand["duration"]; need=seg_len+4.0
    lo=total*0.10; hi=total*0.90-need
    start=random.uniform(lo,hi) if hi>lo else max(0.0,(total-need)/2)
    s=max(0.0,start-1.0); e=min(total,s+need)
    cmd=YTDLP+cookie_args()+["--no-playlist","--retries","3","--fragment-retries","3",
        "-f",segment_format(v),"--download-sections",f"*{s:.2f}-{e:.2f}",
        "-o",str(tmp/f"raw{idx}.%(ext)s"),cand["url"]]
    sh(cmd)
    files=[f for f in tmp.glob(f"raw{idx}.*") if f.suffix.lower() in (".mp4",".webm",".mkv",".mov")]
    if not files: raise RuntimeError("section download produced no file")
    raw=files[0]
    h=video_height(raw)
    if h<int(v["min_height"]): raise RuntimeError(f"source too small ({h}p)")
    return raw, start-s, h

def normalize_segment(src, offset, seg_len, out):
    vf=("scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920,setsar=1,fps=30,"
        "eq=brightness=-0.06:saturation=0.92,unsharp=5:5:0.35,format=yuv420p")
    sh(["ffmpeg","-y","-ss",f"{offset:.3f}","-i",str(src),"-t",f"{seg_len:.3f}","-vf",vf,
        "-an","-c:v","libx264","-preset","fast","-crf","15",str(out)])

def build_scene_segments(dur, led, tmp, semantic_queries=None):
    v=visual_cfg(); c=float(v["crossfade_seconds"])
    n=int(min(int(v["max_segments"]),max(int(v["min_segments"]),round(dur/float(v["segment_seconds"])))))
    seg_len=(dur+(n-1)*c)/n+0.25
    used=set((led.get("scenes") or {}).keys())
    cands=search_visual_candidates(v,used,semantic_queries)
    if not cands: raise RuntimeError("No suitable YouTube visual footage found.")
    segs=[]; chosen=[]; attempts=0; max_attempts=n*int(v["max_attempt_factor"])
    for cand in cands:
        if len(segs)>=n or attempts>=max_attempts: break
        attempts+=1
        idx=len(segs)
        try:
            raw,offset,h=fetch_scene_segment(cand,seg_len,tmp,idx,v)
            norm=tmp/f"seg{idx}.mp4"
            normalize_segment(raw,offset,seg_len,norm)
            raw.unlink(missing_ok=True)
        except Exception as e:
            print(f"Skipping scene {cand['id']}: {e}")
            for f in tmp.glob(f"raw{idx}.*"): f.unlink(missing_ok=True)
            continue
        segs.append(norm); chosen.append(cand)
        print(f"Scene {len(segs)}/{n}: {cand['title'][:60]} | {h}p")
    if not segs: raise RuntimeError("Could not download any usable visual segment.")
    return segs, chosen

def font_path():
    for p in ["/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf","/usr/share/fonts/opentype/noto/NotoNaskhArabic-Regular.ttf"]:
        if os.path.exists(p): return p
    return "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

def make_overlay(path,reciter,verse_label):
    img=Image.new("RGBA",(1080,1920),(0,0,0,0)); d=ImageDraw.Draw(img)
    # Soft dark gradient behind the text so it stays readable on bright 4K scenes.
    top=1250
    for y in range(top,1920):
        a=int(150*(((y-top)/(1920-top))**1.4))
        d.line([(0,y),(1080,y)],fill=(0,0,0,a))
    f=ImageFont.truetype(font_path(),CFG["visual_style"]["font_size"])
    text="القرآن الكريم"; b=d.textbbox((0,0),text,font=f); x=(1080-(b[2]-b[0]))//2; y=CFG["visual_style"]["subtitle_y"]
    d.text((x+3,y+5),text,font=f,fill=(0,0,0,190),stroke_width=3,stroke_fill=(0,0,0,160))
    d.text((x,y),text,font=f,fill="white",stroke_width=1,stroke_fill="white")
    sf=ImageFont.truetype(font_path(),30)
    d.text((540,CFG["visual_style"]["reciter_y"]),f"{reciter}  •  {verse_label}",font=sf,anchor="ma",fill=(245,235,205,235))
    img.save(path)

def compose_video(audio,segs,overlay,out,dur,crossfade,subtitle_path=None):
    n=len(segs)
    cmd=["ffmpeg","-y"]
    for s in segs:
        if n==1: cmd+=["-stream_loop","-1"]
        cmd+=["-i",str(s)]
    cmd+=["-i",str(audio),"-loop","1","-i",str(overlay)]
    ai,oi=n,n+1
    lens=[duration(s) for s in segs]
    filters=[]; prev="[0:v]"; acc=lens[0]
    for k in range(1,n):
        lab=f"[x{k}]"
        filters.append(f"{prev}[{k}:v]xfade=transition=fade:duration={crossfade}:offset={acc-crossfade:.3f}{lab}")
        prev=lab; acc=acc+lens[k]-crossfade
    # Hold the last frame if the segments come up a little short of the audio.
    filters.append(f"{prev}tpad=stop_mode=clone:stop_duration=5[bgv]")
    filters.append(f"[{oi}:v]format=rgba[ov]")
    filters.append("[bgv][ov]overlay=0:0:format=auto[basev]")
    if subtitle_path:
        filters.append(f"[basev]subtitles={subtitle_path}:fontsdir=/usr/share/fonts/truetype/noto,format=yuv420p[v]")
    else:
        filters.append("[basev]format=yuv420p[v]")
    cmd+=["-filter_complex",";".join(filters),"-map","[v]","-map",f"{ai}:a:0","-t",f"{dur:.3f}",
          "-r","30","-c:v","libx264","-preset","medium","-crf","18","-maxrate","9M","-bufsize","18M",
          "-pix_fmt","yuv420p","-c:a","aac","-b:a","192k","-movflags","+faststart",str(out)]
    sh(cmd)

def make_video(audio,led,out,reciter,verse_label):
    v=visual_cfg(); dur=duration(audio)
    tmp=Path(tempfile.mkdtemp(prefix="quran_scenes_"))
    try:
        identified=identify_and_verify_verses(audio,dur,reciter)
        visual_queries=semantic_visual_queries(identified)
        verses=identified["verses"]
        verse_label=f'{identified["surah_name"]} | الآيات {verses[0]["ayah"]}-{verses[-1]["ayah"]}'
        subtitles=tmp/"ayahs.ass"; write_ayah_subtitles(identified,subtitles)
        segs,chosen=build_scene_segments(dur,led,tmp,visual_queries)
        overlay=tmp/"overlay.png"; make_overlay(overlay,reciter,verse_label)
        compose_video(audio,segs,overlay,out,dur,float(v["crossfade_seconds"]),str(subtitles))
        scenes=led.setdefault("scenes",{}); now=int(time.time())
        for c in chosen: scenes[c["id"]]={"title":c["title"],"used_at":now}
        return [c["id"] for c in chosen]
    finally:
        shutil.rmtree(tmp,ignore_errors=True)

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
    r=requests.post(f"https://graph.facebook.com/v24.0/{ig}/media",data={"media_type":"REELS","video_url":url,"caption":caption,"access_token":token},timeout=60)
    r.raise_for_status(); cid=r.json()["id"]
    for _ in range(30):
        s=requests.get(f"https://graph.facebook.com/v24.0/{cid}",params={"fields":"status_code","access_token":token},timeout=30)
        s.raise_for_status()
        if s.json().get("status_code")=="FINISHED": break
        time.sleep(10)
    p=requests.post(f"https://graph.facebook.com/v24.0/{ig}/media_publish",data={"creation_id":cid,"access_token":token},timeout=60)
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
    used_ids={e.get("source_video_id") for e in clips.values() if e.get("source_video_id")}
    selected,clip_key,source_video,audio=pick_and_download(used_ids)
    out=WORK/(clip_key+".mp4")
    scene_ids=make_video(audio,led,out,selected["reciter"],"تلاوة كاملة")
    save_ledger(led)
    public_url=cloudinary_upload(out) if instagram_on else None
    title=f"تلاوة قرآنية خاشعة | {selected['reciter']} #Shorts"
    caption=(f"تلاوة قرآنية بصوت {selected['reciter']}\nاللهم اجعل القرآن ربيع قلوبنا.\n"
             "#القرآن #قرآن #تلاوة #ياسر_الدوسري #عبدالرحمن_مسعد #quran #shorts")
    entry={"source_video_id":selected["id"],"source_title":selected["title"],"source_views":selected["views"],
           "reciter":selected["reciter"],"scene_ids":scene_ids,"title":title,"caption":caption,"video_url":public_url,
           "youtube_id":None,"instagram_id":None,"created_at":int(time.time())}
    clips[clip_key]=entry; save_ledger(led)
    publish_pending_clip(entry,led,out)

if __name__=="__main__": main()
