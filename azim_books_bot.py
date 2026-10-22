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

# Optional free image service (Cloudflare Workers AI, FLUX). If these two are missing, Pollinations is used.
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "").strip()
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "").strip()
USE_CF = bool(CF_ACCOUNT_ID and CF_API_TOKEN)

ROOT = Path(__file__).parent
POSTED_FILE = ROOT / "azim_posted.json"
WORK = ROOT / "azim_work"

W, H = 1280, 720
FPS = 25
PAD = 0.25                # pause after each scene (seconds)
TOTAL_BOOKS = 10000
MAX_SECONDS = 470         # up to 7:50 (hard limit 8 minutes)

if USE_CF:                # fast images -> long, detailed video
    MIN_SCENES, MAX_SCENES = 24, 28
    WORDS = "950 to 1150"
else:                     # slow free images (1 per 45 s) -> shorter video so the job does not time out
    MIN_SCENES, MAX_SCENES = 12, 14
    WORDS = "480 to 600"

# ----- voices (Gemini TTS). Change the names here to try other voices.
GEMINI_TTS_MODELS = ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"]
VOICES = {"female": "Despina", "male": "Charon"}
VOICE_STYLE = {
    "female": ("natural, fluent and confident, like a professional podcast host or documentary narrator "
               "speaking Iranian Persian; warm and engaging; brisk conversational pace, no exaggerated pauses, "
               "no slow syllable-by-syllable reading, not a children's storyteller"),
    "male": ("natural, fluent and authoritative, like a documentary narrator speaking Iranian Persian; "
             "warm and engaging; brisk conversational pace, no exaggerated pauses"),
}
FEMALE_TYPES = {"novel", "psychology"}   # novels + psychology = female voice, rest = male voice


def sh(cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True)


def norm(title):
    return " ".join(title.lower().replace(".", " ").replace(",", " ").split())


# ---------------------------------------------------------------- Gemini text
def gemini_json(prompt, minutes=12, models=None):
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.8},
    }
    headers = {"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"}
    models = models or [GEMINI_MODEL, "gemini-flash-latest", "gemini-flash-lite-latest"]
    dead = set()
    deadline = time.time() + minutes * 60
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
                r = requests.post(url, headers=headers, json=body, timeout=(30, 240))
                if r.status_code == 200:
                    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    data = json.loads(text)
                    if isinstance(data, list):
                        data = data[0]
                    print(f"Gemini OK with {model}", flush=True)
                    return data
                last = f"{model}: {r.status_code} {r.text[:200]}"
                print(last, flush=True)
                if r.status_code in (400, 404) or (r.status_code == 429 and "limit: 0" in r.text):
                    dead.add(model)
            except Exception as e:  # noqa: BLE001
                last = f"{model}: {e}"
                print(last, flush=True)
            time.sleep(8)
        time.sleep(min(30 * round_no, 120))
    raise RuntimeError(f"Gemini failed: {last}")


WRITE_MODELS = ["gemini-pro-latest", GEMINI_MODEL, "gemini-flash-latest", "gemini-flash-lite-latest"]


def pick_book(posted):
    """Step A: choose a new famous book and build a detailed, accurate English outline of it."""
    used = {norm(p["title_en"]) for p in posted}
    recent = [p["title_en"] for p in posted[-200:]]
    number = len(posted) + 1
    for _ in range(4):
        prompt = f"""You are the editor of a Persian channel that summarizes the most famous books in the world.
This is video number {number} of {TOTAL_BOOKS}. Choose ONE very famous, world-renowned book that is NOT in
this already-used list: {json.dumps(recent, ensure_ascii=False)}.
Pick the most famous remaining ones first (world classics, global bestsellers, books everybody knows).

Return JSON with exactly these keys:
- "title_en": English title
- "title_fa": Persian title (the form commonly used in Iran)
- "author_en": author name in English
- "author_fa": author name in Persian
- "book_type": one of "novel", "psychology", "other"
    (novel = fiction/novels, psychology = psychology books, other = everything else)
- "outline_en": a detailed and FACTUALLY ACCURATE outline of the book in English (700-1000 words).
    For a novel: setting and era, main characters (with a short physical description of each),
    the full plot in order with the key events, turning points and the ending, the main themes, why it matters.
    For a non-fiction book: the author and context, the core thesis, the 8-12 main ideas in order, each with
    the argument and a concrete example/story/experiment from the book, practical takeaways, legacy and criticism.
    Only include facts you are sure about. Never invent events, quotes or studies."""
        data = gemini_json(prompt, minutes=12, models=WRITE_MODELS)
        try:
            ok = (
                norm(data["title_en"]) not in used
                and data.get("title_fa") and data.get("author_fa")
                and len(data.get("outline_en", "")) > 800
            )
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            if data.get("book_type") not in ("novel", "psychology", "other"):
                data["book_type"] = "other"
            return data
        time.sleep(5)
    raise RuntimeError("Could not get a valid new book from Gemini")


