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
BOOKS_FILE = ROOT / "azim_books_list.json"        # the most famous novels, built once by azim_make_booklist.py
REJECTED_FILE = ROOT / "azim_rejected.json"       # books that can never be used (no source / blocked)
PENDING_FILE = ROOT / "azim_pending.json"         # finished script of a run that failed later: resumed next run
WORK = ROOT / "azim_work"

START_TIME = time.time()
JOB_BUDGET = 340 * 60     # seconds; GitHub Actions default job limit is 360 minutes

W, H = 1280, 720
FPS = 25
PAD = 0.25                # pause after each scene (seconds)
TOTAL_BOOKS = 2000              # only novels
MAX_SECONDS = 470         # up to 7:50 (hard limit 8 minutes)
MAX_SPEED = 1.2           # maximum voice speed-up (higher sounds unnatural)
SUB_FONT_SIZE = 23        # subtitle size (was 22)
SUB_FONT = "DejaVu Sans"      # tested: «لا» in «تلاش، کلاه، کلاسیک» is drawn correctly (Noto Sans Arabic drew a rectangle)
BEAT_WORDS = int(os.environ.get("AZIM_BEAT_WORDS") or 24)   # a new picture about every 24 spoken words
MAX_WORDS = int(os.environ.get("AZIM_MAX_WORDS") or 800)   # about 5.5 minutes of narration
SCRIPT_VERSION = 9        # a saved script of another version is not resumed

# ----- the ONLY source of the story: the "Plot" section of the novel's English Wikipedia article.
# It is an editor-reviewed, concise summary (the Wikipedia guideline asks for about 400-700 words), free to reuse
# (CC BY-SA). The bot retells it in Persian; it never summarizes the book on its own.
# The list builder already keeps only novels with a Plot of 350-900 words; these limits are a wider safety net.
PLOT_MIN_WORDS = int(os.environ.get("AZIM_PLOT_MIN") or 250)
PLOT_MAX_WORDS = int(os.environ.get("AZIM_PLOT_MAX") or 1100)

# ----- narrator engine: "gemini" (Gemini TTS, default; any failure or finished quota -> Pocket TTS for that scene)
# ----- or "pocket" (offline Pocket TTS only). Switch without editing code: repository variable AZIM_TTS.
TTS_ENGINE = (os.environ.get("AZIM_TTS") or "gemini").strip().lower()
GEMINI_TTS_MODEL = os.environ.get("AZIM_GEMINI_TTS_MODEL") or "gemini-3.8-flash-tts"
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
class GeminiBlocked(RuntimeError):
    """Gemini refused the prompt (content/safety block): the same book would fail again."""


class GeminiQuotaExhausted(RuntimeError):
    """The DAILY quota of every model is finished: stop quickly instead of waiting for nothing."""


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


def gemini_json(prompt, minutes=12, models=None, image=None, read_timeout=150):
    parts = [{"text": prompt}]
    if image is not None:
        parts.insert(0, {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(image).decode()}})
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.7},
    }
    if os.environ.get("AZIM_GEMINI_SAFETY", "relaxed") != "default":
        body["safetySettings"] = SAFETY       # fewer false blocks on the plots of famous novels
    headers = {"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"}
    models = models or [GEMINI_MODEL, "gemini-flash-latest", "gemini-flash-lite-latest"]
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
                r = requests.post(url, headers=headers, json=body, timeout=(30, read_timeout))
                if r.status_code == 200:
                    j = r.json()
                    cands = j.get("candidates") or []
                    text = ""
                    if cands:
                        pts = (cands[0].get("content") or {}).get("parts") or []
                        text = "".join(p.get("text", "") for p in pts)
                    if text.strip():
                        data = json.loads(text)
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


