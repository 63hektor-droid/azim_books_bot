import atexit
import base64
import json
import math
import os
import zlib
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

# The ONLY image service (free Cloudflare Workers AI, FLUX). Without these two secrets the bot stops before it spends
# any Gemini quota. There is no slow fallback any more: its pictures were not checked and were often wrong.
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
BOOKS_FILE = ROOT / "azim_books_list.json"        # the most famous novels, built once by azim_make_booklist.py
REJECTED_FILE = ROOT / "azim_rejected.json"       # books that can never be used (no source / blocked)
PENDING_FILE = ROOT / "azim_pending.json"         # finished script of a run that failed later: resumed next run
WORK = ROOT / "azim_work"

START_TIME = time.time()
JOB_BUDGET = 340 * 60     # seconds; GitHub Actions default job limit is 360 minutes

W, H = 720, 1280          # vertical 9:16 like the reference video (reels / phone screen)
FPS = 25
PAD = 0.25                # pause after each scene (seconds)
TOTAL_BOOKS = 2000              # only novels
MAX_SECONDS = 405         # every video is shorter than 7 minutes (hard limit 420 s)
MIN_SECONDS = 186         # every video is longer than 3 minutes (the voice is slowed a little if needed)
MIN_SLOW = 0.88           # slowest allowed voice speed (below this the narration is too short for 3 minutes)
MAX_SPEED = 1.2           # maximum voice speed-up (higher sounds unnatural)
ZOOM_MAX = 1 + float(os.environ.get("AZIM_ZOOM") or 0.04)    # slow zoom of at most 4 % (0 = no zoom at all)
WATERMARK = os.environ.get("AZIM_WATERMARK", "Samurai Reader").strip()   # Telegram channel name, bottom-left of EVERY picture ("" = none)
TITLE_CARDS = os.environ.get("AZIM_TITLE_CARDS", "1") != "0"   # author / book / theme parts: a title card instead of an AI picture
SUB_FONT_SIZE = 10        # subtitle size in 288-line units = about 44 px on the 1280 px tall vertical video
SUB_FONT = "Vazirmatn"        # bold Persian font like the user's reference subtitle (free); DejaVu Sans is the safe fallback
SUB_FONT_URLS = (             # free download, tried in order (the first that works is used)
    "https://cdn.jsdelivr.net/gh/rastikerdar/vazirmatn@v33.003/fonts/ttf/Vazirmatn-Bold.ttf",
    "https://github.com/rastikerdar/vazirmatn/raw/v33.003/fonts/ttf/Vazirmatn-Bold.ttf",
    "https://raw.githubusercontent.com/rastikerdar/vazirmatn/master/fonts/ttf/Vazirmatn-Bold.ttf",
)
BEAT_WORDS = int(os.environ.get("AZIM_BEAT_WORDS") or 24)   # a new picture about every 24 spoken words
MAX_WORDS = int(os.environ.get("AZIM_MAX_WORDS") or 860)   # about 5 minutes 55 seconds of narration
MIN_WORDS = int(os.environ.get("AZIM_MIN_WORDS") or 560)   # about 3 minutes 50 seconds of narration (never below 3 minutes)
MIN_FINAL_WORDS = int(os.environ.get("AZIM_MIN_FINAL_WORDS") or 470)   # the finished script (after cleaning) must still have this many words
VISUAL_VERSION = 24       # pictures of a saved script written by an older version are planned again (1 Gemini call)
SCRIPT_VERSION = 23       # a saved script of another version is not resumed

# ----- the ONLY source of the story: the "Plot" section of the novel's English Wikipedia article.
# It is an editor-reviewed, concise summary (the Wikipedia guideline asks for about 400-700 words), free to reuse
# (CC BY-SA). The bot retells it in Persian; it never summarizes the book on its own.
# The list builder already keeps only novels with a Plot of 350-900 words; these limits are a wider safety net.
PLOT_MIN_WORDS = int(os.environ.get("AZIM_PLOT_MIN") or 420)
PLOT_MAX_WORDS = int(os.environ.get("AZIM_PLOT_MAX") or 2600)

# ----- narrator engine: "gemini" (Gemini TTS, default; any failure or finished quota -> Pocket TTS for that scene)
# ----- or "pocket" (offline Pocket TTS only). Switch without editing code: repository variable AZIM_TTS.
TTS_ENGINE = (os.environ.get("AZIM_TTS") or "gemini").strip().lower()
GEMINI_TTS_MODEL = os.environ.get("AZIM_GEMINI_TTS_MODEL") or "gemini-3.8-flash-tts"
# a second FREE voice model with its own quota: used for a whole video when the first one is finished or fails
GEMINI_TTS_MODELS = [m for m in dict.fromkeys([GEMINI_TTS_MODEL, os.environ.get("AZIM_GEMINI_TTS_MODEL2")
                                               or "gemini-3.8-flash-lite-tts"]) if m]
GEMINI_TTS_VOICES = {"female": os.environ.get("AZIM_GEMINI_VOICE_FEMALE") or "Sulafat",
                     "male": os.environ.get("AZIM_GEMINI_VOICE_MALE") or "Charon"}      # Gacrux is a FEMALE voice of Google; Charon is a deep male voice
GEMINI_TTS_TEXT = (os.environ.get("AZIM_GEMINI_TTS_TEXT") or "plain").strip().lower()   # plain | spoken
GEMINI_TTS_STYLE = os.environ.get("AZIM_GEMINI_TTS_STYLE") or (
    "a warm, calm storyteller telling a story to one close listener, with real feeling that follows the story: "
    "curious at the start, tense when things get hard, tender or sad where the story is, quietly hopeful at the "
    "end; natural pace and breathing, never a news-reader voice")

# ----- voices: Pocket TTS Farsi v2 via the "sokhanvar" library (free, offline, CPU).
# Each voice is cloned from a reference recording of at most 5 seconds (WAV), placed next to this script.
# female = atena, male = kian
REF_AUDIO = {
    "female": Path(os.environ.get("AZIM_REF_FEMALE", Path(__file__).parent / "azim_voice_female.wav")),
    "male": Path(os.environ.get("AZIM_REF_MALE", Path(__file__).parent / "azim_voice_male.wav")),
}
# The voice alternates: one video with the female voice, the next with the male voice (all books are novels).

DIAC = re.compile(r"[\u064B-\u0655\u0670]")   # Persian/Arabic short-vowel marks (fatha, kasra, damma, shadda, sukun...)


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
class ScriptTooShort(RuntimeError):
    """The narration is too short for the rule "every video is longer than 3 minutes"."""


class GeminiBlocked(RuntimeError):
    """Gemini refused the prompt (content/safety block): the same book would fail again."""


class GeminiQuotaExhausted(RuntimeError):
    """The DAILY quota of every model is finished: stop quickly instead of waiting for nothing."""


CALLS = {"gemini": 0, "voice": 0, "cloudflare": 0}      # how many calls this run made (printed at the end)
CALLS_BY_MODEL = {}
# Cloudflare accounting of THIS run (to compare with the dashboard "Neurons used today"): picture calls that
# worked / failed and the neurons they should cost by the official price list (an ESTIMATE, not the real bill).
CF_STATS = {"ok": 0, "fail": 0, "est_neurons": 0.0, "by_status": {}}


@atexit.register
def print_call_counts():
    per = ", ".join(f"{m}: {n}" for m, n in sorted(CALLS_BY_MODEL.items()))
    print(f"CALLS THIS RUN: Gemini text/picture/audio {CALLS['gemini']} ({per or '-'}), "
          f"Gemini voice {CALLS['voice']}, Cloudflare pictures {CALLS['cloudflare']}", flush=True)
    if CALLS["cloudflare"]:
        print(f"CLOUDFLARE THIS RUN: {CF_STATS['ok']} pictures drawn, {CF_STATS['fail']} calls failed "
              f"(by status: {CF_STATS['by_status']}); estimated neurons by the official price list: "
              f"{CF_STATS['est_neurons']:.0f}. Compare this number with 'Neurons used today' in the Cloudflare "
              f"dashboard (Workers AI) after this run.", flush=True)


DEAD_MODELS = set()      # models whose daily quota is finished (not tried again in this run)
SAFETY = [{"category": c, "threshold": "BLOCK_ONLY_HIGH"} for c in (
    "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")]
BLOCK_FINISH = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY"}


def short_error(text):
    """A short readable summary of a Gemini error body (message + which quota + retry time)."""
    ids = re.findall(r'"quotaId":\s*"([^"]+)"', text)
    delay = re.findall(r'"retryDelay":\s*"([^"]+)"', text)
    msg = re.findall(r'"message":\s*"([^"]{0,160})', text)
    out = [msg[0] if msg else text[:160]]
    if ids:
        out.append("quota: " + ", ".join(sorted(set(ids))))
    if delay:
        out.append("retry in " + delay[0])
    return " | ".join(out)


def parse_json_text(text):
    """json.loads that also survives ``` fences or extra words around the JSON."""
    try:
        return json.loads(text)
    except ValueError:
        t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        for a, b in (("{", "}"), ("[", "]")):
            i, k = t.find(a), t.rfind(b)
            if 0 <= i < k:
                try:
                    return json.loads(t[i:k + 1])
                except ValueError:
                    pass
        raise


def gemini_json(prompt, minutes=12, models=None, image=None, read_timeout=150, audio=None, images=None):
    parts = [{"text": prompt}]
    if images:                      # several pictures in one call, each one labeled with its number
        pre = []
        for k, im in enumerate(images, 1):
            pre += [{"text": f"IMAGE {k}:"},
                    {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(im).decode()}}]
        parts = pre + parts
    if image is not None:
        parts.insert(0, {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(image).decode()}})
    if audio is not None:
        parts.insert(0, {"inline_data": {"mime_type": "audio/mpeg", "data": base64.b64encode(audio).decode()}})
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.7},
    }
    if os.environ.get("AZIM_GEMINI_SAFETY", "relaxed") != "default":
        body["safetySettings"] = SAFETY       # fewer false blocks on the plots of famous novels
    headers = {"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"}
    models = models or FREE_TEXT_MODELS
    dead = set()          # not usable at all (400/404)
    blocked = set()       # models that blocked this prompt
    deadline = time.time() + minutes * 60
    last = None
    round_no = 0
    while time.time() < deadline:
        alive = [m for m in models if m not in dead and m not in DEAD_MODELS]
        if not alive:
            if any(m in DEAD_MODELS for m in models):
                raise GeminiQuotaExhausted(last or "daily quota finished")
            break
        round_no += 1
        for model in alive:
            if time.time() >= deadline:
                break
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            try:
                print(f"Gemini: round={round_no} model={model}", flush=True)
                CALLS["gemini"] += 1
                CALLS_BY_MODEL[model] = CALLS_BY_MODEL.get(model, 0) + 1
                r = requests.post(url, headers=headers, json=body, timeout=(30, read_timeout))
                if r.status_code == 200:
                    j = r.json()
                    cands = j.get("candidates") or []
                    text = ""
                    if cands:
                        pts = (cands[0].get("content") or {}).get("parts") or []
                        text = "".join(p.get("text", "") for p in pts)
                    if text.strip():
                        data = parse_json_text(text)
                        if isinstance(data, list):
                            data = data[0]
                        print(f"Gemini OK with {model}", flush=True)
                        return data
                    block = (j.get("promptFeedback") or {}).get("blockReason", "")
                    finish = cands[0].get("finishReason", "") if cands else ""
                    last = f"{model}: empty answer (blockReason={block or '-'}, finishReason={finish or '-'})"
                    print(last, flush=True)
                    if block or finish in BLOCK_FINISH:
                        blocked.add(model)
                        if len(blocked) >= min(2, len(models)):
                            raise GeminiBlocked(last)
                else:
                    last = f"{model}: {r.status_code} {short_error(r.text)}"
                    print(last, flush=True)
                    if r.status_code == 429 and ("PerDay" in r.text or "limit: 0" in r.text):
                        DEAD_MODELS.add(model)
                        print(f"  -> the daily quota of {model} is finished: not used again in this run.", flush=True)
                        continue
                    if r.status_code in (400, 404):
                        dead.add(model)
            except GeminiBlocked:
                raise
            except Exception as e:  # noqa: BLE001
                last = f"{model}: {e}"
                print(last, flush=True)
            time.sleep(8)
        if not [m for m in models if m not in dead and m not in DEAD_MODELS]:
            continue                      # nothing left to wait for: the loop start decides what to raise
        time.sleep(min(30 * round_no, 120))
    raise RuntimeError(f"Gemini failed: {last}")


# ONLY models of the free tier (the Pro models are paid). Every model has its own free quota, so when one is
# finished the next one continues. The two "-latest" names are a safety net in case a model name changes.
FREE_TEXT_MODELS = [GEMINI_MODEL, "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
                    "gemini-flash-latest", "gemini-flash-lite-latest"]
WRITE_MODELS = list(FREE_TEXT_MODELS)
# cheap jobs (picture check, simple rewrites) go to the lite model first: every model has its own quota
CHEAP_MODELS = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-flash-lite-latest", GEMINI_MODEL,
                "gemini-3.6-flash", "gemini-flash-latest"]


# ---------------------------------------------------------------- the book and its reliable source
def load_json(path, default):
    try:
        return json.loads(path.read_text("utf-8")) if path.exists() else default
    except Exception as e:  # noqa: BLE001
        print(f"Could not read {path.name}: {e}", flush=True)
        return default


def save_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def rejected_titles(rejected):
    # a book refused only because its Plot was too long/short for the OLD limits is tried again (the limits changed)
    return [r["title_en"] if isinstance(r, dict) else str(r) for r in rejected
            if not (isinstance(r, dict) and "no usable Plot section" in str(r.get("reason") or ""))]


def reject_book(rejected, book, reason):
    """A book that can never work (no Wikipedia source, blocked by Gemini...) is never chosen again."""
    rejected.append({"title_en": book["title_en"], "reason": str(reason)[:200], "date": date.today().isoformat()})
    save_json(REJECTED_FILE, rejected)
    print(f"Book rejected for good: {book['title_en']} ({str(reason)[:120]})", flush=True)


def pick_book(posted, rejected):
    """Step A: the next novel of azim_books_list.json (ordered by fame). Without that file Gemini picks a novel."""
    used = {norm(p["title_en"]) for p in posted} | {norm(x) for x in rejected_titles(rejected)}
    used_fa = {norm_fa(p["title_fa"]) for p in posted if p.get("title_fa")}
    listed = load_json(BOOKS_FILE, [])
    if isinstance(listed, dict):
        listed = listed.get("books", [])
    if listed:
        for b in listed:
            try:
                tfa = str(b.get("title_fa") or "")
                if norm(b["title_en"]) in used or (tfa and norm_fa(tfa) in used_fa):
                    continue
                return {"title_en": b["title_en"], "title_fa": tfa,
                        "author_en": b["author_en"], "author_fa": str(b.get("author_fa") or ""),
                        "wikipedia_title_en": b.get("wiki_en", ""), "wikipedia_title_fa": b.get("wiki_fa", ""),
                        "book_type": b.get("book_type") or "novel"}
            except (KeyError, TypeError):
                continue
        raise RuntimeError("Every novel of azim_books_list.json is used or rejected")
    print("azim_books_list.json not found: Gemini picks the novel (slower, uses quota).", flush=True)
    all_titles = [p["title_en"] for p in posted] + rejected_titles(rejected)
    number = len(posted) + 1
    for _ in range(4):
        prompt = f"""You are the editor of a Persian channel that summarizes the most famous novels in the world.
This is video number {number} of {TOTAL_BOOKS}. Choose ONE very famous, world-renowned NOVEL that is NOT in
this already-used list: {json.dumps(all_titles, ensure_ascii=False)}.
Pick the most famous remaining ones first (world classics, global bestsellers, novels everybody knows).
Prefer a novel whose English Wikipedia article has a detailed "Plot" or "Synopsis" section.

Return JSON with exactly these keys:
- "title_en": English title
- "title_fa": Persian title (the form commonly used in Iran)
- "author_en": author name in English
- "author_fa": author name in Persian
- "wikipedia_title_en": the exact title of the English Wikipedia article about the novel
- "wikipedia_title_fa": the exact title of the Persian Wikipedia article about the novel, or "" if you are not sure"""
        data = gemini_json(prompt, minutes=8, models=CHEAP_MODELS)
        try:
            ok = (
                norm(data["title_en"]) not in used
                and norm_fa(data["title_fa"]) not in used_fa
                and data.get("title_fa") and data.get("author_fa") and data.get("author_en")
            )
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            data["book_type"] = "novel"
            return data
        time.sleep(5)
    raise RuntimeError("Could not get a valid new novel from Gemini")


WIKI_HEADERS = {"User-Agent": "azim-book-video-bot/1.0 (educational book summaries; GitHub Actions)"}
HEAD = re.compile(r"^(={2,6})\s*(.+?)\s*\1\s*$", re.M)
# level-2 headings that hold the story: "Plot", "Plot summary", "Plot introduction", "Synopsis", "Summary", "Story"
PLOT_HEAD = re.compile(r"^(plot|synopsis|summary|story|storyline)\b", re.I)
PLOT_SKIP = re.compile(r"adaptation|film|television|reception|translation|publication|criticism|history", re.I)
SIG_KEYS = re.compile(r"legacy|influence|reception|critical|significance|impact|awards?|controvers|cultural|themes?", re.I)


def wiki_api(lang, params):
    p = dict(params)
    p.update({"format": "json", "formatversion": 2})
    for wait in (0, 4, 12):
        if wait:
            time.sleep(wait)
        r = requests.get(f"https://{lang}.wikipedia.org/w/api.php", params=p, headers=WIKI_HEADERS, timeout=40)
        if r.status_code != 429:
            break
    r.raise_for_status()
    time.sleep(0.4)                      # polite pause between Wikipedia requests
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


def wiki_title_fa(title_en):
    """The title of the PERSIAN Wikipedia article that is the interlanguage link of the English article (free, no Gemini
    call), without a trailing disambiguation like «(رمان)». "" when there is none."""
    try:
        d = wiki_api("en", {"action": "query", "prop": "langlinks", "lllang": "fa", "redirects": 1, "titles": title_en})
        links = (d["query"]["pages"][0].get("langlinks") or [])
        t = str(links[0]["title"]).strip() if links else ""
        return re.sub(r"\s*[(（][^)）]*[)）]\s*$", "", t).strip()
    except Exception as e:  # noqa: BLE001
        print(f"Persian title lookup failed ({e}).", flush=True)
        return ""


AUTHOR_FA_FIX = {            # a spelling that is wrong or unusual in Iran -> the spelling that Iranian readers know
    "لف تولستوی": "لئو تولستوی", "لو تولستوی": "لئو تولستوی", "لیو تولستوی": "لئو تولستوی", "لیف تولستوی": "لئو تولستوی",
    "لف نیکولایویچ تولستوی": "لئو تولستوی", "لف نیکلایویچ تولستوی": "لئو تولستوی", "لئو نیکولایویچ تولستوی": "لئو تولستوی",
    "لیو نیکولایویچ تولستوی": "لئو تولستوی", "لئو نیکلایویچ تولستوی": "لئو تولستوی",
    "فیودور داستایفسکی": "فئودور داستایفسکی", "فیودور میخایلوویچ داستایفسکی": "فئودور داستایفسکی",
    "فئودور میخایلوویچ داستایفسکی": "فئودور داستایفسکی", "فیودور داستایوسکی": "فئودور داستایفسکی",
    "آنتون چخوف": "آنتوان چخوف", "آنتون پاولوویچ چخوف": "آنتوان چخوف",
}
AUTHOR_EN_FA = {"leo tolstoy": "لئو تولستوی", "lev tolstoy": "لئو تولستوی", "leo nikolayevich tolstoy": "لئو تولستوی",
                "fyodor dostoevsky": "فئودور داستایفسکی", "fyodor dostoyevsky": "فئودور داستایفسکی",
                "anton chekhov": "آنتوان چخوف"}


def _fa_key(t):
    t = DIAC.sub("", str(t or "")).replace("\u200c", " ").replace("ي", "ی").replace("ك", "ک")
    return " ".join(t.split())


def fa_author(name_fa, name_en=""):
    """The Persian name of the author in the spelling that Iranian readers know (for example «لئو تولستوی», never
    «لف تولستوی»). The Persian Wikipedia title or Gemini can give another spelling: this table fixes it in code."""
    key = _fa_key(name_fa)
    fixed = {_fa_key(k): v for k, v in AUTHOR_FA_FIX.items()}
    if key in fixed:
        return fixed[key]
    en = " ".join(re.sub(r"[^a-z ]", " ", str(name_en or "").lower()).split())
    if en in AUTHOR_EN_FA and (not key or key.split()[-1] in ("تولستوی", "داستایفسکی", "چخوف")):
        return AUTHOR_EN_FA[en]
    return str(name_fa or "").strip()


def fix_author_fa(book):
    """An author without a Persian name in the list: the title of the Persian Wikipedia article of the author (free, no
    Gemini call). Stays empty when there is none (the story spine then asks for it in its own call)."""
    if book.get("author_fa"):
        return book
    try:
        last = norm(book["author_en"].split()[-1])
        hits = [h_ for h_ in wiki_search("en", book["author_en"]) if last and last in norm(h_)]   # the same person, not a book
        if hits:
            t = wiki_title_fa(hits[0])
            if t:
                print(f"Persian author name from Persian Wikipedia: {t}", flush=True)
                book["author_fa"] = t
    except Exception as e:  # noqa: BLE001
        print(f"Persian author lookup failed ({e}).", flush=True)
    return book


def fix_title_fa(book):
    """The Persian title of the book comes from the list file or from Gemini and can be wrong (an invented or misspelled word
    that means nothing). The Persian Wikipedia title of the same book is the reference: when it exists and differs, it
    replaces the title."""
    title = wiki_title_fa(book.get("wikipedia_title_en") or book["title_en"])
    if title and norm_fa(title) != norm_fa(book.get("title_fa") or ""):
        print(f"Persian title corrected: '{book.get('title_fa')}' -> '{title}' (Persian Wikipedia).", flush=True)
        book["title_fa"] = title
    return book


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


def pick_significance(text):
    """Reception / legacy / influence / themes sections: facts for the closing wrap-up (why the novel matters)."""
    lead, secs = split_wiki(text)
    chosen = [(t, b) for t, b in secs if len(b) >= 80 and SIG_KEYS.search(t) and not PLOT_HEAD.match(t)]
    return "\n\n".join(f"[{t}]\n{b[:800]}" for t, b in chosen)


def pick_plot(text):
    """(lead, plot text) of an article: only the level-2 sections that tell the story."""
    lead, secs = split_wiki(text)
    chosen = [b for t, b in secs if len(b) >= 80 and PLOT_HEAD.match(t) and not PLOT_SKIP.search(t)]
    return lead, "\n\n".join(chosen)


# The plot / characters sections of the OTHER Wikipedias (free, same licence, no Gemini call). Another editor's summary of the
# same novel confirms the events, fills what the English one skips and shows the real meaning of a term.
EXTRA_LANGS = (("fa", "Persian"), ("fr", "French"), ("de", "German"), ("ru", "Russian"), ("es", "Spanish"))
PLOT_HEAD_LANG = {
    "fa": re.compile(r"^(خلاصه|داستان|طرح|پیرنگ|مضمون|پیرنگ)", re.I),
    "fr": re.compile(r"^(résumé|resume|intrigue|synopsis|histoire|argument|trame|contenu)", re.I),
    "de": re.compile(r"^(handlung|inhalt|inhaltsangabe|zusammenfassung|kurzinhalt)", re.I),
    "ru": re.compile(r"^(сюжет|содержание|краткое содержание)", re.I),
    "es": re.compile(r"^(argumento|sinopsis|resumen|trama)", re.I),
}
CHAR_HEAD = re.compile(r"^(characters?|main characters?|principal characters?|major characters?|cast of characters|cast)\b", re.I)
EXTRA_WORDS_MAX = int(os.environ.get("AZIM_EXTRA_WORDS") or 600)      # words taken from one other-language summary
EXTRA_SOURCES = int(os.environ.get("AZIM_EXTRA_SOURCES") or 3)       # at most this many other-language summaries


THEME_HEAD = re.compile(r"^(themes?|themes and motifs|analysis|interpretation|symbolism|style and themes|major themes|"
                        r"literary (?:analysis|significance)|meaning)\b", re.I)


def pick_themes(text):
    """The 'Themes' / 'Analysis' sections: the heart of the book (what it is REALLY about). Never a source of events."""
    lead, secs = split_wiki(text)
    return "\n\n".join(b for t, b in secs if len(b) >= 150 and THEME_HEAD.match(t))


def pick_characters(text):
    """The 'Characters' section of the English article: who is who (ages, roles, relations). Never a source of events."""
    lead, secs = split_wiki(text)
    chosen = [b for t, b in secs if len(b) >= 120 and CHAR_HEAD.match(t)]
    return "\n\n".join(chosen)


def fetch_extra_plots(title_en, en_words):
    """[(language name, plot text)] from other-language Wikipedias (interlanguage links of the English article). Any failure
    only means fewer extra sources: the English Plot alone is always enough."""
    out = []
    try:
        d = wiki_api("en", {"action": "query", "prop": "langlinks", "lllimit": "max", "redirects": 1, "titles": title_en})
        links = {x["lang"]: x["title"] for x in (d["query"]["pages"][0].get("langlinks") or [])}
    except Exception as e:  # noqa: BLE001
        print(f"Other-language Wikipedia links not found ({e}).", flush=True)
        return out
    for lang, name in EXTRA_LANGS:
        if len(out) >= EXTRA_SOURCES:
            break
        t = links.get(lang)
        if not t:
            continue
        try:
            found = wiki_fulltext(lang, t)
            if not found:
                continue
            lead, secs = split_wiki(found[1])
            plot = "\n\n".join(b for tt, b in secs if len(b) >= 80 and PLOT_HEAD_LANG[lang].match(tt.strip()))
            words = plot.split()
            if len(words) < max(150, int(en_words * 0.5)):          # a thin section adds nothing
                continue
            lead_w = " ".join(lead.split()[:180])
            out.append((name, (f"INTRODUCTION ({name}): {lead_w}\n\nPLOT: " if lead_w else "") + " ".join(words[:EXTRA_WORDS_MAX])))
            print(f"Extra source: {name} Wikipedia plot, {len(words)} words (using {min(len(words), EXTRA_WORDS_MAX)}).", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"Extra source {lang} skipped ({e}).", flush=True)
    return out


def fetch_wiki(book):
    """The English Wikipedia article of the novel: lead, Plot section and a short 'significance' text."""
    last = (book["author_en"].replace("\u200c", "").lower().split() or [""])[-1]
    found = None
    for c in (book.get("wikipedia_title_en"), book["title_en"]):
        if c:
            found = wiki_fulltext("en", c)
            if found:
                break
    if not found:
        for t in wiki_search("en", f'{book["title_en"]} {book["author_en"]}'):
            found = wiki_fulltext("en", t)
            if found:
                break
    if not found:
        return None
    title, text = found
    if last and last not in text.replace("\u200c", "").lower():
        print(f"Wikipedia page '{title}' does not mention the author: ignored.", flush=True)
        return None
    lead, plot = pick_plot(text)
    return {"title": title, "lead": lead, "plot": plot, "plot_words": len(plot.split()),
            "sig": pick_significance(text), "chars": pick_characters(text),
            "themes": pick_themes(text)}


def fetch_source(book):
    """Facts for the video: ONLY the Plot section of the English Wikipedia article (+ its introduction and a short
    significance text for the closing wrap-up). Nothing comes from memory. None if the novel has no usable Plot."""
    en = fetch_wiki(book)
    if not en or not en["plot"]:
        print("No Plot section on Wikipedia for this novel.", flush=True)
        return None
    pw = en["plot_words"]
    if pw < PLOT_MIN_WORDS or pw > PLOT_MAX_WORDS:
        print(f"The Plot section has {pw} words (allowed {PLOT_MIN_WORDS}-{PLOT_MAX_WORDS}): novel skipped.",
              flush=True)
        return None
    parts = ["INTRODUCTION (what the novel is, who wrote it, when):\n" + en["lead"][:900],
             f"PLOT (an editor-reviewed concise summary of the whole story, {pw} words; the ONLY source of "
             "the events):\n" + en["plot"]]
    n_core = len(parts)
    for name_, txt_ in fetch_extra_plots(en["title"], pw):
        parts.append(f"SECOND SOURCE - PLOT from the {name_} Wikipedia (another editor's summary of the SAME novel: use it to "
                     "confirm events, to fill what the English PLOT skips and to understand a term; when they disagree the "
                     "English PLOT wins; write every name as in the English text):\n" + txt_)
    if en.get("themes"):
        parts.append("THEMES AND MEANING (what the book is REALLY about: use ONLY for the opening hook, the want and fear of the "
                     "heroes and the wrap-up; NEVER a source of events):\n" + " ".join(en["themes"].split()[:450]))
    if en.get("chars"):
        parts.append("CHARACTERS (only to know who is who: ages, roles and relations; NEVER a source of events):\n"
                     + " ".join(en["chars"].split()[:450]))
    if en["sig"]:
        parts.append("SIGNIFICANCE (themes, reception, legacy: use ONLY for the closing wrap-up, to say why the "
                     "novel matters):\n" + en["sig"][:1500])
    core = list(parts[:n_core])
    if en["sig"]:
        core.append("SIGNIFICANCE (themes, reception, legacy: use ONLY for the closing wrap-up, to say why the "
                    "novel matters):\n" + en["sig"][:1500])
    url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(en["title"].replace(" ", "_"))
    print(f"Source: Wikipedia Plot section, {pw} words (+ {len(parts) - 2 - bool(en['sig'])} extra part(s)).", flush=True)
    return {"text": "\n\n".join(parts), "text_write": "\n\n".join(core), "en_words": pw, "url": url, "title": en["title"],
            "credit": "منبع خلاصه: ویکی‌پدیا (CC BY-SA)"}


def plan_length(src):
    """The narration is about as long as the Plot summary itself: short, dense, nothing added."""
    # every video must last more than 3 and less than 7 minutes: never fewer than MIN_WORDS, never more than MAX_WORDS
    target = int(max(MIN_WORDS, min(MAX_WORDS, src["en_words"] * 0.95)))
    n = max(8, round(target / 58))
    lo, hi = max(5, n - 1), n + 1
    return lo, hi, f"{int(target * 0.9)} to {int(target * 1.1)}"


# ---------------------------------------------------------------- script
# The picture styles. The robot only CHOOSES a key (by the rules below); the exact style words are fixed here, so
# the robot can never invent a wrong or mixed style.
STYLES = {
    # ONE fixed style for ALL videos (the user's choice, changed Nov 12, 2026 to the 3 sample pictures): a detailed
    # black-and-white graphite PENCIL drawing in manga / anime style - fine hatching, soft smooth shading, large expressive
    # eyes, attractive faces, rich detailed clothes and backgrounds. Only the first 4 comma pieces go into most prompts.
    "storybook": ("detailed black-and-white graphite pencil drawing in manga-anime style, fine hatching and soft smooth "
                  "shading, realistic adult body proportions with a natural small head (about one seventh of the body height) "
                  "and never a big head or chibi look, expressive eyes and elegant attractive detailed faces, "
                  "richly detailed textured clothing, highly detailed atmospheric backgrounds drawn in pencil, "
                  "strong contrast from deep black to white paper, whole picture in this one style",
                  "every book (this is the only style)"),
}
FIXED_STYLE = STYLES["storybook"][0]
STYLE_GUIDE = "\n".join(f'  * "{k}": use for {v[1]}.' for k, v in STYLES.items())

TONE_RULES = """- TONE AND FLOW: a warm storyteller who tells the story to ONE listener, like a friend on a quiet evening, with
  real feeling that follows the story: curiosity at the start, tension when things get hard, sadness or tenderness
  where the story is sad or tender, quiet hope or irony at the end. The feeling lives in the choice of words and in the
  rhythm, never in invented facts, never exaggerated, never theatrical, never a news-reader voice.
  The text must FLOW as ONE continuous story: every sentence grows out of the previous one through a connector or a
  reference («اما»، «بعد از آن»، «برای همین»، «در همین حال»، «تا این‌که»، «با این همه»، «چون»). It must NEVER be a list of
  short, separate statements. Sentences of 8 to 22 words (a few short ones only for emphasis), 3 to 5 sentences per
  paragraph, and the last sentence of a paragraph leads into the next paragraph.
  Use vivid concrete verbs; show the character's state of mind when the source states it.
  Clean standard spoken Iranian Persian (no slang contractions such as «میگم» or «اون»); never stiff written-style forms
  such as «نمود»، «گردید»، «می‌باشد»، «لذا»، «مذکور»; no advertising tone.
  Punctuation guides the voice: commas for breath, «…» for suspense, «؟» for a real question (rare), «!» only for
  strong emotion.
  MAKE THE LISTENER CURIOUS AND MOVED (a retelling that is only correct is boring): the video opens with a HOOK from the START
  situation of the story (the strange, gripping problem the hero faces), never a dry announcement; the WANT and the FEAR of
  each hero (what he or she risks) are said in plain words the first time he or she matters; every turning point, loss,
  betrayal, discovery and the ending gets weight: a short sentence of its own and ONE concrete detail from the source (an
  object, a place, a spoken line, a gesture) that makes the moment memorable; a scene ends on the question or the tension that
  the next event answers; irony and the price of a choice are said when the source shows them. Everything comes from the
  source: nothing is invented and no spoiler comes before its turn."""

MEANING_RULE = """- MEANING, NOT WORDS - THE LISTENER MUST NEVER HEAR SOMETHING ILLOGICAL. The source is English. Never translate word for
  word. Before you write a sentence, ask: "Is this sentence logical in the real world of THIS story? Could it really
  happen as I wrote it?" When the literal reading is impossible, absurd or silly (a spaceship that «commits suicide», a house
  that «dies», a city that «sleeps», an ocean that «thinks», a thing that does something only a person can do), the author
  meant something ELSE: a metaphor, a personification, irony, a euphemism, a technical term, a nickname, an idiom or a double
  meaning (ایهام). Find the real meaning from the whole story and the SOURCE TEXT, and write what REALLY happened in plain,
  natural Persian (for example a ship that «kills itself» = the ship destroys itself on purpose, so write that it destroyed
  itself). When a word has two meanings at once, write the meaning that the story needs; add the second one in a short
  clause only if the listener would miss the point without it. This applies to the NAMES, TITLES and NICKNAMES of the heroes too: a name can be a pun, a pen name or a double meaning (a
  narrator called «A Square» may really be a square-shaped being): decide what each hero REALLY is from the whole story and
  the source, never from the name read literally. Never invent a meaning that the source and the story do not
  support: when you are not sure, write the plain physical fact that the source states. Every sentence must make sense to a
  Persian listener who never read the book."""

NAMES_RULE = """- NAMES: write the names of people, places, the novel and its author in ENGLISH (Latin) letters, as in the
  SOURCE TEXT (for example Holden, The Catcher in the Rye). SHORT NAMES: a long name misleads the listener. A person with
  a name of several parts (first name, middle name, patronymic, family name, title) is called ONLY by the FIRST name
  (Natasha Ilyinichna Rostova becomes Natasha; Prince Andrei Nikolayevich Bolkonsky becomes Andrei), in EVERY sentence,
  always the same short name; two people with the same first name are told apart by their role («Andrei, the prince»).
  A long place name is shortened to its main word. The title of the novel and the name of its author stay complete. A voice reads them in English. Everything else is
  Persian: never write any other English word and never write digits (numbers as Persian words). Put a space between a
  Persian word and a name, never glue Persian letters to a Latin name (no «Holden‌ی»). If grammar needs a suffix on a name,
  rephrase so that the name stands alone."""


# Starting points that guarantee VARIETY (the source, the place, the era and the age always win over them).
DIVERSITY_SLOTS = (
    ("black hair", "tall and thin", "burnt orange"),
    ("blond hair", "short and round", "deep red"),
    ("red or copper hair", "broad-shouldered and muscular", "navy blue"),
    ("gray or white hair", "slim, of medium height", "mustard yellow"),
    ("dark brown hair", "small and frail", "purple"),
    ("light brown hair", "heavy and tall", "light gray"),
)


EYE_COLORS = ("dark brown eyes", "blue eyes", "green eyes", "hazel eyes", "gray eyes", "light brown eyes")
_VARIETY = {"offset": 0}        # set per book: the main character of one video never gets the same slot as in the next one


def set_variety(book):
    """The starting slots, the accessories and the face numbers are rotated by the title of the book, so the main
    characters of different videos do not all look alike (always the same hair, the same coat and the same hat)."""
    n = _VARIETY.get("n")                  # the number of videos already posted: video 1, 2, 3... get slot 1, 2, 3...
    if n is None:
        n = zlib.crc32(norm(book.get("title_en") or "").encode("utf-8"))
    _VARIETY["offset"] = n % len(DIVERSITY_SLOTS)


def diversity_text():
    off = _VARIETY["offset"]
    rows = "\n".join(f"  character {k + 1}: {DIVERSITY_SLOTS[(k + off) % len(DIVERSITY_SLOTS)][0]}; "
                     f"{DIVERSITY_SLOTS[(k + off) % len(DIVERSITY_SLOTS)][1]}; "
                     f"main outfit color {DIVERSITY_SLOTS[(k + off) % len(DIVERSITY_SLOTS)][2]}; "
                     f"{EYE_COLORS[(k * 5 + off * 2 + 1) % len(EYE_COLORS)]}"
                     for k in range(len(DIVERSITY_SLOTS)))
    return ("- VARIETY SLOTS (starting points so that nobody looks alike; the SOURCE, the place, the era, the gender and "
            "the age always win: when a slot does not fit a character, choose another hair color, eye color, build and "
            "outfit color that nobody else has). The look of every character comes from the source, the era, the job and these "
            "slots:\n" + rows)


# ----- every character gets a DIFFERENT hair color, shirt color and trousers/skirt color (checked by CODE, not only
# by the prompt): when two characters share one of them, the later one gets a free color from the palette.
_COLOR_FAMILIES = (
    ("black", "black"), ("white", "white"), ("gray", "gray"), ("grey", "gray"), ("silver", "gray"),
    ("red", "red"), ("crimson", "red"), ("maroon", "red"), ("burgundy", "red"), ("copper", "red"),
    ("auburn", "red"), ("ginger", "red"), ("pink", "pink"), ("orange", "orange"), ("yellow", "yellow"),
    ("mustard", "yellow"), ("gold", "yellow"), ("blond", "yellow"), ("green", "green"), ("olive", "green"),
    ("teal", "teal"), ("blue", "blue"), ("navy", "blue"), ("purple", "purple"), ("violet", "purple"),
    ("brown", "brown"), ("tan", "brown"), ("beige", "cream"), ("cream", "cream"),
)
OUTFIT_PALETTE = ("red", "navy blue", "forest green", "mustard yellow", "purple", "white", "black", "orange", "teal",
                  "brown", "gray", "pink")
HAIR_PALETTE = ("black", "blond", "copper red", "brown")


def color_family(s):
    s = str(s or "").lower()
    for key, fam in _COLOR_FAMILIES:
        if key in s:
            return fam
    return ""


def _swap_in(txt, olds, new, what):
    for w in olds:
        if what == "hair":      # the color word that stands in front of the word "hair"
            t2, n = re.subn(rf"\b{re.escape(w)}\b(?=(?:[\s-]+\w+){{0,2}}[\s-]+hair)", new, txt, count=1, flags=re.I)
            if n:
                return t2, True
        t2, n = re.subn(rf"\b{re.escape(w)}\b", new, txt, count=1, flags=re.I)
        if n:
            return t2, True
    return txt, False


def _swap_color(c, old, new, what):
    """Write the new color into the look sheet and the short tag instead of the old one (the exact old words first,
    then any word of the same color family). Only when the color is not written there, a short note is added."""
    fam = color_family(old)
    olds = ([str(old)] if old else []) + [k for k, f in _COLOR_FAMILIES if f == fam and k != str(old).lower()]
    for key in ("look_en", "tag_en"):
        txt = c.get(key) or ""
        if not txt:
            continue
        txt2, done = _swap_in(txt, olds, new, what)
        if not done:
            txt2 = txt.rstrip(". ") + f". The {what} color is {new}."
        c[key] = txt2


FACE_TYPES = (
    "a round face with full cheeks and a small snub nose",
    "a long narrow face with a high forehead and a long straight nose",
    "a square face with a strong jaw and a broad nose",
    "an oval face with high cheekbones and large almond-shaped eyes",
    "a heart-shaped face with a pointed chin and a small pointed nose",
    "an angular face with deep-set eyes, thick eyebrows and a hooked nose",
    "a soft wide face with a full chin and small close-set eyes",
    "a thin face with sharp cheekbones and hollow cheeks",
)
FACE_LIST_TXT = "; ".join(f"{k + 1} = {v}" for k, v in enumerate(FACE_TYPES))


def enforce_distinct_faces(chars, picks):
    """Every character gets a DIFFERENT face type (the program decides, not the robot): the type is written into the look
    sheet, from which the reference portrait is drawn, so no two reference faces start from the same description."""
    used = set()
    for k, c in enumerate(chars):
        if c.get("nonhuman"):
            continue                                   # a shape, an animal... has no human face type
        want = picks.get(k)
        if want is None or not (0 <= want < len(FACE_TYPES)) or want in used:
            order = [(x + _VARIETY["offset"]) % len(FACE_TYPES) for x in range(len(FACE_TYPES))]
            want = next((x for x in order if x not in used), k % len(FACE_TYPES))
        used.add(want)
        c["face_en"] = FACE_TYPES[want]
        c["look_en"] = str(c.get("look_en") or "").rstrip(". ") + f". Face: {FACE_TYPES[want]}."
    return chars


_LAYERS_M = ("a sleeveless vest over the shirt", "a long open coat over the shirt", "suspenders over the rolled-sleeve shirt",
             "a short jacket over the shirt", "a knitted sweater")
_LAYERS_F = ("a shawl over the blouse", "a long cardigan over the blouse", "an apron over the dress",
             "a short jacket over the blouse", "a knitted sweater")


def enforce_distinct_garments(chars):
    """Two characters never wear the same kind of upper garment: the later one gets another layer (vest, coat, shawl...)."""
    seen = set()
    for k, c in enumerate(chars):
        key = re.sub(r"[^a-z ]", "", str(c.get("top_en") or "").lower()).strip()
        if key and key in seen:
            text = (str(c.get("look_en") or "") + " " + str(c.get("tag_en") or "")).lower()
            female = bool(re.search(r"\b(woman|girl|female|lady|mother|daughter|wife|she)\b", text))
            pool = _LAYERS_F if female else _LAYERS_M
            for off in range(len(pool)):
                layer = pool[(k + off) % len(pool)]
                lk = re.sub(r"[^a-z ]", "", layer.lower())
                if lk not in seen:
                    print(f"Cast: {c.get('name')} wore the same kind of garment as another character: now '{layer}'.",
                          flush=True)
                    c["top_en"] = f"{c['top_en']} and {layer}"
                    c["look_en"] = str(c.get("look_en") or "").rstrip(". ") + f". Also wears {layer}."
                    key = re.sub(r"[^a-z ]", "", c["top_en"].lower()).strip()
                    break
        seen.add(key)
    return chars


_NEUTRAL_FAMILIES = ("black", "gray", "brown", "white", "cream")
_VIVID_TOPS = ("red", "mustard yellow", "forest green", "purple", "teal", "orange", "navy blue", "pink")


def enforce_vivid_tops(chars):
    """Free check in code: at most ONE character wears a dull top (black, gray, brown, white, cream); every other one gets
    a clear color, so that the viewer tells the people apart at first sight."""
    seen_dull = False
    for c in chars:
        val = str(c.get("top_color") or "").strip()
        fam = color_family(val)
        if not fam:
            continue
        if fam in _NEUTRAL_FAMILIES:
            if not seen_dull:
                seen_dull = True
                continue
            taken = {color_family(x.get("top_color") or "") for x in chars if x is not c}
            free = [v for v in _VIVID_TOPS if color_family(v) not in taken]
            if free:
                print(f"Cast: {c.get('name')} wore a dull top color '{val}': changed to '{free[0]}' (easier to tell apart).",
                      flush=True)
                _swap_color(c, val, free[0], "shirt")
                c["top_color"] = free[0]
    return chars


_FACIAL = ("clean-shaven", "a neat mustache", "a full beard", "a short stubble beard", "a goatee", "long sideburns")
# the words that are pasted into the pictures: a clean-shaven man must never get a mustache out of nothing
FACIAL_TEXT = {"clean-shaven": "clean-shaven with a bare smooth upper lip and chin"}
_FH_WORDS = re.compile(r"(?:\b(?:thick|bushy|neat|full|long|short|gray|grey|white|black|dark|brown|red|blond|trimmed|small|"
                       r"well-groomed)\s+){0,2}(?:mustache|moustache|beard|goatee|stubble|sideburns|whiskers)\b|\bclean[- ]shaven\b",
                       re.I)
_FEMALE_TXT = re.compile(r"\b(woman|girl|female|lady|mother|daughter|wife|she|queen|princess|maid|nun)\b", re.I)


def _norm_facial(v):
    v = str(v or "").lower()
    if not v.strip():
        return ""
    if re.search(r"clean|shaved|none|no facial|smooth", v):
        return "clean-shaven"
    if "goatee" in v:
        return "a goatee"
    if "stubble" in v:
        return "a short stubble beard"
    if "beard" in v:
        return "a full beard"
    if re.search(r"mustache|moustache", v):
        return "a neat mustache"
    if re.search(r"sideburn|whisker", v):
        return "long sideburns"
    return ""


def set_facial_hair(chars):
    """Free check in code: EVERY man of 16 or older has one stated kind of facial hair (clean-shaven is a state too), different
    from the other men (clean-shaven at most twice), written into his look sheet and his key line, so he is the same in every
    picture and nobody gets a mustache out of nothing. The state of the source wins."""
    used, clean = set(), 0
    off = _VARIETY.get("offset", 0)
    men = []
    for c in chars:
        text = " ".join(str(c.get(k) or "") for k in ("look_en", "tag_en"))
        try:
            age = int(re.findall(r"\d+", str(c.get("age") or ""))[0])
        except (IndexError, ValueError):
            age = 30
        if c.get("nonhuman") or _FEMALE_TXT.search(text) or age < 16:
            c["facial_en"] = ""
            continue
        men.append((c, text))
    for k, (c, text) in enumerate(men):
        fh = _norm_facial(c.get("facial_hair"))
        if not fh:
            m_ = _FH_WORDS.search(text)
            fh = _norm_facial(m_.group(0)) if m_ else ""
        if not fh or (c.get("facial_hair_in_source") is not True and (fh in used and fh != "clean-shaven" or (fh == "clean-shaven" and clean >= 2))):
            for step in range(len(_FACIAL)):
                cand = _FACIAL[(k + off + step) % len(_FACIAL)]
                if (cand not in used and cand != "clean-shaven") or (cand == "clean-shaven" and clean < 2 and not fh):
                    fh = cand
                    break
            else:
                fh = fh or "clean-shaven"
        used.add(fh)
        clean += fh == "clean-shaven"
        c["facial_en"] = fh
        for key in ("look_en", "tag_en"):
            t = _FH_WORDS.sub("", str(c.get(key) or ""))
            for _ in range(4):                                                   # «with a , brown hair» -> «, brown hair»
                t = re.sub(r"\b(?:with|and|a|an|the)\s*(?=[,;.]|$)", "", t)
            t = re.sub(r"[,;]\s*(?=[,;.])", "", t)
            t = re.sub(r"\s+([,;.])", r"\1", t)
            t = re.sub(r"^[,;\s]+", "", t)
            c[key] = " ".join(t.split())
        c["look_en"] = c["look_en"].rstrip(". ") + f". Facial hair: {FACIAL_TEXT.get(fh, fh)}."
    return chars


def enforce_distinct_cast(chars):
    """No two characters share a hair color, a shirt color or a trousers/skirt color (two old people may both have gray
    hair). Fills "key_en": the short who-wears-what line that is pasted into every picture of the person."""
    set_facial_hair(chars)
    humans = [c for c in chars if not c.get("nonhuman")]
    enforce_distinct_garments(humans)
    enforce_vivid_tops(humans)
    used = {"hair_color": set(), "top_color": set(), "bottom_color": set()}
    for c in chars:
        if c.get("nonhuman"):                          # drawn as what it is: its key line is its short look
            c["key_en"] = char_tag(c)
            continue
        try:
            age = int(re.findall(r"\d+", str(c.get("age") or ""))[0])
        except (IndexError, ValueError):
            age = 0
        for field, what, palette in (("hair_color", "hair", HAIR_PALETTE), ("top_color", "shirt", OUTFIT_PALETTE),
                                     ("bottom_color", "trousers or skirt", OUTFIT_PALETTE)):
            val = str(c.get(field) or "").strip()
            fam = color_family(val)
            if not fam:
                continue
            old_ok = field == "hair_color" and age >= 60 and fam in ("gray", "white")
            if fam in used[field] and not old_ok:
                pool = (("gray", "white") + palette) if (field == "hair_color" and age >= 60) else palette
                free = [p for p in pool if color_family(p) not in used[field]]
                if free:
                    print(f"Cast: {c.get('name')} shared the {what} color '{val}': changed to '{free[0]}'.", flush=True)
                    _swap_color(c, val, free[0], what)
                    c[field] = free[0]
                    fam = color_family(free[0])
            used[field].add(fam)
        hair = " ".join(x for x in (c.get("hair_color"), c.get("hair_style")) if x)
        top = " ".join(x for x in (c.get("top_color"), c.get("top_en")) if x)
        bottom = " ".join(x for x in (c.get("bottom_color"), c.get("bottom_en")) if x)
        c["key_en"] = (f"{hair} hair, " if hair else "") + (f"{FACIAL_TEXT.get(c['facial_en'], c['facial_en'])}, " if c.get("facial_en") else "") \
                      + (f"wearing a {top}" if top else "") + \
                      (f" and {bottom}" if (top and bottom) else (f"wearing {bottom}" if bottom else ""))
        c["key_en"] = c["key_en"].strip(" ,")
    return chars


_ACCESSORIES_M = ("a flat cap", "round glasses", "a bow tie", "a long scarf", "a brimmed hat", "a walking cane")
_ACCESSORIES_F = ("a headscarf", "a shawl over the shoulders", "a hair ribbon", "a white apron", "a bonnet",
                  "a necklace with a pendant")


def add_signatures(chars):
    """Every person gets ONE different accessory (hat, glasses, shawl...) that is pasted into all of his or her pictures,
    so even small figures differ at first sight. Not for children; not when the sheet already has the same kind of item."""
    for k, c in enumerate(chars):
        try:
            age = int(re.findall(r"\d+", str(c.get("age") or ""))[0])
        except (IndexError, ValueError):
            age = 30
        if age < 12 or c.get("signature_en") or c.get("nonhuman"):
            continue
        text = (str(c.get("look_en") or "") + " " + str(c.get("tag_en") or "")).lower()
        female = bool(re.search(r"\b(woman|girl|female|lady|mother|daughter|wife|she)\b", text))
        pool = _ACCESSORIES_F if female else _ACCESSORIES_M
        for off in range(len(pool)):
            acc = pool[(k + off + _VARIETY["offset"]) % len(pool)]
            key = acc.split()[-1]
            if k == 0 and re.search(r"\b(cap|hat|bonnet|headscarf)\b", acc):
                continue                          # the main character never gets a hat by default
            if key not in text:
                c["signature_en"] = acc
                c["key_en"] = (str(c.get("key_en") or "") + f", with {acc}").strip(" ,")
                break
    return chars


def char_tag(c):
    """A SHORT face tag (the image model reads only the first ~250 tokens, so long sheets get cut)."""
    t = " ".join(str(c.get("tag_en") or "").split())
    return t or " ".join(str(c.get("look_en") or "").split()[:26])


PLACES_SPEC = """- "locations": the 4 to 10 places where the story really happens (one for EVERY different kind of place in which an
  important event takes place: the jungle, a fence, a laboratory, a road in the storm...), most important first, each {{"id": "short_snake_case",
  "name_en": "...", "detail_en": "90 to 120 English words: the concrete look of this place in that era, in this order:
  kind of building or landscape and its size; walls, floor, ceiling or ground and their materials, colors and state (new,
  worn, cracked, wet, dusty); windows, doors, stairs, lamps or other light sources; furniture, machines, tools and
  other objects with their colors; plants, water, sky and weather; typical light and its color; what is seen far away; small
  signs of life or age (stains, scratches, marks, mist, smoke, reflections); and the 3 or 4 objects or features of this
  place that the EVENTS of the story need (the bed of a death, the door of a flight, the table of a quarrel, the bench of a
  trial...).
  Geographically and historically correct for THIS story; take it from the SOURCE TEXT, and where the source is silent
  choose what surely fits the place and the era (never modern things in an old story)."}}"""


def clean_locations(raw):
    out = []
    for x in raw or []:
        if isinstance(x, dict) and str(x.get("id") or "").strip() and str(x.get("detail_en") or "").strip():
            out.append({"id": re.sub(r"[^a-z0-9_]+", "_", str(x["id"]).strip().lower()).strip("_") or f"place_{len(out)}",
                        "name_en": " ".join(str(x.get("name_en") or x["id"]).split()),
                        "detail_en": " ".join(str(x["detail_en"]).split())})
    return out[:7]


NIGHTWEAR = re.compile(r"\b(pajamas?|pyjamas?|nightgowns?|nightdress(?:es)?|nightshirts?|nightwear|sleepwear|night clothes|"
                       r"nightclothes|nightcaps?|bath ?robes?|dressing gowns?|negligees?)\b", re.I)
DAY_CLOTHES_RULE = "real daytime clothes that show his or her job and the era"
BED_WORDS = re.compile(r"\b(bed|beds|bedroom|sleep|sleeps|sleeping|asleep|slept|wake|wakes|woke|waking|dream|dreams|nightgown|"
                       r"pajamas?|pyjamas?|nightwear|sleepwear|bathroom|bath|hospital|sickbed|deathbed|cot)\b", re.I)


MODERN_ERA = re.compile(r"future|space|modern|science|robot|galaxy|planet|\b(19[3-9]\d|20\d\d)\b|twentieth|21st|20th", re.I)


def fix_nightwear(chars, source_text="", era=""):
    """Free check in code: a character whose look sheet says pajamas / nightgown / bathrobe gets real day clothes instead
    (the reference portrait and every picture copy the look sheet, so one such word dresses the hero in pajamas for the
    whole video). Not changed when the SOURCE itself says that the person lives in such clothes."""
    if NIGHTWEAR.search(str(source_text or "")):
        return chars
    for c in chars:
        if not isinstance(c, dict):
            continue
        text = " ".join(str(c.get(k) or "") for k in ("look_en", "tag_en", "top_en", "bottom_en"))
        if not NIGHTWEAR.search(text):
            continue
        female = bool(re.search(r"\b(woman|girl|female|lady|mother|daughter|wife|she)\b", text.lower()))
        modern = bool(MODERN_ERA.search(str(era or "")))
        if NIGHTWEAR.search(str(c.get("top_en") or "")):
            c["top_en"] = "work jacket over a shirt" if (modern or not female) else "blouse with a fitted jacket"
        if NIGHTWEAR.search(str(c.get("bottom_en") or "")):
            c["bottom_en"] = "trousers" if (modern or not female) else "long skirt"
        for k in ("look_en", "tag_en"):
            v = str(c.get(k) or "")
            if NIGHTWEAR.search(v):
                c[k] = " ".join(NIGHTWEAR.sub("daytime clothes", v).split())
        print(f"Cast: {c.get('name')} was dressed in night clothes: changed to day clothes.", flush=True)
    return chars


def design_places(book, src, era):
    """Only for an OLD saved script that has no places yet (one cheap Gemini call)."""
    prompt = f"""You are a film production designer. Design the places of the novel "{book['title_en']}" by {book['author_en']}
(era and place: {era}) for a picture video.

SOURCE TEXT:
{src['text']}

Return JSON with exactly this key:
{PLACES_SPEC}"""
    try:
        return clean_locations(gemini_json(prompt, minutes=5, models=WRITE_MODELS).get("locations"))
    except (GeminiBlocked, GeminiQuotaExhausted):
        raise
    except Exception as e:  # noqa: BLE001
        print("Place design skipped:", e, flush=True)
        return []


_EYE_WORDS = re.compile(r"(?:\b(?:black|dark|brown|blue|green|hazel|gray|grey|amber|violet|light|pale|deep|bright|honey)[ -]?){1,3}eyes?\b",
                        re.I)
_HEADWEAR = re.compile(r"(?:,\s*|\s+)(?:and\s+|with\s+)?(?:an?\s+)?(?:[\w-]+\s+){0,2}(?:hat|cap|bonnet|beret|fedora|bowler|headscarf)\b", re.I)
_HEAD_JOBS = re.compile(r"soldier|sailor|\bnun\b|judge|officer|guard|priest|knight|captain|police|general|pilot|king|queen|"
                        r"emperor|chef|cook|nurse|admiral|cowboy|monk|bishop", re.I)
_TOP_ROTATION = ("deep red", "navy blue", "mustard yellow", "purple", "teal", "burnt orange", "olive green", "burgundy",
                 "sky blue", "forest green")
_DEFAULT_EYES = ("brown", "blue", "green", "hazel", "gray", "amber")


_HUMAN_SPECIES = re.compile(r"^(human|person|people|man|woman|boy|girl|child|lady|gentleman|a human|a man|a woman|a boy|a girl)\b", re.I)


def vary_free_traits(chars, natural_hair, natural_eyes):
    """Free check in code. What the SOURCE does not say about a character (hair color, eye color, color of the outfit) is
    decided by the program, round robin over the videos (the number of videos already posted), inside what is natural for
    the people of that place and time. So the main characters of two videos never share hair, eyes and coat color, and
    nobody gets black eyes only because the image model does that when nothing is written."""
    off = _VARIETY["offset"]
    hair_list = [str(x).strip().lower() for x in (natural_hair or []) if str(x).strip()]
    eye_list = [str(x).strip().lower() for x in (natural_eyes or []) if str(x).strip()] or list(_DEFAULT_EYES)
    used_eyes = set()
    for k, c in enumerate(chars):
        if c.get("nonhuman"):
            continue
        if c.get("hair_in_source") is not True and hair_list and c.get("hair_color"):
            pick = hair_list[(k + off) % len(hair_list)]
            if color_family(pick) and color_family(pick) != color_family(c["hair_color"]):
                print(f"Cast: {c.get('name')} hair '{c['hair_color']}' is not in the source: set to '{pick}' (variety).", flush=True)
                _swap_color(c, c["hair_color"], pick, "hair")
                c["hair_color"] = pick
        eye = str(c.get("eye_color") or "").strip().lower() if c.get("eyes_in_source") is True else ""
        if not eye:
            eye = eye_list[(k * 2 + off) % len(eye_list)]
            for step in range(len(eye_list)):
                if eye_list[(k * 2 + off + step) % len(eye_list)] not in used_eyes:
                    eye = eye_list[(k * 2 + off + step) % len(eye_list)]
                    break
        used_eyes.add(eye)
        c["eye_color"] = eye
        for key in ("look_en", "tag_en"):
            t = _EYE_WORDS.sub("", str(c.get(key) or ""))
            if k == 0 and not _HEAD_JOBS.search(t):          # the main character wears no default hat or cap
                t = _HEADWEAR.sub("", t)
            t = re.sub(r"[,;]\s*(?=[,;.])", "", t)
            t = re.sub(r"\s+([,;.])", r"\1", t)
            c[key] = " ".join(t.split())
        c["look_en"] = c["look_en"].rstrip(". ") + f". Eyes: {eye} eyes; eyebrows the color of the hair."
        if (c.get("outfit_color_in_source") is not True and c.get("top_color")
                and not _HEAD_JOBS.search(str(c.get("look_en") or "") + " " + str(c.get("tag_en") or ""))):
            options = [x for x in _TOP_ROTATION[(k * 3 + off) % len(_TOP_ROTATION):] + _TOP_ROTATION[:(k * 3 + off) % len(_TOP_ROTATION)]
                       if not (k == 0 and color_family(x) == "green")]
            pick = options[0]
            if color_family(pick) != color_family(c["top_color"]) or k == 0:
                print(f"Cast: {c.get('name')} outfit color '{c['top_color']}' is not in the source: set to '{pick}' (variety).",
                      flush=True)
                _swap_color(c, c["top_color"], pick, "shirt")
                c["top_color"] = pick
    return chars


def apply_age_gaps(chars, relations):
    """A pair whose age difference matters in the story (a wife much older than her husband...) really gets that difference in
    code: the older one is aged up and described with the signs of that age, so the two never look the same age."""
    byname = {str(c.get("name") or "").strip().lower(): c for c in chars if isinstance(c, dict)}
    for r in relations or []:
        if not isinstance(r, dict):
            continue
        old = byname.get(str(r.get("older") or "").strip().lower())
        young = byname.get(str(r.get("younger") or "").strip().lower())
        try:
            gap = int(float(str(r.get("gap_years")).strip()))
        except (TypeError, ValueError):
            continue
        if not old or not young or old is young or gap < 10 or old.get("nonhuman") or young.get("nonhuman"):
            continue
        try:
            ya, yb = int(float(str(old.get("age") or 0))), int(float(str(young.get("age") or 0)))
        except (TypeError, ValueError):
            continue
        if ya and yb and ya - yb >= gap:
            continue                                   # the sheets already show the gap
        base = yb or max(ya - gap, 18)
        new_age = min(max(base + gap, ya), 90)
        if new_age >= 65:
            sign, hair = "an old person with white hair, deep wrinkles and a slightly stooped back", "white"
        elif new_age >= 50:
            sign, hair = "a clearly older person with gray hair, wrinkles around the eyes and mouth and a mature body", "gray"
        else:
            sign, hair = "a mature person with a few gray streaks in the hair and fine lines around the eyes", old.get("hair_color") or ""
        old["age"] = new_age
        old["look_en"] = f"Aged about {new_age} years, {sign}; " + str(old.get("look_en") or "")
        old["tag_en"] = f"aged about {new_age}, {sign}; " + str(old.get("tag_en") or "")
        if hair:
            old["hair_color"] = hair
        old["age_fixed"] = True
        print(f"Age gap: {old['name']} (about {new_age}) is {gap} years older than {young['name']} (about {yb or '?'}).", flush=True)
    return chars


def design_cast(book, src):
    """Step B0: BEFORE the script is written, design the era and ONE fixed look sheet for every important character
    (ages that fit the family, very different faces and bodies, different clothes, true to the era)."""
    set_variety(book)
    prompt = f"""You are a film casting director and a costume designer. Design the characters of a picture video about
the novel "{book['title_en']}" by {book['author_en']}.

Who the characters are comes ONLY from the SOURCE TEXT. What the source does not say, you decide so that it fits the
era, the place and the story.

SOURCE TEXT:
{src['text']}

Return JSON with exactly these keys:
{PLACES_SPEC}
- "era_en": the time and place of the story in one short English phrase (for example "provincial Russia, 1860s").
- "natural_hair_colors": 3 to 5 plain color words (black, dark brown, brown, light brown, blond, copper red, gray) that are
  natural for the people of THIS place and time. "natural_eye_colors": 3 to 5 plain color words (brown, hazel, green, blue,
  gray, amber) that are natural for them.
- "costume_en": 20 to 35 English words: what ordinary people of THAT time, place and social classes really wore
  (garments, fabrics, hats, shoes): daytime and working clothes, never nightwear; for a future, space or invented world: what
  its people wear at work. Historically correct: nothing modern in an old story.
- "characters": the important characters, at most 6, most important first, each {{"name": "...", "look_en": "...", "tag_en": "...",
  "age": 30, "face_type": 3, "hair_color": "...", "hair_style": "...", "top_en": "...", "top_color": "...", "bottom_en": "...", "bottom_color": "...",
  "eye_color": "...", "hair_in_source": false, "eyes_in_source": false, "outfit_color_in_source": false,
  "species_en": "human", "species_note_en": "", "facial_hair": "clean-shaven", "facial_hair_in_source": false}}.
  CREATURES: an important non-human being that appears in several events of the story (a dinosaur species, a monster, a
  dragon, a wolf pack, a ship, a machine) is cast as a character too (use the free places of the cast), with "species_en" and
  a look sheet, so that it looks the same in every picture and is drawn whenever the story speaks of it.
  SPECIES CHECK (do it BEFORE the look sheets): decide what every character REALLY is from the whole source, never from the
  name or the title read literally (a name can be a pun, a pen name or a double meaning). "species_en" is "human" for a
  person, otherwise exactly what the character is (for example "a flat geometric square", "a rabbit", "a robot", "a
  talking teapot"), and "species_note_en" is one sentence about a double meaning or a literal-translation trap, or "". A
  character that is NOT human is drawn as what it is, never as a person: its "look_en" (25 to 60 English words) describes its
  body (the shape and size, colors, markings, how many parts it has, eyes and mouth if it has them, how it moves and ONE
  distinctive feature), "tag_en" is a short version, and "face_type" 0, "age" 0 and every hair, top, bottom and eye field "".
  The three booleans are true ONLY when the SOURCE TEXT itself states this person's hair color, eye color, or the color of
  his or her clothes (otherwise false: the program then varies them). Headwear (hat, cap, bonnet, headscarf) is written ONLY
  when the job, the weather or the custom of the era requires it (a soldier, a sailor, a nun, a judge); never as a default
  accessory.
  "name" is the name in English letters: ONLY the FIRST name (or the shortest known name; no middle name, patronymic,
  family name or title), written the same way in every text. "look_en" is ONE look sheet of 55 to 75 English words in
  this fixed order: gender and exact age in years; body build; skin tone; eyes (color and shape);
  eyebrows; mouth and lips; hair (color, length, style); facial hair; ONE distinctive feature (scar, mole,
  freckles, glasses...); the DAY OUTFIT with colors (see the OUTFIT rule below). It is pasted into EVERY picture of this
  character and never changes, so describe the character in the MAIN period of the story. Only appearance: no mood,
  no action, no names of actors. "tag_en" is the SHORT version of the same look: 14 to 20 English words with only what
  makes this person recognizable at first sight (gender and age, hair color and style, ONE distinctive feature, build,
  main outfit color). It never contradicts "look_en".
  FACE: "face_type" is the NUMBER of the face from this list that fits the character's age and gender: {FACE_LIST_TXT}.
  Every character gets a DIFFERENT number. Do NOT write the face shape, cheeks, jaw or nose in "look_en" or "tag_en": the
  program adds the face from your number.
  The extra keys say the same thing in one word or two: "age" is a number of years; "hair_color" (one plain color word:
  black, blond, copper red, brown, gray or white), "hair_style" (for example "short curly", "long braided"), "top_en" (the
  upper garment, for example "linen shirt"), "top_color", "bottom_en" (trousers, skirt or long dress) and "bottom_color"
  (one plain color word each). They must agree with "look_en".

- "relations": a list (empty when there is none) of {{"older": "name", "younger": "name", "kind": "spouse", "gap_years": 25}} for EVERY
  pair of characters whose AGE DIFFERENCE matters in the story: a husband and wife or lovers where the source says that one is much
  older or younger than the other, a guardian and a ward, a teacher and a student. "older" and "younger" are names exactly as in
  "characters"; "gap_years" is the number of years between them (at least 10; the number from the source, otherwise your best fit).

Rules:
- AGE GAP (very important): when the source says (or the story needs) that a spouse or lover is MUCH older or younger than the
  other (for example a wife who is much older than her husband), the two ages differ by that gap and BOTH look sheets show it
  plainly: the older one with gray or white hair, wrinkles and an older body, the younger one with a young face and body. Two people
  who look the same age are never allowed in that case.
- AGES: use the ages of the source; otherwise decide them so they fit the story and the family tree. A parent is at least
  18 years older than his or her child, grandparents are older than parents, a teenager looks like a teenager and an old
  person looks old. The age must FIT THE ROLE: a person who marries, is engaged, is a bride or a groom, has a love story,
  has children or works at an adult job is drawn as a grown person of at least 18 years (when the source gives a younger
  age, choose 18 to 20 for the look sheet); a child is never a bride, a groom or a lover, and never wears a wedding dress.
  A child looks like a child and a grown person never looks like a child.
- FACES: every character has a clearly different face: a different face shape, hair color and style, eye color and
  distinctive feature. Skin tone fits the place and the people of the story. Nobody looks like a twin of somebody else
  unless the source says they are twins.
- BODIES: every character gets a DIFFERENT build (for example thin, heavy, muscular, short, very tall, frail,
  broad-shouldered, round). A build is never copied from the main character unless the source says so; at most ONE
  character is heavy unless the source says that more are.
- CLOTHES: every character wears a different outfit with different main colors, true to his or her job, class, age and
  the era, and historically correct for that time and place. STRICT: no two characters have the same "hair_color", the same
  "top_color" or the same "bottom_color" (only two old people may both have gray hair), and the garments ("top_en",
  "bottom_en") differ in cut too (one in a long coat, one in a waistcoat, one in a shawl and a long skirt...). Choose
  colors that are far apart (red / navy blue / green / yellow / purple / white / black), never two shades of one color.
  A mother and her daughter, or two brothers, also differ in hair style and in clothes colors.
- CLOTHES FIT THE JOB: the outfit shows the real job and class of the person (a merchant in a merchant's coat or
  waistcoat, a farmer in work clothes, a priest in a cassock, a nurse in a nurse's dress); uniforms and armor ONLY for
  soldiers, officers or guards. Say the job at the start of "look_en".
- ATTRACTIVE CAST: every main character is good-looking in the way of an animated feature film: a harmonious, appealing face
  with expressive eyes, well-groomed hair, and a fit or graceful figure that suits the age; write the build in "look_en"
  (slim, athletic, graceful, broad-shouldered...). Keep what the source says (old, ill, ugly, fat, scarred) only when the story
  needs it.
- FACIAL HAIR: every man of 16 or older has "facial_hair": exactly one of "clean-shaven", "a neat mustache", "a full beard",
  "a short stubble beard", "a goatee" or "long sideburns"; "facial_hair_in_source" is true only when the SOURCE states it. Women
  and children: "".
- OUTFIT = DAY CLOTHES (very important): "look_en", "top_en" and "bottom_en" describe what the person wears while ACTING in the
  main part of the story: real day clothes or work clothes of that era, place and job (in a story set in the future, in space
  or in an invented world: what the people there wear for their work, for example a crew uniform, a work jumpsuit, a lab
  coat, a protective suit, a long coat). NEVER nightwear: no pajamas, nightgown, nightshirt, bathrobe or dressing gown, and no
  plain loose pale cotton set that looks like pajamas, unless the source clearly says that this very person stays in bed or in
  night clothes for the whole story. A person who is in bed in one scene still has day clothes as the usual outfit. Give every
  outfit a clear cut (collar, belt, buttons, pockets, boots or shoes), a texture and a strong color, so that it can never be
  mistaken for sleepwear.
- Never contradict the source (gender, age, hair, body, job, family relations).
""" + diversity_text() + "\n"
    try:
        d = gemini_json(prompt, minutes=8, models=WRITE_MODELS)
        locations = clean_locations(d.get("locations"))
        era = " ".join(str(d.get("era_en") or "").split())
        costume = " ".join(str(d.get("costume_en") or "").split())
        chars, picks = [], {}
        for c in d.get("characters") or []:
            if isinstance(c, dict) and str(c.get("name") or "").strip() and str(c.get("look_en") or "").strip():
                try:
                    picks[len(chars)] = int(str(c.get("face_type")).strip()) - 1
                except (TypeError, ValueError):
                    pass
                chars.append({"name": str(c["name"]).strip(), "look_en": " ".join(str(c["look_en"]).split()),
                              "tag_en": " ".join(str(c.get("tag_en") or "").split()),
                              **{k: (c.get(k) if k == "age" else " ".join(str(c.get(k) or "").split()))
                                 for k in ("age", "hair_color", "hair_style", "top_en", "top_color", "bottom_en",
                                           "bottom_color", "eye_color")},
                              "hair_in_source": c.get("hair_in_source") is True,
                              "eyes_in_source": c.get("eyes_in_source") is True,
                              "outfit_color_in_source": c.get("outfit_color_in_source") is True,
                              "species_en": " ".join(str(c.get("species_en") or "human").split()),
                              "species_note_en": " ".join(str(c.get("species_note_en") or "").split()),
                              "facial_hair": " ".join(str(c.get("facial_hair") or "").split()),
                              "facial_hair_in_source": c.get("facial_hair_in_source") is True,
                              "nonhuman": not _HUMAN_SPECIES.match(" ".join(str(c.get("species_en") or "human").split()) or "human")})
        chars = vary_free_traits(chars[:6], d.get("natural_hair_colors"), d.get("natural_eye_colors"))
        chars = apply_age_gaps(chars, d.get("relations"))
        chars = fix_nightwear(chars, src["text"], era)
        chars = add_signatures(enforce_distinct_cast(enforce_distinct_faces(chars, picks)))
        if not era:
            return None
        print(f"Cast designed: {len(chars)} character(s), era: {era}", flush=True)
        return {"era_en": era + (f". Clothing of that time: {costume}" if costume else ""), "characters": chars[:6],
                "locations": locations}
    except (GeminiBlocked, GeminiQuotaExhausted):
        raise
    except Exception as e:  # noqa: BLE001
        print("Cast design skipped:", e, flush=True)
        return None


def cast_prompt(cast):
    """The fixed cast, pasted into the script prompt."""
    if not cast or not cast.get("characters"):
        return ""
    sheets = json.dumps(cast["characters"], ensure_ascii=False)
    return ("\nCHARACTERS (already designed: faces, ages, bodies and clothes are FIXED, do not change them):\n"
            f"{sheets}\nERA AND PLACE: {cast['era_en']}\n"
            "In your JSON copy these sheets unchanged into the key \"characters\". In \"characters_present\" and in the "
            "\"outline\" use ONLY these names. The video must introduce these heroes to the viewer early, by name, with "
            "their relation to each other and what each of them wants.\n")


# ---------------------------------------------------------------- story spine
def plan_events(src):
    """How many key events the story spine may have: about one per 60 words of the Plot, 8 to 14 (a longer video tells more)."""
    return max(8, min(14, round(src["en_words"] / 60)))


def repair_with_timeline(events, timeline, lo):
    """Free check in code (no Gemini call): (1) every timeline point of weight 5 that no event covers is added as an event at
    its place in time (an important event can no longer be missing); (2) an event made only of weight 1 or 2 points is
    removed while there are more events than the minimum."""
    if not timeline or not events:
        return events
    weight = {t["n"]: t["weight"] for t in timeline}
    added = 0
    for t in sorted(timeline, key=lambda x: x["n"]):
        if t["weight"] < 5 or any(t["n"] in e.get("covers", []) for e in events):
            continue
        at = 0
        for k, e in enumerate(events):
            if e.get("covers") and min(e["covers"]) < t["n"]:
                at = k + 1
        events.insert(at, {"id": 0, "who_en": "", "what_en": t["what_en"], "where_en": "", "because_en": "",
                           "role_en": "turning point", "quote_en": "", "flashback": False, "covers": [t["n"]]})
        added += 1
    removed = 0
    for e in list(events):
        cv = [weight[n] for n in e.get("covers", []) if n in weight]
        if cv and max(cv) <= 2 and len(events) > max(lo, 6):
            events.remove(e)
            removed += 1
    for n, e in enumerate(events, 1):
        e["id"] = n
    if added or removed:
        print(f"Story spine check: {added} important event(s) added from the timeline, {removed} minor event(s) removed.",
              flush=True)
    return events


def clean_spine(d, lo):
    """A usable story spine or None. Events are numbered 1..n again, in the order they were given."""
    if not isinstance(d, dict):
        return None
    events = []
    for x in d.get("events") or []:
        if isinstance(x, dict) and str(x.get("what_en") or "").strip():
            events.append({"id": len(events) + 1,
                           "who_en": " ".join(str(x.get("who_en") or "").split()),
                           "what_en": " ".join(str(x["what_en"]).split()),
                           "where_en": " ".join(str(x.get("where_en") or "").split()),
                           "because_en": " ".join(str(x.get("because_en") or "").split()),
                           "role_en": " ".join(str(x.get("role_en") or "").split()),
                           "quote_en": " ".join(str(x.get("quote_en") or "").split())[:240],
                           "flashback": bool(x.get("flashback"))})
    if len(events) < max(5, lo - 1):
        return None
    heroes = []
    for h in d.get("heroes") or []:
        if isinstance(h, dict) and str(h.get("name") or "").strip():
            heroes.append({"name": str(h["name"]).strip(), "role_en": " ".join(str(h.get("role_en") or "").split())})
    timeline = []
    for t in d.get("timeline") or []:
        try:
            if isinstance(t, dict) and str(t.get("what_en") or "").strip():
                timeline.append({"n": int(str(t.get("n")).strip()), "what_en": " ".join(str(t["what_en"]).split())[:160],
                                 "weight": max(1, min(5, int(str(t.get("weight") or 3).strip()[:1])))})
        except (TypeError, ValueError):
            continue
    covers = {}
    for x, e in zip([x for x in (d.get("events") or []) if isinstance(x, dict) and str(x.get("what_en") or "").strip()], events):
        cv = []
        for v in (x.get("covers") or []):
            try:
                cv.append(int(str(v).strip()))
            except (TypeError, ValueError):
                pass
        e["covers"] = cv
    events = repair_with_timeline(events, timeline, lo)
    return {"logline": " ".join(str(d.get("logline") or "").split()), "heroes": heroes[:4], "events": events[:14],
            "world_en": " ".join(str(d.get("world_en") or "").split()),
            "subjects": [{"en": " ".join(str(x.get("en") or "").split())[:200],
                          "fa": [DIAC.sub("", str(w)).strip() for w in (x.get("fa") or []) if str(w).strip()][:4]}
                         for x in (d.get("subjects") or []) if isinstance(x, dict) and str(x.get("en") or "").strip()
                         and x.get("fa")][:6],
            "title_fa": " ".join(str(d.get("title_fa") or "").split()),
            "author_fa": " ".join(str(d.get("author_fa") or "").split()),
            "ending_en": " ".join(str(d.get("ending_en") or "").split()),
            "idea_en": " ".join(str(d.get("idea_en") or "").split()),
            "author_en": " ".join(str(d.get("author_en") or "").split())}


def _norm_en(t):
    return re.sub(r"[^a-z0-9 ]+", "", str(t).lower().replace("-", " "))


def order_events_by_source(events, text):
    """Free check in code (no Gemini call): the position of every event's source quote inside the source text. When the
    story is told in the order of the source (no flashback) and an event stands before an earlier one, the events are
    sorted back into the order of the source. Events whose quote is not found keep their place."""
    src_n = _norm_en(text)
    pos = []
    for e in events:
        q = _norm_en(e.get("quote_en") or "")
        words = q.split()
        at = -1
        for n in (len(words), 8, 5):          # the whole quote, then its first 8 / 5 words
            if n and len(words) >= n and at < 0:
                at = src_n.find(" ".join(words[:n]))
        pos.append(at)
    if any(e.get("flashback") for e in events) or sum(1 for x in pos if x >= 0) < max(4, len(events) - 2):
        return events, 0
    known = [(x, k) for k, x in enumerate(pos) if x >= 0 and 0 < k < len(events) - 1]   # first = start, last = ending: fixed
    if len(known) < 2:
        return events, 0
    order = [k for _, k in sorted(known)]
    moved = sum(1 for a, b in zip(order, sorted(order)) if a != b)
    if not moved:
        return events, 0
    slots = sorted(k for _, k in known)
    out = list(events)
    for slot, k in zip(slots, order):
        out[slot] = events[k]
    for n, e in enumerate(out, 1):
        e["id"] = n
    return out, moved


def build_spine(book, src, cast):
    """Step B1 (ONE Gemini call, English): BEFORE any Persian is written, decide WHAT the viewer must be told: the
    heroes and the few main events in order, each joined to the one before by its cause. The narration is then written
    event by event, so a sentence that serves no event cannot enter the video."""
    n = plan_events(src)
    lo, hi = max(6, n - 1), n + 1
    names = [c["name"] for c in (cast or {}).get("characters", [])]
    names_txt = (f"\nCHARACTER NAMES to use (exactly as written): {json.dumps(names, ensure_ascii=False)}\n"
                 if names else "")
    prompt = f"""You are a story editor. Prepare the SUMMARY SPINE of the novel "{book['title_en']}" by {book['author_en']}:
the few things that a viewer who NEVER read the book must be told, in order, to understand the WHOLE story.

SOURCE TEXT (the only truth; use nothing else). It may hold several summaries of the same novel (English PLOT first, then
summaries from other-language Wikipedias, then CHARACTERS): tell every event ONCE; when they disagree the English PLOT wins;
use the other summaries to confirm an event, to fill a gap that the English one skips and to understand the real meaning of
a term. Names always as in the English text.
{src['text']}
{names_txt}
Return JSON with exactly these keys:
- "logline": ONE English sentence: who the hero is, what he or she wants and what stands in the way, AT THE START of the story
  (NEVER an outcome, a death, a result or how it ends: the logline opens the video and must not spoil anything).
- "heroes": 2 to 4 objects {{"name": "...", "role_en": "..."}}: only the people without whom the story cannot be told.
  "name" is ONLY the FIRST name (or the shortest known name) of the person; "role_en" 6 to 14 English words that introduce this person to a viewer (age or job,
  relation to the other heroes, what he or she wants), only from the source. Say what the hero REALLY is (a person, an
  animal, a shape, a machine, a creature), judged from the whole source and not from the name read literally.
- "subjects": 0 to 6 objects {{"en": "10 to 20 English words: how this thing looks", "fa": ["1 to 4 Persian words that a narrator
  would use for it"]}}: the creatures, animals, monsters, vehicles, machines, cages, fences and other THINGS (not people) that
  are central to the events (for a story about dinosaurs: the dinosaur kinds; for a story about a ship: the ship); [] when
  the story has none.
- "title_fa" and "author_fa": the Persian title commonly used in Iran for this book (when there is no common one, translate the
  MEANING of the English title word by word, never invent a word and never guess by sound) and the Persian name of the author.
- "world_en": ONE English sentence, or "" : when the heroes are NOT ordinary people of our world (geometric shapes, animals,
  objects, machines, fantasy or science-fiction beings) or the world works by rules the viewer does not know, say plainly what
  the heroes are and how their world works, in simple words; "" for an ordinary story.
- "timeline": EVERY plot point of the source in order, 12 to 30 objects {{"n": 1, "what_en": "at most 14 words", "weight": 5}}.
  "weight" says how much the story needs this point: 5 = without it the main conflict or the ending cannot be understood (the
  event that starts the conflict, every turning point, the climax, the ending, every event that changes what the hero has,
  knows, wants or is); 4 = a major cause or a major result of a weight-5 point; 3 = helps to understand; 2 = a side episode, a
  side person, a journey, a repeated episode; 1 = a detail, a description, a mood.
- "events": {lo} to {hi} objects, in the order of the story, each {{"id": 1, "who_en": "...", "what_en": "...",
  "where_en": "...", "because_en": "...", "covers": [1, 2]}}. The events are BUILT FROM THE TIMELINE: "covers" lists the "n" of
  the timeline points that the event tells (one event may merge several neighbouring points). EVERY timeline point with weight
  5 or 4 is covered by an event, so that nothing important is missing; a point with weight 1 or 2 is covered only when it is
  the cause of a heavier point (then merge it into that event); events are never made of weight 1 or 2 points alone.
    "who_en": the people who act (with a few words of role for anyone who is not a hero);
    "what_en": ONE English sentence: what REALLY happens, in plain literal facts that a viewer could picture (a concrete
      event, not a description). When the source says it with a metaphor, a personification, a euphemism or a technical
      word (a ship that «kills itself», a city that «dies»), write the real event behind it (the ship destroys itself on
      purpose), judged from the whole story;
    "where_en": the place (and the time if it changes);
    "because_en": what in the PREVIOUS event caused this one ("" for event 1).
  Every event object also has: "role_en" (exactly one of: "setup", "inciting incident", "turning point", "complication",
  "crisis", "climax", "resolution": the job of this event in the story), "quote_en" (the English sentence of the SOURCE TEXT
  that this event comes from, copied word for word, at most 30 words) and "flashback" (true only if the event happened BEFORE
  the previous event in time; otherwise false).
  Rules for the events:
  * ORDER (very important): the events are in the order in which they HAPPEN in the story's time, from the beginning to the
    true ending, even if the book or the source starts with a prologue, a frame story, a scene from the end or a flashback:
    such a part is moved to its true place in time (a flashback that must stay is marked "flashback": true and its "what_en"
    starts with the time: «years earlier», «as a child»...). The LAST event is the real ending: nothing that happens after
    the climax comes before it, and no earlier event contains the final outcome. Never move an event before the event that
    causes it.
  * IMPORTANCE: choose ONLY the events that carry the story: the situation, the event that starts the conflict, every turning
    point, the climax and the ending. If the source has more events than {hi}, drop the least important ones (a side
    character, a journey, a repeated episode, a detail) and fold their cause into the next event's "because_en". A viewer must
    understand the whole story from these events alone, with no gap in the chain of cause and effect.
  * Event 1 is the situation at the start. The last event is the ending (how the story ends for the hero).
  * REMOVAL TEST: an event is listed only if removing it would break the chain of cause and effect, or would hide a turning
    point, the climax or the change of the hero. Episodes, side characters, trips, descriptions and details are NOT events.
  * COMPLETE FIRST: the list of events must let a viewer understand the WHOLE story, with every important event told (all
    weight 5 and 4 points). When there are more heavy points than {hi} events, merge neighbouring points into one event; do
    NOT throw a heavy point away. Only after that, if there is still room, add weight 3 points; never weight 1 or 2 points.
  * Every event follows from the one before it (a real cause, not just \"then\"). Together they tell the whole story from
    the beginning to the end, with nothing important missing.
  * Only facts of the PLOT in the source text.
- "ending_en": ONE sentence: how the story ends and what has changed.
- "idea_en": the MESSAGE of the novel (its central idea, what the author wants the reader to understand) in ONE English
  sentence, taken ONLY from the INTRODUCTION, THEMES or SIGNIFICANCE part of the source (\"\" if the source has none).
- "author_en": ONE English sentence: who wrote the novel and when it was published, ONLY from the INTRODUCTION (\"\" if the
  source does not say)."""
    d = gemini_json(prompt, minutes=8, models=WRITE_MODELS)
    sp = clean_spine(d, lo)
    if sp:
        sp["events"], moved = order_events_by_source(sp["events"], src["text"])
        if moved:
            print(f"Story spine: {moved} event(s) were put back into the order of the source.", flush=True)
        print(f"Story spine: {len(sp['events'])} main events, {len(sp['heroes'])} heroes. Logline: {sp['logline'][:110]}",
              flush=True)
    return sp


PARTS = ("open", "heroes", "story", "wrap")


def _norm_wrap(t):
    return t.replace("\u200c", "").replace(" ", "")


def tagged_to_scenes(raw, n_ev, strict=True, max_words=None, open_text=""):
    """The writer tags EVERY sentence with the story event it tells (or with its part). The program joins the
    sentences of a scene into the narration and DROPS every sentence whose tag is not allowed for its part, so that a
    sentence that serves no event never reaches the video. Returns (scenes, problems)."""
    problems, scenes, covered, last_ev, dropped = [], [], set(), 0, 0

    def need_of(x):
        try:
            return max(1, min(3, int(str(x.get("need") if x.get("need") is not None else 2).strip()[:1] or 2)))
        except (TypeError, ValueError):
            return 2

    support, dropped_need = {}, 0         # event number -> how many sentences with NEED 2 or 3 tell it
    for s in raw or []:
        if isinstance(s, dict) and str(s.get("part") or "").strip().lower() == "story":
            for x in s.get("sentences") or []:
                if isinstance(x, dict):
                    t = str(x.get("ev") if x.get("ev") is not None else "").strip()
                    if t.isdigit() and need_of(x) >= 2:
                        support[int(t)] = support.get(int(t), 0) + 1
    over = set()                                   # ids of NEED 2 sentences removed because the narration is too long
    if max_words:
        def ev_num(x):
            t = str(x.get("ev") if x.get("ev") is not None else "").strip()
            return int(t) if t.isdigit() else None

        total_w, cands = 0, []
        for s in raw or []:
            if not isinstance(s, dict):
                continue
            is_story = str(s.get("part") or "").strip().lower() == "story"
            for x in s.get("sentences") or []:
                if not isinstance(x, dict):
                    continue
                nw, e_ = len(str(x.get("fa") or "").split()), ev_num(x)
                if is_story and need_of(x) == 1 and e_ is not None and support.get(e_, 0) >= 1:
                    continue                       # already removed as decoration
                total_w += nw
                if is_story and need_of(x) == 2 and e_ is not None:
                    cands.append((nw, e_, id(x)))
        for nw, e_, xid in sorted(cands, reverse=True):
            if total_w <= max_words:
                break
            if support.get(e_, 0) >= 2:            # the event keeps at least one other sentence with NEED 2 or 3
                support[e_] -= 1
                total_w -= nw
                over.add(xid)
    for s in raw or []:
        if not isinstance(s, dict):
            continue
        part = str(s.get("part") or "").strip().lower()
        if part not in PARTS:
            problems.append(f"unknown part '{part}'")
            continue
        keep, ev_ids = [], []
        for x in s.get("sentences") or []:
            if not isinstance(x, dict):
                dropped += 1
                continue
            fa = " ".join(str(x.get("fa") or "").replace("ي", "ی").replace("ك", "ک").split())
            tag = str(x.get("ev") if x.get("ev") is not None else "").strip().lower()
            if not fa:
                continue
            num = int(tag) if tag.isdigit() and 1 <= int(tag) <= n_ev else None
            if part in ("open", "heroes"):
                ok = tag == "intro"
            elif part == "wrap":
                ok = tag == "wrap" or num is not None
            else:
                ok = num is not None
                if ok and need_of(x) == 1 and support.get(num, 0) >= 1:
                    dropped_need += 1             # decoration: its event is already told by other sentences
                    continue
                if ok and id(x) in over:
                    dropped_need += 1             # an explanation that the length of the video cannot afford
                    continue
                if ok and num < last_ev:
                    if not strict:                # last attempt: a sentence that jumps back is deleted, not kept
                        dropped += 1
                        continue
                    problems.append(f"event {num} is told after event {last_ev}")
            if not ok:
                dropped += 1
                continue
            if part == "story":
                last_ev = max(last_ev, num)
                covered.add(num)
            if num is not None:
                ev_ids.append(num)
            keep.append(fa)
        if not keep:
            continue
        scenes.append({"part": part, "narration_fa": " ".join(keep), "ev_ids": sorted(set(ev_ids)),
                       "source_en": str(s.get("source_en") or ""),
                       "characters_present": [str(n) for n in (s.get("characters_present") or [])],
                       "setting_en": str(s.get("setting_en") or ""), "action_en": str(s.get("action_en") or ""),
                       "mood_en": str(s.get("mood_en") or "")})
    if open_text and scenes and not any(x["part"] == "open" for x in scenes):
        scenes.insert(0, {"part": "open", "narration_fa": open_text, "ev_ids": [], "source_en": "", "characters_present": [],
                          "setting_en": "", "action_en": "", "mood_en": ""})
        print("Script check: the opening scene was missing: a plain one was added.", flush=True)
    scenes.sort(key=lambda x: {"open": 0, "heroes": 1, "story": 2, "wrap": 3}[x["part"]])     # a misplaced part goes to its place
    seq = "".join({"open": "o", "heroes": "h", "story": "s", "wrap": "w"}[x["part"]] for x in scenes)
    if not re.fullmatch(r"oh?s{3,}w{1,3}", seq):
        problems.append(f"the order of the parts is '{seq}' (needed: open, heroes, story..., wrap...)")
    missing = [k for k in range(1, n_ev + 1) if k not in covered]
    if missing:
        problems.append(f"events never told: {missing}")
    wraps = [x for x in scenes if x["part"] == "wrap"]
    if wraps and "جمعبندی" not in _norm_wrap(wraps[0]["narration_fa"]):
        problems.append("the first wrap-up scene does not contain «جمع‌بندی»")
    if dropped:
        print(f"Script check: {dropped} sentence(s) without a valid event tag were removed.", flush=True)
    if dropped_need:
        print(f"Script check: {dropped_need} decorative sentence(s) (need 1) were removed.", flush=True)
    if not strict:                    # last attempt: only a broken structure is refused, the rest is repaired by the gate
        problems = [p for p in problems if "order of the parts" in p]
    return scenes, problems


def write_scenes(book, src, plan):
    """Step B: first the SPINE (what must be told), then the narration written sentence by sentence from the spine."""
    min_sc, max_sc, words = plan
    if "author_gender" not in book:
        book["author_gender"] = author_gender(book)
    cast = design_cast(book, src)
    cast_block = cast_prompt(cast)
    spine = None
    for _ in range(3):
        spine = build_spine(book, src, cast)
        if spine:
            for k_ in ("title_fa", "author_fa"):
                if not book.get(k_) and spine.get(k_):
                    book[k_] = spine[k_]
                    print(f"Persian {k_[:-3]} from the story spine: {book[k_]}", flush=True)
        if spine:
            break
        time.sleep(5)
    if not spine:
        raise RuntimeError("Could not build the story spine from Gemini")
    n_ev = len(spine["events"])
    author_src = spine["author_en"] or f"(the source does not say when it was published: only say that {book['author_en']} wrote it)"
    spine_json = json.dumps({"logline": spine["logline"], "world_en": spine.get("world_en", ""), "heroes": spine["heroes"],
                             "events": [{k: v for k, v in e.items() if k != "covers"} for e in spine["events"]],
                             "ending_en": spine["ending_en"]}, ensure_ascii=False)
    author_g = {"woman": "the author is a woman", "man": "the author is a man"}.get(book.get("author_gender"),
                                                                                      "(not known: do not say anything that depends on it)")
    for attempt in range(4):
        prompt = f"""You are a gifted Iranian storyteller and radio host. Write the script of a video that summarizes the
novel "{book['title_en']}" by {book['author_en']} for a Persian-speaking audience.

Use ONLY the facts in the SOURCE TEXT below (taken from Wikipedia). Never add a fact, name, date, number, quote or
opinion that is not in the source. The STORY SPINE below was already decided: it lists the main events, in order, each
with its cause. The viewer never read the book and cannot go back: after this video he must know who the heroes are,
what they want, what happens in order and why, and how it ends. You tell the spine event by event and NOTHING else.

SOURCE TEXT:
{src.get('text_write') or src['text']}

STORY SPINE (the events to tell, in this order; "id" is the event number used for the tags):
{spine_json}
AUTHOR SENTENCE SOURCE (for the opening scene only): {author_src}
AUTHOR GENDER (a fact; every word about the author, in the message too, fits it): {author_g}
CENTRAL IDEA (for the wrap-up only): {spine['idea_en'] or '(none)'}
{cast_block}
Return JSON with exactly these keys:
- "characters": a list of the important characters of the story (at most 6), each {{"name": "...", "look_en": "..."}}.
  "name" in English letters exactly as in the source; "look_en" one fixed look sheet of 35 to 55 English words (gender,
  exact age, face, skin tone, eyes, hair, one distinctive feature, usual clothes with colors). If CHARACTERS are given
  above, copy them unchanged. Empty list if there are no characters.
- "scenes": a list of scene objects in EXACTLY this order of "part" ({min_sc} to {max_sc + 2} scenes in all):
    1. ONE scene with part "open": the introduction, 2 or 3 sentences (35 to 60 words): the name of the novel and of its author
       in English letters, when it was published (only if the AUTHOR SENTENCE SOURCE says so), and in one GRIPPING sentence (a hook: the
       hero, what he or she wants and the problem that threatens it, from the logline; NEVER an outcome, a death, a result
       or anything that happens later: the end of the story is a surprise that only the last scenes reveal). NO SPOILER AND NO JUMP: this scene tells only the situation at
       the START of the story (event 1); it never mentions how the story goes on or ends. Nothing else about the author (no
       biography), no praise.
    2. ONE scene with part "heroes" (only if the spine has 2 or more heroes): 2 to 4 sentences (35 to 65 words) that say who is
       who: for each hero of the spine his or her name and a few words of role and relation to the others (from "role_en").
       Flowing text, not a list. Nothing about what happens to them later (no fate, no ending).
    3. The scenes with part "story": together they tell EVERY event of the spine, in order. Each scene tells ONE event or two
       closely linked events, 2 to 5 flowing sentences (as many as the event needs, 35 to 75 words), every event with its cause.
    4. TWO scenes (three at most) with part "wrap" = the closing wrap-up. The first one starts with the Persian words
       «در جمع‌بندی». It is NOT a second summary: the viewer has just watched the whole story, so do NOT retell the story,
       its events or its ending again, not even in short and not even in other words (no sentence about what somebody did,
       found, lost, killed, escaped or decided; no past-tense story verbs at all). It is written like this, 4 sentences:
       sentence 1 («در جمع‌بندی، ...»): the one central idea of the book beneath the plot; sentence 2: the message of the
       author to the reader; sentence 3: why it matters (its influence, its readers, how it was received: only facts of the
       SIGNIFICANCE text; when the text has none, the author's idea of what the story warns or teaches); sentence 4: short and
       memorable. In 4 to 6 flowing sentences say: (a) what this book is REALLY about beneath the plot, one
       clear idea (from CENTRAL IDEA and the SIGNIFICANCE text); (b) the MESSAGE of the book to the reader, said as the author's
       message; (c) why it matters: its influence, how it was received or why people still read it (only facts of the
       SIGNIFICANCE text, skip what the text does not support); (d) the very last sentence is short and memorable. At most ONE
       short clause may point to the ending (never a list of events). No new events, no empty praise. In the wrap-up NEVER name a
       hero or any other character of the story and NEVER tell what somebody did: it speaks only about the book (its idea, its
       message, its readers, its influence). The program deletes every wrap-up sentence that names a hero or retells the plot.
       LENGTH AND WORDS: the whole wrap-up is 3 or 4 SHORT sentences, 45 words at most. Every noun in it is a GENERAL noun
       (کتاب, انسان, قدرت, آزادی, علم, ترس, امید, جامعه): never a person, a creature, a place or an object of the plot (no
       «جزیره», «پارک», «دایناسور» in a book about dinosaurs): it is the meaning of the book, not its story.
  Every scene is {{"part": "...", "sentences": [{{"fa": "ONE Persian sentence", "ev": TAG, "need": NEED}}, ...], "source_en": "the English
  sentence of the SOURCE TEXT this scene is based on (copy it, at most 45 words)", "characters_present": [names from
  "characters" that this scene is about or that are visible], "setting_en": "where the picture takes place (place, time,
  light)", "action_en": "exactly what is happening in the picture: who does what", "mood_en": "..."}}.
  The TAG "ev" of EVERY sentence:
    * "open" and "heroes" scenes: the word "intro";
    * "story" scenes: the NUMBER of the spine event that this sentence tells (or the cause that leads into it). Every sentence
      of a story scene carries an event number. A sentence that tells no event of the spine is NOT part of this video: the
      program deletes it. The numbers never go backwards and every event number is used;
    * "wrap" scenes: the word "wrap".
  The NEED of EVERY sentence is a number: 3 = the viewer cannot follow the story without it (an event, its cause, the
  introduction of a hero, a turning point, the ending); 2 = it makes an event or its cause clear (a short link, a place, a
  word of explanation); 1 = it only adds color, mood, description or a detail. Write a sentence with NEED 1 only if you
  cannot do without it; the program deletes the sentences with NEED 1 of the story part.

Rules for the narration:
- Total narration: {words} Persian words. Stay close to the source and the spine; never pad.
- ONLY WHAT MATTERS: tell the important events of the spine completely and clearly (who, what, why, what follows) and leave
  out everything else, even if the source mentions it: minor characters, side episodes, small details, names that no later
  event needs. A listener must never be confused by a person or a place that does not matter to the story.
  A short summary keeps ONLY the spine: one clear step for every event, in one or two sentences; no names of side people (a
  name is given only to a hero or to a person that a later event needs), no dates and numbers unless the story needs them, no
  description of a place unless the place matters, no second example, no explanation longer than a few words.
  WORLD: when the spine has a "world_en" that is not empty, the first story sentences (the "heroes" scene) say it plainly
  in simple words before any event, and a hero who is not an ordinary person is called by what he or she is the first time
  (never leave the listener asking "what is this?").
  SENTENCE TEST: before you keep a sentence ask "if I delete it, does the viewer lose a hero, a cause, a turning point, the
  climax or the ending?" If not, do not write it. A sentence that only decorates, repeats, describes a mood or names a side
  person is deleted. Better a short, complete, clear summary than a long one.
- NO SURPRISE PEOPLE: a person is named ONLY if he or she is a hero or acts in an event of the spine; the FIRST time a name is
  said (after the heroes scene) the same sentence says who that person is (relation or job: «پدرِ ...»، «خدمتکارِ خانه»); a
  person who is not in the spine is never mentioned, and nobody appears for the first time in the last scenes or the wrap-up.
- CORRECT, FLOWING PERSIAN (a sentence that is broken or in English word order confuses the listener): every sentence is complete
  (subject, object, verb at the end) in natural Persian word order, never a word-for-word translation; each sentence is linked to
  the one before it by a connector (سپس، پس از آن، اما، چون، برای همین، در همان حال) or by its content; one tense (simple past)
  for the whole story; one idea per sentence, short enough to say in one breath.
- THE ENDING ONLY AT THE END (the listener gets confused when it comes early): no sentence before the last story scene tells
  or hints at the final outcome (a death, a marriage, an escape, who did it, how it ends); the last event and its result are told
  ONLY in the last story scene, after everything else.
- THE ESSENCE: the listener must grasp the heart of the book: what the hero wants and what stands in the way. Every event is told
  as a step of this one main line, in plain words of cause and effect; never lose the main line in side facts. What it cost and
  what the story finally shows come only at the end, never before.
- ORDER: tell the events exactly in the order of the spine, ONE straight line from the first event to the last. NEVER jump
  ahead to the ending or to a later event and then come back (no teaser, no hint of the end before the wrap-up); never
  mention a later event before its turn; say «قبل از آن» or «سال‌ها پیش» only when the spine marks a flashback.
- NO FILLER, NO IRRELEVANT SENTENCE: every sentence of the story part is an event of the spine or the cause that links two
  events. Forbidden there: opinions, praise, the author's life, reception, comparisons with other works, background facts,
  descriptions and details that the following events do not need, a person or a place that no event needs.
- NO AMBIGUITY (the listener never read the book and cannot go back): (1) every person is introduced with a few words of role
  («فلانی، دختر ...») at the FIRST mention, and a name that was not introduced is never used; the heroes are introduced in
  the "heroes" scene, everybody else at his or her first appearance; (2) a pronoun («او»، «آن‌ها»، «این») always points to the
  ONE person or group named in the sentence before it; if two people of the same gender are in that sentence, repeat the
  name; (3) say where and when a new situation takes place whenever it changes; (4) explain every special term, title or
  custom in a few words the first time it appears; (5) write the cause with the effect («چون»، «برای همین»، «در نتیجه»)
  instead of only listing events; (6) one scene = one event and all its facts belong to that event.
- CONTINUITY: the first sentence of every scene picks up exactly where the last sentence of the previous scene stopped (same
  person, time or problem) through a connector that shows cause or time («برای همین»، «چند روز بعد»، «در نتیجه»، «اما»،
  «تا این‌که»); never start a scene from nowhere and never jump to a new subject without a bridge. Whenever a person appears
  again after a gap, remind the viewer who he or she is in a few words.
{MEANING_RULE}
{TONE_RULES}
{NAMES_RULE}
- The voice engine can fail on very long noun chains: at most TWO consecutive ezafe links in a noun phrase (never
  "noun of noun of noun of noun"); prefer verbs and clauses over long genitive chains.
- Correct grammar and spelling, proper half-spaces. It will be read aloud: no emojis, no brackets, no abbreviations.
- NUMBERS: write every number as separate Persian words with spaces and a separate «و» («هفتاد و هفت», «بیست و پنج», «صد و بیست»),
  never as one glued word («هفتادوهفت»), and never with digits.
- Never use English loanwords or words that sound English (for example «پادکست»، «ویدئو»، «آنلاین»، «لایک»،
  «اپلیکیشن»، «استایل»): use the Persian word. Never say the video is a podcast.
- Use everyday words that any listener understands at once. Avoid rare, archaic, heavy literary or Arabic words.
- Do not quote the book at length; paraphrase.

Rules for the visuals:
- Each picture must show EXACTLY what the narration says at that moment (concrete scene, people, objects, action).
- No text, letters, numbers, charts or logos in the pictures. No real celebrities. No scenes copied from films."""
        data = gemini_json(prompt, minutes=15, models=WRITE_MODELS)
        data["style_key"] = "storybook"
        data["art_style_en"] = FIXED_STYLE                # one fixed style for every video
        print("Picture style: storybook (fixed)", flush=True)
        if cast:                                          # the fixed, designed cast replaces the robot's own sheets
            data["characters"] = cast["characters"]
            data["era_en"] = cast["era_en"]
            data["locations"] = cast.get("locations") or []
        try:
            hi_words = int(re.findall(r"\d+", words)[-1]) if re.findall(r"\d+", words) else None
            scenes, problems = tagged_to_scenes(data.get("scenes"), n_ev, strict=attempt < 3,
                                                max_words=int(hi_words * 1.2) if hi_words else None,
                                                open_text=f"در این ویدیو، رمان {book['title_en']}، نوشتهٔ {book['author_en']}، را مرور می‌کنیم.")
        except Exception as e:  # noqa: BLE001
            scenes, problems = [], [f"unreadable script: {e}"]
        if cast:
            okn = {c["name"] for c in cast["characters"]}
            for s in scenes:
                s["characters_present"] = [n for n in s["characters_present"] if n in okn]
        for s in scenes:                                   # fields that the picture planner needs
            first = next((e for e in spine["events"] if e["id"] in (s.get("ev_ids") or [])), None)
            s["setting_en"] = s["setting_en"] or (first["where_en"] if first else "") or "the main place of the story"
            s["action_en"] = s["action_en"] or (first["what_en"] if first else "") or "the main hero in the key situation"
        n_w = sum(len(str(s_.get("narration_fa") or "").split()) for s_ in scenes)
        if n_w < int(MIN_FINAL_WORDS * 0.9) and not problems:
            problems = [f"the narration has only {n_w} words: a video must last more than 3 minutes"]
        ok = (len(scenes) >= 6 and not problems and len(scenes) <= max_sc + 4 and has_wrapup(scenes)
              and data.get("art_style_en"))
        if ok:
            data["scenes"] = scenes
            data.setdefault("characters", [])
            data["outline"] = spine["events"]
            data["subjects"] = spine.get("subjects", [])
            data["spine"] = spine
            data = story_gate(data, book, src)       # blind test with sentence-level repair
            data = scrub_wrap(data, spine)           # the wrap-up never retells the story or names a hero
            data = review_script(data, book, src)    # facts + wording + voice text (last: the voice text must fit the final text)
            if CAST_SCENE:
                data = add_cast_scene(data)
            data = sanitize_narration(data, book)
            return align_visuals(data, book, src)
        print(f"Script attempt {attempt + 1} rejected: {problems or 'too few or too many scenes / no wrap-up'}", flush=True)
        time.sleep(5)
    raise RuntimeError("Could not get a valid script (spine and closing wrap-up) from Gemini")


_RETELL_VERBS = re.compile(r"(کشت|فرار کرد|گرفتار شد|حمله کرد|پیدا کرد|رفت|دید|گفت|شد\b|کرد\b|برگشت|مرد\b|ماند)")


def scrub_wrap(data, spine):
    """Free check in code (no Gemini call): a sentence of the closing wrap-up that names a hero of the story or that mostly
    repeats the words of the story is a second retelling: it is deleted (the first wrap-up sentence with «جمع‌بندی» stays).
    Nothing is deleted when fewer than two wrap-up sentences would remain."""
    names = set()
    for h in (spine or {}).get("heroes", []):
        for tok in re.findall(r"[A-Za-z][A-Za-z'’-]{2,}", str((h or {}).get("name") or "")):
            names.add(tok.lower())

    def toks(t):
        return {w for w in re.findall(r"[\u0600-\u06FF]{5,}", DIAC.sub("", t))}
    story = set()
    for sc in data["scenes"]:
        if sc["part"] == "story":
            story |= toks(sc["narration_fa"])
    plan, kept_total, removed = [], 0, 0
    first, carry = True, False
    for sc in data["scenes"]:
        if sc["part"] != "wrap":
            continue
        keep = []
        for sent in split_sentences(sc["narration_fa"]):
            low = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'’-]{2,}", sent)}
            tk = toks(sent)
            retell = bool(low & names) or (len(tk) >= 3 and len(tk & story) / len(tk) >= 0.3) \
                or bool(_RETELL_VERBS.search(DIAC.sub("", sent)) and len(tk & story) >= 1) or len(tk & story) >= 4
            if retell:
                removed += 1
                if first and "جمعبندی" in _norm_wrap(sent):
                    carry = True                      # the marker «در جمع‌بندی» moves to the next sentence that stays
            else:
                if carry:
                    sent = "در جمع‌بندی، " + sent.lstrip()
                    carry = False
                keep.append(sent)
            first = False
        plan.append((sc, keep))
        kept_total += len(keep)
    if removed and kept_total >= 2:
        scenes = []
        for sc in data["scenes"]:
            if sc["part"] != "wrap":
                scenes.append(sc)
                continue
            keep = next(k for s_, k in plan if s_ is sc)
            if keep:
                sc["narration_fa"] = " ".join(keep)
                scenes.append(sc)
        if sum(1 for s_ in scenes if s_["part"] == "wrap") >= 1 and has_wrapup(scenes):
            data["scenes"] = scenes
            print(f"Wrap-up check: {removed} sentence(s) that retold the story or named a hero were deleted.", flush=True)
    return cap_wrap(data)


def cap_wrap(data, max_words=60):
    """The closing wrap-up is SHORT: whole sentences are kept until 60 words are reached (the first one, with
    «در جمع‌بندی», always stays), the rest is dropped."""
    left, first = max_words, True
    scenes = []
    for sc in data["scenes"]:
        if sc["part"] != "wrap":
            scenes.append(sc)
            continue
        kept = []
        for sent in split_sentences(sc["narration_fa"]):
            w = len(sent.split())
            if kept and left - w < 0:
                break
            if not kept and not first and left <= 0:
                break
            kept.append(sent)
            left -= w
        first = False
        if kept:
            sc["narration_fa"] = " ".join(kept)
            scenes.append(sc)
    if has_wrapup(scenes):
        data["scenes"] = scenes
    return data


def letters_only(s):
    """Only the letters/digits of a text, without diacritics, half-spaces, spaces and punctuation."""
    s = DIAC.sub("", s).replace("\u200c", "").replace("ي", "ی").replace("ك", "ک")
    return re.sub(r"[\W_]+", "", s, flags=re.UNICODE).lower()


def valid_spoken(txt, plain):
    """The voice-only text must contain exactly the same words as the narration (only diacritics and
    punctuation may differ), no stray English words and no digits."""
    if not txt or bad_latin(txt) or DIGITS.search(txt):
        return False
    return letters_only(txt) == letters_only(plain)


def sentence_table(scenes):
    """Every sentence of the narration with an id «scene.sentence» (for example 3.1)."""
    rows = []
    for i, s in enumerate(scenes):
        for k, t in enumerate(split_sentences(s["narration_fa"])):
            rows.append({"id": f"{i}.{k}", "i": i, "k": k, "fa": t})
    return rows


def story_gate(data, book, src, round_=1):
    """Step C (1 to 4 Gemini calls): a viewer who NEVER read the book hears ONLY the narration, retells the story and says
    after which sentence he got lost, which sentences do not belong to the story and which key event he did not hear.
    The repair works on single sentences (replace / delete / insert after), so everything that was fine stays exactly as
    it is. Any trouble keeps the narration as it is."""
    scenes = data["scenes"]
    rows = sentence_table(scenes)
    spine = data.get("spine") or {}
    ev_list = [{"id": e["id"], "event": e["what_en"]} for e in (spine.get("events") or [])]
    shown = [{"id": r["id"], "fa": r["fa"]} for r in rows]
    prompt = f"""You are a viewer who has NEVER read the novel "{book['title_en']}". You only heard this narration of a video
(numbered sentences, in order). Answer ONLY from the narration, never from your own knowledge of the book.

Narration: {json.dumps(shown, ensure_ascii=False)}

Return JSON with exactly these keys, in this order:
- "retelling": 6 to 14 short English sentences: the whole story as YOU understood it from the narration alone (who the hero
  is, what he or she wants, what happens one after the other and why, how it ends). Write it first, from the narration only.
  Where you could not follow, write "UNCLEAR: ..." instead of guessing.
- "confusing": the sentences after which you were lost: [{{"id": "3.1", "problem": "short English"}}]. Typical problems: a person
  who is used without being introduced (who is this?), a pronoun that could mean two people, an unexplained term, title or
  custom, a new place or time that was not announced, a sentence that does not follow from the one before it, a result
  without its cause. Empty list if none.
- "illogical": the sentences that make no sense or sound silly when taken literally in the world of this story (a thing that
  does what only a person can do, such as a ship that «commits suicide»; a metaphor, a personification or an idiom translated
  word for word), each {{"id": "3.1", "meaning_en": "what the author most likely meant, in plain words"}}. Empty list if none.
- "wrap_retells": the ids of sentences of the closing wrap-up (the part after «در جمع‌بندی») that RETELL events of the story
  again instead of saying what the book means, its message and why it matters; each {{"id": "9.0"}}. Empty list if none.
- "irrelevant": ids of sentences that tell nothing the story needs (the removal test: without them the chain of cause and
  effect stays complete): praise, trivia, background, the author's life, a detail that no later event needs, a repeated
  point, a description of a place or a mood, the name of a side person, a second example of something already shown.
  Be strict: when in doubt, list it. Empty list if none.
- "order_problems": ids of sentences that tell something BEFORE the thing that causes it, or whose time order contradicts the
  rest (a later event or the ending told too early, a jump ahead and then back again, an earlier event told after a later
  one). Empty list if the story runs in one straight line.
- "missing_events": ids (numbers) of those key events of this list that you did NOT clearly hear in the narration:
  {json.dumps(ev_list, ensure_ascii=False)}. Empty list if you heard all of them."""
    try:
        t = gemini_json(prompt, minutes=6, models=WRITE_MODELS)
        retell = [str(x) for x in (t.get("retelling") or [])][:14]
        valid = {r["id"]: r for r in rows}
        protect = set()
        for r in rows:
            part = scenes[r["i"]].get("part")
            if part in ("author",) or "جمعبندی" in _norm_wrap(r["fa"]):
                protect.add(r["id"])
        confusing = [{"id": str(g.get("id")), "problem": str(g.get("problem") or "")} for g in (t.get("confusing") or [])
                     if isinstance(g, dict) and str(g.get("id")) in valid][:8]
        for g in (t.get("wrap_retells") or []):
            gid = str(g.get("id") if isinstance(g, dict) else g)
            if gid in valid and gid not in [c["id"] for c in confusing]:
                confusing.append({"id": gid, "problem": "the wrap-up retells the story again: replace it with what the book "
                                                        "means, its message or why it matters (no events); the first wrap-up "
                                                        "sentence keeps the words «در جمع‌بندی» at its start"})
        for g in (t.get("illogical") or []):
            if isinstance(g, dict) and str(g.get("id")) in valid and str(g.get("id")) not in [c["id"] for c in confusing]:
                confusing.append({"id": str(g.get("id")),
                                  "problem": "illogical when read literally; the author probably meant: "
                                             + str(g.get("meaning_en") or "")[:160] + ". Say what REALLY happened"})
        irrelevant = []
        for g in (t.get("irrelevant") or []):
            gid = str(g.get("id") if isinstance(g, dict) else g)
            if gid in valid and gid not in protect and gid not in irrelevant:
                irrelevant.append(gid)
        irrelevant = irrelevant[:8]
        for g in (t.get("order_problems") or []):
            gid = str(g.get("id") if isinstance(g, dict) else g)
            if gid in valid and gid not in protect and gid not in [c["id"] for c in confusing]:
                confusing.append({"id": gid, "problem": "told in the wrong order: fix the order or the bridge"})
        ev_ids = {e["id"] for e in (spine.get("events") or [])}
        missing = [int(x) for x in (t.get("missing_events") or []) if str(x).lstrip("-").isdigit() and int(x) in ev_ids][:4]
        print(f"Story gate (round {round_}): confusing={len(confusing)} irrelevant={len(irrelevant)} "
              f"missing_events={missing}", flush=True)
        for line in retell[:14]:
            print("  viewer understood:", line[:150], flush=True)
        if not (confusing or irrelevant or missing):
            return data
        miss_txt = [{"id": e["id"], "event": e["what_en"], "because": e["because_en"], "where": e["where_en"]}
                    for e in (spine.get("events") or []) if e["id"] in missing]
        fix = f"""Fix the Persian narration of a video about the novel "{book['title_en']}" by {book['author_en']}.
A viewer who never read the book heard only the narration and found these problems (ids refer to the numbered sentences
below):
- sentences after which he was lost: {json.dumps(confusing, ensure_ascii=False)}
- sentences that do not belong to the main story: {json.dumps(irrelevant)}
- key events that he did not clearly hear: {json.dumps(miss_txt, ensure_ascii=False)}
- parts he could not follow in his retelling: {json.dumps([x for x in retell if x.upper().startswith('UNCLEAR')], ensure_ascii=False)}

SOURCE TEXT (the only truth; use nothing else):
{src['text']}

Narration (numbered sentences): {json.dumps(shown, ensure_ascii=False)}

Return JSON {{"ops": [{{"op": "replace", "id": "3.1", "fa": "..."}}]}} where "op" is one of:
- "replace": rewrite that ONE sentence so that the problem disappears: give the person a few words of role at the first
  mention, name the ONE person a pronoun means, explain the term in a few words, announce the new place or time, add the
  cause («چون»، «برای همین»، «در نتیجه»).
- "delete": remove a sentence that does not belong to the main story (only those listed above; never the only sentence that
  tells a key event). "fa" is "".
- "insert_after": add ONE new sentence after the sentence with this id: the missing link or the missing key event, only
  facts of the SOURCE TEXT, joined to the sentence before it by a connector and leading into the sentence after it.
Rules: at most 10 operations; change as little as possible; every other sentence stays untouched. Every new "fa" is ONE
Persian sentence of 8 to 24 words that continues the story smoothly.
{MEANING_RULE}
{TONE_RULES}
{NAMES_RULE}"""
        out = gemini_json(fix, minutes=8, models=WRITE_MODELS)
        ops = [o for o in (out.get("ops") or []) if isinstance(o, dict)][:10]
        sents = {i: [{"id": r["id"], "fa": r["fa"]} for r in rows if r["i"] == i] for i in range(len(scenes))}
        inserts, replaced, deleted = {}, 0, 0
        for o in ops:
            op, oid = str(o.get("op") or ""), str(o.get("id") or "")
            fa = " ".join(str(o.get("fa") or "").replace("ي", "ی").replace("ك", "ک").split())
            if oid not in valid:
                continue
            i = valid[oid]["i"]
            slot = next((x for x in sents[i] if x["id"] == oid), None)
            if slot is None:
                continue
            if op == "replace" and fa and (oid not in protect or (scenes[i].get("part") == "wrap" and "جمعبندی" in _norm_wrap(fa))):
                slot["fa"] = fa
                replaced += 1
            elif op == "delete" and oid in irrelevant:
                live = [x for x in sents[i] if x.get("fa")]
                if len(live) > 1 and scenes[i].get("part") == "story":
                    slot["fa"] = ""
                    deleted += 1
            elif op == "insert_after" and fa and scenes[i].get("part") != "author" and oid not in inserts:
                inserts[oid] = fa
        new_scenes, added = [], 0
        for i, s in enumerate(scenes):
            parts = []
            for x in sents[i]:
                if x["fa"]:
                    parts.append(x["fa"])
                if x["id"] in inserts:
                    parts.append(inserts[x["id"]])
                    added += 1
            s = dict(s)
            s["narration_fa"] = " ".join(parts)
            new_scenes.append(s)
        words = sum(len(s["narration_fa"].split()) for s in new_scenes)
        before = sum(len(s["narration_fa"].split()) for s in scenes)
        if (all(s["narration_fa"].strip() for s in new_scenes) and has_wrapup(new_scenes)
                and words <= max(MAX_WORDS, before) * 1.15
                and all(len(s["narration_fa"].split()) <= 120 for s in new_scenes)):
            data["scenes"] = new_scenes
            print(f"Story gate repaired the narration: {replaced} replaced, {added} added, {deleted} deleted "
                  f"({before} -> {words} words).", flush=True)
            if round_ < 2 and (replaced or added or deleted):         # a second look: did the repair fix everything?
                return story_gate(data, book, src, round_ + 1)
        else:
            print("Story gate: the repair was not safe (length or wrap-up): kept the original narration.", flush=True)
    except (GeminiBlocked, GeminiQuotaExhausted):
        raise
    except Exception as e:  # noqa: BLE001
        print("Story gate skipped:", e, flush=True)
    return data


def review_script(data, book, src):
    """Step C (ONE Gemini call): fact check + wording/tone polish + the voice-only text with FULL diacritics.
    "narration_fa" is the subtitle; "spoken_fa" is only read by the voice. If the call fails or a text does not
    pass the strict word-for-word check, the original narration is used for that paragraph."""
    scenes = data["scenes"]
    items = [{"i": i, "narration_fa": s["narration_fa"]} for i, s in enumerate(scenes)]
    prompt = f"""You are, at the same time, a strict fact checker, a senior Persian (Iranian) editor and a pronunciation
editor for a text-to-speech voice.

SOURCE TEXT (the only truth):
{src['text']}

Below are Persian narration paragraphs, in order, of a video about the novel "{book['title_en']}". A voice reads them
aloud. For EACH paragraph return two texts.

A) "narration_fa" (also shown as the subtitle)
1. FACTS: keep only what the SOURCE TEXT supports. Remove or fix every fact, name, date, number, quote or interpretation
   that is not in the source, every wrong fact, and every sentence that is filler without information. If nothing
   supported remains, return "" for both texts of that paragraph.
   MEANING: a sentence that is illogical when read literally, or a word-for-word translation that says something different
   from what the author meant, is rewritten with the REAL meaning in natural Persian (this is the one case where you may
   change the wording of a sentence). Follow this rule:
{MEANING_RULE}
   Do not remove a sentence that tells a main event, a turning point, the introduction of a hero, the author sentence, the
   ending or the wrap-up. A sentence about a minor detail or a side person that no main event needs may be removed (never
   leave a hole in the chain of cause and effect).
2. WORDING: keep exactly the same sentences in the same order (do not merge, split, add or delete a sentence); only fix
   grammar, word choice, spelling and half-spaces, a literal translation (MEANING above), and make the connectors between the sentences natural. Do NOT add new
   facts. Keep the order and the number of paragraphs. Keep names in English (Latin) letters exactly as they are.
   {TONE_RULES}
   Keep the length and the number of sentences of every paragraph; at most TWO consecutive ezafe links in a noun phrase; numbers as words, no
   digits (a number is written as separate words: «هفتاد و هفت», never «هفتادوهفت»), no emojis, no brackets, no abbreviations, no English word except names; simple everyday words. A paragraph that
   contains «جمع‌بندی» must keep that word and stay a clear, useful conclusion.

B) "spoken_fa" (read by the voice only, never shown): the SAME final text as "narration_fa", written with FULL
diacritics so the voice cannot misread it:
1. Exactly the same words in the same order. You may only ADD vowel marks and voice punctuation (commas for breath, «…»
   for suspense, «!» or «؟» where the meaning asks). Do not add, remove, reorder or reword any word.
2. Put the correct vowel marks (فتحه، کسره، ضمه، تشدید، تنوین) on EVERY Persian word, as it is pronounced in standard
   Iranian Persian: «وَ» for "and", «اِز/اَز» as pronounced, ezafe with kasra («کِتابِ»), «کِه», «بِه», «اِین», «اَست»,
   «می‌خواهَد», «شُد», «گُفت», «نَه» and so on. Use the common Iranian pronunciation when a word has two readings.
   Keep half-spaces.
3. Keep every English (Latin) name exactly as in narration_fa, without any mark.

The novel is "{book['title_en']}" by {book['author_en']}.
Also return "names": an object that maps EACH English name used in your texts to its Persian phonetic spelling with vowel
marks (used only if the English voice is not available), for example {{"Holden Caulfield": "هُلدِن کالفیلد"}}.
Return JSON {{"results": [{{"i": 0, "narration_fa": "...", "spoken_fa": "..."}}, ...], "names": {{...}}}}, one item per
paragraph, in order.
Paragraphs: {json.dumps(items, ensure_ascii=False)}"""
    for s in scenes:
        s["spoken_fa"] = s["narration_fa"]
    try:
        out = gemini_json(prompt, minutes=10, models=WRITE_MODELS)
        res = out["results"]
        by_i = {int(r["i"]): r for r in res}
        originals = [dict(s) for s in scenes]
        keep = []
        edited = dropped = spoken_ok = 0
        for i, s in enumerate(scenes):
            r = by_i.get(i)
            if not r or not isinstance(r.get("narration_fa"), str):
                keep.append(s)
                continue
            txt = " ".join(r["narration_fa"].replace("ي", "ی").replace("ك", "ک").split())
            if not txt:
                dropped += 1
                continue
            if txt != s["narration_fa"]:
                edited += 1
            s["narration_fa"] = txt
            sp = " ".join(str(r.get("spoken_fa", "")).replace("ي", "ی").replace("ك", "ک").split())
            if valid_spoken(sp, txt):
                s["spoken_fa"] = sp
                spoken_ok += 1
            else:
                s["spoken_fa"] = txt
            keep.append(s)
        if len(keep) >= 5 and has_wrapup(keep):
            data["scenes"] = keep
            names = out.get("names") or {}
            data["names"] = {str(k): str(v) for k, v in names.items()
                             if isinstance(names, dict) and str(k).strip() and str(v).strip()}
            print(f"Review: {edited} paragraph(s) edited, {dropped} dropped, diacritized voice text ready for "
                  f"{spoken_ok}/{len(keep)}, {len(data['names'])} name(s).", flush=True)
        else:
            data["scenes"] = originals
            print("Review would remove too much or the wrap-up: kept the original narration.", flush=True)
    except (GeminiBlocked, GeminiQuotaExhausted):
        raise
    except Exception as e:  # noqa: BLE001
        print("Review skipped:", e, flush=True)
    return data


LATIN = re.compile(r"[A-Za-z]")
DIGITS = re.compile(r"[0-9\u06f0-\u06f9]")

# English names inside the Persian text (people, places, the novel and the author): capitalized words, with small
# connecting words (of, the, in, de, van...) allowed between them: "The Catcher in the Rye", "J. D. Salinger".
_NAME_WORD = r"[A-Z][A-Za-z'’\-]*\.?"
_NAME_JOIN = (r"(?:\s+(?:of|the|in|on|and|de|del|della|di|da|du|la|le|van|von|der|den|el|al|bin|ibn|ben|y))*\s+")
NAME_RUN = re.compile(rf"{_NAME_WORD}(?:{_NAME_JOIN}{_NAME_WORD})*")


def bad_latin(text):
    """True if the text has English letters outside names (names are allowed)."""
    return bool(LATIN.search(NAME_RUN.sub(" ", text)))


def strip_bad_latin(text):
    out, pos = [], 0
    for m in NAME_RUN.finditer(text):
        out.append(re.sub(r"[A-Za-z][A-Za-z'’\-]*", "", text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(re.sub(r"[A-Za-z][A-Za-z'’\-]*", "", text[pos:]))
    return " ".join("".join(out).split())


def split_names(text):
    """[(is_english_name, piece), ...] in order."""
    out, pos = [], 0
    for m in NAME_RUN.finditer(text):
        if m.start() > pos:
            out.append((False, text[pos:m.start()]))
        out.append((True, m.group(0)))
        pos = m.end()
    if pos < len(text):
        out.append((False, text[pos:]))
    return out


def _sanitize_core(data, book):
    """English letters are allowed ONLY for names; digits are never allowed (the voice reads them badly)."""
    scenes = data["scenes"]
    for sc in scenes:
        sc["narration_fa"] = sc["narration_fa"].replace("ي", "ی").replace("ك", "ک")

    def bad(i):
        t = scenes[i]["narration_fa"]
        return bool(bad_latin(t) or DIGITS.search(t))

    for attempt in range(2):
        idx = [i for i in range(len(scenes)) if bad(i)]
        if not idx:
            return data
        print(f"Narration check: {len(idx)} scene(s) contain English words or digits, fixing (try {attempt + 1}).",
              flush=True)
        items = [{"i": i, "narration_fa": scenes[i]["narration_fa"]} for i in idx]
        prompt = f"""Fix these Persian narration paragraphs of a video about the novel "{book['title_en']}".
A voice will read them aloud. English (Latin) letters are allowed ONLY for names of people, places, the novel and its
author. Replace every other English word with Persian, and write every digit as Persian words. Keep the same meaning,
order and length. Keep the word «جمع‌بندی» if it is in a paragraph. Use everyday words every listener understands.
Return JSON: {{"fixed": [{{"i": 0, "narration_fa": "..."}}, ...]}}
Paragraphs: {json.dumps(items, ensure_ascii=False)}"""
        try:
            fixed = gemini_json(prompt, minutes=5, models=CHEAP_MODELS)["fixed"]
            for f in fixed:
                i = int(f["i"])
                txt = f.get("narration_fa", "").strip()
                if i in idx and txt:
                    scenes[i]["narration_fa"] = txt.replace("ي", "ی").replace("ك", "ک")
        except (GeminiBlocked, GeminiQuotaExhausted):
            raise
        except Exception as e:  # noqa: BLE001
            print("Narration fix skipped:", e, flush=True)
    # last resort: drop what is still an English word outside a name, so the voice never reads English by mistake
    for i in range(len(scenes)):
        if bad_latin(scenes[i]["narration_fa"]):
            scenes[i]["narration_fa"] = strip_bad_latin(scenes[i]["narration_fa"])
            print(f"Scene {i}: removed leftover English words.", flush=True)
    return data


def sanitize_narration(data, book):
    before = [s["narration_fa"].replace("ي", "ی").replace("ك", "ک") for s in data["scenes"]]
    data = _sanitize_core(data, book)
    for s in data["scenes"]:
        s["narration_fa"] = fix_numbers(s["narration_fa"])
    for s, b in zip(data["scenes"], before):
        if s["narration_fa"] != b or not s.get("spoken_fa"):
            s["spoken_fa"] = s["narration_fa"]       # the narration changed: the old voice text no longer fits
    return data


CAST_SCENE = False   # OFF: the image model cannot guarantee that a face stays the same, so no portrait scene is shown


def add_cast_scene(data):
    """Right after the first paragraph: every important character is introduced by name and his or her face is shown
    (one portrait per sentence)."""
    chars = {c.get("name"): c for c in data.get("characters", []) if isinstance(c, dict) and c.get("name")}
    cast = []
    for c in data.get("cast") or []:
        if isinstance(c, dict) and c.get("name") in chars and str(c.get("sentence_fa", "")).strip():
            cast.append((c["name"], " ".join(str(c["sentence_fa"]).split())))
    cast = cast[:5]
    if len(cast) < 2 or len(data["scenes"]) < 3:
        return data
    data["scenes"].insert(1, {
        "narration_fa": " ".join(t for _, t in cast), "spoken_fa": " ".join(t for _, t in cast),
        "source_en": "", "characters_present": [n for n, _ in cast],
        "setting_en": "portraits of the main characters", "action_en": "portraits of the main characters",
        "mood_en": "calm curiosity", "cast_names": [n for n, _ in cast]})
    return data


def cast_visual(chars, name):
    look = next((c["look_en"] for c in chars if isinstance(c, dict) and c.get("name") == name), "")
    return {"visual_en": "Close-up face portrait of this character, head and shoulders, looking at the viewer with a "
                         "calm expression, simple plain background.",
            "characters_present": [name]}


_PERS = "\u0600-\u06FF"


def _pw(stems, suffix=True):
    """A Persian WORD (not a piece of a longer word: «مشغول» is not «غول», «قفسه» is not «قفس»)."""
    return rf"(?<![{_PERS}])(?:{stems})" + (r"(?:ها|های|ی|ای|ان|هایی)?" if suffix else "") + rf"(?![{_PERS}])"


_THING_LEXICON = [
    (_pw(r"دایناسور|تی\W?رکس|تیرانوزور|ولوسیراپتور|راپتور|براکیوسور|تری\W?سراتوپس|استگوسور"),
     "a huge dinosaur, much taller than the people, scaly green-brown skin, a big head with a wide mouth and strong legs, its whole body visible", ("dinosaur", "raptor", "rex")),
    (_pw("اژدها|اژدر"), "a huge dragon with wings, scales and a long neck, its whole body visible", ("dragon",)),
    (_pw("گرگ"), "a large gray wolf, alert and powerful, its whole body visible", ("wolf",)),
    (_pw("ببر|پلنگ"), "a big striped tiger, powerful and alert, its whole body visible", ("tiger", "leopard")),
    (_pw("خرس"), "a huge brown bear on its hind legs, its whole body visible", ("bear",)),
    (_pw("کوسه"), "a big gray shark in the water, its whole body visible", ("shark",)),
    (_pw("نهنگ"), "a gigantic whale, its whole body visible", ("whale",)),
    (_pw("هیولا|غول"), "a gigantic monster, much taller than the people, its whole body visible", ("monster", "giant")),
    (_pw("قفس"), "a strong metal cage with thick bars, big and clearly visible", ("cage",)),
    (_pw("حصار|نرده|فنس"), "a tall fence with strong posts and wire, clearly visible", ("fence",)),
    (_pw("جیپ"), "an open off-road jeep, clearly visible", ("jeep",)),
    (_pw("سفینه|فضاپیما"), "a large spaceship with metal walls and lights, clearly visible", ("spaceship", "ship")),
    (_pw("ربات"), "a metal robot with clear arms and legs, clearly visible", ("robot",)),
    (_pw("عقاب|شاهین"), "a big eagle with wide wings, clearly visible", ("eagle", "hawk")),
    (_pw("اسب"), "a strong horse, clearly visible", ("horse",)),
    (rf"(?<![{_PERS}])(?:مار|افعی)(?:ها)?(?![{_PERS}])", "a long snake, clearly visible", ("snake",)),
    (_pw("کشتی بزرگ|ناو", False), "a large ship on the water, clearly visible", ("ship",)),
]


_EVENT_LEXICON = [
    (_pw("حمله"), ("attack", "charg", "lung", "pounc"), "the huge creature lunges toward the running people with its mouth wide open"),
    (_pw("تعقیب|دنبال کرد|شکار"), ("chase", "chasing", "hunt", "pursu"), "a creature chases the fleeing people through the place"),
    (_pw("فرار|گریخت"), ("flee", "fleeing", "run", "escap"), "the people run away in fear"),
    (_pw("قفس|گیر افتاد|گرفتار|به دام"), ("trap", "cage", "bars", "caught"), "a person is trapped behind strong bars and cannot get out"),
    (_pw(r"حلق\s*آویز|به\s*دار\s*آویخت|دار\s*زد|خود\s*را\s*آویخت"), ("rope", "noose", "toppled chair"),
     "a thick rope hangs empty from a ceiling beam above a toppled wooden chair in a dim quiet room; nobody hangs from it and "
     "no body is shown anywhere"),
    (_pw(r"کشته|جان\s*باخت|درگذشت|فوت\s*کرد|به\s*قتل\s*رسید|خودکشی"), ("grief", "mourn", "candle", "bowed"),
     "the people who loved the lost person stand with bowed heads and tears of grief beside a small table with a lit candle "
     "and a single white flower; nobody lies on the ground and no body is shown anywhere"),
    (_pw("سقوط|پرت شد"), ("fall", "fell"), "a person falls through the air"),
    (_pw("انفجار"), ("explo", "blast"), "a big explosion with smoke and fire"),
    (_pw("طوفان|باران شدید"), ("storm", "heavy rain"), "a heavy storm with rain and wind"),
]


def inject_event(sc, beat_text):
    """The event that the narration names (an attack, a chase, a trap, a death...) is always in the picture plan: when neither
    the event line nor the picture text has it, a short gentle line is put in front of the event."""
    t = DIAC.sub("", str(beat_text or "")).replace("\u200c", " ")
    have = (str(sc.get("event_en") or "") + " " + str(sc.get("visual_en") or "")).lower()
    hints = [en for pat, keys, en in _EVENT_LEXICON if re.search(pat, t) and not any(k in have for k in keys)][:2]
    if hints:
        sc["event_en"] = ". ".join(hints + ([str(sc["event_en"])] if sc.get("event_en") else []))
    return sc


def inject_subjects(sc, beat_text, subjects):
    """Free check in code (no Gemini call): a creature or thing that the narration of THIS picture names (a dinosaur, a cage, a
    ship...) is always drawn, even when the picture plan forgot it."""
    t = DIAC.sub("", str(beat_text or "")).replace("\u200c", " ")
    for sub in subjects or []:
        if not any(w and w in t for w in sub.get("fa", [])):
            continue
        words = [w for w in re.findall(r"[a-z]{4,}", str(sub.get("en") or "").lower())
                 if w not in ("with", "that", "huge", "large", "small", "from", "very", "tall", "big")][:3]
        text = " ".join(str(sc.get("visual_en") or "").lower().split() + [str(x).lower() for x in sc.get("creatures_en") or []])
        if words and any(w in text for w in words):
            continue
        sc["creatures_en"] = ([sub["en"]] + list(sc.get("creatures_en") or []))[:2]
        sc["must_show"] = ([sub["en"][:90]] + list(sc.get("must_show") or []))[:5]
    for pat, en, keys in reversed(_THING_LEXICON):             # the same check from a fixed word list (no Gemini needed)
        if not re.search(pat, t):
            continue
        text = " ".join(str(sc.get("visual_en") or "").lower().split() + [str(x).lower() for x in sc.get("creatures_en") or []]
                        + [str(x).lower() for x in sc.get("must_show") or []])
        if any(k in text for k in keys):
            continue
        sc["creatures_en"] = ([en] + list(sc.get("creatures_en") or []))[:2]
        sc["must_show"] = ([en[:90]] + list(sc.get("must_show") or []))[:5]
    return sc


def make_beats(text):
    """Cut a paragraph into beats of about BEAT_WORDS words (whole sentences): one picture per beat."""
    sents = split_sentences(DIAC.sub("", text))
    beats, cur = [], []
    for x in sents:
        cur.append(x)
        if sum(len(y.split()) for y in cur) >= BEAT_WORDS:
            beats.append(" ".join(cur))
            cur = []
    if cur:
        if beats and sum(len(y.split()) for y in cur) < BEAT_WORDS // 2:
            beats[-1] += " " + " ".join(cur)
        else:
            beats.append(" ".join(cur))
    return beats or [text]


SHOTS = ("medium-wide shot from head to knees", "medium shot from the head to the hips with the people large in the frame and the place behind them",
         "medium shot from the waist up with the place clearly visible behind", "over-the-shoulder medium-wide shot",
         "low-angle medium-wide shot", "high-angle medium-wide shot", "eye-level shot through a doorway or window",
         "side-view medium-wide shot", "wide establishing shot with a detailed place (only when no named person is in it)")
FRAMING = ("FRAMING (very important): a medium-wide shot, NOT far away and NOT a close-up. The named people are LARGE: "
           "each one fills about half of the picture height, so the face is big enough to show clear eyes, nose and mouth. "
           "The whole head and body of every person is inside the picture (nothing cut by the border). The rest of the "
           "picture is a rich, detailed place. The lower fifth of the picture is only floor or ground (subtitles cover it).")
POSE = ("POSE (very important): the people are INSIDE the scene and busy with the action of the sentence. They look at each "
        "other, at the object of the action or away; they are seen from the side, from three quarters or from behind, but "
        "most of the time the face stays visible. Nobody stands still facing the viewer and nobody looks into the camera.")


def _vwords(t):
    return {w for w in re.findall(r"[a-z]+", str(t).lower()) if len(w) > 3}


def _too_similar(a, b):
    """True if two neighbouring pictures would look almost the same (same shot, or mostly the same words)."""
    if not a or not b:
        return False
    if a.get("shot") and a.get("shot") == b.get("shot"):
        return True
    wa, wb = _vwords(a["visual_en"]), _vwords(b["visual_en"])
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) > 0.6


def diversify_visuals(vis, items, book):
    """ONE extra Gemini call: neighbouring pictures that repeat the same shot or the same scene are written again
    with another camera, another background and other details (the story content stays the same)."""
    bad = [k for k in range(1, len(vis)) if _too_similar(vis[k - 1], vis[k])]
    if not bad:
        return vis
    jobs = [{"i": k, "narration_fa": items[k]["narration_fa"], "current": vis[k]["visual_en"],
             "previous_picture": vis[k - 1]["visual_en"], "previous_shot": vis[k - 1].get("shot", ""),
             "next_picture": vis[k + 1]["visual_en"] if k + 1 < len(vis) else ""} for k in bad]
    prompt = f"""Some neighbouring pictures of a video about "{book['title_en']}" look too similar. Rewrite ONLY the pictures
below. Each one must still show exactly what its narration says, but with a DIFFERENT shot (choose from
{json.dumps(SHOTS)}), a different part of the place or a different background detail, different light or time of day
where the story allows, and different foreground objects than "previous_picture" and "next_picture".
Keep the same characters, the SAME PLACE (its concrete details stay, only the viewpoint and the part of the place change) and the SAME EVENT (what happens in the narration must stay the main thing), no names, no text, 90 to 110 English words, start with the shot type, no person that is not already in the picture. Medium-wide pictures with large people: nobody cropped by the border, no extreme close-ups.
Return JSON {{"visuals": [{{"i": 0, "shot": "...", "visual_en": "..."}}]}}
Pictures: {json.dumps(jobs, ensure_ascii=False)}"""
    try:
        out = gemini_json(prompt, minutes=5, models=WRITE_MODELS)["visuals"]
        n = 0
        for v in out:
            k = int(v["i"])
            if k in bad and isinstance(v.get("visual_en"), str) and v["visual_en"].strip():
                vis[k]["visual_en"] = v["visual_en"].strip()
                vis[k]["shot"] = str(v.get("shot") or "")
                n += 1
        print(f"Visuals diversified: {n} of {len(bad)} similar neighbours rewritten.", flush=True)
    except (GeminiBlocked, GeminiQuotaExhausted):
        raise
    except Exception as e:  # noqa: BLE001
        print("Diversify skipped:", e, flush=True)
    return vis


def force_named_characters(present, narration, chars):
    """The narration writes the names in English letters. A character whose name (or whole first/last name) stands in the
    sentence is ALWAYS in its picture, even when Gemini forgot to list him or her. Order: the listed ones first."""
    names = [c["name"] for c in chars if isinstance(c, dict) and c.get("name")]
    out = [n for n in (present or []) if n in names]
    low = str(narration).lower()
    for n in names:
        if n in out:
            continue
        parts = [w for w in re.findall(r"[A-Za-z][A-Za-z'’\-]+", n) if len(w) >= 3 and w.lower() not in ("the", "and", "von", "van")]
        if n.lower() in low or any(re.search(rf"(?<![A-Za-z]){re.escape(w.lower())}(?![A-Za-z])", low) for w in parts):
            out.append(n)
    return out[:MAX_PEOPLE]


def _str_list(x, n):
    return [" ".join(str(i).split()) for i in (x if isinstance(x, list) else []) if str(i).strip()][:n]


def strip_colors(text):
    """The text without color words (\"a gray-haired man in a brown coat\" -> \"a man in a coat\")."""
    keys = sorted({k for k, _ in _COLOR_FAMILIES}, key=len, reverse=True)
    t = re.sub(r"\b(?:" + "|".join(keys) + r")(?:[- ]?(?:haired|colou?red|skinned))?\b,?\s*", "", str(text), flags=re.I)
    return " ".join(t.split())


_OTHER_BUILDS = ("slim build", "stocky build", "tall lanky build", "short sturdy build", "heavy-set build")
_OTHER_FACES = ("round face", "long narrow face", "square jaw", "oval face with high cheekbones", "heart-shaped face",
                "angular face with a hooked nose", "soft wide face", "thin face with sharp cheekbones")


def fix_others_in_code(vis, chars):
    """FREE check in code: every unnamed person of a picture (rescuer, doctor, neighbour...) gets a hair color, a shirt
    color and a trousers color that NO named person of that picture and no other unnamed person has. The model only
    reads colors well when they are written plainly, so the colors are decided here and not left to chance."""
    byname = {c["name"]: c for c in chars if isinstance(c, dict) and c.get("name")}
    changed = 0
    for v in vis:
        others = v.get("others") or []
        if not others:
            continue
        used = {"hair": set(), "top": set(), "bottom": set()}
        used_face, used_fh, used_build = set(), set(), set()
        for n in v.get("characters_present") or []:
            c = byname.get(n) or {}
            used_face.add(str(c.get("face_en") or "")[:12])
            if c.get("facial_en"):
                used_fh.add(c["facial_en"])
            for f, k in (("hair_color", "hair"), ("top_color", "top"), ("bottom_color", "bottom")):
                fam = color_family(c.get(f))
                if fam:
                    used[k].add(fam)
        new = []
        keys = sorted({k for k, _ in _COLOR_FAMILIES}, key=len, reverse=True)
        color_re = re.compile(r"\b(" + "|".join(keys) + r")\b((?:[- ]?(?:haired|colou?red|skinned))?)", re.I)
        for o in others:
            o = " ".join(str(o).split()[:12])
            old = bool(re.search(r"\b(old|elderly|aged|grandmother|grandfather|white-haired|gray-haired|grey-haired)\b", o, re.I))
            hair_pool = (("gray", "white") + HAIR_PALETTE) if old else HAIR_PALETTE
            hair = next((x for x in hair_pool if color_family(x) not in used["hair"]), "")
            tops = [x for x in OUTFIT_PALETTE if color_family(x) not in used["top"]]
            bots = [x for x in OUTFIT_PALETTE if color_family(x) not in used["bottom"]]
            top = tops[0] if tops else ""
            bottom = next((x for x in bots if color_family(x) != color_family(top)), "")
            seq = [x for x in (top, bottom) if x]          # colors of the clothes, in the order they appear in the text
            state = {"n": 0}

            def recolor(m):
                tail_w = m.group(2) or ""
                after = o[m.end():m.end() + 14].lower()
                if tail_w.lower().strip(" -") == "haired" or re.match(r"\W*(?:\w+\W+){0,1}hair", after):
                    return (hair or m.group(1)) + tail_w
                if "skin" in tail_w.lower():
                    return m.group(0)
                col = seq[state["n"] % len(seq)] if seq else m.group(1)
                state["n"] += 1
                return col + tail_w

            text, n_sub = color_re.subn(recolor, o)
            if not n_sub:             # the model wrote no colors at all: they are added plainly
                text = text.rstrip(" .,") + ", " + ", ".join(
                    x for x in ((f"{hair} hair" if hair else ""),
                                (f"clothes in {top} and {bottom}" if (top and bottom) else "")) if x)
            for k, val in (("hair", hair), ("top", top), ("bottom", bottom)):
                if val:
                    used[k].add(color_family(val))
            # a different build, face shape and (for men) facial hair for EVERY unnamed person of the picture
            build = next((b_ for b_ in _OTHER_BUILDS if b_ not in used_build), _OTHER_BUILDS[len(used_build) % len(_OTHER_BUILDS)])
            face = next((f_ for f_ in _OTHER_FACES if f_ not in used_face), _OTHER_FACES[len(used_face) % len(_OTHER_FACES)])
            used_build.add(build)
            used_face.add(face)
            extra = [build, face]
            if not _FEMALE_TXT.search(o) and not re.search(r"\b(child|boy|girl|kid|baby)\b", o, re.I):
                fh_ = next((f_ for f_ in _FACIAL if f_ not in used_fh), "clean-shaven")
                used_fh.add(fh_)
                extra.append(FACIAL_TEXT.get(fh_, fh_))
            text = text.rstrip(" .,") + ", " + ", ".join(extra)
            new.append(text)
        if new != others:
            changed += 1
        v["others"] = new
    print(f"Unnamed people: colors fixed in {changed} pictures.", flush=True)
    return vis


def review_visual_plan(vis, items, book, chars, src=None):
    """ONE text-only Gemini call (no picture is sent) BEFORE anything is drawn: every picture plan is read next to its
    Persian sentence, and a plan with a mistake is rewritten on the spot (number of people, place and event, must-show
    things, who does what to whom, at most 2 large faces). Closing parts (author / book / theme) are not drawn, so
    they are not reviewed."""
    idx = [k for k, v in enumerate(vis) if str(v.get("kind") or "event") == "event"]
    if not idx:
        return vis
    names = {c["name"]: c.get("key_en") or char_tag(c) for c in chars if isinstance(c, dict) and c.get("name")}
    rows = []
    for k in idx:
        v = vis[k]
        rows.append({"i": k, "narration_fa": items[k]["narration_fa"],
                     "previous_fa": items[k - 1]["narration_fa"] if k else "",
                     "plan": {"event_en": v.get("event_en", ""), "location": v.get("location", ""),
                              "characters_present": v.get("characters_present", []), "people_en": v.get("people_en", ""),
                              "must_show": v.get("must_show", []), "creatures_en": v.get("creatures_en", []),
                              "others": v.get("others", []),
                              "roles": v.get("roles", {}), "visual_en": v.get("visual_en", "")}})
    source_block = f"\nSOURCE TEXT (the truth about who is where and when):\n{src['text']}\n" if src else ""
    prompt = f"""You are a strict script supervisor. Below are picture PLANS (text only) for a video about the novel
"{book['title_en']}" by {book['author_en']}. Each plan will be drawn by an image model while its Persian narration part is
read aloud. Find the plans that have a mistake and rewrite them. Nothing is drawn yet, so every mistake you fix now saves
a wasted picture.
{source_block}
Main characters (name: how they look): {json.dumps(names, ensure_ascii=False)}

Check EVERY plan:
1. PEOPLE COUNT: "people_en", "characters_present" and "others" together name exactly the people that the narration (and
   the source) says, no more, no fewer. "a mother and her child" is ONE adult and ONE child; when the source says she has
   one child, a second child never appears; no crowd, helper or family member that the part does not mention. \"visual_en\" must not contain ANY person that is
   not counted in \"people_en\" (no passers-by, servants, onlookers, figures far away, faces at windows).
2. PLACE AND EVENT: "event_en" and "visual_en" show exactly what HAPPENS in the narration, in the place where it happens
   (inside/outside, kind of place, time, weather); never a calm unrelated scene when something happens.
3. MUST_SHOW: every item of "must_show" is written in "visual_en".
4. WHO TO WHOM: "roles" are right (caregiver/patient, rescuer/rescued, killer/victim, speaker/listener never swapped); the
   rescuer, helper or attacker is a DIFFERENT kind of person (age, gender, build) from the person he or she acts on.
5. TWO FACES AT MOST: at most 2 people have a large clear face; every other person is a small figure far away or seen
   from behind, and "visual_en" says it.
6. Nothing modern in an old story; no names, no text, no logos in "visual_en".
7. PLACE DETAILS: "visual_en" names at least 8 concrete details of the place (objects, furniture, machines, architecture,
   materials, weather, season, light, atmosphere) that belong to THIS event and its mood, taken from the sentence, the source
   and the place; a vague or generic place is rewritten with them.
11. MOMENT MATCH: "visual_en" shows the one most important moment of the narration with the visible RESULT of the action, and
   every concrete thing that the narration names (people and their number, objects, place, weather, time of day); a calm or
   symbolic picture of a part in which something happens is rewritten.
12. FIGURATIVE SENTENCES: when the narration uses a figure of speech (a metaphor, a personification, an idiom, a euphemism), the
   picture shows the REAL event behind it, never the literal impossible image.
16. EVENT AND CREATURE SEEN: when the narration tells an attack, a chase, a capture, a trap, a death or names a creature
   (a dinosaur, a wolf, a monster), the plan shows that EVENT with the creature drawn big and clearly in "creatures_en" and
   "visual_en" (never only the people calmly standing, never only a shadow or a far away dot); a death is shown by its result
   without gore. Fix a plan that shows only characters.
17. PLACE MATCH: the place drawn is the one the narration names or implies (the jungle, the paddock fence in the storm, the
   control room, the laboratory), never a generic room; fix a plan whose place does not fit the sentence.
15. HANDS: at most two hands are clearly visible in the whole picture; people are posed with hidden hands (pockets, sleeves,
   behind the back, under a table or cloak) instead of crossed arms, hand holding, handshakes or hugs; fix a pose that shows
   many hands; nobody has a missing arm or hand (a hidden hand is in a pocket or on an object).
14. AGE FIT: nobody who looks like a child is a bride, a groom, a lover or in a wedding scene; people who marry, are engaged
   or have children are drawn as grown people (at least 18), a child looks like a child, a parent looks older than his or
   her child; fix a mismatch.
13. CLOTHES FIT THE SITUATION: nobody wears pajamas, a nightgown or a bathrobe unless the narration says that he or she goes to
   bed, sleeps, has just woken up or lies ill in bed; everybody wears the day or work clothes of the era and job.
9. CLOTHES FIT THE JOB: the clothes of every person in "visual_en" and "others" match his or her job and class (a uniform
   only on a soldier or guard); fix a mismatch.
10. KILLING AND GHOSTS: a death by a knife shows the knife and a small red stain at the wound (not gory), never a person who
   just sleeps; a ghost or spirit is a translucent glowing figure shaped like the person, not a halo or a light.
8. BODIES: every person has one head, two arms, two hands; nothing is planned that could show a missing head or an extra hand.
For a plan WITHOUT a mistake return {{"i": n, "ok": true}}. For a plan WITH a mistake return {{"i": n, "ok": false,
"problem": "short", "fix": {{...}}}} where "fix" holds ONLY the fields that change, among: "event_en", "visual_en"
(English, 90 to 110 words, starts with the shot type, written again completely whenever the people or the event change),
"people_en", "must_show", "characters_present" (names exactly as in the characters), "others" (one short English
description per unnamed person), "roles".
Return JSON {{"checks": [...]}} with one item per plan, in the same order.
Plans: {json.dumps(rows, ensure_ascii=False)}"""
    try:
        out = gemini_json(prompt, minutes=8, models=WRITE_MODELS)
    except GeminiQuotaExhausted:
        raise
    except GeminiBlocked as e:
        print("Plan review skipped (blocked):", e, flush=True)
        return vis
    except Exception as e:  # noqa: BLE001
        print("Plan review skipped:", e, flush=True)
        return vis
    fixed = 0
    for c in out.get("checks") or []:
        try:
            k = int(c.get("i"))
        except (TypeError, ValueError):
            continue
        fx = c.get("fix")
        if k not in idx or c.get("ok") is not False or not isinstance(fx, dict):
            continue
        v = vis[k]
        if isinstance(fx.get("visual_en"), str) and len(fx["visual_en"].split()) >= 25:
            v["visual_en"] = " ".join(fx["visual_en"].split())
        for key in ("event_en", "people_en"):
            if isinstance(fx.get(key), str) and fx[key].strip():
                v[key] = " ".join(fx[key].split())
        if isinstance(fx.get("must_show"), list):
            v["must_show"] = _str_list(fx["must_show"], 5) or v.get("must_show", [])
        if isinstance(fx.get("others"), list):
            v["others"] = _str_list(fx["others"], 3)
        if isinstance(fx.get("creatures_en"), list):
            v["creatures_en"] = _str_list(fx["creatures_en"], 3)
        if isinstance(fx.get("roles"), dict):
            v["roles"] = fx["roles"]
        if isinstance(fx.get("characters_present"), list):
            ok = [n for n in fx["characters_present"] if n in names]
            v["characters_present"] = ok[:MAX_PEOPLE]
        fixed += 1
        print(f"Plan review: picture plan {k} rewritten - {str(c.get('problem') or '')[:120]}", flush=True)
    print(f"Plan review: {len(idx)} plans read, {fixed} rewritten before drawing.", flush=True)
    return vis


PLAN_CHUNK = int(os.environ.get("AZIM_PLAN_CHUNK") or 14)


def _plain_plan(it, scenes):
    """A part whose plan could not be made: a plain picture from the script of its own paragraph (no Gemini call)."""
    sc_ = scenes[it["scene"]] if it["scene"] < len(scenes) else {}
    act = " ".join(str(sc_.get("action_en") or "").split())
    return {"visual_en": f"Medium-wide shot. {act} Place: {' '.join(str(sc_.get('setting_en') or '').split())}",
            "event_en": act, "characters_present": sc_.get("characters_present") or [], "kind": "event"}


def plan_in_parts(prompt, items, scenes):
    """The picture plan of a LONG video in one answer is cut off or has the wrong number of items, and then every picture falls
    back to a generic one. Up to PLAN_CHUNK parts it is ONE call (as before); more parts go in consecutive pieces. A piece that
    fails is asked once more; a piece that still fails gets plain pictures from the script of its own paragraph (no extra call)."""
    head, sep, _ = prompt.rpartition("Parts: ")
    n_all = len(items)

    def valid(out, n):
        return (isinstance(out, list) and len(out) == n and all(isinstance(v, dict) and isinstance(v.get("visual_en"), str)
                                                                  and v["visual_en"].strip() for v in out))

    def ask(sub):
        """The answer is matched by the number "i" of every item: a cut-off or short answer keeps what is good, and only the
        MISSING parts are asked again (one more call at most), so one bad answer no longer turns every picture of the video
        into the same plain picture of the characters."""
        got, pending = {}, list(sub)
        for attempt in range(2):
            p = head.replace(f"with exactly {n_all} items", f"with exactly {len(pending)} items") + sep + json.dumps(pending, ensure_ascii=False)
            try:
                out = gemini_json(p, minutes=10, models=WRITE_MODELS)["visuals"]
            except (GeminiBlocked, GeminiQuotaExhausted):
                raise
            except Exception as e:  # noqa: BLE001
                print(f"Picture plan piece failed ({str(e)[:80]}).", flush=True)
                out = None
            if isinstance(out, list):
                if valid(out, len(pending)):
                    for it, v in zip(pending, out):
                        got[it["i"]] = v
                else:
                    ids = {it["i"] for it in pending}
                    for v in out:
                        try:
                            k_ = int(v.get("i"))
                        except (AttributeError, TypeError, ValueError):
                            continue
                        if k_ in ids and isinstance(v.get("visual_en"), str) and v["visual_en"].strip():
                            got[k_] = v
                    print(f"Picture plan piece had {len(out)} items for {len(pending)} parts: {sum(1 for it in pending if it['i'] in got)} usable.",
                          flush=True)
            pending = [it for it in sub if it["i"] not in got]
            if not pending:
                return [got[it["i"]] for it in sub]
        if len(got) >= len(sub) // 2:        # most of the piece is good: the few missing parts get plain pictures
            return [got.get(it["i"]) for it in sub]
        return None
    if n_all <= PLAN_CHUNK:
        out = ask(items)
        if out is None:
            raise ValueError("the picture plan could not be made")
        return [v if v else _plain_plan(it, scenes) for v, it in zip(out, items)]
    result, k = [], 0
    pieces = max(1, round(n_all / PLAN_CHUNK))
    size = -(-n_all // pieces)
    while k < n_all:
        sub = items[k:k + size]
        out = ask(sub)
        if out is None:
            print(f"Picture plan: pieces {k}-{k + len(sub) - 1} use plain pictures of their paragraphs.", flush=True)
            out = [_plain_plan(it, scenes) for it in sub]
        result += [v if v else _plain_plan(it, scenes) for v, it in zip(out, sub)]
        k += len(sub)
    return result


_CONNECT = {"و", "با", "به", "از", "برای", "تا", "یا", "هم"}


def _name_patterns(chars):
    pats = []
    for c in chars:
        n = str(c.get("name") or "").strip() if isinstance(c, dict) else ""
        if not n:
            continue
        parts = [w_ for w_ in re.findall(r"[A-Za-z][A-Za-z'’\-]+", n) if len(w_) >= 3 and w_.lower() not in ("the", "and", "von", "van")]
        pats.append((n, [re.compile(rf"(?<![A-Za-z]){re.escape(w_)}(?![A-Za-z])", re.I) for w_ in (parts or [n])]))
    return pats


def split_beats_by_names(beats, chars, per=None):
    """Free check in code (no Gemini call): one picture shows at most MAX_PEOPLE people with a clear face. When one part of the
    narration names MORE characters than that, the part is cut into several pictures (before the name that would be the third),
    so every character whose name is spoken is also SEEN while his or her name is spoken."""
    per = per or MAX_PEOPLE
    pats = _name_patterns(chars)
    if not pats:
        return beats
    out = []
    for b in beats:
        words = b.split()
        trig = []
        for w_ in words:
            hit = None
            for n, ps in pats:
                if any(p_.search(w_) for p_ in ps):
                    hit = n
                    break
            trig.append(hit)
        if len(set(t_ for t_ in trig if t_)) <= per or len(words) < 8:
            out.append(b)
            continue
        pieces, start, seen = [], 0, []
        for i, w_ in enumerate(words):
            t_ = trig[i]
            if t_ and t_ not in seen and len(seen) == per:
                cut = i
                while cut - 1 > start and words[cut - 1].strip("،,.") in _CONNECT:
                    cut -= 1
                pieces.append(words[start:cut])
                start, seen = cut, []
                for k_ in range(cut, i):
                    if trig[k_] and trig[k_] not in seen:
                        seen.append(trig[k_])
            if t_ and t_ not in seen:
                seen.append(t_)
        pieces.append(words[start:])
        merged = []
        for pc in pieces:
            if merged and len(pc) < 3:
                merged[-1] = merged[-1] + pc
            else:
                merged.append(pc)
        if len(merged) > 1 and len(merged[0]) < 3:
            merged[1] = merged[0] + merged[1]
            merged.pop(0)
        out.extend(" ".join(m_) for m_ in merged if m_)
    return out or beats


def align_visuals(data, book, src=None):
    """Step D: write ONE picture for EVERY beat (about 2 sentences) of the FINAL narration (skipped if it fails).
    Every picture shows the REAL place of ITS moment of the story (taken from the source and from a fixed list of
    places), with rich foreground / middle / background details, and the people in it are large enough for clear faces."""
    scenes = data["scenes"]
    chars = data.get("characters", [])
    places = data.get("locations") or []
    if not places and src:
        places = design_places(book, src, data.get("era_en", ""))
        data["locations"] = places
    items, ctx = [], []
    for i, s in enumerate(scenes):
        s.pop("beat_visuals", None)
        if s.get("cast_names"):
            sents = split_sentences(s["narration_fa"])
            if len(sents) == len(s["cast_names"]):
                s["beats"] = sents
                s["beat_visuals"] = [cast_visual(chars, n) for n in s["cast_names"]]
                continue
            s.pop("cast_names")                       # sentences were changed: treat as an ordinary paragraph
        s["beats"] = split_beats_by_names(make_beats(s["narration_fa"]), chars)
        ctx.append({"scene": i, "part": s.get("part", "story"), "full_paragraph_fa": s["narration_fa"], "place_hint_en": s.get("setting_en", ""),
                    "mood_hint_en": s.get("mood_en", ""), "source_en": s.get("source_en", "")})
        for j, b in enumerate(s["beats"]):
            items.append({"i": len(items), "scene": i, "part": s.get("part", "story"), "narration_fa": b})
    for k, it in enumerate(items):                     # neighbours: pronouns ("he", "she") and the place continue from them
        it["previous_part_fa"] = items[k - 1]["narration_fa"] if k else ""
        it["next_part_fa"] = items[k + 1]["narration_fa"] if k + 1 < len(items) else ""
    place_ids = [x["id"] for x in places]
    source_block = f"\nSOURCE TEXT (the truth about who is where and when):\n{src['text']}\n" if src else ""
    prompt = f"""You are a film storyboard artist and a production designer. Below are the FINAL Persian narration parts, in
order, of a video that summarizes the book "{book['title_en']}" by {book['author_en']} (era and place: {data.get('era_en', '')}).
While each part is read aloud (about 10 seconds), ONE vertical picture is shown. For EACH part write the picture so that
it illustrates the exact content of THAT part: a viewer who sees only the picture understands what is being said and
where it happens at that moment.
{source_block}
PLACES OF THE STORY (fixed, always drawn the same way): {json.dumps(places, ensure_ascii=False)}

Return JSON: {{"visuals": [{{"i": 0, "text_en": "...", "kind": "event", "location": "...", "characters_present": ["..."], "shot": "...", "event_en": "...", "must_show": ["..."], "creatures_en": ["..."], "people_en": "...", "others": ["..."], "roles": {{"Name": "..."}}, "time_en": "", "emotion_en": "...", "visual_en": "...", "gentle_en": "..."}}, ...]}}
with exactly {len(items)} items, in the same order.

Rules:
- "text_en": a faithful English translation of THIS part, only what the Persian says (nothing added, nothing left out).
  Write it FIRST: the picture is drawn from this text, not from your idea of the book.
- "kind": ALWAYS "event", whatever the part talks about: the opening (also when it names the novel and its author: draw the
  first place of the story with the main hero), the heroes, the story AND the closing wrap-up (a wrap-up sentence about the
  whole story or its central idea is drawn as an event: the main hero in the key situation of the story, in its main place).
  Never draw a writer, a writing desk or a book.
- "location": the id of the place (from PLACES OF THE STORY: {json.dumps(place_ids)}) where THIS part really happens.
  FIRST read the sentence: when it names or clearly implies a place (a kitchen, a river, a ship's deck, a courtroom, a road,
  a field, a bedroom...), that is the place and "visual_en" draws exactly that place; the fixed list only lends its
  materials and details when it holds the same kind of place. When the sentence names no place, the story is still where the
  previous part was (use "previous_part_fa" and the paragraph); it changes only when the story moves (a journey, a new
  chapter, a flashback). Never pick a place only because it looks nice, never a generic place. If none of the listed
  places fits, write "other" and describe the real place in "visual_en".
- "event_en": ONE English sentence (6 to 14 words) with the central EVENT of this part, as a picture: who does or
  suffers what (for example "a woman is pulled out of the river by two fishermen"). This is the first thing a viewer must
  see. Take it from the sentence itself: what HAPPENS in the sentence (a death, a fight, a meeting, a departure, a
  discovery, a betrayal, a wedding, a trial...) is drawn as that very event, never replaced by a calm unrelated scene or
  a symbol. A death or a killing is shown in a storybook way that is unmistakable: the body collapsed on the ground or on the bed,
  the WEAPON of the story visible (a knife in the hand of the killer, in the body or fallen beside it), a small dark red
  stain on the clothes at the wound, the shocked or grieving people around, a dark mood; the wound is clear but never
  gory (no open flesh, no pools of blood). A person killed by a knife is never drawn as someone who just sleeps.
  GHOSTS AND SPIRITS: a ghost, soul or apparition is drawn as a pale, translucent, softly glowing FULL FIGURE with the
  shape, face and clothes of that person (he or she is counted in "people_en"), floating or standing; never only a halo,
  a light or a mist.
- "must_show": 3 to 5 short English phrases: the concrete things that the sentences of THIS part name or clearly imply
  (people with their NUMBER, objects, features of the place, the action). Every one of them must be visible in the picture,
  so "visual_en" contains all of them. Never add anything that the part does not say.
- "people_en": ONE English sentence with the EXACT number and kind of people who are in the picture, counted from the
  sentence and from the SOURCE TEXT, ending with "nobody else" (for example "exactly 2 people: the young mother and her only
  child, a small boy, nobody else"). «a mother and her child» is ONE adult and ONE child; when the source says that she has
  one child, a second child is NEVER drawn; a crowd, a family or any other person appears only when the part says so.
  Kind "author": "exactly one person, seen only from behind".
- "others": a list with ONE short English description for EVERY person of the picture who is NOT in "characters_present"
  (a rescuer, a doctor, a neighbour, a soldier, a child of the crowd...): gender, age, hair color, build and clothes (color
  and kind) that fit his or her JOB (a soldier in uniform, a doctor in a dark coat; never a uniform on a non-soldier). Each of them must look completely different from every named character and from each other: another hair
  color, other clothes colors and another age or build (the rescuer never looks like the person who is rescued, a stranger
  never looks like the hero). Empty list when there is nobody else.
- PICTURE BY KIND. "event": as described above (also for the opening, the heroes part and the wrap-up). "author": the world of
  the WRITER in the time when the book was written (not the time of the story): a writing desk with an inkwell, a pen and
  manuscript pages, a lamp, a window that shows the writer's town and country, the writer seen ONLY from behind or as a
  silhouette (never a face, never a portrait of a real person); write that time and place in "time_en". "time_en" is empty
  for "event".
- "emotion_en": the FEELING of the people in THIS moment, from the situation and the place, as what the FACE shows: for each
  person with a visible face 6 to 14 English words, e.g. "the mother: wide frightened eyes, raised eyebrows, trembling
  mouth; the boy: tears on his cheeks, lowered eyebrows". Use the right face for the feeling: worry (eyebrows drawn together
  and up, tense lips), fear (wide eyes, open mouth, pale face), joy (a real smile, raised cheeks, bright eyes), sadness (lowered
  eyebrow corners, wet eyes, downturned mouth), anger, shock, relief, calm. The feeling comes from the sentence and from the
  mood of the place (a happy meeting is a bright place with smiles; a loss is a dim place with sad faces); never a neutral
  face unless the moment is neutral.
- "roles": an object that maps EVERY name in "characters_present" to 4 to 12 English words: his or her role in THIS picture and
  what that person does, WHO does it to WHOM, exactly as the sentence says (for example "the mother, sitting at the bed
  and nursing her sick daughter" and "the sick daughter, lying in the bed"). Never swap who gives and who receives
  (caregiver and patient, parent and child, killer and victim, speaker and listener).
- "visual_en": English, 90 to 110 words. It STARTS with the shot type, then the event of "event_en" with the people doing it,
  then the PLACE in three layers: (1) the foreground: 4 concrete objects of THIS moment, each with its color and state; (2) the
  middle ground: the architecture or landscape around the people with its materials, colors and 6 to 8 characteristic details
  copied from the place description of the chosen location (furniture, machines, windows, doors, trees, walls, floor, tools...);
  (3) the background: what is far away, the sky, the weather. Then the time of day, the season, the direction and color of the
  light and the atmosphere (dust, mist, rain, smoke, steam, reflections, sparks, shadows). Write it DENSE and telegraphic
  (nouns with their colors and states, no filler words such as "there is"): the picture text has a strict length limit, so
  the most important things come FIRST (the event, then what the people do, then the foreground, middle ground and
  background) and the light and atmosphere come last in a few words. NO unnamed people in
  the background unless the part itself names them (a crowd, soldiers, villagers...); then they are counted in
  \"people_en\". Otherwise \"visual_en\" ends with: no other person anywhere in the picture. Everything belongs to the era of the story; nothing modern in an old story.
- MOMENT MATCH (very important): the picture shows the ONE most important moment of the part: the event told by its main verb,
  at the instant when it is easiest to see (the door opening, the hand pushing, the ship exploding), and the RESULT of the action
  is visible (an open door, a fallen cup, smoke, a broken window). Every concrete thing that the Persian part names (people and
  their number, objects, animals, vehicles, place, weather, time of day, season, direction of the movement) is visible. A
  thought, a plan or a feeling is shown by the faces, the pose and a concrete object of the story, never by a symbol or a calm
  unrelated scene. When a part tells two events, draw the later and more important one, with the trace of the first one.
- FIGURATIVE SENTENCES: when the part says something with a figure of speech (a metaphor, a personification, an idiom, a
  euphemism: for example a ship that «kills itself»), draw the REAL event behind it, judged from "text_en", the SOURCE TEXT and
  the story, never the literal impossible image.
- AGE FIT: every person is drawn at the age that fits his or her ROLE in THIS moment (parent, bride, groom, worker, child,
  old person); a child is NEVER drawn as a bride, a groom, a lover or in a wedding dress or a wedding scene: a person who
  marries or is engaged is drawn as a young adult of at least 18 years.
- NON-HUMAN HEROES: a character whose sheet has "nonhuman": true (a shape, an animal, a robot, a creature) is drawn as WHAT IT
  IS, never as a person: no clothes, hands or hair unless its sheet says so; it keeps its own shape, size and colors in every
  picture; "people_en" counts it as a character («exactly 2 characters: Square (a flat square), Hex (a hexagon), nobody
  else»); its pose and its feeling are shown by its body, its eyes and its mouth. The rules about hands, clothes and ages
  below are for human characters only.
- CREATURES AND THINGS OF THE STORY (very important): every creature, vehicle, machine, cage, fence, weapon or other thing that
  the narration names or implies and that matters for the event (a dinosaur, a monster, a wolf, a ship, a cage) is listed in
  "must_show" and DRAWN, large and clearly, in the place where it is. In a story about dinosaurs the dinosaurs appear in every
  picture that speaks of them: never a picture of only the people while the sentence tells about a creature.
- EVENTS MUST BE SEEN: when the narration tells an attack, a chase, a fall, a capture, a trap, a death, a rescue, a fight or any
  other EVENT, the picture shows that very event happening (the attacker, the victim in danger, the movement, the place), not
  the people standing calmly. A violent event is shown by the THREAT and its RESULT without gore: the huge creature lunging
  with its mouth open and the people fleeing; a person trapped behind the bars of a cage; the creature rearing over the
  empty place where the person stood, a fallen hat or bag on the ground; the mourners with bowed heads, a lit candle and a flower with the creature far away; a hanging is shown ONLY by an
  empty rope above a toppled chair.
- "creatures_en": the creatures, animals, monsters, vehicles, machines, cages, fences or other THINGS that are the subject of the
  event of this part or that the part names, and that are NOT in the list of characters (a dinosaur, a wolf, a jeep, a
  cage...): each one described in 12 to 25 English words (what it is, its size compared with the people, colors, pose and
  where it is). The picture program draws them FIRST, large and clearly visible; [] only when the part has none. In a story
  about dinosaurs EVERY part that speaks of a dinosaur has it here (or among the characters), drawn big.
- PLACE MATCH (very important): the place of the picture is the place that THIS sentence names or implies (the jungle, the
  fence of the paddock at night in the storm, the control room, the laboratory, a road, a cage, a river), with its own
  materials, plants, weather, light and objects, never a generic room or the place of another part; the first words of
  "visual_en" after the shot name that place.
- "gentle_en" (ONLY for a part with an attack, a fight, a death, a trap, a chase or other danger; "" for every calm part, so the
  answer stays short): English, 50 to 80 words: the SAME event, the same creature(s), people and place (with the main details of the place and the light) as
  "visual_en", but written in the gentlest way a children's picture book would show it, so that a strict safety filter accepts it:
  no blood, no wounds, no dead body, no teeth touching a person, nobody being eaten or hit; the danger is shown by the
  creature ITSELF drawn big and clearly visible (roaring, charging, rearing, looming over the place, its whole head and body
  seen), people running, hiding, climbing or behind bars, and fear on the faces. The creature is NEVER reduced to a shadow, a
  far away dot or something outside the picture.
- NO BODIES (very important): never draw a dead, lying, unconscious or sleeping-on-the-ground person, a body under a cloth or
  a sheet, or a person hanging. A death, a murder, a suicide or an accident is told by what the LIVING do and see: grief on
  their faces, a lit candle, a flower, an empty chair or bed, a dropped hat or bag, an empty place; a hanging is shown only by
  an empty rope above a toppled chair. Everybody in every picture is alive, awake and upright (standing or sitting), except a
  person who sleeps or is ill in a BED when the sentence says so.
- SAME SCALE: all people of one picture are drawn at ONE consistent scale: adults about equally tall at the same distance, a
  child clearly smaller, nobody a giant or a dwarf next to the others; the heads of everybody have the same natural size
  relative to the bodies.
- BEAUTIFUL PLACES: every place is attractive and atmospheric: strong tonal contrast from deep black to white, depth
  (foreground, middle ground, background), dramatic light and shadow and MANY fine concrete details (at least five objects,
  textures, plants, architecture, tools of the place); never a bare or dull backdrop. Every character is striking and
  attractive (a beautiful face, a graceful body, a detailed costume).
- HANDS (the picture model often draws a third hand or loses one): every person has EXACTLY two arms and two hands: each arm
  ends in ONE hand, and every hand is either calmly visible (resting at the side, on the lap, on a table, or holding one object
  against the body) or hidden behind a pocket, a sleeve or an object; NEVER a missing hand, an arm without a hand, a third hand
  or a hand that belongs to nobody. People never touch each other with their hands: no hand holding, handshakes, hugs,
  pointing fingers, crossed arms or several hands on one object; the hands of different people stay apart.
- CLOTHES FIT THE SITUATION: everybody is dressed for THIS moment: day clothes or work clothes of the era and the job; a coat,
  hat, scarf or gloves where the weather is cold; nightwear ONLY when the part says that a person goes to bed, is asleep, has
  just woken up or lies ill in bed. Never pajamas, a nightgown or a bathrobe for a person who walks, talks, works, travels or
  fights.
- THE PLACE OF THE SENTENCE (very important): when the part says where it happens (a jungle, a control room, a cage, a laboratory, a fence, a road, a kitchen), that EXACT place is drawn, even when it is not in PLACES: then "location" is "new" and "visual_en" describes that place in full (materials, colors, objects, light). The nearest place of the list is chosen ONLY when it really is the same place. The event of the part (the attack, the escape, the capture, the death, the discovery) is the MAIN thing in the picture, shown as its moment and its visible result (the fence broken, the cage closed, the jaws open, a dropped hat or bag in the grass), never a quiet portrait of people standing.
- PLACE FITS THE EVENT (very important): the place is never a neutral backdrop; its details tell the same story as the
  event and its mood. Take them from the sentence, from the SOURCE TEXT and from the place description: the objects,
  furniture, tools, architecture, weather, season, time of day and light that THIS exact event needs (a deathbed: a bed
  with rumpled sheets, a candle, a medicine bottle, drawn curtains, grey morning light; a flight: an open door, a
  dropped bag, a dark road, the moon). Write them concretely (material, color, state: worn, broken, open, lit, wet), never
  as general words like "a sad room". Details never add a person. At least 2 items of "must_show" are such details.
- "shot" is exactly one of {json.dumps(SHOTS)}. Two neighbouring pictures NEVER use the same shot.
- {FRAMING}
- {POSE}
- EVERY CHARACTER OF THE SENTENCE IS IN THE PICTURE: every character who is named in the part, or is meant by "he", "she",
  "they", "his", "her" or "the two of them" (look at "previous_part_fa" to know who is meant), is listed in
  "characters_present" (names exactly as in the characters list) AND is doing something visible in the picture. When the
  sentence names more than 2 characters, list the 2 most important (only they get large clear faces) and show the
  others in "others" as small figures far away or seen from behind, or as a small crowd. Write only what
  the characters DO ("the boy sits alone on a bench"): their fixed look is added automatically, so never write their
  faces, ages, bodies or clothes, and never write names or book titles in the picture description.
- LEGS: when 2 or more people stand or walk close together, use a medium shot from the waist up or hide the legs behind
  furniture, a table, a wall or a cart; full-body shots only for ONE person (crossed or overlapping legs of several people
  are drawn wrongly: a third leg appears).
- HANDS (very important, the picture model often draws a third hand): plan every person with hands that are EASY to draw:
  either both hands clearly separate and doing ONE simple action (holding one object, resting on a table or a knee, covering
  the face), or hands hidden (behind the back, in pockets or sleeves, under a shawl or a table, folded in the lap). Never plan
  two people holding or touching the same object or each other with their hands, never crossed, overlapping or interlocked
  arms, never more than one object in a person's hands, never a hand in front of the face of another person.
- BODIES: every person in the picture is a complete human: one head, two arms, two hands, two legs. When hands matter
  (writing, holding, pulling), say which hand holds what. Never plan a picture in which a body is hidden so that a head or
  a hand could look missing or doubled.
- TWO FACES AT MOST: in one picture at most 2 people have a large, clear face. Every other person (named or not) is a
  small figure far in the background or seen from behind, and "visual_en" says so. Only the people that the part really
  names or implies are drawn: never add a child, a crowd or a helper that the part does not mention.
- VARIETY: when several parts are about the SAME place, the place stays the same (same walls, same objects, same
  landscape) but every picture shows another part of it, another angle, other light and other foreground objects; the event
  of its own sentence always stays the main thing.
- No text, letters, numbers, charts or logos. No real celebrities. No scenes copied from films.
- Do not write an art style (it is added separately).

Characters: {json.dumps(chars, ensure_ascii=False)}
Paragraphs (context): {json.dumps(ctx, ensure_ascii=False)}
Parts: {json.dumps(items, ensure_ascii=False)}"""
    try:
        vis = plan_in_parts(prompt, items, scenes)
        if len(vis) == len(items) and all(isinstance(v.get("visual_en"), str) and v["visual_en"].strip()
                                          for v in vis):
            vis = [{"visual_en": v["visual_en"].strip(), "shot": str(v.get("shot") or "").strip().lower(),
                    "event_en": " ".join(str(v.get("event_en") or "").split()),
                    "location": str(v.get("location") or "").strip(),
                    "characters_present": v.get("characters_present") or [],
                    "kind": str(v.get("kind") or "event").strip().lower(),
                    "must_show": _str_list(v.get("must_show"), 5),
                    "people_en": " ".join(str(v.get("people_en") or "").split()),
                    "others": _str_list(v.get("others"), 3),
                    "time_en": " ".join(str(v.get("time_en") or "").split()),
                    "emotion_en": " ".join(str(v.get("emotion_en") or "").split()),
                    "gentle_en": " ".join(str(v.get("gentle_en") or "").split()),
                    "creatures_en": _str_list(v.get("creatures_en"), 3),
                    "roles": v.get("roles") if isinstance(v.get("roles"), dict) else {}} for v in vis]
            for v, it in zip(vis, items):      # ONLY the author part is a writer picture: every other part is a story picture
                v["kind"] = "event"
            vis = diversify_visuals(vis, items, book)
            for v, it in zip(vis, items):      # a character NAMED in the sentence is always in the picture
                v["characters_present"] = force_named_characters(v["characters_present"], it["narration_fa"], chars)
            vis = review_visual_plan(vis, items, book, chars, src)        # text-only check BEFORE drawing
            for v, it in zip(vis, items):
                v["characters_present"] = force_named_characters(v["characters_present"], it["narration_fa"], chars)
            fix_others_in_code(vis, chars)                                # free check in code: unnamed people look different
            k = 0
            for s in scenes:
                if s.get("cast_names"):
                    continue
                n = len(s["beats"])
                s["beat_visuals"] = [{"visual_en": v["visual_en"], "event_en": v.get("event_en", ""),
                                      "location": v.get("location", ""),
                                      "characters_present": v["characters_present"],
                                      "kind": v.get("kind", "event"), "must_show": v.get("must_show", []),
                                      "people_en": v.get("people_en", ""), "others": v.get("others", []),
                                      "time_en": v.get("time_en", ""),
                                      "emotion_en": v.get("emotion_en", ""),
                                      "gentle_en": v.get("gentle_en", ""),
                                      "creatures_en": v.get("creatures_en", []),
                                      "roles": v.get("roles", {})} for v in vis[k:k + n]]
                k += n
            data["visual_version"] = VISUAL_VERSION
            print(f"Visuals aligned: {len(items)} pictures for {len(scenes)} paragraphs, {len(places)} fixed places.",
                  flush=True)
    except (GeminiBlocked, GeminiQuotaExhausted):
        raise
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
        ref = reference_wav(kind)
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
(noun of noun of noun), avoid commas inside noun phrases, keep any English-letter name exactly as it is, no other
Latin letters, no digits, no brackets. Keep all vowel marks (diacritics) of the words you keep.
Return JSON: {{"text": "..."}}
Sentence: {text}"""
    try:
        out = gemini_json(prompt, minutes=3, models=CHEAP_MODELS).get("text", "").strip()
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
    text = force_marks(text)
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


class GeminiTTSUnavailable(RuntimeError):
    """Gemini TTS cannot be used any more in this run (daily quota finished): use Pocket TTS."""


_gtts = {"dead": False, "fails": 0, "model": None}
_gtts_dead_models = set()


def gemini_tts_ready():
    return TTS_ENGINE == "gemini" and not _gtts["dead"] and _gtts["fails"] < 3


def tts_style(scene):
    """The delivery style of a scene: the base narrator style + the mood of the picture of this scene."""
    mood = str(scene.get("mood_en") or "").strip()
    return GEMINI_TTS_STYLE + (f". The feeling of this part: {mood}." if mood else "")


def gemini_tts(text, kind, out_wav, style=""):
    """One paragraph -> WAV (24 kHz mono) with Gemini TTS. The text is read verbatim; the delivery goes in
    speech_metadata.style (never inside the text). Persian words and English names are read in one pass."""
    content = {"type": "text", "text": text}
    if style:
        content["annotations"] = [{"type": "speech_metadata", "style": style}]
    body = {
        "model": _gtts.get("model") or GEMINI_TTS_MODEL,
        "input": [{"type": "user_input", "content": [content]}],
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": GEMINI_TTS_VOICES[kind]}]},
    }
    headers = {"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"}
    last = ""
    for attempt in range(3):
        try:
            CALLS["voice"] += 1
            r = requests.post("https://generativelanguage.googleapis.com/v1beta/interactions",
                              headers=headers, json=body, timeout=(30, 180))
            if r.status_code == 200:
                j = r.json()
                data = None
                for st in j.get("steps") or []:
                    if st.get("type") == "model_output":
                        for c in st.get("content") or []:
                            if c.get("type") == "audio" and c.get("data"):
                                data = c["data"]
                if not data:
                    data = (j.get("output_audio") or {}).get("data")
                if not data:
                    raise RuntimeError("the answer has no audio")
                out_wav.write_bytes(base64.b64decode(data))
                if out_wav.stat().st_size < 2000:
                    raise RuntimeError("empty audio")
                tts_usage_add(_gtts.get("model") or GEMINI_TTS_MODEL)
                return
            last = f"{r.status_code} {short_error(r.text)}"
            note(f"  Gemini TTS: {last}")
            if r.status_code == 429 and ("PerDay" in r.text or "limit: 0" in r.text):
                _gtts_dead_models.add(_gtts.get("model") or GEMINI_TTS_MODEL)
                if all(m in _gtts_dead_models for m in GEMINI_TTS_MODELS):
                    _gtts["dead"] = True
                raise GeminiTTSUnavailable(last)
            if r.status_code in (400, 404):
                break
        except GeminiTTSUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            last = str(e)
            note(f"  Gemini TTS error: {last[:120]}")
        time.sleep(15 * (attempt + 1))
    _gtts["fails"] += 1
    raise GeminiTTSUnavailable(f"Gemini TTS failed: {last[:120]}")


GEMINI_GROUP_WORDS = int(os.environ.get("AZIM_GEMINI_GROUP_WORDS") or 260)   # words per Gemini TTS request


def split_by_silence(path, weights, extras=None, min_gap=1.0, min_sil=0.3, window_min=4.0, win_frac=0.15):
    """Cuts ONE recording into consecutive pieces (paragraphs, or the sentences of a paragraph) whose texts have the given
    weights (characters). The expected place of every cut comes from the speech rate (solved from the whole length, with the
    expected pauses in "extras" seconds after every piece); then ALL cuts are chosen together (dynamic programming) among the
    detected pauses: close to the expected place, long pauses preferred, always in order. One wrong estimate can no longer
    push the voice several sentences ahead of the text. Returns [(start, end), ...] in seconds."""
    total = probe_duration(path)
    n = len(weights)
    if n <= 1:
        return [(0.0, total)]
    out = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af",
                          f"silencedetect=noise=-35dB:d={min_sil}", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", out)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", out)]
    sil = [((a + b) / 2, b - a) for a, b in zip(starts, ends)]
    extras = list(extras) if extras else [0.0] * n
    extras = (extras + [0.0] * n)[:n]
    wsum = float(sum(max(w, 1) for w in weights))
    rate = max((total - sum(extras[:-1])) / wsum, 1e-4)             # seconds of speech per character
    targets, acc = [], 0.0
    for w, ex in zip(weights[:-1], extras[:-1]):
        acc += max(w, 1) * rate + ex
        targets.append(min(max(acc, 0.0), total))
    window = max(window_min, win_frac * total)
    scale = max(window / 3.0, 0.3)
    cands = []
    for t in targets:
        c = [(mid, ln, 0.6 * abs(mid - t) / scale - 1.5 * min(ln, 1.0)) for mid, ln in sil if abs(mid - t) <= window]
        c.append((t, 0.0, 1.6))                                       # no pause near: the expected place itself
        cands.append(c)
    INF = float("inf")
    best = [[INF] * len(c) for c in cands]
    prev = [[-1] * len(c) for c in cands]
    for i, (mid, ln, cost) in enumerate(cands[0]):
        best[0][i] = cost
    for k in range(1, len(cands)):
        for i, (mid, ln, cost) in enumerate(cands[k]):
            for j, (pm, _, _) in enumerate(cands[k - 1]):
                if best[k - 1][j] < INF and mid >= pm + min_gap and best[k - 1][j] + cost < best[k][i]:
                    best[k][i] = best[k - 1][j] + cost
                    prev[k][i] = j
    cuts = None
    if best and min(best[-1]) < INF:
        i = min(range(len(best[-1])), key=lambda x: best[-1][x])
        cuts = []
        for k in range(len(cands) - 1, -1, -1):
            cuts.append(cands[k][i][0])
            i = prev[k][i]
        cuts.reverse()
    if cuts is None:                                                   # no consistent choice: the expected places
        cuts, last = [], 0.0
        for t in targets:
            last = max(t, last + min(min_gap, 0.2))
            cuts.append(min(last, total))
    edges = [0.0] + cuts + [total]
    return list(zip(edges[:-1], edges[1:]))


def paragraph_extras(texts):
    """Expected pauses (seconds) after every paragraph of one recording: a little after every sentence, more at its end."""
    return [0.35 * max(len(split_sentences(t)), 1) + 0.5 for t in texts]


# Voice-only writing of the short words that the Gemini voice swallows or merges («در این» -> «دین»/«این»,
# «را از» -> «از»). Only these words get vowel marks, only in the text sent to the voice: subtitles stay clean.
_PL = "\u0600-\u06FF\u200c"
_VOICE_WORDS = (("در", "دَر"), ("این", "اِین"), ("را", "رَا"), ("از", "اَز"))


# Words that the Gemini voice reads with a wrong vowel. The entry matches the word AND every form that starts with it
# (رسوا -> رسوا، رسوایی، رسواکننده...). Only the voice text gets the vowel mark: the subtitles stay clean.
_VOICE_PREFIX = (("رسوا", "رُسوا"), ("دسر", "دِسِر"), ("دلهره", "دلهُره"))
# «عمو» is read with fatha (عَمو) but «عمومی» keeps its damma: so only these exact word forms are marked.
def _dtol(word):
    """A pattern for a word that also matches it WITH vowel marks between the letters (a mark may already be there)."""
    return "".join(r"[\u200c ]?" if ch in " \u200c" else re.escape(ch) + r"[\u064B-\u0655\u0670]*" for ch in word)


_VOICE_REGEX = ((rf"(?<![\u0600-\u06FF]){_dtol('کشنده')}(?![{_PL}])", "کُوشَنده"),
                (rf"(?<![{_PL}])عمو(?=(?:ی|یی|\u200c?ها|\u200c?های|یم|یت|یش|یمان|یتان|یشان)?(?![{_PL}]))", "عَمو"),)


# Words that MUST be read with these exact vowel marks, by BOTH voices (Gemini and Pocket). The entry matches the word
# and every form that starts with it, even if the text already carries some vowel marks (so it also works on spoken_fa).
#   کندوکاو  -> first letter with fatha (کَندوکاو)
#   نورزیده  -> «ن» and «و» with fatha (نَوَرزیده), also نورزید / نورزیدند
#   ییلاق    -> the FIRST ی with kasra (یِیلاق), also ییلاقی / ییلاقات (the voice read it as «یالاق»)
_FORCE_MARKS = (("\u06cc\u06cc\u0644\u0627\u0642", "\u06cc\u0650\u06cc\u0644\u0627\u0642"), ("کندوکاو", "کَندوکاو"), ("نورزید", "نَوَرزید"),
                ("فرافکن", "فَرافْکَن"),
                ("ثروت", "سَروَت"), ("کشنده", "کُوشَنده"),
                ("هفتاد", "هَفتاد"), ("هشتاد", "هَشتاد"), ("شصت", "شَصت"), ("پنجاه", "پَنجاه"), ("چهل", "چِهِل"))      # ثروتمند / ثروت: the voice read «ث» as «ش»; «س» is the same sound      # فرافکنی / فرافکنانه...: the letter ک must be spoken (sukun on ف, fatha on ک)


# Words with a ZWNJ or a space inside (compounds): matched by a pattern; the lookahead keeps the ending of the word.
#   جیب‌بری: first ب with sukun, SECOND ب with damma (the voice read it with fatha: بَری)
#   پاک‌طینت: ک with sukun (no ezafe), «طینت» written with ت so that ط never gets a kasra of its own
#   توطئه: the voice read «توطیه»: written with ع (glottal stop) so that it is said tav-te-e
#   ورطه: «وَرْطه» (the voice said «وارطه»);  پشیمان: «پَ‌شیمان» (the voice said «پِشیمان»)
_FORCE_REGEX = ((rf"(?<![{_PL}]){_dtol('جیب‌بر')}(?=(?:ی|ان|ها|هایی?|ی\u200c?ها)?(?![{_PL}]))", "جیبْ\u200cبُر"),
                (rf"(?<![{_PL}]){_dtol('پاک‌طینت')}", "پاکْ\u200cتینَت"),
                (rf"(?<![{_PL}]){_dtol('توطئه')}", "تَوْتِعِه"),
                (rf"(?<![{_PL}]){_dtol('ورطه')}", "وَرْتِه"),
                (rf"(?<![{_PL}]){_dtol('پشیمان')}", "پَ\u200cشیمان"))


def force_marks(t):
    for pat, marked in _FORCE_REGEX:
        t = re.sub(pat, lambda m, mk=marked: mk, t)
    for plain, marked in _FORCE_MARKS:
        pat = rf"(?<![{_PL}])" + "".join(re.escape(ch) + r"[\u064B-\u0655\u0670]*" for ch in plain)
        t = re.sub(pat, lambda m, mk=marked: mk, t)
    return t


PRON_FILE = ROOT / "azim_pronunciation.json"
_PRON = {}


def load_pronunciation():
    """Words that the voice reads wrongly. The built-in list is the default; the file azim_pronunciation.json
    (keys: words = exact word, prefixes = the word and every form that starts with it, regex = pattern -> marked text;
    {PL} inside a pattern means 'a Persian letter') adds to it or replaces an entry. A bad entry is skipped."""
    words = dict(_VOICE_WORDS)
    prefixes = dict(_VOICE_PREFIX)
    regexes = {pat: mark for pat, mark in _VOICE_REGEX}
    try:
        if PRON_FILE.exists():
            d = json.loads(PRON_FILE.read_text(encoding="utf-8"))
            words.update({str(k): str(v) for k, v in (d.get("words") or {}).items()})
            prefixes.update({str(k): str(v) for k, v in (d.get("prefixes") or {}).items()})
            for pat, mark in (d.get("regex") or {}).items():
                pat = str(pat).replace("{PL}", _PL)
                try:
                    re.compile(pat)
                    regexes[pat] = str(mark)
                except re.error as e:
                    print(f"Pronunciation: bad pattern skipped ({e})", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"Pronunciation file ignored: {e}", flush=True)
    return words, prefixes, regexes


_VOICE_NAMES = {}


def spell_names_for_voice(t):
    """Every English name of the narration is replaced by its ONE Persian spelling (the map of the video), so that the
    voice says it the same way in every paragraph. A Latin word that is not in the map gets a fixed letter-by-letter spelling."""
    for k in sorted(_VOICE_NAMES, key=len, reverse=True):
        v = _VOICE_NAMES[k]
        if k and v:
            t = re.sub(rf"(?<![A-Za-z]){re.escape(k)}(?![A-Za-z])", lambda m, v=v: v, t)
    return re.sub(r"[A-Za-z][A-Za-z'’\-]*", lambda m: latin_to_fa(m.group(0)), t)


def gemini_voice_text(t):
    t = spell_names_for_voice(t)
    t = fix_numbers(t)
    if not _PRON:
        _PRON["rules"] = load_pronunciation()
        w, pf, rx = _PRON["rules"]
        print(f"Pronunciation rules: {len(w)} words, {len(pf)} prefixes, {len(rx)} patterns", flush=True)
    words, prefixes, regexes = _PRON["rules"]
    for plain, marked in words.items():
        t = re.sub(rf"(?<![{_PL}]){re.escape(plain)}(?![{_PL}])", marked, t)
    for plain, marked in prefixes.items():
        t = re.sub(rf"(?<![{_PL}]){re.escape(plain)}", marked, t)
    for pattern, marked in regexes.items():
        t = re.sub(pattern, marked, t)
    return force_marks(t)


def gemini_voice_groups(scenes, kind):
    """The free Gemini TTS allows only 10 requests a day: the paragraphs are read in a few GROUPS (about
    GEMINI_GROUP_WORDS words per request), then the recording is cut back into paragraphs at the pauses."""
    groups, cur, words = [], [], 0
    for i, sc in enumerate(scenes):
        w = len(sc["narration_fa"].split())
        if cur and words + w > GEMINI_GROUP_WORDS:
            groups.append(cur)
            cur, words = [], 0
        cur.append(i)
        words += w
    if cur:
        groups.append(cur)
    print(f"Gemini TTS: {len(groups)} request(s) for {len(scenes)} paragraphs", flush=True)
    for gi, idxs in enumerate(groups):
        texts = []
        for i in idxs:
            sc = scenes[i]
            texts.append(((sc.get("spoken_fa") or sc["narration_fa"]) if GEMINI_TTS_TEXT == "spoken"
                          else sc["narration_fa"]).strip())
        full = WORK / f"graw_{gi:02d}.wav"
        gemini_tts("\n\n".join(gemini_voice_text(x) for x in texts), kind, full, GEMINI_TTS_STYLE)
        cuts = split_by_silence(full, [max(len(t), 1) for t in texts], paragraph_extras(texts))
        note("  Gemini group %d: cuts at %s s" % (gi, ", ".join(f"{b:.1f}" for _, b in cuts[:-1])))
        for i, (a, b) in zip(idxs, cuts):
            sh(["ffmpeg", "-y", "-loglevel", "error", "-i", str(full), "-ss", f"{a:.2f}", "-to", f"{b:.2f}",
                str(WORK / f"raw_{i:02d}.wav")])
        if gi < len(groups) - 1:
            time.sleep(25)                       # free tier: 3 requests per minute
    verify_gemini_voice(scenes, kind)
    return [WORK / f"raw_{i:02d}.wav" for i in range(len(scenes))]


VOICE_FIX_MAX = int(os.environ.get("AZIM_VOICE_FIX_MAX") or 3)     # at most this many paragraphs are re-read per video


def voice_missing_words(wav, text):
    """Gemini listens to a paragraph and lists the words of the text that were skipped or replaced in the audio.
    Returns a list (empty = faithful) or None if the check is not available."""
    mp3 = wav.with_name(wav.stem + "_chk.mp3")
    try:
        sh(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-ac", "1", "-b:a", "48k", str(mp3)])
        prompt = f"""Listen to this Persian audio. It should be the reading of exactly this text:
"{text}"
List the words of the text that are NOT spoken in the audio, or that are replaced by other words. Ignore vowel marks,
small pronunciation differences, pauses, and the way English names are pronounced. Do not list words that are spoken.
Return JSON: {{"missing": ["word", ...]}} (an empty list if the reading is faithful)."""
        r = gemini_json(prompt, minutes=3, models=CHEAP_MODELS, audio=mp3.read_bytes())
        time.sleep(3)
        return [str(w) for w in (r.get("missing") or []) if str(w).strip()]
    except Exception as e:  # noqa: BLE001
        note(f"  Voice check unavailable: {str(e)[:100]}")
        return None


def verify_gemini_voice(scenes, kind):
    """A paragraph in which words were skipped is read again alone (up to VOICE_FIX_MAX per video). The new reading
    replaces the old one only if it misses fewer words. Any problem here never stops the video."""
    fixes, unavailable = 0, 0
    for i, sc in enumerate(scenes):
        if fixes >= VOICE_FIX_MAX or unavailable >= 2:
            break
        wav = WORK / f"raw_{i:02d}.wav"
        plain = ((sc.get("spoken_fa") or sc["narration_fa"]) if GEMINI_TTS_TEXT == "spoken"
                 else sc["narration_fa"]).strip()
        miss = voice_missing_words(wav, plain)
        if miss is None:
            unavailable += 1
            continue
        if not miss:
            continue
        note(f"  Voice check: paragraph {i} skipped or changed: {', '.join(miss[:6])}")
        if not gemini_tts_ready() or tts_left(_gtts.get("model") or GEMINI_TTS_MODEL) < 1:
            break
        new = WORK / f"fix_{i:02d}.wav"
        try:
            time.sleep(25)                                  # free tier: 3 requests per minute
            gemini_tts(gemini_voice_text(plain), kind, new, GEMINI_TTS_STYLE)
        except GeminiTTSUnavailable as e:
            note(f"  Re-read stopped: {str(e)[:100]}")
            break
        fixes += 1
        miss2 = voice_missing_words(new, plain)
        if miss2 is not None and len(miss2) < len(miss):
            shutil.copy(new, wav)
            note(f"  Paragraph {i}: the new reading is better and is used.")


def reference_wav(kind):
    """The 5-second reference recording of a voice (built from azim_voices_data.py if the WAV is not in the repo)."""
    ref = REF_AUDIO[kind].resolve()
    if not ref.exists():
        try:
            import azim_voices_data
            raw = base64.b64decode(getattr(azim_voices_data, f"{kind.upper()}_FLAC_B64"))
            flac = WORK / f"azim_ref_{kind}.flac"
            flac.write_bytes(raw)
            ref = (WORK / f"azim_ref_{kind}.wav").resolve()
            sh(["ffmpeg", "-y", "-loglevel", "error", "-i", str(flac), "-ac", "1", "-ar", "24000", str(ref)])
        except ImportError:
            raise RuntimeError(f"Reference voice file not found: {ref}")
    return ref


class EnglishVoiceError(RuntimeError):
    """The English voice for names is not available: the scene falls back to Persian phonetic spelling."""


_en = {"fails": 0}


def english_tts(text, kind, out_wav):
    """English names are read by the English Pocket TTS model, cloned from the SAME reference recording, so the
    timbre stays the same as the Persian voice. Raises EnglishVoiceError if it cannot."""
    if _en["fails"] >= 3:
        raise EnglishVoiceError("English voice disabled after 3 failures")
    try:
        if "model" not in _en:
            from pocket_tts import TTSModel
            _en["model"] = TTSModel.load_model()
        model = _en["model"]
        if kind not in _en:
            _en[kind] = model.get_state_for_audio_prompt(str(reference_wav(kind)))
        t = text.strip()
        if t[-1] not in ".!?":
            t += "."
        audio = model.generate_audio(_en[kind], t)
        import numpy as np
        from scipy.io import wavfile
        arr = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
        wavfile.write(str(out_wav), int(model.sample_rate), arr)
        if not out_wav.exists() or out_wav.stat().st_size < 2000:
            raise RuntimeError("empty audio")
    except EnglishVoiceError:
        raise
    except Exception as e:  # noqa: BLE001
        _en["fails"] += 1
        note(f"  English voice failed for '{text[:40]}': {str(e)[:100]}")
        raise EnglishVoiceError(str(e))


def concat_wavs(files, out_wav, gap=0.04):
    """Join pieces (trimmed, same sample rate) with a very short gap."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    labels, n = [], 0
    for k, f in enumerate(files):
        cmd += ["-i", str(f)]
        labels.append(f"[{n}:a]{TRIM},aresample=24000,aformat=channel_layouts=mono[a{n}]")
        n += 1
        if k < len(files) - 1:
            cmd += ["-f", "lavfi", "-t", f"{gap}", "-i", "anullsrc=r=24000:cl=mono"]
            labels.append(f"[{n}:a]aresample=24000,aformat=channel_layouts=mono[a{n}]")
            n += 1
    ins = "".join(f"[a{i}]" for i in range(n))
    fc = ";".join(labels) + f";{ins}concat=n={n}:v=0:a=1[out]"
    cmd += ["-filter_complex", fc, "-map", "[out]", str(out_wav)]
    sh(cmd)


def tts_mixed(text, kind, out_wav):
    """A scene with English names: Persian parts with the Persian voice, names with the English voice."""
    segs = split_names(text)
    if not any(is_name for is_name, _ in segs):
        return tts_text(text, kind, out_wav)
    files = []
    for k, (is_name, seg) in enumerate(segs):
        seg = seg.strip()
        if not re.search(r"\w", seg):
            continue
        f = out_wav.with_name(f"{out_wav.stem}_m{k}.wav")
        if is_name:
            english_tts(seg, kind, f)
        else:
            tts_text(seg, kind, f)
        files.append(f)
    if len(files) == 1:
        shutil.copy(files[0], out_wav)
    else:
        concat_wavs(files, out_wav)


_DIGRAPHS = (("sh", "ش"), ("ch", "چ"), ("kh", "خ"), ("th", "ت"), ("ph", "ف"), ("ck", "ک"), ("oo", "و"),
             ("ee", "ی"), ("ea", "ی"), ("ou", "او"), ("ai", "ی"), ("ay", "ی"), ("ow", "او"), ("qu", "کو"))
_LETTERS = dict(b="ب", c="ک", d="د", f="ف", g="گ", h="ه", j="ج", k="ک", l="ل", m="م", n="ن", p="پ", q="ک", r="ر",
                s="س", t="ت", v="و", w="و", x="کس", y="ی", z="ز")


def latin_to_fa(word):
    """Last-resort Persian spelling of an English word (only when the English voice is not available and Gemini
    gave no spelling): the voice must never crash on Latin letters."""
    w = word.lower()
    out, i = [], 0
    while i < len(w):
        ch = w[i]
        if not ch.isalpha():
            i += 1
            continue
        two = next((fa for d, fa in _DIGRAPHS if w.startswith(d, i)), None)
        if two:
            out.append(two)
            i += 2
            continue
        first = not out
        if ch in "aeou":
            out.append({"a": "آ" if first else "َ", "e": "ا" if first else "ِ", "o": "ا" if first else "ُ",
                        "u": "ا" if first else "ُ"}[ch])
        elif ch == "i":
            out.append("ا" if first else "ی")
        elif ch == "c" and w[i + 1:i + 2] in ("e", "i", "y"):
            out.append("س")
        elif ch in _LETTERS:
            if not (out and out[-1] == _LETTERS[ch] and ch not in "ln"):     # collapse doubled letters
                out.append(_LETTERS[ch])
        i += 1
    return "".join(out)


def phonetic_text(text, names):
    """Fallback: every English word replaced by Persian letters (the Persian voice reads it)."""
    for k in sorted(names, key=len, reverse=True):
        text = text.replace(k, names[k])
    return re.sub(r"[A-Za-z][A-Za-z'’\-]*", lambda m: latin_to_fa(m.group(0)), text)


# free voice quota: requests per model per day (counted in azim_tts_usage.json, reset at midnight Pacific time)
TTS_DAILY_LIMIT = int(os.environ.get("AZIM_TTS_DAILY") or 10)
USAGE_FILE = ROOT / "azim_tts_usage.json"


def tts_usage():
    d = load_json(USAGE_FILE, {})
    if not isinstance(d, dict) or d.get("date_pt") != pacific_today():
        d = {"date_pt": pacific_today(), "used": {}}
    return d


def tts_usage_add(model):
    d = tts_usage()
    d["used"][model] = int(d["used"].get(model, 0)) + 1
    try:
        save_json(USAGE_FILE, d)
    except OSError:
        pass


def tts_left(model):
    return TTS_DAILY_LIMIT - int(tts_usage()["used"].get(model, 0))


def gemini_group_count(scenes):
    """How many Gemini voice requests the whole video needs (same grouping as gemini_voice_groups)."""
    groups = n = cur = 0
    for sc in scenes:
        w = len(sc["narration_fa"].split())
        if n and cur + w > GEMINI_GROUP_WORDS:
            groups, n, cur = groups + 1, 0, 0
        n, cur = n + 1, cur + w
    return groups + (1 if n else 0)


GEMINI_VIDEOS_PER_DAY = int(os.environ.get("AZIM_GEMINI_VIDEOS") or 99)   # free Gemini TTS: 10 requests/day = 2 videos
POSTED_REF = []                 # the list of posted videos (set in main), used to count today's Gemini videos
VOICE_USED = {"engine": None}


def pacific_today():
    """Gemini's daily limits reset at midnight Pacific time."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()


def gemini_voice_allowed():
    n = sum(1 for p in POSTED_REF if p.get("engine") == "gemini" and p.get("date_pt") == pacific_today())
    return n < GEMINI_VIDEOS_PER_DAY


def make_voice(scenes, kind, names=None):
    """ONE engine and ONE voice for the WHOLE video. If Gemini TTS fails on any paragraph, the whole video is
    read again by Pocket TTS (never one paragraph with one voice and the next with another)."""
    _VOICE_NAMES.clear()
    _VOICE_NAMES.update({str(k).strip(): str(v).strip() for k, v in (names or {}).items()})
    if gemini_tts_ready() and gemini_voice_allowed():
        for mdl in GEMINI_TTS_MODELS:
            if mdl in _gtts_dead_models or not gemini_tts_ready():
                continue
            need = gemini_group_count(scenes) + 1            # +1: one spare request for a re-read
            if tts_left(mdl) < need:
                note(f"Gemini voice model {mdl}: {max(tts_left(mdl), 0)} free request(s) left today, {need} needed: "
                     "not started, so no request is wasted on a half video.")
                continue
            _gtts["model"] = mdl
            try:
                durs = _make_voice(scenes, kind, names, True)
                VOICE_USED["engine"] = "gemini"
                return durs
            except GeminiTTSUnavailable as e:
                note(f"Gemini voice model {mdl} could not read the whole video ({str(e)[:100]}): the WHOLE video is "
                     "read again with the next free voice model, or with Pocket TTS, so only one voice is heard.")
        _gtts["dead"] = True
    if gemini_tts_ready():
        note("Gemini voice is used for only %d videos a day: this video uses the uploaded voices (Pocket TTS)."
             % GEMINI_VIDEOS_PER_DAY)
    VOICE_USED["engine"] = "pocket"
    return _make_voice(scenes, kind, names, False)


def _make_voice(scenes, kind, names, use_gemini):
    print(f"Voice: {'Gemini TTS' if use_gemini else 'Pocket TTS Farsi v2'} ({kind}), scenes: {len(scenes)}", flush=True)
    (WORK / "tts_out").mkdir(exist_ok=True)
    names = names or {}
    wavs = []

    en_dead = [False]

    def speak(text, p):
        if en_dead[0] and LATIN.search(text):
            tts_text(phonetic_text(text, names), kind, p)
            return
        try:
            tts_mixed(text, kind, p)
        except EnglishVoiceError:
            en_dead[0] = True        # do not try the English voice again in this video
            ph = phonetic_text(text, names)
            note("  English voice not available: English words are read with their Persian spelling.")
            tts_text(ph, kind, p)

    if use_gemini:
        wavs = gemini_voice_groups(scenes, kind)
    for i, s in enumerate(scenes):
        p = WORK / f"raw_{i:02d}.wav"
        plain = s["narration_fa"].strip()
        spoken = (s.get("spoken_fa") or plain).strip()
        REWRITTEN.clear()
        if use_gemini:
            continue                                  # already read (and cut) by gemini_voice_groups
        key = "spoken_fa" if spoken != plain else "narration_fa"
        try:
            speak(spoken, p)
        except RuntimeError as e:
            if spoken == plain:
                raise
            # the voice could not read the prepared text: use the plain text of this scene
            note(f"  Scene {i}: voice text failed ({str(e)[:80]}); using the plain text.")
            REWRITTEN.clear()
            key = "narration_fa"
            speak(plain, p)
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
    elif total < MIN_SECONDS:
        factor = total / (MIN_SECONDS * 1.03)            # below 1.0 = slower voice, so the video lasts more than 3 minutes
        if factor < MIN_SLOW:
            raise ScriptTooShort(f"Narration too short ({total:.0f}s): the video would be shorter than 3 minutes")
    print(f"voice total = {total:.1f}s, speed factor = {factor:.2f}", flush=True)
    durs = []
    for i, p in enumerate(trimmed):
        mp3 = WORK / f"voice_{i:02d}.mp3"
        af = CLEAN_AF + (f",atempo={factor:.3f}" if abs(factor - 1.0) > 0.001 else "")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(p), "-af", af,
               "-ac", "1", "-c:a", "libmp3lame", "-q:a", "4", str(mp3)]
        sh(cmd)
        durs.append(probe_duration(mp3))
    tot_ = sum(d + PAD for d in durs)
    if tot_ > MAX_SECONDS + 10:
        raise RuntimeError("Narration is longer than 7 minutes")
    if tot_ < MIN_SECONDS - 4:
        raise ScriptTooShort(f"Narration is shorter than 3 minutes ({tot_:.0f}s)")
    return durs


# ---------------------------------------------------------------- images
# Positive wording only: FLUX has no negative prompt, so "no ..." words could even add those things.
CLEAN = "clean crisp image, sharp focus, fine sharp details, clear clean edges, high clarity, hand-drawn black-and-white graphite pencil drawing, not a photo, not colored, not 3D, not chibi"
CLEAN_SHORT = "sharp focus, crisp clean details, black-and-white graphite pencil drawing, natural head size, not a photo, not colored"


def _wc(t):
    return len(str(t).split())


KLEIN_WORDS = 300         # FLUX.2 klein reads a longer text than the old square model
MAX_PEOPLE = 2          # at most this many people with a LARGE clear face in one picture (more of them blend their faces);
                        # every other person is small and far away or seen from behind
ANATOMY = ("Every person has one head, two arms ending in two hands with five fingers each (none missing, none extra) "
           "and two legs; natural human proportions: every head is about one seventh of its body height, never a big head; "
           "limbs of different people never overlap")
NO_EXTRA = "No other person anywhere, no crowd, no faces in windows"
CROWD_WORDS = ("crowd", "soldiers", "villagers", "mourners", "audience", "guests", "students", "workers", "passers",
               "people of", "townspeople", "congregation")
PROMPT_WORDS = 200        # FLUX schnell reads only about the first 256 tokens: everything after that is lost


def _trim_words(t, n):
    w = str(t).split()
    return " ".join(w[:n]).rstrip(",;:- ")


def pick_present(chars, names):
    """At most MAX_PEOPLE people with a large clear face plus ONE creature or other non-human character: a creature is never
    dropped to make room for a person (the event of the story often IS the creature)."""
    byname = {c.get("name"): c for c in chars if isinstance(c, dict)}
    humans = [n for n in names if not (byname.get(n) or {}).get("nonhuman")]
    others = [n for n in names if (byname.get(n) or {}).get("nonhuman")]
    return [n for n in names if n in humans[:MAX_PEOPLE] or n in others[:1]]


def build_prompt(art, chars, scene, fix="", ref_names=None, limit=None):
    """What happens (the event) and the detailed place come first, then the people, then the era and the style. The whole
    text stays inside the word limit so that nothing important is cut away: when it is too long, the people's notes are
    made shorter first, the picture description is cut only as the very last step. When a person has a reference picture
    (ref_names, in the order in which the pictures are sent), the prompt points to it by number: the model copies the
    face from there."""
    byname = {c.get("name"): c for c in chars if isinstance(c, dict) and c.get("name")}
    names = pick_present(chars, [n for n in (scene.get("characters_present") or []) if n in byname])
    present = [byname[n] for n in names]
    ref_idx = {n: k + 1 for k, n in enumerate(ref_names or [])}
    roles = scene.get("roles") if isinstance(scene.get("roles"), dict) else {}
    rw = {"n": 10}                 # words of the "who does what" note of each person (made shorter if the text is too long)

    def role_txt(name):          # who does WHAT in this picture (so e.g. the mother and the daughter are never swapped)
        r = " ".join(str(roles.get(name) or "").split())
        return f", who is {_trim_words(r, rw['n'])}" if (r and rw["n"]) else ""

    era_full = str(scene.get("era_en") or "").strip()
    era_short = era_full.split(". Clothing of that time")[0]
    if scene.get("visual_en"):
        main = str(scene["visual_en"]).strip()
        ev = str(scene.get("event_en") or "").strip().rstrip(".")
        if ev and ev.lower()[:30] not in main.lower():
            main = f"{ev}. {main}"
    else:
        main = (f"{scene['action_en']}. Setting: {scene['setting_en']}. Mood: {scene.get('mood_en', 'cinematic')}.")
    head = (f"The picture must clearly show: {fix}. " if fix else "")
    limit = limit or PROMPT_WORDS
    art_s = ", ".join(art.split(", ")[:4])

    others = [" ".join(str(o).split()) for o in (scene.get("others") or []) if str(o).strip()][:3]
    total = len(present) + len(others)
    people_en = " ".join(str(scene.get("people_en") or "").split()).rstrip(". ")
    creatures = [" ".join(str(x).split()) for x in (scene.get("creatures_en") or []) if str(x).strip()][:2]
    subject = ("Main subject, drawn large and clearly visible: " + "; ".join(_trim_words(x, 28) for x in creatures) + ". "
               if creatures else "")
    if creatures:                    # "nobody else" must not forbid the creature of the event
        people_en = re.sub(r"\bnobody else\b", "no other people", people_en, flags=re.I)
    if people_en:
        count = people_en[0].upper() + people_en[1:] + ". "
    elif total:
        count = f"Exactly {total} {'person' if total == 1 else 'people'} in the picture, nobody else. "
    else:
        count = ""
    if creatures:
        count = count.replace("nobody else", "no other people")
    if total >= 2:
        count += ("Every person has a different face, hairstyle and clothes (light or dark tones); same natural head size for all; "
                  "people stand apart, bodies never overlap. ")
    if any(re.search(r"\b(man|boy|male|father|husband|king|prince|gentleman)\b", str(c.get("tag_en") or c.get("look_en") or "")[:60], re.I)
           and not re.search(r"\b(woman|girl|female|mother|wife|queen|lady)\b", str(c.get("tag_en") or c.get("look_en") or "")[:60], re.I)
           for c in present):
        count += "Men wear men's clothes. "
    if (present or others) and not BED_WORDS.search(" ".join(str(scene.get(k) or "") for k in ("visual_en", "event_en")) + " "
                                                    + " ".join(map(str, scene.get("must_show") or []))):
        count += "Everybody wears proper day clothes. "
    nh_all = bool(present) and all(c.get("nonhuman") for c in present) and not others
    nh_any = any(c.get("nonhuman") for c in present)
    if nh_all:
        count = count.replace("Every person has a different face, hair and clothes color. ",
                              "Every character is clearly different in shape, size and color. ").replace(
                              "Everybody wears proper day clothes. ", "")
    has_people = bool(present or others) or bool(people_en and not people_en.lower().startswith("no people"))
    if has_people:
        count += ((("Every character keeps exactly the body described in its sheet, with no extra parts and no extra limbs. "
                    if nh_all else ANATOMY + " (humans only). ")) if nh_any else ANATOMY + ". ")
        if not any(w in (people_en + " " + " ".join(others)).lower() for w in CROWD_WORDS):
            count += ("No other character anywhere: no crowd, no figures in windows or doorways" if nh_all else NO_EXTRA) + ". "
    if total > MAX_PEOPLE:
        count += f"Only {MAX_PEOPLE} people have large clear faces; the others are small, far away or from behind. "
    if has_people and not BED_WORDS.search(" ".join(str(scene.get(k) or "") for k in ("visual_en", "event_en"))):
        count += "Everybody alive and upright. "
    if not creatures:
        count += "Only the listed people, no animals. "

    def people(full):
        if not (present or others):
            return ""
        if len(present) == 1 and not others and ref_idx.get(present[0]["name"]):
            c = present[0]
            if c.get("nonhuman"):
                return (f"The character is the SAME being as reference image 1: identical shape, size, colors, markings, eyes "
                        f"and mouth ({_trim_words(c.get('key_en') or char_tag(c), 14)}), but NOT its pose or direction"
                        f"{role_txt(c['name'])}. ")
            return (f"The person is the SAME person as reference image 1: identical face, eyes, nose, mouth, hair, age, "
                    f"body and clothes ({_trim_words(c.get('key_en') or char_tag(c), 14)}), but NOT its pose or head "
                    f"direction{role_txt(c['name'])}. ")
        parts = []
        for k, c in enumerate(present):
            ri = ref_idx.get(c["name"])
            key = c.get("key_en") or ""
            if ri:
                desc = key or _trim_words(char_tag(c), 12)
            elif len(present) == 1 and not others:
                desc = _trim_words(c["look_en"], 42) if full else char_tag(c)
            else:
                desc = char_tag(c)
            parts.append(("character " if c.get("nonhuman") else "person ") + f"{k + 1}" + (f" = reference image {ri}" if ri else "") + f": {desc}"
                         + role_txt(c["name"]))
        slots = max(MAX_PEOPLE - len(present), 0)
        for j, o in enumerate(others):
            if j >= slots:        # only MAX_PEOPLE faces are large and clear: everybody else is small, far away or from behind
                parts.append(f"person {len(present) + j + 1} (a small figure far in the background or seen from behind, "
                             f"no clear face): {_trim_words(o, 14)}")
            elif present or len(others) > 1:
                parts.append(f"person {len(present) + j + 1} (a stranger, in no reference image, a completely different "
                             f"face): {_trim_words(o, 30)}")
            else:
                parts.append(_trim_words(o, 30))
        lead = (("Copy each character's look from the reference image with the same number, not its pose; "
                 if nh_all else "Copy each face, hair and clothes from the reference image with the same number, not its pose; ")
                + "a stranger copies no reference. " if (ref_idx and present) else "")
        who = "characters" if nh_all else "people"
        return lead + (f"Clearly different {who}: " if len(parts) > 1 else ("The character: " if nh_all else "The person: ")) \
            + "; ".join(parts) + ". "

    emo = " ".join(str(scene.get("emotion_en") or "").split())
    if emo and has_people:
        count += f"{'Expressions' if nh_all else 'Facial expressions'} that show the feeling: {_trim_words(emo, 28)}. "
    non_event = (str(scene.get("kind") or "event") != "event") or people_en.lower().startswith("no people")

    def compose(main_t, full, era, tail):
        era_part = f"Setting: {era}. " if era else ""
        if scene.get("cover"):      # writer at the desk below, empty wall above (the big book is laid there later)
            return (f"{head}{art_s}. {subject}{count}{main_t} {era_part}Vertical 9:16 composition: the writer and the desk large in the "
                    f"lower half, fully inside the picture, the whole upper part an empty plain wall; {tail}")
        if non_event:       # the writer's desk, a book and its readers, a symbolic scene: objects and place first
            return (f"{head}{art_s}. {subject}{count}{main_t} {people(full)}{era_part}"
                    f"Vertical 9:16 medium-wide shot: the main subject large and fully inside the picture, nothing cut by "
                    f"the border, rich detailed foreground, middle ground and background, no writing on any object; "
                    f"the lower fifth only floor or ground; {tail}")
        if creatures:
            return (f"{head}{art_s}. {subject}{count}{main_t} {people(full)}{era_part}"
                    f"Vertical 9:16 medium-wide shot: the creature and the people large and fully visible, whole bodies inside "
                    f"the picture, the EVENT happening right now with clear action, clear carefully drawn faces, not looking at "
                    f"the camera; detailed environment; the lower fifth only floor or ground; {tail}")
        if nh_all:
            return (f"{head}{art_s}. {subject}{count}{main_t} {people(full)}{era_part}"
                    f"Vertical 9:16 medium-wide shot: the characters large (half of the picture height), their whole bodies "
                    f"inside the picture, clear shapes and expressive eyes, busy with the action, not looking at the camera; "
                    f"detailed environment; the lower fifth only floor or ground; {tail}")
        return (f"{head}{art_s}. {subject}{count}{main_t} {people(full)}{era_part}"
                f"Vertical 9:16 medium-wide shot: all people at the same scale (adults equally tall, about half of the picture "
                f"height) with attractive, clear, carefully drawn faces, whole heads and bodies inside the picture, busy with the action, not looking at the camera; "
                f"detailed environment; the lower fifth only floor or ground; {tail}")

    for full, era, tail, n_role in ((True, era_full, CLEAN, 10), (True, era_short, CLEAN, 8),
                                    (True, era_short, CLEAN_SHORT, 6), (False, era_short, CLEAN_SHORT, 0)):
        rw["n"] = n_role
        out = compose(main, full, era, tail)
        if _wc(out) <= limit and len(out) <= 1850:
            return out
    rw["n"] = 0
    spare = limit - _wc(compose("", False, era_short, CLEAN_SHORT))     # keep the people, shorten the picture
    out = compose(_trim_words(main, max(spare, 25)), False, era_short, CLEAN_SHORT)
    while len(out) > 1850 and len(main.split()) > 25:                   # the character limit of the service
        main = _trim_words(main, len(main.split()) - 4)
        out = compose(main, False, era_short, CLEAN_SHORT)
    return out


class ImageServiceUnavailable(RuntimeError):
    """Cloudflare cannot draw now (daily free quota finished, key refused, or down for a long time). The video is NOT
    made with bad pictures: the finished script stays saved and the next run continues with it."""


class ImagePromptRefused(RuntimeError):
    """Cloudflare's safety filter refused every wording of ONE picture. This is not a quota problem and not a reason
    to stop the video: the caller reuses the previous picture."""


def _cf_errors(r):
    """(set of numeric error codes, lower-case message text) of a Cloudflare answer. Codes are read ONLY from the JSON
    field, never searched inside the raw text (a request id such as 'be240066-...' contains '4006' and was once taken
    for 'the daily quota is finished')."""
    codes, msgs = set(), []
    try:
        for e in (r.json().get("errors") or []):
            if isinstance(e, dict):
                try:
                    codes.add(int(e.get("code")))
                except (TypeError, ValueError):
                    pass
                msgs.append(str(e.get("message") or ""))
    except Exception:  # noqa: BLE001
        pass
    msg = " ".join(msgs).lower()
    if not codes and not msg:
        msg = (r.text or "").lower()
    return codes, msg


SAFE_PREFIX = ("Detailed black-and-white graphite pencil drawing in manga-anime style, graceful adult proportions with a "
               "natural head size, soft shading, everyone fully clothed, alive and upright. ")


# FLUX.2 [klein] 4B on Cloudflare (free daily allowance, same as before): it takes width/height (so the picture is
# really 9:16 and nothing has to be cut off) and up to 4 reference pictures (the same face in every picture).
CF_MODEL = os.environ.get("AZIM_IMG_MODEL") or "@cf/black-forest-labs/flux-2-klein-4b"
CF_FALLBACK_MODEL = "@cf/black-forest-labs/flux-1-schnell"       # square only: the video builder fits it without cutting
IMG_W = int(os.environ.get("AZIM_IMG_W") or 512)      # 512x912 costs the SAME free neurons as 432x768 (2 tiles of 512x512) but has 40% more pixels
IMG_H = int(os.environ.get("AZIM_IMG_H") or 912)
SAFE_SIZE = (432, 768)    # the size that already worked on Cloudflare: used if 512x912 is refused
REF_SIDE = 512            # reference pictures must be at most 512 px (one tile = the same price as 384 px)
MAX_REFS = int(os.environ.get("AZIM_MAX_REFS") or 3)   # reference pictures sent with ONE picture (each one costs like a picture)
_STATE = {"refs_ok": True, "klein_ok": True, "refs_fail": 0, "klein_fail": 0, "size_ok": True, "scene_ok": False}
CAST_REFS = {}            # {character name: reference picture file}, filled in main() (the image check looks at them too)


def img_size():
    return (IMG_W, IMG_H) if _STATE["size_ok"] else SAFE_SIZE


def _cf_log(r, klein, w, h, n_refs, secs):
    """One line for every Cloudflare picture call: status, time, size, estimated neurons (official price list:
    FLUX.2 klein 26.05 per output 512x512 tile + 5.37 per reference tile; FLUX.1 schnell 4.8 per tile + 9.6 per
    step, 4 steps). A failed call also prints the answer headers (rate-limit / retry information)."""
    if klein:
        est = math.ceil(w / 512) * math.ceil(h / 512) * 26.05 + n_refs * 5.37
    else:
        est = 4 * 4.8 + 4 * 9.6
    ok = r.status_code == 200
    CF_STATS["by_status"][r.status_code] = CF_STATS["by_status"].get(r.status_code, 0) + 1
    if ok:
        CF_STATS["ok"] += 1
        CF_STATS["est_neurons"] += est
    else:
        CF_STATS["fail"] += 1
    print(f"  CF call #{CALLS['cloudflare']}: status={r.status_code} {secs:.1f}s size={w}x{h} refs={n_refs} "
          f"est_neurons={est:.0f} | run total: {CF_STATS['ok']} ok, {CF_STATS['fail']} failed, "
          f"~{CF_STATS['est_neurons']:.0f} neurons", flush=True)
    if not ok:
        hdr = {k: v for k, v in r.headers.items()
               if k.lower() not in ("set-cookie", "server", "date", "connection", "vary")}
        print(f"  CF answer headers: {str(hdr)[:700]}", flush=True)


def to_gray(path):
    """Every picture is a black-and-white pencil drawing. Whatever colour a model (klein, klein with references, the old
    square model) adds is removed here, in code (free): the pictures of one video can never change to coloured ones."""
    try:
        tmp = path.with_name(path.stem + "_g" + path.suffix)
        sh(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-vf", "hue=s=0,normalize=blackpt=black:whitept=white:smoothing=0,eq=contrast=1.04", "-q:v", "2", str(tmp)])
        if tmp.exists() and tmp.stat().st_size > 1000:
            os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        print(f"Gray conversion skipped ({str(e)[:80]}).", flush=True)


def cf_image(model, prompt, path, seed=None, refs=None, size=None):
    """One Cloudflare Workers AI picture. Returns "ok", "hard" (this way of asking is refused: do not repeat it),
    "flagged" (the safety filter refused this wording: waiting does not help, a gentler wording may) or
    "soft" (a short problem: waiting may help)."""
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{model}"
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}"}
    klein = "flux-2" in model
    w, h = size or img_size()
    base_prompt = prompt
    last_flagged = False
    for attempt in range(1, 4):
        last_flagged = False
        try:
            print(f"Image(CF {'klein' if klein else 'schnell'}{' +' + str(len(refs)) + ' ref' if refs else ''}): "
                  f"attempt={attempt}", flush=True)
            CALLS["cloudflare"] += 1
            t_call = time.time()
            if klein:           # multipart form (also for a prompt alone), references as input_image_0..3
                files = {"prompt": (None, prompt[:1900]), "width": (None, str(w)), "height": (None, str(h))}
                if seed is not None:
                    files["seed"] = (None, str(int(seed) % 2147483647))
                for k, rp in enumerate((refs or [])[:4]):
                    files[f"input_image_{k}"] = (Path(rp).name, Path(rp).read_bytes(), "image/jpeg")
                r = requests.post(url, headers=headers, files=files, timeout=180)
            else:               # schnell accepts ONLY prompt and steps (a square 1024 picture)
                r = requests.post(url, headers={**headers, "Content-Type": "application/json"},
                                  json={"prompt": prompt[:1900], "steps": 4}, timeout=120)
            _cf_log(r, klein, w, h, len(refs or []), time.time() - t_call)
            if r.status_code == 200:
                ctype = r.headers.get("content-type", "")
                if ctype.startswith("image"):
                    path.write_bytes(r.content)
                    to_gray(path)
                    return "ok"
                b64 = (r.json().get("result") or {}).get("image")
                if b64:
                    path.write_bytes(base64.b64decode(b64))
                    to_gray(path)
                    return "ok"
            print(f"  -> status={r.status_code} body={r.text[:600]!r}", flush=True)
            codes, msg = _cf_errors(r)
            if 4006 in codes or "daily free allocation" in msg:
                raise ImageServiceUnavailable("the daily free quota of Cloudflare Workers AI is finished")
            if r.status_code in (401, 403):
                raise ImageServiceUnavailable("Cloudflare refused the key (CF_API_TOKEN / CF_ACCOUNT_ID are wrong)")
            if 3030 in codes or "flagged" in msg or "nsfw" in msg:       # false alarm of the safety filter
                last_flagged = True
                seed = (seed or 0) + 7
                prompt = SAFE_PREFIX + base_prompt                        # other seed + gentler wording (not stacked)
                if attempt >= 2:                                          # every try may cost free neurons: stop here
                    return "flagged"
                continue
            if r.status_code == 429:
                time.sleep(30)
                continue
            if r.status_code in (400, 404, 405, 415, 422, 500) or 5006 in codes or 7000 in codes:
                return "hard"
        except ImageServiceUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"  -> error: {e}", flush=True)
        time.sleep(8 * attempt)
    if last_flagged:
        return "flagged"
    return "soft"


_VIOLENT = [
    (r"\b(?:is|are|was|were|gets|get|got) (?:eaten|killed|devoured|bitten|attacked|murdered|slaughtered)\b", "is in terrible danger"),
    (r"\b(?:blood(?:y)?|gore|gory|wounds?|wounded|mangled|torn apart|corpses?|dead bod(?:y|ies))\b", ""),
    (r"\b(?:kill(?:s|ed|ing)?|murder(?:s|ed|ing)?|slaughter(?:s|ed|ing)?|devour(?:s|ed|ing)?|eat(?:s|ing)?|ate|attack(?:s|ed|ing)?|assault(?:s|ed|ing)?)\b",
     "charges at"),
    (r"\b(?:bit|bites?|biting)\b", "lunges at"),
    (r"\b(?:hang(?:s|ed|ing)? (?:himself|herself|themselves)|hanged|hanging from (?:a|the) (?:rope|noose|beam))\b", "an empty rope hangs above a toppled chair"),
    (r"\b(?:dying|dies|died|death|dead)\b", "is mourned by the others"),
    (r"\b(?:scream(?:s|ed|ing)?|shriek(?:s|ed|ing)?)\b", "cries out"),
    (r"\b(?:jaws?|teeth|fangs?|claws?)\b", "head"),
    (r"\b(?:guns?|rifles?|shot|shoots?|shooting|weapons?|knife|knives|swords?)\b", "tool"),
]


_VIOLENT_ANY = re.compile("|".join(p_ for p_, _ in _VIOLENT), re.I)


def soften_violence(text):
    """Free fallback in code: the same event with the violent words replaced, when the plan has no "gentle_en"."""
    t = str(text or "")
    for pat, new in _VIOLENT:
        t = re.sub(pat, new, t, flags=re.I)
    return " ".join(t.split())


def make_image(build, path, seed, scenes_left, fix="", safe_build=None):
    """build(fix, use_refs) -> (prompt, [reference files]). First FLUX.2 klein with the references, then klein without
    them, then the old square model. If Cloudflare has a short problem, wait (2 and 5 minutes) and try again.
    If the SAFETY FILTER refuses the picture, waiting is useless: gentler wordings are tried (safe_build level 1, then
    level 2 = a quiet scene without people), and if even that fails ImagePromptRefused is raised (the caller reuses the
    previous picture). A refused picture NEVER stops the whole video."""
    for wait in (0, 120, 300):
        if wait:
            if time.time() - START_TIME + wait + (scenes_left + 1) * 15 > JOB_BUDGET:
                break
            print(f"Cloudflare could not draw this picture: waiting {wait} s and trying again.", flush=True)
            time.sleep(wait)
        plans = []
        if _STATE["klein_ok"]:
            if _STATE["refs_ok"]:
                plans.append((CF_MODEL, True))
            plans.append((CF_MODEL, False))
        plans.append((CF_FALLBACK_MODEL, False))
        tried = flagged = 0
        for model, use_refs in plans:
            prompt, refs = build(fix, use_refs, KLEIN_WORDS if model == CF_MODEL else PROMPT_WORDS)
            if use_refs and not refs:
                continue                                  # nobody in this picture has a reference: same as no refs
            res = cf_image(model, prompt, path, seed, refs if use_refs else None)
            if (res == "hard" and model == CF_MODEL and _STATE["size_ok"] and not _STATE["scene_ok"]
                    and img_size() != SAFE_SIZE):
                _STATE["size_ok"] = False
                print(f"WARNING: the size {IMG_W}x{IMG_H} was refused: using {SAFE_SIZE[0]}x{SAFE_SIZE[1]} instead.",
                      flush=True)
                res = cf_image(model, prompt, path, seed, refs if use_refs else None)
            tried += 1
            if res == "ok":
                if model == CF_MODEL:
                    _STATE["scene_ok"] = True
                time.sleep(1)
                return
            if res == "flagged":
                flagged += 1
                continue
            if res == "hard":
                if use_refs:
                    _STATE["refs_fail"] += 1
                    if _STATE["refs_fail"] >= 2:
                        _STATE["refs_ok"] = False
                        print("WARNING: reference pictures are refused: drawing without them from now on.", flush=True)
                elif model == CF_MODEL:
                    _STATE["klein_fail"] += 1
                    if _STATE["klein_fail"] >= 3:
                        _STATE["klein_ok"] = False
                        print("WARNING: FLUX.2 klein is refused: using the old square model from now on.", flush=True)
        if tried and flagged == tried:
            print("Safety filter refused this picture in every way: trying gentler wordings (no waiting).", flush=True)
            break
    if safe_build:
        # three gentler wordings (the same event, told softly), ONE call each with the model that works: never more calls than before
        for level in (1, 2, 3):
            model = CF_MODEL if _STATE["klein_ok"] else CF_FALLBACK_MODEL
            lim = KLEIN_WORDS if model == CF_MODEL else PROMPT_WORDS
            print(f"Gentler wording, level {level} ({'klein' if model == CF_MODEL else 'schnell'}).", flush=True)
            res = cf_image(model, safe_build(level, lim), path, seed + 13 * level, None)
            if res == "ok":
                time.sleep(1)
                return
    raise ImagePromptRefused("the safety filter refused this picture in every wording")


SAVE_DIR = ROOT / "azim_saved_pics"      # pictures already drawn for the book that is still unfinished (kept in git)


def saved_pics_dir(book):
    """The folder with the pictures already drawn for THIS book (made in an earlier run that stopped because the free
    daily Cloudflare allowance ended). Pictures of another book are deleted. Nothing is drawn (and paid for) twice."""
    key = norm(book["title_en"]) + f"|v{VISUAL_VERSION}|{IMG_W}x{IMG_H}"
    marker = SAVE_DIR / "azim_book.txt"
    try:
        if not (marker.exists() and marker.read_text(encoding="utf-8").strip() == key):
            if SAVE_DIR.exists():
                shutil.rmtree(SAVE_DIR, ignore_errors=True)
            SAVE_DIR.mkdir(parents=True, exist_ok=True)
            marker.write_text(key, encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"Saved pictures folder not usable ({e}).", flush=True)
    return SAVE_DIR


def keep_saved(src_file, name):
    try:
        if SAVE_DIR.exists():
            shutil.copy(src_file, SAVE_DIR / name)
    except Exception as e:  # noqa: BLE001
        print(f"Picture could not be saved for the next run ({e}).", flush=True)


def _ref_prompt(c, era_short, extra=""):
    if c.get("nonhuman"):
        return (f"Black-and-white graphite pencil drawing, manga-anime character reference picture of {c.get('species_en') or 'a character'}: "
                f"{_trim_words(c['look_en'], 70)}. The whole character from one end to the other, large, in the middle, "
                f"facing the viewer, a clear simple outline, plain soft light-gray background, nothing else. {extra}"
                f"Setting: {era_short}. Style: {_trim_words(FIXED_STYLE, 24)}. Sharp focus, crisp clean details.")
    return (f"Black-and-white graphite pencil drawing, manga-anime character reference portrait. {_trim_words(c['look_en'], 95)}. Dressed in {DAY_CLOTHES_RULE}. "
            f"From the head down to the "
            f"waist, the face large and carefully drawn with clear eyes, eyebrows, nose and mouth, facing the viewer with a "
            f"calm neutral expression, the whole head and both shoulders inside the picture, plain soft light-gray "
            f"background, nothing else. {extra}Setting: {era_short}. Style: {_trim_words(FIXED_STYLE, 24)}. "
            f"Sharp focus, crisp clean details.")


def _draw_ref(c, k, prompt, cseed):
    raw, small = WORK / f"refraw_{k}.jpg", WORK / f"ref_{k}.jpg"
    if cf_image(CF_MODEL, prompt, raw, cseed, None, size=(512, 512)) != "ok":
        return None
    sh(["ffmpeg", "-y", "-loglevel", "error", "-i", raw.name, "-vf", f"scale=-2:{REF_SIDE}", small.name], cwd=WORK)
    keep_saved(small, f"ref_{k}.jpg")
    return small


def check_cast_refs(chars, refs, index, era_short):
    """ONE Gemini call looks at all reference faces: a face that is distorted, does not fit its look sheet or looks like
    another character is drawn again (once). Every picture of the video copies these faces, so they must be good."""
    if not (IMAGE_CHECK and refs):
        return
    names = list(refs)
    by_name = {c["name"]: c for c in chars if isinstance(c, dict) and c.get("name")}
    blocks = [{"n": k, "name": n, "look_sheet": by_name.get(n, {}).get("look_en", ""),
               **({"not_human": by_name[n].get("species_en")} if by_name.get(n, {}).get("nonhuman") else {})}
              for k, n in enumerate(names, 1)]
    prompt = f"""You check character reference portraits for an illustrated video. The images above are labeled IMAGE 1 to
IMAGE {len(names)}. Items: {json.dumps(blocks, ensure_ascii=False)}
For EACH image answer: "ok": true only if (a) the face is drawn cleanly and naturally (two eyes, one nose, one mouth,
natural proportions, nothing melted, doubled or cut off), (b) gender, age, hair, build and clothes fit the look sheet,
(c) the whole head is inside the picture, (d) this person looks clearly DIFFERENT from every other image (face shape, hair,
age, clothes), (e) no text or letters, (f) the person wears real day or work clothes of the story's world, NOT pajamas,
nightwear or a bathrobe (when the clothes look like sleepwear, set "ok" to false and say in "fix_en" which day clothes to
draw). Otherwise "ok": false and "fix_en": what the new portrait must clearly show, in
positive words, at most 25 words.
For an item with "not_human": it is NOT a person: the criteria about a face, eyebrows, shoulders, hair and clothes do not apply; it
is "ok" when the WHOLE being is drawn cleanly, matches its look sheet (shape, size, colors, parts) and nothing is melted,
doubled or cut off.
Also list in "similar_pairs" EVERY pair of images whose faces, hair or clothes look alike, even a little: [n, m].
Return JSON {{"checks": [{{"n": 1, "ok": true, "fix_en": ""}}], "similar_pairs": [[1, 3]]}} with one check per image."""
    try:
        out = gemini_json(prompt, minutes=4, models=CHEAP_MODELS, images=[refs[n].read_bytes() for n in names])
        time.sleep(3)
    except Exception as e:  # noqa: BLE001
        print("Reference face check skipped:", e, flush=True)
        return
    bad = []
    for c in out.get("checks") or []:
        try:
            k = int(c.get("n"))
        except (TypeError, ValueError):
            continue
        if 1 <= k <= len(names) and c.get("ok") is False:
            bad.append((names[k - 1], " ".join(str(c.get("fix_en") or "").split())))
    for pr in out.get("similar_pairs") or []:
        try:
            a_, b_ = sorted(int(x) for x in pr[:2])
        except (TypeError, ValueError, IndexError):
            continue
        if 1 <= a_ < b_ <= len(names) and names[b_ - 1] not in [x for x, _ in bad]:
            face = by_name.get(names[b_ - 1], {}).get("face_en", "")
            bad.append((names[b_ - 1], "a clearly different face shape, hair style, hair color and clothes than every other "
                        "character" + (f"; {face}" if face else "")))
    print(f"Reference face check: {len(names)} checked, {len(bad)} to draw again.", flush=True)
    for n, fix in bad:
        c = by_name.get(n)
        k = index.get(n)
        if not c or k is None:
            continue
        try:
            cseed = zlib.crc32(("again" + n).encode("utf-8")) % 1000000
            new = _draw_ref(c, k, _ref_prompt(c, era_short, f"The portrait must clearly show: {fix}. " if fix else ""),
                            cseed)
            if new:
                refs[n] = new
        except ImageServiceUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"Reference picture of {n} not redrawn: {e}", flush=True)


def make_cast_refs(chars, era, book, seed):
    """ONE reference picture per important character (drawn once per video, then sent with every picture of that
    character so the face, hair and clothes stay the same). Returns {name: file}. Without klein: no references."""
    refs = {}
    if not (_STATE["klein_ok"] and "flux-2" in CF_MODEL):
        return refs
    era_short = str(era or "").split(". Clothing of that time")[0]
    index = {}
    for k, c in enumerate([x for x in chars if isinstance(x, dict) and x.get("name") and x.get("look_en")][:6]):
        cseed = zlib.crc32((book["title_en"] + c["name"]).encode("utf-8")) % 1000000
        saved = SAVE_DIR / f"ref_{k}.jpg"
        small = WORK / f"ref_{k}.jpg"
        index[c["name"]] = k
        if saved.exists():                                # drawn in an earlier run: not drawn (and paid for) again
            shutil.copy(saved, small)
            refs[c["name"]] = small
            continue
        try:
            got = _draw_ref(c, k, _ref_prompt(c, era_short), cseed)
            if got:
                refs[c["name"]] = got
        except ImageServiceUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"Reference picture of {c['name']} skipped: {e}", flush=True)
    if not (SAVE_DIR / "azim_refs_checked.txt").exists():       # a reference set that was already checked is not checked again
        check_cast_refs(chars, refs, index, era_short)
        try:
            (SAVE_DIR / "azim_refs_checked.txt").write_text("1", encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    print(f"Reference pictures: {len(refs)} of {len(chars)} characters.", flush=True)
    return refs


REDRAW_ROUNDS = int(os.environ.get("AZIM_REDRAW_ROUNDS") or 2)   # a bad picture is drawn again this many times (each costs free neurons)
CHECK_BATCH = int(os.environ.get("AZIM_CHECK_BATCH") or 4)    # pictures checked in ONE Gemini call
MIN_SCORE = int(os.environ.get("AZIM_MIN_SCORE") or 7)


def expected_people(sc):
    """How many people the picture must show (None = a crowd or unknown: the count is not enforced)."""
    people = str(sc.get("people_en") or "").lower()
    if any(w in people + " " + " ".join(sc.get("others") or []).lower() for w in CROWD_WORDS):
        return None
    if sc.get("cover") or sc.get("kind") == "author":
        return 1
    if people.startswith("no people"):
        return 0
    m = re.search(r"exactly\s+(\d+|one|two|three|four|five)\b", people)
    if m:
        w = m.group(1)
        return int(w) if w.isdigit() else {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}[w]
    n = len(sc.get("characters_present") or []) + len(sc.get("others") or [])
    return n if n else None


def check_images(jobs, models=None):
    """ONE Gemini call looks at up to CHECK_BATCH pictures. Returns a list with (score, what is missing) or None for
    each picture; returns None if the check is not available (the pictures are then kept as they are)."""
    blocks = []
    for k, j in enumerate(jobs, 1):
        sc = j["sc"]
        sheets = sc.get("look_sheets") or []
        blocks.append({"n": k, "narration_fa": sc["narration_fa"],
                       "intended_picture": sc.get("visual_en", sc.get("action_en", "")),
                       "exact_people": sc.get("people_en") or "", "must_show": sc.get("must_show") or [],
                       "kind": sc.get("kind") or "event", "cover": bool(sc.get("cover")),
                       "expected_people": expected_people(sc),
                       "character_sheets": sheets, "era": str(sc.get("era_en") or "").split(". Clothing")[0]})
    ref_names = []
    for j in jobs:
        for n in (j["sc"].get("characters_present") or []):
            if n in CAST_REFS and n not in ref_names:
                ref_names.append(n)
    ref_names = ref_names[:4]
    ref_note = ""
    if ref_names:
        ref_note = ("\nAfter the pictures come REFERENCE portraits of characters, labeled IMAGE "
                    f"{len(jobs) + 1} and up: {json.dumps({n: len(jobs) + 1 + k for k, n in enumerate(ref_names)}, ensure_ascii=False)} "
                    "(character name: its IMAGE number). Every picture must show that character with THE SAME face (face shape, "
                    "eyes, nose, hair, age) and the same clothes as in his or her reference portrait.\n")
    prompt = f"""You check illustrations for a video. The pictures to check are labeled IMAGE 1 to IMAGE {len(jobs)}.
For each one you get the Persian narration that is read while it is shown, the intended picture, and (maybe) the look
sheets of its characters. Items: {json.dumps(blocks, ensure_ascii=False)}
{ref_note}
For EACH picture return:
- "score" 0-10: how clearly it shows the main subject and main action of ITS narration (10 = a viewer understands what is
  said just by looking; 0 = unrelated). Strict limits:
  * the EVENT of the narration (a death, a fight, a meeting, a departure, a discovery...) must be recognisable; a calm
    or unrelated picture of a place or a person gets at most 3;
  * a killing, death or suicide: it is shown by the grief of the living and objects (candle, flower, empty chair or bed, dropped
    hat or bag, an empty rope above a toppled chair for a hanging); ANY dead, lying or sheet-covered body, or a person hanging
    from a rope or by the hands: at most 2. A ghost or spirit must be a translucent glowing figure with the person's shape and
    clothes; only a halo, light or mist: at most 3;
  * two bodies that merge under one head, a body with two heads, or a man in a dress or women's clothes (or a woman in men's
    clothes) against his or her sheet: at most 2;
  * the place and situation must fit the narration (inside/outside, kind of place, time, weather), else at most 5;
  * every character of the sheets must be visible in the picture; one missing: at most 5;
  * who does what to whom must match the narration (for example the mother nurses the daughter, not the other way
    round); roles swapped between the people: at most 3;
  * people who just stand and face the viewer or look into the camera with no action of the narration: at most 5;
  * STYLE: every picture must be the same detailed black-and-white graphite PENCIL drawing in manga-anime style (fine hatching,
    soft shading, large expressive eyes, attractive faces, graceful adult proportions, rich detailed clothes and backgrounds).
    A colored picture, a photo-like, 3D, flat vector or painted look, or characters whose heads are suddenly much bigger (a
    chibi look) or whose style differs from the other pictures: at most 3;
  * SCALE: a person much bigger or much smaller than the others at the same distance (a giant or a dwarf; a child is smaller by
    nature): at most 4;
  * a missing arm, hand or head, or a hand growing from the wrong place: "limbs_ok" false;
  * CLOTHES FIT THE JOB: every person wears what his or her job, class and the era say (a merchant in a merchant's coat,
    a servant in plain work clothes, a soldier in uniform ONLY if he is a soldier); a wrong costume: at most 4;
  * CLOTHES FIT THE SITUATION: a person who wears pajamas, a nightgown or a bathrobe while the narration is NOT about
    going to bed, sleeping, waking up or lying ill in bed: at most 4;
  * EVENT AND CREATURE: the narration tells an event (an attack, a chase, a trap, a fall, a death, a rescue...) or names a
    creature or a thing (a dinosaur, a cage, a vehicle) but the picture shows only people standing or walking calmly, or the
    creature is missing: at most 3;
  * PLACE MATCH: the place that is visible (jungle, laboratory, road, cage, room...) is not the place that the narration
    names: at most 4;
  * PLACE DETAIL: a bare, empty or generic place that lacks the objects, materials and light that "intended_picture" names
    (so the viewer cannot tell where and in what situation this is): at most 5;
  * a face or a body cut off by the border of the picture, or a zoomed close-up, at most 4;
  * LOOK AT EVERY BODY: one head, two arms, two hands (five fingers each), two legs. A person with a third arm or hand,
    extra fingers or legs, a missing head or a headless body, a head on the wrong place, merged or floating bodies, melted
    faces, or a head lying on an object: at most 2. Hands that hold nothing but look doubled: at most 4;
  * COUNT THE PEOPLE: their number and kind must equal "exact_people" (for example one mother and ONE child: two girls
    next to the mother is wrong); one person too many or too few: at most 4. Any extra figure that is not in "exact_people" (a small person far away,
    a crowd, a face in a window, a figure in a painting or a statue that looks like a person): at most 3;
  * FACE FEELING: the faces must show the feeling of the moment (worry, fear, joy, sadness, anger, shock...) that fits the
    narration and the place; blank, smiling-by-default or wrong-feeling faces (a smile at a death, a calm face in danger):
    at most 5;
  * every item of "must_show" must be visible; two or more of them missing: at most 5;
  * two different people who look like twins or clones (the same face, the same hair, the same clothes): at most 3, and
    the person who rescues, helps, attacks or talks to another person must not look like that other person;
  * with 3 or more people, any two with the same hair color or the same color of shirt or trousers: at most 4;
  * when "cover" is true: ONE writer (the gender and age in "exact_people"; a wrong gender: at most 3) INDOORS in the LOWER
    half, doing something with writing (desk, typewriter, library table, reading), seen from the side or behind (no
    portrait); outdoors: at most 3; the UPPER 40 percent must be an EMPTY plain wall (a big book is placed there later): any
    object, window, shelf full of books, text or person in that upper wall, a second person, or no writer: at most 4;
  * a head twisted backward, a neck turned unnaturally or a face looking over the shoulder at the viewer (kind "author"): at most 2;
  * AGE FIT: a person who looks like a child but is a bride, a groom, a lover or in a wedding scene: at most 3;
  * kind "author" (and "cover" false): ONE writer of the gender in "exact_people", seen from behind or the side (no portrait),
    INDOORS in a library with bookshelves and a large EMPTY plain wall area in the upper middle (nothing hangs there: the program
    adds a painting later); any object, frame, shelf, lamp, window or plant in that upper-middle wall area, or a writer whose
    head reaches it: at most 4; a wrong gender: at most 3; any other person: at most 3. Kind
    "book": the book as an object with readers and ONE sign of THIS story; kind "theme": a symbolic scene made of things of
    THIS story; an unrelated landscape or a random scene for these kinds: at most 3.
- "look_score" 0-10, only when sheets are given: each person looks like his or her sheet and like his or her reference
  portrait (same face shape, eyes, nose, hair, age, build, clothes); a person whose face, hair or age is clearly different
  from the reference: at most 3; a face that differs from the reference in face shape, eye shape, nose, hairstyle or age
  even slightly (it could be another person): at most 5; a face that is blurred, distorted or too small to see: at most 5; several people are
  clearly different from each other in face, body, hair and clothes (with 3 or more people, any two with the same hair
  color or clothes colors: at most 4); clothes and objects fit the era (10 = all correct).
- "people_count": the NUMBER of people you can see in the picture. Count EVERY person: also small ones, far away, in a
  window or doorway, in a painting or a statue, half hidden or cut by the border. Count first, then compare with
  "expected_people" (null = a crowd, do not compare).
- A character that is NOT human (a shape, an animal, a robot, a creature: its look sheet says so) counts as a person in
  "people_count"; for it "limbs_ok" is true when its body matches its look sheet with no extra and no missing parts, and
  "hands_per_person" is [].
- "limbs_ok": false if ANY person has a wrong body (when you are NOT SURE about the count of a hand, an arm or a leg, say false). Look at EVERY person from the head down to the feet, one by one and COUNT
  the hands of each person (exactly two, never three: look at hands that hold something, at hands near the face and at hands
  between two people, they are often doubled): more or fewer than two legs, two arms, two hands or two feet (a third leg or a doubled leg between two standing people is
  common: check it), a doubled, missing or misplaced head, merged bodies. Otherwise true.
- "head_ok": false when the head of ANY person is clearly too big for his or her body (a big-head, chibi, bobble-head or toy look; a
  natural head is about one seventh of the body height), or when the people at the same distance have very different head sizes.
  Otherwise true.
- "style_ok": false when the picture is NOT the same detailed black-and-white graphite pencil drawing in manga style as the other
  pictures of this call (it looks colored, 3D, painted, photographic, flat vector or cartoon-like, or its line work and shading differ
  clearly from the other pictures). Otherwise true.
- "hands_per_person": a list with ONE number for each person you can see: how many HANDS that person has in the picture
  (count them one by one: two is right; three or more is a mistake that must be reported here).
- "twins": true if two people in the picture look alike (the same face, the same hair or the same clothes).
- "missing": a short English phrase: the key thing that is missing or wrong (empty if the score is 8 or more).
- "fix_en": only if the score is below 8: what a new picture must clearly show, in POSITIVE words, at most 22 words
  (for example "a woman lying motionless on the ground, two men standing over her in shock, all fully visible, wide shot").
Return JSON {{"checks": [{{"n": 1, "people_count": 2, "limbs_ok": true, "head_ok": true, "style_ok": true, "hands_per_person": [2, 2], "twins": false, "score": 7, "look_score": 7, "missing": "...", "fix_en": "..."}}]}} with one item per picture."""
    try:
        out = gemini_json(prompt, minutes=4, models=models or CHEAP_MODELS,
                          images=[j["img"].read_bytes() for j in jobs] + [CAST_REFS[n].read_bytes() for n in ref_names])
        time.sleep(3)
        by_n = {}
        for c in out.get("checks") or []:
            if isinstance(c, dict) and str(c.get("n", "")).isdigit():
                by_n[int(c["n"])] = c
        res = []
        for k, j in enumerate(jobs, 1):
            c = by_n.get(k)
            if not c:
                res.append(None)
                continue
            score = int(c.get("score", 10))
            if j["sc"].get("look_sheets"):
                score = min(score, int(c.get("look_score", 10)))
            fix = str(c.get("fix_en") or c.get("missing", "")).strip()
            try:
                if j["sc"].get("look_sheets") and int(c.get("look_score", 10)) < MIN_SCORE:
                    fix = ("every person with exactly the face shape, eyes, nose, hairstyle and age of his or her reference "
                           "image, the same person as in the other pictures; " + fix)[:200]
            except (TypeError, ValueError):
                pass
            exp, pc = expected_people(j["sc"]), c.get("people_count")
            if isinstance(exp, int) and isinstance(pc, (int, float)) and not isinstance(pc, bool) and int(pc) != exp:
                score = min(score, 3 if int(pc) > exp else 4)         # counted by the judge, enforced by the program
                fix = (f"exactly {exp} {'person' if exp == 1 else 'people'} in the whole picture, nobody else anywhere; "
                       + fix)[:200]
            hp = c.get("hands_per_person")
            many = isinstance(hp, list) and any(isinstance(x, (int, float)) and not isinstance(x, bool) and x > 2 for x in hp)
            if c.get("limbs_ok") is False or many:
                score = min(score, 2)
                fix = ("every person with exactly one head, two arms, two hands and exactly two legs, hands hidden behind "
                       "the back or in pockets, no third hand, no third leg; " + fix)[:200]
            if c.get("head_ok") is False:
                score = min(score, 2)
                fix = ("every person with a natural small head about one seventh of the body height, adult proportions, never a big head; "
                       + fix)[:200]
            if c.get("style_ok") is False:
                score = min(score, 3)
                fix = ("a detailed black-and-white graphite pencil drawing in manga style with fine hatching, exactly like the other pictures; "
                       + fix)[:200]
            if c.get("twins") is True and isinstance(exp, int) and exp >= 2:
                score = min(score, 3)
                fix = ("the people with clearly different faces, hair colors and clothes colors; " + fix)[:200]
            box = c.get("painting_box")
            try:
                box = [float(x) for x in box][:4] if isinstance(box, (list, tuple)) and len(box) >= 4 else None
                if box and not (0 <= box[0] < box[2] <= 1000 and 0 <= box[1] < box[3] <= 1000):
                    box = None
            except (TypeError, ValueError):
                box = None
            if box:
                fr = c.get("painting_free")
                fr = {k_: (fr.get(k_) is True) for k_ in ("bottom", "top")} if isinstance(fr, dict) else {}
                box = {"box": box, "free": fr}
            res.append((score, fix, box))
        return res
    except (GeminiBlocked, GeminiQuotaExhausted):
        raise
    except Exception as e:  # noqa: BLE001
        print("Image check skipped:", e, flush=True)
        return None


def _is_multi(sc):
    return len(sc.get("characters_present") or []) + len(sc.get("others") or []) >= 2


def _judge_groups(todo, jobs):
    """Pictures with 2 or more people (counting them and telling twins apart is hard) are judged by the stronger
    models, the others by the cheap ones. The number of Gemini calls stays about the same; a finished quota falls
    back to the next model by itself."""
    multi = [k for k in todo if _is_multi(jobs[k]["sc"])]
    ms = set(multi)
    single = [k for k in todo if k not in ms]
    out = []
    for lst, models in ((multi, WRITE_MODELS), (single, CHEAP_MODELS)):
        for a in range(0, len(lst), CHECK_BATCH):
            out.append((lst[a:a + CHECK_BATCH], models))
    return out


WRITER_SCENES = ("the writer sits at a wooden writing desk and writes in a thick open book with a pen, an inkwell, a lamp and "
                 "manuscript pages on the desk",
                 "the writer sits at a long reading table in a quiet library, a notebook and a pen in front, tall stacks of books "
                 "and a reading lamp on the table",
                 "the writer sits in an armchair beside a small table and reads a handwritten manuscript, a cup of tea and a "
                 "pile of pages on the table")


def author_gender(book):
    """'woman' / 'man' / '' from Wikidata (property P21) of the author's Wikipedia page: no Gemini call, no guessing."""
    try:
        t = wiki_search("en", book.get("author_en") or "")
        if not t:
            return ""
        d = wiki_api("en", {"action": "query", "prop": "pageprops", "ppprop": "wikibase_item", "redirects": 1, "titles": t[0]})
        qid = d["query"]["pages"][0]["pageprops"]["wikibase_item"]
        r = requests.get(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json", headers=WIKI_HEADERS, timeout=40)
        r.raise_for_status()
        cl = r.json()["entities"][qid]["claims"].get("P21") or []
        sex = cl[0]["mainsnak"]["datavalue"]["value"]["id"]
        return {"Q6581072": "woman", "Q6581097": "man"}.get(sex, "")
    except Exception as e:  # noqa: BLE001
        print("Author gender not found:", e, flush=True)
        return ""


WRITER_CUES = {
    "woman": "a woman with long hair pinned up in a bun and a few loose strands on her shoulders, narrow shoulders, a long dress with a shawl",
    "man": "a man with short hair, broad shoulders, a jacket and trousers",
}
_MALE_WORDS = re.compile(r"\b(man|male|he|his|him|gentleman|beard|mustache|moustache|boy)\b", re.I)
_FEMALE_WORDS = re.compile(r"\b(woman|female|she|her|lady|girl|dress|skirt|blouse)\b", re.I)


def writer_look(a):
    """The look sheet of the writer. The gender is a fact (Wikidata): when the sheet of the robot says the other gender, or
    says nothing visible from behind, the sheet is built from clear gender cues (hair, shoulders, clothes), because a
    writer seen from behind is otherwise drawn as a man."""
    a = a or {}
    gender = a.get("gender") if a.get("gender") in WRITER_CUES else ""
    look = str(a.get("look_en") or "").strip()
    if not gender:
        return look or "a writer"
    wrong = _MALE_WORDS.search(look) if gender == "woman" else _FEMALE_WORDS.search(look)
    if not look or wrong:
        return WRITER_CUES[gender]
    return f"{look}; {WRITER_CUES[gender]}"


def design_author(book, src):
    """ONE small Gemini call: who the writer is (gender, age, outfit of the writer's time) and 4 indoor writing places
    of the writer's time. The look is pasted into every writer picture, so it never changes."""
    prompt = f"""The novel "{book['title_en']}" was written by {book['author_en']}. Return JSON with exactly these keys:
- "gender": "woman" or "man" (the real gender of this writer{(": it is a " + book["author_gender"]) if book.get("author_gender") else ""}).
- "age": the age of the writer when the book was written (a number).
- "look_en": 25 to 35 English words: gender, age, hair (length and style, visible from behind), build and the clothes (colors)
  of that writer in the time when the book was written. The gender in the words is the one of "gender" above. No face details.
- "scenes_en": 4 English texts of 25 to 40 words, each ONE different INDOOR place and activity that has to do with
  writing, fitting the writer's own time (a writing desk with pen and ink; a typewriter only if typewriters already existed;
  a library reading table with stacks of books; an armchair with a manuscript...). Each starts with "the writer sits" and
  names 4 concrete objects. Everything stands in the LOWER part of the picture; nothing is outdoors."""
    try:
        d = gemini_json(prompt, minutes=4, models=WRITE_MODELS)
        sc = [" ".join(str(x).split()) for x in (d.get("scenes_en") or []) if str(x).strip()][:4]
        look = " ".join(str(d.get("look_en") or "").split())
        gender = "woman" if str(d.get("gender") or "").lower().startswith("w") else "man"
        gender = book.get("author_gender") or gender          # the fact from Wikidata wins over the guess
        if look and sc:
            print(f"Writer designed: {gender}, {look[:80]}", flush=True)
            return {"gender": gender, "look_en": look, "scenes_en": sc}
    except GeminiQuotaExhausted:
        raise
    except Exception as e:  # noqa: BLE001
        print("Writer design skipped:", e, flush=True)
    return {}


def writer_desk_plan(era, author=None, n=0):
    """Parts about the author, the book or its message: ONE picture plan for all three. The writer (always the same
    person, always indoors) works in a writing-related place in the lower half; the upper part stays an empty wall,
    where the big book with the English names is laid later (make_title_cards)."""
    a = author or {}
    scenes = a.get("scenes_en") or list(WRITER_SCENES)
    who = writer_look(a)
    gender = a.get("gender") or "person"
    visual = (f"Vertical black-and-white pencil drawing scene in two parts, INDOORS in a room, never outdoors. LOWER half: {scenes[n % len(scenes)]}. "
              f"The writer is {who}; seen from behind (back view), the head and the body facing the SAME direction, never a head turned around; exactly "
              f"one head, two arms and two hands. UPPER 45 percent of the picture: a completely empty plain warm-toned wall with "
              f"a faint soft shadow, no objects, no window, no shelves, no people, no text.")
    return {"kind": "author", "visual_en": visual, "event_en": "the writer works on a book", "cover": True,
            "characters_present": [], "others": [], "roles": {},
            "people_en": f"exactly one person: the writer, a {gender}, seen from behind (back view, head and body facing the wall), nobody else",
            "must_show": [f"one {gender} writer indoors", "a writing-related place (desk, typewriter or library table)",
                          "an empty plain wall in the upper part of the picture"]}


def author_library_plan(author=None):
    """The ONE picture about the writer: the first picture of the video. A library; on its wall a framed painting of a samurai."""
    a = author or {}
    who = writer_look(a)
    gender = a.get("gender") or "person"
    visual = (f"Vertical black-and-white pencil drawing scene INDOORS in a quiet old library: tall wooden bookshelves full of books on both sides, "
              f"a wooden reading table in the lower part with an open book, a pen and a warm lamp, and the back wall between the two "
              f"bookshelves, in the upper middle of the picture, is a large completely EMPTY plain smooth wall area (nothing hangs on it: "
              f"no frame, no picture, no shelf, no lamp, no window, no plant, no clock, no text), about 36 percent of the picture width "
              f"and 36 percent of its height, with the writer's head and shoulders far BELOW it. A {gender} writer, {who}, sits at the table facing the back wall, seen from BEHIND (back "
              f"view), looking down at the open book; the head and the body face the SAME direction, the neck is natural and "
              f"the head is never turned around; the face is not visible; exactly one person, one head, two arms, two "
              f"hands; no text anywhere.")
    return {"kind": "author", "visual_en": visual, "event_en": "the writer sits in a library", "cover": False,
            "characters_present": [], "others": [], "roles": {}, "emotion_en": "",
            "people_en": f"exactly one person: the writer, a {gender}, seen from behind (back view, head and body facing the wall), nobody else",
            "painting": True,
            "must_show": [f"one {gender} writer indoors", "a library with bookshelves", "a large empty plain wall area in the upper middle of the picture"]}


def _ass_rect(x1, y1, x2, y2):
    return f"m {x1} {y1} l {x2} {y1} {x2} {y2} {x1} {y2}"


def _wrap_latin(text, size, width_px):
    import textwrap
    maxch = max(int(width_px / (0.62 * size)), 4)
    return textwrap.wrap(text, maxch, break_long_words=False) or [text]


def cover_ass(title, author, font, bold):
    """An ASS file (drawn by ffmpeg/libass, no extra library) with a BIG book standing in the background: leather cover,
    spine, page edges, gold frame, and the English title and author name on its cover."""
    X1, Y1, X2, Y2 = 80, 70, 640, 590                  # the cover (the upper part of the 720x1280 picture)
    fx1, fy1, fx2, fy2 = X1 + 70, Y1 + 30, X2 - 30, Y2 - 30
    cx = (fx1 + fx2) // 2
    width = fx2 - fx1 - 36

    def shape(layer, color, rects, alpha="00", blur=0):
        path = " ".join(_ass_rect(*r) for r in rects)
        tag = f"{{\\an7\\pos(0,0)\\bord0\\shad0\\1c&H{color}&\\1a&H{alpha}&" + (f"\\blur{blur}" if blur else "") + "\\p1}"
        return f"Dialogue: {layer},0:00:00.00,0:00:10.00,Card,,0,0,0,,{tag}{path}{{\\p0}}"

    ev = [shape(0, "000000", [(X1 + 14, Y1 + 18, X2 + 24, Y2 + 28)], alpha="80", blur=10),          # shadow
          shape(1, "C8E6F1", [(X1 + 8, Y1 + 8, X2 + 16, Y2 + 16)]),                              # page block
          shape(2, "9FB8C4", [(X2 + 4, Y1 + 14, X2 + 5, Y2 + 14), (X2 + 9, Y1 + 14, X2 + 10, Y2 + 14),
                              (X1 + 14, Y2 + 4, X2 + 14, Y2 + 5), (X1 + 14, Y2 + 9, X2 + 14, Y2 + 10)]),   # page lines
          shape(3, "1A1A6B", [(X1, Y1, X2, Y2)]),                                                # leather cover
          shape(4, "0E0E40", [(X1, Y1, X1 + 38, Y2)]),                                           # spine
          shape(5, "37AFD4", [(X1 + 38, Y1, X1 + 42, Y2), (fx1, fy1, fx2, fy1 + 4), (fx1, fy2 - 4, fx2, fy2),
                              (fx1, fy1, fx1 + 4, fy2), (fx2 - 4, fy1, fx2, fy2),
                              (fx1 + 12, fy1 + 12, fx2 - 12, fy1 + 14), (fx1 + 12, fy2 - 14, fx2 - 12, fy2 - 12),
                              (fx1 + 12, fy1 + 12, fx1 + 14, fy2 - 12), (fx2 - 14, fy1 + 12, fx2 - 12, fy2 - 12),
                              (cx - 70, 426, cx + 70, 429)])]                                   # gold lines and frame
    for size in (68, 60, 52, 46, 40, 34):             # the biggest title size that fits in 4 lines inside the frame
        tl = _wrap_latin(title, size, width)
        if len(tl) <= 4 and all(len(x) * 0.62 * size <= width for x in tl):
            break
    ty = 272
    ev.append(f"Dialogue: 6,0:00:00.00,0:00:10.00,Card,,0,0,0,,{{\\an5\\pos({cx},{ty})\\fs{size}\\1c&H37AFD4&\\3c&H000000&"
              f"\\bord2\\shad0\\fsp1}}" + "\\N".join(tl))
    if author:
        al, asz = [author], 34
        for size_try in (42, 36, 30):                  # one line if it fits, else two lines of a smaller size
            one = _wrap_latin(author, size_try, width)
            if len(one) == 1 and len(author) * 0.62 * size_try <= width:
                al, asz = one, size_try
                break
        else:
            al, asz = _wrap_latin(author, 32, width)[:2], 32
        ev.append(f"Dialogue: 6,0:00:00.00,0:00:10.00,Card,,0,0,0,,{{\\an5\\pos({cx},{490 if len(al) == 1 else 480})\\fs{asz}\\1c&HE6F0F5&\\3c&H000000&"
                  f"\\bord2\\shad0\\fsp3}}" + "\\N".join(al))
    return ("[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Card,%s,60,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,%d,0,0,0,100,100,0,0,1,2,0,5,10,10,0,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n") % (
        W, H, font, 1 if bold else 0) + "\n".join(ev) + "\n"


CALM_MAX = float(os.environ.get("AZIM_CALM_MAX") or 26)    # brightness spread (0-255) of a calm plain area of the canvas


def region_spread(img, x, y, rw, rh, w, h):
    """How busy a rectangle of the picture is: the spread (standard deviation) of its brightness. A plain wall or canvas
    corner is small; a head, a lamp, a book or a shelf makes it large. None when the rectangle cannot be read."""
    try:
        x, y = max(int(x), 0), max(int(y), 0)
        rw, rh = max(min(int(rw), w - x), 4), max(min(int(rh), h - y), 4)
        raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(img), "-vf",
                                       f"crop={rw}:{rh}:{x}:{y},scale=64:-2,format=gray", "-f", "rawvideo", "-"], timeout=60)
        if len(raw) < 16:
            return None
        m = sum(raw) / len(raw)
        return (sum((b - m) ** 2 for b in raw) / len(raw)) ** 0.5
    except Exception as e:  # noqa: BLE001
        print(f"Painting check could not read the picture ({e}).", flush=True)
        return None


PAINT_ASPECT = 690 / 402           # height / width of the painting file
PAINT_W_FRAC = float(os.environ.get("AZIM_PAINT_W") or 0.36)       # the painting is this part of the picture width wide


def painting_file():
    """The ONE samurai painting of the channel: the user's own picture (a samurai kneeling and reading a scroll), used WITHOUT any
    change, with the red signature of the channel (a flowing «SR» monogram, the seal 侍 and the name) painted INTO it. It is the same file in EVERY video, so the painting and the signature never change, never move and are never
    missing. The file is written from the data at the end of this script (no extra file in the repository)."""
    p = WORK / "azim_samurai_painting.jpg"
    try:
        if not p.exists():
            p.write_bytes(base64.b64decode(_PAINTING_B64))
        return p
    except Exception as e:  # noqa: BLE001
        print(f"Samurai painting file could not be written ({e}).", flush=True)
        return None


def stamp_painting(jobs):
    """The library picture of the writer has an EMPTY plain wall area on purpose: the fixed painting (with its signature) is
    pasted there. The calmest free place of the wall is used, so the painting is never hidden by anything and nothing that
    stands in front of it can cover its signature (the painting is the front-most thing on the wall)."""
    pf = painting_file()
    if not pf:
        return
    for k, j in enumerate(jobs):
        sc = j.get("sc") or {}
        if not (sc.get("painting") and sc.get("kind") == "author"):
            continue
        img = j["img"]
        try:
            dims = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                            "stream=width,height", "-of", "csv=p=0:s=x", str(img)], text=True).strip()
            w, h = (int(x) for x in dims.split("x"))
            pw = int(w * PAINT_W_FRAC)
            ph = int(pw * PAINT_ASPECT)
            cands = []
            for cxf in (0.5, 0.40, 0.60, 0.33, 0.67):
                for tyf in (0.07, 0.11, 0.15, 0.04):
                    x, y = int(w * cxf - pw / 2), int(h * tyf)
                    if x < 4 or x + pw > w - 4 or y + ph > h * 0.62:
                        continue
                    sd = region_spread(img, x, y, pw, ph, w, h)
                    cands.append(((sd if sd is not None else 255.0) + 6 * abs(cxf - 0.5) + 10 * tyf, x, y, sd))
            if not cands:
                x, y = max((w - pw) // 2, 0), int(h * 0.06)
                sd = None
            else:
                _, x, y, sd = min(cands)
            tmp = f"painting_tmp_{k}.jpg"
            sh(["ffmpeg", "-y", "-loglevel", "error", "-i", img.name, "-i", pf.name, "-filter_complex",
                f"[1:v]scale={pw}:{ph}:flags=lanczos[p];[0:v][p]overlay={x}:{y}", "-frames:v", "1", "-q:v", "2", tmp],
               cwd=WORK)
            shutil.move(str(WORK / tmp), str(img))
            print(f"Samurai painting with its signature placed at {x},{y} ({pw}x{ph}) on the {w}x{h} library picture "
                  f"(wall spread {sd if sd is None else round(sd, 1)}).", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"Samurai painting could not be placed ({e}): the plain picture is used.", flush=True)


def make_title_cards(jobs, book):
    """Parts about the author, the book or its message: the picture (a writer at a desk) is drawn and checked like every
    other picture; here a BIG book with the ENGLISH title and author name is laid over its empty upper wall."""
    cards = [k for k, j in enumerate(jobs) if j.get("card")]
    if not cards:
        return

    def esc(t):
        return re.sub(r"[{}\\]", "", str(t or "")).replace("\n", " ").strip()

    title, author = esc(book.get("title_en")), esc(book.get("author_en"))
    if not title:
        return
    font = setup_sub_font()
    (WORK / "azim_cover.ass").write_text(cover_ass(title, author, font, _sub_bold), encoding="utf-8")
    done = 0
    for k in cards:
        img = jobs[k]["img"]
        tmp = f"cover_tmp_{k}.jpg"
        try:
            sh(["ffmpeg", "-y", "-loglevel", "error", "-i", img.name, "-vf",
                f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                f"subtitles=azim_cover.ass:fontsdir=azim_fonts", "-frames:v", "1", "-q:v", "2", tmp], cwd=WORK)
            shutil.move(str(WORK / tmp), str(img))
            done += 1
        except Exception as e:  # noqa: BLE001
            print(f"Book cover {k} failed ({e}): the plain picture is used.", flush=True)
    print(f"Book covers: {done} of {len(cards)} author / book / message parts got the big book with the English names.",
          flush=True)


def check_and_fix_images(jobs):
    """Every picture is checked (4 per Gemini call). A picture with a low score is drawn again with the missing
    thing at the START of the prompt, up to 2 times; the best picture of all tries is kept. If the check or the
    redraw is not available, the pictures that already exist are kept: the video never fails because of this step."""
    best = {}                                              # picture number -> (score, bytes)
    todo = list(range(len(jobs)))     # every picture is checked (the writer pictures too); a copied (failed) one as well
    for rnd in range(3):
        results = {}
        for grp, models in _judge_groups(todo, jobs):
            try:
                res = check_images([jobs[k] for k in grp], models)
            except GeminiBlocked as e:
                # one picture in the group was refused by Gemini: check them one by one, keep the refused ones as they are
                print(f"Image check blocked for a group ({e}); checking pictures one by one.", flush=True)
                res = []
                for k in grp:
                    try:
                        one = check_images([jobs[k]], models)
                        res.append(one[0] if one else None)
                    except GeminiBlocked:
                        print(f"Image check: picture {k + 1} refused by Gemini, kept as it is.", flush=True)
                        res.append(None)
                    except GeminiQuotaExhausted:
                        res.append(None)
            except GeminiQuotaExhausted as e:
                print(f"Image check stopped ({e}): the pictures are kept as they are.", flush=True)
                res = None
            if res is None:
                continue
            for k, r in zip(grp, res):
                if r is not None:
                    results[k] = r
        if not results:
            break
        low = []
        for k, r in sorted(results.items()):
            score, missing = r[0], r[1]
            box = r[2] if len(r) > 2 else None
            data = jobs[k]["img"].read_bytes()
            if k not in best or score > best[k][0]:
                best[k] = (score, data, box)
            if score < MIN_SCORE:
                print(f"Image check: picture {k + 1} score {score}/10 - {missing}", flush=True)
                low.append((k, missing))
        print(f"Image check round {rnd + 1}: {len(results)} checked, {len(low)} to draw again.", flush=True)
        if rnd >= REDRAW_ROUNDS or not low:
            break
        todo = []
        try:
            for k, missing in low:
                j = jobs[k]
                try:
                    make_image(j["build"], j["img"], j["seed"] + 1000 * (rnd + 1), 0, fix=missing)
                    j["reused"] = False
                    todo.append(k)
                except ImageServiceUnavailable:
                    raise
                except Exception as e:  # noqa: BLE001
                    print(f"Redraw of picture {k + 1} failed ({e}): the others continue.", flush=True)
        except ImageServiceUnavailable as e:
            print(f"Redraw stopped ({e}): the best pictures so far are kept.", flush=True)
            break
    for k, (score, data, box) in best.items():             # keep the best picture of all tries
        jobs[k]["img"].write_bytes(data)
        jobs[k]["painting_box"] = box
    if best:
        low_left = sum(1 for sc_, _, _ in best.values() if sc_ < MIN_SCORE)
        print(f"Image check done: {len(best)} pictures, {low_left} still below {MIN_SCORE}/10.", flush=True)


# ---------------------------------------------------------------- video
def srt_time(t):
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = int(t % 60)
    ms = int((t - int(t)) * 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


SUB_PHRASE_WORDS = int(os.environ.get("AZIM_SUB_WORDS") or 4)    # words per subtitle line (0 = whole sentences)


def chunk_phrases(sent, maxw=None):
    """Like the reference video: a short one-line phrase at a time (inside the dark box), cut at commas first and then
    into equal parts. An English name made of several words is never cut in the middle."""
    maxw = SUB_PHRASE_WORDS if maxw is None else maxw
    if maxw <= 0:
        return [sent]
    out = []
    for part in [p for p in re.split(r"(?<=[\u060c\u061b:,;])\s+", sent) if p.strip()]:
        toks = []
        for w in part.split():
            if toks and re.match(r"[A-Za-z]", w) and re.match(r"[A-Za-z]", toks[-1].split()[-1]):
                toks[-1] += " " + w
            else:
                toks.append(w)
        if len(toks) <= maxw:
            out.append(" ".join(toks))
            continue
        n = -(-len(toks) // maxw)
        size = -(-len(toks) // n)
        out += [" ".join(toks[k:k + size]) for k in range(0, len(toks), size)]
    final = []
    for c in out:                                  # a line must fit the narrow vertical screen: cut long ones in two
        w = c.split()
        if len(c) > 26 and len(w) > 1:
            h = len(w) // 2
            final += [" ".join(w[:h]), " ".join(w[h:])]
        else:
            final.append(c)
    return final or [sent]


_NUM_UNITS = "یک|دو|سه|چهار|پنج|شش|هفت|هشت|نه"
_NUM_TEENS = "ده|یازده|دوازده|سیزده|چهارده|پانزده|شانزده|هفده|هجده|نوزده"
_NUM_TENS = "بیست|سی|چهل|پنجاه|شصت|هفتاد|هشتاد|نود"
_NUM_HUNDREDS = "صد|دویست|سیصد|چهارصد|پانصد|ششصد|هفتصد|هشتصد|نهصد"
_NUM_BIG = "هزار|میلیون|میلیارد"
_NUM_ALL = f"{_NUM_BIG}|{_NUM_HUNDREDS}|{_NUM_TENS}|{_NUM_TEENS}|{_NUM_UNITS}"
_GLUED_NUM = re.compile(
    rf"(?<![\u0600-\u06FF])({_NUM_TENS}|{_NUM_HUNDREDS}|{_NUM_BIG})\u200c?و\u200c?"
    rf"({_NUM_UNITS}|{_NUM_TEENS}|{_NUM_TENS}|{_NUM_HUNDREDS})"
    rf"(?:(?![\u0600-\u06FF])|(?=\u200c?و\u200c?(?:{_NUM_ALL})(?:[^\u0600-\u06FF]|$|\u200c?و)))")


def fix_numbers(text):
    """A number written as ONE glued word («هفتادوهفت», «بیست‌وپنج», «صدوبیست») is read wrongly by the voice («هفتید و هفت»):
    every part of a number becomes its own word with a separate «و»."""
    t = str(text or "")
    for _ in range(6):
        t2 = _GLUED_NUM.sub(r"\1 و \2", t)
        if t2 == t:
            break
        t = t2
    return t


def split_sentences(text):
    parts = [p.strip() for p in re.split(r"(?<=[.!?؟…])\s+", text.strip()) if p.strip()]
    return parts or [text.strip()]


_sub_font_path = None
_sub_bold = False


def find_sub_font_file():
    """The bold Persian subtitle font file: already in azim_work/azim_fonts, or installed, or downloaded (free).
    Returns a path or None (then the old tested DejaVu Sans is used)."""
    global _sub_font_path
    if _sub_font_path is not None:
        return _sub_font_path or None
    d = WORK / "azim_fonts"
    d.mkdir(parents=True, exist_ok=True)
    target = d / "Vazirmatn-Bold.ttf"
    if not (target.is_file() and target.stat().st_size > 50000):
        try:
            out = subprocess.check_output(["fc-list", "Vazirmatn:bold", "file"], text=True)
            for x in out.splitlines():
                x = x.strip().rstrip(":")
                if x.lower().endswith(".ttf") and Path(x).is_file():
                    shutil.copy(x, target)
                    break
        except Exception:  # noqa: BLE001
            pass
    if not (target.is_file() and target.stat().st_size > 50000):
        import urllib.request
        for url in SUB_FONT_URLS:
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "azim-bot"}),
                                            timeout=60) as r:
                    data = r.read()
                if len(data) > 50000 and data[:4] in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
                    target.write_bytes(data)
                    print("Subtitle font downloaded:", url, flush=True)
                    break
            except Exception as e:  # noqa: BLE001
                print("Subtitle font download failed:", url, short_error(str(e)), flush=True)
    _sub_font_path = str(target) if (target.is_file() and target.stat().st_size > 50000) else ""
    return _sub_font_path or None


_font_cmap = None


def font_chars():
    """Code points of the subtitle font (to find characters that would be drawn as rectangles)."""
    global _font_cmap
    if _font_cmap is None:
        _font_cmap = set()
        try:
            from fontTools.ttLib import TTFont
            path = find_sub_font_file() or subprocess.check_output(["fc-match", "-f", "%{file}", "DejaVu Sans"],
                                                                   text=True).strip()
            _font_cmap = set(TTFont(path, fontNumber=0, lazy=True).getBestCmap().keys())
            print(f"Subtitle font file: {path} ({len(_font_cmap)} characters)", flush=True)
        except Exception as e:  # noqa: BLE001
            print("Subtitle font check unavailable:", e, flush=True)
    return _font_cmap


_dropped_chars = {}


_OK_CHARS = re.compile(r"[\u0621-\u063A\u0641-\u064A\u067E\u0686\u0698\u06A9\u06AF\u06CC\u06C0"
                       r"A-Za-z0-9\u06F0-\u06F9\s.,;:!?\u2026\u00AB\u00BB\u060C\u061B\u061F\"'()\-]")


def clean_subtitle(text):
    """Only Persian letters, names, digits and ordinary punctuation survive: every mark or invisible character that
    could be drawn as a rectangle is removed (the removed code points are listed in the voice report)."""
    cm = font_chars()
    out = []
    t = unicodedata.normalize("NFKC", text).replace("...", "\u2026")
    t = t.replace("ي", "ی").replace("ك", "ک")
    for ch in t:
        cp = ord(ch)
        if ch == "\u200c":                       # half-space: keep only if the font really has it
            if cm and cp in cm:
                out.append(ch)
            else:
                _dropped_chars[cp] = _dropped_chars.get(cp, 0) + 1
            continue
        if not _OK_CHARS.fullmatch(ch):
            _dropped_chars[cp] = _dropped_chars.get(cp, 0) + 1
            continue
        if cm and cp >= 0x250 and cp not in cm and not ch.isspace():
            _dropped_chars[cp] = _dropped_chars.get(cp, 0) + 1
            continue
        out.append(ch)
    return " ".join("".join(out).split())


def fa_typo(t):
    """Persian typing rules: Persian punctuation, no space before it and one space after it, «» quotes."""
    t = t.replace(",", "\u060c").replace(";", "\u061b").replace("?", "\u061f")
    t = re.sub(r'"([^"]+)"', "\u00ab\\1\u00bb", t)
    t = re.sub(r"\s+([\u060c\u061b\u061f!.:\u2026\u00bb)])", r"\1", t)
    t = re.sub(r"([\u060c\u061b\u061f!:])(?=\S)", r"\1 ", t)
    t = re.sub(r"([\u00ab(])\s+", r"\1", t)
    return " ".join(t.split())


def image_aspect(path):
    """width / height of a picture (0 when it cannot be read)."""
    try:
        w, h = (int(x) for x in subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of",
             "csv=p=0:s=x", str(path)], text=True).strip().split("x"))
        return w / h if h else 0
    except Exception:  # noqa: BLE001
        return 0


def setup_sub_font():
    """Copies ONE tested font into azim_work/azim_fonts: the lam-alef «لا» ligature (U+FEFB) and all Persian letters
    must exist in it, otherwise another installed font that has them is chosen. Returns the font family name."""
    from fontTools.ttLib import TTFont
    need = [0xFEFB, 0x0644, 0x0627, 0x067E, 0x0686, 0x0698, 0x06A9, 0x06AF, 0x06CC, 0x200C]
    cands = []
    try:
        vz = find_sub_font_file()
        if vz:
            cands.append(vz)
        cands.append(subprocess.check_output(["fc-match", "-f", "%{file}", "DejaVu Sans"], text=True).strip())
        out = subprocess.check_output(["fc-list", ":charset=fefb 644 627 67e 686 698 6a9 6af 6cc", "file"], text=True)
        cands += [x.strip().rstrip(":") for x in out.splitlines() if x.strip()]
    except Exception as e:  # noqa: BLE001
        print("Font search failed:", e, flush=True)
    cands.append("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    pref = ("vazirmatn-bold", "dejavusans.ttf", "notonaskharabic", "notosansarabic")
    cands.sort(key=lambda f: next((k for k, x in enumerate(pref) if x in Path(f).name.lower()), 99))
    for path in cands:
        try:
            if not Path(path).is_file() or Path(path).suffix.lower() not in (".ttf", ".otf"):
                continue
            f = TTFont(path, fontNumber=0, lazy=True)
            cm = f.getBestCmap()
            if not all(c in cm for c in need[:-1]):
                continue
            fam = f["name"].getBestFamilyName()
            d = WORK / "azim_fonts"
            d.mkdir(parents=True, exist_ok=True)
            shutil.copy(path, d / Path(path).name)
            print(f"Subtitle font: {fam} ({path})", flush=True)
            global _sub_bold
            _sub_bold = "bold" in Path(path).name.lower()
            note(f"Subtitle font = {fam} ({path}); ZWNJ in font: {need[-1] in cm}")
            return fam
        except Exception as e:  # noqa: BLE001
            print("Font skipped:", path, e, flush=True)
    return "DejaVu Sans"


SUB_LEAD = float(os.environ.get("AZIM_SUB_LEAD") or 0.18)      # seconds: a subtitle is shown a little before its words


def subtitle_text(text, names=None):
    """Subtitle: no diacritics; English names stay in English (Latin) letters exactly as the voice says them.
    A line that starts with a Latin name gets a right-to-left mark so the Persian line keeps its direction."""
    return fa_typo(clean_subtitle(DIAC.sub("", text)))


_LATIN_RUN = re.compile(r"[A-Za-z](?:[A-Za-z0-9'\u2019\-]|\.(?=[A-Za-z]))*(?:\.? (?=[A-Za-z])[A-Za-z](?:[A-Za-z0-9'\u2019\-]|\.(?=[A-Za-z]))*)*")


def bidi_wrap(line):
    """A line with English names: the whole line is a right-to-left paragraph (RLI...PDI) and every English name is
    an isolated left-to-right run (LRI...PDI), so the order of the Persian sentence is never scrambled."""
    if not _LATIN_RUN.search(line):
        return line
    return "\u2067" + _LATIN_RUN.sub(lambda m: "\u2066" + m.group(0) + "\u2069", line) + "\u2069"


def ensure_names(data, book):
    """Persian spelling for EVERY English name in the narration (one cheap Gemini call only for the missing ones)."""
    names = dict(data.get("names") or {})
    found = set()
    for sc in data["scenes"]:
        for is_name, piece in split_names(sc["narration_fa"]):
            if is_name:
                found.add(piece.strip())
    missing = sorted(n for n in found if n and n not in names)
    if missing:
        prompt = f"""Write each English name below in Persian letters, as an Iranian reader would say it, WITHOUT vowel
marks, for subtitles of a video about the novel "{book['title_en']}". Return JSON {{"names": {{"English": "فارسی"}}}}.
Names: {json.dumps(missing, ensure_ascii=False)}"""
        try:
            got = gemini_json(prompt, minutes=4, models=CHEAP_MODELS).get("names") or {}
            for k, v in got.items():
                if str(k).strip() in missing and str(v).strip():
                    names[str(k).strip()] = str(v).strip()
        except (GeminiBlocked, GeminiQuotaExhausted):
            raise
        except Exception as e:  # noqa: BLE001
            print("Name spelling skipped:", e, flush=True)
    data["names"] = names
    return data


def build_video(scenes, durs, out_path, names=None):
    clips = []
    srt = []
    n = 0
    t = 0.0
    for i, (s, d) in enumerate(zip(scenes, durs)):
        length = d + PAD
        total_frames = math.ceil(length * FPS)
        beats = s.get("beats") or [s["narration_fa"]]
        bw = [max(len(x), 1) for x in beats]
        used, vparts = 0, []
        for j in range(len(beats)):
            frames = max(total_frames - used, 1) if j == len(beats) - 1 else max(round(total_frames * bw[j] / sum(bw)), 1)
            used += frames
            if ZOOM_MAX <= 1.0001:
                zoom = "z='1'"
            else:
                inc = (ZOOM_MAX - 1) / max(frames, 1)    # zoom speed spread over the whole picture (no early stop)
                zoom = (f"z='min(zoom+{inc:.6f},{ZOOM_MAX:.4f})'" if (i + j) % 2 == 0
                        else f"z='if(eq(on,1),{ZOOM_MAX:.4f},max(zoom-{inc:.6f},1.0))'")
            # The WHOLE picture is fitted inside the 9:16 frame (nothing is cut off). A picture that is not 9:16 gets a
            # blurred copy of itself behind it. Big lanczos upscale BEFORE zoompan (no shimmer) + light sharpening.
            asp = image_aspect(WORK / f"img_{i:02d}_{j}.jpg")
            if asp and abs(asp - W / H) / (W / H) <= 0.30:
                # a picture that is (nearly) 9:16, even a shorter one: it FILLS the frame (a little is cropped); no bar at all
                vf = (f"scale=1472:2616:flags=lanczos:force_original_aspect_ratio=increase,crop=1440:2560,"
                      f"unsharp=5:5:0.6:5:5:0.0,"
                      f"zoompan={zoom}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},"
                      "format=yuv420p")
            else:                                  # a very different shape (square...): fitted, on a DARK blurred copy
                vf = (f"split[a][b];[a]scale=360:640:force_original_aspect_ratio=increase,crop=360:640,boxblur=12:4,"
                      f"eq=brightness=-0.45:saturation=0.8,scale=1440:2560[bg];"
                      f"[b]scale=1440:2560:flags=lanczos:force_original_aspect_ratio=decrease[fg];"
                      f"[bg][fg]overlay=(W-w)/2:(H-h)/2,unsharp=5:5:0.6:5:5:0.0,"
                      f"zoompan={zoom}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},"
                      "format=yuv420p")
            vname = f"vclip_{i:02d}_{j}.mp4"
            sh(["ffmpeg", "-y", "-loglevel", "error", "-i", f"img_{i:02d}_{j}.jpg", "-vf", vf,
                "-frames:v", str(frames), "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", vname], cwd=WORK)
            vparts.append(vname)
        (WORK / f"vl_{i:02d}.txt").write_text("".join(f"file '{v}'\n" for v in vparts), encoding="utf-8")
        sh(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", f"vl_{i:02d}.txt",
            "-c", "copy", f"vid_{i:02d}.mp4"], cwd=WORK)
        clip = WORK / f"clip_{i:02d}.mp4"
        sh(["ffmpeg", "-y", "-loglevel", "error", "-i", f"vid_{i:02d}.mp4", "-i", f"voice_{i:02d}.mp3",
            "-af", "apad", "-t", f"{length:.2f}", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "64k", "-ar", "44100", "-ac", "1", clip.name], cwd=WORK)
        clips.append(clip.name)

        # subtitles: always the clean text (no diacritics). EVERY phrase gets its own place on the real pauses of the voice
        # (one dynamic-programming cut for the whole paragraph), and is shown SUB_LEAD seconds early: the voice is never ahead.
        full_sents = split_sentences(subtitle_text(s["narration_fa"], names))
        phr_all = []
        for fs in full_sents:
            ph = chunk_phrases(fs)
            for k_, x in enumerate(ph):
                phr_all.append((x, k_ == len(ph) - 1))
        pspans = None
        if len(phr_all) >= 2:
            try:
                vmp3 = WORK / f"voice_{i:02d}.mp3"
                real = probe_duration(vmp3)
                extras = [0.3 if end_ else (0.12 if x.rstrip().endswith(("،", ",")) else 0.0) for x, end_ in phr_all]
                cuts = split_by_silence(vmp3, [max(len(x), 1) for x, _ in phr_all], extras,
                                        min_gap=0.25, min_sil=0.1, window_min=0.9, win_frac=0.2)
                k_ = d / real if real > 0 else 1.0
                pspans = [(a * k_, b * k_) for a, b in cuts]
            except Exception as e:  # noqa: BLE001
                note(f"  Subtitle timing of paragraph {i} by pauses failed ({str(e)[:80]}): characters are used.")
        if not pspans or len(pspans) != len(phr_all):
            wts = [max(len(x), 1) for x, _ in phr_all] or [1]
            tot_w, acc = float(sum(wts)), 0.0
            pspans = []
            for w_ in wts:
                pspans.append((d * acc / tot_w, d * (acc + w_) / tot_w))
                acc += w_
        for idx_, ((x, end_), (sa, sb)) in enumerate(zip(phr_all, pspans)):
            start = max(t + sa - SUB_LEAD, 0.0)
            end = max(t + sb - SUB_LEAD, start + 0.25)
            if idx_ == len(phr_all) - 1:
                end += PAD * 0.5
            n += 1
            srt.append(f"{n}\n{srt_time(start)} --> {srt_time(end)}\n{bidi_wrap(x)}\n")
        t += length

    if _dropped_chars:
        note("Subtitle characters removed (would be rectangles): "
             + ", ".join(f"U+{c:04X} x{n}" for c, n in sorted(_dropped_chars.items())))
    (WORK / "list.txt").write_text("".join(f"file '{c}'\n" for c in clips), encoding="utf-8")
    (WORK / "subs.srt").write_text("\n".join(srt), encoding="utf-8")
    sh(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
        "-i", "list.txt", "-c", "copy", "joined.mp4"], cwd=WORK)

    font_family = setup_sub_font()
    # like the reference video: white bold text, centered, on a semi-transparent dark box (BorderStyle=3: the box is
    # drawn with OutlineColour; &H80 = about half see-through), no outline/shadow, placed in the lower part of the picture
    style = (f"FontName={font_family},FontSize={SUB_FONT_SIZE},Bold={1 if _sub_bold else 0},"
             "PrimaryColour=&H00FFFFFF,OutlineColour=&H8C141414,BackColour=&H8C141414,"
             "BorderStyle=3,Outline=2,Shadow=0,Alignment=2,MarginV=62,MarginL=12,MarginR=12")
    vf_final = f"subtitles=subs.srt:fontsdir=azim_fonts:force_style='{style}'"
    if WATERMARK:     # channel name in the bottom-left corner of every picture: same font, one size smaller than the subtitle
        wm = re.sub(r"[{}\\]", "", WATERMARK)
        (WORK / "wm.ass").write_text(
            "[Script Info]\nScriptType: v4.00+\nPlayResX: 384\nPlayResY: 288\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: WM,{font_family},{max(SUB_FONT_SIZE - 1, 1)},&H00FFFFFF,&H00FFFFFF,&H8C141414,&H8C141414,"
            f"{1 if _sub_bold else 0},0,0,0,100,100,0,0,3,2,0,1,10,10,10,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            f"Dialogue: 0,0:00:00.00,9:59:59.00,WM,,0,0,0,,{wm}\n", encoding="utf-8")
        vf_final += ",subtitles=wm.ass:fontsdir=azim_fonts"
    # the only lossy pass: best quality that still fits under Telegram's 50 MB limit
    fits = False
    for crf, maxrate in (("23", "720k"), ("27", "520k"), ("31", "380k")):
        sh(["ffmpeg", "-y", "-loglevel", "error", "-i", "joined.mp4",
            "-vf", vf_final,
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
IRAN_TZ = timezone(timedelta(hours=3, minutes=30))        # Iran has no daylight saving time any more
PUBLISH_HOURS = (os.environ.get("AZIM_PUBLISH_HOURS") or "8,15").strip()     # Iran hours; "off" = post at once
PUBLISH_MAX_WAIT = int(os.environ.get("AZIM_PUBLISH_MAX_WAIT") or 75) * 60   # never wait longer than this


def wait_for_publish_time():
    """The finished video waits until the next publishing hour (Iran time, default 08:00 and 15:00) if that hour
    is at most PUBLISH_MAX_WAIT seconds away. A video that is already late, or a manual run far from a publishing
    hour, is posted at once. The wait never goes beyond the job's time budget."""
    if PUBLISH_HOURS.lower() in ("off", "0", "no"):
        return
    try:
        hours = sorted({int(x) for x in PUBLISH_HOURS.split(",") if x.strip()})
    except ValueError:
        print(f"AZIM_PUBLISH_HOURS={PUBLISH_HOURS!r} is not understood: posting at once.", flush=True)
        return
    now = datetime.now(IRAN_TZ)
    slots = [datetime(d.year, d.month, d.day, h, 0, tzinfo=IRAN_TZ)
             for d in (now.date(), now.date() + timedelta(days=1)) for h in hours]
    nxt = [t for t in slots if t > now]
    if not nxt:
        return
    wait = (min(nxt) - now).total_seconds()
    left = JOB_BUDGET - (time.time() - START_TIME) - 600
    if wait > PUBLISH_MAX_WAIT or wait > left:
        print(f"Iran time is {now:%H:%M}: the next publishing hour is {min(nxt):%H:%M} ({wait / 60:.0f} min away): "
              f"posting now.", flush=True)
        return
    print(f"Video is ready at {now:%H:%M} Iran time: waiting {wait / 60:.0f} min and posting at "
          f"{min(nxt):%H:%M}.", flush=True)
    end = time.time() + wait
    while time.time() < end:
        time.sleep(min(60, max(end - time.time(), 0)))


def send_video(path, caption):
    with open(path, "rb") as f:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo",
            data={"chat_id": CHANNEL_ID, "caption": caption,
                  "supports_streaming": "true", "width": W, "height": H},
            files={"video": f}, timeout=1200)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram error: {r.status_code} {r.text[:300]}")


# ---------------------------------------------------------------- the website (GitHub Pages, free)
# After every posted video the bot also writes ONE clean web page about the novel (the same summary that was spoken) into the
# folder docs/, plus the home page, sitemap.xml and robots.txt. GitHub Pages publishes the folder; Google finds the pages and
# learns the name of the channel. Everything is static text (no tracking, no cost). A failure here NEVER stops a video.
SITE_NAME = os.environ.get("AZIM_SITE_NAME") or "Samurai Reader"
SITE_DIR = ROOT / "docs"
SITE_META = SITE_DIR / "azim_site_pages.json"
_SITE_PARTS = (("open", "معرفی کتاب"), ("heroes", "شخصیت‌های اصلی"), ("story", "خلاصهٔ داستان"), ("wrap", "جمع‌بندی"))
_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

_SITE_CSS = """
:root{--bg:#f6f4ef;--card:#ffffff;--ink:#1b1b1f;--muted:#6a6a73;--line:#e4e0d6;--accent:#b3261e;--accent-ink:#ffffff}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1c1c20;--ink:#ececf0;--muted:#a0a0aa;--line:#2c2c33;--accent:#e0584f;--accent-ink:#141416}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"Vazirmatn","Noto Sans Arabic",Tahoma,system-ui,sans-serif;line-height:2;font-size:17px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.top{background:var(--card);border-bottom:1px solid var(--line)}
.top .in{max-width:820px;margin:0 auto;padding:14px 20px;display:flex;align-items:center;justify-content:space-between;gap:12px}
.brand{font-weight:800;font-size:19px;color:var(--ink);letter-spacing:.2px;direction:ltr}
.brand b{color:var(--accent)}
.btn{display:inline-block;background:var(--accent);color:var(--accent-ink);padding:8px 18px;border-radius:999px;font-weight:700;font-size:15px;line-height:1.8}
.btn:hover{text-decoration:none;opacity:.9}
main{max-width:820px;margin:0 auto;padding:28px 20px 56px}
.hero{background:var(--card);border:1px solid var(--line);border-radius:18px;overflow:hidden}
.hero img{display:block;width:100%;height:auto;max-height:560px;object-fit:cover;object-position:center 22%;background:#ddd}
.hero .t{padding:22px 24px 26px}
h1{margin:0 0 6px;font-size:28px;line-height:1.7}
.sub{color:var(--muted);font-size:15px;margin:0}
.sub bdi{direction:ltr}
article{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:8px 26px 20px;margin-top:20px}
h2{font-size:21px;margin:26px 0 4px;padding-right:12px;border-right:4px solid var(--accent);line-height:1.8}
p{margin:10px 0;text-align:justify}
.cta{margin-top:22px;background:var(--card);border:1px solid var(--line);border-radius:18px;padding:22px 24px;text-align:center}
.cta p{text-align:center;margin:0 0 12px}
.more{margin-top:34px}.more h3{font-size:18px;margin:0 0 10px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}
.card{display:block;background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden;color:var(--ink)}
.card:hover{text-decoration:none;border-color:var(--accent)}
.card img{display:block;width:100%;aspect-ratio:3/4;object-fit:cover;object-position:center 22%;background:#ddd}
.card .c{padding:10px 14px 12px}.card .c b{display:block;font-size:16px;line-height:1.7}.card .c span{font-size:13px;color:var(--muted)}
.card.noimg .c{padding:16px 14px}
footer{max-width:820px;margin:0 auto;padding:0 20px 40px;color:var(--muted);font-size:13.5px;line-height:1.9;text-align:center}
footer a{color:var(--muted);text-decoration:underline}
.lead{font-size:18px;color:var(--muted);margin:6px 0 22px}
@media (max-width:560px){body{font-size:16px}h1{font-size:23px}article{padding:4px 18px 14px}.hero .t{padding:18px}}
"""


def _site_slug(title_en):
    sl = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", title_en).encode("ascii", "ignore").decode().lower()).strip("-")
    return sl or "book"


def _site_base():
    u = (os.environ.get("AZIM_SITE_URL") or "").strip().rstrip("/")
    if u:
        return u
    repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}"
    return ""


def _site_channel_url():
    u = (os.environ.get("AZIM_CHANNEL_URL") or "").strip()
    if u:
        return u
    cid = str(CHANNEL_ID).strip()
    if cid.startswith("@"):
        return "https://t.me/" + cid[1:]
    try:
        r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getChat", params={"chat_id": cid}, timeout=30)
        un = ((r.json() or {}).get("result") or {}).get("username")
        if un:
            return "https://t.me/" + un
    except Exception:  # noqa: BLE001
        pass
    return ""


def _fa_html(text):
    """Escaped text; every English name/word is wrapped in <bdi> so that the right-to-left sentence never scrambles."""
    import html as _h
    t = _h.escape(str(text), quote=False)
    return re.sub(r"[A-Za-z][A-Za-z0-9'’\-]*(?: [A-Z][A-Za-z0-9'’\-]*)*", lambda m: f"<bdi>{m.group(0)}</bdi>", t)


def _attr(text):
    import html as _h
    return _h.escape(str(text), quote=True)


def _site_head(title, desc, canonical, base, image=None, extra_ld=None, kind="article"):
    gsc = (os.environ.get("AZIM_GSC_TOKEN") or "").strip()
    og_img = f'<meta property="og:image" content="{_attr(image)}">\n<meta name="twitter:card" content="summary_large_image">' if image \
        else '<meta name="twitter:card" content="summary">'
    can = f'<link rel="canonical" href="{_attr(canonical)}">\n<meta property="og:url" content="{_attr(canonical)}">' if canonical else ""
    ver = f'<meta name="google-site-verification" content="{_attr(gsc)}">' if gsc else ""
    ld = f'<script type="application/ld+json">{json.dumps(extra_ld, ensure_ascii=False)}</script>' if extra_ld else ""
    return f"""<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_attr(title)}</title>
<meta name="description" content="{_attr(desc)}">
<meta name="robots" content="index, follow, max-image-preview:large">
{ver}
{can}
<meta property="og:type" content="{kind}">
<meta property="og:site_name" content="{_attr(SITE_NAME)}">
<meta property="og:locale" content="fa_IR">
<meta property="og:title" content="{_attr(title)}">
<meta property="og:description" content="{_attr(desc)}">
{og_img}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;700;800&display=swap" rel="stylesheet">
<style>{_SITE_CSS}</style>
{ld}
</head>
"""


def _site_top(home_href, channel):
    btn = f'<a class="btn" href="{_attr(channel)}" rel="noopener" target="_blank">عضویت در کانال</a>' if channel else ""
    return (f'<body>\n<header class="top"><div class="in"><a class="brand" href="{_attr(home_href)}">'
            f'{_fa_html(SITE_NAME.split(" ", 1)[0])}<b> {_fa_html(SITE_NAME.split(" ", 1)[1]) if " " in SITE_NAME else ""}</b></a>{btn}</div></header>\n')


def _site_footer(extra=""):
    return (f'<footer>{extra}<p>© {date.today().year} {_fa_html(SITE_NAME)}</p></footer>\n</body>\n</html>\n')


def _site_card(p, prefix):
    img = f'<img src="{_attr(prefix + p["slug"] + "/cover.jpg")}" alt="{_attr(p["title_fa"])}" loading="lazy">' if p.get("cover") else ""
    cls = "card" if p.get("cover") else "card noimg"
    return (f'<a class="{cls}" href="{_attr(prefix + p["slug"] + "/")}">{img}<div class="c"><b>{_fa_html(p["title_fa"])}</b>'
            f'<span>{_fa_html(p["author_fa"])}</span></div></a>')


def _site_write_index(pages, base, channel):
    pages = sorted(pages, key=lambda p: (p.get("date", ""), p["slug"]), reverse=True)
    n = len(pages)
    title = f"{SITE_NAME} | خلاصهٔ رمان‌های معروف جهان"
    desc = (f"{SITE_NAME}: خلاصهٔ روان و دقیق رمان‌های معروف جهان به فارسی، همراه با ویدیو در کانال تلگرام. "
            f"تا امروز {str(n).translate(_PERSIAN_DIGITS)} رمان.")
    ld = {"@context": "https://schema.org", "@type": "WebSite", "name": SITE_NAME, "alternateName": ["SamuraiReader", "Samurai Reader fa"],
          "inLanguage": "fa", "description": desc}
    if base:
        ld["url"] = base + "/"
    cards = "\n".join(_site_card(p, "") for p in pages)
    cta = (f'<div class="cta"><p>ویدیوی تصویری هر خلاصه را در کانال تلگرام {_fa_html(SITE_NAME)} ببین.</p>'
           f'<a class="btn" href="{_attr(channel)}" rel="noopener" target="_blank">رفتن به کانال</a></div>') if channel else ""
    html_ = (_site_head(title, desc, (base + "/") if base else "", base, extra_ld=ld, kind="website") + _site_top("./", channel)
             + f'<main>\n<h1>{_fa_html(SITE_NAME)}</h1>\n<p class="lead">خلاصهٔ رمان‌های معروف جهان، روان و فشرده، با ویدیو در تلگرام.</p>\n'
               f'<div class="grid">\n{cards}\n</div>\n{cta}\n</main>\n'
             + _site_footer())
    (SITE_DIR / "index.html").write_text(html_, encoding="utf-8")


def publish_site_page(book, src, scenes, cover_img=None, site_dir=None):
    """Writes docs/<slug>/index.html (+ cover.jpg), the home page, sitemap.xml, robots.txt, 404.html, .nojekyll."""
    global SITE_DIR, SITE_META
    if site_dir is not None:
        SITE_DIR, SITE_META = Path(site_dir), Path(site_dir) / "azim_site_pages.json"
    title_en = str(book.get("title_en") or "").strip()
    title_fa = str(book.get("title_fa") or title_en).strip()
    author_en = str(book.get("author_en") or "").strip()
    author_fa = str(book.get("author_fa") or author_en).strip()
    base, channel = _site_base(), _site_channel_url()
    slug = _site_slug(title_en)
    pdir = SITE_DIR / slug
    pdir.mkdir(parents=True, exist_ok=True)

    sections = []
    for part, head in _SITE_PARTS:
        paras = [" ".join(str(sc.get("narration_fa") or "").split()) for sc in scenes if sc.get("part") == part]
        paras = [x for x in paras if x]
        if paras:
            sections.append((part, head, paras))
    if not sections:
        raise RuntimeError("no narration text for the page")
    first = sections[0][2][0]
    desc = (f"خلاصهٔ رمان «{title_fa}» نوشتهٔ {author_fa}: " + first)[:300]
    if len(desc) >= 300:
        desc = desc[:desc.rfind(" ", 0, 297)] + "…"

    cover = False
    if cover_img and Path(cover_img).exists():
        try:
            sh(["ffmpeg", "-y", "-loglevel", "error", "-i", str(cover_img), "-vf", "scale='min(720,iw)':-2", "-q:v", "4", str(pdir / "cover.jpg")])
            cover = (pdir / "cover.jpg").exists() and (pdir / "cover.jpg").stat().st_size > 2000
        except Exception as e:  # noqa: BLE001
            print(f"Site cover skipped ({str(e)[:80]}).", flush=True)

    meta = load_json(SITE_META, [])
    meta = [m for m in meta if m.get("slug") != slug]
    rec = {"slug": slug, "title_fa": title_fa, "title_en": title_en, "author_fa": author_fa, "author_en": author_en,
           "date": date.today().isoformat(), "cover": cover}
    meta.append(rec)
    save_json(SITE_META, meta)

    url = f"{base}/{slug}/" if base else ""
    ld = {"@context": "https://schema.org", "@type": "Article", "headline": f"خلاصهٔ رمان {title_fa}", "inLanguage": "fa",
          "datePublished": rec["date"], "dateModified": rec["date"], "description": desc,
          "about": {"@type": "Book", "name": title_en, "alternateName": title_fa,
                    "author": {"@type": "Person", "name": author_en, "alternateName": author_fa}},
          "author": {"@type": "Organization", "name": SITE_NAME}, "publisher": {"@type": "Organization", "name": SITE_NAME}}
    if base:
        ld["mainEntityOfPage"] = url
        ld["publisher"]["url"] = base + "/"
        if cover:
            ld["image"] = f"{base}/{slug}/cover.jpg"
    page_title = f"خلاصهٔ رمان {title_fa} | {SITE_NAME}"
    img_abs = f"{base}/{slug}/cover.jpg" if (base and cover) else None
    hero_img = (f'<img src="cover.jpg" alt="{_attr("طراحی مدادی برای رمان " + title_fa)}" width="576" height="1024">' if cover else "")
    body = []
    for part, head, paras in sections:
        body.append(f"<h2>{_fa_html(head)}</h2>")
        body.extend(f"<p>{_fa_html(x)}</p>" for x in paras)
    others = [m for m in sorted(meta, key=lambda m: (m.get("date", ""), m["slug"]), reverse=True) if m["slug"] != slug][:6]
    more = (f'<section class="more"><h3>رمان‌های دیگر</h3><div class="grid">' + "".join(_site_card(m, "../") for m in others)
            + "</div></section>") if others else ""
    cta = (f'<div class="cta"><p>ویدیوی تصویری این خلاصه را در کانال تلگرام {_fa_html(SITE_NAME)} ببین.</p>'
           f'<a class="btn" href="{_attr(channel)}" rel="noopener" target="_blank">دیدن ویدیو در تلگرام</a></div>') if channel else ""
    wiki = _attr(src.get("url") or "")
    credit = (f'<p>این متن بازنویسی و خلاصه‌ای از بخش داستان مقالهٔ <a href="{wiki}" rel="noopener" target="_blank">ویکی‌پدیا دربارهٔ '
              f'<bdi>{_fa_html(title_en)}</bdi></a> است و مانند آن با مجوز '
              '<a href="https://creativecommons.org/licenses/by-sa/4.0/deed.fa" rel="license noopener" target="_blank">CC BY-SA 4.0</a> '
              'منتشر می‌شود.</p>') if wiki else ""
    html_ = (_site_head(page_title, desc, url, base, image=img_abs, extra_ld=ld)
             + _site_top("../", channel)
             + f'<main>\n<div class="hero">{hero_img}<div class="t"><h1>خلاصهٔ رمان «{_fa_html(title_fa)}»</h1>'
               f'<p class="sub">{_fa_html(author_fa)} · <bdi>{_fa_html(title_en)}</bdi> · <bdi>{_fa_html(author_en)}</bdi></p></div></div>\n'
               f'<article>\n' + "\n".join(body) + f'\n</article>\n{cta}\n{more}\n</main>\n'
             + _site_footer(credit))
    (pdir / "index.html").write_text(html_, encoding="utf-8")

    _site_write_index(meta, base, channel)
    # sitemap + robots + 404 + .nojekyll
    urls = ([(base + "/", max(m["date"] for m in meta))] if base else []) + [(f"{base}/{m['slug']}/", m["date"]) for m in meta] if base else []
    if base:
        sm = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        sm += [f"<url><loc>{_attr(u)}</loc><lastmod>{d}</lastmod></url>" for u, d in urls]
        sm.append("</urlset>")
        (SITE_DIR / "sitemap.xml").write_text("\n".join(sm) + "\n", encoding="utf-8")
        (SITE_DIR / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {base}/sitemap.xml\n", encoding="utf-8")
    else:
        (SITE_DIR / "robots.txt").write_text("User-agent: *\nAllow: /\n", encoding="utf-8")
    (SITE_DIR / "404.html").write_text(
        _site_head(f"صفحه پیدا نشد | {SITE_NAME}", "این صفحه وجود ندارد.", "", base, kind="website") + _site_top("./", channel)
        + '<main><h1>صفحه پیدا نشد</h1><p class="lead">آدرس را بررسی کن یا به صفحهٔ اصلی برگرد.</p>'
          '<p><a class="btn" href="./">صفحهٔ اصلی</a></p></main>\n' + _site_footer(), encoding="utf-8")
    (SITE_DIR / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Website page written: docs/{slug}/ ({len(meta)} page(s) in total).", flush=True)
    return slug


# ---------------------------------------------------------------- main
def get_book_and_source(posted, rejected):
    """Next novel that has a usable Plot section on Wikipedia. A novel without one is rejected for good
    (never summarized from memory, never tried again)."""
    tries = 60 if BOOKS_FILE.exists() else 6
    for _ in range(tries):
        book = fix_author_fa(fix_title_fa(pick_book(posted, rejected)))
        print("Book candidate:", book["title_en"], "|", book["book_type"], flush=True)
        src = fetch_source(book)
        if src:
            return book, src
        reject_book(rejected, book, "no usable Plot section on Wikipedia")
    raise RuntimeError("Could not find a novel with a usable Plot section on Wikipedia")


CF_STATE_FILE = SAVE_DIR / "azim_cf_state.json"      # committed with azim_saved_pics: no code needed in the workflow


def cf_quota_finished_today():
    """Cloudflare's free quota resets at 00:00 UTC. When a run found it finished, the next runs of the SAME UTC day stop
    at once: they cannot draw anything, and they would only burn Gemini quota (script, voice) for nothing."""
    if os.environ.get("AZIM_IGNORE_CF_STATE"):
        return False
    st = load_json(CF_STATE_FILE, {})
    return isinstance(st, dict) and st.get("exhausted_utc_date") == datetime.now(timezone.utc).date().isoformat()


def main():
    if cf_quota_finished_today():
        print("The free Cloudflare picture quota was already finished earlier today (it resets at 00:00 UTC, "
              "03:30 Iran time). Nothing is started now, so no Gemini quota is wasted. The next run continues.", flush=True)
        return
    posted = load_json(POSTED_FILE, [])
    rejected = load_json(REJECTED_FILE, [])
    _VARIETY["n"] = len(posted)
    if len(posted) >= TOTAL_BOOKS:
        print("All books are done.")
        return

    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    if not USE_CF:
        raise ImageServiceUnavailable("the secrets CF_ACCOUNT_ID and CF_API_TOKEN are missing or empty in GitHub")
    print(f"Image service: Cloudflare FLUX; "
          f"image check: {'on' if IMAGE_CHECK else 'off'}; voice text prep: {'on' if VOICE_PREP else 'off'}",
          flush=True)

    # a script that was finished in an earlier run which failed later (voice/images/video): resume it,
    # so the Gemini quota is not spent twice. After 3 failed resumes the book is rejected.
    book = src = script = None
    pending = load_json(PENDING_FILE, None)
    if pending and pending.get("version") != SCRIPT_VERSION:
        print("Saved script is from an older version of the bot: ignored.", flush=True)
        PENDING_FILE.unlink(missing_ok=True)
        pending = None
    if pending:
        done = {norm(p["title_en"]) for p in posted}
        try:
            if norm(pending["book"]["title_en"]) in done:
                pending = None
            else:
                attempts = int(pending.get("attempts", 0)) + 1
                if attempts > 3:
                    reject_book(rejected, pending["book"], "failed 3 times after the script was written")
                    pending = None
                else:
                    pending["attempts"] = attempts
                    save_json(PENDING_FILE, pending)
                    book, src, script = pending["book"], pending["src"], pending["script"]
                    print(f"Resuming the saved script of '{book['title_en']}' (resume {attempts} of 3).", flush=True)
        except Exception as e:  # noqa: BLE001
            print("Saved script ignored:", e, flush=True)
            pending = None
        if not pending:
            PENDING_FILE.unlink(missing_ok=True)

    if not pending:
        for _ in range(4):
            book, src = get_book_and_source(posted, rejected)
            print("Book:", book["title_en"], "| source:", src["url"].replace("\n", " + "), flush=True)
            plan = plan_length(src)
            print(f"Plan: {plan[0]}-{plan[1]} scenes, {plan[2]} words (from a {src['en_words']}-word Plot)",
                  flush=True)
            try:
                script = write_scenes(book, src, plan)
                break
            except GeminiBlocked as e:
                reject_book(rejected, book, f"Gemini blocked the prompt: {e}")
        else:
            raise RuntimeError("Gemini blocked 4 books in a row")
        save_json(PENDING_FILE, {"book": book, "src": src, "script": script, "attempts": 0, "version": SCRIPT_VERSION,
                                 "date": date.today().isoformat()})

    if book.get("author_fa"):                       # the spelling that Iranian readers know (never «لف تولستوی»)
        book["author_fa"] = fa_author(book.get("author_fa"), book.get("author_en"))
    elif fa_author("", book.get("author_en")):
        book["author_fa"] = fa_author("", book.get("author_en"))
    if script.get("visual_version") != VISUAL_VERSION:
        print("The pictures of this script are planned again (exact people, different looks, place from the sentence).",
              flush=True)
        script = align_visuals(script, book, src)
        if pending:
            pending["script"] = script
            save_json(PENDING_FILE, pending)
    (WORK / "azim_source.txt").write_text(src["text"], encoding="utf-8")
    if not CAST_SCENE:      # a saved script from an older run may still contain the portrait scene: remove it
        script["scenes"] = [sc for sc in script["scenes"] if not sc.get("cast_names")]
    scenes = script["scenes"]
    words = sum(len(s["narration_fa"].split()) for s in scenes)
    print(f"Script: {len(scenes)} scenes, {words} words, style: storybook (fixed)", flush=True)
    (WORK / "azim_script.json").write_text(json.dumps(script, ensure_ascii=False, indent=1), encoding="utf-8")
    (ROOT / "azim_last_script.json").write_text(json.dumps(script, ensure_ascii=False, indent=1), encoding="utf-8")
    (ROOT / "azim_last_source.txt").write_text(src["text"], encoding="utf-8")
    REPORT_FILE.write_text(f"Book: {book['title_en']}\nSource: {src['url']}\n", encoding="utf-8")

    # the voice alternates: female, male, female, male...
    last_voice = posted[-1].get("voice") if posted else None
    if last_voice in ("female", "male"):
        kind = "male" if last_voice == "female" else "female"
    else:
        kind = "female" if len(posted) % 2 == 0 else "male"
    print(f"Voice of this video: {kind}", flush=True)
    POSTED_REF[:] = posted
    try:
        durs = make_voice(scenes, kind, script.get("names") or {})
    except ScriptTooShort as e:
        # the rule is "every video is longer than 3 minutes": this book cannot make one, so it is never used
        print(f"STOP: {e}. The book is rejected and the next run takes another book.", flush=True)
        reject_book(rejected, book, f"too short for a 3-minute video: {e}")
        save_json(REJECTED_FILE, rejected)
        PENDING_FILE.unlink(missing_ok=True)
        sys.exit(1)

    seed = int(time.time()) % 100000
    saved_pics_dir(book)
    cast_refs = make_cast_refs(script.get("characters", []), script.get("era_en", ""), book, seed)
    CAST_REFS.clear()
    CAST_REFS.update(cast_refs)
    n_pics = sum(len(sc.get("beats") or [1]) for sc in scenes)
    done_pics = 0
    last_img = None
    jobs = []
    cover_n = 0
    cover_pos = None                          # no book picture any more (user's decision, Nov 5, 2026)
    author_pos = (0, 0) if TITLE_CARDS else None      # the ONE writer picture: the first picture of the video
    if TITLE_CARDS and not script.get("author"):
        script["author"] = design_author(book, src)
        if pending:
            pending["script"] = script
            save_json(PENDING_FILE, pending)
    has_event = any(str(b.get("kind") or "event") == "event" for s in scenes for b in (s.get("beat_visuals") or []))
    for i, s in enumerate(scenes):
        beats = s.get("beats") or [s["narration_fa"]]
        bvs = s.get("beat_visuals") or []
        for j, beat in enumerate(beats):
            sc = dict(s)
            sc["narration_fa"] = beat
            sc["era_en"] = script.get("era_en", "")
            if j < len(bvs):
                sc["visual_en"] = bvs[j]["visual_en"]
                sc["event_en"] = bvs[j].get("event_en", "")
                sc["roles"] = bvs[j].get("roles", {})
                sc["characters_present"] = bvs[j]["characters_present"]
                sc["kind"] = bvs[j].get("kind", "event")
                sc["must_show"] = bvs[j].get("must_show", [])
                sc["people_en"] = bvs[j].get("people_en", "")
                sc["emotion_en"] = bvs[j].get("emotion_en", "")
                sc["gentle_en"] = bvs[j].get("gentle_en", "")
                sc["creatures_en"] = bvs[j].get("creatures_en", [])
                if _VIOLENT_ANY.search(" ".join([str(sc.get("visual_en") or ""), str(sc.get("event_en") or "")])):
                    gentle_ = str(sc.get("gentle_en") or "").strip() or soften_violence(sc.get("visual_en"))
                    sc["visual_full_en"] = sc["visual_en"]
                    sc["visual_en"] = gentle_                       # the event told gently: the safety filter accepts it
                    sc["event_en"] = soften_violence(sc.get("event_en"))
                    sc["must_show"] = [soften_violence(x) for x in (sc.get("must_show") or [])]
                    sc["creatures_en"] = [soften_violence(x) for x in (sc.get("creatures_en") or [])]
                sc["others"] = bvs[j].get("others", [])
                if bvs[j].get("time_en"):          # the author's study or a library: not the time of the story
                    sc["era_en"] = bvs[j]["time_en"]
            elif not sc.get("visual_en"):          # no plan for this beat: the picture of its OWN part of the story
                sc["visual_en"] = ("Medium-wide shot. " + " ".join(str(s.get("action_en") or "").split()) + " Place: "
                                   + " ".join(str(s.get("setting_en") or "").split()))
                sc["event_en"] = " ".join(str(s.get("action_en") or "").split())
            inject_subjects(sc, beat, script.get("subjects"))
            inject_event(sc, beat)
            card_kind = None
            if TITLE_CARDS and (i, j) == author_pos:
                sc.update(author_library_plan(script.get("author")))
                sc["era_en"] = "the writer's own time"
            elif sc.get("kind") == "author" and script.get("author"):        # same writer, same gender in every picture
                a_ = script["author"]
                sc["visual_en"] = f"{sc.get('visual_en', '')} The writer (seen from behind) is {writer_look(a_)}."
                sc["people_en"] = f"exactly one person: the writer, a {a_.get('gender', 'person')}, seen from behind, nobody else"
            sc["look_sheets"] = [f"{c['name']}: {c['look_en']}" for c in script.get("characters", [])
                                 if isinstance(c, dict) and c.get("name") in (sc.get("characters_present") or [])]
            img = WORK / f"img_{i:02d}_{j}.jpg"
            reused = False
            def build(fix="", use_refs=True, limit=None, sc=sc):
                chars = script.get("characters", [])
                names = pick_present(chars, [n for n in (sc.get("characters_present") or [])])
                rn = [n for n in names if n in cast_refs][:MAX_REFS] if use_refs else []
                return (build_prompt(FIXED_STYLE, chars, sc, fix, ref_names=rn, limit=limit), [cast_refs[n] for n in rn])

            def safe_build(level, limit=None, sc=sc):
                """Gentler wordings for a picture the safety filter refused (positive words only)."""
                era = str(sc.get("era_en") or "").split(". Clothing of that time")[0]
                gentle = str(sc.get("gentle_en") or "").strip() or soften_violence(sc.get("visual_en"))
                if level == 1:       # the SAME event told gently (threat and result, no gore)
                    sc2 = dict(sc, visual_en=gentle, event_en=soften_violence(sc.get("event_en")),
                               must_show=[soften_violence(x) for x in (sc.get("must_show") or [])])
                    return build_prompt(FIXED_STYLE, script.get("characters", []), sc2, "", ref_names=[],
                                        limit=max((limit or PROMPT_WORDS) - 10, 60))
                if level == 2 and gentle:    # the same event, wide: the creature stays large, the people are small
                    return (f"Wide black-and-white graphite pencil drawing, the creature large and the people small, natural head size: {_trim_words(gentle, 45)}. "
                            f"Setting: {_trim_words(str(sc.get('era_en') or ''), 25)}. Style: "
                            f"{', '.join(FIXED_STYLE.split(', ')[:4])}. Vertical 9:16 composition, {CLEAN_SHORT}")
                art_s = ", ".join(FIXED_STYLE.split(", ")[:4])
                cr = [soften_violence(x) for x in (sc.get("creatures_en") or []) if str(x).strip()][:2]
                if cr:           # the creature and the place of the story, no people: a safe picture that still shows the subject
                    return (f"Detailed black-and-white graphite pencil drawing, no people: {'; '.join(_trim_words(x, 30) for x in cr)}. "
                            f"Setting: {_trim_words(era, 40)}. Style: {art_s}. Vertical 9:16 composition, wide framing, "
                            f"the creature large in the middle, atmospheric detailed environment, {CLEAN_SHORT}")
                return (f"Detailed black-and-white graphite pencil drawing of a calm, quiet place in a peaceful moment. "
                        f"Setting: {_trim_words(era, 40)}. Style: {art_s}. Vertical 9:16 composition, wide framing, "
                        f"soft warm light, {CLEAN_SHORT}")

            saved_img = SAVE_DIR / img.name
            try:
                if saved_img.exists():
                    shutil.copy(saved_img, img)          # drawn in an earlier run: not drawn (and paid for) again
                    print(f"Picture {i}.{j}: taken from the earlier run.", flush=True)
                else:
                    make_image(build, img, seed + i * 10 + j, n_pics - done_pics - 1, safe_build=safe_build)
                    keep_saved(img, img.name)
            except ImageServiceUnavailable:
                raise
            except Exception as e:  # noqa: BLE001
                if last_img is None:
                    raise
                # one failed picture must not lose the whole video: reuse the previous picture
                print(f"WARNING: image {i}.{j} failed ({e}); reusing the previous picture.", flush=True)
                shutil.copy(last_img, img)
                reused = True
            jobs.append({"img": img, "sc": sc, "seed": seed + i * 10 + j, "reused": reused,
                         "build": build, "card": card_kind})
            last_img = img
            done_pics += 1
    if IMAGE_CHECK:
        check_and_fix_images(jobs)
    make_title_cards(jobs, book)          # after the check: the cards use the best version of the story pictures
    stamp_painting(jobs)                  # "Samurai Reader" in red on the painting of the writer picture

    final = WORK / "azim_final.mp4"
    total = build_video(scenes, durs, final, script.get("names") or {})
    print(f"video length = {total:.1f}s, size = {final.stat().st_size / 1e6:.1f} MB", flush=True)

    caption = (f"{book.get('title_fa') or book['title_en']}\n{book.get('author_fa') or book['author_en']}\n\n"
               f"{src.get('credit') or 'منبع خلاصه: ویکی‌پدیا (CC BY-SA)'}\n{src['url']}")
    wait_for_publish_time()
    send_video(final, caption)

    posted.append({"title_en": book["title_en"], "title_fa": book.get("title_fa") or "",
                   "type": "novel", "voice": kind, "engine": VOICE_USED["engine"], "date_pt": pacific_today(), "wiki": src["title"], "date": date.today().isoformat()})
    save_json(POSTED_FILE, posted)
    try:                                   # the website page: any problem here is only printed, the video is already posted
        cover_ = next((j["img"] for j in jobs if not j.get("card") and Path(j["img"]).exists()),
                      next((j["img"] for j in jobs if Path(j["img"]).exists()), None))
        publish_site_page(book, src, scenes, cover_)
    except Exception as e:  # noqa: BLE001
        print(f"Website page skipped: {e}", flush=True)
    PENDING_FILE.unlink(missing_ok=True)
    shutil.rmtree(SAVE_DIR, ignore_errors=True)
    shutil.rmtree(WORK, ignore_errors=True)
    print("Done.")


# ---------------------------------------------------------------- the fixed samurai painting (original artwork + its red signature)
# A PNG with transparent shadow, written to the work folder by painting_file(). Never edit: it must stay identical in every video.
_PAINTING_B64 = (
    "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"
)


if __name__ == "__main__":
    try:
        main()
    except GeminiQuotaExhausted as exc:
        print(f"STOP: the daily Gemini quota is finished for every model ({exc}). "
              "No video now; the next run continues.", flush=True)
        sys.exit(1)
    except ImageServiceUnavailable as exc:
        print(f"STOP: no good picture can be made now ({exc}). No video is posted with bad pictures; "
              "the finished script is saved and the next run continues with it.", flush=True)
        if "daily free quota" in str(exc):
            SAVE_DIR.mkdir(parents=True, exist_ok=True)
            save_json(CF_STATE_FILE, {"exhausted_utc_date": datetime.now(timezone.utc).date().isoformat()})
        pend = load_json(PENDING_FILE, None)
        if pend and int(pend.get("attempts", 0)) > 0:      # this stop is not the book's fault
            pend["attempts"] = int(pend["attempts"]) - 1
            save_json(PENDING_FILE, pend)
        sys.exit(1)