def write_scenes(book):
    """Step B: write the Persian narration and a visual storyboard from the outline."""
    for _ in range(3):
        prompt = f"""You are a gifted Iranian writer and podcast host. Write the script of a video that summarizes the
book "{book['title_en']}" by {book['author_en']} for a Persian-speaking audience. Use ONLY the facts in this outline:

{book['outline_en']}

Return JSON with exactly these keys:
- "art_style_en": ONE consistent visual style for all images of this video, either painterly
  (for example "cinematic digital oil painting, rich colors, dramatic lighting") or cartoon
  (for example "stylish 2D cartoon illustration, bold shapes, vibrant colors"), chosen to fit this book.
- "characters": a list of the recurring characters, each {{"name": "...", "look_en": "detailed fixed visual
  description in English: age, face, hair, clothes, colors"}}. Empty list if the book has no characters.
- "scenes": a list of {MIN_SCENES} to {MAX_SCENES} objects, each with:
    "narration_fa": 2-3 sentences of Persian narration (28-45 words),
    "characters_present": list of character names (from "characters") visible in the picture, may be empty,
    "setting_en": where the picture takes place (concrete place, time, light),
    "action_en": exactly what is happening in the picture: who is doing what,
    "mood_en": the mood and color atmosphere.

Rules for the narration:
- Total narration: {WORDS} Persian words.
- Structure: (1) a gripping hook, (2) who wrote it and why it matters, briefly, (3) the content in order with real
  depth: specific events, ideas, examples and turning points, not vague generalities, (4) the key lessons,
  (5) a memorable closing sentence.
- Natural, fluent, elegant SPOKEN Iranian Persian, like a skilled podcast host. Not a literal translation.
  Correct grammar and spelling, proper half-spaces. Mix short and medium sentences; no childish tone.
- It will be read aloud: write numbers as words, no Latin letters, no emojis, no brackets, no abbreviations.
- Do not quote the book at length; paraphrase.

Rules for the visuals:
- Each picture must show EXACTLY what the narration says at that moment (concrete scene, people, objects, action).
- For non-fiction ideas, draw a concrete scene or clear visual metaphor that illustrates the idea being narrated.
- No text, letters, numbers, charts or logos in the pictures. No real celebrities. No scenes copied from films."""
        data = gemini_json(prompt, minutes=15, models=WRITE_MODELS)
        try:
            scenes = data["scenes"]
            ok = (
                len(scenes) >= max(8, MIN_SCENES - 4)
                and all(s.get("narration_fa") and s.get("action_en") and s.get("setting_en") for s in scenes)
                and data.get("art_style_en")
            )
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            data["scenes"] = scenes[:MAX_SCENES + 2]
            data.setdefault("characters", [])
            return polish(data)
        time.sleep(5)
    raise RuntimeError("Could not get a valid script from Gemini")


def polish(data):
    """Step C: a strict Persian editor improves the narration (skipped if it fails)."""
    texts = [s["narration_fa"] for s in data["scenes"]]
    prompt = f"""You are a senior Persian (Iranian) editor and radio narrator.
Below is a JSON list of Persian narration paragraphs for one video; a voice will read them aloud, in order.
Rewrite them so they are flawless, natural, elegant, engaging SPOKEN Persian, not a literal translation:
- fix grammar, word choice, awkward phrasing and spelling; use proper half-spaces;
- keep exactly the same number of items in the same order and keep each item's meaning and its information;
- each item 2-3 sentences, at most 45 words; smooth transitions between items; confident, warm, adult tone;
- numbers as words, no Latin letters, no emojis, no brackets, no abbreviations.
Return JSON: {{"narration": [ ...the rewritten items... ]}}
JSON: {json.dumps(texts, ensure_ascii=False)}"""
    try:
        new = gemini_json(prompt, minutes=6, models=WRITE_MODELS)["narration"]
        if len(new) == len(texts) and all(isinstance(x, str) and x.strip() for x in new):
            for s, t in zip(data["scenes"], new):
                s["narration_fa"] = t.strip()
            print("Narration polished.", flush=True)
    except Exception as e:  # noqa: BLE001
        print("Polish skipped:", e, flush=True)
    return data


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


TRIM = ("silenceremove=start_periods=1:start_silence=0.05:start_threshold=-50dB,areverse,"
        "silenceremove=start_periods=1:start_silence=0.05:start_threshold=-50dB,areverse")


