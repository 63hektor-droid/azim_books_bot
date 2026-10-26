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

# A second Gemini call looks at every picture and regenerates it (up to 2 times) when it does not match the text.
# Only used with the fast Cloudflare images. Set AZIM_IMAGE_CHECK=0 to turn it off.
IMAGE_CHECK = USE_CF and os.environ.get("AZIM_IMAGE_CHECK", "1") != "0"

# A separate "voice-only" text (diacritics on ambiguous words + phonetic spelling of foreign names) is prepared
# for the voice. The subtitles always keep the clean original text. Set AZIM_VOICE_PREP=0 to turn it off.
VOICE_PREP = os.environ.get("AZIM_VOICE_PREP", "1") != "0"

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
SUB_FONT_SIZE = 23        # subtitle size (was 22)

# ----- voices: Pocket TTS Farsi v2 via the "sokhanvar" library (free, offline, CPU).
# Each voice is cloned from a reference recording of at most 5 seconds (WAV), placed next to this script.
# female = atena, male = kian
REF_AUDIO = {
    "female": Path(os.environ.get("AZIM_REF_FEMALE", Path(__file__).parent / "azim_voice_female.wav")),
    "male": Path(os.environ.get("AZIM_REF_MALE", Path(__file__).parent / "azim_voice_male.wav")),
}
FEMALE_TYPES = {"novel", "psychology"}   # novels + psychology = female voice, rest = male voice

DIAC = re.compile(r"[\u064B-\u0652\u0670]")   # Persian/Arabic short-vowel marks (fatha, kasra, damma, shadda, sukun...)


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
def gemini_json(prompt, minutes=12, models=None, image=None):
    parts = [{"text": prompt}]
    if image is not None:
        parts.insert(0, {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(image).decode()}})
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.7},
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
CHECK_MODELS = ["gemini-flash-latest", GEMINI_MODEL, "gemini-flash-lite-latest"]


# ---------------------------------------------------------------- the book and its reliable source
def pick_book(posted, rejected):
    """Step A: choose a new famous book. Only its name and details: the content comes from Wikipedia."""
    used = {norm(p["title_en"]) for p in posted} | {norm(x) for x in rejected}
    used_fa = {norm_fa(p["title_fa"]) for p in posted if p.get("title_fa")}
    all_titles = [p["title_en"] for p in posted] + list(rejected)
    number = len(posted) + 1
    for _ in range(4):
        prompt = f"""You are the editor of a Persian channel that summarizes the most famous books in the world.
This is video number {number} of {TOTAL_BOOKS}. Choose ONE very famous, world-renowned book that is NOT in
this already-used list: {json.dumps(all_titles, ensure_ascii=False)}.
Pick the most famous remaining ones first (world classics, global bestsellers, books everybody knows).
Prefer a book whose English Wikipedia article has a detailed "Plot", "Synopsis", "Summary" or "Contents" section.

Return JSON with exactly these keys:
- "title_en": English title
- "title_fa": Persian title (the form commonly used in Iran)
- "author_en": author name in English
- "author_fa": author name in Persian
- "wikipedia_title_en": the exact title of the English Wikipedia article about the book
- "wikipedia_title_fa": the exact title of the Persian Wikipedia article about the book, or "" if you are not sure
- "book_type": one of "novel", "psychology", "other"
    (novel = fiction/novels, psychology = psychology books, other = everything else)"""
        data = gemini_json(prompt, minutes=8, models=WRITE_MODELS)
        try:
            ok = (
                norm(data["title_en"]) not in used
                and norm_fa(data["title_fa"]) not in used_fa
                and data.get("title_fa") and data.get("author_fa") and data.get("author_en")
            )
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            if data.get("book_type") not in ("novel", "psychology", "other"):
                data["book_type"] = "other"
            return data
        time.sleep(5)
    raise RuntimeError("Could not get a valid new book from Gemini")


WIKI_HEADERS = {"User-Agent": "azim-book-video-bot/1.0 (educational book summaries; GitHub Actions)"}
EN_KEYS = re.compile(r"\b(plot|synopsis|summary|contents?|overview|premise|story|narrative|themes?|"
                     r"main ideas?|ideas|structure|chapters?|argument|thesis|characters?|description)\b", re.I)
