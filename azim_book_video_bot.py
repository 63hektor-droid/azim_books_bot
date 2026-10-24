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

START_TIME = time.time()
JOB_BUDGET = 340 * 60     # seconds; GitHub Actions default job limit is 360 minutes

W, H = 1280, 720
FPS = 25
PAD = 0.25                # pause after each scene (seconds)
TOTAL_BOOKS = 10000
MAX_SECONDS = 470         # up to 7:50 (hard limit 8 minutes)
MAX_SPEED = 1.2           # maximum voice speed-up (higher sounds unnatural)

if USE_CF:                # fast images -> long, detailed video
    MIN_SCENES, MAX_SCENES = 24, 28
    WORDS = "620 to 740"   # Pocket TTS speaks slowly: this fits in 8 minutes with no speed-up
else:                     # slow free images (1 per 45 s) -> shorter video so the job does not time out
    MIN_SCENES, MAX_SCENES = 12, 14
    WORDS = "380 to 460"

# ----- voices: Pocket TTS Farsi v2 via the "sokhanvar" library (free, offline, CPU).
# Each voice is cloned from a reference recording of at most 5 seconds (WAV), placed next to this script.
REF_AUDIO = {
    "female": Path(os.environ.get("AZIM_REF_FEMALE", Path(__file__).parent / "azim_voice_female.wav")),
    "male": Path(os.environ.get("AZIM_REF_MALE", Path(__file__).parent / "azim_voice_male.wav")),
}
FEMALE_TYPES = {"novel", "psychology"}   # novels + psychology = female voice, rest = male voice


def sh(cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True)


def norm(title):
    t = " ".join(title.lower().replace(".", " ").replace(",", " ").replace(":", " ").split())
    for prefix in ("the ", "a ", "an "):
        if t.startswith(prefix):
            t = t[len(prefix):]
    return t


def norm_fa(title):
    return norm(title.replace("\u200c", " ").replace("ي", "ی").replace("ك", "ک"))


