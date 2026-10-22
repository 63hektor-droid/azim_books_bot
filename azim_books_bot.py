import asyncio
import json
import math
import os
import shutil
import subprocess
import time
import urllib.parse
from datetime import date
from pathlib import Path

import edge_tts
import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

ROOT = Path(__file__).parent
POSTED_FILE = ROOT / "azim_posted.json"
WORK = ROOT / "azim_work"

W, H = 1280, 720
FPS = 25
PAD = 0.4                 # pause after each scene (seconds)
MAX_SECONDS = 178         # hard limit: under 3 minutes
MAX_WORDS = 330
TOTAL_BOOKS = 10000

VOICES = {"female": "fa-IR-DilaraNeural", "male": "fa-IR-FaridNeural"}
FEMALE_TYPES = {"novel", "psychology"}   # novels + psychology = female voice, rest = male voice

STYLE = ("beautiful detailed digital painting, storybook illustration, rich colors, "
         "cinematic lighting, highly detailed, masterpiece, no text, no letters, no watermark")


def sh(cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True)


def norm(title):
    return " ".join(title.lower().replace(".", " ").replace(",", " ").split())


# ---------------------------------------------------------------- Gemini
def gemini_json(prompt):
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.8},
    }
    headers = {"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"}
    models = [GEMINI_MODEL]
    if "gemini-flash-latest" not in models:
        models.append("gemini-flash-latest")
    last = None
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            try:
                print(f"Gemini: model={model} attempt={attempt + 1}", flush=True)
                r = requests.post(url, headers=headers, json=body, timeout=(30, 240))
                if r.status_code == 200:
                    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    data = json.loads(text)
                    if isinstance(data, list):
                        data = data[0]
                    return data
                last = f"{model}: {r.status_code} {r.text[:300]}"
                print(last, flush=True)
                if r.status_code in (400, 404):
                    break  # this model name is not usable, try the next one
            except Exception as e:  # noqa: BLE001
                last = f"{model}: {e}"
                print(last, flush=True)
            time.sleep(15 * (attempt + 1))
    raise RuntimeError(f"Gemini failed: {last}")


def pick_book(posted):
    used = {norm(p["title_en"]) for p in posted}
    recent = [p["title_en"] for p in posted[-200:]]
    number = len(posted) + 1
    for _ in range(4):
        prompt = f"""You produce short Persian (Farsi) video summaries of the most famous books in the world.
This is video number {number} of {TOTAL_BOOKS}. Pick ONE very famous world-renowned book
that is NOT in this already-used list: {json.dumps(recent, ensure_ascii=False)}.
Pick the most famous remaining ones first (world classics, bestsellers, books everybody knows).

Return JSON with exactly these keys:
- "title_en": English title
- "title_fa": Persian title
- "author_fa": author name in Persian
- "book_type": one of "novel", "psychology", "other"
   (novel = fiction/novels, psychology = psychology books, other = everything else)
- "scenes": a list of 9 to 11 objects, each with:
    "narration_fa": 1-2 short sentences (max 28 words) in fluent, high-quality, natural Persian,
                    like a skilled literary translator and narrator;
    "image_prompt_en": a vivid English description of ONE painted scene that matches exactly what is
                    narrated in this scene. Repeat the same short description of the main
                    character(s) in every prompt so they look consistent.

Rules:
- All narration together must be at most {MAX_WORDS} Persian words.
- First scene = a strong hook. Last scene = the key message or lesson of the book.
- Image prompts: painting / cartoon illustration style, no text or letters inside the image,
  no real celebrity faces, no scenes copied from films.
"""
        data = gemini_json(prompt)
        try:
            title = data["title_en"].strip()
            scenes = data["scenes"]
            ok = (
                norm(title) not in used
                and len(scenes) >= 5
                and all(s.get("narration_fa") and s.get("image_prompt_en") for s in scenes)
                and data.get("title_fa") and data.get("author_fa")
            )
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            if data.get("book_type") not in ("novel", "psychology", "other"):
                data["book_type"] = "other"
            return data
        time.sleep(5)
    raise RuntimeError("Could not get a valid new book from Gemini")


# ---------------------------------------------------------------- voice
async def _tts(text, voice, rate, path):
    await edge_tts.Communicate(text, voice, rate=rate).save(str(path))


def probe_duration(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path)])
    return float(out.strip())