EN_SKIP = re.compile(r"adaptation|film|television|reception|legacy|influence|critic|award|sequel|cultural|"
                     r"publication|translation|see also|references", re.I)
FA_KEYS = ("خلاصه", "داستان", "محتوا", "مضمون", "درون", "موضوع", "طرح", "نگاه کلی", "ساختار", "فصل", "شخصیت")
FA_SKIP = ("اقتباس", "فیلم", "نقد", "پذیرش", "جوایز", "ترجمه", "پانویس", "منابع", "جستارهای وابسته")
HEAD = re.compile(r"^(={2,6})\s*(.+?)\s*\1\s*$", re.M)


def wiki_api(lang, params):
    p = dict(params)
    p.update({"format": "json", "formatversion": 2})
    r = requests.get(f"https://{lang}.wikipedia.org/w/api.php", params=p, headers=WIKI_HEADERS, timeout=40)
    r.raise_for_status()
    return r.json()


def wiki_fulltext(lang, title):
    """(real title, plain text) of a Wikipedia article (redirects followed), or None."""
    try:
        d = wiki_api(lang, {"action": "query", "prop": "extracts", "explaintext": 1, "redirects": 1,
                            "titles": title})
        p = d["query"]["pages"][0]
        if p.get("missing") or not p.get("extract"):
            return None
        return p["title"], p["extract"]
    except Exception as e:  # noqa: BLE001
        print(f"Wikipedia ({lang}) error for '{title}': {e}", flush=True)
        return None


def wiki_search(lang, query):
    try:
        d = wiki_api(lang, {"action": "query", "list": "search", "srsearch": query, "srlimit": 3})
        return [x["title"] for x in d["query"]["search"]]
    except Exception as e:  # noqa: BLE001
        print(f"Wikipedia search error: {e}", flush=True)
        return []


def split_wiki(text):
    """Return (intro, [(level-2 title, text including its sub-sections), ...])."""
    ms = list(HEAD.finditer(text))
    if not ms:
        return text.strip(), []
    lead = text[:ms[0].start()].strip()
    secs, cur_title, cur_parts = [], None, []
    for i, m in enumerate(ms):
        level, title = len(m.group(1)), m.group(2).strip()
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        body = text[m.end():end].strip()
        if level == 2:
            if cur_title is not None:
                secs.append((cur_title, "\n".join(cur_parts).strip()))
            cur_title, cur_parts = title, [body]
        elif cur_title is not None:
            cur_parts.append(f"{title}: {body}")
    if cur_title is not None:
        secs.append((cur_title, "\n".join(cur_parts).strip()))
    return lead, secs


def pick_sections(text, lang):
    lead, secs = split_wiki(text)
    chosen = []
    for t, b in secs:
        if len(b) < 80:
            continue
        if lang == "en":
            hit = EN_KEYS.search(t) and not EN_SKIP.search(t)
        else:
            hit = any(k in t for k in FA_KEYS) and not any(k in t for k in FA_SKIP)
        if hit:
            chosen.append((t, b))
    body = "\n\n".join(f"[{t}]\n{b}" for t, b in chosen)
    return lead, body


def fetch_wiki(book, lang):
    key = "title_en" if lang == "en" else "title_fa"
    author = book["author_en" if lang == "en" else "author_fa"].replace("\u200c", "").lower()
    last = author.split()[-1] if author.split() else ""
    cands = [book.get("wikipedia_title_" + lang), book[key]]
    found = None
    for c in cands:
        if c:
            found = wiki_fulltext(lang, c)
            if found:
                break
    if not found and lang == "en":
        for t in wiki_search("en", f'{book["title_en"]} {book["author_en"]}'):
            found = wiki_fulltext("en", t)
            if found:
                break
    if not found:
        return None
    title, text = found
    if last and last not in text.replace("\u200c", "").lower():
        print(f"Wikipedia ({lang}) page '{title}' does not mention the author: ignored.", flush=True)
        return None
    lead, body = pick_sections(text, lang)
    return {"title": title, "lead": lead, "body": body}