WRITE_MODELS = ["gemini-pro-latest", GEMINI_MODEL, "gemini-flash-latest", "gemini-flash-lite-latest"]
# cheap jobs (picture check, simple rewrites) go to the lite model first: every model has its own quota
CHEAP_MODELS = ["gemini-flash-lite-latest", GEMINI_MODEL, "gemini-flash-latest"]


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
    return [r["title_en"] if isinstance(r, dict) else str(r) for r in rejected]


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
                if norm(b["title_en"]) in used or norm_fa(b["title_fa"]) in used_fa:
                    continue
                return {"title_en": b["title_en"], "title_fa": b["title_fa"],
                        "author_en": b["author_en"], "author_fa": b["author_fa"],
                        "wikipedia_title_en": b.get("wiki_en", ""), "wikipedia_title_fa": b.get("wiki_fa", ""),
                        "book_type": "novel"}
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
            "sig": pick_significance(text)}


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
    if en["sig"]:
        parts.append("SIGNIFICANCE (themes, reception, legacy: use ONLY for the closing wrap-up, to say why the "
                     "novel matters):\n" + en["sig"][:1500])
    url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(en["title"].replace(" ", "_"))
    print(f"Source: Wikipedia Plot section, {pw} words.", flush=True)
    return {"text": "\n\n".join(parts), "en_words": pw, "url": url, "title": en["title"],
            "credit": "منبع خلاصه: ویکی‌پدیا (CC BY-SA)"}


def plan_length(src):
    """The narration is about as long as the Plot summary itself: short, dense, nothing added."""
    target = int(max(300, min(MAX_WORDS, src["en_words"] * 0.9)))
    if not USE_CF:                    # slow free images: fewer scenes
        target = min(target, 360)
    n = max(5, round(target / 58))
    lo, hi = max(5, n - 1), n + 1
    return lo, hi, f"{int(target * 0.9)} to {int(target * 1.1)}"


# ---------------------------------------------------------------- script
# The picture styles. The robot only CHOOSES a key (by the rules below); the exact style words are fixed here, so
# the robot can never invent a wrong or mixed style.
STYLES = {
    "cartoon": ("stylish 2D cartoon illustration, bold clean outlines, flat vibrant colors",
                "light, funny, whimsical or children's stories; fairy-tale and nonsense worlds; talking animals "
                "(for example Alice in Wonderland, Pinocchio, The Little Prince, The Wizard of Oz)"),
    "anime": ("anime style illustration, large expressive eyes, clean cel shading, vivid colors, detailed backgrounds",
              "young heroes, coming-of-age, first love, school life, magic and adventure for teenagers "
              "(for example Anne of Green Gables, Harry Potter, The Hobbit, Little Women)"),
    "pencil": ("black and white pencil and charcoal drawing, detailed graphite shading, cross-hatching, monochrome, "
               "only shades of gray",
               "dark, tragic, psychological, crime, war, poverty, prison, dystopia, gothic horror "
               "(for example Crime and Punishment, 1984, Les Miserables, Frankenstein, Dracula)"),
    "ink_watercolor": ("delicate ink and watercolor illustration, fine ink linework, transparent watercolor washes, "
                       "gentle muted colors",
                       "quiet, poetic, nostalgic, pastoral, tender love stories and slow literary novels "
                       "(for example Pride and Prejudice, Jane Eyre, Wuthering Heights, Madame Bovary)"),
    "oil": ("classical oil painting, rich deep colors, visible brushwork, dramatic lighting, museum quality",
            "grand historical epics, big family sagas, aristocratic and 19th-century realist novels, "
            "serious social panoramas (for example War and Peace, Anna Karenina, The Count of Monte Cristo)"),
    "comic": ("comic book illustration, thick black outlines, dynamic angles, bold shadows, halftone shading, "
              "saturated colors",
              "fast action, pirates, duels, thrillers, detective stories, science fiction, superhero-like adventure "
              "(for example Treasure Island, The Three Musketeers, Journey to the Center of the Earth, Sherlock Holmes)"),
}
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
  strong emotion."""

NAMES_RULE = """- NAMES: write the names of people, places, the novel and its author in ENGLISH (Latin) letters, exactly as in the
  SOURCE TEXT (for example Holden Caulfield, The Catcher in the Rye). A voice reads them in English. Everything else is
  Persian: never write any other English word and never write digits (numbers as Persian words). Put a space between a
  Persian word and a name, never glue Persian letters to a Latin name (no «Holden‌ی»). If grammar needs a suffix on a name,
  rephrase so that the name stands alone."""


def design_cast(book, src):
    """Step B0: BEFORE the script is written, design the era and ONE fixed look sheet for every important character
    (ages that fit the family, very different faces and bodies, different clothes, true to the era)."""
    prompt = f"""You are a film casting director and a costume designer. Design the characters of a picture video about
