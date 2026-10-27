import base64
import json
import math
import os
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
BOOKS_FILE = ROOT / "azim_books_list.json"        # 2000 novels, built once by azim_make_booklist.py
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
SUB_FONT = "Noto Sans Arabic"
SCRIPT_VERSION = 4        # a saved script of another version is not resumed

# ----- narrator engine: "gemini" (Gemini TTS, default; any failure or finished quota -> Pocket TTS for that scene)
# ----- or "pocket" (offline Pocket TTS only). Switch without editing code: repository variable AZIM_TTS.
TTS_ENGINE = (os.environ.get("AZIM_TTS") or "gemini").strip().lower()
GEMINI_TTS_MODEL = os.environ.get("AZIM_GEMINI_TTS_MODEL") or "gemini-3.8-flash-tts"
GEMINI_TTS_VOICES = {"female": os.environ.get("AZIM_GEMINI_VOICE_FEMALE") or "Sulafat",
                     "male": os.environ.get("AZIM_GEMINI_VOICE_MALE") or "Gacrux"}
GEMINI_TTS_TEXT = (os.environ.get("AZIM_GEMINI_TTS_TEXT") or "plain").strip().lower()   # plain | spoken
GEMINI_TTS_STYLE = os.environ.get("AZIM_GEMINI_TTS_STYLE") or (
    "a warm, calm storyteller telling a story to one close listener, with real feeling that follows the story: "
    "curious at the start, tense when things get hard, tender or sad where the story is, quietly hopeful at the "
    "end; natural pace and breathing, never a news-reader voice")

# ----- sources: full text of public-domain novels (Project Gutenberg) + other-language Wikipedia plots
USE_GUTENBERG = os.environ.get("AZIM_GUTENBERG", "1") != "0"
MAX_FULLTEXT_WORDS = int(os.environ.get("AZIM_FULLTEXT_MAX_WORDS") or 130000)

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


SIG_KEYS = re.compile(r"legacy|influence|reception|critical|significance|impact|awards?|controvers|cultural|themes?", re.I)


def pick_significance(text):
    """Reception / legacy / influence sections: facts for the closing wrap-up (why the novel matters)."""
    lead, secs = split_wiki(text)
    chosen = [(t, b) for t, b in secs if len(b) >= 80 and SIG_KEYS.search(t)]
    return "\n\n".join(f"[{t}]\n{b}" for t, b in chosen)


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
    return {"title": title, "lead": lead, "body": body, "sig": pick_significance(text) if lang == "en" else ""}


def _ascii_words(s):
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.findall(r"[a-z0-9]+", s)


def gutenberg_find(book):
    """The Project Gutenberg edition of a public-domain novel (via the Gutendex catalog API), or None."""
    title_words = [w for w in _ascii_words(book["title_en"]) if w not in ("the", "a", "an")]
    last = (_ascii_words(book["author_en"]) or [""])[-1]
    if not title_words or not last:
        return None
    try:
        r = requests.get("https://gutendex.com/books/", headers=WIKI_HEADERS, timeout=40,
                         params={"search": f"{book['title_en']} {book['author_en']}", "languages": "en"})
        if r.status_code != 200:
            return None
        results = r.json().get("results", [])
    except Exception as e:  # noqa: BLE001
        print("Gutendex error:", e, flush=True)
        return None
    best = None
    for it in results:
        got = set(_ascii_words(it.get("title", "")))
        authors = " ".join(a.get("name", "") for a in it.get("authors", []))
        if not set(title_words) <= got or last not in _ascii_words(authors) or it.get("copyright") is True:
            continue
        fmts = it.get("formats", {})
        url = next((u for k, u in fmts.items() if k.startswith("text/plain") and not u.endswith(".zip")), None)
        cand = (it.get("download_count", 0), it["id"], url)
        if best is None or cand[0] > best[0]:
            best = cand
    return {"id": best[1], "url": best[2]} if best else None