def fetch_source(book):
    """The ONLY source of facts for the video: the book's Wikipedia article (English + Persian when available)."""
    en = fetch_wiki(book, "en")
    if not en or len(en["body"]) < 1200:
        print("No usable English Wikipedia plot/summary for this book.", flush=True)
        return None
    fa = fetch_wiki(book, "fa")
    parts = ["ENGLISH WIKIPEDIA - INTRODUCTION:\n" + en["lead"][:1600],
             "ENGLISH WIKIPEDIA - CONTENT SECTIONS:\n" + en["body"][:7000]]
    if fa and len(fa["body"]) > 400:
        parts.append("PERSIAN WIKIPEDIA (use its Persian names and wording when it agrees with the English):\n"
                     + fa["lead"][:700] + "\n" + fa["body"][:2500])
    url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(en["title"].replace(" ", "_"))
    return {"text": "\n\n".join(parts), "en_words": len(en["body"].split()), "url": url, "title": en["title"]}


def plan_length(src):
    """Video length follows how much real content the source has (no padding)."""
    target = max(300, min(700, int(src["en_words"] * 0.85)))
    if not USE_CF:                    # slow free images: fewer scenes, so fewer words
        target = min(target, 400)
    n = round(target / 27)
    if USE_CF:
        lo, hi = max(10, n - 2), min(28, n + 2)
    else:
        lo, hi = max(8, n - 2), min(14, n + 2)
    return lo, hi, f"{int(target * 0.9)} to {int(target * 1.1)}"


# ---------------------------------------------------------------- script
TONE_RULES = """- TONE: a warm, calm, friendly storyteller who talks to ONE listener, like a close friend telling a story in the
  evening. Not a news anchor, not a textbook, not a lecturer, not childish, not dramatic or theatrical.
  Natural spoken Iranian Persian in clean standard form (no slang contractions such as «میگم» or «اون»).
  Use simple connectors the way people really talk («اما»، «بعد»، «برای همین»، «تا این‌که»، «حالا»).
  Never use stiff written-style verbs or forms such as «نمود»، «گردید»، «می‌باشد»، «بدین سبب»، «لذا»، «مذکور».
  Start sentences with the subject or the action, not with long clauses. Vary the rhythm: short, then a bit longer.
  No exaggerated praise words and no advertising tone."""