def make_voice(scenes, voice):
    rate = "+0%"
    for _ in range(3):
        durs = []
        for i, s in enumerate(scenes):
            p = WORK / f"voice_{i:02d}.mp3"
            last = None
            for attempt in range(4):
                try:
                    asyncio.run(_tts(s["narration_fa"], voice, rate, p))
                    last = None
                    break
                except Exception as e:  # noqa: BLE001
                    last = e
                    time.sleep(5 * (attempt + 1))
            if last:
                raise last
            durs.append(probe_duration(p))
        total = sum(d + PAD for d in durs)
        print(f"voice total = {total:.1f}s (rate {rate})")
        if total <= MAX_SECONDS:
            return durs
        rate = f"+{int((total / MAX_SECONDS - 1) * 100) + 6}%"
    raise RuntimeError("Narration is longer than 3 minutes")


# ---------------------------------------------------------------- images
def make_image(prompt, path, seed):
    full = f"{prompt}, {STYLE}"
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(full)
           + f"?width={W}&height={H}&model=flux&nologo=true&seed={seed}")
    for attempt in range(6):
        try:
            r = requests.get(url, timeout=240)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                path.write_bytes(r.content)
                return
        except requests.RequestException:
            pass
        time.sleep(15 * (attempt + 1))
    raise RuntimeError("Image generation failed")


# ---------------------------------------------------------------- video
def srt_time(t):
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = int(t % 60)
    ms = int((t - int(t)) * 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def build_video(scenes, durs, out_path):
    clips = []
    srt = []
    t = 0.0
    for i, (s, d) in enumerate(zip(scenes, durs)):
        length = d + PAD
        frames = math.ceil(length * FPS)
        clip = WORK / f"clip_{i:02d}.mp4"
        vf = (f"scale=1920:1080,zoompan=z='min(zoom+0.0006,1.15)':"
              f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},format=yuv420p")
        sh(["ffmpeg", "-y", "-loglevel", "error",
            "-i", f"img_{i:02d}.jpg", "-i", f"voice_{i:02d}.mp3",
            "-vf", vf, "-af", "apad", "-t", f"{length:.2f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            clip.name], cwd=WORK)
        clips.append(clip.name)
        srt.append(f"{i + 1}\n{srt_time(t)} --> {srt_time(t + d + PAD * 0.5)}\n{s['narration_fa'].strip()}\n")
        t += length

    (WORK / "list.txt").write_text("".join(f"file '{c}'\n" for c in clips), encoding="utf-8")
    (WORK / "subs.srt").write_text("\n".join(srt), encoding="utf-8")
    sh(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
        "-i", "list.txt", "-c", "copy", "joined.mp4"], cwd=WORK)

    style = ("FontName=Noto Sans Arabic,FontSize=22,PrimaryColour=&H00FFFFFF,"
             "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=28")
    sh(["ffmpeg", "-y", "-loglevel", "error", "-i", "joined.mp4",
        "-vf", f"subtitles=subs.srt:force_style='{style}'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "25", "-c:a", "copy",
        "-movflags", "+faststart", out_path.name], cwd=WORK)
    return t


# ---------------------------------------------------------------- telegram
def send_video(path, caption):
    with open(path, "rb") as f:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo",
            data={"chat_id": CHANNEL_ID, "caption": caption,
                  "supports_streaming": "true", "width": W, "height": H},
            files={"video": f}, timeout=900)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram error: {r.status_code} {r.text[:300]}")


# ---------------------------------------------------------------- main
def main():
    posted = json.loads(POSTED_FILE.read_text("utf-8")) if POSTED_FILE.exists() else []
    if len(posted) >= TOTAL_BOOKS:
        print("All books are done.")
        return

    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    book = pick_book(posted)
    scenes = book["scenes"]
    print("Book:", book["title_en"], "|", book["book_type"], "|", len(scenes), "scenes")

    voice = VOICES["female" if book["book_type"] in FEMALE_TYPES else "male"]
    durs = make_voice(scenes, voice)

    seed = int(time.time()) % 100000
    for i, s in enumerate(scenes):
        make_image(s["image_prompt_en"], WORK / f"img_{i:02d}.jpg", seed + i)

    final = WORK / "azim_final.mp4"
    total = build_video(scenes, durs, final)
    print(f"video length = {total:.1f}s, size = {final.stat().st_size / 1e6:.1f} MB")

    caption = f"{book['title_fa']}\n{book['author_fa']}"
    send_video(final, caption)

    posted.append({"title_en": book["title_en"], "title_fa": book["title_fa"],
                   "type": book["book_type"], "date": date.today().isoformat()})
    POSTED_FILE.write_text(json.dumps(posted, ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.rmtree(WORK, ignore_errors=True)
    print("Done.")


if __name__ == "__main__":
    main()