def gutenberg_text(found):
    """Plain text of the book without the Gutenberg license header/footer. Mirror first, politely (2 s delay)."""
    gid = str(found["id"])
    path = "/".join(gid[:-1]) if len(gid) > 1 else "0"
    base = f"https://aleph.gutenberg.org/{path}/{gid}/"
    urls = [base + f"{gid}-0.txt", base + f"{gid}.txt", base + f"{gid}-8.txt"]
    if found.get("url"):
        urls.append(found["url"])
    for u in urls:
        time.sleep(2)
        try:
            r = requests.get(u, headers=WIKI_HEADERS, timeout=90)
            if r.status_code == 200 and len(r.content) > 50000:
                r.encoding = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else "utf-8"
                text = r.text
                m = re.search(r"\*\*\* ?START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*", text, re.I | re.S)
                if m:
                    text = text[m.end():]
                m = re.search(r"\*\*\* ?END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK", text, re.I)
                if m:
                    text = text[:m.start()]
                return text.strip()
        except requests.RequestException as e:
            print(f"Gutenberg download error ({u}): {e}", flush=True)
    return None


def fetch_fulltext_summary(book):
    """For a public-domain novel: ONE Gemini call reads the whole novel and writes a detailed chronological
    plot summary (English). Any failure -> None (the Wikipedia source is used). Quota exhaustion is raised."""
    found = gutenberg_find(book)
    if not found:
        return None
    text = gutenberg_text(found)
    if not text:
        print("Gutenberg text could not be downloaded: Wikipedia only.", flush=True)
        return None
    words = len(text.split())
    if words < 8000 or words > MAX_FULLTEXT_WORDS:
        print(f"Gutenberg text has {words} words: outside the limits, Wikipedia only.", flush=True)
        return None
    print(f"Gutenberg full text: {words} words (ebook {found['id']}). Summarizing it with Gemini...", flush=True)
    prompt = f"""You are a careful literary analyst. Below is the COMPLETE text of the novel "{book['title_en']}" by
{book['author_en']}. Write, in English:
1. "plot_summary": a detailed, strictly chronological plot summary of 700 to 1000 words that a storyteller can retell:
   the main characters (names exactly as in the text), where and when the story begins, every important event in order
   with its cause and consequence, the turning points, and how it ends. Show what the characters feel when the text
   shows it.
2. "themes": 3 to 5 short sentences about the central themes that the text itself makes visible.
Use ONLY what the text says: no outside knowledge, no opinions about fame or reception, nothing invented.
Return JSON {{"plot_summary": "...", "themes": "..."}}

NOVEL TEXT:
{text}"""
    try:
        out = gemini_json(prompt, minutes=8, models=WRITE_MODELS, read_timeout=300)
        plot = str(out.get("plot_summary", "")).strip()
        if len(plot.split()) < 400:
            print("Full-text summary too short: Wikipedia only.", flush=True)
            return None
        return {"summary": plot, "themes": str(out.get("themes", "")).strip(), "words": len(plot.split()),
                "id": found["id"]}
    except GeminiQuotaExhausted:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"Full-text summary failed ({str(e)[:100]}): Wikipedia only.", flush=True)
        return None


OTHER_LANG_KEYS = {
    "fr": r"résumé|intrigue|synopsis|argument|histoire|trame|contenu",
    "de": r"handlung|inhalt|zusammenfassung|inhaltsangabe",
    "es": r"argumento|resumen|sinopsis|trama|contenido",
    "ru": r"сюжет|содержание|краткое",
    "it": r"trama|riassunto|sinossi|contenuto",
    "ar": r"ملخص|القصة|الحبكة|الأحداث|قصة",
}
OTHER_LANG_SKIP = re.compile(r"adaptation|adaptaci|adattament|экранизац|اقتباس|réception|critique|rezeption|recepci|"
                             r"recension|kritik|влияние|ترجمة|édition|ausgabe|référence|einzelnachweise", re.I)