def write_scenes(book, src, plan):
    """Step B: write the Persian narration from the source text only (no outside facts)."""
    min_sc, max_sc, words = plan
    for _ in range(4):
        prompt = f"""You are a gifted Iranian writer and radio host. Write the script of a video that summarizes the
book "{book['title_en']}" by {book['author_en']} for a Persian-speaking audience.

Use ONLY the facts in the SOURCE TEXT below (taken from Wikipedia). Never add a fact, name, date, number,
quote or opinion that is not in the source. If the source says nothing about something, say nothing about it.

SOURCE TEXT:
{src['text']}

Return JSON with exactly these keys:
- "art_style_en": ONE consistent visual style for all images of this video, either painterly
  (for example "digital oil painting, rich colors, clean sharp brushwork") or cartoon
  (for example "stylish 2D cartoon illustration, bold clean outlines, flat vibrant colors"), chosen to fit this book.
  The style must be crisp and clean: never use words like glow, haze, bloom, dreamy, soft focus or mist.
- "characters": a list of the recurring characters, each {{"name": "...", "look_en": "detailed fixed visual
  description in English: age, face, hair, clothes, colors"}}. Empty list if the book has no characters.
  Base the look on what the source says; if it says nothing, choose a fitting look for the era and keep it fixed.
- "scenes": a list of {min_sc} to {max_sc} objects, each with:
    "narration_fa": 3 or 4 short sentences of Persian narration (22-34 words) about ONE single idea or event,
    "source_en": the English sentence or two from the SOURCE TEXT that this paragraph is based on (copy it, at most 45 words),
    "characters_present": list of character names (from "characters") visible in the picture, may be empty,
    "setting_en": where the picture takes place (concrete place, time, light),
    "action_en": exactly what is happening in the picture: who is doing what,
    "mood_en": the mood and color atmosphere.

Rules for the narration:
- Total narration: {words} Persian words. Do not stretch it: if the source has less to say, write less.
- NO FILLER. Every sentence must carry a concrete fact, event or idea from the source. No generic praise, no
  rhetorical questions, no empty transitions, no repeating the same point, no moral or lesson that is not in the source.
- Structure: (1) one or two sentences: what the book is, who wrote it and when (only if the source says so),
  (2) the content in the SAME ORDER as the source, covering ALL its key events or ideas, including the ending,
  (3) the book's central message ONLY if the source states it, (4) a mandatory, clear closing wrap-up (next rule).
- MANDATORY wrap-up: the LAST 2 or 3 scenes are a real conclusion. The first of them must start with the
  Persian word «جمع‌بندی» (for example «در جمع‌بندی») and only restates, in order, what was already said (no new
  claim); the very last sentence of the video is a short, memorable closing sentence.
- Every scene covers exactly ONE idea or event, so one picture can illustrate it fully.
- Translate the MEANING faithfully into natural Persian, never word by word. If a Persian Wikipedia text is given,
  use its Persian names, titles and terms when they agree with the English.
- The voice engine is fragile with long noun chains, so: every sentence at most 12 words; at most TWO
  consecutive ezafe links in a noun phrase (never "noun of noun of noun of noun"); prefer verbs and simple
  clauses over long genitive chains; no comma inside a noun phrase.
{TONE_RULES}
  Correct grammar and spelling, proper half-spaces. Mix short and medium sentences.
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
                len(scenes) >= max(8, min_sc - 4)
                and all(s.get("narration_fa") and s.get("action_en") and s.get("setting_en") for s in scenes)
                and data.get("art_style_en")
                and has_wrapup(scenes)
            )
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            data["scenes"] = scenes[:max_sc + 2]
            data.setdefault("characters", [])
            if not has_wrapup(data["scenes"]):
                continue
            data = polish(data)
            data = sanitize_narration(data, book)
            data = verify_facts(data, book, src)
            data = sanitize_narration(data, book)
            data = prepare_spoken(data, book)
            return align_visuals(data, book)
        time.sleep(5)
    raise RuntimeError("Could not get a valid script (with closing wrap-up) from Gemini")


def polish(data):
    """Step C: a strict Persian editor improves the wording only (skipped if it fails)."""
    texts = [s["narration_fa"] for s in data["scenes"]]
    prompt = f"""You are a senior Persian (Iranian) editor and radio narrator.
Below is a JSON list of Persian narration paragraphs for one video; a voice will read them aloud, in order.
Improve ONLY the wording so it is flawless, natural, elegant SPOKEN Persian, not a literal translation.
Do NOT add, remove or change any fact, name, number or idea; do not add new sentences.
- fix grammar, word choice, awkward phrasing and spelling; use proper half-spaces;
- keep exactly the same number of items in the same order and keep each item's meaning and its information;
- the closing wrap-up items at the end (they contain the word «جمع‌بندی») must stay a clear, complete conclusion
  and must keep the word «جمع‌بندی»;
- each item 2-4 sentences, at most 36 words; every sentence at most 12 words; at most TWO consecutive ezafe
  links in any noun phrase (split long noun chains into separate short sentences); no comma inside a noun phrase;
  smooth transitions between items;
{TONE_RULES}
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


