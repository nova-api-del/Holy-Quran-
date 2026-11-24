import os, json, random, hashlib, subprocess, time
from pathlib import Path
import requests
from PIL import Image, ImageDraw, ImageFont

ROOT=Path(__file__).resolve().parents[1]
CFG=json.loads((ROOT/"config/sources.json").read_text(encoding="utf-8"))
WORK=ROOT/"work"; WORK.mkdir(exist_ok=True)
LEDGER=WORK/"published.json"
PADDING_MS=180

def sh(cmd): subprocess.run(cmd, check=True)
def ledger(): return json.loads(LEDGER.read_text()) if LEDGER.exists() else {}
def save_ledger(x): LEDGER.write_text(json.dumps(x, ensure_ascii=False, indent=2))

def yt_cookie_file():
    raw=os.environ.get("YOUTUBE_COOKIES")
    if not raw: return None
    p=WORK/"youtube_cookies.txt"; p.write_text(raw, encoding="utf-8"); return p

def yt_download(url, out, audio=False):
    cmd=["yt-dlp","--no-playlist","--retries","3","--fragment-retries","3"]
    c=yt_cookie_file()
    if c: cmd += ["--cookies",str(c)]
    if audio:
        cmd += ["-f","bestaudio/best","-x","--audio-format","mp3","-o",str(out)]
    else:
        cmd += ["-f","bv*[height<=1080]+ba/b[height<=1080]","--merge-output-format","mp4","-o",str(out)]
    cmd += [url]
    sh(cmd)

def enabled():
    return [x for x in CFG["reciters"] if x.get("enabled") and x.get("source_url")]

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
    start=max(0,int(block[0]["start_ms"])-PADDING_MS)
    end=int(block[-1]["end_ms"])+PADDING_MS
    return block,start,end

def extract_audio(source_audio, start_ms, end_ms, out):
    if end_ms <= start_ms: raise RuntimeError("Invalid audio clip range.")
    sh(["ffmpeg","-y","-ss",f"{start_ms/1000:.3f}","-i",str(source_audio),
        "-t",f"{(end_ms-start_ms)/1000:.3f}","-vn","-c:a","libmp3lame","-q:a","2",str(out)])

def scene_url():
    ys=[x for x in CFG.get("visual_sources",[]) if x.get("enabled") and x.get("url")]
    if ys: return ("youtube",random.choice(ys)["url"])
    r=requests.get("https://api.pexels.com/videos/search",
      headers={"Authorization":os.environ["PEXELS_API_KEY"]},
      params={"query":"cinematic nature mountains clouds ocean night sky","per_page":30},timeout=60)
    r.raise_for_status(); vids=r.json().get("videos",[]); random.shuffle(vids)
    for v in vids:
        for f in sorted(v.get("video_files",[]),key=lambda x:x.get("width",0)*x.get("height",0)):
            if f.get("width",0)>=720 and f.get("height",0)>=1200: return ("direct",f["link"])
    raise RuntimeError("No suitable vertical visual found.")

def font_path():
    for p in ["/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf","/usr/share/fonts/opentype/noto/NotoNaskhArabic-Regular.ttf"]:
        if os.path.exists(p): return p
    return "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

def make_video(audio,scene,out,reciter,verse_label):
    scene_path=WORK/"scene.mp4"
    if scene[0]=="youtube": yt_download(scene[1],scene_path,audio=False)
    else:
        r=requests.get(scene[1],timeout=120); r.raise_for_status(); scene_path.write_bytes(r.content)
    dur=duration(audio)
    bg=WORK/"bg.mp4"
    sh(["ffmpeg","-y","-stream_loop","-1","-i",str(scene_path),"-t",f"{dur:.3f}",
      "-vf","scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,eq=brightness=-0.10:saturation=0.80",
      "-an","-c:v","libx264","-preset","veryfast","-crf","20",str(bg)])
    img=Image.new("RGBA",(1080,1920),(0,0,0,0)); d=ImageDraw.Draw(img)
    f=ImageFont.truetype(font_path(),CFG["visual_style"]["font_size"])
    text="القرآن الكريم"; b=d.textbbox((0,0),text,font=f); x=(1080-(b[2]-b[0]))//2; y=CFG["visual_style"]["subtitle_y"]
    d.text((x+3,y+5),text,font=f,fill=(0,0,0,190),stroke_width=3,stroke_fill=(0,0,0,160))
    d.text((x,y),text,font=f,fill="white",stroke_width=1,stroke_fill="white")
    sf=ImageFont.truetype(font_path(),30)
    d.text((540,CFG["visual_style"]["reciter_y"]),f"{reciter}  •  {verse_label}",font=sf,anchor="ma",fill=(245,235,205,235))
    overlay=WORK/"overlay.png"; img.save(overlay)
    sh(["ffmpeg","-y","-i",str(bg),"-i",str(audio),"-loop","1","-i",str(overlay),
      "-filter_complex","[2:v]format=rgba[ov];[0:v][ov]overlay=0:0:format=auto[v]",
      "-map","[v]","-map","1:a:0","-t",f"{dur:.3f}","-c:v","libx264","-preset","veryfast","-crf","19",
      "-c:a","aac","-b:a","192k","-shortest","-movflags","+faststart",str(out)])

def cloudinary_upload(path):
    import cloudinary, cloudinary.uploader
    cloudinary.config(cloud_name=os.environ["CLOUDINARY_CLOUD_NAME"],api_key=os.environ["CLOUDINARY_API_KEY"],api_secret=os.environ["CLOUDINARY_API_SECRET"])
    return cloudinary.uploader.upload_large(str(path),resource_type="video",folder="holy-quran")["secure_url"]

def youtube_upload(path,title,description):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    c=Credentials(token=None,refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],token_uri="https://oauth2.googleapis.com/token",client_id=os.environ["YOUTUBE_CLIENT_ID"],client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],scopes=["https://www.googleapis.com/auth/youtube.upload"])
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
    rs=enabled()
    if not rs: raise SystemExit("No recitation source enabled.")
    rec=random.choice(rs)
    source_key=hashlib.sha256(rec["source_url"].encode()).hexdigest()[:12]
    led=ledger(); used=led.get(source_key,[])
    block,start_ms,end_ms=choose_complete_ayah_block(rec,used)
    verse_keys=[v["verse_key"] for v in block]
    clip_key=hashlib.sha256((source_key+"|"+",".join(verse_keys)).encode()).hexdigest()[:12]
    source_audio=WORK/(source_key+"_source.mp3")
    if not source_audio.exists(): yt_download(rec["source_url"],source_audio,audio=True)
    audio=WORK/(clip_key+".mp3"); extract_audio(source_audio,start_ms,end_ms,audio)
    verse_label=verse_keys[0] if len(verse_keys)==1 else f"{verse_keys[0]}–{verse_keys[-1]}"
    out=WORK/(clip_key+".mp4"); make_video(audio,scene_url(),out,rec["name_ar"],verse_label)
    title=f"القرآن الكريم | {rec['name_ar']} | {verse_label} #Shorts"
    caption=f"القرآن الكريم • {rec['name_ar']} • {verse_label}\n#القرآن #قرآن #تلاوة #quran #shorts"
    if CFG["publish"]["youtube"]: youtube_upload(out,title,caption)
    public_url=cloudinary_upload(out)
    if CFG["publish"]["instagram"]: instagram_reel(public_url,caption)
    used.append(",".join(verse_keys)); led[source_key]=used; save_ledger(led)

if __name__=="__main__": main()