def has_wrapup(scenes):
    """True if the last scenes contain the word 'جمع‌بندی' (the closing wrap-up is mandatory)."""
    tail = "".join(s["narration_fa"] for s in scenes[-3:])
    tail = tail.replace("\u200c", "").replace(" ", "")
    return "جمعبندی" in tail


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
    used_fa = {norm_fa(p["title_fa"]) for p in posted if p.get("title_fa")}
    all_titles = [p["title_en"] for p in posted]      # the full list, not only the last 200
    number = len(posted) + 1
    for _ in range(4):
        prompt = f"""You are the editor of a Persian channel that summarizes the most famous books in the world.
This is video number {number} of {TOTAL_BOOKS}. Choose ONE very famous, world-renowned book that is NOT in
this already-used list: {json.dumps(all_titles, ensure_ascii=False)}.
Pick the most famous remaining ones first (world classics, global bestsellers, books everybody knows).

Return JSON with exactly these keys:
- "title_en": English title
- "title_fa": Persian title (the form commonly used in Iran)
- "author_en": author name in English
- "author_fa": author name in Persian
- "book_type": one of "novel", "psychology", "other"
    (novel = fiction/novels, psychology = psychology books, other = everything else)
- "outline_en": a rich, detailed and FACTUALLY ACCURATE outline of the book in English (1100-1500 words).
    For a novel: setting and era, main characters (with a short physical description of each),
    the full plot in order with ALL the key events, turning points and the ending, the main themes, the book's
    message, why it matters.
    For a non-fiction book: the author and context, the core thesis, the 8-12 main ideas in order, each with
    the argument and a concrete example/story/experiment from the book (names, places, outcomes), practical
    takeaways, the book's overall conclusion, legacy and criticism.
    Only include facts you are sure about. Never invent events, quotes or studies."""
        data = gemini_json(prompt, minutes=12, models=WRITE_MODELS)
        try:
            ok = (
                norm(data["title_en"]) not in used
                and norm_fa(data["title_fa"]) not in used_fa
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
    for _ in range(4):
        prompt = f"""You are a gifted Iranian writer and radio host. Write the script of a video that summarizes the
book "{book['title_en']}" by {book['author_en']} for a Persian-speaking audience. Use ONLY the facts in this outline:

{book['outline_en']}

Return JSON with exactly these keys:
- "art_style_en": ONE consistent visual style for all images of this video, either painterly
  (for example "digital oil painting, rich colors, clean sharp brushwork") or cartoon
  (for example "stylish 2D cartoon illustration, bold clean outlines, flat vibrant colors"), chosen to fit this book.
  The style must be crisp and clean: never use words like glow, haze, bloom, dreamy, soft focus or mist.
- "characters": a list of the recurring characters, each {{"name": "...", "look_en": "detailed fixed visual
  description in English: age, face, hair, clothes, colors"}}. Empty list if the book has no characters.
- "scenes": a list of {MIN_SCENES} to {MAX_SCENES} objects, each with:
    "narration_fa": 3 short sentences of Persian narration (22-34 words) about ONE single idea or event,
    "characters_present": list of character names (from "characters") visible in the picture, may be empty,
    "setting_en": where the picture takes place (concrete place, time, light),
    "action_en": exactly what is happening in the picture: who is doing what,
    "mood_en": the mood and color atmosphere.

Rules for the narration:
- Total narration: {WORDS} Persian words.
- Structure: (1) a gripping hook, (2) who wrote it and why it matters, briefly, (3) the content in order with real
  depth: specific events, ideas, examples and turning points, with concrete names, places and details from the
  outline, never vague generalities or filler, and no repetition, (4) the key lessons, (5) a mandatory,
  clear closing wrap-up (see next rule).
- MANDATORY wrap-up: the LAST 2 or 3 scenes are a real conclusion. The first of them must start with the
  Persian word «جمع‌بندی» (for example «در جمع‌بندی») and restate the book's central message and its main
  takeaways in order; the very last sentence of the video is a memorable closing sentence.
- Every scene covers exactly ONE idea or event, so one picture can illustrate it fully.
- The voice engine is fragile with long noun chains, so: every sentence at most 12 words; at most TWO
  consecutive ezafe links in a noun phrase (never "noun of noun of noun of noun"); prefer verbs and simple
  clauses over long genitive chains; no comma inside a noun phrase; use 3 or 4 short sentences per scene.
- Natural, fluent, elegant SPOKEN Iranian Persian, like a skilled podcast host. Not a literal translation.
  Correct grammar and spelling, proper half-spaces. Mix short and medium sentences; no childish tone.
- It will be read aloud: write numbers as words, no Latin letters, no emojis, no brackets, no abbreviations.
- Write EVERYTHING in Persian script, with no English or Latin word at all, not even for names and titles. Use the
  Iranian forms: the book is «{book['title_fa']}» and the author is «{book['author_fa']}». Write other foreign names
  the common Iranian way (for example «الیزابت»، «دارسی»).
- Never use English loanwords or words that sound English (for example «پادکست»، «ویدئو»، «آنلاین»، «لایک»،
  «اپلیکیشن»، «استایل»): use the Persian word instead («برنامه»، «تصویر»...). Never say the video is a podcast.
- Use everyday words that any listener understands at once. Avoid rare, archaic, heavy literary or Arabic words,
  long compound words, and slang borrowed from English. If a simple word exists, use it.
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
                and has_wrapup(scenes)
            )
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            data["scenes"] = scenes[:MAX_SCENES + 2]
            data.setdefault("characters", [])
            if not has_wrapup(data["scenes"]):
                continue
            data = polish(data)
            data = sanitize_narration(data, book)
            return align_visuals(data, book)
        time.sleep(5)
    raise RuntimeError("Could not get a valid script (with closing wrap-up) from Gemini")


def polish(data):
    """Step C: a strict Persian editor improves the narration (skipped if it fails)."""
    texts = [s["narration_fa"] for s in data["scenes"]]
    prompt = f"""You are a senior Persian (Iranian) editor and radio narrator.
Below is a JSON list of Persian narration paragraphs for one video; a voice will read them aloud, in order.
Rewrite them so they are flawless, natural, elegant, engaging SPOKEN Persian, not a literal translation:
- fix grammar, word choice, awkward phrasing and spelling; use proper half-spaces;
- keep exactly the same number of items in the same order and keep each item's meaning and its information;
- the closing wrap-up items at the end (they contain the word «جمع‌بندی») must stay a clear, complete conclusion
  and must keep the word «جمع‌بندی»;
- each item 2-4 sentences, at most 36 words; every sentence at most 12 words; at most TWO consecutive ezafe
  links in any noun phrase (split long noun chains into separate short sentences); no comma inside a noun phrase;
  smooth transitions between items; confident, warm, adult tone;
- numbers as words, no Latin letters, no emojis, no brackets, no abbreviations;
- no English or Latin word anywhere, and no English loanword (such as «پادکست»، «ویدئو»، «آنلاین»): replace each with a
  plain Persian word;
- replace any rare, archaic, heavy or hard-to-pronounce word with a simple everyday word every listener understands.
Return JSON: {{"narration": [ ...the rewritten items... ]}}
JSON: {json.dumps(texts, ensure_ascii=False)}"""
    try:
        new = gemini_json(prompt, minutes=6, models=WRITE_MODELS)["narration"]
        if len(new) == len(texts) and all(isinstance(x, str) and x.strip() for x in new):
            check = [{"narration_fa": x} for x in new]
            if has_wrapup(check):
                for s, t in zip(data["scenes"], new):
                    s["narration_fa"] = t.strip()
                print("Narration polished.", flush=True)
            else:
                print("Polish dropped the wrap-up word: kept the original narration.", flush=True)
    except Exception as e:  # noqa: BLE001
        print("Polish skipped:", e, flush=True)
    return data


LATIN = re.compile(r"[A-Za-z]")
DIGITS = re.compile(r"[0-9\u06f0-\u06f9]")


def sanitize_narration(data, book):
    """Make sure the narration has no English/Latin words and no digits (the voice reads them badly)."""
    scenes = data["scenes"]
    for sc in scenes:
        sc["narration_fa"] = sc["narration_fa"].replace("ي", "ی").replace("ك", "ک")

    def bad(i):
        return bool(LATIN.search(scenes[i]["narration_fa"]) or DIGITS.search(scenes[i]["narration_fa"]))

    for attempt in range(2):
        idx = [i for i in range(len(scenes)) if bad(i)]
        if not idx:
            return data
        print(f"Narration check: {len(idx)} scene(s) contain English letters or digits, fixing (try {attempt + 1}).",
              flush=True)
        items = [{"i": i, "narration_fa": scenes[i]["narration_fa"]} for i in idx]
        prompt = f"""Fix these Persian narration paragraphs of a video about the book "{book['title_en']}".
A voice will read them aloud, so: replace EVERY English/Latin word with its Persian translation or the common Persian
spelling (the book title is «{book['title_fa']}», the author is «{book['author_fa']}»), and write every digit as Persian
words. Keep the same meaning, order and length. Keep the word «جمع‌بندی» if it is in a paragraph.
Use everyday words that every listener understands.
Return JSON: {{"fixed": [{{"i": 0, "narration_fa": "..."}}, ...]}}
Paragraphs: {json.dumps(items, ensure_ascii=False)}"""
        try:
            fixed = gemini_json(prompt, minutes=5, models=WRITE_MODELS)["fixed"]
            for f in fixed:
                i = int(f["i"])
                txt = f.get("narration_fa", "").strip()
                if i in idx and txt:
                    scenes[i]["narration_fa"] = txt.replace("ي", "ی").replace("ك", "ک")
        except Exception as e:  # noqa: BLE001
            print("Narration fix skipped:", e, flush=True)
    # last resort: drop what is still Latin, so the voice never reads English
    for i in range(len(scenes)):
        if LATIN.search(scenes[i]["narration_fa"]):
            t = LATIN.sub("", scenes[i]["narration_fa"])
            scenes[i]["narration_fa"] = " ".join(t.split())
            print(f"Scene {i}: removed leftover English letters.", flush=True)
    return data


def align_visuals(data, book):
    """Step D: write each picture from the FINAL narration so the image matches the text (skipped if it fails)."""
    scenes = data["scenes"]
    chars = data.get("characters", [])
    items = [{"i": i, "narration_fa": s["narration_fa"]} for i, s in enumerate(scenes)]
    prompt = f"""You are a film storyboard artist. Below are the FINAL Persian narration paragraphs, in order, of a video
that summarizes the book "{book['title_en']}" by {book['author_en']}. While each paragraph is read aloud, ONE picture
is shown. For EACH paragraph write the picture so that it illustrates the exact content of that paragraph.

Return JSON: {{"visuals": [{{"i": 0, "characters_present": ["..."], "visual_en": "..."}}, ...]}}
with exactly {len(scenes)} items, in the same order.

Rules for "visual_en":
- English, 25 to 55 words. Start with the main subject doing the main action, then the place and time, then
  the important objects.
- Show the single most important thing that the paragraph says: concrete people, objects, place and action.
  For non-fiction, show the concrete story, example or experiment from the paragraph, or a clear literal visual
  metaphor of the idea. For the closing wrap-up paragraphs, show a symbolic scene of the book's central message.
- The image model does not know names: describe people by their fixed look (use the characters' looks below)
  and never write book titles or names in the picture description.
- No text, letters, numbers, charts or logos. No real celebrities. No scenes copied from films.
- Do not write an art style (it is added separately).
"characters_present": names (from the characters list) visible in the picture, may be empty.

Characters: {json.dumps(chars, ensure_ascii=False)}
Paragraphs: {json.dumps(items, ensure_ascii=False)}"""
    try:
        vis = gemini_json(prompt, minutes=8, models=WRITE_MODELS)["visuals"]
        if len(vis) == len(scenes) and all(isinstance(v.get("visual_en"), str) and v["visual_en"].strip()
                                           for v in vis):
            for s, v in zip(scenes, vis):
                s["visual_en"] = v["visual_en"].strip()
                s["characters_present"] = v.get("characters_present") or []
            print("Visuals aligned to the final narration.", flush=True)
    except Exception as e:  # noqa: BLE001
        print("Visual alignment skipped:", e, flush=True)
    return data


# ---------------------------------------------------------------- voice
def probe_duration(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path)])
    return float(out.strip())


_speakers = {}
SEEDS = (42, 7, 123)      # a failed generation is retried with another seed


def get_speaker(kind, seed=42):
    """Load the Pocket TTS Farsi v2 model once per voice and seed (the weights download on first use)."""
    key = (kind, seed)
    if key not in _speakers:
        from sokhanvar import Sokhanvar
        ref = REF_AUDIO[kind].resolve()
        if not ref.exists():
            # no WAV in the repo: build it from azim_voices_data.py (base64 FLAC)
            try:
                import azim_voices_data
                raw = base64.b64decode(getattr(azim_voices_data, f"{kind.upper()}_FLAC_B64"))
                flac = WORK / f"azim_ref_{kind}.flac"
                flac.write_bytes(raw)
                ref = (WORK / f"azim_ref_{kind}.wav").resolve()
                sh(["ffmpeg", "-y", "-loglevel", "error", "-i", str(flac), "-ac", "1", "-ar", "24000", str(ref)])
            except ImportError:
                raise RuntimeError(f"Reference voice file not found: {ref}")
        cfg = WORK / f"azim_sokhanvar_{kind}_{seed}.yaml"
        cfg.write_text(
            "backend: pocket_tts_farsi\n"
            "model: mehdi-hf/pocket-tts-farsi-v2\n"
            "language: fa\n"
            f"reference_audio: {ref}\n"
            "reference_seconds: 5.0\n"
            "eos_threshold: -4.0\n"
            f"seed: {seed}\n"
            "retries: 1\n"
            f"output_dir: {(WORK / 'tts_out').resolve()}\n", encoding="utf-8")
        _speakers[key] = Sokhanvar.from_yaml(str(cfg))
    return _speakers[key]


def text_variants(text):
    """The same text written in 3 safe ways. The model needs a final full stop to know where to end."""
    base = " ".join(text.split())
    core = base.rstrip(" .،,;؛:!?؟…")
    v1 = core + "."                                                  # always ends with a full stop
    v2 = core.replace("\u200c", "") + "."                            # without half-spaces
    v3 = re.sub(r"[،,;؛:!?؟…«»\"'()\-]", " ", v2.rstrip("."))         # without punctuation inside
    v3 = " ".join(v3.split()) + "."
    out = []
    for v in (v1, v2, v3):
        if v not in out:
            out.append(v)
    return out


REPORT_FILE = ROOT / "azim_voice_report.txt"


def note(msg):
    """Print and keep a line in the voice report (uploaded as an artifact by the workflow)."""
    print(msg, flush=True)
    try:
        with open(REPORT_FILE, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def suspicious(text, dur):
    """A generation whose length does not fit its words is probably babble or cut speech."""
    words = len(text.split())
    if words < 6:
        return False
    per_word = dur / words
    return per_word < 0.22 or per_word > 0.9


def tts_try(text, kind, out_wav):
    """Read one text with Pocket TTS. Tries 3 written forms; length-cap or odd-length results are retried with 3 seeds."""
    last = "unknown error"
    for variant in text_variants(text):
        for seed in SEEDS:
            try:
                get_speaker(kind, seed).synthesize(variant, str(out_wav))
                if out_wav.exists() and out_wav.stat().st_size > 2000:
                    dur = probe_duration(out_wav)
                    if not suspicious(variant, dur):
                        return
                    last = f"length cap: suspicious duration {dur:.1f}s for {len(variant.split())} words"
                else:
                    last = "empty audio file"
            except Exception as e:  # noqa: BLE001
                last = str(e)
            note(f"  TTS rejected (seed {seed}): {variant[:60]} -> {last[:90]}")
            if "ezafe" in last:
                raise RuntimeError(f"Pocket TTS failed: {last}")   # deterministic: other forms/seeds do not help
            if "length cap" not in last and "silence" not in last:
                break          # another seed does not help for this kind of error
    raise RuntimeError(f"Pocket TTS failed: {last}")


def rewrite_sentence(text, strong=False):
    """Ask Gemini to say the same thing in simpler, shorter sentences (used when the voice cannot read a sentence)."""
    prompt = f"""Rewrite this Persian sentence for a voice engine that cannot handle long noun chains.
Keep exactly the same meaning and information. Use {"2 to 3 very short sentences (each at most 6 words)" if strong else "1 or 2 short, simple sentences (each at most 10 words)"},
spoken Iranian Persian, correct grammar and half-spaces. Avoid more than two consecutive ezafe links
(noun of noun of noun), avoid commas inside noun phrases, no Latin letters, no digits, no brackets.
Return JSON: {{"text": "..."}}
Sentence: {text}"""
    try:
        out = gemini_json(prompt, minutes=3, models=WRITE_MODELS).get("text", "").strip()
        return out if out and out != text else None
    except Exception as e:  # noqa: BLE001
        print("Rewrite skipped:", e, flush=True)
        return None


def tts_pieces(text, kind, out_wav, level=0, skip=False, rewrites=2):
    """Whole text first; if it fails: sentences (up to 2 simpler rewrites), then phrases, then word groups."""
    if not skip:
        try:
            tts_try(text, kind, out_wav)
            return [out_wav]
        except RuntimeError:
            pass
    if level == 1:
        for k in range(rewrites):
            new = rewrite_sentence(text)
            if not new:
                continue
            note(f"  REWRITE: {text[:70]}\n        -> {new[:70]}")
            try:
                return tts_pieces(new, kind, out_wav.with_name(f"{out_wav.stem}_rw{k}.wav"),
                                  level=0, rewrites=0)
            except RuntimeError:
                continue
    if level == 0:
        parts = [x.strip() for x in re.split(r"(?<=[.!?؟…])\s+", text.strip()) if x.strip()]
    elif level == 1:
        parts = [x.strip() for x in re.split(r"(?<=[،,;؛:])\s+", text.strip()) if x.strip()]
    else:
        words = text.split()
        if len(words) <= 1:
            raise RuntimeError(f"Pocket TTS could not read: {text[:80]}")
        mid = (len(words) + 1) // 2
        parts = [" ".join(words[:mid]), " ".join(words[mid:])]
        note(f"  SPLIT by words: {text[:60]}")
    if len(parts) <= 1:
        if level >= 2:
            raise RuntimeError(f"Pocket TTS could not read: {text[:80]}")
        return tts_pieces(text, kind, out_wav, level + 1, skip=True, rewrites=rewrites)
    files = []
    for j, part in enumerate(parts):
        files += tts_pieces(part, kind, out_wav.with_name(f"{out_wav.stem}_{level}{j}.wav"),
                            level + 1, rewrites=rewrites)
    return files


def tts_text(text, kind, out_wav):
    """Whole scene. The library itself puts natural pauses between sentences; pieces are joined only on fallback."""
    files = tts_pieces(text, kind, out_wav)
    if not files:
        raise RuntimeError(f"Pocket TTS could not read this scene at all: {text[:80]}")
    if len(files) == 1:
        if files[0] != out_wav:
            shutil.copy(files[0], out_wav)
        return
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    labels = []
    n = 0
    for k, f in enumerate(files):
        cmd += ["-i", str(f)]
        labels.append(f"[{n}:a]aresample=24000,aformat=channel_layouts=mono[a{n}]")
        n += 1
        if k < len(files) - 1:
            cmd += ["-f", "lavfi", "-t", "0.2", "-i", "anullsrc=r=24000:cl=mono"]
            labels.append(f"[{n}:a]aresample=24000,aformat=channel_layouts=mono[a{n}]")
            n += 1
    ins = "".join(f"[a{i}]" for i in range(n))
    fc = ";".join(labels) + f";{ins}concat=n={n}:v=0:a=1[out]"
    cmd += ["-filter_complex", fc, "-map", "[out]", str(out_wav)]
    sh(cmd)


TRIM = ("silenceremove=start_periods=1:start_silence=0.05:start_threshold=-50dB,areverse,"
        "silenceremove=start_periods=1:start_silence=0.05:start_threshold=-50dB,areverse")


def make_voice(scenes, kind):
    print(f"Voice: Pocket TTS Farsi v2 ({kind}), scenes: {len(scenes)}", flush=True)
    (WORK / "tts_out").mkdir(exist_ok=True)
    wavs = []
    for i, s in enumerate(scenes):
        p = WORK / f"raw_{i:02d}.wav"
        tts_text(s["narration_fa"].strip(), kind, p)
        wavs.append(p)
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
        factor = total / MAX_SECONDS * 1.02
        if factor > MAX_SPEED:
            raise RuntimeError(
                f"Narration too long ({total:.0f}s): needs speed x{factor:.2f}, above the x{MAX_SPEED} limit")
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
# Positive wording only: FLUX has no negative prompt, so "no ..." words could even add those things.
CLEAN = "clean crisp image, sharp focus, fine sharp details, clear clean edges, high clarity, pure illustration"


def build_prompt(art, chars, scene):
    names = scene.get("characters_present") or []
    looks = "; ".join(f"{c['name']}: {c['look_en']}" for c in chars
                      if isinstance(c, dict) and c.get("name") in names)
    if scene.get("visual_en"):
        return (f"{scene['visual_en']} "
                + (f"Characters: {looks}. " if looks else "")
                + f"Art style: {art}. Wide 16:9 composition, {CLEAN}")
    return (f"{scene['action_en']}. Setting: {scene['setting_en']}. "
            + (f"Characters: {looks}. " if looks else "")
            + f"Mood: {scene.get('mood_en', 'cinematic')}. "
            + f"Art style: {art}. Wide 16:9 composition, {CLEAN}")


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


def make_image(prompt, path, seed, scenes_left):
    """Try Cloudflare first. If it fails, fall back to Pollinations only when the time budget allows."""
    if USE_CF:
        if cf_image(prompt, path):
            time.sleep(1)
            return
        print("WARNING: Cloudflare failed for this image, falling back to Pollinations (slow).", flush=True)
    needed = (scenes_left + 1) * 50
    if time.time() - START_TIME + needed > JOB_BUDGET:
        raise RuntimeError("Not enough time left for slow image generation")
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
        inc = 0.12 / max(frames, 1)    # zoom speed spread over the whole scene (no early stop)
        zoom = (f"z='min(zoom+{inc:.6f},1.12)'" if i % 2 == 0
                else f"z='if(eq(on,1),1.12,max(zoom-{inc:.6f},1.0))'")
        # big lanczos upscale BEFORE zoompan (no shimmer/blur while zooming) + light sharpening
        vf = ("scale=2560:1440:flags=lanczos:force_original_aspect_ratio=increase,crop=2560:1440,"
              "unsharp=5:5:0.6:5:5:0.0,"
              f"zoompan={zoom}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},"
              "format=yuv420p")
        sh(["ffmpeg", "-y", "-loglevel", "error",
            "-i", f"img_{i:02d}.jpg", "-i", f"voice_{i:02d}.mp3",
            "-vf", vf, "-af", "apad", "-t", f"{length:.2f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",    # near-lossless intermediate
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
    # the only lossy pass: best quality that still fits under Telegram's 50 MB limit
    fits = False
    for crf, maxrate in (("23", "720k"), ("27", "520k"), ("31", "380k")):
        sh(["ffmpeg", "-y", "-loglevel", "error", "-i", "joined.mp4",
            "-vf", f"subtitles=subs.srt:force_style='{style}'",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", crf,
            "-maxrate", maxrate, "-bufsize", "1440k", "-c:a", "copy",
            "-movflags", "+faststart", out_path.name], cwd=WORK)
        if out_path.stat().st_size < 48 * 1024 * 1024:
            fits = True
            break
    if not fits:
        raise RuntimeError(
            f"Video is still {out_path.stat().st_size / 1e6:.1f} MB after the last attempt: too big for Telegram")
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
    (WORK / "azim_script.json").write_text(json.dumps(script, ensure_ascii=False, indent=1), encoding="utf-8")
    (ROOT / "azim_last_script.json").write_text(json.dumps(script, ensure_ascii=False, indent=1), encoding="utf-8")
    REPORT_FILE.write_text(f"Book: {book['title_en']}\n", encoding="utf-8")

    kind = "female" if book["book_type"] in FEMALE_TYPES else "male"
    durs = make_voice(scenes, kind)

    seed = int(time.time()) % 100000
    for i, s in enumerate(scenes):
        prompt = build_prompt(script["art_style_en"], script.get("characters", []), s)
        img = WORK / f"img_{i:02d}.jpg"
        try:
            make_image(prompt, img, seed + i, len(scenes) - i - 1)
        except Exception as e:  # noqa: BLE001
            if i == 0:
                raise
            # one failed picture must not lose the whole video: reuse the previous picture
            print(f"WARNING: image {i} failed ({e}); reusing the previous picture.", flush=True)
            shutil.copy(WORK / f"img_{i - 1:02d}.jpg", img)

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
