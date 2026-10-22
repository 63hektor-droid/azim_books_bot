import base64
import json
import math
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from datetime import date
from pathlib import Path

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

# Gemini TTS (much more natural than the old voices). Change voice names here if you want others.
GEMINI_TTS_MODELS = ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"]
VOICES = {"female": "Sulafat", "male": "Charon"}
VOICE_STYLE = {
    "female": ("warm, soft and captivating storyteller, calm and natural pacing with gentle emotion, "
               "native Iranian Persian pronunciation"),
    "male": ("deep, calm and engaging narrator, natural pacing with expressive emphasis, "
             "native Iranian Persian pronunciation"),
}
FEMALE_TYPES = {"novel", "psychology"}   # novels + psychology = female voice, rest = male voice

STYLE = ("beautiful detailed digital painting, storybook illustration, rich colors, "
         "cinematic lighting, highly detailed, masterpiece, no text, no letters, no watermark")


def sh(cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True)


def norm(title):
    return " ".join(title.lower().replace(".", " ").replace(",", " ").split())


# ---------------------------------------------------------------- Gemini
def gemini_json(prompt, minutes=15):
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.8},
    }
    headers = {"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"}
    models = [GEMINI_MODEL]
    for extra in ("gemini-flash-latest", "gemini-flash-lite-latest", "gemini-2.5-flash-lite"):
        if extra not in models:
            models.append(extra)
    dead = set()          # model names that do not exist / are not allowed
    deadline = time.time() + minutes * 60   # time budget for this call
    last = None
    round_no = 0
    while time.time() < deadline and len(dead) < len(models):
        round_no += 1
        for model in models:
            if model in dead or time.time() >= deadline:
                continue
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            try:
                print(f"Gemini: round={round_no} model={model}", flush=True)
                r = requests.post(url, headers=headers, json=body, timeout=(30, 120))
                if r.status_code == 200:
                    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    data = json.loads(text)
                    if isinstance(data, list):
                        data = data[0]
                    return data
                last = f"{model}: {r.status_code} {r.text[:200]}"
                print(last, flush=True)
                if r.status_code in (400, 404):
                    dead.add(model)
            except Exception as e:  # noqa: BLE001
                last = f"{model}: {e}"
                print(last, flush=True)
            time.sleep(10)
        time.sleep(min(30 * round_no, 120))
    raise RuntimeError(f"Gemini failed: {last}")


def polish(data):
    """Second pass: a strict Persian editor rewrites the narration for quality (skipped if it fails)."""
    prompt = f"""You are a senior Persian (Iranian) editor and radio narrator.
Below is JSON with Persian narration for a book-summary video that a voice will read aloud.
Rewrite ONLY the "narration_fa" fields so they are flawless, natural, elegant and engaging spoken Persian,
not a literal translation:
- fix grammar, word choice, awkward phrasing and spelling; use proper half-spaces;
- keep the SAME number of scenes in the SAME order and keep each scene's meaning;
- each scene at most 28 words; smooth transitions between scenes; warm storytelling tone;
- numbers as words, no Latin letters, no emojis, no brackets, no abbreviations.
Return the SAME JSON structure, changed only in "narration_fa".
JSON: {json.dumps(data, ensure_ascii=False)}"""
    try:
        new = gemini_json(prompt, minutes=5)
        old_scenes, new_scenes = data["scenes"], new["scenes"]
        if len(old_scenes) == len(new_scenes) and all(x.get("narration_fa") for x in new_scenes):
            for a, b in zip(old_scenes, new_scenes):
                a["narration_fa"] = b["narration_fa"].strip()
            print("Narration polished.", flush=True)
    except Exception as e:  # noqa: BLE001
        print("Polish skipped:", e, flush=True)
    return data


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
- Write like a gifted Iranian storyteller / radio narrator, NOT a literal translation:
  natural, elegant, fluent spoken Persian (Iranian), correct grammar and spelling, proper half-spaces.
- Scenes must flow into each other like one smooth story, with an emotional, engaging tone.
- The text will be read aloud by a voice: write numbers as words, no Latin letters, no emojis,
  no brackets, no abbreviations. Write names the way they are commonly written in Persian.
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
            return polish(data)
        time.sleep(5)
    raise RuntimeError("Could not get a valid new book from Gemini")