def make_voice(scenes, kind):
    voice = VOICES[kind]
    style = VOICE_STYLE[kind]
    print(f"Voice: {voice} ({kind}), scenes: {len(scenes)}", flush=True)
    wavs = []
    for i, s in enumerate(scenes):
        p = WORK / f"voice_{i:02d}.wav"
        tts_gemini(s["narration_fa"].strip(), voice, style, p)
        wavs.append(p)
        time.sleep(5)   # stay under the free per-minute limit
    # trimmed copies (no long silences at start/end), then speed up a little only if too long
    trimmed = []
    for i, p in enumerate(wavs):
        t = WORK / f"trim_{i:02d}.wav"
        sh(["ffmpeg", "-y", "-loglevel", "error", "-i", str(p), "-af", TRIM, str(t)])
        trimmed.append(t if t.exists() and probe_duration(t) > 0.5 else p)
    raw = [probe_duration(p) for p in trimmed]
    total = sum(d + PAD for d in raw)
    factor = 1.0
    if total > MAX_SECONDS:
        factor = min(1.45, total / MAX_SECONDS * 1.02)
    print(f"voice total = {total:.1f}s, speed factor = {factor:.2f}", flush=True)
    durs = []
    for i, p in enumerate(trimmed):
        mp3 = WORK / f"voice_{i:02d}.mp3"
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(p)]
        if factor > 1.0:
            cmd += ["-filter:a", f"atempo={factor:.3f}"]
        cmd += ["-ac", "1", "-c:a", "libmp3lame", "-q:a", "4", str(mp3)]
        sh(cmd)
        durs.append(probe_duration(mp3))
    if sum(d + PAD for d in durs) > MAX_SECONDS + 5:
        raise RuntimeError("Narration is longer than 8 minutes")
    return durs


# ---------------------------------------------------------------- images
def build_prompt(art, chars, scene):
    names = scene.get("characters_present") or []
    looks = "; ".join(f"{c['name']}: {c['look_en']}" for c in chars
                      if isinstance(c, dict) and c.get("name") in names)
    return (f"{scene['action_en']}. Setting: {scene['setting_en']}. "
            + (f"Characters: {looks}. " if looks else "")
            + f"Mood: {scene.get('mood_en', 'cinematic')}. "
            + f"Art style: {art}. Wide cinematic 16:9 composition, highly detailed, sharp focus, "
              f"beautiful lighting, no text, no letters, no watermark")


_cf_size_ok = True


def cf_image(prompt, path):
    """Cloudflare Workers AI FLUX.1 schnell (free: 10,000 neurons/day). Returns True on success."""
    global _cf_size_ok
    url = (f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
           f"/ai/run/@cf/black-forest-labs/flux-1-schnell")
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}", "Content-Type": "application/json"}
    for attempt in range(1, 4):
        payload = {"prompt": prompt[:1900], "steps": 4}
        if _cf_size_ok:
            payload.update({"width": 1024, "height": 576})
        try:
            print(f"Image(CF): attempt={attempt}", flush=True)
            r = requests.post(url, headers=headers, json=payload, timeout=120)
            if r.status_code == 200:
                ctype = r.headers.get("content-type", "")
                if ctype.startswith("image"):
                    path.write_bytes(r.content)
                    return True
                b64 = r.json().get("result", {}).get("image")
                if b64:
                    path.write_bytes(base64.b64decode(b64))
                    return True
            print(f"  -> status={r.status_code} body={r.text[:200]!r}", flush=True)
            if r.status_code in (400, 422) and _cf_size_ok:
                _cf_size_ok = False      # this model does not accept width/height: use default size
                continue
            if r.status_code in (401, 403):
                return False
        except Exception as e:  # noqa: BLE001
            print(f"  -> error: {e}", flush=True)
        time.sleep(8 * attempt)
    return False


def pollinations_image(prompt, path, seed):
    """Free Pollinations allows 1 image every 45 s per IP: on a 402 we wait the time it asks for."""
    q = urllib.parse.quote(prompt[:1500])
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
    for attempt in range(1, 12):
        url = (f"https://image.pollinations.ai/prompt/{q}?width={W}&height={H}"
               f"&nologo=true&seed={seed}&model=flux")
        try:
            print(f"Image(Pollinations): attempt={attempt}", flush=True)
            r = requests.get(url, headers=headers, timeout=150)
            ctype = r.headers.get("content-type", "")
            if r.status_code == 200 and ctype.startswith("image") and len(r.content) > 5000:
                path.write_bytes(r.content)
                time.sleep(47)   # respect the free limit before the next image
                return
            print(f"  -> status={r.status_code} type={ctype} body={r.text[:160]!r}", flush=True)
            if r.status_code == 402:
                m = re.search(r"Retry after (\d+)s", r.text)
                time.sleep(min((int(m.group(1)) + 3) if m else 47, 60))
                continue
        except requests.RequestException as e:
            print(f"  -> error: {e}", flush=True)
        time.sleep(10)
    raise RuntimeError("Image generation failed")