the novel "{book['title_en']}" by {book['author_en']}.

Who the characters are comes ONLY from the SOURCE TEXT. What the source does not say, you decide so that it fits the
era, the place and the story.

SOURCE TEXT:
{src['text']}

Return JSON with exactly these keys:
- "era_en": the time and place of the story in one short English phrase (for example "provincial Russia, 1860s").
- "costume_en": 20 to 35 English words: what ordinary people of THAT time, place and social classes really wore
  (garments, fabrics, hats, shoes). Historically correct: nothing modern in an old story.
- "characters": the important characters, at most 6, most important first, each {{"name": "...", "look_en": "..."}}.
  "name" is the name in English letters exactly as in the source. "look_en" is ONE look sheet of 45 to 65 English words in
  this fixed order: gender and exact age in years; body build; face shape; skin tone; eye color; hair (color, length,
  style); facial hair; ONE distinctive feature; the usual outfit with colors. It is pasted into EVERY picture of this
  character and never changes, so describe the character in the MAIN period of the story. Only appearance: no mood,
  no action, no names of actors.

Rules:
- AGES: use the ages of the source; otherwise decide them so they fit the story and the family tree. A parent is at least
  18 years older than his or her child, grandparents are older than parents, a teenager looks like a teenager and an old
  person looks old.
- FACES: every character has a clearly different face: a different face shape, hair color and style, eye color and
  distinctive feature. Skin tone fits the place and the people of the story. Nobody looks like a twin of somebody else
  unless the source says they are twins.
- BODIES: every character gets a DIFFERENT build (for example thin, heavy, muscular, short, very tall, frail,
  broad-shouldered, round). A build is never copied from the main character unless the source says so; at most ONE
  character is heavy unless the source says that more are.
- CLOTHES: every character wears a different outfit with different main colors, true to his or her job, class, age and
  the era, and historically correct for that time and place.
- Never contradict the source (gender, age, hair, body, job, family relations)."""
    try:
        d = gemini_json(prompt, minutes=8, models=WRITE_MODELS)
        era = " ".join(str(d.get("era_en") or "").split())
        costume = " ".join(str(d.get("costume_en") or "").split())
        chars = []
        for c in d.get("characters") or []:
            if isinstance(c, dict) and str(c.get("name") or "").strip() and str(c.get("look_en") or "").strip():
                chars.append({"name": str(c["name"]).strip(), "look_en": " ".join(str(c["look_en"]).split())})
        if not era:
            return None
        print(f"Cast designed: {len(chars)} character(s), era: {era}", flush=True)
        return {"era_en": era + (f". Clothing of that time: {costume}" if costume else ""), "characters": chars[:6]}
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


def write_scenes(book, src, plan):
    """Step B: write the Persian narration from the source text only (no outside facts)."""
    min_sc, max_sc, words = plan
    cast = design_cast(book, src)
    cast_block = cast_prompt(cast)
    for _ in range(4):
        prompt = f"""You are a gifted Iranian storyteller and radio host. Write the script of a video that summarizes the
novel "{book['title_en']}" by {book['author_en']} for a Persian-speaking audience.

Use ONLY the facts in the SOURCE TEXT below (taken from Wikipedia). Never add a fact, name, date, number,
quote or opinion that is not in the source. If the source says nothing about something, say nothing about it.
The PLOT part of the source is a summary of the whole story. Your job is to DISTILL it: a viewer who never read the
book must understand the whole story from this video alone. So tell only what matters: the central conflict, the turning
points, how the heroes change and the ending. Leave out every episode, side character, place and detail whose removal
would NOT break the chain of cause and effect (the "removal test"). Add nothing that is not in the source.