def verify_facts(data, book, src):
    """Step C2: a fact checker compares every paragraph with the source and fixes or drops unsupported claims."""
    scenes = data["scenes"]
    items = [{"i": i, "narration_fa": s["narration_fa"]} for i, s in enumerate(scenes)]
    prompt = f"""You are a strict fact checker. SOURCE TEXT (the only truth):
{src['text']}

Below are Persian narration paragraphs of a video about the book "{book['title_en']}". For EACH paragraph decide if
every fact, name, number and claim in it is supported by the SOURCE TEXT.
A paragraph is NOT ok if it contains a fact, name, date, number, quote or interpretation that is not in the source,
a wrong fact, or filler that carries no information.
Return JSON {{"results": [{{"i": 0, "ok": true, "fixed": ""}}, ...]}} with one item per paragraph, in order.
For a paragraph that is not ok, put in "fixed" the same paragraph rewritten in Persian so that it keeps only the
supported facts (same spoken style, 2-4 short sentences, no Latin letters, no digits). If nothing supported
remains, put "" in "fixed". Keep the word «جمع‌بندی» in a paragraph that contains it.
Paragraphs: {json.dumps(items, ensure_ascii=False)}"""
    try:
        res = gemini_json(prompt, minutes=8, models=WRITE_MODELS)["results"]
        by_i = {int(r["i"]): r for r in res}
        before = [s["narration_fa"] for s in scenes]
        keep = []
        fixed_n = dropped_n = 0
        for i, s in enumerate(scenes):
            r = by_i.get(i)
            if not r or r.get("ok", True):
                keep.append(s)
                continue
            fx = (r.get("fixed") or "").strip()
            if fx:
                s["narration_fa"] = fx
                keep.append(s)
                fixed_n += 1
            else:
                dropped_n += 1
        if len(keep) >= 8:
            data["scenes"] = keep
            if not has_wrapup(data["scenes"]):          # never lose the closing wrap-up
                data["scenes"] = scenes
                for s, old in zip(scenes, before):
                    s["narration_fa"] = old
                print("Fact check would remove the wrap-up: kept the previous narration.", flush=True)
            else:
                print(f"Fact check: {fixed_n} paragraph(s) fixed, {dropped_n} dropped.", flush=True)
    except Exception as e:  # noqa: BLE001
        print("Fact check skipped:", e, flush=True)
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


def prepare_spoken(data, book):
    """Step C3: a voice-only version of every paragraph ("spoken_fa").
    Same words and same order, but: short-vowel marks (diacritics) only on ambiguous words, and foreign names
    spelled the way they are pronounced. The subtitles keep the clean "narration_fa". Skipped if it fails."""
    scenes = data["scenes"]
    for s in scenes:
        s["spoken_fa"] = s["narration_fa"]
    if not VOICE_PREP:
        return data
    items = [{"i": i, "narration_fa": s["narration_fa"]} for i, s in enumerate(scenes)]
    prompt = f"""You are a Persian (Iranian) pronunciation editor for a text-to-speech voice. The voice reads text exactly as
written and often guesses wrongly. For EACH paragraph below write "spoken_fa": the SAME paragraph, prepared for the voice.

What to do:
1. Keep every word, every sentence and the same order. Do NOT add, remove, reorder or reword anything.
2. Put short-vowel marks (فتحه، کسره، ضمه، تشدید) ONLY on words whose reading is ambiguous or often misread, such as
   «کِشت / کُشت»، «شِکَن / شُکُن»، «مَرد / مُرد»، «گُل / گِل»، «بَرد / بُرد»، «خوان / خان»، «عِلم / عَلَم»،
   «دَر / دُر»، «پُر / پِر»، «کُرد / کَرد»، «بِه / بَه»، «رَسید / رِسید» and similar.
   Do NOT put marks on clear, common words: too many marks make the voice sound broken and slow.
3. Ezafe (the «-e» link between two words): mark it with a kasra on the first word ONLY when the voice could miss it
   or read it wrongly (for example a word ending with a vowel or «ه»: «خانه‌ی» stays «خانه‌ی»). Never more than needed.
4. Foreign names (people, places) and the book title: spell them exactly the way an Iranian reads them aloud, with
   Persian letters and the needed vowel marks (for example «دارسی» as «دارسی»، «ایزابِل»، «ریمون»), so the voice cannot
   misread them. Keep the SAME name in every paragraph.
5. Keep all punctuation and the final full stop of every sentence exactly. Keep half-spaces.
6. Keep the word «جمع‌بندی» if it exists. No Latin letters, no digits, no brackets, no emojis.

The book is «{book['title_fa']}» by «{book['author_fa']}».
Return JSON: {{"spoken": [{{"i": 0, "spoken_fa": "..."}}, ...]}} with one item per paragraph, in order.
Paragraphs: {json.dumps(items, ensure_ascii=False)}"""
    try:
        res = gemini_json(prompt, minutes=8, models=WRITE_MODELS)["spoken"]
        by_i = {int(r["i"]): r for r in res}
        used = 0
        for i, s in enumerate(scenes):
            txt = " ".join(str((by_i.get(i) or {}).get("spoken_fa", "")).replace("ي", "ی").replace("ك", "ک").split())
            if not txt or LATIN.search(txt) or DIGITS.search(txt):
                continue
            plain_len = len(s["narration_fa"])
            new_len = len(DIAC.sub("", txt))
            if not (0.8 * plain_len <= new_len <= 1.35 * plain_len):
                continue                                           # it changed the text too much: do not trust it
            if len(split_sentences(txt)) != len(split_sentences(s["narration_fa"])):
                continue                                           # the sentence structure must stay the same
            s["spoken_fa"] = txt
            used += 1
        print(f"Voice text prepared: {used} of {len(scenes)} paragraph(s) with diacritics/phonetic spelling.",
              flush=True)
    except Exception as e:  # noqa: BLE001
        print("Voice text preparation skipped:", e, flush=True)
    return data