def make_image(prompt, path, seed):
    if USE_CF and cf_image(prompt, path):
        time.sleep(1)
        return
    pollinations_image(prompt, path, seed)


# ---------------------------------------------------------------- video
def srt_time(t):
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = int(t % 60)
    ms = int((t - int(t)) * 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def split_sentences(text):
    parts = [p.strip() for p in re.split(r"(?<=[.!?؟…])\s+", text.strip()) if p.strip()]
    return parts or [text.strip()]


def build_video(scenes, durs, out_path):
    clips = []
    srt = []
    n = 0
    t = 0.0
    for i, (s, d) in enumerate(zip(scenes, durs)):
        length = d + PAD
        frames = math.ceil(length * FPS)
        clip = WORK / f"clip_{i:02d}.mp4"
        zoom = ("z='min(zoom+0.0004,1.12)'" if i % 2 == 0 else "z='if(eq(on,1),1.12,max(zoom-0.0004,1.0))'")
        vf = ("scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
              f"zoompan={zoom}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},"
              "format=yuv420p")
        sh(["ffmpeg", "-y", "-loglevel", "error",
            "-i", f"img_{i:02d}.jpg", "-i", f"voice_{i:02d}.mp3",
            "-vf", vf, "-af", "apad", "-t", f"{length:.2f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
            "-c:a", "aac", "-b:a", "64k", "-ar", "44100", "-ac", "1",
            clip.name], cwd=WORK)
        clips.append(clip.name)

        sents = split_sentences(s["narration_fa"])
        weights = [max(len(x), 1) for x in sents]
        total_w = sum(weights)
        acc = 0
        for x, w in zip(sents, weights):
            start = t + d * acc / total_w
            acc += w
            end = t + d * acc / total_w + (PAD * 0.5 if acc == total_w else 0)
            n += 1
            srt.append(f"{n}\n{srt_time(start)} --> {srt_time(end)}\n{x}\n")
        t += length

    (WORK / "list.txt").write_text("".join(f"file '{c}'\n" for c in clips), encoding="utf-8")
    (WORK / "subs.srt").write_text("\n".join(srt), encoding="utf-8")
    sh(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
        "-i", "list.txt", "-c", "copy", "joined.mp4"], cwd=WORK)

    style = ("FontName=Noto Sans Arabic,FontSize=22,PrimaryColour=&H00FFFFFF,"
             "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=28")
    for crf, maxrate in (("27", "650k"), ("31", "420k")):   # keep the file under Telegram's 50 MB limit
        sh(["ffmpeg", "-y", "-loglevel", "error", "-i", "joined.mp4",
            "-vf", f"subtitles=subs.srt:force_style='{style}'",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", crf,
            "-maxrate", maxrate, "-bufsize", "1300k", "-c:a", "copy",
            "-movflags", "+faststart", out_path.name], cwd=WORK)
        if out_path.stat().st_size < 48 * 1024 * 1024:
            break
    return t


# ---------------------------------------------------------------- telegram
def send_video(path, caption):
    with open(path, "rb") as f:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo",
            data={"chat_id": CHANNEL_ID, "caption": caption,
                  "supports_streaming": "true", "width": W, "height": H},
            files={"video": f}, timeout=1200)
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

    print(f"Image service: {'Cloudflare FLUX' if USE_CF else 'Pollinations (slow)'}", flush=True)
    book = pick_book(posted)
    print("Book:", book["title_en"], "|", book["book_type"], flush=True)
    script = write_scenes(book)
    scenes = script["scenes"]
    words = sum(len(s["narration_fa"].split()) for s in scenes)
    print(f"Script: {len(scenes)} scenes, {words} words, style: {script['art_style_en']}", flush=True)

    kind = "female" if book["book_type"] in FEMALE_TYPES else "male"
    durs = make_voice(scenes, kind)

    seed = int(time.time()) % 100000
    for i, s in enumerate(scenes):
        prompt = build_prompt(script["art_style_en"], script.get("characters", []), s)
        make_image(prompt, WORK / f"img_{i:02d}.jpg", seed + i)

    final = WORK / "azim_final.mp4"
    total = build_video(scenes, durs, final)
    print(f"video length = {total:.1f}s, size = {final.stat().st_size / 1e6:.1f} MB", flush=True)

    caption = f"{book['title_fa']}\n{book['author_fa']}"
    send_video(final, caption)

    posted.append({"title_en": book["title_en"], "title_fa": book["title_fa"],
                   "type": book["book_type"], "date": date.today().isoformat()})
    POSTED_FILE.write_text(json.dumps(posted, ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.rmtree(WORK, ignore_errors=True)
    print("Done.")


if __name__ == "__main__":
    main()
