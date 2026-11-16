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


SLOW_COUNT = {}


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
                if "timed out" in str(e).lower():
                    SLOW_COUNT[model] = SLOW_COUNT.get(model, 0) + 1
                    if SLOW_COUNT[model] >= 2 and len([m for m in models if m not in DEAD_MODELS and m != model]) >= 1:
                        DEAD_MODELS.add(model)
                        print(f"  -> {model} timed out twice (2.5 minutes lost each time): not used again in this run.", flush=True)
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


_ACCESSORIES_M = ("a flat cap", "a bow tie", "a long scarf", "a brimmed hat", "a walking cane")
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
  freckles; never glasses, they are drawn deformed); the DAY OUTFIT with colors (see the OUTFIT rule below). It is pasted into EVERY picture of this
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


def clean_spine(d, lo, relax=0):
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
    if len(events) < max(5, lo - 1 - relax):
        print(f"Story spine rejected: the model gave {len(events)} events, {max(5, lo - 1 - relax)} are needed.", flush=True)
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


def build_spine(book, src, cast, relax=0):
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
    sp = clean_spine(d, lo, relax)
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



_SOFT1 = ((r"\b(?:abus(?:e|es|ed|ing|ive)|molest\w*|rap(?:e|es|ed|ist)|tortur\w*)\b", "harsh treatment"),
          (r"\b(?:beat(?:s|en|ing)?|flog(?:s|ged|ging)?|whipp?(?:s|ed|ing)?|thrash\w*)\b", "punish"),
          (r"\b(?:sexual\w*|naked|nude|erotic\w*)\b", "private"))
_SOFT2 = ((r"\b(?:kill(?:s|ed|ing)?|murder\w*|slaughter\w*|suicide|hang(?:s|ed|ing)?|stab\w*|shot|shoot\w*)\b", "meet a sad end"),
          (r"\b(?:blood\w*|gore|wound\w*|corpse\w*|dead|death|dies|died|dying)\b", "loss"))


def _soft_text(t, level):
    t = str(t or "")
    for pat, new in _SOFT1 + (_SOFT2 if level >= 2 else ()):
        t = re.sub(pat, new, t, flags=re.I)
    return t


def _fa_sentences(x):
    """A list of Persian sentences from whatever the model returned (strings or {"fa": ...} objects)."""
    out = []
    for it in (x if isinstance(x, list) else [x]):
        if isinstance(it, dict):
            it = it.get("fa") or it.get("text") or ""
        it = " ".join(str(it or "").replace("ي", "ی").replace("ك", "ک").split())
        if it:
            out.append(it)
    return out


def write_scenes_chunked(book, src, spine, cast, words, author_src, author_g, soft=1):
    """The reliable way to write the script (used when the one-call script failed or was blocked): the narration is written EVENT BY
    EVENT (3 events per call) from the spine only, so the ORDER and the coverage are fixed by the program, every event gets its own
    words, and the raw source text (which can trigger the safety filter) is never sent. Returns {"characters":..., "scenes": raw}
    in the same format as the one-call script."""
    nums = [int(x) for x in re.findall(r"\d+", str(words))] or [560, 860]
    lo_w, hi_w = nums[0], nums[-1]
    n_ev = len(spine["events"])
    per_ev = int(max(38, min(75, ((lo_w + hi_w) / 2 - 150) / max(n_ev, 1))))
    names = [c["name"] for c in (cast or {}).get("characters", [])] if cast else [h["name"] for h in spine["heroes"]]
    rules = f"""RULES FOR THE PERSIAN NARRATION:
- A gifted Iranian storyteller telling a story to ONE listener who never read the book. Correct, natural, flowing Persian: complete
  sentences (subject, object, verb at the end), one tense (simple past), one idea per sentence, short enough for one breath, each
  sentence joined to the one before by a connector (سپس، پس از آن، اما، چون، برای همین، در همان حال) or by its content.
- Only the facts given below; never add a fact, name, date or number. Introduce every person with a few words of role at his or her FIRST
  mention; a pronoun always points to the one person named in the sentence before it. Say where and when when the place changes.
- YEARS: say a year like «هزار و هفتصد و هشتاد» (1780), NEVER «هفده صد و هشتاد». NUMBERS: separate Persian words, a separate «و», never glued, never digits.
- No English loanwords. Everyday words. No emojis, no brackets. At most two ezafe links in a row.
{MEANING_RULE}
{TONE_RULES}
{NAMES_RULE}"""
    clean = lambda e: {k: (v if k == "id" else _soft_text(v, soft)) for k, v in e.items() if k in ("id", "who_en", "what_en", "where_en", "because_en", "role_en")}

    # ---- 1) the opening and the heroes
    heroes = spine["heroes"]
    p1 = f"""Write the opening of a Persian video that summarizes the novel "{book['title_en']}" by {book['author_en']}.
{rules}
FACTS: logline (the situation at the START only): {_soft_text(spine['logline'], soft)}
AUTHOR SENTENCE SOURCE: {author_src}
AUTHOR GENDER: {author_g}
HEROES: {json.dumps([{'name': h['name'], 'role_en': _soft_text(h.get('role_en'), soft)} for h in heroes], ensure_ascii=False)}
{('WORLD: ' + spine['world_en']) if spine.get('world_en') else ''}
Return JSON {{"open": [2 or 3 Persian sentences, 35 to 60 words in all: the name of the novel and of its author in English letters, when it was
published (only if the author sentence source says so), and ONE gripping sentence: the hero, what he or she wants and the problem
(the situation at the START only; NO outcome, NO death, NO later event)],
"heroes": [{'2 to 4 Persian sentences (35 to 65 words) that say who is who: each hero by name with a few words of role and relation to the others; no fate, no ending' if len(heroes) >= 2 else '[]'}]}}"""
    d1 = gemini_json(p1, minutes=8, models=WRITE_MODELS)
    open_s, heroes_s = _fa_sentences(d1.get("open")), _fa_sentences(d1.get("heroes"))
    if not open_s:
        raise RuntimeError("no opening from the event-by-event writer")

    def scene(part, sents, ev=None, need=2, extra=None):
        extra = extra or {}
        return {"part": part, "source_en": "", "characters_present": extra.get("characters_present") or [],
                "setting_en": extra.get("setting_en") or "", "action_en": extra.get("action_en") or "", "mood_en": extra.get("mood_en") or "",
                "sentences": [{"fa": t, "ev": ("intro" if part in ("open", "heroes") else "wrap" if part == "wrap" else ev), "need": need}
                              for t in sents]}

    raw = [scene("open", open_s)]
    if len(heroes) >= 2 and heroes_s:
        raw.append(scene("heroes", heroes_s))

    # ---- 2) the story, 3 events per call, in the order of the spine
    evs = spine["events"]
    prev_fa, prev_en = "", ""
    for a in range(0, n_ev, 3):
        part_ev = evs[a:a + 3]
        last_chunk = a + 3 >= n_ev
        got = {}
        for attempt in range(2):
            p2 = f"""Write the Persian narration of {len(part_ev)} consecutive events of the novel "{book['title_en']}".
{rules}
CHARACTER NAMES (exactly as written): {json.dumps(names, ensure_ascii=False)}
THE EVENT BEFORE THESE (already told; do not tell it again): {_soft_text(prev_en, soft) or '(none: this is the first event after the introduction)'}
LAST PERSIAN SENTENCE ALREADY SAID: {prev_fa or '(none)'}
EVENTS TO TELL NOW, in this order: {json.dumps([clean(e) for e in part_ev], ensure_ascii=False)}
For EACH event write ONE scene of 2 to 4 flowing Persian sentences ({per_ev - 12} to {per_ev + 12} words in all) that tells WHAT happens and WHY (its cause).
The first sentence of the first scene continues from the last sentence already said through a connector.
{'This is the LAST chunk: the last event is the ending and may tell how it ends.' if last_chunk else 'NEVER tell, hint at or foreshadow the outcome or anything that happens after these events (no teaser, no «later», no ending).'}
Return JSON {{"scenes": [{{"ev": <event id>, "sentences": [{{"fa": "ONE Persian sentence", "need": 3 or 2}}], "characters_present": [names from the list that are visible],
"setting_en": "where (place, time, light)", "action_en": "exactly what happens in the picture: who does what", "mood_en": "..."}}]}} with exactly one scene per event, in order."""
            try:
                d2 = gemini_json(p2, minutes=8, models=WRITE_MODELS)
            except GeminiBlocked:
                if soft < 2:
                    raise
                raise
            got = {}
            for sc in d2.get("scenes") or []:
                if isinstance(sc, dict) and str(sc.get("ev", "")).strip().isdigit():
                    got[int(str(sc["ev"]).strip())] = sc
            if all(e["id"] in got and _fa_sentences(got[e["id"]].get("sentences")) for e in part_ev):
                break
            print(f"Event-by-event writer: events {[e['id'] for e in part_ev]} incomplete, asking again.", flush=True)
            got = {}
        if not got:
            raise RuntimeError("the event-by-event writer could not write events " + str([e["id"] for e in part_ev]))
        for e in part_ev:
            sc = got[e["id"]]
            sents = []
            for x in (sc.get("sentences") or []):
                t = _fa_sentences(x)
                if t:
                    try:
                        nd = max(2, min(3, int(str(x.get("need", 2))[:1]))) if isinstance(x, dict) else 2
                    except (TypeError, ValueError):
                        nd = 2
                    sents.append((t[0], nd))
            r_ = scene("story", [t for t, _ in sents], ev=e["id"], extra=sc)
            for k_, (_, nd) in enumerate(sents):
                r_["sentences"][k_]["need"] = nd
            raw.append(r_)
            prev_fa = sents[-1][0] if sents else prev_fa
            prev_en = e.get("what_en", "")

    # ---- 3) the closing wrap-up (only the meaning of the book)
    p3 = f"""Write the closing wrap-up of a Persian video about the novel "{book['title_en']}".
{rules}
CENTRAL IDEA: {spine.get('idea_en') or '(none: say what the story warns or teaches)'}
Return JSON {{"wrap": [3 or 4 SHORT Persian sentences, 45 words at most]}}. The first sentence starts with the words «در جمع‌بندی». It is NOT a second
summary: do NOT retell the story or its ending, name no person, creature, place or object of the plot, use only GENERAL nouns (کتاب, انسان, قدرت,
آزادی, ترس, امید, جامعه). Sentence 1: the one central idea of the book; sentence 2: the message of the author to the reader; sentence 3: why it
matters; the last sentence: short and memorable."""
    d3 = gemini_json(p3, minutes=8, models=WRITE_MODELS)
    wr = _fa_sentences(d3.get("wrap"))
    if not wr:
        raise RuntimeError("no wrap-up from the event-by-event writer")
    if "جمعبندی" not in _norm_wrap(wr[0]):
        wr[0] = "در جمع‌بندی، " + wr[0]
    if len(wr) >= 3:
        raw += [scene("wrap", wr[:2]), scene("wrap", wr[2:])]
    else:
        raw.append(scene("wrap", wr))
    return {"characters": [], "scenes": raw}



def remove_spoilers(data, spine):
    """ONE cheap Gemini call: sentences of the opening, the heroes scene and the first half of the story that tell or hint at how the
    story ENDS are deleted (the ending belongs to the last scenes only). The opening is never left empty."""
    try:
        evs = spine["events"]
        n_ev = len(evs)
        tail = [e for e in evs if e["id"] >= max(n_ev - 2, 2)]
        items = []
        for si, sc in enumerate(data["scenes"]):
            early = sc["part"] in ("open", "heroes") or (sc["part"] == "story" and max(sc.get("ev_ids") or [0]) <= max(1, int(n_ev * 0.6)))
            if early:
                for ti, t in enumerate(split_sentences(sc["narration_fa"])):
                    items.append({"scene": si, "i": ti, "fa": t})
        if not items:
            return data
        prompt = f"""The ENDING of a story: {spine.get('ending_en', '')}
The last events: {json.dumps([e.get('what_en', '') for e in tail], ensure_ascii=False)}
Below are Persian sentences from the START of a video about this story. Which of them tell, hint at or foreshadow the ENDING or the last events
(a death, a marriage, an escape, who wins, how it all turns out, «in the end», «finally», «later he will»)? A sentence that only tells the
situation at the start or an early event is fine.
SENTENCES: {json.dumps(items, ensure_ascii=False)}
Return JSON {{"spoilers": [{{"scene": 0, "i": 0}}]}} (an empty list when there is none)."""
        out = gemini_json(prompt, minutes=4, models=CHEAP_MODELS)
        bad = {(int(x["scene"]), int(x["i"])) for x in (out.get("spoilers") or []) if isinstance(x, dict)
               and str(x.get("scene", "")).isdigit() and str(x.get("i", "")).isdigit()}
        if not bad:
            return data
        new = []
        for si, sc in enumerate(data["scenes"]):
            sents = split_sentences(sc["narration_fa"])
            keep = [t for ti, t in enumerate(sents) if (si, ti) not in bad]
            if len(keep) != len(sents):
                print(f"Spoiler check: {len(sents) - len(keep)} sentence(s) of scene {si + 1} ({sc['part']}) told the ending too early: removed.", flush=True)
            if not keep:
                if sc["part"] == "story":
                    keep = sents[:1]
                else:
                    continue
            sc["narration_fa"] = " ".join(keep)
            new.append(sc)
        data["scenes"] = new
        return data
    except (GeminiBlocked, GeminiQuotaExhausted):
        raise
    except Exception as e:  # noqa: BLE001
        print(f"Spoiler check skipped: {e}", flush=True)
        return data


def write_scenes(book, src, plan):
    """Step B: first the SPINE (what must be told), then the narration written sentence by sentence from the spine."""
    min_sc, max_sc, words = plan
    if "author_gender" not in book:
        book["author_gender"] = author_gender(book)
    cast = design_cast(book, src)
    cast_block = cast_prompt(cast)
    spine = None
    for att_ in range(3):
        spine = build_spine(book, src, cast, relax=att_ * 2)
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
    use_chunks = False
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
- YEARS: say a year like «هزار و هفتصد و هشتاد» (for 1780), NEVER «هفده صد و هشتاد»: the style of «seventeen hundred» is wrong in Persian.
- NUMBERS: write every number as separate Persian words with spaces and a separate «و» («هفتاد و هفت», «بیست و پنج», «صد و بیست»),
  never as one glued word («هفتادوهفت»), and never with digits.
- Never use English loanwords or words that sound English (for example «پادکست»، «ویدئو»، «آنلاین»، «لایک»،
  «اپلیکیشن»، «استایل»): use the Persian word. Never say the video is a podcast.
- Use everyday words that any listener understands at once. Avoid rare, archaic, heavy literary or Arabic words.
- Do not quote the book at length; paraphrase.

Rules for the visuals:
- Each picture must show EXACTLY what the narration says at that moment (concrete scene, people, objects, action).
- No text, letters, numbers, charts or logos in the pictures. No real celebrities. No scenes copied from films."""
        data = None
        if attempt == 0 and not use_chunks:
            try:
                data = gemini_json(prompt, minutes=15, models=WRITE_MODELS)
            except GeminiBlocked as e:
                print(f"The one-call script was blocked ({e}): it is written event by event from the spine.", flush=True)
                use_chunks = True
        if data is None:
            try:
                try:
                    data = write_scenes_chunked(book, src, spine, cast, words, author_src, author_g, soft=1 + min(attempt, 1))
                except GeminiBlocked as e:
                    print(f"The event-by-event writer was blocked ({e}): the events are told in softer words.", flush=True)
                    data = write_scenes_chunked(book, src, spine, cast, words, author_src, author_g, soft=2)
            except GeminiQuotaExhausted:
                raise
            except GeminiBlocked:
                raise
            except Exception as e:  # noqa: BLE001
                print(f"Event-by-event script attempt {attempt + 1} failed: {e}", flush=True)
                time.sleep(5)
                continue
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
            try:
                data = remove_spoilers(data, spine)       # the opening never tells the ending
            except (GeminiBlocked, GeminiQuotaExhausted):
                pass
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
- "spoilers": ids of sentences of the OPENING and HEROES parts (the first scenes) that tell or hint at something that happens LATER in the story (the ending, a death, a result, who wins or loses, how it ends); each {{"id": "0.1"}}. Empty list if none. The opening may only describe the situation at the START.
- "missing_events": ids (numbers) of those key events of this list that you did NOT clearly hear in the narration:
  {json.dumps(ev_list, ensure_ascii=False)}. Empty list if you heard all of them."""
    try:
        t = gemini_json(prompt, minutes=6, models=WRITE_MODELS)
        rt_ = t.get("retelling") or []
        retell = [str(x) for x in ([rt_] if isinstance(rt_, str) else rt_)][:14]
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
        for g in (t.get("spoilers") or []):
            gid = str(g.get("id") if isinstance(g, dict) else g)
            if gid in valid and gid not in protect and gid not in [c["id"] for c in confusing]:
                confusing.append({"id": gid, "problem": "it reveals something that happens later (the ending or a result): replace it with a sentence about the START situation only, with no outcome"})
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
    (_pw(r"کشته|جان\s*باخت|درگذشت|فوت\s*کرد|به\s*قتل\s*رسید|خودکشی"), ("grief", "mourn", "candle", "bowed", "kill", "murder", "stab", "sinking", "falling", "victim"),
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
- "others": an EMPTY LIST by default; a person is listed ONLY when the Persian part itself names or clearly implies him or her (a rescuer, a doctor...); NEVER invent a bystander, servant, child, relative or crowd. When there is one: a list with ONE short English description for EVERY person of the picture who is NOT in "characters_present"
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
- SAME AGE EVERYWHERE (very important): every named character is drawn at exactly the age of his or her look sheet in EVERY picture, never as a child in one picture and as a grown person in another. When the narration speaks about the childhood or youth of a character, draw the place, the objects and the other people of that scene, or the adult character remembering (adult age, thoughtful face), NEVER a child version of the named character.
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
- KILLING IS PICTURED, VERY GENTLY (very important): when the narration tells a killing, a murder or a death by violence, the
  picture shows THAT moment in the softest storybook way: the killer and the victim in the same picture, the weapon or the threat
  visible, the victim falling or sinking down with a shocked face, the killer standing over in shadow, a dark and sad mood; NO
  blood, NO open wound, NO gore, no explicit body. A death by illness, age, accident or suicide is told by what the LIVING do and
  see: grief on their faces, a lit candle, a flower, an empty chair or bed, a dropped hat or bag; a hanging is shown ONLY by an
  empty rope above a toppled chair. Never draw a body under a sheet or a person hanging. Everybody else is alive, awake and upright
  (standing or sitting), except a person who sleeps or is ill in a BED when the sentence says so.
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
    if "پیپ" in text:
        style = (style + ". " if style else "") + ("Pronounce the word «پیپ» (a smoking pipe) like the English word 'pip': a short "
                                                   "p-ee-p that ends with a plain p sound, never with an extra vowel after the last p")
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
_FORCE_MARKS = (("وقار", "وَقار"), ("باوقار", "باوَقار"), ("\u06cc\u06cc\u0644\u0627\u0642", "\u06cc\u0650\u06cc\u0644\u0627\u0642"), ("کندوکاو", "کَندوکاو"), ("نورزید", "نَوَرزید"),
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


_PIP_RE = re.compile(rf"(?<![{_PL}]){_dtol('پیپ')}(?=(?:\u200c?ها|\u200c?های)?(?![{_PL}]))")


def force_marks(t):
    t = _PIP_RE.sub("پیپ\u0652", t)          # «پیپ»: the last «پ» has a sukun (the voice read it with a kasra)
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
           "and two legs; natural human proportions: every head is small, only about one eighth of its body height (long legs, narrow shoulders), NEVER a big head, never a child-like or chibi head on an adult body; "
           "limbs of different people never overlap")
NO_EXTRA = "No eyeglasses on anybody, no other person anywhere, no crowd, no faces in windows; every person appears exactly once with exactly one head and one body, never a second copy, twin, reflection or extra head"
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


def build_prompt(art, chars, scene, fix="", ref_names=None, limit=None, style_ref=False):
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
    if style_ref:
        art_s += (f". Draw with exactly the same pencil technique, line weight, shading and paper tone as reference image "
                  f"{len(ref_names or []) + 1} (copy ONLY its drawing style, never its people, objects or layout)")

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
            try:
                age_n = int(re.findall(r"\d+", str(c.get("age") or ""))[0])
            except (IndexError, ValueError):
                age_n = 0
            if age_n and not c.get("nonhuman") and "years old" not in desc:
                desc += f", exactly {age_n} years old"
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
STYLE_REF = {"path": None}   # the first good picture with people: every later picture copies its drawing style
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
    (r"\b(?:kill(?:s|ed|ing)?|murder(?:s|ed|ing)?|slaughter(?:s|ed|ing)?)\b",
     "stands over, in shadow, threatening, while the shocked victim sinks gently down (soft storybook moment, no blood)"),
    (r"\b(?:devour(?:s|ed|ing)?|eat(?:s|ing)?|ate|attack(?:s|ed|ing)?|assault(?:s|ed|ing)?)\b",
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


REDRAW_ROUNDS = int(os.environ.get("AZIM_REDRAW_ROUNDS") or 3)   # a bad picture is drawn again this many times (each costs free neurons)
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
    style_img = None
    try:
        sp_ = STYLE_REF.get("path")
        if sp_ and Path(sp_).exists():
            style_img = Path(sp_).read_bytes()
    except Exception:  # noqa: BLE001
        style_img = None
    ref_note = ""
    if ref_names:
        ref_note = ("\nAfter the pictures come REFERENCE portraits of characters, labeled IMAGE "
                    f"{len(jobs) + 1} and up: {json.dumps({n: len(jobs) + 1 + k for k, n in enumerate(ref_names)}, ensure_ascii=False)} "
                    "(character name: its IMAGE number). Every picture must show that character with THE SAME face (face shape, "
                    "eyes, nose, hair, age) and the same clothes as in his or her reference portrait.\n")
    if style_img:
        ref_note += (f"\nThe LAST image (IMAGE {len(jobs) + len(ref_names) + 1}) is the STYLE REFERENCE of the whole video. For \"style_ok\": "
                     "false when a picture differs from it in drawing technique, line weight, shading, paper tone or head-to-body proportions.\n")
    prompt = f"""You check illustrations for a video. The pictures to check are labeled IMAGE 1 to IMAGE {len(jobs)}.
For each one you get the Persian narration that is read while it is shown, the intended picture, and (maybe) the look
sheets of its characters. Items: {json.dumps(blocks, ensure_ascii=False)}
{ref_note}
For EACH picture return:
- "score" 0-10: how clearly it shows the main subject and main action of ITS narration (10 = a viewer understands what is
  said just by looking; 0 = unrelated). Strict limits:
  * the EVENT of the narration (a death, a fight, a meeting, a departure, a discovery...) must be recognisable; a calm
    or unrelated picture of a place or a person gets at most 3;
  * a killing or murder: it is shown GENTLY (killer, weapon or threat and victim in a soft storybook moment); an explicit,
    bloody or gory scene, a wound or open flesh, a body under a sheet, or a person hanging: at most 2; a killing that cannot be
    recognised at all (only a calm place or only mourners): at most 4. A death by illness, age or accident: shown by the grief of
    the living and objects (candle, flower, empty chair or bed); ANY dead lying body: at most 3. A ghost or spirit must be a
    translucent glowing figure with the person's shape and clothes; only a halo, light or mist: at most 3;
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
  * kind "author": the writer must be ACTIVELY WRITING (a pen touching paper, or hands on the keys of a typewriter), not reading or idle: at most 4;
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
- "people_count": the NUMBER of people you can see in the picture. Count EVERY person (the same character drawn twice counts as two people): also small ones, far away, in a
  window or doorway, in a painting or a statue, half hidden or cut by the border. Count first, then compare with
  "expected_people" (null = a crowd, do not compare).
- A character that is NOT human (a shape, an animal, a robot, a creature: its look sheet says so) counts as a person in
  "people_count"; for it "limbs_ok" is true when its body matches its look sheet with no extra and no missing parts, and
  "hands_per_person" is [].
- "limbs_ok": false if ANY person has a wrong body (when you are NOT SURE about the count of a hand, an arm or a leg, say false). Look at EVERY person from the head down to the feet, one by one and COUNT
  the hands of each person (exactly two, never three: look at hands that hold something, at hands near the face and at hands
  between two people, they are often doubled): more or fewer than two legs, two arms, two hands or two feet (a third leg or a doubled leg between two standing people is
  common: check it), a doubled, missing or misplaced head, merged bodies. Otherwise true.
- "head_ok": false when the head of ANY person is even a little too big for his or her body (a big-head, chibi, bobble-head or toy look; a
  natural head is about one seventh to one eighth of the body height; when in doubt say false), or when the people at the same distance have very different head sizes.
  Otherwise true.
- "style_ok": false when the picture is NOT the same detailed black-and-white graphite pencil drawing in manga style as the other
  pictures of this call (it looks colored, 3D, painted, photographic, flat vector or cartoon-like, or its line work and shading differ
  clearly from the other pictures). Otherwise true.
- "hands_per_person": a list with ONE number for each person you can see: how many HANDS that person has in the picture
  (count them one by one: two is right; three or more is a mistake that must be reported here).
- "twins": true if two people in the picture look alike (the same face, the same hair or the same clothes).
- "glasses": true if ANY person wears eyeglasses (they are always drawn deformed here: no eyeglasses are wanted at all).
- "age_ok": false when a named character looks clearly younger or older than the age of his or her look sheet (a child instead of an
  adult or an adult instead of a child) or than his or her reference portrait. Otherwise true.
- "duplicate": true when the SAME person (same face, hair and clothes) appears twice in one picture (a copy, a twin, a reflection
  that looks like a second person, an extra head or a second body of the same person).
- "missing": a short English phrase: the key thing that is missing or wrong (empty if the score is 8 or more).
- "fix_en": only if the score is below 8: what a new picture must clearly show, in POSITIVE words, at most 22 words
  (for example "a woman lying motionless on the ground, two men standing over her in shock, all fully visible, wide shot").
Return JSON {{"checks": [{{"n": 1, "people_count": 2, "limbs_ok": true, "head_ok": true, "style_ok": true, "hands_per_person": [2, 2], "twins": false, "glasses": false, "age_ok": true, "duplicate": false, "score": 7, "look_score": 7, "missing": "...", "fix_en": "..."}}]}} with one item per picture."""
    try:
        out = gemini_json(prompt, minutes=4, models=models or CHEAP_MODELS,
                          images=[j["img"].read_bytes() for j in jobs] + [CAST_REFS[n].read_bytes() for n in ref_names] + ([style_img] if style_img else []))
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
            if c.get("glasses") is True:
                score = min(score, 4)
                fix = ("nobody wears eyeglasses, clean natural eyes; " + fix)[:200]
            if c.get("age_ok") is False:
                score = min(score, 3)
                fix = ("every named person at exactly the age of his or her reference image, never a child instead of an adult or the other way round; " + fix)[:200]
            if c.get("duplicate") is True:
                score = min(score, 2)
                fix = ("every person appears exactly ONCE with one head and one body, never a copy or a second version of the same person; " + fix)[:200]
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
              f"view), ACTIVELY WRITING: the right hand holds a pen whose tip touches a sheet of manuscript paper and the writer's arm and shoulder show the writing movement (an inkwell and a pile of written pages beside it; or the hands on the keys of an old typewriter with a sheet of paper rolled into it); never just reading, never only sitting; the other hand rests flat on the table; the head and the body face the SAME direction, the neck is natural and "
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


PAINT_ASPECT = 733 / 418           # height / width of the painting file
PAINT_W_FRAC = float(os.environ.get("AZIM_PAINT_W") or 0.36)       # the painting is this part of the picture width wide


def painting_file():
    """The ONE samurai painting of the channel: the user's own picture (a black-and-white samurai kneeling and reading a scroll, only
    the small seal 侍 «samurai» is red), used WITHOUT any change in EVERY video. It is written from the data at the end of this
    script every run, so an old file with the same name in the repository can never replace it."""
    p = WORK / "azim_samurai_painting.jpg"
    try:
        WORK.mkdir(parents=True, exist_ok=True)
        data = base64.b64decode(_PAINTING_B64)
        if not p.exists() or p.read_bytes() != data:
            p.write_bytes(data)
        return p
    except Exception as e:  # noqa: BLE001
        print(f"Samurai painting file could not be written ({e}).", flush=True)
        return None


def _wall_gray(img, x, y, rw, rh):
    """The grey level (0-255) of the plain wall: the mean of a band of the picture, or None."""
    try:
        raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(img), "-vf",
                                       f"crop={int(rw)}:{int(rh)}:{int(x)}:{int(y)},scale=1:1,format=gray",
                                       "-f", "rawvideo", "-"], timeout=60)
        return raw[0] if raw else None
    except Exception as e:  # noqa: BLE001
        print(f"Wall colour could not be read ({e}).", flush=True)
        return None


def _fg_mask(img, x, y, pw, ph, out_path):
    """Writes a raw 8-bit mask (255 = something of the drawing stands IN FRONT of the wall here) for the rectangle of the painting.
    Foreground = pixels that differ from the plain wall colour (thin hatching and specks are ignored); from the TOP-most foreground
    pixel of a column down to the bottom of the rectangle everything is foreground, so the inside of a head, a body or a coat
    (whose colour can equal the wall) hides the painting too. Returns True when the mask was written."""
    try:
        import numpy as np
        from scipy import ndimage as ndi
        raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(img), "-vf", f"crop={pw}:{ph}:{x}:{y},format=gray",
                                       "-f", "rawvideo", "-"], timeout=60)
        if len(raw) != pw * ph:
            return False
        a = np.frombuffer(raw, dtype=np.uint8).reshape(ph, pw).astype(np.float32)
        a = ndi.gaussian_filter(a, 1.5)
        band = a[: max(int(ph * 0.12), 6)]
        wall, sd = float(np.median(band)), float(band.std())
        fg = np.abs(a - wall) > max(28.0, 3.0 * sd)
        fg = ndi.binary_opening(fg, structure=np.ones((3, 3), bool), iterations=2)
        lab, n = ndi.label(fg)
        if n:
            sizes = ndi.sum(fg, lab, range(1, n + 1))
            keep = np.zeros(n + 1, bool)
            keep[1:] = sizes >= 150
            fg = keep[lab]
        fg = ndi.binary_dilation(fg, structure=np.ones((3, 3), bool), iterations=3)
        fg = np.maximum.accumulate(fg, axis=0)               # everything below the top of a thing in front is hidden
        m = ndi.gaussian_filter(fg.astype(np.float32) * 255.0, 1.2)
        Path(out_path).write_bytes(np.clip(m, 0, 255).astype(np.uint8).tobytes())
        return True
    except Exception as e:  # noqa: BLE001
        print(f"Wall mask could not be made ({e}).", flush=True)
        return False