def align_visuals(data, book):
    """Step D: write each picture from the FINAL narration and its source sentence (skipped if it fails)."""
    scenes = data["scenes"]
    chars = data.get("characters", [])
    items = [{"i": i, "narration_fa": s["narration_fa"], "source_en": s.get("source_en", "")}
             for i, s in enumerate(scenes)]
    prompt = f"""You are a film storyboard artist. Below are the FINAL Persian narration paragraphs, in order, of a video
that summarizes the book "{book['title_en']}" by {book['author_en']}, each with the English source sentence it comes
from. While each paragraph is read aloud, ONE picture is shown. For EACH paragraph write the picture so that it
illustrates the exact content of that paragraph, so a viewer who sees only the picture understands what is being said.

Return JSON: {{"visuals": [{{"i": 0, "characters_present": ["..."], "visual_en": "..."}}, ...]}}
with exactly {len(scenes)} items, in the same order.

Rules for "visual_en":
- English, 30 to 60 words. Start with the main subject doing the main action, then the place and time, then
  the important objects. Name 3 to 5 specific, concrete things from the paragraph (people, objects, place,
  clothing, weather) that must be visible.
- Show LITERALLY what the paragraph says at that moment, not a mood and not a general idea. One clear subject,
  one clear action, one place per picture.
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
SEED = 42                 # ONE fixed seed for the whole video: another seed can sound like another voice


def get_speaker(kind, seed=SEED):
    """Load the Pocket TTS Farsi v2 model once per voice (the weights download on first use)."""
    key = (kind, seed)
    if key not in _speakers:
        from sokhanvar import Sokhanvar
        ref = REF_AUDIO[kind].resolve()
        if not ref.exists():
            # no WAV in the repo: build it from azim_voices_data.py (base64 FLAC), if that file exists
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
    """The same text written in safe ways. The model needs a final full stop to know where to end.
    The last variant has no diacritics at all, in case the voice model does not like them."""
    base = " ".join(text.split())
    core = base.rstrip(" .،,;؛:!?؟…")
    v1 = core + "."                                                  # always ends with a full stop
    v2 = core.replace("\u200c", "") + "."                            # without half-spaces
    v3 = re.sub(r"[،,;؛:!?؟…«»\"'()\-]", " ", v2.rstrip("."))         # without punctuation inside
    v3 = " ".join(v3.split()) + "."
    v4 = DIAC.sub("", v1)                                            # without diacritics
    out = []
    for v in (v1, v2, v3, v4):
        if v not in out:
            out.append(v)
    return out


REPORT_FILE = ROOT / "azim_voice_report.txt"
REWRITTEN = []            # (original sentence, spoken sentence) pairs, used to keep the subtitles equal to the voice


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
    """Read one text with Pocket TTS (always the same seed). Tries several written forms of the text."""
    last = "unknown error"
    for variant in text_variants(text):
        try:
            get_speaker(kind).synthesize(variant, str(out_wav))
            if out_wav.exists() and out_wav.stat().st_size > 2000:
                dur = probe_duration(out_wav)
                if not suspicious(variant, dur):
                    return
                last = f"length cap: suspicious duration {dur:.1f}s for {len(variant.split())} words"
            else:
                last = "empty audio file"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        note(f"  TTS rejected: {variant[:60]} -> {last[:90]}")
        if "ezafe" in last:
            raise RuntimeError(f"Pocket TTS failed: {last}")   # deterministic: other forms do not help
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
                files = tts_pieces(new, kind, out_wav.with_name(f"{out_wav.stem}_rw{k}.wav"),
                                   level=0, rewrites=0)
                REWRITTEN.append((text, new))
                return files
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
# light noise cleaning + the SAME loudness for every scene (so the voice does not jump between scenes)
CLEAN_AF = "highpass=f=70,afftdn=nr=12:nf=-50,loudnorm=I=-16:TP=-1.5:LRA=7"


def make_voice(scenes, kind):
    print(f"Voice: Pocket TTS Farsi v2 ({kind}), scenes: {len(scenes)}", flush=True)
    (WORK / "tts_out").mkdir(exist_ok=True)
    wavs = []
    for i, s in enumerate(scenes):
        p = WORK / f"raw_{i:02d}.wav"
        plain = s["narration_fa"].strip()
        spoken = (s.get("spoken_fa") or plain).strip()
        REWRITTEN.clear()
        key = "spoken_fa" if spoken != plain else "narration_fa"
        try:
            tts_text(spoken, kind, p)
        except RuntimeError as e:
            if spoken == plain:
                raise
            # the voice could not read the prepared text: use the plain text of this scene
            note(f"  Scene {i}: voice text failed ({str(e)[:80]}); using the plain text.")
            REWRITTEN.clear()
            key = "narration_fa"
            tts_text(plain, kind, p)
        # a simpler rewrite was spoken: keep the text that matches the voice
        for orig, new in REWRITTEN:
            s[key] = s[key].replace(orig, new)
            if key == "narration_fa":
                s["spoken_fa"] = s["narration_fa"]
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
        af = CLEAN_AF + (f",atempo={factor:.3f}" if factor > 1.0 else "")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(p), "-af", af,
               "-ac", "1", "-c:a", "libmp3lame", "-q:a", "4", str(mp3)]
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


def cf_image(prompt, path, seed=None):
    """Cloudflare Workers AI FLUX.1 schnell (free: 10,000 neurons/day). Returns True on success."""
    global _cf_size_ok
    url = (f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
           f"/ai/run/@cf/black-forest-labs/flux-1-schnell")
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}", "Content-Type": "application/json"}
    for attempt in range(1, 4):
        payload = {"prompt": prompt[:1900], "steps": 4}
        if seed is not None:
            payload["seed"] = int(seed) % 2147483647
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
        if cf_image(prompt, path, seed):
            time.sleep(1)
            return
        print("WARNING: Cloudflare failed for this image, falling back to Pollinations (slow).", flush=True)
    needed = (scenes_left + 1) * 50
    if time.time() - START_TIME + needed > JOB_BUDGET:
        raise RuntimeError("Not enough time left for slow image generation")
    pollinations_image(prompt, path, seed)


def check_image(path, scene):
    """Ask Gemini (it looks at the picture) how well the picture shows the narration. (score 0-10, what is missing)."""
    prompt = f"""You check illustrations for a video. Narration (Persian): "{scene['narration_fa']}"