SOURCE TEXT:
{src['text']}
{cast_block}
Return JSON with exactly these keys:
- "style_key": ONE picture style for the WHOLE video, exactly one of these keys (write the key only):
{STYLE_GUIDE}
  How to choose: decide by the OVERALL mood of the whole book, never by one scene. Check in this order and take the
  first rule that fits: (1) the book is mainly for children or is a light fairy-tale/nonsense story -> "cartoon";
  (2) the book is dark, tragic, criminal, about war, prison, poverty or a grim future -> "pencil";
  (3) the book is a grand historical epic or a large social saga -> "oil";
  (4) the book is mainly action, adventure on the sea, duels, detective or science fiction -> "comic";
  (5) the hero is a teenager or young person growing up, or the story is magical youth adventure -> "anime";
  (6) otherwise (quiet, poetic, romantic, literary) -> "ink_watercolor".
- "characters": a list of ALL the important characters of the story (at most 6), each {{"name": "...", "look_en":
  "..."}}. "name" is the name in English letters exactly as in the source. "look_en" is ONE fixed look sheet in English
  of 35 to 55 words that will be pasted into EVERY picture of this character, so the face is always the same: gender,
  exact age, face shape, skin tone, eye color, hair (color, length, style), facial hair, ONE distinctive feature (scar,
  glasses, freckles, hat...) and the usual clothes with colors. Empty list if there are no characters.
  Base the look on what the source says; if it says nothing, choose a fitting look for the era and keep it fixed.