def stamp_painting(jobs):
    """The library picture of the writer has an EMPTY plain wall on purpose. The fixed painting (the user's own picture, black and
    white, only its seal red) is hung there ON THE WALL: the calmest free place is chosen (a smaller painting is tried when the wall is
    busy), and everything of the drawing that is NOT plain wall inside the painting's rectangle (a head, a lamp, a shelf) is drawn
    again IN FRONT of the painting, so a person or thing in front of the painting hides that part of it."""
    pf = painting_file()
    if not pf:
        return
    pf = str(Path(pf).resolve())
    for k, j in enumerate(jobs):
        sc = j.get("sc") or {}
        if not (sc.get("painting") and sc.get("kind") == "author"):
            continue
        img = j["img"]
        try:
            dims = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                            "stream=width,height", "-of", "csv=p=0:s=x", str(img)], text=True).strip()
            w, h = (int(x) for x in dims.split("x"))
            best = None
            for frac in (PAINT_W_FRAC, 0.30, 0.25, 0.21):
                pw = int(w * frac)
                ph = int(pw * PAINT_ASPECT)
                for cxf in (0.5, 0.40, 0.60, 0.33, 0.67):
                    for tyf in (0.07, 0.11, 0.15, 0.04):
                        x, y = int(w * cxf - pw / 2), int(h * tyf)
                        if x < 4 or x + pw > w - 4 or y + ph > h * 0.62:
                            continue
                        sd = region_spread(img, x, y, pw, ph, w, h)
                        score = (sd if sd is not None else 255.0) + 6 * abs(cxf - 0.5) + 10 * tyf
                        if best is None or score < best[0]:
                            best = (score, x, y, pw, ph, sd)
                if best and best[5] is not None and best[5] <= CALM_MAX:
                    break                                   # a calm wall place was found at this size
            if best is None:
                pw = int(w * 0.25)
                ph = int(pw * PAINT_ASPECT)
                best = (0, max((w - pw) // 2, 0), int(h * 0.06), pw, ph, None)
            _, x, y, pw, ph, sd = best
            tmp = f"painting_tmp_{k}.jpg"
            made = False
            maskraw = WORK / f"painting_mask_{k}.raw"
            if _fg_mask(img, x, y, pw, ph, maskraw):
                fc = (f"[1:v]scale={pw}:{ph}:flags=lanczos[p];[0:v]split=2[v0][v1];"
                      f"[v1]crop={pw}:{ph}:{x}:{y},format=rgb24[c];[c][2:v]alphamerge[fg];"
                      f"[v0][p]overlay={x}:{y}[b];[b][fg]overlay={x}:{y}")
                try:
                    sh(["ffmpeg", "-y", "-loglevel", "error", "-i", img.name, "-i", pf, "-f", "rawvideo", "-pixel_format", "gray",
                        "-video_size", f"{pw}x{ph}", "-i", maskraw.name, "-filter_complex", fc,
                        "-frames:v", "1", "-q:v", "2", tmp], cwd=WORK)
                    made = True
                except Exception as e:  # noqa: BLE001
                    print(f"Painting with the wall mask failed ({e}): plain placing is used.", flush=True)
            if not made:
                sh(["ffmpeg", "-y", "-loglevel", "error", "-i", img.name, "-i", pf, "-filter_complex",
                    f"[1:v]scale={pw}:{ph}:flags=lanczos[p];[0:v][p]overlay={x}:{y}", "-frames:v", "1", "-q:v", "2", tmp],
                   cwd=WORK)
            shutil.move(str(WORK / tmp), str(img))
            print(f"Samurai painting placed at {x},{y} ({pw}x{ph}) on the {w}x{h} library picture "
                  f"(wall spread {sd if sd is None else round(sd, 1)}, people/things in front hide it: {made}).", flush=True)
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


_YEAR_WORDS = {"شانزده": "هزار و ششصد", "پانزده": "هزار و پانصد", "هفده": "هزار و هفتصد", "هجده": "هزار و هشتصد",
               "نوزده": "هزار و نهصد"}
_YEAR_RE = re.compile(r"(?<![\u0600-\u06FF])(?:هزار و )?(شانزده|پانزده|هفده|هجده|نوزده)[\u200c ]?صد(?![\u0600-\u06FF])")


def _fix_year_words(text):
    """«هفده صد و هشتاد» is not Persian for 1780: it is «هزار و هفتصد و هشتاد» (also for the voice and the subtitles)."""
    return _YEAR_RE.sub(lambda m: _YEAR_WORDS[m.group(1)], str(text or ""))


def fix_numbers(text):
    text = _fix_year_words(text)
    return _fix_numbers_core(text)


def _fix_numbers_core(text):
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
                sref = STYLE_REF["path"] if (use_refs and STYLE_REF["path"] and Path(STYLE_REF["path"]).exists() and sc.get("kind") != "author") else None
                return (build_prompt(FIXED_STYLE, chars, sc, fix, ref_names=rn, limit=limit, style_ref=bool(sref)),
                        [cast_refs[n] for n in rn] + ([str(sref)] if sref else []))

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
            if STYLE_REF["path"] is None and not reused and sc.get("characters_present"):
                try:
                    sh(["ffmpeg", "-y", "-loglevel", "error", "-i", img.name, "-vf", f"scale=-2:{REF_SIDE}", "azim_style_ref.jpg"], cwd=WORK)
                    STYLE_REF["path"] = WORK / "azim_style_ref.jpg"
                except Exception as e:  # noqa: BLE001
                    print(f"Style reference not made ({e}).", flush=True)
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
    "/9j/4AAQSkZJRgABAQAAAQABAAD/6xaJSlACEQAAAAEAABZ/anVtYgAAAB5qdW1kYzJwYQARABCAAACqADibcQNjMnBhAAAAFllqdW1iAAAAR2"
    "p1bWRjMm1hABEAEIAAAKoAOJtxA3VybjpjMnBhOjA3OGJjNjQzLTgzNDAtNDc4Yi04YWM5LTlmNTQ3M2JiY2Q1NwAAAAOUanVtYgAAAClqdW1k"
    "YzJhcwARABCAAACqADibcQNjMnBhLmFzc2VydGlvbnMAAAAAuWp1bWIAAABEanVtZGNib3IAEQAQgAAAqgA4m3ETYzJwYS5pbmdyZWRpZW50Ln"
    "YzAAAAABhjMnNo+1tJ7hEIH7yPLyAan0nmEgAAAG1jYm9yo2lkYzpmb3JtYXRqaW1hZ2UvanBlZ2ppbnN0YW5jZUlEeCx4bXA6aWlkOmMxOTBk"
    "OGY0LTQwZjMtNDlmYS05YjkyLTc5MTI5ZTgzMDIzNWxyZWxhdGlvbnNoaXBocGFyZW50T2YAAAHianVtYgAAAEFqdW1kY2JvcgARABCAAACqAD"
    "ibcRNjMnBhLmFjdGlvbnMudjIAAAAAGGMyc2gSqzZyOM4S+cStQDaizxwEAAABmWNib3KiZ2FjdGlvbnOComZhY3Rpb25rYzJwYS5vcGVuZWRq"
    "cGFyYW1ldGVyc6FraW5ncmVkaWVudHOBomN1cmx4LXNlbGYjanVtYmY9YzJwYS5hc3NlcnRpb25zL2MycGEuaW5ncmVkaWVudC52M2RoYXNoWC"
    "C1nsn0bnilwoc9Y9YLF3ss6n+SITeNjH93+FnHmITE6KRmYWN0aW9ueB1jb20uYW50aHJvcGljLmNsYXVkZS5wcm92aWRlZGpwYXJhbWV0ZXJz"
    "oXgfY29tLmFudGhyb3BpYy5vcmlnaW4tY29uZmlkZW5jZWd1bmtub3dua2Rlc2NyaXB0aW9ueGZDbGF1ZGUgcHJvdmlkZWQgdGhpcyBmaWxlIG"
    "F0IHRoZSByZXF1ZXN0IG9mIGEgdXNlciBhbmQgbWF5IGhhdmUgY3JlYXRlZCBvciBtb2RpZmllZCB0aGUgZmlsZSBjb250ZW50cy5tc29mdHdh"
    "cmVBZ2VudKFkbmFtZWZDbGF1ZGVyYWxsQWN0aW9uc0luY2x1ZGVk9QAAAMhqdW1iAAAAQGp1bWRjYm9yABEAEIAAAKoAOJtxE2MycGEuaGFzaC"
    "5kYXRhAAAAABhjMnNo8CyldtZPirYOjGll4kHvjQAAAIBjYm9ypWNhbGdmc2hhMjU2Y3BhZE4AAAAAAAAAAAAAAAAAAGRoYXNoWCAXR7vAFDBx"
    "SgfuZkDQnEu4AqpZQQr6iJa2Ld+ze/JanmRuYW1lbmp1bWJmIG1hbmlmZXN0amV4Y2x1c2lvbnOBomVzdGFydBRmbGVuZ3RoGRaLAAACPmp1bW"
    "IAAAAnanVtZGMyY2wAEQAQgAAAqgA4m3EDYzJwYS5jbGFpbS52MgAAAAIPY2JvcqVjYWxnZnNoYTI1NmlzaWduYXR1cmV4TXNlbGYjanVtYmY9"
    "L2MycGEvdXJuOmMycGE6MDc4YmM2NDMtODM0MC00NzhiLThhYzktOWY1NDczYmJjZDU3L2MycGEuc2lnbmF0dXJlamluc3RhbmNlSUR4LHhtcD"
    "ppaWQ6OTBhMDc2NTAtZDQwMi00YTVjLTg3MTktMWQ5MjcxMTc2MjllcmNyZWF0ZWRfYXNzZXJ0aW9uc4OiY3VybHgtc2VsZiNqdW1iZj1jMnBh"
    "LmFzc2VydGlvbnMvYzJwYS5pbmdyZWRpZW50LnYzZGhhc2hYILWeyfRueKXChz1j1gsXeyzqf5IhN42Mf3f4WceYhMToomN1cmx4KnNlbGYjan"
    "VtYmY9YzJwYS5hc3NlcnRpb25zL2MycGEuYWN0aW9ucy52MmRoYXNoWCAiO1aa1ATdpsbixEI58rlXUa1T8lolcSXOkwTpyImXkaJjdXJseClz"
    "ZWxmI2p1bWJmPWMycGEuYXNzZXJ0aW9ucy9jMnBhLmhhc2guZGF0YWRoYXNoWCCdg75JccbHc12vHo0gZIwLR1Dow9c52Zm/4A8y6a4lI3RjbG"
    "FpbV9nZW5lcmF0b3JfaW5mb6NkbmFtZW9BbnRocm9waWMgRmlsZXNndmVyc2lvbmUxLjAuMGtzcGVjVmVyc2lvbmUyLjQuMAAAEDhqdW1iAAAA"
    "KGp1bWRjMmNzABEAEIAAAKoAOJtxA2MycGEuc2lnbmF0dXJlAAAAEAhjYm9y0oRZAhKiASYYIVkCCjCCAgYwggGNoAMCAQICFEDloAruwjnQvr"
    "iD+gZCBT1nVRMAMAoGCCqGSM49BAMDMEkxFzAVBgNVBAoTDkFudGhyb3BpYywgUEJDMS4wLAYDVQQDEyVBbnRocm9waWMgQ29udGVudCBDcmVk"
    "ZW50aWFscyBSb290IENBMB4XDTI2MDgwNzE4NDM1NloXDTI4MDgwNjE5NDM1NlowRDEXMBUGA1UEChMOQW50aHJvcGljLCBQQkMxKTAnBgNVBA"
    "MTIEFudGhyb3BpYyBDbGF1ZGUgQ29udGVudCBTaWduaW5nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEmHoKa8tQGAUU1TS9QqU5W0Tp2N3X"
    "svlK7BfQt6YWKwEzd2R3/dzKPEUDdCjlLjp9fT+KFjRVnuZ9v0oXvTe3k6NYMFYwDgYDVR0PAQH/BAQDAgeAMBUGA1UdJQQOMAwGCisGAQQBg+"
    "heAgEwDAYDVR0TAQH/BAIwADAfBgNVHSMEGDAWgBTOUeIEgU5kWyP448TPmj6cwddcwjAKBggqhkjOPQQDAwNnADBkAjAxcx0UngF60stVjs5G"
    "4T2eiptsBk5mf9oCtfJPAUBl8qs/PEXa8+gk1/X5QJ2DVcYCMHBfXN31YapiSqYvlIWrDVDJKOvXMl+kkz37Wt0PBI8sw486Mq6JeOhT+lRR4b"
    "1HCaFjcGFkWQ2eAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA9lhAk+e/5ZL0idmb4z45s8t7u+BhU41sJ38cJJyGvRYPwaBcrhu73VixCk47jpIXxSTc"
    "xwnGv1722lISeJnKldksHv/bAEMAAgEBAQEBAgEBAQICAgICBAMCAgICBQQEAwQGBQYGBgUGBgYHCQgGBwkHBgYICwgJCgoKCgoGCAsMCwoMCQ"
    "oKCv/bAEMBAgICAgICBQMDBQoHBgcKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCv/AABEIAt0BogMB"
    "IgACEQEDEQH/xAAfAAABBQEBAQEBAQAAAAAAAAAAAQIDBAUGBwgJCgv/xAC1EAACAQMDAgQDBQUEBAAAAX0BAgMABBEFEiExQQYTUWEHInEUMo"
    "GRoQgjQrHBFVLR8CQzYnKCCQoWFxgZGiUmJygpKjQ1Njc4OTpDREVGR0hJSlNUVVZXWFlaY2RlZmdoaWpzdHV2d3h5eoOEhYaHiImKkpOUlZaX"
    "mJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4eLj5OXm5+jp6vHy8/T19vf4+fr/xAAfAQADAQEBAQEBAQEBAAAAAAAAAQ"
    "IDBAUGBwgJCgv/xAC1EQACAQIEBAMEBwUEBAABAncAAQIDEQQFITEGEkFRB2FxEyIygQgUQpGhscEJIzNS8BVictEKFiQ04SXxFxgZGiYnKCkq"
    "NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqCg4SFhoeIiYqSk5SVlpeYmZqio6Slpqeoqaqys7S1tre4ubrCw8TFxsfIyc"
    "rS09TV1tfY2dri4+Tl5ufo6ery8/T19vf4+fr/2gAMAwEAAhEDEQA/APkr4i+MfDfiT/gnedM03Uv9KtLPTrfV7T/ltbzR30f/ACz/APaleNr4"
    "c/4STxJ8IPDef+Pvw3D/AKX/ANvN3XXeP/gF/Zv7Ntr8bPDepf6LeWdncavpH/XSWOP93/20kj/+O1yPgHxjpupfEj4a6kPslraeHLP7BefbLz"
    "/lj5txJ5v/AJEoAn/aQvviR/wknhXw38SP+YR532T/AK8/Nj/+N1u/toQY8E+FNSx/pf2z/wBpeZW9/wAFCJ/+Jl4L1L/RP+Py8/7eP3tvXmnx"
    "71zTdS8E+H9N03Uv9E+2f9trebyv+un+roA8Z8S/8hK7/wCvyb/0bJRD0/CpfEn/ACErv/r7m/8ARslEP3aAPb/2Y/8Akrf/AHBov/SGvoBp+T"
    "x3rwP9mP8A5K3/ANwaL/0hr3y46/jQAef7UUeR70UAWIZ+KkqOpKACHp+FSw/dqKHp+FWoO34UATVJUdTw9fxoAWDvViq9WYf6UAXFueBz2q1D"
    "Vdeg+lbXhbVf+Eb8SaV4kOnWv+iXkNx9kvP+mcvmf8s6APsT4A/8E9/hx41+G3hXxt411LxVpV3ef8jdpH/HpN/qpPLjjjkj8y3/AHn2eT/rnX"
    "zf8bPgt43+APjY/DjxqbT7V9ihuPtlmPOhuIZP+ef/AJEj/wC2dfTPjD/go54aPwB0rxL4b060u/H93ec+HLz99DpHlyfvPM/6ZyR/u/8Anr/p"
    "Hm/8s6+XfjN8Tf8AhbXxJu/iPp3gm08P/bPJ/wBEs9X+1w+d/wAtJf8AVx/6z/nnQBzEH3KvQwVFZQjGM9qvWcFAFiz6fjV+H71VbPofpV6GCg"
    "CzZ17H+yl+z1qXxr8b/wDEz+y3WlaReQ/2vafa/JvPJk8z97H/AMtPL/d15BZwj1r1X9nX44j4J+Nv7S1I3V3pX/MXtLO0tvOv/wB1J5f7yT/V"
    "/vPLk/1lAHWfG79jvxJ8JtN1bxtpviTSrvQLO8/0P7Z5vneTJL5cf/TOST95Xj88H/MN/wDSyvX/AIufHbwV8R/hN/wjenf8JBaappHiOX7HZ3"
    "mryzQ6hp0nmf6TJ/yz8z/pn/yy/efvZK8phtz6GgD6j0j4+fCXUtPDaZqdrpX2Pybf7J4k/wBd+7j8z9x5kn7yP/P/ADzryH4P+PvBPxa+LX/C"
    "yfhvqX2vSrzWJvsn/Pb/AKaRSf8APOTzK8u8b6FqPiTTrTw5pg1W1+2Xn+l6vZ+VD/Z8Mf7z/lpHL5kkn+r/AO2kkv8AyzrzXxNZy/sf/EfQvi"
    "z8EPGl1/wlXizVobG9+Hd6ZNRm8aS/u/8AV/6y4jnjjT/j4/1ccZAk8smgD9etA/5B3PpXM/HH9qj4b/st6Z/wknxt+1eH9A/c2/8AwkV5eRfY"
    "/Ok/5Zf6zzP/ACHXhfhXwb/wUd+Pum2uqfFj4t6B8DfC3/Qu/DfytX16eH/p41O8j+z28n/Xvbyf9dKtn9kL4CeHPiP/AGj8JfhNbeKfiVpNnz"
    "8RfiPd3PiKbwzNJ/q5PMvJP3k//PO3j8v/AKa+VHJQBNZf8FLtT/az8SWvwC/YV8N6r/b93Z+fq/jrxJpHk2fhHSP9XJfSWkkn2iSeT/V21vcR"
    "x+bL+9/1Ucldx4U8K+Hr7TLTwS+o+KLu5sx/yFvEerR3d5f/AN+5u5P+ekn+s8v/AFX/ACzqb9iL4Rab4K8SfEr4tHxLquv3fi/xJDp//CRaxe"
    "ebeavDpEf2eS5k/dxx/wDH5Jf/ALuOPy/Kjj8r93WJ8W9b8bacdV1HwXpotbn/AJc7M/uqAL3jX4f6npumaB/wzf8AEnSdKu7Tzvtl3rGkS6j5"
    "/wC6/wBbHH9ojj+0f9NJPMryD4ffsfeI/wBo7/goX4Bb4+/FnVviBpfgXSJvF2raP4jFtDptjN+8s9Mij0y3j8v95J59x9ok/ef8SyOP/lpXd2"
    "et+JNN/wCJYdR/5c/8x17x+xVYrpfiTxBq8vg7TVutWWKfVvES3UnnT+QDHbxjzBykcZfj935Zd+P3lAHafET9mY+M/Gn/AAkdh4ix9rvYvtdr"
    "d/8ALCJI/wDln/3xHV7QP2cbHwx8QR4ysdU+z2wuhPa2oH+p/wCmf+5XH6P+0/4j074iXOneMjZ/ZOP9E/5a2Mv7yvVfhl8TtN+JOm3V/p+m3N"
    "r9l4Oe/wDwOgDp6sVXqxQAVXqxVegDj/2jP+TffiB/2J+rf+kUlfykfGuD+zfixqv/AG5/+kVvX9W/7Q8Q1D9n7x+Txnwhqw/8kpK/lR+Pn/Ja"
    "/EH1s/8A0hgoA4MfxV9T/wDBJefP7WniHP8A0Iktv/5PWlfMQ/i/z/y0r6c/4JSf2b/w1p4h1LUv+hEm/wBMvP8AU/8AH9af6ygD6t/4Kcwf8Y"
    "T+IOf+XzTv/S6Cus/4JXw/8YT+FP8Ar91H/wBOU9cd/wAFM9W8N6l+xzqv9m6la/8AIY07/jzvIv8An+T/AJZ10n/BMfxV4b039ijwp/xUmlWt"
    "39s1H/RP7Xi/5/p/+WdAHm//AAXE5+Engz/sM3f/AKTf/bK+wv2XoM/s4/D/AIu/+RO07/0mjr4r/wCC0HiPw34k+G3gzTNN8SaVd/8AE5u/+P"
    "O7im/5do/+ef8Aq6+x/wBmrxx4J074CfD/AE7UvG1paf8AFHadz/a8X/Ptb/8ATT93QB6yX1InO4/lRXN/8LJ8N/8ARRtK/wDAuL/45RQB+SPx"
    "Rg/41wf8Sw/8wfTv/Sm3r58u/Cv/AAknjf4V+G9N1L7JdXmkQ/6XZ/67/j5vPLkr23xP8RvBXiT9gW68N+GtS/0qz0fTre8s/wDlt/x/W/meXH"
    "/y0jry3wz/AMlr+C3/AGB4f/S67oAxPjxoXxI8E6l4f8N/EjUvtdrZ3n/Eo1f/AJY+T/y0/wDadan7YmleCtS8E+FPG3gn/S/td59nvLuz/c/8"
    "sv8Ann/z0ruP+Ciulab/AGl4K/7fP/beOvLP2jvhl/wrbw34f8SaaPtdpq9nD/od5/y73ccXmSeX/wAs/LoA8c8Tf8f93/19y/8Ao2SpLPra/W"
    "l8S/cuv+vyX7Z/1282pNO7/hQB7P8Asyf8lYP/AGB//bGvoG46j6CvAv2Y/wDkrN5/2B4v/SavfT1/CgAog70VJQAVJD0/CiHp+FSUALD92pYO"
    "34UQdvwqSGCgC1T4ev40Q9fxoh6/jQBL5HvVtPu1BD0/CrC29AFpcYH/ABMR0qWHr+NVbfoPoKtQ9fxoAsw9PwqzB0qtD0/CrMP3aALcIx/zEv"
    "zFfZXwB/YJ8E+JvhJpXiXxv/av9q+IrOG4s7v+1/Jht4bj95H5cfl/6zy/+enmV8e2m3dnNeky/tFfEj/hW+lfBPTNR/srw/Z/6P8A8SfzIZrj"
    "/rpJ/wA8/wDpnQBzuu6V/wAI34j1TTdN/wCPWzvJrez/ANLim/c+b+7/AHkf7uSiz6/hVLTO1eh/AefwRpvxI0rUfi14c+1eFftn/E3/ANE86H"
    "95FJ5f+r/6aeXQB7tpP/BO/wD4ST4TaB4k03xFd6B4qu/J/tez1e0/cwQySeX/AKv/AFkckcf7z/pr/wCi/K/j78ENS+AfxHuvBQ1K6urT9z9j"
    "1i8tPJhuPMi/eRV9naN4r034uabaeIfhN8STdaVaXkX2sfZZfO/56eXJHcfvP3mz/lp/yykr50/b6/a203TDpXw21LxvoGlWmr+d/a+j3l5FNN"
    "PNb3Mfl+X+88yP95/0z/5Z0AeHiAZ6/rV47ep/+M+RXlWv/FrUvGumjw1+zjqFrqmq2l5D9r1f/W6PYfvP3kVxP/y0n/d/8e8fmSx/8tfKq1f/"
    "ALL+mfEdrTU/j54kvPFN0DFcXVpefudIHl/vPKj0zzJLfy/M/wCenmSS+X+9koAsN8RPG3xZzpfwC+y/2WP+Pz4i3ln52m+V/wBOEf8AzEJP+m"
    "n/AB7Rf89ZP9VXq37L3wC8N+CPiRaalp32rVfEGr+Tb6t4t1j99qV//wA84vM/1dvH/wBO9v5cX/TKn2cAIzXpvwF8Ef2nqV3qOf8Aj08n/l78"
    "mbzv8/8AoygD6Y1/V9Q8N/8AEt1LTuP+Pi0tM1neBviD4b8R6h/Z2meItL0s/wCvuwP+esn7ySST/rpXnfiS4/tE3enf2l/pf+v/ANM83/pnH/"
    "20rtvgz4A1HUvtYx/1+fbP3v77/rnQB7PpFkdN0z+zT+dUW+HPhxtR/wCEj/s03V0f+fs+b/5DrT02w/s3Tf7NJ/Sr0IYEkD86APMoP2Y9PHxG"
    "/wCEj/tO1/sv7X5/2X/2n/1zr2Dwx8M9L8G+C7u+8FaXnVLqzlNpm6/L/WfcSk06A8AfrXfWsBK7SaAPjTWPhB4x+IniRfEH9nanc6ppV5i4XM"
    "n7n+D5/wDppX058D/AeoeCPBn/ABMdRu912IZ/sd2I/wDQJfL/AHkaV2f2CNQTipPJUjdjj3oAdUMM9Z3jXw3/AMJt4Y1Tw2dT1PSzeWcsH2zR"
    "7vybu33x/wCsjk/5ZyV8xfsS/GLxx8Jvi34p/YZ/aR1E3Piq0u7vXvh34gvRFFF4t0OSXzJJLfH+skt5JP3sf+sj8/8A1fleXLKAfWlFFB460A"
    "cZ8d/+SAeNf+xQ1H/0mkr+Uz49/wDJbfEH0s//AEhgr+rT47A/8KR8bcf8yfqP/pNJX8qH7QX/ACWzX/pZ/wDpDb0AcSvQ/SvXf2P/ANnrTf2k"
    "Pjbq3gnU9S+y/Y/Dn2//AI8/O/ffaY4//aleTL9019Xf8EjLfP7UXiD/ALET/wBvoKAJf2p/+Cdvhr4A/BO7+JOm+JPtX2O8s7f7J/ZEUMM/mX"
    "Mcf+s/7aVp/spf8E5/DXx++CelfFrUfG32W6vPOg/5BEU3+rlkj/1cn/XOvov/AIKhgj9jnxAf+ozpH/pyt66X/gltCR+xP4VbH/L5qP8A6XXE"
    "dAHxN+3J+xBpv7JXgnw/4k03xt/av9r6zNb/AGP+x4rP7P5cXmf8s69e+FX/AAR+8E/Ej4baB8SNS8Sar/xN9Hs7+7/4lFtNDb+ZbRyf8tK6n/"
    "gtjDj4S+C/+xkmPX/p2r65/ZWsR/wzf4Ax/wBCHpB/8kY6APjz/hyX4J/6KPa/+ENa/wDxyiv0FNvp+T8wooA/Db40fs5+G/Df7JNr8fvDepfZ"
    "f+JPp1xd6R/robj7RLHb/u/+/nmV5t8NfH+m/wDC2vh/qXiT/iVWmkeT9k/64+bcSf8AtSul8Z/FT4taZ+y3/wAKl8bab/xT+r2dnceHby8/5d"
    "4Y7m3uI/Lk/wCef7vy/Lk/56f8s6yH0Pw38SPG3wg8FalqX2q0u/Df2C7+x/8AX9dyf+1KAPQv2/77TdSHw/1LTdS/0W7F39ju/wDwHqj+2xB/"
    "xaXwV/1+Q3H/AH8tvMrhf2kvhJ4k+Ceo6Tpmpal/aulXl5N/ZF5/z7zf8tP3f/PT/V1tftH/ABU8N/Ej4S+FdN8N/a/tVneQ/bLT/tl5dAHgnj"
    "iD/iYat/1+Tf8Ao2Sn2HU/Wl+InOoarj/n8vP/AEbSWHU0Aez/ALNP/JWrv/rzi/8ASGvfbjqPoK8H/Zkg/wCLs3f/AF6Rf+kNe+z/ANBQBFUl"
    "SUQ9PwoAIen4VJUdWoO34UATU77OfQflUcHb8Ksw9fxoAIev41dqOjyPegAh6fhV6Dt+FVYen4Vag7fhQBYW3OBwOnpV6DvVaDoKswwUAFacP3"
    "qrQ2+OCKs2fU0AWIev41ds7celRwwVds4KAPVP2PvH3hvwV8atJ/4SXw5aapaav/xKP+PTzprea4lj8vy4/wB5/wAtP3clfcNx4c+EnhvTLv4K"
    "L4aGgaX4u+2W91aaPaeT9omki8uSX93/AKuTy/8A0XX54/DjxHqXw28SWniXTP8Al0vPtH2S8/5b19e/Dj9tDTPiOLvTdT0200C7FlNcWdn9r8"
    "7+0P3X/PTy4/3n/TOgDzr486F8R/2S/EmqeG/gl8W7u00Dxd5Or3hu/wDXafNH+7klkuPL/wBXJ+7/AO/dfMfiX4YeN/Eem/8ACbfDjTv7K0Dx"
    "H4k+z6t8RfscsN5r81x5lxcXNnH5fmfvP3n/ABMJJP8AnnLF9p8zza91/aE/Zk+AGn6joHiTUvDdpqt3d+JIr/8A4qTzdS+0WnmyXEkVx5kn+o"
    "/eR/u/9VL5cfm+Z5kte1/Hn4p/DfxL8Jtf074ceN7Q/wCmQ2F5Z/u/O1eHzI5JJI5JP3kn/bP/AJ50AfL2g+FtM8N6Za6Z4b037Ja2dn9ns7P/"
    "AJ96+nv2WPgVpv8Awjf/AAm3jbTLS6/1N/4du7O8l/0fy/8Anp/yzjkjk/66V87wxkd/xr3L9nv4+6j4c1O08OeNfEv2XwrZ2c32O0s9Ij/13m"
    "+Z/wAs4/M/56f6ygDsPi58F/hL4203xB8ffDfjb/Rfsf2j/iT+V5PnRxyf9/PMkrzz4Dz/APFR2hbUh+Ves/GH43/DfTdN/wCEK0zTbXxBaXdn"
    "9oz+7ls7f/nn/wAs/wDtp+7ryv4S25/4SK0yB/x+UAfTHhb4cf8AFSf8JJqOpC7/AOfPn/v3/wC1P+/ld1p1lpumnFZXhsr/AGZ93j1rbi27uF"
    "/WgCxbQfNVlOtVrb74+tXYhlulAHQeEID/AGlnFdmQR/Suf8EQeg6etdDywOaAG0o60lLg+lAHB/HgjTPBP/CSf8JHd2v2Pr9jMn7/AP6Z+XHX"
    "wx8ePgd8f/j54jtbDwT8WLTQDaXlnq3hG8u/CMl5P4a1z94kd7b3Edx5kcckmyOT935XlSSV+iWqafpmpafc6dqX/Hrd/uPrVbw74O8M+CtL/s"
    "/w5pf2YD6n/wAfkoA8v/Y6/aE1P43fDf8A4Rz4jeHm8PfEnwl5OkfEXwnd3Uc02n6j5YPmJJH+7uLS4j2XEVxHxJHJyI5I5Io9n4x/HFPh3jTd"
    "O07FyPKzd3f+p8r1/wBZ/ufnXif7V2neHfgr/wAFC/gv+1DqfiI6VpXiK01fwH4tuvtnkw3G+2kvNPE+f9Z5cgu/L/66Vp/tP614e8QeJb3w63"
    "h/7Pcm0+z6rdXdzIPPEkX/ADz8z93+7/7aUAdf4g+Lw8a/AXxB4b1Nj/aer+G9bgtLr7Lxn7O/9zp9/wD8cr+Zz9omHPx/8Vcf8vlnj/wBt6/d"
    "PQfiPqR8R3emab0+x/8AIXP/AC38z/WR1+Xf7df7JGnfDf8Abr8K6l8WvGulf8Ir8XPGEP2z+x/N06bT4ftNvbyRSf8APP8AdyR+VJ/0zoA+Qo"
    "oP+PX3r6n/AOCSFvn9qPxBpuf+ZDmz/wCB1nX6U+Cv+CcH7JXhv4A/8Ml6n4Ju9V8P3esf2vd/bNX8nUr+8j/1dzJcR+V/yz/d/wDXKOvhD4nf"
    "Cv4jf8Eu/wBrXVfiTpnwB0q08FeLrObSPCP9j+IrmbTYP3tvJ9muLi4jkuJLvy45JP3nl+b+8li/dR0Ae2f8FPYcfsT+IDj/AJfNI/8ATlb10H"
    "/BK+DP7E/hXj/l81L/ANLrj/2nXyF+1F/wUD8R/Gz4Jar8JtS+G2laV9svLO4+2WeryTTW/l3Mcn+rkjq7+zJ/wUR+I/wC+CWlfCXw38JNK1W0"
    "s/tlx9rvLy5h+0eZcySfu/LjoA9e/wCC2pI+Engs4/5mS8z/AOA1fXv7LEP/ABjf4A/7E7Tv/SaOvy4/bG/bT8bftReG9K0zxH8N7TQLXR7ya4"
    "+12d5czfvpI/L8rzJI469e+G//AAVQ+Nfw4+G/h/w3p3wT8P8A2XSNHs7C0vNY1e+/0iGOLy45f3cf/LSgD9LcaX/e/Sivz1P/AAV//aAz/wAm"
    "ueFf/Cu1P/5HooA8B+Meh6aP+CZ1pqX9m2t3d/2PpGftn/TS5t4/Nj/7ZyV8y6LpXiTwT43+H+peCfteq3er6PDf2ln/ANtbiPyo/wDv3X0Z45"
    "8ceG/En/BOC68E6ZqX2u70jR9I+12n2vyZrf8A063j82OP/nn/ANNK8q8Awf8AF7fgqP8AqW4f+PP/AK+bugCl+0l8W9S+Ng8Kf2l4b+y3ekXc"
    "1vd/8sf9ZJH/AMs5P+/dWP2xvhX4b+G/hvwr428N/ZLT+1/Jt9Xs/wDlj53lRyebH/5Ersv+CjnhXTdN8R+FNS03Tfsl1eCaC8u/+fjy/s/l/w"
    "DoySvLfjl/wsjTfBHh/wAN/FrUv+JV/wAwi8/10Nx+6/8AIf7ugDx/xZ/yFdV/6+5v/RtSxf6wUnirH9q3ep/9Pc3/AKNqWL74/GgD2L9mSD/i"
    "7V5x/wAukX/pFX0Aev4V8/8A7Mn/ACVm7/684v8A0mjr6APX8KAJKKKXyVoAkh+7UsHb8Kih+7UsHb8KALEMFS+Sfeo4fvVdhgoAKSLofpS1JF"
    "938KAJYYKt1Tq/DBQBZ8j3qxF938Kjhgq7DBjrQA6ADjNXrO3zkL65Fdr+zt+z14l/aR8SXfhzw3qVpaXVpZ+f/pn+p/55/wDLP/V/9dP3lXvj"
    "X8AvEnwBXQNN8bala/2rq9nNcXlpZ/vvsHlyeX5Xmf8ALST/AD+9oA4yGCrtn1P1qtZ9D9Ks2fU/WgC/D0/CrumE8c1Ws4K07Pr+FAFizsjpvQ"
    "1sQE7+tVbODtWlBB8w+tAFm0g5Na1nBVa0g5Na9nBQBatoPmIru/hVAD4itD71yNrB81dx8KYP+KitPrQB9TeHuNMGPetuDp+FZHh//kGD8a3I"
    "Pu/nQBYgIOQx71esztXb7VRg5BHpWt4csjqWoA/nQB2+hQDTdLx6e9X3HTFQRf6s/wCe9TuDxigBAATjNcv8VPiNYfDjw9/aIP8ApV2fItOM/v"
    "e2+uo3gfMRXmn7QXgHU/Gum2mpacLu6urTpaWhi8nP/PT95QB5neftL+ItU1G10z/hIxa3WPPxiP8A7+f9c6+gvD2uad4i0u08Q6e262ugPstf"
    "IGv/ALEv7RnjLxPZ+MvDPjPQPBl5a2sx/tbVrO51fj+C2uLSO4tt8fz+Z+7uI/L8uvVovj/8R/2btN0rS/2sfh1pVpoJEVifiH4QvZJtIsJc+W"
    "n2+3uP9I09JOB5mbiIZ/eyx0AeW/8ABUmLUPiL8Rv2cP2c/DfiR7e88RfGy11C8s7K5QTHTdPtriSe5dz+8jSPf+Mskcfevcfj18IPB+padqfx"
    "FvhcfajafMLVv3U8v+rSvm74b+LPDP7aP/BYnXfGWmRkaV+zt4Qm0ZBc2Jhlm1y7vbiCeTzP47fyopPK/wC/lfct7Y6bqWdP1HTftI9TQB8T3f"
    "w71D/hHLvxnpuni7trQTQXd16iOPzPL/791+XHgP8Aba+AXwT/AGtPiT8SPjb8ErrxBaaveQ/8Ijafu7z/AIRnzIvMu/L+2f8AHvHceZHJL5f/"
    "ADzj/dV++Pxh0jTNO+AHjM6fpo+zf8IhqOLTH/TtJX8tf7Qp/wCL2eIBnvaf9/vsNpQB+5/jD4mfAL4cfErQP+E3+I40vXvF1pN/wjv2y78m0v"
    "7S3/eSeX5n7uP93J/20r4S8B/tRfFr9qL/AIKQa/4a8SeN9Ku9A8DeG9Xt/Dv/AAjf/INv4ft1p/p3+sk8ySSOP/Wf9+v+WnmfBl74i8R+JDaj"
    "xJ4ju9V+x2cNhafbLvzfs9nH/q4o/wDpn/0zr6C/4JUwD/hsfxBj/oQ5v/S6woA+mf8AgphpP/GHPiAHH/H7pH/XH/j+jrb/AOCW+hY/Y50DH/"
    "P5qP8A6UyVS/4Ke5/4Y78QZ/6DGnf+l0db3/BLbn9ijwrj/n71H/0pkoA8m/4LU2P9m/DjwV7avL/6Kr6w/ZT8Ogfs4eANTGpf8ydpH/pFHXyp"
    "/wAFqv8Akm/gr/sMXf8A6TV9jfspwf8AGL3gA/8AUnaR/wA8v+faOgDoz4eYHGbv/wAi/wDxuiugDaZj/kdP8/8AfuigD8K/il+znpvgn9ku0+"
    "Nnhs3Vr/xJ9OuNX0i8vPO+0TSS28fmx/8APP8AeSRyf88q8++EHxG03/hdnw11LxJ/otr4cs4bf/j0l/0iHzLiTzf+mn7ySSP/ALZ11/xM1X4/"
    "eCf2Xbr4b+NvDf8AxSur2dp/ZF5/x+fZ4ftNvJHFb3H/ADz/AHfl+XJXMaDofgn4tfEj4QeCf7S+12n9j/YNX/1v+jzfbruT/v5/q6APSv8Ago"
    "h4j03xJqHw/wBS03UbS7tcXn+mWf77/lrb1V/bw0r/AIsn4A/6/LT/ANIa4H9rD4O6l8E/Eek/8TL7XaXd5/of2z/XW/l+X/rP/If7ytH9pD47"
    "6b8WvhvoHhv+zfsuq6ReQ/a7P97/AKnypI/N/wDIdAHz742/5GPxB/2GLz/0ZJVt+MYqp48/5GXxB/2GLv8A9KpKtv2oA9d/Zq/5K1ef9ecP/p"
    "NHX0NP/QV4H+zV/wAlavP+vSL/ANIq+gZ+lADF6D6UtA6CpYfu0AJU0Hb8KIYPb/4zVmHr+NAEkPX8alg71HUkHegCOrEPT8Kjq9ZUAWILfI5F"
    "WsD0qKDoKswd6AFs4K0oYKqw/dq1D96gD2r9kz9pLTf2Xv8AhINR/wCFcXeq3er2cMFn/pnk/Z/Llk8z/ln/ANNP/Idc38bfjV4k+PnjX/hN/E"
    "mmm1/0PyLSz/132eHzJJP/AGpXExQAYAFXbcgqRigCaGCr0MHNRQ9fxqzZ0AWrPofpWtpsFUrODtWtZwUAaVn1P1rRtIOD9KpWZGevatG0I2nm"
    "gDQs+v4VtWcFZtnBW3ZwUAX7WD5jXZ/Cn/kY7T61x8fJ49K7T4VQf8VDaUAfTuiwdK6Cwg+WsPRIB/ZuK6G2+7QA+AHqa6nwFZcf2kefauWgz0"
    "9BXf8AhW3/ALO0wfWgCxqms6X4fw+qagLb7UfJUn1pvhvxX4e8SaYNQ8OagLq1N15GQcgGvEP2svEOp+GyP7V8SZ0y7HNrdj9zB5f/AE08vy//"
    "AGpXn/7DX7Wnw31LTtesPiR43tvCl3ZXkxOk+LrqLTjcQx+Wn2iOSR/LuI/+mkckkfz0AfYB4OKKwfA3xM+HXxH0z+0vh1430nxBa/8AP3o2rR"
    "XcP/fyM1sef7mgCas3XNK03xLpd34c8Saba3lrdWfkXdnd2nnRTxSfu5I3jP8ArEq55/ua8Q+O3w/1X9pD4j23wS8SjVLP4f6PaRat42+w6tLa"
    "f29dySf6HpG+P955H7uS4uR5kf8Ay5x/vI5ZRQB84f8ABJHwp4b/AGcf2t/2kf2PDqd1dXWkeJNO17w9qv2zzhcaFcRSJaR+Z/07RiO3k/6aR1"
    "+goU46V+a/7Qf7IDfs4/tP+Fv2sv8Agmd4L0Dw9d6TeRaR4u8D6NZ2ukaRq1nJcx/a7n935fmSeXJ+8/6945I/3sXly/oB4E+IumeN9Nub2wDW"
    "/wBku/IJI64x9z++lAFb4027an8J/FPhvThm6vfDuoW9oD/y3me2kjSOvyo+IX/BD74AeJP+Eq+JGm+NvFV3dav4bhn8I6R9sihhsNRjsY7fzZ"
    "JJP9Z5kkcf7uT/AFXmSf8ATLy/0i/aX0XD2niIeItKtcnyLQ3l35M083mf6tP+eleTQ6F4jHib+09S8Sf8SrH/ACCf+mv7v95+8oA+AP2ff+CR"
    "nwj+CXgm7/4bF8SaV4g8U6vpHFpZ6vJpum6RD5cfnyxyeZHJcSR/6v7RJ5cX+r/dV87/APBNIeGv+G6/Gp8FG7u9A/4RvUbfSLz/AKc49Tt47e"
    "WT/tnX6Hftz/sI/wDDbHjbwWfEnxau7Twr4dvJrjV/DlnpEXnT/wCr/wCPeT/lnJJ/q/Mk/wBVHXyX+0J/wSa1H4AalafEf9l3416rpX9r/wDE"
    "vvLPxJ4j+yfYIf8AWeVHcW8fmXEcnl/6uT/nnQB3X/BT8/8AGHHiD/r90jH2P/r+t62/+CWmP+GJvCh1Ifa/9M1L/wBLrj/lnXxr+0h+zp+1F4"
    "J+Cl343+I/xsOq6BZ3ln9rs7Pxdc3kPnSXMccfmRyfu5P3knmVZ/Zi/Ze/a1+JPwl0rxL8N/jZdaBpV4Lz7JpFn4ivrT7P5dzJHJL5dv8Au/3k"
    "nmUAe4/8Ftefht4K0z/qMXc//ktHX17+y7cD/hm7wAdS/wChP0jr/wBeMdfLH7NH/BJrxt8WvEn/AAkn7bHxI1/xB4Vs7Ob/AIR0aR46uZv337"
    "yOTzPtEfmR+X+7/wBX/wBtazvip/wTa/aj8E+N7vw58E/2otV/4RW0s7O38O/2x46uftn7u2jj/eRx/u4/3nmf6ugD732f7I/8DKK/PX/h2N+3"
    "6P8Am6D/AMu/Uv8A43RQB418eP7O1L/glT/aWpaba/8AIt6F/wCnKz//AHn/AGzr5S8P+ANS/wCEj+G2m/DfUv7K1XxFo8Nx/wAfcsP2i7+03k"
    "ccsf8Azz/49469T8efFv4kf8Mc3Xwm8bab/ol5o+kXGkXn/La38u5t5P8AV/8APOud+FP/ACWn4G/9gaH/ANLtQoAx/wBqLxj8SPEmpaV4b+Nm"
    "mfZdV8O3n/L5/rriGTy/+ef/AFzrof23ND8E/wDCE+FPiR4K+yfa7z/QLy7s/wDrl5n7yOus/wCCllidS8SeANNAP2W7+12/2T7X/wBNbf8A+O"
    "V5x+0/8F9S+Cmm+H/EmmandXelav5Nxaf8sZvO/wBZH/5DkoA8L8UYOp6r/wATL7XdfbJf9M/5+P3lXW6/nVLxQR/aerZP2T/ib3f/AKMk8yrs"
    "XU/jQB7F+zP/AMlavP8Arzi/9Ia+g5uVXPpXgP7M/wDyVnVv+vOH/wBIY6+g5uMZ9DQA6Dt+FSfZz6D8qWH71LQAkP3qkh6/jUcP3qsQ9fxoAW"
    "rEMHFVoev41ZoAIen4Vag7fhUUP3at0ATrCcDr0qzZ1FBBwKuw9PwoAWH7taVn1/Co/JPvVmGCgC5D0/CrUHb8Kih+7V6zgoAWGDmrtn0/GooY"
    "OKvWcFAF6z6/hWlZVRs4K17KD5sUAWbQncea1tNJ45qlad/rWlZ9T9aANazgrXh6/jVHTYK17Pp+NAFiH7td58I4P+KmtK42zg7V3/wl/wCRlt"
    "PrQB9Kabxil1HXNO0z/r771Jo/GPrXk3xv1zTdT8R3TacObT9wRn/X/wDbP/lnQB6r4F8deG/GmoWthpwNrdel2P8AyHXscB6Y9K+CPg/4q8R6"
    "l4ju9N1PTLrSvsl5/od3+9/1Mf8Ay1j/AOmnl192+GCP+EcszZfafs/2KLm7P77Hl/8ALT/pp0oA8ou/hj+2P4z0y6XxH+0h4W0AXQlFn/wjnw"
    "8M09h/01+0Xt5LHJJ/27+X/wBMqteCP2MfgR4YGl6r4m8D2njbxTo96L9fG/jq1j1DUzeeW8f2mOWSPFv+7kk/dW/lRR+Y/lxivYScnNFAHhvx"
    "Z/Y08HeI9Yuviz8EXtfht8SAM23i7w7YxxfaNmcRalBGPL1CD/pnJ+9G9/KkizmvBZ/+CsXxf8b+J7v9m34KfsVa9qvxx0e8l0/xFZ/2tbTeFt"
    "Bmj/dyX1xqlvJJ/on+skjik+zXMvl+V5cclfUf7TTfDnTfgn4i8RfFrxvqfh7wxpNn9v8AEF7pGrS6dMbSL95JH58GJI/MP7vEflyS/wCrzzXz"
    "1+z/AGOq/snfATwd8OfD3w50zwtqmr2k3ibxbpFqMfZtW1O4nu7i1f8A65yT+X/wBKAOEP7En7UXx68aDVf2o/8Agpd4/wBJ8ZXYP2XSPhAbHT"
    "dB0/8Ad/PbRi4iluLjy/3n7yTyvM+ep9S/Y1/bW+A2paD4i1P/AIKz6rd+H/7Yht7XSNX8JW0M1/5nmeXbR3ElxJHJJ/q/+Xf955b1el+MXhvU"
    "viPqvhqwP2bVR+/vLTH/ACxk8z/lp/zzk/eVsfFDxT8RPG/wqtPhRp3iXQ9L0C7PkauLvw+bu7FpJ8/+j+Z/o8c8cnl+XJJHiPGaANbxhfeJNN"
    "+yDw5p1rd2n2z/AIm4/wBT5EPlSfvY4/8Alp+88v8Ad/8ATSuM/aNh/Zv8O+HLX4kfH74k6/4BuhZ/8Sjx14bvL601K3/55+XJbx+XJ/1zkjki"
    "/wC/lcV8ePGP7SP7Jfhy08S6bp1p8VPAFpZ+fq954w8R6bp2paRD/q4/9I8y2jvPM8zy4/3fmfu/9bL5ldhoF1+1f/wVD+FF58OtM8Lj4G/C67"
    "E2geLD4v0eLUfFV/LBhLy2t7ST/Q9PjjkHl/aJPtEnD+VHH/rKAPiTWP8Agt9+1l4a8Far4M8beI/D/jXS7TzhpHiPWPDv2PWP3f8Ax73P+h3H"
    "l/8ALP8AeeZ5f/XWStP/AIJj/wDBRj4/ftH/ALUXjXw38bPDV1qo8R2dp/ZH9j/6HZ6BDafbP3Ukfl/8tPtEf7zzJJf3cf8ArP8Aln+ifg7/AI"
    "JcfsUfst/CbxXrHw5+C9td6+3hDUFuvFfiW7k1HUZv9Gk/eeZP/wAe5/6944+lfid8DP2vvGv7E/7R/jXxL4b0zw+P+Eis9Nt9X/4SQS+T5P2G"
    "OT7T5lvH5n7uSTzPL/5a/wDfqWgD9h7zVvEp8SWmm6Zp139lNn/pd3Z+V/o//TKvA/20JPjV+0ANU/Zf/ZO+CouvGmkmzv8AWPEfiPyrTTtIhk"
    "/eRx293JHJJ9rkj/5945PLj8zzPL8yPzPLbv8A4Lf/ALN+malpWmeI/gn4qurW70f7R4ju7O0j/wCJRqMkUcklt9n/AOXjy5PMjkk/79ebX0t+"
    "wZ+x9pn7HXhfxl8V9S+IninVNW+Id5Fr/i7StXvI7yKxu445D/oknl+f/q/Ljk8yST/UR+X5f+roA/IP9rXxV+1l+y94ku/2b/2xvEl3pV3q9l"
    "Df2mkXniKXXdN1eHzfMjuY7uT/AFcfmRyf9cvL/wCmle0fsT/8FJvDfwU+G+k/Djxt8OLv+wLS8m/4qLR7uSb/AI+LmS4/eR+XH/z0/wDIf/LS"
    "v1r8SQ+CPiR4K1XUtS8N/wDH5o95pH2u8s4/O+ySfu5Io5JP9XHJXhX7OP7DP7HXw2Hh/TfDXwktftfh2z+z/wBr3n/H5q/7ry5Ptnl/u7z/AJ"
    "6fvI/L83/VeXQBgeKf+Cmn7L3gr4b3a+G/Deq3Wq/Y7S4s/Dv73Tv7Xhnk8v7d9ruI44/sn+s/ef62Xy/3UUlXvh7+0L4c+JP7WmlfCX9kvw5p"
    "Wv8AhXSPDcN/458c/vfJt4biSf7PHb/9N/8AR5P+/kf/ADzkr17xh4O+E2m6pdeNdM+HGk6r4qtLKHSLO7+x/vreH95JHbeZ5f8Ao8f7yT/0VX"
    "5n/FvxV8e/+Cb/AO2JdfFrTWvPCvgz4imL7bq2j3X9o2X9o/PJPHJB5cf+sk824j/5a/vJ/KoA/XYeBtLIztH5UV8LW/7dH7VWtW6axoeo+Crm"
    "yu0E1nc/2Rc/vYmG5H/1ndSD+NFAH5//ABM+Lfgnxt/wTPuvBOm+JLX+39Is9I+2Wn2P99+7ubf95H/yz/1fmf8ATX95+9rxLw3ZeNvDfxI+Fe"
    "peCNS+16reaPDf6R9s8r/R/wB7cfuv/RlewfGT4EeCf+GF9K+P3hv7Xa6r/Y+nfa/sd35MNx5lzHHJ5kf/AC0/1leI+A/iNqXhv4tfDbxJ420y"
    "7/srw75P2T7H/wAvGnebceZ5f/bSSSgDuf2tPip4k+LXiPwoPG3hz+yrrSNYl/48/N/5aS2//PT/AJafu6639uufTR8E/ht/Zv8ApX+l2mP/AA"
    "GjrJ/b88VeCfiRqPw/8SeCNTtP+Py7sNXvLP8A11v+9t5I/Mj/ANZ5n7ySuW/aQ8G/Ej4b+G9A8N/Ej7VdaV9sP9kf6X/x73n/AFz/AOufmUAf"
    "P/jb/kY/EH/YYvP/AEZJWu/aszxh/wAjHq3/AGGLz/0ZJWmvQfSgD2P9mHP/AAtm85/5c4f/AEh/+119BzwY3YrwH9lX/krF5/15Q/8ApDX0I3"
    "8X4UAR/Zz6D8qmqSigCOnw9fxpakoAjg71Yh6fhUcPT8Ksw/doAl+ze1WbPr+FRw/eq7B3oAsQwcVei6VFD92rNr3oAu2/T8Klh6fhUdv0q7DB"
    "QBLB2/CrtnBVaGCr0MFAEtnBWnZ9fwqjZwdq0oYOaALtn0/GtOD7w+tVdNtz/hWnZQHINAFi07/WtfR7HFUbSDk1t6bBQBp2fX8K0YYKrWdaWm"
    "9qANHTYM16B8K4P+Kkta4mzgruvhZx4jtT7UAfRWlkHj3qj41+HGneNOc2lp/pn+l3Z/13lf8APOP/AMh1c0bjBHFdDpsWP/r0AYnwX+Ath4cl"
    "HjPxCRdXef8ARTaeZz/rE+f1/wBZXrsRyvA6UyzgAHHSn/7WKADd+NeC/HH9qXxr8JfEl3p4+HJu7W1yLT/lrNf/ALvzP3flySSf9s/Lr3org8"
    "flXmPxK/Zv0z4i+IDqeo6kFF3/AMhYn97LPF/BFH/zzjoA81/Zf8Sn9ucj9or4r+GvEGlaXpGr/Z/Dnw88SWUcVnYXkGySPUpPLkkj1Cf54/Lk"
    "z5dtLH+6j82OSWvQv2g/g6fEbnxppiXV1qY8qD7LZn/llXXfCX4YeHfhP4O/4RDw2xFot9POq9BAJJd/lJz8iRp8gT0SuuNvjtigD4X1H4E+IP"
    "DvxW0uyHh8HU9VzObT039f3lams/CXUvGupaVp2pfarW7+2ZtP+WX/AD08yvs77Dpv9ofb/wCzh9pxUV3pWlSaha6lqGmqbm2/49f+mNAHjXwX"
    "+CSP4hXxL8RvB1rd3GkN5/h06taRyzWM2ZB5kZf/AFcmz/lp/wBNK6n4BfDnUfhtp3irTBpf2W21f4javq9mv2zzf3V3c/aJJP8Apn5kkk8nl/"
    "8ATSvQZcBcV5x4s+MGpeBviNpEG7TtT8LaveHSru7tFHnaRqOySRJLgiT/AFEkaeV/q8xyeX/yzk/dgHSfGUgfBPxWx6f8I3qP/pNJX8ufxg1w"
    "6Z8WvFR03Uf9LvBadP8AU2/l2NhJHJ/00/eeZ/21t4/+edf0Y/GX40ahp2pap4c1HTjdaTdj7OLOztPO8+GSLy/+Wf8AB+8r4E/4LefsoaZ4l/"
    "ZL0r4keCdO0rStK+F/nH/hHLPSLaGGeG7ls7f93cf8s/L8vzPLjj/e0Afkn4b0PxJ428SWumeG9Nu9V8QaveQ2+kWln/x+XF5JL5cflyf89PM/"
    "5aV/QL8JfGXxb8N/CXStN+Nup6VqvjWztPs+sXnhvzPsdxN/z0j8z/yJ/q/3tfgZ8PfiB42+Evje1+JHw38SXela/af8ed3Z/wCu/efu/wDyJ/"
    "q6/XT9hb9ub4cftI/DU6Z42+JFpdfEC0s5r/xF4ds9Ils/s8P2ny4/s8cnmRyR/vLf/lpJ/rJKAPoTxJqh8SabjTT9l/687v8Acz15loOreI/7"
    "UuhqXBs7z7PZXf8A2y/5Z/8AkT/v3Ufx+/aM8E/su/CW6+JPjbTdV1XStI8mA/2P5XnXHmSxxx/6ySP/AJaSR1W+HHjH/hZPgnw/8SfDZ/0XxH"
    "Zw6gLP/Xf8fEUcnleZ/wBM6AIvjB+2P8Nf2XvDdrqnxr8R69a6Bq15/wAjHaaRc6jaW93/AMs4p/s/mSR/6v8A1nl+X/20rxT4wf8ABRH9gP41"
    "+GtV+HHxJ1O71XQNXs/s95af8I5fTWf/AGzuI45I/wDtpHJXsfxh/Ze+Cfx//wCJl8fvDf8Ab9ro9n/yLusavc/2b+7/AHnm/Z7eSP7RJ/108z"
    "/Vx/8AbT4v/aQ/4ImaYftfjX9kvxv9lz51x/wiOsf6n/W/8u9x/wAu/wDy0/1nmf8AXWKgDye4+Cv7GL3DvoX7TnisWJcmz+1eAtTll8rPyb3+"
    "zfO23GW7nmivjzWrvUdF1m70b/hHdVufsl1JD9otbiOWKXaxXejq+11OMhhwQciigD3nxn+0n/aX7Hd18AfEnhu0tPtej6db6RefvYftENvc29"
    "x+8jk/5aeXHJ+8jrD8H+HPDfiT4tfCDw3qWm/arW78Nw293Z/8/EP2m/r3X4x+FdN1L/gl7aeJP7N/0r/hG9IuLO7/AOW3/H9b+ZF/1z8uvDvh"
    "tn/hbXwW/sz/AKFuH/0pvKAK/wC1/wDAjTfgn400D/hCftd1a6v/AMud5/0zlj8uLzP+2kdR/tIfHDUviR4c8P8Agnxt4b+y6rpF5DcXl3/qft"
    "H7ry/+Wldj/wAFAfEf9peIvBf/ABLbq0+x/bP+u3+tt/8Aln/yzk/d1pf8FFPB2m/8IT4J1L+zbS0+2ax9n+1/ZP33+q/56f8AkSgD5B8VT/8A"
    "FR6rqQH/ADGLz/0bJWwn3RWP4qg/s3xLr+m6b/0GLz/0pkrXk7UAey/srf8AJWrr/rzh/wDSGvof/lnXz7+yt/yVq64/5c4f/SH/AO2V9DT/AO"
    "rFADaKkog70AFEMFEHerMMFAEcMFSQ/dohgq1D96gCxABgcVZgA54qKCDgfSrsPT8KAJoYKu2cFVoYK07OCgCxDBV6H71RQwVZ8k+9AC+R71dh"
    "gqOzgqz9nHofyoAsw/erSgg5FUbO3PpWlZA8cd6AL1qTuPPetKzhPrVK0+8a1rODtQBZ02DFb+m9qyNNgzW5ZwdqANKzgrXs+v4VmWcFa2mwZo"
    "A1rCDK13Hw1t/+KjtuO1cpp8Hy12vw0/5GS2oA9+0jPT1rrfB9hjU8/nxXL6RjaDXf+CrH/iXdaANgnnIpMnrmg8HFFAFigknqaKKADJHQ0Dk8"
    "0UUAJcFRy3v1qHdu5B6+hryv9o/xV4l08Dw5p6/6LeWn+l3f2T/X/wDTOvnI/tQeNfgv8RtL+G3w38F3mqXV4f8Aj0u7yKHR7eJzs/eSf6yOT9"
    "5HJ+7jk/d0AfUf7SHizTvDfw5utM1J7o/2t/o9qLMR/wDtSvkfxj8T9R8OfZdN/wBLuv7WvIbf7JZ2f/oz/nnX2N40+FNh8RfDGm2HjLUN15Zf"
    "6QLmzHk5P8fyHzPk/GvAtH/Zu8a+N9SuseGxa/ZBNzef89v+eUf/AMcoAzfBWrbfDWqk6d9r+x2c1/z/ANO9t5nlf+Q/9ZX42/8ABWf9s/TP2w"
    "/j7pY8Eabq2laV4d8Nw2F3pF5eRfv9R8yS4+0xxxyf887iOPzJP3n7uv3e8I+AfGXgn9nTx9Z+NNM+yt/Y+ozWY3Ryy4Nk/wDzz71/NR8ZP+St"
    "eIM+sP8A6TW9AHLxcH/t7/8AalfXX/BISfP7UXirj/mQ/wD2+tK+SYzkk/8AT3D/AOjK+tv+CRUH/GUXirTf+pD/APbm0/8AjlAH0z/wVR/5Mo"
    "18f9RnSP8A05wVv/8ABMG/1LUf2J/C39pal9q+x3upW/8A2xjvn/dVif8ABUO3x+xR4gP/AE+6R/6coK6j/gkXYabqf7JXgvTdRH+if2zef+lz"
    "yRxf9tKAPpPwf8Ax41bS/EepeJP+JV/x8Ys/9d50f/LKSSvJv2hfhIP+EluvDXxI8S3WlfDbSNH+0eI7v95p1nq8sksn+jXF/wDu/s9vHHH5ks"
    "ccn+k/aPK/1UckUn0r4wufEfiPTLs/DjxJpX2q0vP9L/5bfZ/+WkkUnlyf6z/45WH8SPGPhz+zrvw22mfa7qzvYftdneWn7nzv3dxHLHJJ/rP+"
    "Wf8Aq/8AlrQB+fWnfs5aJ4b0+Dw7oXiXxB9hsIVtrP8A4oPVz+6QBU5HB+UDpRX2j9v1H+8P1ooA/Em//ah/tL9ju6+APiTw39k/4k9pb6ReWf"
    "8A0zuY7j955n/TOP8A5Z1h/BifTf8AhdvwV/s3Uvtf+iQ293/y2+zzfabjzIv+ulet/GTw5pupf8EvbTxt/Zv2q6/4RvTre0vP3X+jzR31vHJ5"
    "cn/LP/lpXz54U8HeJPDfjf4a6b8N9S+yXWr2f2//AEz/AJd5vtNxH+7k/wCWcfl0Ae1f8FJoBpviXwDqX/Pne3f/AKMt/wDv5WF+3HpWpeG/BX"
    "gz+0v+PX7b59nef9s/9VXH/tN/Eb4kal4l0Dw38WtN/srVdIvZvsd39ji/0jzPL8zzP+Wcn+rj/wCeddz+3T4x8E+Nvhx4B/4QnUvtVp/wkv2e"
    "8+2f8u/+jUAfKHxAg/4qXxD/ANhi8/8ASmStKqXj/wD5HjxD/wBhjUf/AEpkq3B2/CgD2T9kqA/8La1U/wDTnD/6Q+XX0ZP90fSvnj9k7/krN5"
    "/16Q/+kNfQ8/Qf7tADYYKk8j3qODvUlAEdn0/GpKIen4VLDBQBLDBVmz6/hUcP3qs+R70AT2/T8KvWcHaq0H3R9KvQdvwoAtQwc1ch6fhVKz6/"
    "hWvZUAWIYKvQwVHDBzV2GCgBfsI96swQZOD61peKvGPiTxtqX9peJf8Aj7+xw2/+h/uf9X/z0/6af9NKpW8HI+tAEkUJ3HmtazhPrWdF1P1rWs"
    "+p+tAF2zgrSs4KrWfT8a07Pr+FAF2zgras4KzrODtWtZwUAa1lWvpsGKo2ZG7rW3psFAGjp33TXY/DX/kZLX6Vydr9z8a7T4aQZ1G2Ge1AHvWh"
    "Qdf0r0zTIBpun56+1cL4ChJ1ME/nXfRc4GO1ADqsVXPBxRQBYooooAKKKKAOT+MXhXUvEfg42Xh9gLkXMXBP+v8A+mdeXaP+zJqT+NdN1HxDpu"
    "lXVstni7/7+ZeNP3f7z7kde/8AJHFQeTnqDQAkMBHGelIOTz60hOTmlXqPrQBznxe4+EnirH/Quaj/AOk8lfysfGok/FjxDk/8vkP/AKTW9f1V"
    "/F7/AJJH4q/7FzUP/SeSv5WPjLB/xdjVsf8ATH/0mt6AOWsv+QnafSvoX/gn98cfhv8AAD9oDxB4k+JGpXVrpV34O8j/AEOz86bzvNjk/wDacl"
    "fP1nB/x6CvpP8A4JwfCTwT8Wvj/wCKvDfjbTftVpaeDvPs/tn/AD2+028f/tSgD139ub9u74BfFr9m/Vfhv4c1PVbq7vLyzuD9s0iWH9zHcxyS"
    "f+i6l/Ys/b1+APwT/Zv8P/DfxJqWq/a7P7Z9r+yaR+5/eXMkn+s8z/nnJHWt+37+yv8ACX4S/su6r4l8OeCdK0q6+2ad9kvLO0/ffvLmOOT/AN"
    "GSVt/8E6v2V/hL8SP2XdA8beNvhxpWq3V3eXn+mXtp532jy7mSP/2nQBN8Qf8AgtV4b+CfhrQNN/Zv8Ff2raf2xNPq+kaxZ/2dDPDJbSeX9nkj"
    "83y5PtHlyf6v/ln/AMsq6iL/AILA/AHxHptrqfjXUvFV3qv2OH7Z9j8OReTbzf6zyrfzLj/Vx+ZXhX/BVb4A/CX4TeCPCupfDfwRpWlfbNYvPt"
    "Zs7Tyf3P2b/wC2V9F/s7fsP/AHxJ8E/BXiTUvhvoF1dXfhvTrj7ZeaRF53nfZo/wD2pJQBz3/D4j9ijv4L+IH/AIKLH/5Nor1f/hiD9lvv8AfB"
    "f/hOW1FAH5U/Ez4EeNvhv+x3/wALJ03xH9q8P3ej2n2uzvLSWGb/AFtvHHL/AM85P3n/AFzrjPBPjHTNN+LXwg1LxIfslrZ6PD/pn/TH7Tcfva"
    "9k+I3xo8FfEf8A4JwXXgrpqtno+kf8fl5++863vrOSSXy/+Wkf7uSvIPCvg7TPEnxI+C3hvxJptrdWur+G4ftlnef9f1/QB13/AAUa8ReGviRq"
    "Hw21Lw3qWlaraf6Zb3f2PyvO/wBbbyeVcf8AfyT/AFn/AD0rm/20vgDpnwl07wp428E6n/oniL/R/wCyP+fCbyv+Wcn/ADzq7+3J8FtN+E3iLw"
    "r/AMIR9q/4m95N/od5/wAsPL+z/wDLT/tpVL9q74t+NvG3hvw/8N/iR4J/srVdIvPtH/PH7RD/AMs/+Wn/AEz/ANZQB8++LLfUv7S1X+0v+Pv+"
    "2Lv7X/1282StCDt+FQfFH/kpHiDUv+XS78SXdxZ/9cZLmTy6uQ/eoA9m/ZRH/F2rrj/l0h/9IfLr6Gm+5+Ar55/ZQ/5K1df9ekP/AKQ19DzfdH"
    "0FAEvkrUXke9SUsMFABDBR9nHofyqWGCpIYKACGCr0MFRQ9fxqzDBxQBYs+n41es4O1RwdvwqzZ9fwoAs2fT8a17OCqMMFaVnBQBdhg4qzD92o"
    "4YKt0AJDBVmD7w+tEMHNS2/X8aAHwwcVr2cFUoYK1rOgC9Zwdq0oYOao2cFa+mdD9aANGzgrWs/u1Ts+v4V2vwr8Hf8ACbeJP7NOpWlr/wBfn/"
    "Lx/wBMqAM6zJ3da27ODtXonxztvhuFuz/Z1paeIMw3H/Ta4/5Z/wDfv/Wf9+687s/vc+tAGza/c/Gu1+Fv/IStfpXE2n+rruvhpkaja8UAfTPw"
    "8sR/Zv8AaWOTXU45ArN8NQnTdNthknHtWkTxmgBaKKsUAFFFFABRRRQAUUUUAFV6sVXoAx/ih/yTjxV/2Lt1/wCk0lfyu/GW3A+LmrDH/PH/AN"
    "Jrev6nvit/yTfxX/2Ll3/6TSV/LF8Zv+St+If+vyH/ANFR0ActaQfL/wBvlfWX/BIayz+1r4qGf+ZD/wDb6zr5Wj+7a19Y/wDBIWwJ/ai8U/8A"
    "P1/wgf8A7fW9AH01/wAFSef2OdfI/wCf3SP/AEut62P+CWtv/wAYc+FR/wBPuo4/8Drivo+8/Zl+E3xs8E2nhz4seGzqtpd/v7y0vDJDDcTRy+"
    "ZH5n/LT/WR1xvwY/Z71L9nDwRqvw307w59l8K2fiXV7jw5/pnnf8S6S+kuI/M/5af8tPL/AO2dAHyP/wAFtgP+Fb+C+f8AmMXn/pNX17+zHF/x"
    "jf4Ax/0J+kf+kMdfIf8AwW6vf+KI8AaaM/8AIZ1Ht/07V9g/BODUfBH7N/gD/hJP+hP0i3vMf9NLaOOgDsjvzxqN3/5DorLPjrw5n/kd/wBZKK"
    "APxW+J37NXw20z9hXSvj/pvhoWuv2ej6dcf8fcvk3HmS29vJ5kf/bT/wAh15V4L8ceJPhv418AeJPEmm/2r9ks/tHhyz+1yzf6H9pk/df9/I5K"
    "9N8V/tNab4k/Yn/4Zv1Lw3dWuq2ej2lhZ3n/ACx1D7HcxyR+Z5n+rk8uOuf+EH9m6l8Wfgtpum6laf6HZ/6X9ju/+Pf/AE68k/eeX/10joAsft"
    "efHfw18f8ASvCup6d/ov8AZH2v7Z9svP8AnpLb+X5cn/bOu5/4KVaHpv8AwhHgzOm2n/IY/wDIMkX+rrB/4KQ/Drw14J8Z+CtS8N6daaVd6v8A"
    "a/td3Z/9M/s/73/yJJXn/wC0F4O+LXwk0zQfDfxIH2vQPtn/ABKLyz/5+/8Anr/1zoA8c+IVh/ZviDVdN03/AI9LPWLu3s7T/rndSeXWlF/rD/"
    "ntWT48vv7R8Q6+P+oxef8Ao2StdvvGgD2f9kmD/i7V1z/y5xf+kMlfQ8nf6Cvnj9k//krVz/2x/wDSGvoxYOfwH/HnQAyH7tT1XqxQA6GerUMH"
    "NRQ9fxqzDBQBJD0/CrMMFRQ9Pwq5QA6zg7Vp2cFVbOCr1nBQBpQ/erRhg4qvDBzV2Hp+FAFmGCpYYKWpIev40AFXbeDkVF5Bq1ADjpQBdt+v4V"
    "pabBiqNvBwK0rOCgC1Z9T9a1rOCqVn1/CtvTYMUAWdM6n6Vv8AhXVT4b1L+0tM+yf9fn+u/wBZWTZwVr2XLYoA173xH4j8Ral/aXiPU/tef8+V"
    "H/zzq9aH5+fSqNnCQOMVq6bASeKANTTYOuD9K9M+CdkdS8SWvNecabbjaM9q9s+AGl4H9p478Y70Ae9ab90mrAAyRiq+nfdNT5wW5oAk8z2qx5"
    "ntWF4q8feG/BP2UeJNSFr9su/s9p71rrcbuRJn6UASUUUUAFFFFABRRRQAUUUUAYnxJ/5Jx4g/7A13/wCi3r+Vz4t/8lJ1f/r7h/8ARUdf1RfE"
    "P/knPiD/ALA93/6Kev5YvjBcD/hZOr/SG4H/AIDW9AHNRwcA/wDT5X2X/wAEW9c/sz9sbXxqecDwH/ph/wCff/SbT97/AOi6+OLUfJk/3q9W/Y"
    "//AGhtN/Zv+Nmq+NT4cu9V+2eG/sH2Oz8rzv3lzbyeb+8/1n+r/wDIlAH7jeNv7S8Si11Lw341tbUWl3595dj97FPDH/yzql8TfEWpaZptppv9"
    "m2htdX/5fK/PbxV/wWB8bab8Jf8AhCPDfwC8QeFbv99b/wBr+MLSWGb95LJJ+7t/L/1n7z/lpVHwT/wVPGm/Df8AtPxv8OLu6u7P/kL+I/El3/"
    "Z1ncTSf6vy/Mj/AOudAH274J0LUf8AhZGleNhqelWlppFneWF5aXv+uuPtEtvJHJHJ/wAs/L+z/wDkSvSNAv8Aw3qOo/8ACNeHNOtM+HfK/wBE"
    "/wCeHmf5k/7+V8qf8E5/2w9S/ay8ReIPEum/ALVbXwrjyPDnjq7P+h6hLH/rIo/tEcVw/wB+P/Vxyf6uTzfKk/d19FtqmpeHNSxp3+i3Q/8AJi"
    "gDtD4d8Qk5z+n/ANrorynUPizqIv5w3xYtQRM2R/a0XqaKAPw8h/Zi+JGpfCXQPG3hvUbvVbTV7OG4+yXn/Lv+6/56V5d4U8XalpviT/iWi70r"
    "VdIvP+XM/vreaP8Adyf9NP8A0ZX6efsr2Q1L9l3wWDj/AJFu0/6Y/Z/3X+tr4w/Y/wDAHhv4tftReP8Aw343037Vaf8AEx6/8u/+nf62P/yJQB"
    "5v8YPjD41+Nf8Awj//AAmuo2n2vSPO/wCJvZ2f+v8AM8v/AFn/AID1237afxj8E/FvwR4W/wCEJ1L/AEvSLv8A0yzvLPyZrf8A1f73/ppHUX7c"
    "fwC034A+NfD48E6ldWv9r+dn/TP33+tt/wD45Xm/ibwf4j8Ni1HiTTrr/TLKH7Hd/wDLG4hki8zzf+eckf7ygDz/AOJn/JSfFeB/zMmo/wDkO5"
    "krQvIKy/EUGm6mbvUtN/0r/n7rYm6/jQB7X+yV/wAlb1X/AK84v/SGOvomb71fPf7In/JWtU+tp/6Q19CTfeoAZU8PX8aPJPvUkPX8aAFhg5qx"
    "DBxRDBxVmH7tABDBV6Hp/wAvVVoYKuw9fxoAswwcVesqih+7V6z6/hQBowwcVehgqtD0/Cr9AD4YKsxdDVaGEetXCRg80AOiJz1qzZE+tRRdDV"
    "6GCgC7ZwVpWVUrPp+NaUMFAFmzrb03rVLTYM1rWcFAF6z6H6Vo2cFV7ODtWlD0/CgCzZk7SM9q29M6H61kab2rbs+n40AbGmwA4OfrX0P8MtK/"
    "s3TLXPX+deJ/D3SRqPiWz5zXv3h3P9qZHvQB6Jb9vxq0RkVV0+rROKAPPPjv8H9S+LFhbDTdSFtdWnmgC6/1M/mev/fFb/wp0bxH4b8F2+neMt"
    "Rtbm7thnNpyPK/g+euiH3c54+lPHYLQBPRVerFABRUPnH3qagAooooAKBycUUDg5oA574oH/i22v57+Hbv/wBJ5K/nh/Z7/YC8bft1fEbx/pvg"
    "nxvpXhW08I/Y/wDS7y0+1/aJri2/dxeXHJH/AM86/oZ+LgP/AAqTxUD28OXf/pNJX8+f7Cvx4/4UD+2Nqupf8Lt8P+C9K1ezhg1e88SWks1nf+"
    "XbW/lxSeXJH9nk/eSSfaJP9V+8/wCelAH6E/Ej/gm1+xR43+E3/Ck/DXw48P8AgrXx9juLTxF4b0i2/tK3+zyx+Z5ckn7yTzP3kf7z/npXwH/w"
    "Te+H/hs/tZ+P/BWp/wClf2Ro80B+2Wnkzfu76OPzf3n+r8yP955dfo1+1d+0T4J/Zc+G9p8fvGw0rVdVzaWFnZ2XlQ3mrwyeX5n2DzLj/Vx/8f"
    "H+s/1cclfC/iP9tjUP2tv2tbrxH+y98E9V0rxTq/w4m0i9IFtaalP5dzbyW9zPd/6vT7T955f2j95c+VH/AKNH5ssctAHvv7RfxN034kHVvhNp"
    "3iTwrdeH/tsNv4u8XeMPK8nSJv8AoG2ccn7vUL//AFf/AEytvM/e+b/qpPnD4P8A7Anhv9rM6r8SPG3xa8a6r4K+2XmkeEdI8Sav/wAhf7P5lv"
    "Jffu44/sdv9o/1ccf73yo/Nl/1nlR/V/wm/YY+G/gnTNA8bfFnw3/wmnjTw75N/aavef6mwmjl8zy9MtJP3dnH+8k/ef8AHzL/AK2WWWX97Xof"
    "xAvvBPgnwXd+Jf8AhHLXQLTSf7R1fV7wiL+PzLu4uZ/+eknmSSSSUAbnwB+Fen/s3/su6B8J/ht42u/EFrpFn9n8OXesWcVnNcQxy/6ry44444"
    "/+/dedf8FHvhjqXxq/Zv8A+EK034t2vgu6vPEdnb6tqwtLmXUb6HzPMksbeO3kjk8+SSOOP955kXleZ+6/5aV4Z4z/AOClf7UHwT/ZM8F/GvxJ"
    "4c8P3ev+ObSHGkavpEv2PSd9j9o82OOOTzI/+WfmR+ZJ5X/PWvefhX4i8S/Gz4b6B8fdS8N2l1411fwHp3/Lp5P2iaS28zy/M/5Zx+ZJJ/38ko"
    "A+Ff8AhxP4auP9I/4Qu7/efN/pXjK2SXnn51XR5lDeoEsgB6O/3iV7xcf8EgPBXjW4k8ZfE79pP43XPiXV3N74huPDfiCWy06W+lO+d7W384+R"
    "AZGcxxZOxCq9qKAOX/ZGJ/4Zw8F8/wDMs2f/AKTV8kf8E++f2xfGv/cX/wDId9X2B+yiP+McPBXH/Ms2dv8A+Qq+R/8Agn3/AMnreNf+45/6U0"
    "Aa3/BV2x/4qP4bj/r7t/8Av3Laf/HK9W8H/Dnw34k+CPhX/hJNNtbv/im9O/6bf6u2j8uvNv8AgrT/AMhL4b/9xH/0bZ17l8K/+SIeFP8AsW9O"
    "/wDIcSUAfkrqUH9m/wBq8/8AHneTW/8A5Frqbbo30rmdS/5iv/X7N/6Nrprbo30oA9t/ZL/5Kzqn/XnD/wCkNfQs3X8a+f8A9kr/AJKxqn/XpZ"
    "/+myvomaCgCtD1/GrsMHFR+R71Yh6fhQAVag7fhUUP3alg7fhQBZh6/jV6zgqrZwVdh6fhQBas4K0rOq0PX8a0oen4UAXYO34VZhgqOH71WYev"
    "40ASxE561JDCfWo44OT9auwdvwoAlh+9V2zqvD1/Grtn0/GgCzHBya0bOCmWkHNa1nY84oAs6PY1t2cHaqOmwVr2cHagC7ZwVZh6fhUUMHFXoO"
    "34UAXbOCta1+6fpWbCQDya29OgGOvagD0v4M6SBH/aeOa9V8O8apx6H+Vcn4D0r+zfDFpXWeHf+Qn+B/lQB6Lp33TVgfeNU9P6H6Vdg6n6UAGS"
    "epooooAdJ2rK8d+MNO8E+GbvxJqRP2W0H/LrWrJ2p08IOVPTFAHyBpX7QJ8HfGe2vdPa6+y3t1NObT7Vx5X+skOK+tNA1j/hJPDVpqdgP+Pu0h"
    "n/AO/lcLrX7Nvw61D4kWvxDGn4+yf8wu1tIvJnl/efvJPkzn95/wCOV6HZ2f2Bf7OBz6UATUUUUAFFFFAHLfGaf/i0vilsf8y3qPH/AG7SV/Ov"
    "8Af2OvEn7Wfx+8QeG/8AhJbTS7W08m4u7y8tLnybiGPy4/Kt5I45I5J/3n+r/d/6uv6J/jKP+LJ+Khn/AJlvUf8A0mkr8Dv2M/iP+0gfj94q+E"
    "nw3+LV14f8K3Z+3+I7y8+zeTpEPlRxyS2/2iOXy7iTy/L8zy5P+essX7ugDgf23P2cNX/Yq8SaC2oeNP8AhKxqljDB4fvbyzis7ye7+f8A0KSD"
    "zJJPLj8vzJJP9VHGa5X9iH/hdfgjxvqup/ADw5/b+q2ej/8AE3/0SL/XSSR/vZPMkj/eeZH/AM9K6n4ifC/xN8e/7W/bDsNM8QjwFc6xFoHh7x"
    "De3d1rk406OWPzNSk8ySSSSS4k/efu/wDlnHH/ANNa/Vn9mn9kP4S/su/Ca0+G2nf2Vqv+mfaP7Y/siKGa48yKP/WfvJP+ef8A6LoA+J9B/bD/"
    "AOCkPw3Olan8SPgnaf8ACP6ReQ3Gr3n2O2hm+yeb5lx5kn2yT93/ANNPL/dRVzn7ZX7aXx//AGsfgJdeC/A/wTNp4W+ImsWeg2fiOyvP32rw3d"
    "9HafZre3k8uTyJI5PL+0f6qXzP3X/PWvtL4/fsoeHPjWNK8E5urrwrZ+JLS/1e0+yf8f8AaR/aP9GuP3kfmQeZ9n/1f+t8vyv9XVn/AIUf8JPG"
    "unaVk3d1d+HfEn9r2Zs/s0P+l2999st/M/5afu/3cfl0AfmB+2Z4o/a18S+G9A/4X94ctNK0r7ZN/ZFnZ6RbWkMH7ry/Kt44/wDlnHHXqvws+I"
    "3/AAVE/wCFb6Bpvw28N2t1oFno9n/ZH+h6R53k+V+7/wBZ+88zy67b/gtLY/2b4b8FY6/2xef9Mf8AllX1H+yjoemj9m/wBjTbXP8Awh+nY/8A"
    "AaOgD5G/4Tf/AILE/wB7VP8AwD8P0V+gP9n6d/0Dbv8AKigD4p/ZXgx+zf4KGmfav+Rbs/8A0VHXyP8A8E/QP+G2fGvH/QX/APS6vrz9lex0z/"
    "hm/wAFf2b/AMev/CN2f/oqvkf9gT/k9fxp9dX/APS6gDoP+CrkB/4ST4bf9fmo/wDo2zjr274S/wDJE/C3/Ysad/6LjrxD/gq5/wAjJ8Nv+vu8"
    "/wDRlnXuHwtg/wCLKeFf+xa07/0mjoA/JnVOl3/1+zf+ja6OHp+FYGsddW/6/Lz/ANGyV0Fp/wAhGgD3H9kP/krWq/8AXlD/AOmyvoqbp+FfPX"
    "7JE/8AxdzVf+vKH/0219E0ARw9PwqxUdWoO34UAWLfr+FSwwUQdBVmGCgAs+n41eg7fhVaGCr0P3qALMMHNaMP3ar2cFXYYKAL0PX8asGDg81X"
    "h6/jVxuh+lAE1rByR71JB2/Co7XvVmGCgC7DBV6zg7VWh6fhWlZwdqANGzgrbs4KpabBmtazoAvWcHatGzgqvZ9T9a0rPp+NAFmGCrEcHBqtD9"
    "2tKODg/SgCxZ25yfm6V1HgjS/7S8S2ummuahhzwRmvTfg/peT/AGl6CgD0m0gBIra8Mf8AITNZ8P3xWh4e/wCQmfpQB6LbfdqwPvGqen9D9KuD"
    "7xoAWiiigAqxVerFABRk9M0YPpRg+lABRRRQAUDk0YPpVO+vdP03Tf7Tv9QFrbWg8+6JoAwvjNDj4SeKjjH/ABTmo/8ApNJX810H7Lnxa/aQ+N"
    "niD4KfCbw3a2niDV7Oa41e81j7T9j0HTvsPmW/2jy45PLkuJPL8rzP+WXmf89a/az40/tI/EX9sH4S+Mx+zD4kuvCvw2tPDmrT3fxSPlfa9e8i"
    "3ceVosEnmfuDKR5moSR+WY/9V5vmebH8Yf8ABIv4E/Dfwz/wmnxuPxa+1+IPEd5NYXfhy81ePzoIbT95Jc3HmSeZJJJJJJJ5n7v/AFlAH0f+zr"
    "8OfGvwm+EnhXTdTFraaraaPaf2vpFn/qbebyo/tFtH/wA9I45PMr0jUdD/AOEi0y01LxJpp+1Wf+kCzJ/495pIv9V/37krI0fQ/ELeJdV/4SXU"
    "ydK+xw/Y7Oz82GaD/np5n/kOvhH9lfQ9N+G//BUP4qeCPDfgi68K2n/CN/6ZpH/CRyajDcTSXNh/pPmSf8s5PM8zy/8AllQB93XvxA8N+HDa+G"
    "j9qurvV7z7PZ3lmP3P+rkk/eSf9s5KpaPofhzwTqN1/ZumfZbW7vJrj/Q/+e0n7yTzP+2n/oyvmb/go/rviXwV+y9q3iTw3qV1aarpGsaRcWd3"
    "Z/uZvO+3W/8Ay0/7aVrf8E3/ABH4k8bfsv6X4k8SeJdW1XVdX1jUbi8u7y886aeaS5k/1n/bPy4/+2dAHkP/AAWunB8NeCtS07/Rf+Jxef6H/w"
    "Bso/8Av3X1f+yTbg/su+AMj/mUNO/9Jo6+Uf8AgtFx4J8Fj/qM3mP+/Uf/ADzr6v8A2SOP2W/h/n/oRNI/9JY6AO5+1al6GirP/Et/6BwooA+H"
    "P2P7H/jG7wV/2Ldn/wCiq+R/+CfsH/GdXjT/ALjtx/22+3R19e/slwf8Yu+C/wDsW7T/ANp18jfsE/8AJ9fjX/uO/wDpdQBsf8FXf+Rk+G3/AF"
    "+Xn/oyzr3X4U/8kV8K/wDYuad/6TR14d/wVeBHiX4bZH/L5qP/AKMtK92+GFj/AMWU8KED/mW9N/8ASaOgD8m9Z+/q3/X5N/7UrbtP+QjWJrX/"
    "ACEtW/6/Jv8A0bJW3pv/ACEaAPev2Tf+Suar/wBeUP8A6Q19FTfdr51/ZI/5K5df9eUP/pDX0TQAsMFWYYKbU9nBQBJZ9fwqzDBxUcMFXYYKAC"
    "zg7VehgqOzByeKuwA88UAWYen4VehgqtDBV2GCgCWGDnNWYOoqKGA1at7fkcUASRdD9a0rPr+FUYoOa17OCgC7ZwVpabBUWmwZrXs4KAJbODtW"
    "3Z2POKrabBitazgoAswwc1dh6fhRZ9PxqzDBQAsMFXYO9Rwwc1dhgoAt2R4BNe1/DjSv7N8NWmO9eTeD9JGpanaaZ6171p0WAAKALNty5Nauh/"
    "8AIVrPtev41q6D/wAhf8aAO8tvu1YH3jVe2+7VgfeNAC0VHUlABVioIfu1OOooAdggAGszXvE+meHbD7fqGofZrYVbvrjahIr5D8YeJPEuLthq"
    "V3n1/e/9dP3kf/PSgD600zXNO1LTf7T07UvtVt9c14H8b/2iNR8N6n/auneIfstpZib7V9ku/wB1Pny/Lk/9Drjf2Nf2pPE3xasdf+FOl/BbXx"
    "d2d1MCLwRWlnYQyF44/tEnmeZ+8EZk/dxydf8AlnWp8Rv2T/EepjVNS1PStLurQmL/AESz8z9//q958uT/AJ5/vP8AvigDYj/a1+NnxF0u0H7N"
    "/wCzBdeKxcWmW8W+JPEMfh7QjLj5xHJ5dzeSJ9/95HbmP/ppXiH7MnhX49/8FMPBmu/tH/tQ+Ixd+Aby7nsPA3wk8OXkkWhauLSURnUruST95q"
    "FvJLbv5ccn7qSL975X7yt/9qb4h/Ef4c/syaX+zb8NdQNr8SfjPq//AAiPge2u8fa9PtJMJf6v5H9yztvMl/66eX/z0r66+Ffw38F/BP4a+Hvh"
    "P8N9O+x6D4c0e00nSbMHi3tLeJI4x/37joA+V9c+HXiSw0vX11HTbr7Ld+Btc6WkkX7n7HPHJ+7/AOujweX/ANMnjr8UvB/wq+Pvxa/aiutN+A"
    "Gm3d3qtn4k063vMeb9jt/Muf8AR/t/l/8ALp5lvH/rP+edf0ofF61S++EfirTievh3UAPxt3FfnN/wTO+Dx8E/svXfxJ8N6h9q8QfEW8u9X+yX"
    "h8mH93cyWlnF/q/Mjj8u3j/efvKAPKf23rL/AIKrfCbTbr42ab8fvCtp4V+HXhuG/wBXPg/w5/Z32+a4l+zyRSW+oXFz9ojt4/8ASP3nl/6z91"
    "FLLXif/BNL4neNvjZ+2x40+JPxI8S/2rqur+D/APS7z91D9o8u5s44/wB3H+7/ANXHX6EftcfGDTPgl+y/q3xa+Lfwnu/FdrpNpaZ0iztI/wDj"
    "7kljjjkk8zpH9o8v95+88uvzd/4JJT/2j+1D411Iabaf6Z4P+0fZLP8AdQ283263k/1f/POgD6U/4Kek/wDDHPiE/wDT5o//AKcretL/AIJZw5"
    "/Y68Pgnrd6j/6U1W/4Kf8AH7HPiEf9Pmj/APpdHV3/AIJZwH/hjrw9/wBfl5/6UyUAeU/8FqOfBPgH/sL3lv8A+S0f/wAbr6w/ZLg/4xc8Ac/8"
    "yFpH/pFHXyn/AMFnIM+CfAP/AGF7z/0mr6w/ZS5/Zb+Gp/4+/wDigtI/5fP+nKOgD0EaqCOf/adFSrremYH/ABMu3/PnLRQB8Nfsi2/9m/s3+C"
    "/+xbs//RVfI/8AwT9g/wCM6vGn/cd/9Lv/ALXX1x+yLB/xjf4L/tIf8y3D/wAvn/TKvkb/AIJ+/wDJ9PjX66v/AOl1AHR/8FaP+Rk+Gn/X5qX/"
    "AKMsK9x+FkA/4VL4V/7FzTf/AEmjrwn/AIKwf8jL8NP+vzUv/bOvf/hN/wAkl8K/9i7pv/oqOgD8j9d/5iv/AF+zf+jZK3NN/wCQjWRr3/IS1T"
    "/r8l/9GyVraR/yE7X6UAfQP7Ilv/xdvVuP+XO0/wDTbX0XPB8rfWvnT9k+3/4u3d8f88f/AE2yV9In7p+tAENXIYOar1cg70ASQ9Pwq9ZwVFDB"
    "V6Hr+NACwk5696uwk469qrQwc1ds+n40AXYO34Vehg5qOz6/hViGCgBSTk81Zhh9TUUXWrMMFAFmzgroNNgxWbZ9Pxra02DNAGnpsGa0bOCo7O"
    "Ctezg7UAWLOCtaGDiq1nBWlD92gCOH7tWofvVFB2/CrUPX8aALFnV6zg7VWh/pWlZwdqAO7+Emlf8AEx/tP616lb/w1yvw10n+zNN/OuwHb8aA"
    "JLXr+Naug/638ayrXr+Namg/8hegDvdO+6asD7xqvbfdqwPvGgBlSVHUlABVgDJxTYO34U4HBzQATY6j3rzn4m/BIfEa+/tRfEJtLkjyACPNir"
    "0abHQdDmvDv2hf2p9T/ZzW88SeNtAtf7CtLP8A0a7+1yfvjj/V48s+ZJ5n7vy44zJQB6P4N+GPgrwTg+HdNHFnDYcf88Y/9XXH/tA/tPeHPgm3"
    "/CO6V4evPFPjS8szPpPhDSP9aYuP9Ku5P9Xp9hH/AMtLi49fLj82XyopPHfAX7a37Un7R+pj4LfDj9l+78BeIDgeJPFniHxFY6jZeEof+epjg/"
    "eXF3J+8+zW8kcfTzLny/8AVH3Hw9+zr4J8D/DzUfCOgrcLfawPN8ReIL1xd6jq85/5ebuU/wCtk/KOMfu4xHGAAAfJA8EeC/Efjk/Ej4tAat8U"
    "/Ed5D/a3i6ytJf8AR7O3l8y0sbDzP+Pewj8z/V/8tZfMuZf3tfYX7Oh1IfDm2OpeI/7UuvtcuT9r87yP+mVefeOP2Z9U099N/wCEdC6rhsXX2s"
    "+V+98z/WH/AGP/AIivZfCfg7w54J0waZ4b077KfX/95QBR+LsI/wCFSeKuf+Zb1D/0mkr+er4D/tz/ABH/AGOf2kNV1IC71/w/++t/+ERvPEVz"
    "DZ2/2j7PJJLbx/vY45/9Z/yz/wCelf0K/FwA/CXxUB/0Lmo/+k0lfzAfGkf8Xb185x/pv/tKOgD1j9oP/gpp+1H8ff8AhK/DepeNbvSvBXiO9h"
    "P/AAiNn5UsNvDH5fl20dxJH5nl/u/Mk/1fm/vK7X/gjzAD+0h4qx38B/8Atzb18lRQccA19a/8Efuf2kPFP/Yn/wDtzb0AfTP/AAU4/wCTN/EP"
    "/X5pv/pdb1o/8Es4P+MOvD3/AF+aj/6XSVX/AOCmf/JnXiD/ALDOm/8ApdHV3/glnB/xh14eP/T3qP8A6UyUAeVf8FpYP+KI8A/9hm8/9Ja+r/"
    "2T4P8AjHD4fn/qTtI/9Jo6+Wf+C0n/ACI3gH/sZbz/ANJa+r/2Vf8Ak174f5/6ELSP+m3/AC4x0Ad+INNx/wDclFNH9o4H/Ex/8lKKAPhD9kuf"
    "/jG/wUNS/wChbs//AI3XyN+wH/yfV40/7i//AKUyf/a6+nv2Y9D1H/hSfhXUtN1L/mDw/wDPKvlD9iG48R/8NjeNR4a077Xd/wDE3/55f8/0f/"
    "PSgDuv+CtE4OpeAP8Ar81H/wBtK94+Ftx/xabwsP8AqWdO/wDSaOvmP/gpZ4x1LxJqXgDTNS03+yvsl5ef8uf7n/l3/wBXX0V8Idc07/hU3hbT"
    "T/0BbT/t3m8uOOgD8p9f/wCQnqv/AF+S/wDo2StbQ/8AkJ2n0rF1n/kKat/1+zf+1K1tC/5CdrQB9Ffslf8AJXdW/wC3T/0219HmE7T16186/s"
    "kwZ+LerH/pztP/AE219Hn7p+tADasQ9Pwqv5HvWjZ9D9KAJbKrMPX8ajs7c+lWYbc56UAWbPp+NXYYKjhgqzZg5PHegC7D1/GrEX3fwqOzqw3Q"
    "/SgB0XQ/Wr0PX8aqxda0rOCgDS02Cug02DNZFnBXSWcFAGlZ9PxrW02Cq1nBWlZwUAWbOCrMMFEMFWoev40AR+SferEEHNRw9fxqzB1FAFpPui"
    "tvw5Yf2nqtppnvWSnau6+Fmlf8TX+08fWgD0/R4Kv1DB2/CrVAD4vvj6mtDw5/yFKyz94VqeHP+QpQB3lt92rMHU/Sq1t92rEHU/SgBaKKKACr"
    "FFFAFPWTqY0u6/sv7L9q+yH7J9sz5Xm/8s99eQ6v+y2upavc/EXxp421TX/FTWUUP9r3n7qzsIRGBJFZ2n+rt0k/ef8APSX95+9ll8uOva+Fzu"
    "wPrUQuVxjqPrQB55+zb8O774dfCm20zUtOFrq2q+dqGrjOSLu4zI8f+35fyR/9s69AHbPej/aB6UcscE0AJk+tGT0zRRQBkfEj/km/iD/sD3f/"
    "AKLkr+Xz4ygf8LK1bj/l8h/9Jo6/qG8fEf8ACEeIDn/mD3f/AKLev5ffjIR/wsrVuf8Anj/6TR0AcgkHzGvrv/gkJb5+P3irj/mW4v8A0pjr5O"
    "teX4r62/4I/QakPj94q5/5luH/ANKo6APpj/gpn/yZxq//AGGNI/8AS6OtH/glrn/hjnw//aXX7Xef+lNU/wDgpp/yZ14h/wCwzpv/AKXW9XP+"
    "CV1v/wAYc6Tx/wAvd5/6U0AeZ/8ABZ6D/iivAHA/5DN5/wCik/8AtlfU37KH/Jt3gD/sT9O/9Jo6+Xv+CzEI/wCEK8Af9hm7/wDRaV9S/son/j"
    "G7wAM/8ydp3/kS2joA9CFrwP8AiZUUGHnrRQB8DfsjE/8ADOHgr+0z/wAy1Z/9+a+S/wDgnt/yfR41/tL/AEXjV/8A0tr6z/ZX/wCTXfBX/Ys2"
    "n/oqOvkz9gif/jPr4gfTXf8AyHex0Abf/BWT/kZfh/8A9fl5/wCjLOvWvhxof/FpvD+pZ/5lyzn/AO/kUcn/ALUryr/gqvOP+Em+H/P/AC+Xn/"
    "oyzr2z4TZ/4Un4V00j/j08Oab/AOk0dAH5Q6t/zFf+v2b/ANGyVr6F/wAhO1rI1H/l7/6/Zv8A0bJW34c/5Cdr9KAPo79kPH/C3NW/69If/SGT"
    "/wC119ISwZIzXzh+x3/yVu7/AOvOH/0hkr6Xl4Iz6UAQ1ehgqtDBV2z6n60AWrMnPWrsJOOvaq0HersP3aALMNv7VbtuTz60ln1/CltiAeTQBY"
    "h6fhVm36fjUUIPpVq3ByOPSgCSGCtvTYM1m2fX8K29Nse39m0Aa2nWODXSWcFZujwV0Fn1/GgCxZwVr2cHaq1n0/GrsMFAFqHr+NSQ9fxoh6/j"
    "UkHegCOGCr1lVaHp+FaUP3qAJrM4IPtXqvw40s6ZpgwK880GwGpal/ZnQGvaNHsiAABQBbh5OfpVqo6koAfF98fU1oeHv+QmfpVOL74+prQ8Of"
    "8AIUoA7bSvv/gavQ/6v8P61RsPvj6VZ/5Z0ATUsP3aSlh+7QBPQOtFFAHlf7S/jXxp4O8N2uoeDfEQtbrEvBEf78/9tP8AV1y37GmoeIdUOveJ"
    "PEPhsWtzq32Sf7XdXZ86ffHv/eRyfvI/3ckcmP8AppXuOs6RpniGw+wanYWtz6i8tfNFSwWCOvyY+uKAAnJqejpRQBXoqxVegDL8a/8AIkat/w"
    "Bgeb/0VJX8xXjy3GpfG280zUvslp9svLO3vLu8/wBTb/uo4/Nkk/6Z1/Tx4v8A+RJ1b/sDzf8AouSv5g/jB/yUrVv+2P8A6TR0Ac/qdh/Zuof8"
    "hL7Xm7FfV/8AwSE5+PvirP8A0KA/9KY6+Sz0x7Cvrn/gj7B/xfzxVn/oUIf/AEpjoA+lP+Cl3/JnPiH/AI9P+PzTf/S63rS/4JW/8mdaT/2GNS"
    "/9Karf8FNIP+MOtX/6/NI/9Lrer3/BK3P/AAx1pI/tL/mL6j/6UySf+1KAPKf+Cy//ACJPw+P/AFGLz/0VHX1N+yYT/wAMufD/AJ/5k7Tf/SaO"
    "vmf/AILLQf8AFFfD7/sMXn/oqP8A+N19Q/smz/8AGLvw/P8A1IWkf+k0dAHe/Zx7UUnkp/0E7T/yL/8AHKKAPgL9kaf/AIxw+H//AGLVp/6Kr5"
    "I/YMnP/DdPjQf9hz/0t/8AtlezfssftX/CXTfhN4U8E6l9qtLuz0aG3vPtln5MPneV+88uSOT/ANp14r+wbq3/ABmv411If8et5/a9xaf9PHmX"
    "sfl0AdR/wVP/AORi+Gv01H/0bZ17P8J5z/wpTwtz/wAy1p3/AKTR14f/AMFSZ/7S1H4f/TUf/bOva/hZPpn/AAqbQP8AiZ/8yzaf+k0dAH5Yan"
    "1u9MH/AE2/9qVreHP+Qna/SsnWfvat/wBfl5/6Nrb0j/kI2tAH0Z+yD/yVzVP+vOH/ANIZK+mmtxjoep7V8y/sgf8AJa7r/rzh/wDTbX1I/QfU"
    "0AQQdvwq9DBzVb7OfT9Ksw25z0oAsw9Pwq7B2/Co4YKkg7fhQBah6/jVm361Wh6/jV2DqKALBJyeav2ZOevaqsUHNaVnQBZ02Ct/R7HNZum2Nd"
    "TpsFAGtpsFa1nBVKzgrWhIxnPagC1ZwdqvQ9fxqGzgz0q1DB3oAkg71Zh6fhUUMHFWYYMdaAGQwVpQwc1FDBV6zg70AdL8NdKzqnWvUYfuD61y"
    "Pw00r/iX/wBp9q7ODov1FAFipKjqSHr+NAD4vvj6mtHRP+QifpVKH7w/Gruif8hE/SgDtbD74+lXof8AV/h/WqOlff8AwNXof9X+H9aAFpYfu0"
    "lWKACiiigAyT1NGSOhoooAKKKKACq46irFVx1FAFHxX/yLGrf9ek3/AKLr+YP4sf8AJSNV/wC2P/oqOv6gPE3PhjVcf8+k3/ouv5iPi/Bj4j6t"
    "n/pj/wCk0dAHJTE4bmvrr/gkBx8fvFWP+hOh/wDSlP8A7ZXyJ2avr/8A4I9wf8Xt8Vf8S3/mWof/AEpoA+mP+CmH/JnOvf8AYY0//wBLrer3/B"
    "MKc/8ADHmlf9fmo/8ApTJVL/gpX/Zo/ZL1TPbWNO/9LretL/gl5B/xidpWpf8AUYu//RtAHl3/AAWe/wCJl4I8F/8AHoP+Jvd/+io6+ov2Uf8A"
    "k134a/8AYi6R/wCk0dfLv/BZKD/iiPBf9m/9Bi7/APSaOvp39lef/jG/wBn/AKFHTv8A0mjoA77zvElFH2g+tp+dFAH5XfCb9kL4J/Ej4BeFPE"
    "mpeG7X+1bvwzD9rvP3sM3nf89fMjr5b/Z5+EepfFr4++Kvhv4b8S/ZP7I/tH7J9stPO/497ny44v8Av3X6B/sowf2b+zf4K/4+v+RZtK+P/wBg"
    "GD/jPvx//wBxz/yHff8A2ygDgP2ufAHxa+Euo+FNN+JHiT7V/pk39kf6ZczfZ/8Aj3k/1cn+r/5Z123hz9oz42fCXwVa/wDCbeCbv+wLSxht7O"
    "7vLP8Ac+T5cfl/6R/1z/7a11P/AAVhg/4mPw15/wCgj/7Z17f8J/Cunal8JvCx/wCXr/hGdO5/7do6APys1P8A4mTXZ/5/DNcf9/Ja2tBgzqVY"
    "uswc6t/1+3n/AKNkrf8ADUH/ABMrSgD6H/ZAx/wuy6/68of/AE2f/a6+qHg4HPc18r/sjf8AJbLrTfb/ANsZJK+rbmDgfWgBDCcn5u/rUkJOOv"
    "aiLrViH7tAEkP3atQ/eqKDt+FWoev40ASQwVZg6io7MHHTvVmytznp3oAs2fU/WtazgqjZwVr2cFAGlpsFdTpkGDmsjR7Guk02CgC9aWxAya5z"
    "41/tB/BT9l3wT/wmvxr+I+laBaf9Pf8Arrj/AK5x/wDLSulvrjUtN00akNNu7v8A6c7PyvOuP+/lfj9/wV5/4KKWHxr8R3H7MOo/DjU9BtPD17"
    "9m1a7vNYH23zo8furiK38y3ePkSeX5kkkcuZP3co8qgD6tP/BeP4A3+p2o8E/CXX9W0oXn+maveXsVp9nhj/5eZI/3skf/ACz/AHfl/wCq/wCu"
    "dfZXwA+OHhr42eGrvxJpmm/ZPsd59n+x/a4pv9Z/q5Y5I/8AlnJ/yz8zy5f3kf7qv5ftM1zUfDuqLqGnHFzaH/RWx0r6Z/Zp/bx8dfCXUBp+pe"
    "IdWtbO8s4tI1ci78r/AIlzyZP/AF08qRP3f/XSSgD+iuGAkDI+lSw2xIw3GK8Y/YD/AGof+GtPgFa+N9R/4+rMw2Gr3f8ArvtE0f7vzf8Atp5f"
    "mV7gIDtwDQBFD1/GtrR4KzYYcmus8B6WP7S+8KAO/wBBseela8Pb8KrWcFXofvUALRRUlAE8P3h+NaPhz/kI/jWdD94Vo6F/yEqAO20v734VYg"
    "+6T7VX0r7/AOFWx900AJViq9WKACiiigAooooAKKKKACoF6j61PVcdRQBU8Sf8i3df9ecv/ouv5jvi7x8SNVx6xf8AouOv6dda40u5z/z6Tf8A"
    "ouv5ivi8f+K21TB/54/+io/LoA5AfdNe8fsOfAnUvjZ8StV03TvEl1pQs9HhuPtdn/10/wCuleHj7pr7F/4JAQD/AIXb4q/7FuH/ANKaALP7VH"
    "7Fuo/CX4J3XjbU/i1quq/ZLyz/AOPy0/c/vLmOPzf9Z/00o/ZL/Yu8SfFn4JWnxI8NfFvVfD/2y8m/0Sz/AOXfy7mSP/n4jr6P/wCCk1uf+GS/"
    "EAP2v/j803/0ut6s/wDBM22J/ZL8Pj/Sv+PzUf8A0pkkoA+Ov24/2WNS+CfhvQPEniT4karr/wDa95Nb/wCmH/j3/df8s/Mkk/6Z1618H/8Agn"
    "fqXjf4b+H/ABIPiRqtr/a+j2l/9j+yed9n8yOOT/lpJW//AMFkbfPgnwVx/wAxi8wP+2Uf/wBsr6V/ZXsNMH7N/gDOf+RP07/0mjoA+cf+HZhH"
    "/NatV/8AIdFfaeT/ANA4UUAfAP7LH/Jt/grUv+pZtP8A0VXyJ+wPB/xn341GP+hi/wDS2Ovrr9lgH/hm/wAFcf8AMtWf/oqvkX9g3j9vvxpn/q"
    "O/+l1AG/8A8Fa4B/aHw2x/z+aj/wC2FfQ/wP4+CfgrUv8AqXNN/wCeX/Ptb188f8FcP+Qh4A/7iP8A7Z19GfBWD/iyfhT/AJe/+Kcs/wD0mjoA"
    "/JPV+uq/9fk3/o2uj8OQY1Guc1Lpqv8A1+Tf+1K6jw7Bxa/SgD3z9kf/AJLTc/S0/wDTbX1ZJByfrXyv+yBB/wAXquf+2X/ptkr6oXqfrQAzyP"
    "er0MFRwwVNQA6Dt+FXYulRwwVZig4P0oAliJz1q9ZwnpVa171o6bBmgC/a2/zVuabBiPiszT7H5jjTa6XR7H93xQBs6bBitq0gyDVKzgrWiIxj"
    "I6UAN1LStS1DTP7N/tG7tOf+Pyz/ANd/2z/+OV+aP7cX/BGD4h/Gv4zat8RfDV74W8FeGNJ8NS39pbaPpcs0OYzLmLy/9ZcXUoQXEr/u/LL+X5"
    "dzJ5slfp5CMEAHqar+M/Hngz4VeC7n4i/EXxDbaDoOlWonutW1a78qGA/7byUAfz3/ABa/4JsftV/s/wDgfR/G/wAZvBr+F9B1dZt94ySXUunz"
    "x/8ALrdeXGfIeT/lkhxHL/z0rofgNpH/AATY8LYsPjxph177YM3l4PG9zFN5nOfIgs9Ok8scnIk/ef8APP8A6aehf8FFP2xviJ/wUZ8b6rH4J1"
    "Qab8LtH1iK38Jm4tZIpdW/eeXHJ5G/9xHJ+8kyI/MP/LT/AJZRx+V/s3/sp/DnU/Dlt438beNrr7UxzaaPZLFj/W7PKfzPv/8ALOT/AKaeZ/yy"
    "8vzKAP3B/wCCafwz/Ze+HH7Llrpn7JniS61Xwreazd3H9r3nm+dcXcnlx+V+8/ef6PHHb2/mf9O//LP/AFVfQQgwD8361+W/7FvxI8E/AIWo/Z"
    "v8bf8ACP8A2u88i7s7zV7nUdN1+aP95JFcW8kn+hzxx/8ALxb/AL3yo/N8ryo5ba2/Rv4H/Hfw18bNN/4lv2q11W0/0fV9IvP9dBN/10/5aR/u"
    "5P3nl/8ALOT/AFcsckUYB2MMAOTj6V6D8O9Pzpma5HR9IK6lkGvSNCsSDkHp0oA2Ifu1ah+9UUHb8KmoAKkqPB9KkhBz070AW16CtLR+p+lZq9"
    "B9K0tG6n6UAdjpX3/wq2Pumqmlff8Awq2PumgBKsUUUAFFFFABRRRQAUUUUAV6KsVXoAqa2SdOusn/AJc5v/RdfzL/ABog/wCLjapn/pj/AOQ4"
    "o6/pp1b/AI8Lr/rzmr+Zr4x/8lHufrD/AOiqAOPH3TX2X/wR1/5LZ4q03+0v+Zbh/wDIdyn/AMcr4ygg+U19q/8ABG/J+NvjX/iZf8yfDcf+TK"
    "UAfSH/AAUoH9mfsd6/j/n707/0ug/+2VJ/wTAsT/wyXpP/AB9/8hnUf/RvmeV5lL/wUu/5M88Qf9hfTv8A0ujp/wDwTAtz/wAMl6Tx/wAxm8/9"
    "G0AeX/8ABZODHgnwYB/y+axqP/oq3/8AjlfTv7K8H/GN/gA/9Sjp3/PL/n2jr5n/AOCx0GfBPgzj/mMXf/oqOvpn9lj/AJNu8Af9ihp3/pNHQB"
    "6B54/6Bl3/AOQqKj+1D+8KKAPyO/Zv/b1+Evgn4S+H/BHiTTdf+1aRZw2/2yzs/Oh/9p15L+xD4q8NeGv22fFXjbxJqX2TStXs9X+yXd5+5+0e"
    "ZfW8kcX/AF0r6M/Z7/Z6+G/jb4A+C/Enjbw3a6rd/wBj2lx/plnF/wBtK+OP2afgf/wsj9qLxp8N9S8SaraWtp/aP2T/AJ7f6PfRx/8AtSgD2P"
    "8A4Ks32m6kvw21LTdS+12v2zUbf7X/AOAf/LSvo/4H33/Fk/BXF3/yLmm/+k0dfDX7an7POpfALUdA00eJLvVbXV7yb/Q/9T9n8uWP/Wf89P8A"
    "WV6j4Lsf2xvDXhvSdS8NeN7rVdKu9Is7i0s/7X879zJFHJH+7uI5KAPizUoP+Qrz/wAvl3/6NkrrfDn/ACE7X6Vx15Bn7V/22rsdD/5CVpQB7v"
    "8AsdQf8Xquf+2X/ptkr6xk6H618p/sdQf8X8ueP+eXT/rxkjr62k++frQBXhgqzDBUFXLPr+FAFizqxFBwfpSw9PwqzF0/CgBkUHP41raPBVa0"
    "g5P1rb02DFAF3RrCup0eCszR4K6CzgoAv2H3x9KvIcYqrbA7ulXk6CgCv4l8VeGvBPhu68a+NtStNK0rSLOa4vLy8u/3NvDH/wAta/Cj/gpp/w"
    "AFFfEv7ffxa/4RvTNRvNK+G2j3n/FPaQePt3/LOTUp/wDnpJ/zz/55x/8AbSvob/gvL/wUB/4SX/jDn4S6njSrO8huPF13/wA/E3+st7aOSP8A"
    "d/8ATST/ALZ/9NK/MQXv+z/0woA9NvPH2o/8I1d6Zp3/AD+faB/36k/9qRx/9/KydO+IuoaYLvTSM2n+f/jlx/4EVyX9rDOOPp/21qKefigD2z"
    "4fftCeJtMP/ITu83fk3Bu7MfvrfUreP/R76OT/AJZyfu4JPM/56xyV9pfsjfta6lqOp6B4k8N6l9l8Qf8AHvpH/bSK38u2kj/5aW8kkn2fy/8A"
    "llF/17R+X8N/s/8A7F3xq+P/AIM1Txt8KdOtboWfW0u7vyjcf9M43/56f9M6o+FPEXxL+APjQ+HfGulXelXVneAXlpeWfkzW+z/WeX/2zeSPzP"
    "8ApvQB/Tr+xZ8ffBP7UPwltPi14cH/ADx/tez/AHXnWE3leZ+88v8Az/rK96tAccGvxY/4IWfteDTv2obX4bjUf9E8c+dmz/5Y+d5Udx5v/XT7"
    "RJ5f/XKv2rssFOfWgCyeTmpB1FMI54p8AO/pQBYqxB3qOHr+NSQd6AHr0H0rS0b/AJCLVRXoPpV7Rup+lAHY2X3x9Ktj7pqCx6j/AD2qcdCKAJ"
    "YO34U6iigAooooAKKKKACiiigBs/f8ahqafv8AjUNAEOs/8g65/wCvSX+lfzLfFiDPjW7yf+eP/oqPzK/po1r/AJBt1/16S/8AoFfzMfFj+0v+"
    "E2u/+2P/AKLjoA5azg4b619mf8EdP+S3eNP+xPh/9Kf/AN3XydpvhvUhpp8R/wBm/wCifbPs/wBr/wCm37uT/wBF16l+yX8M/iP8SPG+v6b8JP"
    "Ev9lXVno//AB+fbLmH9z5v+r/0egD7z/4KQ5H7JfiH+0tN/wCXzTf/AEujkp//AATF/wCTTdI/6/Lz/wBG18tftC/s5/tIeCPhLeeJPiP8WrvV"
    "dK+2Wf2u0/te+m+0fvf+nj/P7un/ALNH7Of7Ufjb4S2niXwR8R/7K0q7u5vsdn/wl19af+Q7ePy/9ZQB63/wWShH/CFeCz2/ti74/wC2UdfSH7"
    "K8+P2cPAByf+RP07/0mjr88f2vfgP8bPhNpulal8WviR/atpd3k32P/ib30376P/r4j/6aV3Xw3/Y7/ai8bfDnQPEnh34s/ZdKu9HhuLOzHi7U"
    "ofs8Mkf7v93HH5cdAH6MY07/AKBv/klRXwN/wxT+1qOP+GkdV/8ACj1KigCv+yaT/wAM3+C/7TP/ADJ9p/6Kjr45/YKg/wCNiPjT+0vXXP8A0u"
    "jr7L/ZH/5Nx8F/9i3Z/wDouOvjj9hD/k//AMafXXf/AEuoA6n/AIKuQf8AEy+Gnveal/6MsK+gvg3Yj/hSXgv/ALFzTv8A0lj/APtdeDf8FXP+"
    "Qn8M/wDuJf8Aoyzr6C+C3/JE/Bf/AGKGnf8ApNHQB+Serk41Y5/5fJf/AGpXU6FP/wATG0z6Vy2sQHGrAf8ATX/2pXU+HIP+Jj+FAHvn7HP/AC"
    "Xu8/684f8A0219eD7xr5I/Yt/5L5d/9sf/AE2yV9dj7xoAZDBzViGDio4O9XoYKAHw/dq9FBwaIYR61YihGDzQBa0uD5R9K6DR4KzNMseMV0mn"
    "QYoAvw2rY6Vr2Vq2elU7SIjjbUXjD4keCvhxpn9pfEjxFa6VaXn/AB5/bLz99cf9c4/9ZJ/2zoA6CDPf0rwX9vz9tLwT+y/8FLvUdR037V/a/n"
    "W9nnyvJuPL/wBZ/rP9Z/2z8z97/wBNfKryX9uj/gq7pvwU8FXWmfDf4bfav9D/AOQv4wu/skP/AEz+z2n+suPM/wC2f/TXy/8Alp+R3xy+N/xb"
    "+P3jf/hZHxa8b3Wq6qB/on23/l3/AOmccf8Ayzjj/wDIVAGJ8ZPiPqfxN8VXfjPxEbMXd0M/ZLW08oQeZ+8/77/z/rM1ofsvfALV/jt44uLPF4"
    "dL0fyZ9V/sq0868uPMcRx20A7zSORjtGPMk/5ZVw+rWbIf7RAx83kWdp6f5/8AalfUH/BLJ9M1W+8feCNS8SHSjq2lWptbv/nhs8+OSX/tn5/m"
    "fhQBzf7aH7Kngf4EG38R/Dm+1MWYuYrHVLHxJf293d20r+Z5cguIEjjuIJPIlTzI44/LMflyf6yOQ+LfD74c+N/iRqP9m+CPDV3quf8Aj7+x2f"
    "neR/10r6N/bv8AEd94c+AXgT4T+IBfDXrq7m1DxX9svAYRKhfy4oU/5Z8SGT/tpX1l/wAEtv2etL+G/wAArXxHqGnH+3vER+0XWe//ADzioA2f"
    "+CbXww8SfBP4J2nhvxLpv2TVbu8mn/7+f+i6+tde/wCCcfwC/b38N2ngz4s+Hvsl19i/0TWLP9zeW8v/ACzljk/66f8ALOvlrxR+1z8OfhR8Wr"
    "XwT8V9NutKtRd+R/wkRPmwwf8ATScR/vI0/wCmn+rr9N/2LtJ03U/BOlfEbwV4itNVH2OX/jzvIpobj93+7/eR/wDPSTy6APln/gnv/wAG+vxH"
    "/Ym/aBtP2jPEnx+0rVdB8OXfkaRZWWkSfbL8SXMfMknmeXH5cdfppZHCZJ71c8RKF0DQfDuM8Sz5/wCuflx/+13/AO/dQQhSvPWgCxUlRjkZqS"
    "gCxD1/GpKjh6/jVigB69B9K09H6t9ay5O1bGkfxUAddZfd/E1PF1NQWX3fxqaLqaAJqKKKACiiigAooooAKKKKACq9TT9/xqGgCDXeNNusf8+c"
    "v/ouv5ovit/yUq5/z/zzr+l3XedNusf8+kv/AKLr+ar4n/8AI8XX0h/9FR0AUtP8YeJNM8FXPgjObS7vYbj/AI8/+W0f+r8uvp3/AIJIwbvjb4"
    "1AP/MuQ/8Ao23r5RADDJFfW3/BIf5fjZ40x/0LcH/pTHQB9Gf8FGYP+MS9e/6/NO/9LrepP+CZVj/xibpY1L/n8u//AEbJHU3/AAUag/4xM14f"
    "9Punf+l1vU//AATQx/wyZpX/AGGNR/8ASmST/wBqUAeWf8Ff7cf8IV4MGP8AmMXf/pNX0f8AsxwD/hQXgD/sUNO/9Jo6+fv+Cwv/ACJfgz/sMX"
    "dx/wCS1fR37LkH/GOPgD/sUNO/9Jo6AOn+z+9p+VFadFAHwB+yPPn9m/wD/wBi3Z/+iq+PP2EP+UgHjTPrrn/kO+r7D/ZG/wCTbvBf/Yn6b/6K"
    "jr49/YV/5P8A/Gv/AHHP/S6gDr/+Cq3/ACMngD/l0/5CP/o2zr6F+Cn/ACRPwZ/2Lek/+k0dfPX/AAVR/wCQl4A/7iP/AKNs6+gPhNe6d/wpLw"
    "t/2Lenf+k0dAH5K6z/AMxX/ttXW+Ff+QjXJa9/zFP+2v8A7UrrfDX/ACETQB7t+xdP/wAX8us/9Mv/AEhkr7EXqfrXx3+x1/ycdd/W0/8ATRX2"
    "ZbQcmgBsMFWrDqPqKWpLf7/40AW4AMDitLTgMdO1Urfp+Fa9n1P1oA0tNgxW1p0AIrO02DNb1rAMHn7LQBz/AMQvGOneGza6dqWpXf2q8/5BHh"
    "3w3/yEr/8A+Nx/9NPMjii/5ay14D+078TdR/Z/0v8A4Tb4kDQPhCdY/wCQR4d+G/lav4w8Tf3IrjV7j93Z/wAfmeXHJ5X/AD/V9RWXhX+zvtY8"
    "E/8AEqu9W/4/NXvP33/bWTzP9Z/0yjr5LP7Ifhn9rT41ap8Wfi2tzafCHRzNBpFpeav/AMjPNBczxyalcXHlxyfZ5I445P8Ann+8kii8uLzPtA"
    "B+cvxS8H+IvjSp+LXiXTToHgw6tN9i1eyvZNRhnmMnl/ZrCS8k8zVL+SRP3lxJ/wA8/N/1X+k15D8TbNPD2q3X/CSeCbzShtxpPhw+ZFDbxeZv"
    "/eSSRiST/lp5kn/LT95/q/8Aln9u/tm623jaTVv2pPiNqNnpXgDRdH/4RP4GeHdJX7KLe0u5PLFzaW+I/wDV2fn3kkkfHNvFHL+7l8v5S1fXtU"
    "Xw7aar4iNr4/8ABmRADd3nm/2fv/6aSf6Rp9x/0zk/dS/9NaAPJvhj8O/G3x88bjw94YsCDdHNyR/qreL1r72+F37M/gr4K+G/7TbVM3dn5M4z"
    "+6+z/upPMk/d/wDPTzK4j9kz4ufBLw34btPDem6adKtc/wCmXn/La38z/n4/+Of6r/rlXsf7R/hzxJ41+G+v+GvBI/4mt5o80H/bHy/3kVAHwV"
    "+0p8TNT+NvxcudR/tH7Va2hx9sNqIfP/56S+XH/wA9P+ecdfrN+yL4j/tPwT4f1ID7J/ocX+if88P+mVfjn8MvFX/CEeNf7S1HTP8Al87/APLC"
    "aP8A+2V+r/8AwTS1XwUfhJdfFrxt420rS9A0j/n8/wBEhP8ArPMlkkkk/wCuf/kSgD6d+Nf7Hnw2+Lfgq6+JB8S3XhXxBpGjzf8AE3/si21Gz1"
    "C0ki/eWN/b3EckckEn/tSvJf8Agkr+yB8efhJqvjIfCf8Aai15TpXhGafRrL+ybaWzN5JIiW4nj8z9587p5nlyR/8AXWsL9uH/AIKhfsgxeHNA"
    "0v4efHm311rPV86x4b8OrLL9vh9ftafu4/L/ANZ/rP8AlnX1z/wRm8V+C/G/w88Y/EnwX4kNz/b76cdJF5/x+fZPtBfzJI4/3f8ABJ+8/wCmdA"
    "H1P8DfAPxJ8E+CbXTfi18f9V8f6/8AY/8AS9XvNIttOht/+mVvb2/+rj/66eZL/wBNf9XXfwE4xnvUUVXF6j60AT1JD1/GoofvVZh6/jQBJVmH"
    "p+FRQketSZHrQA6TtWppH8VUU7Vp6Z1agDqbL7oqzD92q1l90VYoAkooooAKKKKACiiigAooooAKr1YqCb7tAFfV/wDkGXX/AF6y/wDouv5tPi"
    "d/yOmq4H/L5/7Tjk/9qV/SXq/Gm3QP/PpL/wCi6/m0+Js3/FaXWB/zx/8ASaOgDnzBhrOvrX/gkIP+L3eNcH/mW4f+3f8A0mvkswfMOa+r/wDg"
    "kjf6Zpvxv8a6lqR/5k+H/wBKaAPpj/gol/yaXrxP/P5p3/TH/l+t6f8A8Eyf+TTdA/49P+PzUf8A0pkpn/BQe+03Uv2S9UH9pWl1/pmnXH/Xf/"
    "Trern/AATP/wCTTdK/6/Lv/wBKZKAPNf8Agrx/yJngv/sM3f8A6TV9Ffsvf8m3+AP+Jb/zJ+n/APpNHXz1/wAFev8AkS/Bv/YYl/8ARUdfQv7K"
    "P/Jt/gD/ALFDTv8A0ljoA7saQMDj/wAm/wD7XRVteg+lFAH56fstf8m4eCf+xas//RVfIn7Cuf8Ahvzxrn/qO/8Afn7d+8/9p19dfs2/8m3+Cv"
    "8AsW7T/wBFR18ifsQf8pDfGn113/0toA6f/gq5/wAhbwB/3Ef/AGwrhf22vid8SPht8NfhD/whHjbVdK/4o/8A0z+x7yWz+0eXHYf8867b/grF"
    "/wAhX4bfTUv/AEZYVwv7eXg3U/Evw2+FY03/AJc/B832v/W/88rD/wCN0AfKmrf8hPVtNH/P5eW4/wC/sldh4V/5Ctp9a47XP+Ql4gP/AE+zf+"
    "jZK7Hw3/yFfxoA91/Yu/5OOuf+vOH/ANNr19l2/U18cfsW/wDJx+qfWL/0219mW8GGOaAJoO34VZig4NRw/eqxF0oAu2MHyite0g4JrNsoRjFb"
    "enQgA89qANDR7fBwPWug023989uKxP7V03w3p11qWp6la2tpaWc095efa/3NvDH+8kkkkr85v26P+CqTeM/Ez/DT4c+JLnQPCtycXpV3j13V95"
    "kHEEUe+0j/ANX+7eS3k/d/vP8AWeXQB9ZfHH9uzw3/AMJHqvwT+AWm3XinX9I/0fV/+Ec8O/2jDbzf8tP9Ikkjs7fy/wDVySSSf62PyvKkr5P+"
    "Ofwd/bG+LXhw6l8R9S+P13zDb2mkeG9X03V7O3hj/eR/8SzR7iOP93H5f/LOvky1/bV8arqVp4b8E2Gq+IbW8MVvpIu7Ox06KeZ/3cccdpHHJ/"
    "z08v8AeSS123h7/gorp3g3xv8A8IV4i/Zx0e7P2w2Bu/CB+xTeaJNn+j/ZPs32j95/y0k5/eUAR/Ef4W/EvUUvEHibwt8Srm0IgvbLV9JuYfFM"
    "BHSOSO8jjvO/+rs7iTr/ANta8lsdC8NamRqXw41UaVqpM1ubTWLuP+zb7/npbR3kn7uP/r3vP3f7z/WSV+h3wa+InwC/ay1K0Hjf4kWl1a+HdX"
    "hN3q935lp4p8MzJ/q7b+0LePzLiOSSPy/tH7uOKOSSTzbmWKOKSl+2L+wJpv8AZh8Q+JDdapm9lgvPEdnZXOo6xYf6z91dySSf8Ti3+TzP9I8u"
    "5jj/ANXL+8itpAD86LzQ9TfUrvU/BWmr4f8AFVn5ovPDn7wef/z0js/M6ycf8ecn/bPzf9VH9b/8E5/jCfjTpp8D6nkXdnZ/6IM/8sY//tcleF"
    "/F/wCDniX4ctaeGfi1qVqBd2kX/CDfEW0vJLuzv7T935cclx/y0sY8fu7jy/Ntv9VLF/rIraz+y1428Q/BP9o/SfHHjbw9dWt3e3c2k6xdXXlw"
    "w33mE2/2j5P3fmRySR+Z1/1kcv8A00kAIv8AgoZ+yv4j+HHje6+LVh4cI0nV7z/TD1+zy/8ATT/nn+8/d1U/YW8OH9q34laD+y58R/iLr1t4VI"
    "+33Ok6QP8AXyx/6uMf880/eV9o/wDBTv8AaY0zw58J9V8EfDnTgLT7HF/bBvbP/j4mkkjjjHl/884/9Z/2z8qvzZ8M+F/GrQ23xH8FafdWlrdX"
    "osBdaNcy+dBN/rPsyGP95vkj/ef9c0k/550AfrT8Df8Agnr/AMEzvi18ffEH7L3gnUdVuvFVn51vpF3d+ZeQ6fNbxR/bJfM/1ckcfmeX/wA9Pt"
    "Nfq1+yX+zn4b/ZM/Zw8K/s3+CdSurvSvCOj+R9rvLvzpribzfMkkk/66SSSSf88v3lfkx/wbo/AHxvqfxs/wCFtal9r+y6Ro93Pd3mf+Pia7kk"
    "/deZ/wA9P3nmf9/K/a+E45xjigB0XQ5q3b/f/GqhyT0q3ADv6UAWKkh6/jRUkHegBYen4VYg6VHD0/CpF6j60AW4+1amm/cP41Qj7Vf037p/Gg"
    "DpLL7v41N/y0qGy+7+JqeH71AEtFFFABRRRQAUUUUAFFFFABUE33anqvQBFqn/ACD7n/r0mr+bH4kZ/wCE11T/ALZf+ivLr+k7Uf8AkG3X1Nfz"
    "c+Pf+R01T/r8P/oqOgDnYGA4Jqjr9yp1L+0if+Xy07f9MriStcW6EZNFp4F1DxrqN3/Zo/49Pslx/wBsf9Ijk/8AadAH3d4rA1L/AIJTaBqY/w"
    "CgRpH/AGw/06OvWf8Agmh/xMv2TdJ6/wDH5ef+jZK8z8e6J/wjf/BLnS9N1L/lz8N6R/02/wBZfR16h/wTRg/4xN0n/r8vP/RtAHmv/BXr/kSv"
    "Bn/YYu//AEVHX0V+y9/ybf8AD/8A7E/T/wD0ljr50/4LF/8AIl+Fv+vy7/8ASby6+j/2Xv8Ak3H4f/8AYoaf/wCk0dAHon2TUTzv60VU/tb2P/"
    "gJLRQB+fn7Mv8Aybd4L/49f+Rbs/8A0VXyD+w3/wApEfGn/X5rv/pdX1r+zBqmnD4A+FtN07UbT/kW4f8Ajz/65eZ/38r5C/Yinz+3/wCNdTx3"
    "1zn/ALfvMoA7L/grFP8A8TL4aD/p81H/ANGWde6/CyDTf+FJ+Ff7S/6FzTf+Xz/p1t6+fP8Agq3/AMhP4ffTUf8A0bZ19B/Cyc/8KU8K/wDH1/"
    "yLmm/+k0f72gD8mbr/AJBt1/22rtfDc/8AxMrSuO1GD/j70wH/AJ7V2Phrra/WgD3j9ir/AJOQuv8Atl/6aJK+0FtQRyK+OP2LYP8AjJC6/wCv"
    "OL/0219mr0oAlh6/jViGCo4YOauw9PwoAu6bbitfTbeqOmwg9627SDgj26UAfFf/AAWW+OHxq8FfDXwv8OPhN4jtdK0rxbeXdh4i1fOJrfy7aO"
    "eO28z/AJZ+ZH5knmf635P+mlfmx8K/BviQtrvxK8FrdN/Y1kPsurNafvjqM/mW+JJJ/Mjj48+4k/69/wDtrXsP7e/7TPjT4/ftZ+NCNUtbTwt8"
    "OrPUbC1s9v7mf7PLHHJ/4EXHlx/9ckjrxzUfhP438SeCdA8R/EbUR/xN7yaf+1/F+rxWf2eGP9x5cf2ySP8A5afaJPLj/wCecdAHoHwx8MfFvS"
    "9K8QeIdM8ba9d6pa2cNh4dtLPxvFNP508v7y5kgjuP9XHbxyR/vI/9bdx/886TwhP8Wvhvpuv/ABI+JHgcXVr4csofsh1jw79kzqMkscFvHHJb"
    "xx+Z/rJJP+Wn/HvXM/Ef4WaCfC3gzwVpV/4DIsbOW/vG/wCEu02Gaa7vJU/cySXEkfmeXbwWn7z/AFf7ySt3Sfg98XPh18JbVvhPp+rG71fxJe"
    "XF5eeBLz7Z/okdtafZ/tEmnySxxx+ZJd/u5P8AnnQA74J+I/gppvhrxtY6ZpuqeF/FN54c04eEtYvvMvDoN3BqUFxJLHPZxx+X5kcf2P8AeR/8"
    "vH/XSvur9in9rfxzpniAfCr42eCRr/inV9IivtJFp4ki1G08a2kWI0udNfzJYLi7j2eXJbxyR+bHH5n7vypYpPg7UtJ8R+JPgn/aXiTwTaXWrX"
    "fjD+z/ALXo9pFp15cWlnbRySeZ5f8Ao8n7y8j/AOWf/LP/AFtdV8J/Efjj4IfCe01ddMHirw/4s8TS3F54dvbr7Jd28VpbWn+nQf8ALSzu/MnT"
    "/TLeT939nj83/lnQB9O/t0/AfxnqXw3u/iPp+pnSf+EcsjfjwNd3dt51xaSeZcXdzfx28kkdvHcH93bW8f8ArJI/N8yPzIq+Hbzwdp3xp8GZ8D"
    "G7u7sWn+h6Td/66Dy/9ZZD/np/o8dv5Un/AEz8r/VxR+X96eLvj58A/iT+zjqniL4j+JNfuvD97Z/Zx450i0ilhg8z7RHcR6vpkf8ApFnfx/aI"
    "45JLeOSLyruOX93Le+bJ8K/seTf2b+0la+GdN8N3XjS0urPUfsmk6LpMsw1eH7NOPNt4PL8yTy/+Pjy/L8yPyP8AnpHQB7P8bNd1L41/sc+Ffi"
    "RqH/H1ef8AEo8Rf9hG3jkkkl/7aRxx3H/bSua/YF+Dmq/F/wCCnjT7DqF1Z3i+I9O0bw4LK7igAlnsbu/u72R5P3ey3j0i0kP/ADzjkkMfOY6+"
    "z/ip+yd8Jh/avwk+Cfx+0nx/4f8AEVn9v1fV7Ozih+wavb/6PHcx3EfmR/vLf/lncSebLFcfvZZZfK8vX/ZH/wCCdGmfCf4LeK/gsviHVNT0nx"
    "cZoNXF3+6x9ojt5MeYnlyb5PsdvH+7kj/dXcn/AD1/dgH0F/wRY/bf8S/EX9jvQPGeqDSrTS7PWf7H1j7JZyzefqX+j+ZL9nt4/Mt5PLuLf95+"
    "8iuf3ksvly+bLc/o5oV7pviTTbXU9M1K0u7S8/487yz/ANTPX8y37Ivxz+Lf7A37ROqfANdRUeC/jNaG3tT4lcQxXH+k3lhHcyD/AFduJLiO4t"
    "5JP+Wccnmf8s6/oK/Z6+N/w38a+CdK+Lfhv4j/AOieIryG38R6P4kEdpeW+r/6uST955cfmfu5PM/695P+WsUtAHt1v0H4VaqKA5NWfI96ACHr"
    "+NWIYOKjyPWrGR60ASw/dq1D96qsP3at0AOh/wBZ/n0q3pf3DUH/AC0qxpf3D9aAOmsvu/iam/5aVDZfd/Gpv+WlAE1FV6sUAFFFA6igBy5XPH"
    "60MS2MD9a5P4rT/EVfDgPw5062urs+pjx5X/bStDwRp2p6d4a03TfEN79rvFtP9Kuj+8zN/HQBtUUHqaKACq9FFAEV7/yDhX84fjyx/wCK11T/"
    "AK+zX9Hl52+pr+cfxtB/xWuqDH/L3N/n/wAiUAc1c+lfT/8AwSbgz8a/FR/6luL/ANKa+ZZvvH/tjX0z/wAElf8AkvnjX/sWov8A0pjoA+nv26"
    "bH+zv2S9e/6/NO/wDS6Otb/gm3B/xibpeD/wAvl3/6MkrO/bvgA/ZL17P/AD+Wn/pTHWj/AME4ef2TdAwf+Xy77f8ATzJHQB5p/wAFf/8AkS/B"
    "f/Ey/wCYxL/6Kr6L/ZQg/wCMcfAH/Yoad/6TR186f8Fef+RJ8Lf9hm7/APSWvov9l2DH7OHgD28Iad/6TR0Ad79qT/oJiio/NHc0UAfi18Kv2L"
    "dS8SfDfw/438OfFvVbX7Zo8Nx/yCIv9H8z/rnJXh3wB8DfEnxJ+0j4r8N+CPiRqtpqtn/aP/E3/e+dceXc+XJ5nl1+gv7I2D+y/wCCv+xbs/8A"
    "0XXx9+w5CR+3340I/wCo6P8AyeoA479sjw58bPBI8Lf8La8a3eq/a7yb+yP9Mlm8j97b+Z/rI/8ArnXo/hOb9tjTfBOk/wBm6na/2V/Y8RtLO8"
    "vIv9T5Ufl/u/8AlpWn/wAFVbLGpeAD6Xupf+2le/fCDS/+LJ+FdU/s3/mW9O/9FR0Afk/rNx/xLrzBrtvCvS0+lcdrP/L5/wBfktdj4V6Wn+7Q"
    "B77+xbB/xf26+lp/6aLivs236/59K+N/2Lf+S+3P/bL/ANNElfaKkAcmgCWzgrSs+h+lVrOCrtpBwR7UAalr93A9TXkn/BQv9pHxF+yd+y9q3x"
    "I8FWIuNfu7uLSdIJ/1NhNOJP8ASJMf6zy445JK9gtMYPPavgX/AILsfGzw74f8F+FfgiNTIuvtkur3lqf+PseZHJb28qen7tL+P/tpHQB+eXhv"
    "xJ8R9N8ELqXhzUbu3/4SLVzcXd0bSOW7mitI0jjl+0SfvP8AWSTx/u/+eclaHjX4IeJNR8a6r4Z8NeHLq6/sk/2Qbu8/c+feR/u5/wB5JJ/y0u"
    "PMk/7aVp+ENb8Q6l8WfCnwj0vTbbStL26dYXn2T/j8/s7y/tkn+kSf6uTFxP8AvI/L/wBZWb8APCf/AAtf9oPwv4h+I5F5Z3Os/wBrasbsSS5s"
    "7fzL+7j+f/pnHPQB6F+0V8Oh42/aQ8a6l4b021utKs9X+waR9ivIv9ItLSOO0t/Lj8z/AJ529XvjD+z5448Oal4V07Tvhx4g0v8A4Rzwfp2n3e"
    "r2lnLF/pcnmX9xJ9ot/wDlp9ov/L/7Z14p4P8Aha/xF8T6X4bvrD7LdeItZs7fI/5YS3cuz/2pXbavp2qL8WPFPiH4SeItT8P/AGvWLz+yLTw5"
    "qklpD5MdzJHbxIbeT/nnHHQB6L458W/Gf4beGPBml2PxCHiG7PhuXV/EWleL7WPVogbu9vHt4v8ASPnjf7H9jlMkckZ/f9R5ZrQ/aC8Q+C/E3g"
    "vwV4H8aaZa+C/Fdr4cs9e0bVrK7l/seD+1/L1PypJJPNuNPkjjuI4/+XmL/nr5f+trN+LXxN+Nfhr4j+K/BONK8f6B4c1m80GytPGFn5155NhL"
    "9jjljv7fy7jzJI7f/np5X7ytz9rLw14I+LPxtuvDxNzoPinSPEdn4YvLLWGj+x6taWEkGnxyWl3H+7jk+z28afZ5PL/j8v8A55UAdlo2j6nqfi"
    "D4qfCjxEFtbXwL4d1GwvNI8OaVF5X2S0toLTU73Fv+8knuLjVkk/4BH/yyjHl8v4E+Eumadp/hTwX8Rn8Vt9jEnifwjrHgW6ihvLfSbsfvLGN7"
    "j7P5c/2ny5PMj8yKOSe4/wBZWj+1n8UvG/7OH/BS7xXqXhzVLj7L4uvfEVvd2Z/cm4s7/W7i0kj/AHkcsckckdpb/wCsjkj8v/tnLVrV/i58QP"
    "E3iXwXqd74a024s7OxutI8OWdreyWv2HT/ACh5NlIfKkjcRpbkjy/L8zMknlnzM0AZfw/+Pf7fXgfT7r+0dV8U+IdKN3LBaG68RS+aJ4JOnMcl"
    "necxxv8A6Rbyf9s6+r/2dv2+/wBpDU9N0o+G/BVpd2v7m/tLPxHpN9N/q5I5P9Hjt7eys45P+uckn/XKSuE+AXxo/wCEJ1LVfDniT4S+ILv7Jr"
    "EN/wD8SezjvPs/mW0f+s/0iP8A1n7z/lnX078DPjf4Ib9m/wAFaaTqtr/ZN5Db/wDE40iW0/cx30kcdAH5Afty+Pm+KvibdpPgk6BpdpeTXxtD"
    "5c01/d3mJLjUri4/vyfJ+7/1UYT/AFskvmyyfV3/AASt/wCCh/x8Hh3Xz411Lw94quvDo0eC0/tfwjbzXmofaNWs7eSK/u0j8y4tPsf2z/WeZJ"
    "5vkS/8sq8a/wCCingfw54c8bXf9m/a/sv2zUbfi0lh+zwx3P8Ao/7z/Vyfu5I/9XXj37G3jBfDfxZbwRqob+wfHSTeGfEFmP8AlvFdkR2+faO7"
    "8iT/ALZvQB/XJ8N/FZ+JHw30rxt/wjt1pX9r2cNxeaRef66wm/5aW0kcn7zzI63GwGBNfA3/AAQb/aO1Hxn8E7v4KfEnVLq61+01nUbe8vL28u"
    "bua41i0P7+S4uJPMk/0i3kjuPMk/uXFfoDklTQA7A9KkhA9KWHp+FS/Zx6H8qAIa0Iev41FD96rMMHNAE3/LOrOmfcqt/yzqzZf6sfWgDoYPuj"
    "6VYqOy+6Klh+7QAsHb8KdRRQAUUYPpRg+lADfs5zmnUUYPpQAUUUUAFV6KKAIp+v+fav50viBY/8Vrquf+fyav6LZyM9f88V/Ot8RrEDxrr+dO"
    "+yf8Tibr/11koA5m5++frXpv7Ht98bNM+I/iAfBPUv+Jr/AGNF9s/49v8AU+b/ANPH/TTy683ufvGvp7/gkrY4+NfjXP8A0LcX/pTQBH+0RP8A"
    "ts6l8JtV0z4talaXegfbLT/j8+zf6P8AvfLj8v7P/wBdP/IklXf2a779us/CbHwA1L/iQfbLv7H/AMg3/Xeb+8/4+I/+elfRf/BQGx/4xd1//r"
    "807/0ut6l/4JtaHj9l3SsaZ0vLsD/v7QB8j/ti337Wmp+GtJ/4aR1L/RDeTf2R/wAg3/XeV+8/48/+mf8Az0r0L4Z6r/wUg/4Vv4f/AOEJ8Sf8"
    "Sr+xof7Is/8AiWf6nyo/L/1n7z/V12//AAVt0nHw48KD01i8/wDSaveP2XvDn/GN/wAPyP8AoTtN/wDSaOgD5o/tv/gqN28SH/ykf/G6K+1/+E"
    "e0/t/OigD8+/2Rf+Jb+zf4Kz/0Ldp/6Lr49/YSgOm/8FDPGn113/0ujr7G/ZS/5Nx8Ff8AYt2n/ouvjn9hyDP/AAUN8aAeuu/+l1AHX/8ABWL/"
    "AJCfgD/r81H/ANtK+gvhBBj4JeFcf9C1p3/pNHXz7/wVj/5CXw2/6+9R/wDbSvoP4Kc/BLwtj/oWtO/9Jo6APyV1/pd/Wau28L9LSuE1z7t39Z"
    "q7vwtyOKAPoL9jKD/i/d3z/wA8f/TRJJX2ZB0FfGf7Ff8AycZdfSH/ANNFfasEHyj6UAWbL7n4VpabB3qlYdB9BWlZwUAaGmjkevFfij/wUM1j"
    "UP2jv2+PEWoN4jtP7LOrjSbI/a4j9ntLf/Q5B+7/AI/MtLiTy/8Ab8zvX7FfGD4jf8Km+E3iDxvpum/a9VtNHm/si0vP+Xi78v8A0eKT/pn5n+"
    "s/6ZxyV+I3xC8VnUvj940+I4H9rf6HqNhpGr3g8n/S/s32P7TJ5n/PPzJ5P3fmfvfL/wCutAC+Atc0w+ONf+JOm+GTdXX9j67f2l5q5l5hksbi"
    "OOL7PbyR+X/rI4/9ZJWl8Gde8Ri/8QJ5ekWtrZ+BNRP2Wz8I6bnzriKOwjH/AB77/wDWXccn+s/5Z15D/wALOXTVutOXxJd3X2uy+zm00c/ZIf"
    "K/55+ZJ/rP+/dZlx4080l28N/auf8Aj7u9VuZZf0koA+lPgxqvjbw18SbXxJqHgrQNV/sgTav9k1j4dabN9o+yWslwkXl/Z/8AnpHHV34P3o1D"
    "4s+H/DniT4b+C/8Aj8h/489IvtO+z/vPM/1dveRR/wDLP/nnXyy2oEn/AIl+m/ZT/wBOd55P/tOul0L4yfG7w7qe7w58R9ftfsf/AB5/a9W+1/"
    "Z/+ufmUAfYvwe0nw18Sv2gPCviPxJ8OPFNndeLvHenXF3pFpeWV59omu72PzIo/tH2KSP/AFkn/LSSrK/CvUPGv7SPgDxp4eF3q3/F1NDv9X/0"
    "WSzvLe0uNXgke+nsLjy7yO3k8yT/AEiSPyv9X+9r5N8I/tM/GrwRqFt4j03TdAumtLwagv8Aon2PyJo/3iSf6HJbV9GfswftvaV411Lwr4c8Ze"
    "DPEF5/wiWr2f8AwiPh29urXUbPzvM/cRW9xeRxyaP/AKT9n/eRyXH+s/1X/LSgDn/2o/EXhj4jeM7PxHqRtrW8+HnxT13wzeZspPO/sO7mkv8A"
    "TN/ln959nke/j8uP/lnHHXR+FPG/hz/ii/7R8SaVa/Y7z7R/purxxef+6kj/AOWnlSf8tK8D8MeKvEXiTV/H/iTxDpa2mqat46gvry0uSYpYbx"
    "7i7eROf7n7xPxr1ez/ALNI8F6aeB9s6/8AbtcUAfVfwx8Vadpvxs1/+0v+PS88N6d/pYtP3Nx5ct3H/rP9X/zzr6C/Zp+IGnad+y7dabpniO1+"
    "16R48u8Zu4v+WfiD7ZH/AOQ6+J/BNj4JPxI1Uf2ba/8AIHs7j7H9ki/56XHmf+069f8AgnB4c/4Zc8Vam2m/6XZ3mu3Fp/pcv+j+XfXEkfl/9+"
    "6AOS/4Lb+B9N1LxHqvxH8Nacf9LvIbi7vLP/U2/mW32eT95/10tP8AyJX5rXmkjUf7VOef3Nx0/wCWPlSV+jn/AAVV8D+CNM0271Lw34a0q1xp"
    "Fn9j/wC2lzfx/wDkTy46+DPhPpJ8Sx6oSf8AmEaf/wClIjoA/Qr/AIJ5/taeNtK+Nvw3+PenDxVqugeLr2zsNX8O6Obm8s/+Eoj+z29xcyWEfm"
    "+ZPHJHbyeZ5fm+Vrf/AEzr+g3w5rem+JNNtPEnhrUrS7tLzpeWf76v5If2Ev2l/GfwW8Q6/wDCTTfEC2dr4ttPIs7xbvyRBqOHj/1h/wBXHc28"
    "lxZyf89PMg8z/V8f0A/8Eev2rtO8SfDrSfBK6ZdWuk+I/wDkE3l4Y4YbfV/9XJHbx+Z5kdpcfZ5PL/6frTUIv+WkVAH3cbfnp+lSfZvapzyTin"
    "w8Hn1oAkh6/jViDvUcMHNWKAGydqsWVRuQcYNWdM4K5oA2outPpkQOelSQg7elAE9H1ooweuKAHEZ64P0rN17xHpnhrTv7S8RaiLa2HYnp/wDF"
    "1yvxS+PPgz4V6eTf6l9pue1uOPI/33/g/wBz/WV8b/HD9qbxp411Q6dpuo/arw9bX7X5MNh5n/ov/rn+8lloA+yPhr+0F8J/iwbnT/D+vm1vba"
    "78h9K1a2NpdKf+ucvMn4Cu7A5wO1fH/wCxr+xXq+neJrT49/HZLu71UMJ9A0u8PFuOdly8HSOT5/3f/LXB/ec8R/XIyBj29KAJutFFFAEU0ny9"
    "Ki3+1cxq/hbxsfiPa+I9N8S/ZdLx/plnXU0AVm+7X873xBgx448Q/wDYYu//AEZX9EL/AHTX88/xHg/4uV4hH/UyXf8A6MkoA5G5gr6m/wCCRk"
    "P/ABezxp/2LcP/AKU18zN0P0r6i/4JIwf8Xr8aD/qW4f8A0qj/APjdAH0j/wAFDLf/AIxL17/r807/ANLo6k/4JnWP/GJ2lD/p9u//AEbR/wAF"
    "Ff8AkzjXf+vzT/8A0ut6k/4JhZ/4ZO0rH/QZ1H/0b5dAHnn/AAWJg/4tz4VH/UYm/wDSb/8AeV9B/sww/wDGN3w/yf8AmUNO/wDSaOvAP+CvsH"
    "/FufCp/wCoxef9Nv8Al2r6D/Zbg/4xu8ADP/Moad/6TR0Ad79p96KP7P8Af9aKAPzg/ZRP/GN3gv8AtIn/AJFuH/0VXyD+w3B/xsO8f/8AX5rv"
    "/pdX19+yUD/wzd4L/tIf8y3af+iq+Pv2FP8AlId40+mu/wDpdHQB1f8AwVbAOp/DXI/5fNS/9G2lfQ/wUA/4Ut4W4/5lrTf/AEmjr53/AOCs/w"
    "DzIH/cS/naV9D/AAY/5It4V/7Fu0/9Jo6APyS1XpdfU/8Ao2vQ/CnOo2efQ155qn/Lz/22r0jwbb/8TKz4oA9+/Yxgx+0dd8f88v8A00V9nREh"
    "Bg9jXxt+xh/ycbq3/bH/ANNklfZNpB8ooA0YfvVdtOBn61Sh+9V205GPrQBzvxs+Dum/H7wVd+CdS8SappVpef8AH2NH/wCXjzJY/wDlp/rI45"
    "I/Mj/dyRy+VcSV+HfxS8BalqXiT4keJMD7No/+gA2Yk+xwaj9uj/dQevlx/aP/AEZ/y0r97r6yGpabdacNSu7T7ZZzW/2yz/10H7r/AFsf/TSv"
    "xG/4KS/AnwP8Af2nrr4J/CXUtV/su08OWlw1pearJefZ5Z5JJH5/78UAfNxssi7AA/4/OhrsYPDem5tF1I1gfZ8ab/Liu20eD+0dNtcjrd8f9/"
    "aAJLPwONM1H+zdS0zJ/wCXSjV/B+mab/xMv7N/0XHb/l3/AOudd/4J/wCEb1PUv+Ea8ban9l/587z/AJ95vL/5af8ATP8A56f/AGutPxV4O1Hw"
    "5qX/AAjfiTTMfa7T7RacfubiH/nrbyf6uSP/AKaR0AecQ+DdT8N/8Vrpeo/a9LsxFnV7Kz8n+z5pP9XHdx/8u/7z93/zyl/5ZS16r4s+Ben+HN"
    "M8P/GzwWbX+wPEX+j6sbP/AFMEv/PSP/nn5knl/u5P9VL/ANdK820fxJ4j+CXjf+0fDeo/Zf8AXW//ACymh1C0k/1ltcRyfu7i3k/5aRyfupa+"
    "k/hFH8N/idp1x/woDSlW21ezlXx18DhrHm3kJ8s/8TPw8kvz3af8tPsf7y9tsfu/NjzLGAc9+1d8O9Kbwv4W/ah8N4Ft8RLWGw8Xcf6nxbpMkc"
    "d3LJx/y+W89veeZn95Kbv/AJ51wemQ6aPGulabz/x5zT//ABv/AFlfVdl8Pz44/wCCZ/xU8OaiLW7u/CV5aeJrK7/68I7P/SY/+umn6hcf+Q/+"
    "edfCOj/ED/ipP+Jb4b0kZ/0f7XefafO/8h3FAH0f4VGpaj4j1bTf+E3uvsuLP/jz+zf8tIpJP+ef/XP/AL+V23wn10ab8Arsr8Wbkf6Xd/8AEo"
    "FnYzQ/6ReyRx+Z/o//AC0rwL4Pzf8ACSeI9f8AEniXTf8AiV2fnfZLSzu5P+JvdxxR+ZHJ5kn7uP8A49/+Wf8Ay0r6h8B33wlPgnSvDfiX4A+C"
    "tVtB9k76lDN+7/eR/wDHnqMf+r8v/wAh0AcB+3f4/Gp/De707TvEt3d3V5/Z2nj7Z5UM1x+8uJJPM+zxx+Z+7r5U+AOD4j8VacBz9js+D/1/Rx"
    "/+1K/Rz9qj4c/AHUv2S/Gmo+G/gn4V0nVrTR5r+01ey+3XepW80ctvH/x8Xlxcyf8ALSOvzs/ZohOpfFvVdN03H/H3ac/9xe0k/wDadAHJ+K/A"
    "B034tXfhvUNQ+y/8VHNb/bP+3mv1n/4Io/swjxH8WT4e8a+MrXQLQ3v/ABN7KxtLm01LXpfLk/1mt29xHcW/mf8ATv5cfmR2kv8Az0ik+A/2xv"
    "A2m6b8SNV8Sabz/wAVJdwZH/kSvrT/AIJGeK/i1pmpar8bNS8a6r/whXhLR9cg1fN75uPsmm/aI44/M/eW/wC8uLf95HQB/RNFANNPX/2t/wCR"
    "Ks5Xsa+Bf+CZn/BZ/wAE/tP6jpfwF+NupW1t4zFlFBpPiICOKLV5v3cfluP+WFxJJ2/1cv8A0z/1VfoFhcYPX1oAbUlGD6VJQA+KDHH+RV6ztw"
    "e/Pc1HFBkH1r5L/wCCof8AwUc0/wDY88F/8K6+HGpWf/CyPENp/oPHnRaDZyb4/wC0riP+/wDu5I7a3/5ayRv/AMsorkxgHvGr/tr/ALJ3hr4t"
    "/wDChfEPx98LWvjUXcNv/wAI2dWH2zzpI/MSLy/+enlfvPL/ANZ5fNd1N8TvBQ5/4SO1teP+XtvKr+cb4DfD/wCJXx8+P2leN/BniM2lr4S8Xw"
    "6hqvi68HnXkGoxyx3GfM/5eNSkk8uST/n2jk/651+uenftq/GrTlwPEdrdD/p6tI5f5UAfXUvxw+Emcj4kaBn0/teKvFf2iP28/hL4csP+Ef8A"
    "BfxY0C6vMYuhZ6vHLx/sSf8ALP8A66f/ALyPhYv2/vit026UfrpMv/xyobj9v74iufm0vw//AOAdz/8AHKAPDtS+KfiT4tal/wASzUfsv2v/AJ"
    "fP+un/AD5x/wDtST/yLXvv7Jnwy8OeCdS/4SL/AIVtqviDXrT/AI9PttqYbPSP+2lx+8uJ/wDppHHJ/wBdKxrn/goF8U4+FXSf+3TSJf8A45XL"
    "6/8At3/Ggn/iZ+M/s3/XppMcX/oyOgD7M8U/F4+CrBfEXjPQbuz0sMPtur2X72GxAP8ArLjIjeOPH/LT95HH/wAtCK7ocjIPbmvzp0LwH+2h+1"
    "+134K1HStUtPC+sEW+sXfjoXMOm3FpJ9/ZaSf8fn7v/lnHH5X/AD0ljr9BvC+hp4d8MaT4c06S4uxpdnFALq7OZT5cXl/P/wBNMUAXjyaKD14o"
    "oAkqvUs3T8KioAgbqfrX89/jzn4k6+f+pk1H/wAh3Mlf0IN1P1r+fzxr/wAjtr//AGMmo/8Ao2SgDkLz7wr6h/4JE/8AJbPGv/YtWn/pTXy9qP"
    "3/AMK+pv8AgkV/yX7xr/2LNp/6U0AfSP8AwUTt/wDjEvXv+vzTv/S63qz/AMEx/wDk07Sv+wzd/wDpTUf/AAUS4/ZL8UZ/5/NO/wDS63qx/wAE"
    "yP8Ak2DSv+wxqP8A6N8z/wBqUAed/wDBYD/km3hT/sMTf+k1fQ/7Ln/Jt3w//wCxO07/ANJo6+ff+CxP/JNfCf8A2F5v/Savob9l0Z/Zw8AD/q"
    "T9O/8ASaOgDucp7flRTxaEjP8Aon5UUAfmh+y9/wAkB8Fkf9C3Z2//AJCr5D/Yb/5SH+NP+vzXP/S7/wBF19g/ssT/ANpfs3+C/wDn1/4Ruz/9"
    "FV8h/sK/8pD/ABr/ANzF/wClwoA6X/grF/yFPh9/1+al/wCjLOvoL4NT/wDFlPCo/wCpR03/ANJo6+ff+Cs//IU+H/8A1+al/wCjLOvfvgr/AM"
    "kU8FH/AKlDTv8A0mjoA/Ju4g/5CvH/AC+TV6b4F/5CNp9TXmTcLquf+fyb/wBqV6t4HgxqFpmgD3T9jiD/AIyP1f8A7Zf+mz/43X2VafcH0r41"
    "/Y4g/wCMj9WP/XL/ANNFfYtp9wfjQBow/erRtPums6H71aVn0/GgC3EccY4wK/EP9vDxSvjb9sX4u+NtpBs/EX9kW2fWwjS0/wDQ7cV+0PxE8f"
    "8Ahz4S/DfVfi14kP8AomkaNNf3f/Tx5cXmeV/10/5Z/wDbSvwV8a67qfiQeIPEuqf8fer6vLf3n/XW4ufMk/8ARlAHIzQAgmux+FcDamLTTe32"
    "yGucs7AalppZa0fAV+dM1P8AtE9LM/8A2ygDqfEn/Er1PgfZPWvQPh9+0yfDfhz/AIQjxIPD/irw/wD9C54wtJfJ87/nrH5flyW8n/TS3kjqLx"
    "v4T8E/EjTf7S8M6l/ZV3/r/wDTB50Pnf8ATOSP95H/AN+68yvPhN8Sf7Uxp2m2l3/2+Rw/+jPLoA9k1O+/YV+JGP7R1Lxr4Au+5svsXijTbf8A"
    "7+SWV5H/AN/Lis2z+Dvwh07xHban8Nv22Phvdf8APiPElnrek3kE3ukdvcxxyf8AbxXJ+DP2R/jX411D+z/+EO0nS+cC68SeLdO06H6/vLjzJP"
    "8AtnX0Xon/AASZ0rTvBln4i1T9pPwvr1zeN/puk6Nq1roekaeM/wAerah+8k/7d7OSgD3vwD+0ifiH8Fde+FPxH1P4WeKvEHi7wfq3hm88ceHv"
    "iaDd38U9tJBb3Or6ZHH5kvlyeXH9st/Muf8AplL/AMs/zc8ReFJfhD40uPh58SvgldaVr2l3n+k211e3Jlg9tgk/fpJx5bx/u5PNHlyc19Or/w"
    "AEkvHHxN8SWg+FX7Un7Mdrc2YwLPw58T7n5RknzJnuI5Lid8kx4H9z/VV9G/GH/gjx8fPG3wD0vUfDHgq11Xxp4cM32S08CeLba70f/np/oclx"
    "HHeWfmf8+f8AqvNk8yLy/Ml8wA8H/ZNvvgh4Z/ZN+L3wV8aDQLWz/wCJdjxbaaR9r1I+Ir/zI5La08yTzJLezs/tEn/TST97/wA8o61NC8K/Df"
    "UtNzpv7SN1df2RZ/2h/wAkvuYf+edv/q5L2P8Ad+ZPHXkv/DNn7Y3w31L/AIVx4j/ZK+KlpdWY/wCQR/wiNzD/ANtf3cfl/wDbT/yLXsfgj9nP"
    "9sb/AIRvVtT039m7xpd/6FDp/wDofhy5m+0Q3Eskkn7uOP8A5Z/Y7f8A8CI6AMr9sPxhqfw3/ZN1/TP+Fk3WqC8tIbfP/CIx2nn/AGi5t/8Alp"
    "JeS+X/AMe//POvkb9lkt4b0208ZHP+l+MdOgtAf+eUcskn/wAcr1/9rjwx8W/Gfjjw/wDCf4teCtV8FWv2ya/vP+EkvI7O8MMEUkkkn2ST95+7"
    "jjkHmeX5XmV434dvj4b8F+FtO031huP+23+s/wDalAHqP7VPivTfEnxH1bw3pv8Ay56zd3F5j/ntJLJ/7Tkr1L9mL4++HPhN+wJ8VPhuNSz4g8"
    "deMLSwtLPH777H5Uf2iWOP/pp+7t/+mtfMN5qoP2rUdR1Hv592K+n/APgn9dfAD4T+B9V/av8AjTpdzdeILO9ng0m2HlxRaT+6/d/vJP8AlvJv"
    "/wBZ+8l5/dx/6zzAD3j9hX4D6l+y7420v4j/ABZ8OWl34q1f/j70i882b+yNOki8v7N+7/1d3JH+78z959m/7+V+6P7MPxv8NfGj4d/b9N8S/a"
    "7vST9h1bPXzo/3fmeX/wAs/M8v/wBGV/PH8Nfj18f/ANubxwNT8aahr2l+DDdm38O+BvAq3GnzeJpjkfvHST7RImP9ZL5nl/8AXL95X65/Czwn"
    "4a8FeJNK+I/wU1Lw/wCH9fs/9H+yfY/J+3xf8tLaeSP/AI+PM8v/AJaeZ+9/e/62gD7z+zr6Vahg7H8OaxvA/jHTfGemDUdP+0+l3aXg/fQf9d"
    "BXk37d/wC3Z8Jv2G/hQ3jTxwwu9evBMvhzw4t55U19Kn35ZH/5YQR9Zbg9P+unlx0AZ3/BQr9vPwT+xZ8Ox/ZhttV8e+IhNB4R8OE4+0H+O5n/"
    "AOmEf/kTH1kj/IH4YfBz42ft8fEa6+I/jfxHdWmgXmsfaPFvjjHnTatdx/fitJJP+efl+X9o/wBVF/qov9X5Vd38JP2efjX/AMFC/ixdftQ/tZ"
    "6ld/2BrA8+0tLzzLSbXof+Wcccf+s0/Tf+efl/vbn/AK5/vZPtvw34N8N+G9OtfDnhvTbS1tLOz+z2lpZfuYbeGP8A5Zxxx/6uOgDk/AfwR+HH"
    "w38E2ngrwV4atLTStI/cWlpZ2nk/+RJP3kkn/LTzJP3sta3/AAgXhwE40wc/9da7E6URyD+lE1mSAD+NAHHn4f8Ahk9dM7f8/cv/AMcqrcfD/w"
    "AOdf8ASv8AwMk/+OV2v2Ijqai0Xwx4i8Z+Irbwb4L003eqXQz9kUGKG3i/56Tv/An+YqAOS0n4WL4i8R2vgvw74currVLv/jztDdydf45P9Z+7"
    "jj/5aSV9efsz/sjeDPgDZDxJq+n2+p+KGU/atWFmD9niyB9mtB1jj56/6yTnPaOPoPgZ+z54K+CemlrA/atWuv8AkLavtPmz56Rpj/Vwf884/b"
    "/lpJ+8r0QDoSKAG5PrRViq9ABRRUdAElR1JUdAEL9T9K/n7+IJP/Caa/z/AMxm7/8ASqSv6BH6/hX8/wD41n/4rTXx3/ti7/8ASqSgDkb3/WV9"
    "Tf8ABIX/AJLb41/7FqL/ANKa+YtS4kya+nP+CRH/ACX3xr/2J1p/6Ux0AfTn/BQ7j9kvxB/y9f8AIO/9Lrf/AJ50f8Ex5/8AjEvSgR/zGLz/AN"
    "G1L/wUOg/4xL8Qf9fmnf8Apdb0f8EyP+TTNK/7DGo/+lUlAHnX/BXu4x8OPC3/AGGLv/0mjr6M/Zd/5Nw8Af8AYoad/wCk0dfO/wDwWC/5Jv4M"
    "H/UYu/8A0mr6H/ZX/wCTbvAH/YoaR/6TR0Adx9h1P/oI0VY+xf8AUN/SigD8zP2Wuf2cPBZP/QuWf/oqvj39iKf/AIz/APGvP/L54i/78/2l/w"
    "Du6+uf2UuP2cPBQP8A0Ldn/wCiq+Qv2Fp/+M//ABp/3HP/AEpoA6j/AIKxT/8AEz+H/wD1+aj/AO2lfQfwXn/4sn4K/wCxa07/AL8/Zo6+e/8A"
    "grDxqfw0x/z+aj/7Qkr3H4Of8kU8Ff8AYnad/wCk0dAH5ZfZ8Lqx/wCnyW3/APRkdeseBP8AkIW1eVwdLz/r8lr2HwJ/yEW/GgD2v9j+D+zf2k"
    "NV/wB6H/00SV9c28/JFfJX7JX/ACX7Vh/tWn/ptkr6wsepoA2bPqfrWjafdNZtn1rWi4GfagD5l/4LAePv+EJ/Y5PhwZI8W+MNOsD/ANcbfffy"
    "f9s/9E/8iV+R+ugf2YR71+iX/Bc7xVqWm/8ACtvBRObS8/te/u//ACUt4/8A24r89NdhzpvXmgCXSNL/AOJZ6ZqsIT/aWM554rqdOscabyKxNd"
    "AB/CgC74W8Y6npn/Et/tH/AEX/ANJ66n/hKieNNNcdaaKNR03+0tN5NaOgeHPEZP8AZv8AZ34D/U0AX4vEWo6Zqf8AaWn6ld2t3/z91FrGqalq"
    "X/Ey1LUbq6usf8fd5eedV298H6ZpxxqmpdKwIbjTf7TI8N6d9r4/4/L3zfJ/790Aeq/A3wf428R/a/8AhCfDn2rn/TNXvP3VnYQx/vPNuLuT93"
    "bx/wDLT/45XZaF+0Xpvw31MeCf2b/Dv/CVeKtYvPs58R6xZ/6GP+mVpaSfu5I/+ni8/wCef+qiiryHxF8VPi18W/DWl/BPTdVtF8P6QP8ATLS0"
    "8u003/Wf8fM/l/u9kf8A38ll/e/vJK7X4fT6b8ONN/s34bH/AEq8/wCQv4uvLT99P/0ygt5P+PeD/rp+9loA+wovhL+y5po0rw3+3V8bfEHxJ8"
    "aav5P/ABSPgOzjtNHsJv8AWeXH9jt/Mk8v/npH/wCQ69++Kn7DH/BODTdM/tPw343u/BV1Z2cP2wWWr/bIf+mf2iS8k+0f9s/tEdfm14P+NX/C"
    "pdN1XU/Dem/a9fu/9H/te8HnQ2EP/LTy4/8AlpJ/6K8urvhX40+NvDWpWvjbxHqZ1W7+2faLT7Z++h87/n5kj/56UAfRvjn/AIISaV41OqfFnw"
    "X+1noHhaz1bSP+JTZ3fgaSE3HmeX5ktukdx9okkk8v/WSRx/6yvhj9pH9nrxJ8APEmladqfxJ8F+ILr99cf8UH4il1H7PD+78uWTzI4/L8z/ln"
    "/wA9fLr6a/4a++I/xI1K08N6d421X/TPJ/4SLV/sn2v9z/yzijj/ANZJJJ5fl/8AXL975X+q8ztv+Fx/Dn9nH4J6r411L4UeFrq6Gr/2f4R8C3"
    "vh2X7Hf+IriOTzNX1O3uP+Pz7Hbx+Z+88yWWWS3i/560AfngYNN/tK0PiP/j1s/wDSLy0x/wAfH/TL/tpXW+Gj43+PvxI0vTf+EbutfujeY0jw"
    "j4bs7m7m/wCuUdpb/vJP+Wf+rr9IPhn8Hv2A/G3hzSz+0h+zhpOq3Vp5X2z/AIQPSNN0L7fN5X7yW7uLOOOSSSSTzJPLt/s0UX+q8qT/AFte86"
    "B+2H8AvgB4Ju/hv+xV+yd4W+G1rd8Xd5o/+uuP+vif93JJ/wBtJJKAPmf4G/8ABJD9vrxqv/CS/GvUbX4WaV9jwLPxFq0enQ/ZP+fHzLOSS4j/"
    "AOudvHH9yvWbv9gHxz8KnOp/Drwv8FvGRtGP/IH1aQavBD/131eOO4eOP/np9skrA1/4t+NvGviS61PxJqV3afbP+Pv/AIm//tOqWu2OpHUj43"
    "/ZL+JFr4q1Q/6Rq/h281eWGbzo/L/0m3kk/wDRdAHvOm/te6Z+wvpuleNfEviLX9V8ffY4R4d+F/hzxDHqMuoRXHmR2/8AyD7ySP7P5nmfu5I/"
    "9bH+682qXwr/AGO/ix+0/wDFn/hsT/goVqV1quv3l59o0jwNeeV9j0iGOX9x9ojjkkjk8v8A5Z2/+rj/AOnmX95Xz78Pfjj8N/8AhZH/AAkXxa"
    "/Zeu9K1/P+meObM/8AEysP+Wn+s/8AIn+s8qv0W/Z98f6l42+G9p4kHiS11W1u/wDkE6uNIktJriH/AJ6eXJ/n/wBG0AdT/ZTnnHf/AD/20qTy"
    "H6ZqwAhHSpvsq/3TQBVMakYx+tKLDI7/AJVifGD4t/CT9n/wV/wm3xa8bWulWn/T5/rrj/rnHHXxr8df+CmPiLxv8OrnxD+yh4d0nU/9M+z2lr"
    "rHiG2imn/6awf8s7j93/yz8zy/M/1n/PKgD7s8J+BPEXxW8Rf8IZ4LFrxzeXd0uYrCF/8Alq//ALTj/wCWv/XPzJI/qb4L/Bnwb8FfDJ0Lw8oe"
    "6usNqeq3Q/f302MGR/8A4j/lnXw5/wAEH/F/x88Z+Edf8SfET4e+KdM0HVGmnur7xaPJDahHKkY+z5kk8wGPzPMkTEf7uOv0XkQuMKcZNABk+t"
    "V6sVXoAKKKKAJKjqSo6ACo6kqOgCGTtX4B+NgD421/I/5jF3/6UyV+/j84xX4EeNIP+Ki18f8AUYvP/RslAHI38BGOf+eNfUP/AASN4/aA8agf"
    "9CfD/wClMdfM+qf9vVfTv/BJL/k4Dxr/ANidD/6Ux0AfSf8AwUIJ/wCGS9f5/wCXzTv/AEut6X/gmOT/AMMm6Tz/AMxjUf8A0qkqT/goT/yaX4"
    "g/7h3/AKXQVF/wTF/5NM0r/sM6j/6MoA4X/gr3B/xbbwp/2GJv/Savof8AZiOP2cPAB/6k7Tv/AEmjr50/4LB/8k28Kf8AYyXn/pNX0N+zdfH/"
    "AIZu8Af9ifp3/pNHQB2xnyc4ornDr2qA4DGigD89f2aoP+LBeC/bw3af+iq+P/2G5/8AjP7xqMcfbNc/9La+wP2dptO/4UF4W/s0f8y5af8Aou"
    "Ovjv8AYhP/ABn941P/AD+f27z/ANvvmf8AtOSgDrv+CrU//Ew8AHH/AEEf/bSvZ/hNPj4K+Fcf9C1af+k0deH/APBVSf5fh/8A9xL/ANG2le1/"
    "Cv8A5JP4W/7FrTv/AEmjoA/MbTOl3/n/AJa17D4Hg/49Pp/hXkfhq3P+l4H/AD2/9qV7V4R/pQB7P+yL/wAlu1Ue9p/6bXr6lgn+U18vfspQf8"
    "X/ANf+tpcf9/NNkkr6ZgBweO9AGrZ9D9K27I4GfesSz6H6VraZNk/zoA/M/wD4LeeMT/w0h4V8Nf8AQI8CQ3B/7b313J/6Ljjr5GtB/aWp2oHH"
    "SevaP+CpXjgeNv22PGvI/wCJR9k0iz/7d7GOOT/yJ5leHfDe3P8ApWpgfh/n/tnQB1EWQN3t0rnNX6ZHp3rS1LVRpowK5fWL4dAKANbQfFOmeH"
    "Ov8qu6n8cAf+QZpv8ApWf8/u683vNV49vY1e8N2OpakB/wjmmY5/4/P+WNAHQXmuDUv+Jl421Hr/y6f8sYKsWc2p+ImP8AZxu7TSh/y92Y/fT/"
    "APXP/wCOVS/4RzTNNxqfiQ/a7vsP+WP/ANsq7DqGp6nnUtS1H7Jn/v8AUAeiaFpPhzw3plqNSH/XnaWf+f3kn/TSrupa5qGpH+zQTx/y51x3hu"
    "40zU9T/szw3/pV39f/AEZJXqPhrSfDfhv/AJCOp2v2r28r/v3H/wBM6AKWm+HSedS65/z/ANs6xPiF4qHhvTf+Xs/a+tn/AMtp/wDpl/n/AJZR"
    "yV22vXx/sz+09S1L7La/8/lfOl/8TPEmqanr/jXTdUurS1tNI/0QE/8ATXy4I/8Af8yTzP8AgElAHWeB4fiP/Z3HhvX7v/l4vPsfhy5/10n/AG"
    "z/AHn/ANrr0DQPFYHxF+EWlfEm/Gl6VaXuuavdHWPN4mkl/wCufmf8sLfy6+fvA0nxX+JfjS08Nal4x166P2K7nH2vV5Zf9XbSSevyJ+7qKKw/"
    "tPS7XTim3VbO8l+12l3/AK64/ef63y/9Z5kf+r8v/pnQB+nvgj4nfDbxsbvTfBHxI0rVbq07aP8A/G5K6DyNS6jU/wDtzzX5YeIvgj8bvht410"
    "zTB4M1+01W9u8aSNHs5Pthl8vPlRxx/vI5PLkj/d19cfsofBP9vz4s3/8Awhfgv4kanql1aXv2fVvthttR07QP+mV3fyRyeZP/ANOdv5kn/PTy"
    "6APom+g/tPUv7O/4Rz7X2P8ApddT4r0nwTqX9leCdNOq+H/FVpZ/aLO7u/KhhuPL/wBZFJcRyeX/AKvzP+mv7uvXPh//AME6NR07TrTUvEfx68"
    "aXOqgQ/wClraaTDD53/Xv9j/dp/wBtP+2kle3/AAf/AGXfhv8ADfxHa+ONS1PVdV1W0H+iXmseV/rv+evlxxx/vKAOE+A37JXiPxJqWgfFv4tf"
    "8Su70i8/5BH/AB+fb4v/AI3J+7kj8z/2rX1PNPgdPwrIl1XK54qIar3OfrigDbHoRXN/Gf4xeGvgp8N9W8b+JNS/0TSLP7R/mP8A5aUmveK/7N"
    "04jUdS/wDAz/2pX58f8FF/+ChIzdfs3/APw7/wmnjPVz9gtNIs7SXybeaTy4445P8An4nk8yPy7eP/ALa0Acb8ff2vfiP+1p4kuv8AhXPjX7Jp"
    "Vpef8i7rFp5M1/D/ANNJPL8yP95+88yP91/0y/d1Y/Z2+C//AAsbxra+CdO8Oa/aa/q/7i00mztP31x/z0ljj/eRyR/9NP8Av75dd3D/AMEO/w"
    "Dgpdp+naU3jP4A2uq6mbOG4s9W8OeJLG1+weZF/qpPMuIri3nj/wBXJ5ckkUv/AD1r9f8A9hf9hTw5+x34IbTj4j1TxB4g1byrjVdV1cxZhmES"
    "RyC38uMeWntQB6H+zD8Or/4TfATwZ8OdS021tbrR/DVnY3dtadPOjiQSY/7aeZXoBzgGjGDxx6CjljgmgBKKKMH0oAr0UUUAFR1JRQAVGehqSo"
    "z0NADF6H6V+A/ijnxJquf+gxef+jJK/fheh+lfgZ41g/4qTVf+wxef+lMlAHKal97/ALfK+nP+CRX/ACW7xt/2LVp/6Nkr5nlg5Jr6d/4JLZHx"
    "/wDG2e/hu0/8h3P/ANsoA+lP+ChAH/DJev8AH/L5p3/pdBUX/BNT+zf+GTtK/sw/8xjUf/Rv/wBrq1/wUCI/4ZL1/n/l807/ANLreqv/AATU/w"
    "CTX7Q/9Rm7/wDRtAHAf8Fe5/8Ai2vhT/sZJv8A0mr274C339m/s4eCv+xP07/0mjrwT/gsRP8A8W18Kcf8zJN/6TSV7J8Bb7/iwfgD/sT9O/8A"
    "SaOgDofI96Kj/wCJd/0Ej+VFAHwZ+zfcEfADwUAf+Zbs/wD0VXyH+w+T/wAN1+NOe2r/APpTWt8N/GH7Y+m/Dfw/pvgnUf7K0r7HDb6R9s+zTf"
    "8AXPy/Mt68p+Cc3xJ/4XZr+m/DYXf/AAlVp9r/ALW/0v8A55y/6R/5E/8ARdAHsX/BU+cjUvAHH/QR/wDbSvYvhxP/AGZ8JfCv/Ey/5lvTj/5C"
    "jr5C/ax/4W1puo6Bqfxa1P8AtX7ZeTfY/wDTPO+z/wDHv5n/ALTrv/DPwP8AjZ4k8NaTqf8Awm3+iXdnaT2n2zzZof3kUf8Aq/3lAHzV4W/5ff"
    "wr1/wd/wAhO0+grzLTc6mLzUvp/wBMa9b8K/0oA9d/ZS/5L7r/AP15Wf8A6RSR19NwdBXzb+ylB/xevxB/15Wf/pFX0dAflzQBftAu0HpWvpIG"
    "3g1iWU4PWvKv2xf2wtN/Ze8EWunabi68U+IR5Hh20J5g7SXz+qR/+RZfLjoA/KD42z/8Jt8b/GnjbUj/AKJeeMNX1C0/6eIft1xJ5v8A1zrN8G"
    "50zw3af9PdZF5Pjw6NNJ/4+/Kt/wDtj/8Au617u+/szTBpxNAEfiO+yf5VyVjpXiPxJqX9nabp3+i/8vl4BWte/wBmkn/hJOg/5c/+W3/bT/nn"
    "QPGAx/ZpP2S0/wCnOzoAu6R8OvDfho/8TEf2rdf9Pn+p/wC/dTa/4w/s05/49KyItU1HUv8AkWtMHP8Ay+Xl3/qP+mskkn+rrb8OXPwl8FagNR"
    "1MXXirVc9fsX7nzv8Ap3ST/Wf9dJI6AMjRvC3xI8bc+G/Dd39lu/8Al7vP3MP/AJE/1leufC39gPxn421JT8RfE9tpdr/ELRZJZgf+2iRxp/10"
    "/eV337NFj4j+LHja11HxvqP9gaBni00c/wCl3/8A0zkuJP8AVx/9NI/+2ckf+tr6U+OP/BQPwV+zdptp8OPhNpn2rX/+hd0e88nyP+mt5J/yz/"
    "8ARsv/AJEoA8kh+GP7Jnwl8Nf8IRpvw4u/FOq/9Ai0vJLy887/AKePL/1f/fv/ALZV4N8QtJ8OeHNTuv8AkFaB/wAt/wCx7zV/J8j/ALZ3EnmV"
    "6/qE37T/AO1rIdP+IvxGuxZ3h8gWVpe/ZNOAk/5ZeR5kUcn/AG0kuJf+uldlqH/BP74Tah4JtdN+JHwS1/xUNJs+dW+G93Y2l5YQ/wDXO41GSO"
    "T/AL9/9sqAPhn4nfGn+0fDn/CE+HNRP2X/AJe7v/n4h/55R/8ATOs6Uap4L0610oafc/6J/p2rm06ed5f7uOTZ08uOST/tpcSV9N6r/wAEvvCv"
    "jhdV1T9kz9orS9UvNJtBcXfgb4jaXLpGrafB3kk48uTy5CP3myO2/wCmnP7z561/9nn9qD4b+N/+Fb+I/hN4gtteuz59nafZPN+0f9NY7iP93J"
    "H/ANNI5KAKOg/Fv+ztS/tI6baXXH/H3ef67/v5H+8r3f8AZ0+Nfjbxut3/AGZ4Ms/7Ls7yH7XqviO7+yWljLGCkB+1xx/aPP8An8uOOOOSX95/"
    "00NJ8Af2Cx8WNT2+If7L1a6I/wBM0jw5q0n2PT5v+onfx/u4/wDr3t/Mlr9H/wBnP9l/4R/BT7L4k8Zx2nijX7Pzv7Iu7O0jtNO0Hn/V2Fh+8j"
    "t/+uknmScf6zy/3dAHmn7K37DHxG+I+mWepfEfTf8AhAdAu7P/AEyz0jVpP7Y17/npF5kkfmaZaSf88/3lzJ/y1kjr7o+HPhfwV8JvDlr4J+HH"
    "h210DSrT/j0tNHtPKht/+ufl1ws/xMOP+JafstRS/FMjobqgD16bxVp5GRqIqObxQ3Qj9K8bm+KeT15+lVpviUMf8hO1x9KAPYL3xwARjj8KxN"
    "e+Jx07TDqRz9ks/wDn8Hkw2/8A10ryHxJ8cdN8Oadd6ln/AMA//RslxJ+7jr86/wBsf/gqX43+MniQ/DX4Ex3f9m2l3i68Q6QZPOmP/TH0I/57"
    "/wDfvyv9YQD9l/Df7HPxb/aL8MHx18ffitefCP4fg/aG23cdprN/B/y0Ie8/5Bkcn/PST97/ANMrf/WV3f7P37M//BEL9jr4kH49/DbxF4BtPF"
    "FlZeQNY1b4iPq5sSf9ZJGbi5ljgnk3v5kkf7yTzJPU1+Bfgz4+p4H8OaXvTVvG3im8EU95/wAJfeyXem6OccRxx+Z5l5P/ANNJPLjj/wCedzT9"
    "Y+MfiT/RNS8SeJLrVfEH/Lpafu/sen/9c7eP93H/ANs446AP6ZL/AP4Kl/sN6eA2nfHa01PLeRarpOl3N350v/POPyozk1TvP+CnP7Obajc+Hf"
    "BWpjXruz9Ly2i5/wBySTzE/wC/dfzs6b8VfG2m+Cf7MHiXHirxF/yF/Ed5eeT9g06T/V2Nv/z5wf8ALS5kj/ey+Z5X7zy/Kk+wf2A4PBHw38E/"
    "8USc/wDCReT9r8R3ln9jm17/AKZWkf8ArPskf7z95/z1kk/65RgH7EeEf27/AIXeIBnxFpWqacAOboWn2qI/9+P3n/kOvWvCni7w14000eIPDf"
    "iG01O16LdWhEoNfkT4r+Mem/Df7Kc3Xv8AY62vhd+2d4z8Grd/Gn4Ki7OqaSP+Ki8O3p87+14Y/wDnpHH/AKyT93J5ckf/AC1/dUAfrmckc9qA"
    "cV57+zF8fPBn7T3wT8P/ABr+HWpB9M8Q2PnqVuRL5Ev3JIvM/j8uQOPwr0LAJwP1oAQ8mijB9KMH0oAKr1YooAjqOpZvu1FQBD/DmvwZ8dW4/w"
    "CE11X/ALDN5/6Mkr96h90V+D3xGsf+K21//sM6j/6UyUAcLfgbDx3r6Y/4JCQf8X+8ajUh/wAy1af+RLn/AO1182ap9w/Su/8A2SPip8WvhL8S"
    "df1P4J+Cf7VurvRorf8A49JZv+Wvmf8ALOSOgD7o/wCChPP7JXiAAdb3Tf8A0ugqv/wTfnx+yXpRI6axef8Ao2vmb9oP9qf9qH4kfBPVfDXjb4"
    "T6VaaVd+T9rvLPSL6Ga38u5jkj/eSXEkf/ACzqt+zf+1D+1F8OPhL/AMI18OPhNaarpX2y8ntLy80i5m/1kvmSfvI7igD0z/gr5MT8OPCpH/QY"
    "mx/4DV7J+z0f+LBeCv8AsT9N/wDSaOvhr9s39oX4/fFvw5pWl/Fr4b6V4ftLO8muLP8A4lFzDNcTeVJH5X7yST/npXWfDj9rf9sbw18N9K07wT"
    "8E7S70q00eG3tLu88OXM32iHyv3cv7u4/650Afc2/PP9pfpHRXw7/w3Z+3R3+APhX/AMJzUv8A5MooA5b9mqyOm/ALwrz/AMy3af8Aoqvkj9ka"
    "xP8Aw3X41Gpf8/mu/wDpTJX2L+zsD/woTwrx/wAy3af+iq+Ov2S+f27PiBj/AJ/NX/8ASmSgDc/4KawY1DwX/wBfl5/6Ns691+FsH/FpfD+P+g"
    "Rpv/ouOvE/+Cmn/IQ8F/8AX5d/+2dez/C//kknhTH/AEB9N/8ASaOgD4U0awO6654/ff8AoySvW/DluP7SAJrzLQbIHT7pcf8APb/2pXqug8/Z"
    "Mn/tzoA9b/ZjsR/wtrxABz/odn/6bY694guBnGfpXhfwAt/+LteKSW/6B3H/AHDa9il1TTNN0+71HUtS+y2lnZ/aLv8A64/8tJaAI/ip8aPht8"
    "APBN38SfiR4k+yaVaf9/r+b/lnbQR/8tJP3dflL8ff2hvEv7UX7R//AAsfUj9k5+z2dn/0D7S38zy4vM/7aeZ/11uJKk/at/aT8SftIeNrv4ka"
    "iLr+yrT/AEfw5pBP/Hhaf8s/3f8Az0k/1kn/ANriryHwrqZ07+1dSP8Ay52f/kagCtYzf2lqmCf+POpNe8SHTuf+Xr/l0/6d/wD7ZUVncN4c8N"
    "/2lz9qvP8Ajz/z/wCRKyrK3H/IRx/pVACQ2zbsgVZht7DONTzd/wDTpZn/ANGSf8s6rQ3WpajqP/CN6cOf+Xu8/wCeFa95JpvhzTen6/8AkWSg"
    "Cjq9/gf2aTg/8udp+88n/rr/APbK9I+EHwr/ALS1I+JPEp+1Wv8A358//pl/0zgrj/hN4I/tPUv+El8Sf6Vaf8vf/Tx/0yrtvjJ8X28EAeC/Di"
    "D+1byzzd3dp+5/smH/AJ5Rx/8APf8A5af9M45I/wDtmAbfxo/aWPhoXXw3+ExH9qn9xd6v/wAtrDy/+WUHpJ/00/5Z/wDXT/V+d+FpvEmnf8xK"
    "7/0z/kL3ln/rrj/lp/rP9ZWJ4I8OnTtNyx5vP/IEVdZoNj/xM+Pw4oA+0PgnPpnw38FWn9mm1u7v/j41f7Z++muPM/6af6zy69i+Hv7Qvhw/ZD"
    "/omlXWP9D4/c/9s5P+WclfOGm6r/aXhzS9R0zvZw//ABuSsPUvGI0051L88UAfp54a8L/s4ftH/ZdS1H4b6D/b9p/x6avo9nFaalYTf6vzILi3"
    "/wBIt5P+uf7ysD4rfCHwbpOn/wDCPfFT9k/Qfil4W/19nrFj4SspdTt5v+nuDy4vMk/6eP8Av75f/LT4v+D37QmpaaRqenal/wDdHl/89P8App"
    "X1x8H/APgoHpum/wDEs+I/hu6yf+YvZ/vf+/nmSUAfPPxW+Dmoaa//AAm37A3xa1/w/qtp/wA078S2f+hj/pnaXEkktvHJ/wBM7jy/+mVzHXin"
    "hj/gq143VbrwX8SPhL9k8aWh+z3dneXktpDPLH/rI/Lkjkkgn/6Zyen/AGzr9cPCvjH4A/tI+HLrTfDfiTStV+2WU1veWf8AqZrfzP8Alp5cn7"
    "yvxi/bE/Z1fwP8WNW+CPxr0+7tNetb3/invEV5/rbjTvMPlyxv/wAvEHJ/9FfuvLoA6LU/+CpHxr1D/kGfBPw/a/8AX5q1zN/6Ljjribz/AIKo"
    "ftIaZqRB8FeH7T8Lmb/v3+8ry/TZtU8O6j/whXxGza3X2vyLTVv+WM//AEzk/wCecn/oz/0Zpaz4H03U+NR0v6/89regDupv+CoX7SGpDI8OeF"
    "v/AAEuZv8A24rO1n/go/8AtY7sJ410u0H/AE5+Hc/+jPMrw7XrfUvA+pf8I3qX/gX/AO1aqifPzEY+lAHeeO/j18bfix/xLfiN8SdU1S1I/wCQ"
    "R9q8qK4/347f92//AG0qro5/tL/iW6bpv2S0/wCXv7H+5/zHXL2nQ/Suj0fxJqX/ACLfgnw1dardY/585KAO3h/s3wzpvA/0v/n8qjoPiPUvEm"
    "p58E6baXV2R/x93n7mzg/7aSf6yT/yFWj4V/Zz1LUha6l8bPGtra/9Qiz8qab/AONx17t8MZ/hJ8N9S/tLw38JbXxB9k7+PLOLV4f+/f7q3/8A"
    "IclAFb9nv4OjUdS/tLwX4Juvi/4q+1/6Zd2dn9s0HSJv+eskkn+j3En/AE0uJPKr7X8K+JNO+CXgnHjbxJa+KvGt5/yF7uzvJZoYJvK/1Ul//w"
    "AvHlx/88/3VeA6x/wUl+NnxIP/AArfTf8AhH9KtLP/AKFvSPJhg/6ZRx3Eklcn4j8c6j/Zv9p+JPEl1dXf/Ln9soA9w03xjqXxI8Sar4l8S6n9"
    "qx/xL/8Apj50n7z93/zz8uPy/wDv5XS/D3xHqXw38SWniXTBd/ZMf6Z9j/54yf58z/tnXyF4D+JvjfwTqf8AaWnald/6X/x+D/nv/wBs69+0D4"
    "qf2lqWlaZ/y9av/o9AH7S/8Eurn4bD9mUeGfh4otltfEerXF1a2owBPd3Ul4+xOkaZuBiL/Vxf6v8A5Z19LkHJAr8t/wDgk78ez8Ofi3Z+CtQ1"
    "QDS/FjS6Rk/8/UZ8y1k/7+SSR/8AbxHX6kk4HHWgB2T60oJyOaSgdRQAHqaKD1NFAEdV6sUUAVT91j9a/Cf4qwf8Vtr/AB/zF7v/ANKpK/dw/d"
    "b8a/CP4t/8lI1/TM/8xe7/APSmSgDz3X8bhj/P/PSvov8A4JX6UNS+Nfin+0v+XTR7O4/8i/vP/adfOevkbgc9q+lf+CVMH/F6/GeP+hcs/wD2"
    "n/8AbKAPef2+LL/jF3xD/wBfmm/+l1vVf/gn54c03Uv2XdJ/tL/n7vP/AEbJ/wAtK0f+Cgc5/wCGXfEP/X5pv/pdHVf/AIJ4f8ml6R/196j/AO"
    "lNAHi//BWnStN0zwV4W/s3/oMXf/pL/wA9K9n/AGd/Dmm/8KB8F/8AYoab/wCk0deQf8Fef+SceFv+wxN/6SyV7j+zv/ybj4M/7E/Tf/SaOgDo"
    "/wCw9O/6Bgoq59o0z0/WigD4J/Z9wPgr4Vxx/wAU3Z/6WP8ArlXyH+yL/wAn0eND/wBPmr/+lMle+/Dr47+CfBXwn0DTdS1L/S7TR4f+XOX/AF"
    "0cXl18xfAH4gab4J/ae8VfEjxrpuq2ulXd5qP2P/iUS/8ALS58yOgDvP8AgpyDqepeAFHH+maj/wC2le2fB6+z8JfCo/6g+m/+k0dfMv7X/wAT"
    "tO+Nmo+H/wDhCOuk3l5/pn/XT7P5f/ouu/8AC/7Tem6d4I0vw3pmm3X+h2dnb3n+if8APOL/AKaUAeH6PB/xLrn/ALbV6no8HrXD6b4P1L+zgf"
    "7TtOP+uteg6D/n/P8AyzoA9K+AJx8SfFQ03TPtf/II/wBD/wBd++/s3/nnXnn/AAU4+P8Aqfw28En4Baabu11/xHZw/wBr/bLSWGa30j/rnJ/z"
    "8f6v/rl5n/TKvof9g3VG8F/G3x98RtR8anQNK8JeHP7X1bVsR+dBaW/h+S4uJP8ASI5Y/wDj3r8pPj98ePG3x++JHiD4/fEnUvtXiDxdrH9oXn"
    "2z/ntJ/q7aP/pnHH5ccf8A0yjoA4DxhqxH/EuyP9EH/kajwp4c/wCEk0671L/l07/bP+mdaOj+AMab/wAJL41xaWv/AC6Wn/xz/wCN1S8YeIdS"
    "8SD+zf7N+yaT/wBenk/9/KAMDxVqh8Sajxn7J/y6VXmveRp2mD/S7uibg575pPC1uTqX9olv9L/5dD/zw/8AtlAHSeGdKHhrS/7O00f6V/y93f"
    "8An/0XWZeQf8JL4ktPDemDH/P3eU7U9cOnJ/ZuP/ueH/nr/wBdKu/CjTNOL6rqfiNjbWlmALu7P/PH955n/A//AGo9AHSav43/AOFceGrX+zfs"
    "v9q3n/IItMf8e/8A08yf+0/+mv8A1zryqzsTqepYz/x93mPtfp/00qXxBrmoeNvEl34j1E83Z4tP+eEX/LOOP/rnHWroUCjxtan/AJ8/J/8ARV"
    "AHdw2400ZNJ4bA/tS6B9Iaj1efpz71L4KgH9nDU8/8ffX/ALZ/u6APefgNqn9o+G7vw4D/AMel59o/7+Rf/a6o+O4P+KkuxnGa5f4V+Iz4a8S3"
    "QP8Az5zf+jY//jdXdXm/tEZagDlpdc1PwTqP/EtH+i3f/H5aV2+gfFvxJpo/tLw3qd3/AJ/6Z1Sm0I+NvDf9mj/j6/5dP/jdcJo8+p+G9R/s40"
    "AfUvwl+MX9palaZ1P+ytf+2f6JeWf7n/v3/wA85P8ApnX2HpnxH+E37UPgm1+Cf7bHgq11X/nz8Rf6maCb/nr5kf8Ax5z/APTSP91/yyl/dV+Y"
    "Gm32RjuK90+A/wAff7NP/CNeNv8AS7T/AJ/P+W1v/wDa6AGftr/sJf8ACgPsmm6X42tPH3gvVxMNJ1f7J5N5YeR5f+jXcf8Aq5PL8z93cR/9NP"
    "3UcVfKlh4o1T4U6r/wjPjX7VdaATiyvCQLuw2fX/WeX/zz/wC/VfdvxN8cf8Jt9k03Tf8AS7Wzs/8An0/5bSeX5n/ouP8A7918mftQ/Cw+GdU3"
    "Lp32vSry85s/9TNBN+8/1En/ACz/AMxUAWPEng7w3428Of8AFSHFp+5+x3dn/wBNP9XJH/z0jkrxjxJ8MfEngnUv7N8SalafZLz/AI9NX/5Y3H"
    "/XP/pp/wBM69T/AGaW8Eug8NeGvjRaXf2q88i18P8AiOz/ALO1K3lkx5kSSSSfZ5PM/wCefmf61I/9X5kklen+KfAHhvUvDd3p2o6Z9q0q7/4+"
    "7P7J/wCRfL/1nmf+RYv/AEWAeA+G/Cvgg86iLrVcdf8AljD/AN+69AtPEf8AZum/2b4b+y2lp62f7muE+JHwq8R/Db/iZaav2vw//wA/f/Pv/w"
    "BMpP8A45WRZ65/aJAAoA9VPiQZ/wCJlqVWbz4uad/Zn9naZxXE6N4U00j+0vEg+lWdev8ATf8AkG6b6/5loA9W+Bx07TdNu/Emp/6VdXn/AB6W"
    "n/TGP/7ZXUa/PqPiP/iZV4poOqajpw/tHTdTr0fR/iZpmpabjH+lf8+n/wAboA17CxznNb/gvxVqXiT4laVpum/8en2yH/vzH+8/z/10rknn1L"
    "Us5r0P4T/D86dYf2lj/Srv/PlR0Ae2/EP4t6j8NvBOk+NvDmpfZbv+2NS+yXln/wAu839meZby/wDXSO4jt5P+2dfuX+x3+0jpv7UXwWs/GgUW"
    "uvWf+geIrPvBeR8P/wBs5P8AWR+xr+f79qPwPqenfs3+FPGup/a/sur+L/8AyD5ckccv/bT/AON19X/8E5v2qNT+G+o6B8SdM+I40rS/Fuj2dh"
    "4tu/3f+jzR+XHJL5dx/rPLk/eR/wDTK4/6aUAftcAoHT9aML/d/WvMx8Mvi5qXA/aO1T/wnrH/AAqX/hVnxs/6OO1T/wAJ2x/woA9Hwv8AkUYX"
    "/Irzj/hWHxu/6OM1P/wnLL/Cj/hWPxu/6OOuf/CdsqAPR8j0FGR6CvOf+FYfGn/o466/8Jy1pv8AwrD409v2jrr/AMJ2xoA9GY4Qkdlr8JvjPk"
    "/EjxAcc/2zef8AoySv2QPwy+NYQk/tHXnuP+Ectq/HD4sn/i5HiDr/AMfs1x/5FoA8w1j+lfSP/BK7/ktfir/sE2f/AKNkr538TQf8S7Pqa97/"
    "AOCXOuDTfiz405/5hFmf/IlAH0h/wUHn/wCMX9f/AOvzTv8A0tt6zv8AgnVPn9l7Sv8Ar9vP/RlQ/t9eJNN/4Zf1/Tv7S/5fNO/9LY//AI3WN+"
    "wR4x8N6Z+zfaf2nqVpaf8AE5vP+XyL/np/00oA4f8A4K5/8k38Lf8AYYm/9Jq92/Zun/4x/wDABz/zJ2m/+k0dfN//AAVV8U6d4k+Gnhb+ztRt"
    "Lr/ibzf8vcU3/Lt/0zr3X9nXxx4a039n/wAAaZqWpWn+h+D9N/5fIv8An2j/AOen/XOgD0X7bp3/AEE7X8qKyP8AhOPDP/Q8j/wMiooA/BiZv+"
    "oof/AOq0BB7n8q1rPwb8R9S40zw1z/ANPh/wDjcclXtI+CnxH1Dn+0dKtP+nTMk3/xugDIs4AOMce9dBptuQOn5V1Hhb9jz42akdv9pfhZ+HPJ"
    "/wDIkkkkde0fDb/gk38f/G3Xw1q32Xtef2vbf+i7O3kkoA8X024JGBW/4bhwLUY47f8AfqvsHwR/wQk8RakbX/hJNU14Y6/Y9Jvpv/Sy4sq9y+"
    "Ff/BGL4JfDjUbXUtR+Cuv+ILv11jxbFocP/fvT/tMn/kSgD86fj58TdN+AP7L/AMVPE3iXULq1uvHOkWfhnSfsf+uuPt+ifY5Iv+mn+j+fJ/2z"
    "/wCmtfmrP4x1HP8AxTenfZR/y6Xf/L5/38/5Z/8AbOvpz/grB8StR/ag/bV8UeCfgl8ONL0rwD8MLubwz4fs/Dt1JLpont/3d/ei4kSM3LyToD"
    "5vEvlpB6Zr510fw5/Zvf8A0rNABaWOp5/tPU9T+13X/T5/7TqPXAdO0zg49T/7Tq7q+t6Z4bOdT1L8+1cTr3jg6rydN+n/AO7oAg1Kf+0ucV1v"
    "hvSdM8N6b/xUmmfarv8A58//AGr5n/LOub8Hw6lj+0gf/ueovEuuf8w3Tf8At8u/+figBIMalqR1HTsc3nGf8/6uPy/M/wC2dM8WauNN0618Fa"
    "auNKszkYP/AB/zf89JP/jdavw40r+09T/s7H+lWdn9o/8Ajn/tOrPxC8Oakfsh/s37JQBxsfCdf85rf8Of8SzU7XUu3tWLLB/Zo/sxqfDfbT60"
    "Adv4klGp/ZdNB5vK6gTH/kGD864rwfquNS/tPUtR/wCvMVvxz9MDj6/+i6AOk8MjPiW15H5V2V7BjH1rhNN8YeHPDfJ/0u79f/tla0Hj9f8AoF"
    "/6L7UAaU1+dMP9pgVpa94VPxb0z/hJPDR/4qC0/wCXP/n/AIf/AI//AOjf+/dYF5rem6lpn/EsqLwd4x/4RvU/+Jl6UAM8N3wxkit3TZznnvXV"
    "eK/AGn/EjTT8SfBB/wCJr9j+0XlnZj/kLw/8tJY4/wDn7j/8i/8AXX/WcdaX/wDammf2nnFAHtnww8SaZqWmf2Zqf/H16/8APxXNfEE/8LI8E+"
    "NCTm7s7y7+yf8AbOPzI/8AyJXE6Z4qOmj/AIlw/wBKzRB4w/szw3qumAj/AEv/AJdP+e80lAHz98avDGnpqlnqumHnVbT7QCO3P/xv/wBF17P8"
    "Cv2mdS8SacT411D/AI9LSH/hI7sf64RR/u477/0XHJ/008v/AJ6fu4/hB4b074kfFrw+xYXdrpOsaRoOk/8APG48sx/aJf8AcrnviF8JNS+AXx"
    "I/tLw4Ptdr+++yWd5/y8Qyfu5LaT/rpH/6MoA+rPCnwk03xJ4ltPDepanaf8I/d/6PrA/6Yyf58v8A6ZeZ5v8Az1qL9qb/AIJJaj8ONN/4Wz8A"
    "fDX9v+HzZ/aLzSP7XuYdSsP+ufl+Z9oj/wCmfl+b/wBdP9bXkXgL9pvxH8FjZj+zR4y8F/Y4RaH7WYdR0+H/AJ9vM/1dxHH/AKvy5P3n/LLzIq"
    "+7f2TP2yvBPiP4cWq6dqeqarpV3539kXmr2gN5/o/l+ZbTxxyS+XPb+ZH5n/PWK4t5f9b5lAH5tyWXgdfDlpqTah8QNBtM86vZm21y0/7+f6FJ"
    "H/38pdL+FkfibVT/AMIL8atB1Mbv9JXWtJvbW9x7pHFcRyf9s7iSvub9qD9nb4a/FnxHd/Ef4L6h/wAIZ4pvAZxq9mZRaXx7G7SP/Wc/8vEf7z"
    "r5n2n/AFdfHk0r/Db4jnw58fvgiTr1mRPmzu5dOmni/wCfmOS3/wBHuI/+mnl/9taALtn+y74j8Sm103w38fvBd0Lv/j0tNY+3aR9o/wCucklv"
    "Jb/+RKxPip+z18fv2b2tdS+Nnwk1XQLS8/49NX/dzWdx/wBc7u3kkt/M/wC2leo+G/G/7N2p6d/wjnhrTv7K/wCoR4w828s7ib/nr9r+0eZbyf"
    "8AgNF/z1rqPAXx+8SfDbTdV8FeCPi3deCrW8s/s934R8YaR/bug38X/PK4j8uTy4/+3eT/AK60Acb+z38TfhKePjZ43urW7tP+POzvNIuZobiH"
    "/ppJHHJX1r+zf4U8NfH7xxa+G/BPiS1u7X/j4u7uz/59P+2f+r/5518l6R+zpqXjbxJqviXTR4ftdKtP9INp8N7yLV5rj/p2s7DzPtFv/wAtP+"
    "Pjy4ov+/cVa0X7W3xZ+Engi6+CX7N+m/8ACq9B+2f8Te0s/wB9r2r3f/Pzf38kfmSSf9M7eO2ii/55UAfen7fmhj4kfDb/AIZd+HGmfavGl39k"
    "uLPSNH/1Onwx3Mcnmzyf6uzgjjjk/eSf9cv9bXdfso/svad8Afhv4f8ADniTUrTVdVtLP/S/sf76H7ZJ/rPL8yP/ANp18Ufsa/tp6b8FvhMdP+"
    "JXx9tATrN5qBs7vRr3ULy4mk8v/Sbue2j/ANIf/r4kkki/8hR3fiR/wUR8S/EnUf7N039rLwVpVr3tLP8AtLQpv/Ji3/8AbigD9j/+CaH7cOmf"
    "EmbVf2T/AIkan/xVPgcy2+kXW1/+Jvp0Z2Rn3njj8vzP+mZQ/wDPTH2MNa00EnFzz/06S1/Nt+w5+1N4l8O/t0/DbUdP1O1utVtPGEP+maNq9t"
    "dw3+myfu7v95bySR/6vz/M8ySv3Suv+Cif7H+l6iNN8QfGvSNMu84OdYtpvIHrJ9nkk8tP+mklAHvP9v6b6XP/AICS0f2zp3pdf+AstUft2nak"
    "M7+1S8/36ALP9raX/dFH9u6Z21IflVLnrv8A0o84+v6UATPqmmlTjUc+1fiH8Zx/xcfxAPS8mr9sJ7/jPtX4n/Hf/iWfEjVdSJ/4+6APMvE5/w"
    "CJca4qfXRpmp/2lpv/AB9/9Od5L/8AHK1viF4p/s0/2YBx/wAvea8u1DXCDtA+poA2ta8X6pqf2vTf7Su/sn/Pp/a9z5P/AKMrNs/G3ibTedM1"
    "K7tP+vO8uf8A45WDN4g56frVGXWM8YoA6DxJ4z8Q+JP+Rk8Sarqv/Pp9s1aWb/0ZJUcPjrxDpo/s3TvEmqWv+p/487uWGuam1H/Zpn9o/wCyaA"
    "On/wCFg+NP+h31/wD8KK5orlvth/u/rFRQB+yGg/8ABLf4SfDj/iZeI/BOk3X/AE93fxQ+xw/+Q9KjrqdO0L9kz4Scf8K4+Bd1/wBhj4oy6jN/"
    "5E06SvhH4zf8FX/DfgjUrof8Laute1X/AJ8/DdpY3n/bOS4uP3cf/fySX/plXyF+0V/wU7/ai+P2mf8ACNab4jHhXSv+Xz+x/K+2f9cpLuOOOT"
    "y/+ufl0AfrP+2j/wAFEPE3wT+CV0P2PPBfw/0vxXaXdpP9s0a6iu4be083zLiKOOTT7aOT/P8Ay1r550H/AIOb/G3gnTrvTfjb8EvAGq6r9j/0"
    "S88OeOfJ/ff8s/tFp/pMn/fuSvySn0vUvG2o41L/AEu6/wCXy7vP33/oyr0Pw600nGpgXV3/AM+dnQB9neJP+Dmv9ufTdLutN03xFpV39rMx/t"
    "f/AIRKLzrfzP8AlnH5nlx7I/8AppbyV4f43/4Lpf8ABR74km60/U/2jvGhtbrodH1f+zsf+CuO2kryabwPpencabp1ran/AK9P/alRy+HMcZH0"
    "oA4zU/ir8SDpv9m+G/BH9lWn/Tn5kP8A2y/eSf6uuT1j/hZGcaj9k0r6XkUX/ov95Xqs3hXTj/zDPrzST2Phzw3zqQtbX3/1M1AHk1n4A1LUv+"
    "Qkuq3fHH/Eo8n/AMiXFaP/AArrUtNGP+Ebu8/9Pmrxf+0663WPH2m9NN1L7X/1+f8Axv8A1lc5eeHPEviT/iZ6lqWq897LSJf/ACHQBy2v+JBp"
    "p/szTR9l/wCfv/RPJrGsoBqQziu5h+DvY+G9V/8AAOqOp+ANR00f8gy6tPrZ+TQBzmna5qfhrxJaeJNNGbqzu/8APmV9FQ2/hvxrptrqmm6b/o"
    "mr2f2g2f8A7S/7Z/6uvGbP4WeJNSP/ABMm+yWv/P5/9rr1D4YQDw14JOmHUj9ks7yb/S/9T+5/1n7zzP8AV/vPM/7+UAct47+Emf8AiZeG/wDw"
    "E/8AjdcLaeG9R1HTf+Jbpt0fW8/5Y/8AbST/AJZ16B42+O2mabqf9meCMXWT/wAfZ/1P/bOP/WSV5l4r8SeJPEepf2j4k1K6use37m3/AOuaf6"
    "uOgBNN10ab/wDWrcstWIA1LTf+3vmuOYgjr9a0tN0PxIBjTtO7cf8ALH/v3/z0oAih1XUs/StvRvHraf8A8hEZr2v4S/8ABMX9q/4taf8A8JHq"
    "vgb/AIRbSR+/N34ibyZjFn/lnaf8fEn/AH7j/wCule0/Df8A4JmfsveCdO/4ST40+NPEGvjP/Hnn+zoZ5v8Anl5cfmSf+RKAPk7+3NO1P/iZeG"
    "/Ev2W7/wCfO8/cw/8AkT93T/DGoeOPE+B4a8E69qt1a8Xf2TSpLv8A9Aj4r7t8OeEP2X/BK7fBP7JvgvGf+PvWLT+0ZoP+2lxXLftc/FT/AIqT"
    "Sv7O1L7LpR0f/kEWd55NnbzRyyeZ+7j/ANX/AMs6APmLRV/ai8DOfEXh34TeM9KNmftBvF0q5iH/AF12PHRq/wAR9N+LP/FR6dqH/CP+ICP9Mt"
    "P3cNnf/wDTWP8A593/APIUv/TKuol+IHiTxKbrTfDem3V1d/8APpo9pJNN/wCQ/wB5Xldl8B/jZpptf7S+HVzaEdBe3cdnMf8AtncSRyUAbR1T"
    "xN4bA/tPTrq0yMj+17SSI3H/AFz/AOen/XSOsPxh4s1LTf8AmJZu7z/v9BS6N4+1TwV9r00agTj9x9j+1/ufO/55/wDPOjw5B4l8b/8ANOLTVe"
    "P+YP4S/fed/wBdLOOgDuv2b9bPgnU/+Ej2/wCi+EdHmuLP/p41GSL93FXQfGDVf7S8SWmm6lqX2v8AsjR4f9L/AM/9c68q8c/8JH4Ja1XU/Dl1"
    "pXPn2mkXf2mHz5v+evl3H7z/ALaSVv8Awr8ZeB/G3iW08N/Fnw5d2t1eXn+iXdnd/uZ/M/5ZTxyf6v8A66R0AZPw+8U51C68Fam32XS7y8muNI"
    "u7z9zCPM/1kX7z/wAh/wDTXzP+elenfAvxfqn7MniO88M+NbDXbPS7q7jurS8ttJkEVjNGHzJs8uWORJInTzP3cn+rg7x+ZH6Defs9eCBnTtS8"
    "FfZbv/p8s/J/9GR16j8O/wDgnR8fv+EbtPEvgr416r4L8P8A2Pz7SzvLOXV7O/8A+ef2e08z93H/ANNP3dAGv4D/AGn/AAV4mFr/AGZ4j0DVLq"
    "7u+bSz8W6bFNP/ANu+oXFt/wB+7eS5kq1+0FrPwV8TeCD4c/ag+CnxJtLQL9otNXvPA97D/Z83/PzHd+X5af8Afzy/+etef+K/gb+3R4cU6fqf"
    "woHim1H/AB56r4cvLaaE/wDbOSSO4j/8B62/hl4k/bG+G2pWngnw14J1W7+2H/kUdYu/Omnm/wCnSOO48yOT/nrH+8il/wCeVAHg19+y8NS03+"
    "1Pgl8V7TxTa/8ALpZ3lpLaXk8P/XSPzbOT/wACI/8ArlWZqXwy8bfDjTBqXxa8N+KvD9pZ/wDMW/4RGW8s7j/t4+0R2/8A37kkr7L8bf8ABLr4"
    "3af4Zu/j344/Yo8U/DWz+yfaNW1fwf4hyfK/56T6bb/vI/8Arp5f7r/lrXi/xHvPG3w303/hNf2bfGt14g1Wz/0e80jSLzV7ya4h/wCfnzLeTy"
    "5PL/5aR/8ATSgDxzRvEnwU1Pj/AITjxBdDH/Qo2P8A8sa62Hw58N/GwtB4k+LXjQ6V/qP7I/0aGH/pn+7kvLmP/v3HXp/wvt5fiJpZ1H9oz4L+"
    "F1uiQbUD4M6nq0pif7/mT3lxF5f/AGzkr1z4TeAvhK3ia18O/CfUzoGrXn/HpaaP8DIoZrj/AKZW9xeXEccf/gRQB5L4a/ZgbxtptrqH7N3wTO"
    "LT/j81a8tIrya4m/6Z3dx/x7/9c7fy6k1jw5+2N8ODnxJpnj/S/sf/AC+Wd5czWfk/9fFvJLH/AORK+kvFX7Hf/BXb4kfatR/ZM8GeNdK0q8u4"
    "jaf8JJq0WnTeV5f7ySeO4j8uN5JP9XHHJcR/9NKn0H/glT/wXk+JGnXXgn4t6paWul3ln9nvLy81fSLvP/LP/ln+88z/ALaUAfPHgL9u79rTw3"
    "qX9m+G/j94g1X00jWPK1eH/rl5dx5tfT/wM/bX+CvxJ8E3eo/tQeI/AHgHxV9t8j7Hd+IotIhv4fLj/wBJjs7i48y3/wDtdN8If8GtH7UPiLOm"
    "fEn9rS6+yH/j7tLPVvseIf8ArnHHc+Z/5DrsvBH/AAbW/sm6bqI0zU/2xdfuv9M8i7tLTSJJsf8AbfzI/wD0XQB6R8O/+ClvwS+Enhy28F/DP9"
    "uTwBpWlaXZw2OlaSPiHpE0NjFEPLjjSO4k8tEwB/q63tU/4KqeIdV020Hgz4+2niH7Z/x6f8I54i0P/wBGeZHWnpP/AAbCfsA+GB/aep+N/iTd"
    "H/pzvIof/beSSruk/sa/8E4vgH9q+Cvhz4AeC/FFtdt/xNrzx3pNtqOpX837zzI5PtFv+78uP/nnQB85/HD/AIKt/Fcad/whXw1+It1qniu8/w"
    "CYRefEWL/QP3X+t8u3kk8z/rnHcV9yf8EpPgJ8X1/Y70vxL+1B8VPFR8aeLdX1HXdWaz8Xy58qSUR2/mSRyeXI/wBngj/1f7v56m+AX/BL/wDY"
    "GPhy18R/Dj4S+H7XgYP/AArrw3Fn93/f/sr95XvUfwxsPhN4bG3426/pWk2dp/olmbLRIYbeL/nnHHHp9AEfi/wR4K8OaX/aGqfETxp9lPW7Hi"
    "K+Ig8v955kkkcnlxx/9dK/Ef8AaV+NGnf8Jtd/8IT4a8Varnzv+YRL/n/95X3v+19+0z4l+IwufA/w5/aA8aaXpYH/AB+WdppHnX//AH80/wDd"
    "x/8AXOviPxL8FhqOpXRHxa8VfUf2R/8AK6gD5V8VeMfEYH9pal4J8af+E7JWB/aviTGB8OfFX/hOXP8A8br6ym+B46/8Lt8af+Aeif8AyvqXR/"
    "2c9M1Ln/hbXir/ALfLPTf/AJDoA+Of7Tv/APoSfFf/AITt1/8AG6r/ANtX4/5lrxB/4KLr/wCN19wQ/sv6Z/0UvxV/4Bab/wDI9WbP9mYdNN+L"
    "viq1/wCnzGm//IdAHwhDrbAf8i3qv/gnuf8A43UsN9qvX/hHNV/8E8v/AMbr7um/Zy5/5Kz40/8AKb/8h0f8M1ab/wBFH8U/lpv/AMh0AfDX25"
    "+/hrVf/BNL/wDG6K+6f+Ga/Dn/AEUbxB/5Tf8A5DooA/IIftB6X/zEfDg/8DIv/jdRz/G/w1qX/IO+1Wv/AG+R/wDtSPy6/Si8+Ffhvr/Ztp+t"
    "YGu/s2fCXxGQdR8FeH7r3vNItpv/AEZQB+f6/FfB/wCRg8U231vLGb/23p83xd8N/wDQw+Kv+3y7tv8A43X2PrP7DXwC1P8A5p1pVp/15/uf/R"
    "dZE/7Afwm00f8AEt0+7tfw87/0Z5lAHy5p3x38NH/kJald3Xr/AKv/ADJXUjxh4b1I/wDEt1G7uuf+XOzlm/8Aade0a/8A8E/NO1JdumfEfX/+"
    "vS70iymh/wDRcdUdB/4J3eNjqf8AZngn4lXd1df8+ekeEYpv/IcdAHjGpatpvX/hG/EF3jv/AKn/ANGSVmfaPDendfhtqufrbTf+1K+x/AX/AA"
    "Ri/ai8SN/xU3xrtNAtAel74djmvD/2zj8yOP8A7aSV7R4c/wCCMPw28N6b/aepeNPGnim79ru2hh/7929vHJ/5EoA/OOzmwM/2dd6AD/y93mkf"
    "+RI44/3kkf8A1zjrsLLwN8FPEenD/i9d3aXXf/Q5f/Ikckcfl19dfHn/AIJl+G9U03+ztN8SeKtAu/8AX2d3rHiK51GG4m/6afbJPM/79yR18s"
    "/EL9mz41/DY/8AEy8N2mv2v/P34Pu/Om/66/Z5PLk/79+ZQBxviT4H/EjTv+ZautVtP+nPV/8AX/8Afy4rh/EviDUPhsgfUPgzrulkn/j5uwkM"
    "Q/7aRivTvh/441HTdT/szTNS/wC5cu/Nhm/7ZxyfvPMr2zQfhV8W/GvPhv4A+P7rI/4/Lzw7c2dn/wB/7yOO3k/7+UAfEcvx/wBR3f8AEu8NaV"
    "a88/bP33/xuuX1jxVqfiMY8R+I/wDt0+1/uf8Av3/q6/THQf8Agn5+0B4l/wCabaBpX/TprGr/AL7/AMk7e5j/APIla8P/AATp+P2m9fBHhUf9"
    "efi7/wCOW8dAH5taPYjP/Et8N/a8Dp/ZHnf+i604fDeo4J/4VJqvP/PnaXPFfokv7Bfx/wCmmfCW6+v/AAsWL/2ncVal/YJ/aQ00Z03w19k9fs"
    "fi7zqAPztsfhy2oeJdJsdR+E3iCztbq9hgu727tZfJETy/9NI/Lr2P9j/9tr4cfALUf7Q+I/7N+gXV1aWc39k+IvDdp/xOID/zzkkuPM8z/nn+"
    "78vyq+k/Ev8AwT9/aj1LTf8AiWeG7v7V/wBPnxFufJ/8B4/3cleHQf8ABFz9rQ/8hPUc4/589Iil/wDbiOgB+r/8FkPirqXiE6i3wQ8Gf2Eelr"
    "d3t9/aPk/9fcdx5fmf9u/lf9Mq6zwB+1Z4I/aNB03wzqgttfu/+ZT1cR/bPO/6d5P9Xef9s/Ll/wCmVUrH/giF8Rg2fE1/rze1p/ZsP/o+5kro"
    "9K/4IZBmLaiuv3PoB4j0Tj/vigDWi8HfEgfa/wCzPhtr/wBLzSJYf/Iklc18WvgF4k1HTdK8S/Ej4ceILvSrS8/0u80ez+2fuf8Alp+8j8yPy/"
    "8AV19R/Cb/AIJ0/tjad4ctfDnw4+Nnxd+y/wDLpjxD9rgt/wDrnJcWUnl/9s5K7Xw3/wAE3P2xfER/tFvi58c7onpd2fjrUvJP/fuz8uP/ALZ0"
    "AfJnw9t/gD/Ztp4a8OfEjX/AGlf8vekf8I5F/pH/AE08zzJP3n/TSSOSvo/4KaH8JdN03+zvhJ4J0q6tPsf/AB96NZxTXlx/00kkk/eSf9dJJK"
    "9l8Mf8EWf2pvEBzqPx9+Ltr/vfHDVo/wCcldz4b/4IL+JeviTxra+IPtX/AB+WfjvxbqWuQ/8AgPeRyR0AfE/7SvwX/Zw+LOo/2jqXwT1b4ka/"
    "aD/RNI0f9z5E3/T5fx/8edv/ANM/M83/AJ5RSV57oH/BPTVPGdgJfG3xG17S2H/Hp4d8Cr/Ymj2UX/POC3T55P8ArpJJ5ktfrp4O/wCCQ+peHB"
    "aaf/wmulWlpaf8udnaS+Tb/wDXOP8Adx/+Q66LUf2X/ht+zkbUabplrqmq3ZH/ABONY8O+bFB/0yjk8uSPf/rP3cf72gD8h/BH/BEb4J+JNSx4"
    "b8NeKtVu/wDl7+x3ks3+s/6516z4U/4NtdM8S/8AIQ+Cnik2npd6v9k/8h3ElfrN8MbP4sfEjw3aeJNNF34MtRZ8aReWclpMP4/MktP+Wf8A37"
    "r0vQvCvxJ0znUvGtrd/wDcJ/8AtlAH5b/Db/g3p1LwStrpfhnw3qmlWlof9EtP+FtatDF/3xb3Hl1694V/4IOabz/wkepaBaf+BOr/APpZX6I2"
    "NrqKD/iYm1P/AF6WpiqbdgdfxoA+LtC/4Ih/sJeCNM/tPxv8KLPxTc2n782ln4dtrMf9s47ePzP/ACJXW+Gfhx+x18EtPz8E/hRa+C7q07WYkh"
    "x5f+sikj8yvpDxVokHifTW0pv7UtRdHm70i7+yTf8AfyOTzK8u1v8AYN+AHjLUf7T8b2HijXblR/op1nxdcy/Z/wDrnJHJ5goAxPCGveNdN+1e"
    "MvEXh3VP7LtLyaeztLO086a48yX935ccf8H/AMcrY8FeMvgH8RtTutO8SeCfCtt9kP8Aon9r2dt51x/z0/dyR1tab+x78ANN+ynTvD2qWv2Pp9"
    "k8R6lD/wC3H7yvSbLTNK0xP7P05gBQByx+BHwTyR/wpPwX/wCE7bf/AButrQvA/hrw2P8Aim/DelaV/wBednFD/wCi60vt2nE/8hC0qXz/AGoA"
    "j8pvU/nWRoXiVtS1L+zv+EK1W1/13+l3nl+T/wCjK28j1FRSwgnNAHJfEb4rjwXqI07TfDf2o/8AP2f9UKi8OfHfwZqRtRqQubW5u/3H/PaGD9"
    "55f+srxj4nfs7fGrUfEeqH4T+C9A+y3fNodW1Y487/AJ6eX5f7vy/+mdVfhj+yr+1D4b8O2q+NvGXhfVNUz595eWd1cwjzv+maeX+7oA+qb3T9"
    "O1PTRp2o4uvqK8+uP2Tv2dH8R/8ACSah8FtAubsdDd6REfI/658V22nE6Xptr/aOofaroWf+lXfr/wBNK8a+Pn7W/hzwSP7N8OfZbu7P/fn/AO"
    "2UAdx46+K3w4+A3hvA+yWmBi00izEcP/kOP/V18QftBftM/Ef416l/xMRaWulf8ulpZ6vc8/8AkOs3xl4w8S+NdS/tPUtS/wBLNcne2IzjHHrm"
    "gDA0yybxHqX9mal400rQP+nzWLzyYbj/AKZ/5/551xOsZ/tK70zTPEVpqv8A0+Wf+puP+uddzrGlt/kVwmpQt6fpQBniBRxW/wCFbJSeP5ViYb"
    "0NdF4JmPoaAN2HTz/0D7v/AMh1Zg0hv+fC7qz5fvV2G1031oAzv7NH961/Kj+zR/etfyrahj/6if8A5N1FNH8vWgDF+x6l/ftPzorW3t/0FP8A"
    "0XRQB4LN4a/6hX/kpS/8Iof+Qb/ZVp/4CSV1nkP6j/wEi/8AjlENk3c8f9en/wB0UAcJ4ksf+Eb8N3fiT/hHLu79LOztPOmuP+ucdeK6/wDtMf"
    "2bqOT+zB8Uz/3I0Xk/9/I7iSvqGaAd9OtB/n/r4q7DpY6f2bpX/gJL/wDHKAPijXv2p/jVpmNR8N/sUXlr63fiS01LUYZ/+3SOO2kj/wC/kldH"
    "4W/4Keft0eGtO/s3TP2f/C1paf8APpZ/CPUoYf8A0or6v/4R7TV4/wCEb8P/APkWrU2hHtplp+dAHzLZ/wDBXP8Aba66l8ANK/8ACE1KH/28kr"
    "X/AOHyH7Sf/MS/ZKtfy1Kz/wDacle/fYj/ANAw/wDbnRDq+ndf+PX/ALdPOoA8GtP+Cznx/B/5M5vP+3PV9Shh/wDImn1Y/wCHw/x91L/mxTxD"
    "/wCFFff/ACqr6Ehtz/0Ex+dSTaQemBQB83D/AIKuftI6l/zZPq3/AIGalN/5Ejs6r/8ADzL9qP8A5hv7E93/ANvmk6vN/wC046+nodBX/kG6aK"
    "6Tw38G/GviQf8AEt8EXX/gJ+5oA+PYf+Cj/wC2sOR+yZk/9ijq/wD8crR03/gpB+1npox4k/YVu7q1/wCnPw5qUP8A8cr7w0D9iz4kakf+Jl9k"
    "tf8At7/+N13Xhv8A4J+aXpp/4qXxp9qz/wA+flw/+jKAPhb4Z/t1fH34meJv+EZ8F/8ABNLxlPqln5VxeWtr4it4gPM/6+xb10fjD9r741/Djx"
    "rc+CvGv/BPP4k2t1pN59nu/sWr200P/bOS38yOSvv/AEH9mfU/BGnHw34b+LV3a+H8/aLTSLz7NNNbzf8APX7R/wBdP+Wf/oyuh8N/A3wTpup/"
    "2lqOm2uq3X/P3eWfnTf+i6APzj0f9r/4keIxjTf+CePxUu//AAJ/9p6dJXovhv40+LD/AMTHxJ/wSb+KN3/1+a/qf/tPR4q/QzTolB/s3TdMHv"
    "zL/wDG6u2dkvbS/slAHxHo37VHh3TBg/8ABHbxT/4Band/+jNLrsdJ/wCCjvjbw5z4b/4JV+NLT/rz0i+h/wDcXX1z9n1IcbhUXkZ4/sz/AMko"
    "v/jlAHynN/wVe+N3Q/8ABOD4kf8AgFq//tPR6Uf8FZPj5qR/4l3/AATw8f8A1/sfxB/8oq+sdoHJ/wBF/wC/cNSQ32o4/wCQja/+BUdAHypp/w"
    "DwU9/ak1LI0z/gnj41+v2PV4v/AEZo1aEP/BSD9rvof+CZ/j7/AMCZf/lfX1J/bGqf9BC0/wDAqP8A+N1F9ub/AKCFp/4F/wD2ugD5ln/4KOft"
    "ZjjTv+CX/j65/wC4tJD/AO2dc/qP/BRf/go2w/4ln/BK/X7b/r78RyTf+2cdfYH23UPX/wBG/wDxujzk/wCgb/5OS/8AxugD4O1b9ub/AILGA/"
    "8AFNfsEDTPr4Rkm/8AcrFWJqX7an/Bbxv+bUPy8Dyj/wBGXElfoUb84x/ZuP8At7l/+N1ieO/C2qeNtL/sxvEWq2lr3+x+UP8AyJJb0AfFHxI/"
    "aJ/4LO/DjTbTUiPhvqv2v/R/snhrSpZpoP8Apr5lxHHH5dc/eftJ/wDBdJhj/hU13aj1HhK2l/8AjlfYmk/s46ppun/2fqHxa8e3VqB591am5t"
    "rSGf8A66fZ7OKvTrFdT08Y0/8A0XHcfvf/AEZHQB+bV58cv+C8Woc6d4H1Tn18I20X/tOsTU/Fv/BfLU150nxOfwis/wD0XX6jfb9V7al/5KVC"
    "16xP/H9/5KUAflTLL/wXQ1DppfjIf9vVzTPJ/wCC4H/QO8Z/+Bdz/wDG6/Vv+1/+oiP/ACFT4W1bsbn8o/8A43QB+UEV/wD8Fz8f2bp8fj08f8"
    "/Un/oyS3rM1zwL/wAFm/EA2+IfDHj+5HbHi3Uv/adfruG1Y8f2pdflH/8AG6izqvdh/wCBn/2ugD8fdN+D3/BYnTjjTfhR40/8OLq8P/tOuq8O"
    "Qf8ABc3TBnT/AAt489s+N7mb/wBKLOSv1Qmv/Eh4Gm2v/gZL/wDG6o3nirxrpvbSj75koA/P3wt8TP8Ag4V00AJ8JrO6PYeI7uxl/wC/nl29t/"
    "6Mr2Pw/wDtDf8ABXiw0z+0fiL+zj8IvD+DydY8XSww/wDkp9pr1P4nftXal4KH9m6fqGlXV1jpaCSvm/x58cPEniTUf7S1PxJ9q470Aeq/Ef8A"
    "a+8beJPBNrpmo6baWmq4/wCJv/Y95LNZ+d/0zkkjjk8v/rpHHXiGoap/aXFY95queuqVTl1AkZx096ANGedM1WmuI6oT3pz/AMhIVWvJj/k0AL"
    "rEz/5Ned6lp7+ldteXLetc3eRP/kUAYUNgM9P/AEZ/8crpPCukD0FUvs/vaflXSeG4KAL0Olcf8hK6q99h/wCojd1HDKfQ/lVnzR/dNADfsXv/"
    "AOTf/wBrpnkH/oJXf/kP/wCN1Z8/61RvL3+zR/y62lAEu7Uv+gkP8/8AbOiqX9pt/wBAw0UAfmt/w9K+Nn/Q6aB/4Tv/ANspf+HrPxb/AOh20D"
    "/wka5KHx/8Wv8AoW/ht+VzD/7TqT/hYPxsHXTvAP5XP/xugDrbP/gqV8Wc/wDI6+Ff/CQ/+2UTf8FWPiwf+Z28K/8AhIf/AGyuSPj/AONfXUtN"
    "8AXX43VSf8J18af+ha+Gv53VAHUw/wDBVb42/wDQ6+FP/CR/+2VY/wCHqHxbPH/Ca+FP/CSl/wDjlcn/AMJj8bP+hd+G1r/4E/8AxurP/CXfGr"
    "PPh34bf+TP/wAboA6m8/4KofFo/wDIS8beH/8AwkP/ALZS/wDDzv4k6acf2j4f/wC3zwhLN/6MkrDs/EXxrPOm+HPhtaH63P8A8brqPDc3xrA+"
    "98Fv+3w6v/8AI9ACwf8ABVf4k6b/AMS3/hOPBf0/4RGX/wCOV698IP2p/wBsb4tfCXxV8f8A4b+HPCuq+FfAv/I3avo/gXzoYP3XmSfu/tHmSe"
    "XH5cknlx/uopKzfh7qvxs1LUrXw3pv/DO/2u8/0f7ITqUP/kS4s/Lr9fvhLoXgr4SeCbXw34b8N2mgf6HD/oln/wBcvL/eSf8ALT/rpQB+VWu/"
    "8FLf2ofgD/ZX/CyPBVr4WutWs/tGkf8ACSfA25077RDH5fmeX9o8vzP9ZH/38jqxp/8AwXb+PunH5vGvgDPt4G/+2V+kn7V3wd8N/FnwPdeJNS"
    "HPhzR9Rv8ASLS7tJPsdxd/ZpPs/wBo8v8AeeX5n/LOOvyzg+MXxsx/xMvDn7Nlrdf9Pmkan/7TjoA7Kz/4OBfj7n/krfgr/wAIXyf/AEZUkP8A"
    "wcFfH09f2gPAH/hD2/8A8drjIfjv8fuw/Zs/LxB/8j1es/j9+0jn/kGfs13f4a3/AO1I6AOpt/8Ag4S+PY4/4Xb4W/Hwnbf+05KX/iIT+PZ/5r"
    "74L/8ACSj/APjlc9N+1B+0h/0Dv2Yv/APXP/jdaWm/tF/tjeJPtZ8NfDj9m3Vfsdn9vu/7H0jxBd/Z4Y/+WskdvHJ+7/eUAa3/ABEC/tGf9Fs8"
    "K/8AhDn/AOOVL/xEMfHrt8W/Bn/hEH/45XHf8Nh/tRAf8g/9lf8A7c9I8S/6n/tnb1dg/a7/AGotSPX9lf8A8FHiD/2pb0AdL/xEF/H3/orXgz"
    "/whpf/AI5SQ/8ABwJ+0b/0VrwZ/wCENLWD/wANnftQAf8AIN/ZXP8A26a5DN/6LqX/AIbH/ahzxpv7K93/ANy5rn/xugDevP8Ag4D/AGjtN/5r"
    "V4M+v/CD0f8AD/r9p/8As7+0/wDhbPhb7L/z+f8ACsZYYf8Av5JHWJZ/tl/tR6af+Qf+yv8A+CjW4f8A0XHUX/DYf7SGMf8ACOfsm/av+wRrf/"
    "oz7PQBuf8AEQx8fP8Aov8A4Y/8IiP/AON0Q/8ABw18ff8Ao4DwZ/4Q9Yk37Zf7UZ7/ALMX/gp1z/43Uf8Aw2j+1JpvP9p/ssf+CnXP/kagDo4f"
    "+Dhn4+9P+GgPBv8A4Q9Sw/8ABw38fe/xu8Lf+EN/9srmx+2X+1Gf+JkD+zF/4KdXqKb9tL9qPPJ/ZX/8FOuf+i/LoA6H/iId+P4/5r/4V/8ACG"
    "qL/iIb/aQxt/4X74L+v/CDS/8A7usOb9s39qPr/wAYm/8Ago1v/wCNUTftl/taHj+0v2V//Cd8Qf8AyNQBrf8AEQp+0d/0cD4W/wDDeGpYP+Dg"
    "39o8YA+P/gz/AMN5L/7TrI/4bS/a076f+yv/ANudnq//AKL+z1L/AMNpftZj/mH/ALK//b5o+p//ABugDbh/4ODfj8R/yW3wX/4Q8v8A8cpP+I"
    "gz9o//AKLZ4L/8IeX/AOOVkxftw/tZ5yfDX7Jn/b54c1L/AONVLB+3f+1Dn/km37IN1/3Lmrw/+0qANOX/AIOBP2jgf+SteC//AAhpf/jlRXn/"
    "AAcNfH3TRkfGzwX/AOENczf+i5KrQ/t3ftQ/9E3/AGN//BTq/wD8j0Q/t+ftQ/8ARNv2Qf8AwU6vD/7ToAs/8RGPxsGBpnxs8Fj/ALkaX/45UQ"
    "/4OLvj2enxt8A/+G7uv/jlYGvf8FCv2tC2NN8E/sr8H/oEavN/5Erxjx7+1h+1F41N2NR/4VBaf9Oln/afk/8AfugD3PV/+Dh79o0cf8Lb8Kn1"
    "+yeB7qL+dcf4k/4L4fH7xH/yEfjb4e/8JGWH/wBGV4MPi58bNNx/xLfhZ/4CX3k1Vm+OPxr76b8LP/AS+oA9D1j/AILA/EnUuNS+JPh7/wAJyq"
    "Q/4K3eNwP+R48P/wDhI/8AtSOuJ/4Xh8bP+gd8Nf8Atzs7mopvjh8a/XwB/wCAVz/8boA7f/h7L8Ruv/CbeH//AAkpf/jlRTf8FXviNn/kdvD/"
    "AP4SP/2yuNvPjT8bNRP/ACDvh/8A+RP/AI3Uc3xp+Pv/AEDvBX/bnd/+0/s9AHd/8PX/AIkf9DL4V/8ACR/+2VHN/wAFX/iPn/kZPCv/AISEv/"
    "xyuDvPi38bP+gZ4A/X/wBpx0XnxU+NZ6eGvh//AJ/7Z0Aegf8AD0v4kf8AQz+H/wDwnJP/AI5Va8/4Kh/Ej/oZvCv0/wCEdk/+OVwE3xU+NnX/"
    "AIRv4a/+AX/2uov+FqfFrTf+Zc8Ff+AX/wBroA7+b/gqT416f8JJ4W/8JGX/AOOVLZ/8FV/iP/0Mnhb/AMJ2X/45XAf8LO+LPfTfBf8A4B//AG"
    "uk/wCFqfGw8DTvBX5f/a6APSf+Hr/xHHH/AAkvhb/tz8OS/wDxyj/h7B8Wf+hk8Lf+EjJ/8crzL/hZnxs/6B/gv9f/AJHpYfiJ8Ws/8g3wB+X/"
    "ANz0Aemf8PZPiyOP+El8K/8AhISf/JFVf+HrHxJ1Ln/hJNB/8Jy5/wDakleef8LE+NP/AEDvBP5//c1H/Ce/Fn/mJad4K/P/ANp+XQB6J/w9F+"
    "LH/Q7Wn/hO/wD2yiuA/wCFg/Egcf8ACNeAP8/9s6KAPrU/sEfAALj/AIRrVf8AwcS//HKF/YC+AH/Qtaqf+4vc/wDxyvoa08O/2nwD+tdBoXwe"
    "8Sal/wAwzrQB8uQfsE/APUuNS8N6r/4Udz/8crpfh5/wTR+APiTxJ/Zv/CNa/aWmP+Pyz1i5u/s//XSPzK+s9B/ZzPXUdRz+NdJ/wzp8JdS/4l"
    "niTTru6tP+Xyz+2fuZ4f8AnlJH/wAtKAPkvxV/wS1/Zu8N8+Gv+Jr/AKZ/x6DV7nzvJ/56f6yOtbwr/wAEmvhx4lO4/De6+yf9he5/+OV9a+A/"
    "g78E/hJ/yTj4caVpQ/5+7PSIoZvJ/wCefmf88/8AV13+mT6jqhwCeO9AHynoP/BHH4Ag/wDEy067/wC3PWLn/wBqSV0F5/wSU/ZL0zTf7T1HVN"
    "WtLWz6fbNY8n/yJX1fpuh6iP4hz7VifE3wd8JM2mpfEj4c3Wq/9Pf/AAjlzd/+RI6APky8/ZC/4J4+CQf7O+JF3d3fT/iT3lzNNBN/10kk8uvd"
    "LjVh8Wfsvh34c+JbTXv+X+8Oj3kXneTHL+8lj/7aVnXmhaZ/aWfhv+wHaara/wDLpd6xpH/xyPy//IldN4X1L9tXTtR/4ov4B+AdL/7dI4f3P/"
    "PP93ceZQB3Oha7pumabaabqepWlp/of/L5eRQzf+RK8X17/gld+zb8R/Eeq/EjU9M1W6utXvJr+8+x3n7nzpP3knl19CaYvxr8R/ZL7xn4c8F2"
    "f/P3i7ubz99/0w/1VdzFg9Tg0AfIMP8AwRq/Ze9Nf/8AAuiD/gjH+yYTj+zdVuv+4vL/APHK+tdY8K6b4j41LS/tf/Aqs6bpem6b/wAS3TdM/w"
    "BF+tAHx7ef8EYv2OdNxqOpjxB9lz/0MckMNcTpnwX/AGcP2S/G39o/s3eGvFWq+ILuz+wXf2M3P+p82OT939o/dyf6v/np/wAs6/QTK/3qCM9K"
    "APhnTP2UtK/bG1PSl+Nf7L3irSrbSftn2S7vD/ZENv58u+T/AI97j/SHk/dyf6uSu0P/AARc/YnPA8N6tn31e5/+OV9ZBT0zipYLhtwoA+Sf+H"
    "KX7E55/wCEc8Qf+FHJVg/8EU/2KAM/8ITq3/hR3P8A8cr67z6mnnGDzQB8gf8ADln9iXv4c1X/AMKK5/8AjlVpv+CJv7E2f+Rc1/8A8KKSvsHU"
    "tK0zUtNOmalpn2q1u+tpmpNN0rTdM03+ztN037J/15mgD45b/giP+woqc+C9fuhnv4uua5f/AIdifsBeG9QutOHw38U2htP+puufO/79+ZX3N4"
    "k8ODxJpv8AZn9p3drn/nzu/Jm/7+V5g37E3wYHiP8A4SPUdU8Z/a/+x41OEfvP+mccscdAHiPhb/gj/wD8E8vGumf8JJp3w41+6z0P/CXX3P8A"
    "5ErX/wCHKn7Cw6+CvEH1/wCEtuf/AI5X1D4b8Kab4Z0z+ztOF19ms+95dyS/+jK0y2fbigD5Fn/4Im/sKf8AQk+IP/Cuuf8A45Uf/Dk79hP/AK"
    "FrxB/4V1zX15QOTigD4/m/4Ig/sKd/BXiD/wAK65pf+HIP7Cn/AEJXiD/wr7qvsHCDqKP3dAHx7/w47/YE/wChK8Qf+Ffc0s3/AAQ6/YC/6Erx"
    "B/4V19/8cr7B85P7v6VHNeoRwKAPjiX/AIIffsB5z/whPiD/AMK65/8AjlZus/8ABFX/AIJ46bgHwV4gP/c3XP8A8cr658SeL9O0sV4n8TvioO"
    "o1P680AfNXjb/glt/wTf8ADRGm6d8OPEF39PF19/8AHK8Y8ff8E/P2FvDQ/wCJb4J1X8fF19/7Ukr1/wCKnxT3fa/7MP8Apf0rynUr7UNS5z+d"
    "AHlOp/sPfsu8/wBm+CtV/wDCiuf/AEZ5lVrP9hD9m/PHhvVf/Civv/jlerww+1a2m2RPGBQB45B/wT9/Zwx/yJeq/wDhRXP/AMco/wCHev7OHT"
    "/hH9V/8KG+/wDjle82ekHv/KrP9mmgDwH/AId6/su/9CVqv/hR33/x2oof+Ce37Lv/AEJOq3f/AC7/APIx33/x395X0PNZc/dqTTYdS03U/wC0"
    "fDeoaraXX/P5Z3f77/tnQBwOjf8ABG79m8DOpf8AEq+2Wf8Aof2PxFfTfaJv+mkckn7usT4k/wDBIb4JeC9O/tEfDjX7vSgIc6tZ+I7mGHzv+e"
    "cfmSfvP+uleq+ML74seNtStdS1P41+NP8AQ/8Ajz+x6vFZ/Z/+mv8Ao8f7yT/ppUcX/CbanplppupfEjxVqtp/1GPEUt5/6M/eUAfOf/Dvv9nD"
    "/oSNU/8ACiv/AP45Uc3/AAT7/Zv/AOhI1T/worr/AOOV9EDROO35VHNpR9R+VAHzl/wwF+zj/wBCTqv/AIUV9/8AHKl/4d+fs3f9CTqn/hRXX/"
    "xyvoObQz/eH5VH/ZJ9vyoA8A/4YD/Zv/6EbVP/AAorn/45R/wwR+y7/wBCzqv/AIVt9/8AHK+godKfpij+yn7r+lAHzv8A8ME/s3f9CRqn/hRX"
    "P/xypf8Ahgn9n8f8y5qn/hR3VfSMPhbS8/8AIMFEXhbSz/zDBQB86j9h/wCChGf+Eduv/Bvc/wDxyivo4+G9Oz900UAe+6ZofhvTudO03itezO"
    "pk/JWtp3h0t9K6XSPB+PlWgDlrPw3qupfwj8q6TR/h8/oBXSaboY5GOnvW1DCuen60AYum+B9K03iugh0pOlSQo2Onap9jelADobd+4q1Bv3dK"
    "buX1p32of3qAJIQufvd/SrGF/vfpVP8AtE/3aP7RP92gC5tP+TRhumf1qn9u/wBsfpR9u/2x+lAFzzPb9Kl8z6/lWT/aqd1/SpYb1M8CgDR8+P"
    "0o89Ow57VSF8p7VLDOmcDrQBdBOOF/WrUJY8FfTvWZDJnkH8MVJDJgZBoA1obkd1/WpftA/u/rWbDcNn73H1qX7U3qKAL32r/ZqT7V6LWb5vo3"
    "6VJ5n+0KAL3mH1/So/MPr+lVvOWjzloAs+YR0P6Ub/f9KrectRfbk/u/pQBd+0L/AHKPtC/3Kpf2w3tVX+3dP/vD86ANXziBxmoZr4Nlu9c/q/"
    "jDTdOOAMe9cJ4r+Jgzw2aAPSdS8cabphzz+NcR4w+LJJryHxh8a9N0wfLqP1NeN/EP48kH/iXH8qAPVviF8cs8JqFeA/EH4t6jqX/IN5/GuW1n"
    "xZqfiXjUzVKC2x19elAEEqjUuSPzogsAOlaUNjkZqzDpVAGdZ6WfWtez0onvRZwdqswwUAWLO2HoKs/Zx7UQd6lh6fhQAggB5BqQQKeQP1pAuD"
    "wfwrqfC3wzHiTTv+El/tG0tcD/ADLHQBzf2c+hq34b8H+JvFJmTwz4bv8AUDbxGW5Wws3mMSDq7bAdo4PJ44rpvGHgbTfDf/Ex8N+JLW7tf+ny"
    "8immuJv+mdeC/tkfHHxx8NPhBL8MPCfiOeysvGN1GdaubORo5zDbMMQh1IZUczkNgjOBXLjsUsHhZVmr2/zse/wvkNXibP8AD5ZTlyuq7Xetkk"
    "23brZJnT3HxD+HVncy2d34/wBFimhcpNFJqsKtGw6hgWyD7Goh8RPhwW2D4g6JnOMf2tDnP/fVeLXH7OP7OPwp0XSLf9qP49+IdG8T63pEGqDQ"
    "PDekyXP9n20oJiNxJzmR8Hj2qBPBf/BOcybZP2lPiejDlt3hmXP/AKD1FfNf6yVlvCK/7eP3GHghlFRc1PF15x6SjQk0/NPquzPbz8QfhqACfi"
    "BoeD0/4m8PP/j1RL46+GcX+t+IehL/AL2rQj/2avFm8E/8E5e37SvxNP8A3LEv/wATTT4K/wCCczct+0x8TSPfwzIf/ZaHxLWX2I/+BFvwNytf"
    "8xGJ/wDCeR7dF4/+GCkhviPoIx1B1iD/AOKq1aePPhncSLDD8RdCd3ICourwksT0AG6vB/8AhDP+CcgO4/tKfEtgO3/CLyZPsPlq7pP7OH7Mnx"
    "hS+8M/sy/tC+ItV8XWelzajZ6N4n0iS1i1GGIZkjhkOAJADx+NOPEleUrckf8AwIzn4I5PTd6mLrwj1lLDySXm30XmfUGqeAPFHhy3tbvxD4bv"
    "7CK+i8yylvLN4luE4+ZCwAccjkZ6j1qr/Yoryf8AYS+L/j/x78PL74U+Kdfu73S/CNwtxoaXsrySWwvGk8yIO5LbAbZQqknHPrXu8OlV9HgMUs"
    "bhI1rWv/nY/EeK8gqcL8Q4jK5z5nSdr7XTSknb0aMX+wqK6P7Cfaius+ePqaHwuvTNaUOmL61dhRcdO1P2L6UARfYfrSCBScU+mS8c0AG73NG7"
    "3NR4HpUePY0AXPOWjzlqtketFAEnnD+7+lRGYAZ2/pTKDBxyaAF84dz+lR+cOx/SlhtyD1NHkH+9+tAD/NY9qk81/SmeU394fnUnlN/eH50ASe"
    "cCN2MD0q1DO2No7e9VYoct6irUUOUwetAFjzj71JDPzUdSUASfaDRBcH/61R+f7VF9uNAGl549aPPHrXO3ni3Tc4zWTqXxC96AO0mvuao3fiPJ"
    "xXnF58QdUz1qt/a/iXUh1oA9AvfGWDjj8qozeMh7flXJw6Lqmcg8VYh8O6p60AaV54z1NsAdapHXdRUYA5qtqf8AZuljC/ia43xR4+0vTRk/lQ"
    "BreKfFP9nd8+1eKfEj4g5JJGKteK/HGo6rwPzrhNRhXURjNAHI69rmq6l2uqxIYJDwa7KbSo+lR/2TH6fpQBgQ6YvTP6Vehs19a1vsK/3f1qb+"
    "zl9DQBmQwdqlhgq/9hHvTfI96AK/ke9SQwVJ5HvRQAsMFSTcDNVv+JlVqGCgBcADjmuT1jwd8SNS1L+0vDfxs1+0P/LpZ/66H/rl5cn/ACz/AO"
    "mddtDb7Ae9WhBxkH9aAOW0ew+JB8Sf2n4k+JF3qv2yz/49PscUMP8A37jj/d14v/wUIJGieHCCR+8uei8H95bd+1fSEMXPX9K+eP8AgopCq6H4"
    "ZbDE+bc4A7/vLXivKzyXLldR+n5o/RvCVX8Q8B6z/wDTczD/AOCiDs/7Q9huOCfh3opdicjOx8kkfex615/8BfgV8Tv2kviLB8KPhTp63WsT2s"
    "1zHHcT+XGIo8biznheo5Neg/8ABRV0/wCGhNOkaRVH/Ct9EeQk/dGx+uK+2v8AgkJ+yu/wQ+DOqftH/EfTZLTV/FFmZbSC5j/eWWlRqWGR1VpB"
    "1HsK+Cp4b6xjJJ/Cnqz+vMz4tjwlwDh8TC3tpRUaafWTb6dUup+bHxk+EXxK/Z+8ZN8P/jNoLaDq6wiZLS4uQ/nRHO10YcFTg816n+z9/wAE6v"
    "2hv2lfg+3xv+GepaGdDWW4jc3l7skDQjLnBPT0r6T+Cv7eX7Ifj7xF44+IX7VvgG81HUdZ8UTN4ZWfwrNfeRpCHbDGrrwBwx219o/s3fEX4DfE"
    "b4HN4y+BXhw2HhPzbpXs/wCx3tDuUfvT5Lc8j866sLl2FnUd53XbqvU+Q4i8U+LMrwFOEsHyVU4qVRq9N3V2o6/dr0Z+Dv8AadkGMX9qwja7KV"
    "E/JYMVwPcEGvd/+CcMhb9rnR3aXP8AxTuqcbOMiL7wJ6Z9q+yfGX7YX/BKvUvDGr6foPwkljvprO4is5D8P5wFnOQDuxwdw618a/8ABOGKe3/a"
    "50e2uopI5l8P6wJI5l2sBsJHB6cEcVyLCvD4mC5k030PtKfFeJ4l4WxzxGDnQcYfb+1ftpsdL/wTTjDXnjjHYaf3/wCmt9zX1LDBXy9/wTLTzL"
    "zxz7DTv/Rt9X1fDBX3eQ/8imn8/wD0pn8heL65fEbHK99af/pqBDRUlFewfmp9T/23p3/QStv/AALFH/CR+HP+hktf/AuuW/4Uh4c/6CF1+cVH"
    "/Cj/AA5/0ELr84qAOs/4SPw138SWn/gXUf8AbmmdtStP/AuuX/4Uf4c76tdUf8KW8OdtVuvyoA6f/hI/Dn/QStP/AALqP+29P/6CVpXLTfB3ww"
    "f+Yjd1zmseFPDGm/8AINF3d0Aemf27p56ana1IdV03POon8q8v0j4f6jqQyTmuo0v4c6bp3Tp6UAdR/aen9tRP50f2nYf9BE/nWPD4b070qz/Y"
    "en+hoAv/ANuaf/0EaX+1rH1/UVX/ALB0n1p/2HSfQfnQBa+2f7H6VL9uzxsqhNJpueTzVabUtM/vUAbf2hP7lH9ojGNprl5vECjtWTqd54i1L/"
    "kHCgDuLzxVpfrn2rOn+J3hzqdTtRx0rzLXdK1InGo+JMe9clrOlAcbc+9AHsmo/Gnw7/0EjXHa98cG/wCYbmvP4vB2pamfuGup0H4OalqQ+5QB"
    "Rh8f6jqX/MRH51rabrnhr/mJakPzrpNN/Z60zjkflV6b4A+GvUflQBzdl458NjjNdBpnivwSf+Qj4ltayNS+E3gvTf8AmKXVcTr3hfw7ph/4lx"
    "uqAPW5vi54K03n+0ulc34l+OWm4/4lteQ6lZadn7v0qlNC2fu/rQB0/iT4lHUz1rjtT1k6keWp01jVb+z/AH/WgCtN0/Cq3ke9a39n+/60fZva"
    "gDEmsarf2f7/AK1tzWNM+wj3oAzPs59B+VH2E+1aUNjzUnkrQBmzWNRQ2NbH2Ee9SQ2NAGR/Zf0qX+yfb9K0obGpIYKAM2z0qpvsI960IbGpYb"
    "GgDK+wj3qx9nHofyrR+w+wqX7H7frQBlQwn1NfNX/BSK38nQ/C7BgHL3YDFug8y1zj1NfU0MFfMP8AwUyj2aP4S95Lz/0ZaV5OeO2VVPl+aP0f"
    "wjdvEXAf4pf+m5lH9sXxL4c8F/tl+EPFnizw1FrOk6V4S8OXOpaXc8JdQKH3KR3PIOPav1im+IPgj4mfBbXfEPw68Q2uo6e3h6eNJbNlKKfJ/w"
    "BUSOFK9Mdq/IL/AIKIhZP2h9PR/wDonWh9f9x6+3/+CSCg/wDBOjxNHvPzaprIJPP/ACzWvkMvrOOMq0um5+9+IOS0q3CGWZm5Pmg4wt0ak7/e"
    "rf1oYn7MP7edl4S/Z88JeHLX9iPx5r4sNIWF9XsPDcUlvebJHBkjfyzuz25r03w3/wAFFb/U9XsPDVt+w38UdOivbyOHzToPlQ2yucF3CoAAO9"
    "cN/wAEiv2mviL8W/hlZfB7TPDmgWWkeA7L7Hf3Uuob764y7+XLHCDlEJz8xzVf4n/8FW/jn8Gv2j1/Z1+In7Nthb3R1yC0W9j1XKz28zEJcxnP"
    "zKR27c13Uq9NUYzc3bvbqfB4vh7EYjO8RgaeFTqQvJr2rvyvW6XpZtdLn0v+0l8WdI/Z88IWPiWy/Z81Lxl9u1AWosfCuhQTTW4Iz5rgRn5K/M"
    "X9mrxZF4+/4KY614xstBudJj1RdcmXS7uEJLa/ul/duoHyEY6e9foj+37+2Rr37FPw10Xx/oXgOHxDJqesJYSW01z5Xlhlzvz0J56e1fmt+xR4"
    "ym+IX7fEvj+SzW2l1uz1u/e2WTf5TSRjK7h16Vy46tTeIpwUru/Y+p8M8qxcOGcwx8qfuyg4qble9nquXpbTXzNb/gmJGHvfHX007/0bfV9Z/Z"
    "z6D8q+UP8Agl7/AMffjzPppv8A6Nv6+uPI96+qyJJZVTS8/wD0pn414wJrxHx6feH/AKagR+SfeipMH0or1z81PpzJPU1ynxP8cal4J006lpun"
    "2t0f7Hu78/az/wAsbcx+Z/q4/wB5/rK6OaY+n4Vx/j6w/tPUjpv/AD+eGtXt/wDv4begDjbT44fGzUvEd14c07wZpQ+xnreGT/2n/q/3f7yvRP"
    "CvirUvEfhy01LUdN+y3V3Z/wCmWf8A02/5af8AkSvmqOw/4Rzxta/8U5dWt3jTp7QXl3c+d/pFzH9o/wBH/wBXJ5ckf+s/d/6v/lpX0P8ADIZ8"
    "E2rD1m/9GyUAas1j/aVSQ+HBjFS/2h7fpUX9tadQBY/spx2FP+wyf5xWf/wkq+oqKbX1x1oA1/K0z+8PyqL7UvqKyP7S/wBqo9/+cUAaU2rp/k"
    "VSvNQ1ADiov7T0z+/+tVbzxB/tUAVby41Prmq02rH1rO1LxN7Vk3ms+1AHRXnjHTazbzxVqWpdqxoU9q6DR9MB4xQBn2mh6lqXWup0D4cjOcVd"
    "06fTdN4rSh1zigCbTvAmn8nH51tQ22n6b2rl7zxy/aub1j4gP6H8qAO/1Lxhpum8Zrjdd+JnvXFal4kHTbXP3l+P7n60Abmu+LuM5xXL3l3jnN"
    "PmBz071V+zH0oAq5PrUfkn1/Wr32b2o+ze1AFaaCq00FalFAGX5HvUfke9aM33aX7OPQ/lQBm+R71F9hPtWl5K0eStAFH7CfaiGCr0NjzUv2Ee"
    "9AGT5J96SGCtLyVpfsI96AKMMHNSfY/b9avfYR71JDBQBFDBUv2Ee9Wfs59B+VWfI96AMn7CPerP2c+g/KrPke9SQd6AKX2E+1fKv/BUWJhpXg"
    "9VXJL3uFz1/eWfFfXfke9eWftgfsqat+0V8IJ9d8F65p6eI/Cdylxp+j6jeLAup28p/wBIjRm/5aARR7QAclq8zOKcquW1IRV27fmj7rwzx2Dy"
    "7jrA4jFTUaak029ErxlG7fRXZ8vf8FFLy0g/aE0mW7uY40m+HGiNC0r7RMux+VPp719kf8Epfif8NfD/APwT88Q6NrnxC0KzuZNU1gi2u9USOR"
    "gUGMKecHt618o+CPjT+2j4E8HWHw91b9mhvFVnosfkaTN4q+Ht5dzWUQ/5ZRyqg3RjtnNag/aR/awxlv2GNAxtGY0+Gmorz36DHP0r4qjTrUa7"
    "qOD18mf1Fn06Od8N0cpVaio0pRakq0PeUW7aOzV7/LzPHv2Tv2p9V/ZR+MOnfFzwvdW9x5Pnw6ppAuMDULdnbMZP5EE9Oa+z/wBnvxn8B/8Agp"
    "34nl+M37T/AIwsvDfiP4f60n9iafpmrxWtq1kxzGrGQFpGGDu5x6YrxE/tJ/tXcj/hhbwyx4AY/Cy/HHp93t60f8NJftY9v2FvDi4GcQ/DHUU3"
    "H8AP1ooQrU5RjJNw3tys04jlg88i6+HdGjiOVR9qsRDm5V0t2ez6+Z7R/wAFqP2oPgX8Q/AHhX4ReAviZo2t6lZ63/aOoJpd4Jo7aKMYXc68ZP"
    "p14r5n/wCCbVzZ337XOkraXscqp4a1ZpFiOTEoi+82Og/lXXD9ov8AatV/l/YP8LbSclf+FW3+4/8AAtvX61Q8WfHn9tPxV4Y1HwfoP7M6+FIt"
    "YgNtqV/4V+Hd7b3VxA33oTKyHah4zjBpVo1K+M9soyXlyseR1MLk/CsslhVpe9zXm68LJyabdkunRfj1Nv8A4JbRl7rx6xBGRpuD/wBtb+vrwQ"
    "cjmvHv2Jv2UvEv7P3wxk8b+OtR02LWPGUoaXQbG9E0mm29uW8nzsfckcTsSpAI2mva7fp+Nfa5LSnRyunCas9fxbZ/Lnihj8HmfHmOxGFmp03K"
    "KUlqnywjF2fqmQ/8TKitCivUPgT27z/pWb4g0LTfEotf7SF1k/8APndy2c3/AH8t5Km0yVj+FOPQ0AcrN8MvBP8AyEj/AMJBgf8AU3al53/fz7"
    "RWlpulNp2mWum6Z/x6Wnf/APeVqw/dpZc7MZoAzPsPsKl/sKpoZG6VTvL2T0oAteTpnoKjm6/jUc3X8aimlbGaAIpr7vWbfX3BFaU/f8azppGo"
    "AyZpn/yapTK2Pw9a1puv402zjW7/ANZQBmQ6PnnPX2q7pvhHsT+lbEMYrXhjXrQBl2fhj3/SrM2m9s1e3n0FY95K1AD5r4elZl7qvGRUOp3L8c"
    "VkXty+3p2oAfeXo6YrNvL4dadPcv6VV8z2oAq5PrVfJ9asVHQBHDCc9aPJP979aX7S/wDZnTtS7z6CgCHyT71JU8Ma4zS0AU/JPvSTQVY2D1NM"
    "h6/jQBW+zn0H5VL5J96kp80a0AU/s59B+VH2c+g/Kr81shPWpJrKMd6AMf7N7VL9nPoPyq5sHqaWgCL7Cfal8k+9WKIO9AFfyPejyPersPT8KI"
    "en4UAReR71J5HvUs3T8KseStAFOGCjyVq3Z2yY60s0YoAg+zj0P5UfZx6H8qseUPb8qjn7/jQBDRWnNGvWs2b7tAC/Zx6H8qbVineUPb8qAK/2"
    "ceh/Kj7OPQ/lT4YlosolvOJOOe1ADPs49D+VSW/UfhViCMUQRigCP7D/ANQ7/wAnKKd/bM/9wfnRQB//2Q=="
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