# ---------------------------------------------------------------- voice
def probe_duration(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path)])
    return float(out.strip())


def tts_gemini(text, voice, style, out_wav):
    headers = {"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"}
    body = {
        "contents": [{"role": "user", "parts": [{"text": text, "speech_metadata": {"style": style}}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"voice": voice}},
        },
    }
    dead = set()
    deadline = time.time() + 6 * 60
    last = None
    round_no = 0
    while time.time() < deadline and len(dead) < len(GEMINI_TTS_MODELS):
        round_no += 1
        for model in GEMINI_TTS_MODELS:
            if model in dead or time.time() >= deadline:
                continue
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            try:
                r = requests.post(url, headers=headers, json=body, timeout=(30, 180))
                if r.status_code == 200:
                    parts = r.json()["candidates"][0]["content"]["parts"]
                    audio = next(p["inlineData"]["data"] for p in parts if "inlineData" in p)
                    raw = base64.b64decode(audio)
                    if len(raw) > 2000:
                        out_wav.write_bytes(raw)
                        return
                last = f"{model}: {r.status_code} {r.text[:200]}"
                print("  TTS:", last, flush=True)
                if r.status_code in (400, 404):
                    dead.add(model)
            except Exception as e:  # noqa: BLE001
                last = f"{model}: {e}"
                print("  TTS:", last, flush=True)
            time.sleep(8)
        time.sleep(min(20 * round_no, 90))
    raise RuntimeError(f"Gemini TTS failed: {last}")


def make_voice(scenes, kind):
    voice = VOICES[kind]
    style = VOICE_STYLE[kind]
    print(f"Voice: {voice} ({kind})", flush=True)
    wavs = []
    for i, s in enumerate(scenes):
        p = WORK / f"voice_{i:02d}.wav"
        tts_gemini(s["narration_fa"].strip(), voice, style, p)
        wavs.append(p)
        time.sleep(6)   # stay under the free per-minute limit
    raw = [probe_duration(p) for p in wavs]
    total = sum(d + PAD for d in raw)
    factor = 1.0
    if total > MAX_SECONDS:
        factor = min(2.0, total / MAX_SECONDS * 1.03)
    print(f"voice total = {total:.1f}s, speed factor = {factor:.2f}", flush=True)
    durs = []
    for i, p in enumerate(wavs):
        mp3 = WORK / f"voice_{i:02d}.mp3"
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(p)]
        if factor > 1.0:
            cmd += ["-filter:a", f"atempo={factor:.3f}"]
        cmd += ["-c:a", "libmp3lame", "-q:a", "3", str(mp3)]
        sh(cmd)
        durs.append(probe_duration(mp3))
    if sum(d + PAD for d in durs) > MAX_SECONDS + 2:
        raise RuntimeError("Narration is longer than 3 minutes")
    return durs


# ---------------------------------------------------------------- images
def make_image(prompt, path, seed):
    """Free Pollinations allows 1 image every 45 s per IP: on a 402 we wait the time it asks for."""
    full = f"{prompt}, {STYLE}"
    q = urllib.parse.quote(full)
    models = ["flux", "turbo", None]
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
    other_failures = 0
    for attempt in range(1, 16):
        model = models[min(other_failures // 2, len(models) - 1)]
        url = f"https://image.pollinations.ai/prompt/{q}?width={W}&height={H}&nologo=true&seed={seed}"
        if model:
            url += f"&model={model}"
        try:
            print(f"Image: attempt={attempt} model={model}", flush=True)
            r = requests.get(url, headers=headers, timeout=150)
            ctype = r.headers.get("content-type", "")
            if r.status_code == 200 and ctype.startswith("image") and len(r.content) > 5000:
                path.write_bytes(r.content)
                time.sleep(47)   # respect the free limit before the next image
                return
            print(f"  -> status={r.status_code} type={ctype} body={r.text[:160]!r}", flush=True)
            if r.status_code == 402:
                m = re.search(r"Retry after (\d+)s", r.text)
                wait = int(m.group(1)) + 3 if m else 47
                time.sleep(min(wait, 60))
                continue
        except requests.RequestException as e:
            print(f"  -> error: {e}", flush=True)
        other_failures += 1
        time.sleep(10)
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

    kind = "female" if book["book_type"] in FEMALE_TYPES else "male"
    durs = make_voice(scenes, kind)

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