def fetch_other_languages(en_title, limit=2):
    """Plot sections of the same novel in other Wikipedias (often longer than the English one)."""
    try:
        d = wiki_api("en", {"action": "query", "prop": "langlinks", "lllimit": "max", "redirects": 1,
                            "titles": en_title})
        links = {ll["lang"]: ll["title"] for ll in d["query"]["pages"][0].get("langlinks", [])}
    except Exception as e:  # noqa: BLE001
        print("Language links error:", e, flush=True)
        return []
    out = []
    for lang, pattern in OTHER_LANG_KEYS.items():
        if lang not in links:
            continue
        found = wiki_fulltext(lang, links[lang])
        if not found:
            continue
        lead, secs = split_wiki(found[1])
        body = "\n\n".join(f"[{t}]\n{b}" for t, b in secs
                           if len(b) >= 200 and re.search(pattern, t, re.I) and not OTHER_LANG_SKIP.search(t))
        if len(body) >= 700:
            out.append((lang, body))
    out.sort(key=lambda x: -len(x[1]))
    return out[:limit]


def fetch_source(book):
    """Facts for the video: the FULL TEXT summary (public-domain novels) + the novel's Wikipedia articles
    (English, Persian and other languages). Nothing comes from memory."""
    en = fetch_wiki(book, "en")
    full = fetch_fulltext_summary(book) if USE_GUTENBERG else None
    en_ok = bool(en) and len(en["body"]) >= 1200
    if not en_ok and not full:
        print("No usable plot source for this novel.", flush=True)
        return None
    fa = fetch_wiki(book, "fa")
    parts = []
    if en:
        parts.append("ENGLISH WIKIPEDIA - INTRODUCTION:\n" + en["lead"][:1600])
    if full:
        parts.append("FULL-TEXT SUMMARY (written from the complete text of the novel; the most detailed and reliable "
                     "source for the events):\n" + full["summary"]
                     + ("\n\nTHEMES SEEN IN THE TEXT:\n" + full["themes"] if full["themes"] else ""))
    if en_ok:
        parts.append("ENGLISH WIKIPEDIA - CONTENT SECTIONS:\n" + en["body"][:(4000 if full else 7000)])
    if en and en.get("sig"):
        parts.append("ENGLISH WIKIPEDIA - SIGNIFICANCE (reception, legacy, influence: use ONLY for the closing "
                     "wrap-up, to say why the novel matters):\n" + en["sig"][:1800])
    others = fetch_other_languages(en["title"]) if en else []
    for lang, body in others:
        parts.append(f"WIKIPEDIA ({lang}) PLOT - extra details; use only what does not contradict the sources above:\n"
                     + body[:3000])
    if fa and len(fa["body"]) > 400:
        parts.append("PERSIAN WIKIPEDIA (use its Persian names and wording when it agrees with the English):\n"
                     + fa["lead"][:700] + "\n" + fa["body"][:2500])
    title = en["title"] if en else book["title_en"]
    url = ("https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))) if en else ""
    credit = "منبع خلاصه: ویکی‌پدیا (CC BY-SA)"
    if full:
        url = (url + "\n" if url else "") + f"https://www.gutenberg.org/ebooks/{full['id']}"
        credit = "منبع خلاصه: متن کامل رمان از پروژه گوتنبرگ (دامنهٔ عمومی) و ویکی‌پدیا (CC BY-SA)"
    langs = ", ".join(l for l, _ in others)
    print(f"Source: wikipedia={'yes' if en_ok else 'no'}, full text={'yes' if full else 'no'}, "
          f"other languages=[{langs}]", flush=True)
    return {"text": "\n\n".join(parts),
            "en_words": max(len(en["body"].split()) if en_ok else 0, full["words"] if full else 0),
            "url": url, "title": title, "credit": credit}


def plan_length(src):
    """Shorter and denser: every paragraph (scene) is 45-75 words of flowing story."""
    target = max(260, min(520, int(src["en_words"] * 0.6)))
    if not USE_CF:                    # slow free images: fewer scenes
        target = min(target, 360)
    n = max(5, round(target / 55))
    lo, hi = max(5, n - 1), n + 2
    return lo, hi, f"{int(target * 0.9)} to {int(target * 1.1)}"


# ---------------------------------------------------------------- script
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


def write_scenes(book, src, plan):
    """Step B: write the Persian narration from the source text only (no outside facts)."""
    min_sc, max_sc, words = plan
    for _ in range(4):
        prompt = f"""You are a gifted Iranian storyteller and radio host. Write the script of a video that summarizes the
novel "{book['title_en']}" by {book['author_en']} for a Persian-speaking audience.

Use ONLY the facts in the SOURCE TEXT below (taken from Wikipedia). Never add a fact, name, date, number,
quote or opinion that is not in the source. If the source says nothing about something, say nothing about it.

SOURCE TEXT:
{src['text']}

Return JSON with exactly these keys:
- "art_style_en": ONE consistent visual style for all images of this video, either painterly
  (for example "digital oil painting, rich colors, clean sharp brushwork") or cartoon
  (for example "stylish 2D cartoon illustration, bold clean outlines, flat vibrant colors"), chosen to fit this novel.
  The style must be crisp and clean: never use words like glow, haze, bloom, dreamy, soft focus or mist.
- "characters": a list of the recurring characters, each {{"name": "...", "look_en": "detailed fixed visual
  description in English: age, face, hair, clothes, colors"}}. Empty list if there are no characters.
  Base the look on what the source says; if it says nothing, choose a fitting look for the era and keep it fixed.
- "scenes": a list of {min_sc} to {max_sc} objects, each with:
    "narration_fa": 3 to 5 flowing sentences of Persian narration (45-75 words) about ONE single idea or event,
    "source_en": the English sentence or two from the SOURCE TEXT that this paragraph is based on (copy it, at most 45 words),
    "characters_present": list of character names (from "characters") visible in the picture, may be empty,
    "setting_en": where the picture takes place (concrete place, time, light),
    "action_en": exactly what is happening in the picture: who is doing what,
    "mood_en": the mood and color atmosphere.

Rules for the narration:
- Total narration: {words} Persian words. Short and dense: if the source has less to say, write less.
- NO FILLER. Every sentence must carry a new concrete fact, event or idea from the source. No generic praise, no
  rhetorical questions, no empty transitions, no repeating the same point, no moral or lesson that is not in the source.
- Structure: (1) the first paragraph: what the novel is, who wrote it and when (only if the source says so) and the
  situation where the story begins; (2) the story in the SAME ORDER as the source, covering ALL its key events, including
  the ending; (3) the closing wrap-up (next rule).
- MANDATORY, USEFUL wrap-up in the LAST 2 or 3 scenes. The first of them starts with the Persian words «در جمع‌بندی».
  It must give the listener something to keep, not repeat sentences: (a) in 2 or 3 flowing sentences tell the whole arc
  of the story from beginning to end; (b) then what the novel is really about and why it matters, ONLY from the source
  (its themes, its reception, its influence: see the SIGNIFICANCE part of the source if present); (c) the very last
  sentence is short and memorable. No new plot events, no empty praise.
- Every scene covers exactly ONE idea or event, so one picture can illustrate it fully.
- Translate the MEANING faithfully into natural Persian, never word by word. If a Persian Wikipedia text is given,
  use its Persian terms when they agree with the English.
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
    raise RuntimeError(f"Gemini TTS failed: {last[:120]}")


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


def phonetic_text(text, names):
    """Fallback: English names replaced by their Persian phonetic spelling (read by the Persian voice)."""
    for k in sorted(names, key=len, reverse=True):
        text = text.replace(k, names[k])
    return text


def make_voice(scenes, kind, names=None):
    print(f"Voice: {'Gemini TTS' if gemini_tts_ready() else 'Pocket TTS Farsi v2'} ({kind}), scenes: {len(scenes)}", flush=True)
    (WORK / "tts_out").mkdir(exist_ok=True)
    names = names or {}
    wavs = []

    def speak(text, p):
        try:
            tts_mixed(text, kind, p)
        except EnglishVoiceError:
            ph = phonetic_text(text, names)
            note("  English voice not available: this scene uses the Persian phonetic spelling of the names.")
            tts_text(ph, kind, p)

    for i, s in enumerate(scenes):
        p = WORK / f"raw_{i:02d}.wav"
        plain = s["narration_fa"].strip()
        spoken = (s.get("spoken_fa") or plain).strip()
        REWRITTEN.clear()
        if gemini_tts_ready():
            try:
                gemini_tts(spoken if GEMINI_TTS_TEXT == "spoken" else plain, kind, p, tts_style(s))
                wavs.append(p)
                time.sleep(3)
                continue
            except Exception as e:  # noqa: BLE001
                note(f"  Scene {i}: Gemini TTS not used ({str(e)[:100]}); Pocket TTS reads this scene.")
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


def clean_subtitle(text):
    """Remove everything the subtitle font cannot draw (it would show as a rectangle): invisible format characters,
    and characters missing from the font. Latin letters are left to the font fallback (they are in names)."""
    cm = font_chars()
    out = []
    for ch in unicodedata.normalize("NFC", text):
        cp = ord(ch)
        cat = unicodedata.category(ch)
        if ch == "\u200c":                       # half-space: keep only if the font really has it
            if cm and cp in cm:
                out.append(ch)
            else:
                _dropped_chars[cp] = _dropped_chars.get(cp, 0) + 1
            continue
        if cat == "Cf" or cat == "Co" or cat == "Cn":
            _dropped_chars[cp] = _dropped_chars.get(cp, 0) + 1
            continue
        if cat == "Cc":
            out.append(" ")
            continue
        if cm and cp >= 0x250 and cp not in cm and not ch.isspace():
            _dropped_chars[cp] = _dropped_chars.get(cp, 0) + 1
            continue
        out.append(ch)
    return " ".join("".join(out).split())


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
        sents = split_sentences(clean_subtitle(DIAC.sub("", s["narration_fa"])))
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

    if _dropped_chars:
        note("Subtitle characters removed (would be rectangles): "
             + ", ".join(f"U+{c:04X} x{n}" for c, n in sorted(_dropped_chars.items())))
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
def get_book_and_source(posted, rejected):
    """Next novel that has a usable Wikipedia article. A novel without one is rejected for good
    (never summarized from memory, never tried again)."""
    tries = 40 if BOOKS_FILE.exists() else 6
    for _ in range(tries):
        book = pick_book(posted, rejected)
        print("Book candidate:", book["title_en"], "|", book["book_type"], flush=True)
        src = fetch_source(book)
        if src:
            return book, src
        reject_book(rejected, book, "no reliable Wikipedia source")
    raise RuntimeError("Could not find a novel with a usable Wikipedia source")


def main():
    posted = load_json(POSTED_FILE, [])
    rejected = load_json(REJECTED_FILE, [])
    if len(posted) >= TOTAL_BOOKS:
        print("All books are done.")
        return

    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    print(f"Image service: {'Cloudflare FLUX' if USE_CF else 'Pollinations (slow)'}; "
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
            print(f"Plan: {plan[0]}-{plan[1]} scenes, {plan[2]} words (from {src['en_words']} source words)",
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

    (WORK / "azim_source.txt").write_text(src["text"], encoding="utf-8")
    scenes = script["scenes"]
    words = sum(len(s["narration_fa"].split()) for s in scenes)
    print(f"Script: {len(scenes)} scenes, {words} words, style: {script['art_style_en']}", flush=True)
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
    durs = make_voice(scenes, kind, script.get("names") or {})

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
               f"{src.get('credit') or 'منبع خلاصه: ویکی‌پدیا (CC BY-SA)'}\n{src['url']}")
    send_video(final, caption)

    posted.append({"title_en": book["title_en"], "title_fa": book["title_fa"],
                   "type": "novel", "voice": kind, "wiki": src["title"], "date": date.today().isoformat()})
    save_json(POSTED_FILE, posted)
    PENDING_FILE.unlink(missing_ok=True)
    shutil.rmtree(WORK, ignore_errors=True)
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except GeminiQuotaExhausted as exc:
        print(f"STOP: the daily Gemini quota is finished for every model ({exc}). "
              "No video now; the next run continues.", flush=True)
        sys.exit(1)