- "outline": the key events of the PLOT in order, as few as the story needs (usually 7 to 12), each {{"event": "short
  English sentence", "because": "what caused it or what it leads to"}}. Include ONLY events that change the direction of
  the story (removal test). The scenes must tell ALL of them, in this order, and no other event of the plot.
- "cast": the same important characters in order of importance (at most 5), each {{"name": "...", "sentence_fa": "..."}}:
  ONE Persian sentence of 8 to 18 words that introduces the character to the viewer (who he or she is and his or her
  role in the story, only from the source). The sentence STARTS with the name in English letters. Empty list if none.
- "scenes": a list of {min_sc} to {max_sc} objects, each with:
    "narration_fa": 3 to 5 flowing sentences of Persian narration (45-75 words) about ONE single idea or event,
    "source_en": the English sentence or two from the SOURCE TEXT that this paragraph is based on (copy it, at most 45 words),
    "characters_present": list of character names (from "characters") that this paragraph is about or that are visible in the picture (the character's fixed look is then drawn automatically), may be empty,
    "setting_en": where the picture takes place (concrete place, time, light),
    "action_en": exactly what is happening in the picture: who is doing what,
    "mood_en": the mood and color atmosphere.

Rules for the narration:
- Total narration: {words} Persian words. The source is already short: stay close to it, never pad.
- NO FILLER. Every sentence must carry a new concrete fact, event or idea from the source. No generic praise, no
  rhetorical questions, no empty transitions, no repeating the same point, no moral or lesson that is not in the source.
- Structure: (1) the first paragraph: what the novel is, who wrote it and when (only if the INTRODUCTION says so) and in
  one sentence what the story is about; (2) the next paragraph or two: introduce the main heroes by name, who each one is,
  how they are related to each other and what each one wants or fears at the start (only from the source); (3) the story in
  the SAME ORDER as the PLOT: only its turning points, each one linked to its cause, including the ending; (4) the closing
  wrap-up (next rule).
- MANDATORY, USEFUL wrap-up in the LAST 2 or 3 scenes. The first of them starts with the Persian words «در جمع‌بندی».
  It must give the listener something to keep, not repeat sentences: (a) in 2 or 3 flowing sentences tell the whole arc
  of the story from beginning to end; (b) then what the novel is really about and why it matters, ONLY from the
  INTRODUCTION and SIGNIFICANCE parts of the source (its themes, its reception, its influence); (c) the very last
  sentence is short and memorable. No new plot events, no empty praise.
- Every scene covers exactly ONE idea or event, so one picture can illustrate it fully.
- COVERAGE AND IMPORTANCE: follow your "outline": every one of its events must be told, in order, with its cause (the
  "because"), so a viewer who never read the book understands the WHOLE story, the cause of each event, how the main
  character changes, and the ending. Tell NOTHING else: for every sentence ask "if I delete it, does the story still make
  sense and does the chain of causes stay unbroken?" If yes, delete it. Never tell side episodes, minor characters, travel
  or description details, or any place or person that does not change what happens next.
- CONTINUITY: the first sentence of every paragraph picks up exactly where the last sentence of the previous paragraph
  stopped (same person, time or problem) through a connector that shows cause or time («برای همین»، «چند روز بعد»،
  «در نتیجه»، «اما»، «تا این‌که»); never start a paragraph from nowhere and never jump to a new subject without a bridge.
  Whenever a person appears for the first time after a gap, remind the viewer who he or she is in a few words.
- Translate the MEANING faithfully into natural Persian, never word by word.
{TONE_RULES}
{NAMES_RULE}
- The voice engine can fail on very long noun chains: at most TWO consecutive ezafe links in a noun phrase (never
  "noun of noun of noun of noun"); prefer verbs and clauses over long genitive chains.
- Correct grammar and spelling, proper half-spaces. It will be read aloud: no emojis, no brackets, no abbreviations.
- Never use English loanwords or words that sound English (for example «پادکست»، «ویدئو»، «آنلاین»، «لایک»،
  «اپلیکیشن»، «استایل»): use the Persian word. Never say the video is a podcast.
- Use everyday words that any listener understands at once. Avoid rare, archaic, heavy literary or Arabic words.
- Do not quote the book at length; paraphrase.

Rules for the visuals:
- Each picture must show EXACTLY what the narration says at that moment (concrete scene, people, objects, action).
- No text, letters, numbers, charts or logos in the pictures. No real celebrities. No scenes copied from films."""
        data = gemini_json(prompt, minutes=15, models=WRITE_MODELS)
        key = str(data.get("style_key") or "").strip().lower().replace(" ", "_").replace("-", "_")
        if key not in STYLES:
            key = "cartoon"                               # the robot named no valid style: safe default
        data["style_key"] = key
        data["art_style_en"] = STYLES[key][0]             # the exact words come from the fixed table
        print(f"Picture style: {key}", flush=True)
        if cast:                                          # the fixed, designed cast replaces the robot's own sheets
            data["characters"] = cast["characters"]
            data["era_en"] = cast["era_en"]
            okn = {c["name"] for c in cast["characters"]}
            for s in data.get("scenes") or []:
                if isinstance(s, dict):
                    s["characters_present"] = [n for n in (s.get("characters_present") or []) if n in okn]
        try:
            scenes = data["scenes"]
            ok = (
                len(scenes) >= max(5, min_sc - 2)
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
            data = review_script(data, book, src)       # ONE call: facts + wording/tone + voice text
            if CAST_SCENE:
                data = add_cast_scene(data)
            data = sanitize_narration(data, book)
            return align_visuals(data, book)
        time.sleep(5)
    raise RuntimeError("Could not get a valid script (with closing wrap-up) from Gemini")


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
   IMPORTANCE: also remove every sentence that tells a minor episode or detail whose removal does not break the chain of
   cause and effect of the story. Never remove a turning point, the introduction of a hero, the ending or the wrap-up.
2. FLOW AND WORDING: make the paragraph read as ONE connected story, not as separate short statements: join short
   sentences with connectors, make each sentence grow out of the previous one, and let the last sentence lead into the
   next paragraph. Also fix grammar, word choice, spelling and half-spaces. Do NOT add new facts. Keep the order and the
   number of paragraphs. Keep names in English (Latin) letters exactly as they are.
{TONE_RULES}
   Each paragraph 3-5 sentences, 45-75 words, at most TWO consecutive ezafe links in a noun phrase; numbers as words, no
   digits, no emojis, no brackets, no abbreviations, no English word except names; simple everyday words. A paragraph that
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


def align_visuals(data, book):
    """Step D: write ONE picture for EVERY beat (about 2 sentences) of the FINAL narration (skipped if it fails)."""
    scenes = data["scenes"]
    chars = data.get("characters", [])
    items = []
    for i, s in enumerate(scenes):
        s.pop("beat_visuals", None)
        if s.get("cast_names"):
            sents = split_sentences(s["narration_fa"])
            if len(sents) == len(s["cast_names"]):
                s["beats"] = sents
                s["beat_visuals"] = [cast_visual(chars, n) for n in s["cast_names"]]
                continue
            s.pop("cast_names")                       # sentences were changed: treat as an ordinary paragraph
        s["beats"] = make_beats(s["narration_fa"])
        for j, b in enumerate(s["beats"]):
            items.append({"i": len(items), "scene": i, "narration_fa": b, "source_en": s.get("source_en", "")})
    prompt = f"""You are a film storyboard artist. Below are the FINAL Persian narration parts, in order, of a video
that summarizes the book "{book['title_en']}" by {book['author_en']}. While each part is read aloud (about 10 seconds),
ONE picture is shown. For EACH part write the picture so that it illustrates the exact content of THAT part, so a
viewer who sees only the picture understands what is being said at that moment.

Return JSON: {{"visuals": [{{"i": 0, "characters_present": ["..."], "visual_en": "..."}}, ...]}}
with exactly {len(items)} items, in the same order.

Rules for "visual_en":
- English, 25 to 50 words. Start with the main subject doing the main action, then the place and time, then
  the important objects. Name 3 to 5 specific, concrete things from the part (people, objects, place,
  clothing, weather) that must be visible.
- Show LITERALLY what the part says at that moment, not a mood and not a general idea. One clear subject,
  one clear action, one place per picture. Two neighbouring parts must not get the same picture.
  For the closing wrap-up parts, show a symbolic scene of the book's central message.
- The image model does not know names: never write names or book titles in the picture description. When a part is
  about a character (even only as "he", "she" or by name), list him or her in "characters_present" and write only what
  the character DOES ("the boy sits alone on a bench"): his fixed look sheet is added automatically, so the face is
  the same in every picture. Show the character in the situation of that part (place, action, feeling).
- No text, letters, numbers, charts or logos. No real celebrities. No scenes copied from films.
- At most TWO named characters in one picture (more of them make the image model blend their faces and bodies); other
  people are unnamed (a crowd, a servant, a soldier), small or seen from behind. Never describe the faces, ages, bodies or
  clothes of the named characters (their fixed sheets are added automatically), and never show anything that does not
  belong to the era of the story.
- Do not write an art style (it is added separately).
"characters_present": names (from the characters list) visible in the picture, may be empty.

Characters: {json.dumps(chars, ensure_ascii=False)}
Parts: {json.dumps(items, ensure_ascii=False)}"""
    try:
        vis = gemini_json(prompt, minutes=8, models=WRITE_MODELS)["visuals"]
        if len(vis) == len(items) and all(isinstance(v.get("visual_en"), str) and v["visual_en"].strip()
                                          for v in vis):
            k = 0
            for s in scenes:
                if s.get("cast_names"):
                    continue
                n = len(s["beats"])
                s["beat_visuals"] = [{"visual_en": v["visual_en"].strip(),
                                      "characters_present": v.get("characters_present") or []}
                                     for v in vis[k:k + n]]
                k += n
            print(f"Visuals aligned: {len(items)} pictures for {len(scenes)} paragraphs.", flush=True)
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


_gtts = {"dead": False, "fails": 0}


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
        "model": GEMINI_TTS_MODEL,
        "input": [{"type": "user_input", "content": [content]}],
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": GEMINI_TTS_VOICES[kind]}]},
    }
    headers = {"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"}
    last = ""
    for attempt in range(3):
        try:
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
                return
            last = f"{r.status_code} {short_error(r.text)}"
            note(f"  Gemini TTS: {last}")
            if r.status_code == 429 and ("PerDay" in r.text or "limit: 0" in r.text):
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


def split_by_silence(path, weights):
    """Cuts ONE long recording of several paragraphs back into paragraphs: every cut goes to the longest pause that is
    near the expected place (expected place = share of the characters). Returns [(start, end), ...] in seconds."""
    total = probe_duration(path)
    out = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af",
                          "silencedetect=noise=-35dB:d=0.3", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", out)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", out)]
    sil = [(a, b) for a, b in zip(starts, ends)]
    wsum = float(sum(weights))
    cuts, acc, prev = [], 0.0, 0.0
    window = max(4.0, 0.08 * total)
    for w in weights[:-1]:
        acc += w
        t = total * acc / wsum
        best, best_score = t, None
        for a, b in sil:
            mid = (a + b) / 2
            if abs(mid - t) <= window and mid > prev + 1.0:
                score = (b - a) - 0.15 * abs(mid - t)
                if best_score is None or score > best_score:
                    best, best_score = mid, score
        best = max(best, prev + 1.0)
        cuts.append(best)
        prev = best
    edges = [0.0] + cuts + [total]
    return list(zip(edges[:-1], edges[1:]))


# Voice-only writing of the short words that the Gemini voice swallows or merges («در این» -> «دین»/«این»,
# «را از» -> «از»). Only these words get vowel marks, only in the text sent to the voice: subtitles stay clean.
_PL = "\u0600-\u06FF\u200c"
_VOICE_WORDS = (("در", "دَر"), ("این", "اِین"), ("را", "رَا"), ("از", "اَز"))


def gemini_voice_text(t):
    for plain, marked in _VOICE_WORDS:
        t = re.sub(rf"(?<![{_PL}]){plain}(?![{_PL}])", marked, t)
    return t


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
        cuts = split_by_silence(full, [max(len(t), 1) for t in texts])
        note("  Gemini group %d: cuts at %s s" % (gi, ", ".join(f"{b:.1f}" for _, b in cuts[:-1])))
        for i, (a, b) in zip(idxs, cuts):
            sh(["ffmpeg", "-y", "-loglevel", "error", "-i", str(full), "-ss", f"{a:.2f}", "-to", f"{b:.2f}",
                str(WORK / f"raw_{i:02d}.wav")])
        if gi < len(groups) - 1:
            time.sleep(25)                       # free tier: 3 requests per minute
    return [WORK / f"raw_{i:02d}.wav" for i in range(len(scenes))]


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


GEMINI_VIDEOS_PER_DAY = int(os.environ.get("AZIM_GEMINI_VIDEOS") or 2)   # free Gemini TTS: 10 requests/day = 2 videos
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
    if gemini_tts_ready() and gemini_voice_allowed():
        try:
            durs = _make_voice(scenes, kind, names, True)
            VOICE_USED["engine"] = "gemini"
            return durs
        except GeminiTTSUnavailable as e:
            note(f"Gemini TTS could not read the whole video ({str(e)[:100]}): the WHOLE video is read again "
                 "with Pocket TTS, so only one voice is heard.")
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
    """The style comes FIRST (the end of a long prompt can be cut); then the fixed character sheets, the picture, and
    the era so that clothes and objects always fit the time of the story."""
    names = scene.get("characters_present") or []
    present = [c for c in chars if isinstance(c, dict) and c.get("name") in names]
    looks = " ".join(f"{c['name']}: {c['look_en']}." for c in present)
    if len(present) > 1:
        looks += " These people look completely different from each other: different faces, ages, body builds and clothes."
    era_full = str(scene.get("era_en") or "").strip()

    def compose(era):
        era_part = (f"Setting: {era}. Everything shown (clothes, buildings, objects) belongs to that time. "
                    if era else "")
        chars_part = (f"Characters (always with exactly this face, body and clothes): {looks} " if looks else "")
        if scene.get("visual_en"):
            main = f"{scene['visual_en']} "
        else:
            main = (f"{scene['action_en']}. Setting: {scene['setting_en']}. "
                    f"Mood: {scene.get('mood_en', 'cinematic')}. ")
        return f"Art style: {art}. {chars_part}{main}{era_part}Wide 16:9 composition, {CLEAN}"

    out = compose(era_full)
    if len(out) > 1850:                                   # keep the style and the characters: shorten the era
        out = compose(era_full.split(". Clothing of that time")[0])
    return out


def _build_prompt_old(art, chars, scene):
    names = scene.get("characters_present") or []
    looks = "; ".join(f"{c['name']}: {c['look_en']}" for c in chars
                      if isinstance(c, dict) and c.get("name") in names)
    if scene.get("visual_en"):
        return ((f"Characters (always with exactly this face and clothes): {looks}. " if looks else "")
                + f"{scene['visual_en']} "
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
        r = gemini_json(prompt, minutes=2, models=CHEAP_MODELS, image=path.read_bytes())
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
    for retry in range(2):
        res = check_image(path, scene)
        if res is None:
            return                   # the check is not available: keep the current picture
        score, missing = res
        print(f"Image check: score {score}/10 {('- ' + missing) if score < 6 else ''}", flush=True)
        if best is None or score > best[0]:
            best = (score, path.read_bytes())
        if score >= 6 or retry == 1:
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


_font_cmap = None


def font_chars():
    """Code points of the subtitle font (to find characters that would be drawn as rectangles)."""
    global _font_cmap
    if _font_cmap is None:
        _font_cmap = set()
        try:
            from fontTools.ttLib import TTFont
            path = subprocess.check_output(["fc-match", "-f", "%{file}", SUB_FONT], text=True).strip()
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


def setup_sub_font():
    """Copies ONE tested font into azim_work/azim_fonts: the lam-alef «لا» ligature (U+FEFB) and all Persian letters
    must exist in it, otherwise another installed font that has them is chosen. Returns the font family name."""
    from fontTools.ttLib import TTFont
    need = [0xFEFB, 0x0644, 0x0627, 0x067E, 0x0686, 0x0698, 0x06A9, 0x06AF, 0x06CC, 0x200C]
    cands = []
    try:
        cands.append(subprocess.check_output(["fc-match", "-f", "%{file}", SUB_FONT], text=True).strip())
        out = subprocess.check_output(["fc-list", ":charset=fefb 644 627 67e 686 698 6a9 6af 6cc", "file"], text=True)
        cands += [x.strip().rstrip(":") for x in out.splitlines() if x.strip()]
    except Exception as e:  # noqa: BLE001
        print("Font search failed:", e, flush=True)
    cands.append("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    pref = ("dejavusans.ttf", "vazir", "notonaskharabic", "notosansarabic")
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
            note(f"Subtitle font = {fam} ({path}); ZWNJ in font: {need[-1] in cm}")
            return fam
        except Exception as e:  # noqa: BLE001
            print("Font skipped:", path, e, flush=True)
    return SUB_FONT


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
            inc = 0.12 / max(frames, 1)    # zoom speed spread over the whole picture (no early stop)
            zoom = (f"z='min(zoom+{inc:.6f},1.12)'" if (i + j) % 2 == 0
                    else f"z='if(eq(on,1),1.12,max(zoom-{inc:.6f},1.0))'")
            # big lanczos upscale BEFORE zoompan (no shimmer/blur while zooming) + light sharpening
            vf = ("scale=2560:1440:flags=lanczos:force_original_aspect_ratio=increase,crop=2560:1440,"
                  "unsharp=5:5:0.6:5:5:0.0,"
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

        # subtitles: always the clean text (no diacritics), cut into sentences
        sents = split_sentences(subtitle_text(s["narration_fa"], names))
        weights = [max(len(x), 1) for x in sents]
        total_w = sum(weights)
        acc = 0
        for x, w in zip(sents, weights):
            start = t + d * acc / total_w
            acc += w
            end = t + d * acc / total_w + (PAD * 0.5 if acc == total_w else 0)
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
    style = (f"FontName={font_family},FontSize={SUB_FONT_SIZE},PrimaryColour=&H00FFFFFF,"
             "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=28")
    # the only lossy pass: best quality that still fits under Telegram's 50 MB limit
    fits = False
    for crf, maxrate in (("23", "720k"), ("27", "520k"), ("31", "380k")):
        sh(["ffmpeg", "-y", "-loglevel", "error", "-i", "joined.mp4",
            "-vf", f"subtitles=subs.srt:fontsdir=azim_fonts:force_style='{style}'",
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
def get_book_and_source(posted, rejected):
    """Next novel that has a usable Plot section on Wikipedia. A novel without one is rejected for good
    (never summarized from memory, never tried again)."""
    tries = 60 if BOOKS_FILE.exists() else 6
    for _ in range(tries):
        book = pick_book(posted, rejected)
        print("Book candidate:", book["title_en"], "|", book["book_type"], flush=True)
        src = fetch_source(book)
        if src:
            return book, src
        reject_book(rejected, book, "no usable Plot section on Wikipedia")
    raise RuntimeError("Could not find a novel with a usable Plot section on Wikipedia")


def main():
    posted 