Intended picture: "{scene.get('visual_en', scene.get('action_en', ''))}"
Look at the image. Score from 0 to 10 how clearly it shows the main subject and the main action of the narration
(10 = a viewer understands what is said just by looking; 0 = unrelated).
Return JSON: {{"score": 7, "missing": "short English phrase: the key thing that is missing or wrong"}}"""
    try:
        r = gemini_json(prompt, minutes=2, models=CHECK_MODELS, image=path.read_bytes())
        time.sleep(3)
        return int(r.get("score", 10)), str(r.get("missing", "")).strip()
    except Exception as e:  # noqa: BLE001
        print("Image check skipped:", e, flush=True)
        return None


def make_checked_image(prompt, path, seed, scenes_left, scene):
    """Make the picture; if the check says it does not match the text, draw it again (up to 2 more times)."""
    make_image(prompt, path, seed, scenes_left)
    if not IMAGE_CHECK:
        return
    best = None                      # (score, bytes)
    for retry in range(3):
        res = check_image(path, scene)
        if res is None:
            return                   # the check is not available: keep the current picture
        score, missing = res
        print(f"Image check: score {score}/10 {('- ' + missing) if score < 6 else ''}", flush=True)
        if best is None or score > best[0]:
            best = (score, path.read_bytes())
        if score >= 6 or retry == 2:
            break
        try:
            make_image(prompt + f" The picture must clearly show: {missing}.", path,
                       seed + 1000 * (retry + 1), scenes_left)
        except Exception as e:  # noqa: BLE001
            print("Redraw failed:", e, flush=True)
            break
    if best is not None:
        path.write_bytes(best[1])    # keep the best picture of all tries


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

        # subtitles: always the clean text (no diacritics), cut into sentences
        sents = split_sentences(DIAC.sub("", s["narration_fa"]))
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

    style = (f"FontName=Noto Sans Arabic,FontSize={SUB_FONT_SIZE},PrimaryColour=&H00FFFFFF,"
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
def get_book_and_source(posted):
    """Pick a book that has a usable Wikipedia article. A book without one is skipped (never summarized from memory)."""
    rejected = []
    for _ in range(8):
        book = pick_book(posted, rejected)
        print("Book candidate:", book["title_en"], "|", book["book_type"], flush=True)
        src = fetch_source(book)
        if src:
            return book, src
        rejected.append(book["title_en"])
        print(f"Skipping '{book['title_en']}': no reliable source text.", flush=True)
    raise RuntimeError("Could not find a book with a usable Wikipedia source")


def main():
    posted = json.loads(POSTED_FILE.read_text("utf-8")) if POSTED_FILE.exists() else []
    if len(posted) >= TOTAL_BOOKS:
        print("All books are done.")
        return

    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    print(f"Image service: {'Cloudflare FLUX' if USE_CF else 'Pollinations (slow)'}; "
          f"image check: {'on' if IMAGE_CHECK else 'off'}; voice text prep: {'on' if VOICE_PREP else 'off'}",
          flush=True)
    book, src = get_book_and_source(posted)
    print("Book:", book["title_en"], "| source:", src["url"], flush=True)
    plan = plan_length(src)
    print(f"Plan: {plan[0]}-{plan[1]} scenes, {plan[2]} words (from {src['en_words']} source words)", flush=True)
    (WORK / "azim_source.txt").write_text(src["text"], encoding="utf-8")
    script = write_scenes(book, src, plan)
    scenes = script["scenes"]
    words = sum(len(s["narration_fa"].split()) for s in scenes)
    print(f"Script: {len(scenes)} scenes, {words} words, style: {script['art_style_en']}", flush=True)
    (WORK / "azim_script.json").write_text(json.dumps(script, ensure_ascii=False, indent=1), encoding="utf-8")
    (ROOT / "azim_last_script.json").write_text(json.dumps(script, ensure_ascii=False, indent=1), encoding="utf-8")
    (ROOT / "azim_last_source.txt").write_text(src["text"], encoding="utf-8")
    REPORT_FILE.write_text(f"Book: {book['title_en']}\nSource: {src['url']}\n", encoding="utf-8")

    kind = "female" if book["book_type"] in FEMALE_TYPES else "male"
    durs = make_voice(scenes, kind)

    seed = int(time.time()) % 100000
    for i, s in enumerate(scenes):
        prompt = build_prompt(script["art_style_en"], script.get("characters", []), s)
        img = WORK / f"img_{i:02d}.jpg"
        try:
            make_checked_image(prompt, img, seed + i, len(scenes) - i - 1, s)
        except Exception as e:  # noqa: BLE001
            if i == 0:
                raise
            # one failed picture must not lose the whole video: reuse the previous picture
            print(f"WARNING: image {i} failed ({e}); reusing the previous picture.", flush=True)
            shutil.copy(WORK / f"img_{i - 1:02d}.jpg", img)

    final = WORK / "azim_final.mp4"
    total = build_video(scenes, durs, final)
    print(f"video length = {total:.1f}s, size = {final.stat().st_size / 1e6:.1f} MB", flush=True)

    caption = (f"{book['title_fa']}\n{book['author_fa']}\n\n"
               f"منبع خلاصه: ویکی‌پدیا (CC BY-SA)\n{src['url']}")
    send_video(final, caption)

    posted.append({"title_en": book["title_en"], "title_fa": book["title_fa"],
                   "type": book["book_type"], "wiki": src["title"], "date": date.today().isoformat()})
    POSTED_FILE.write_text(json.dumps(posted, ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.rmtree(WORK, ignore_errors=True)
    print("Done.")


if __name__ == "__main__":
    main()
