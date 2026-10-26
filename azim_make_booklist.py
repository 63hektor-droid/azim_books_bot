"""
azim_make_booklist.py  -  builds azim_books_list.json: the 2000 most famous NOVELS, ordered by fame.

Run it ONCE (workflow "azim-make-booklist", manual start). It uses almost no Gemini quota:
  1. Wikipedia categories of novels  -> candidate novels
  2. Wikipedia page views (last 60 days) -> fame ranking
  3. For the top ones: the English article must have a real plot/summary section (>= 1200 characters)
     and its first lines must say that it is a novel
  4. Persian title: Persian Wikipedia language link (or Wikidata label); author: Wikidata (property P50)
  5. Only what is still missing (a few Persian names) is asked from Gemini, in small batches

Output: a JSON list of {title_en, title_fa, author_en, author_fa, wiki_en, wiki_fa, views}.
"""
import json
import os
import re
import sys
import time
from collections import deque
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).parent
OUT = ROOT / "azim_books_list.json"
TARGET = int(os.environ.get("AZIM_BOOKS_TARGET", "2000"))
POOL = int(os.environ.get("AZIM_BOOKS_POOL", "5000"))          # how many ranked candidates are checked at most
MAX_CATS = int(os.environ.get("AZIM_BOOKS_MAX_CATS", "2500"))
MAX_DEPTH = int(os.environ.get("AZIM_BOOKS_DEPTH", "3"))
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODELS = ["gemini-flash-lite-latest", "gemini-flash-latest"]

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "azim-booklist/1.0 (educational book summaries; GitHub Actions)"})


def log(*a):
    print(*a, flush=True)


def api(host, params, tries=4):
    p = dict(params)
    p.update({"format": "json", "formatversion": 2})
    for t in range(tries):
        try:
            r = SESSION.get(f"https://{host}/w/api.php", params=p, timeout=60)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(5 * (t + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(3 * (t + 1))
    return None


def wiki(params):
    return api("en.wikipedia.org", params)


# ---------------------------------------------------------------- 1. candidates from categories
SEEDS = [
    "Category:Novels by country", "Category:Novels by genre", "Category:Novels by language",
    "Category:19th-century novels", "Category:20th-century novels", "Category:21st-century novels",
    "Category:18th-century novels", "Category:Novels by year",
]
SKIP_CAT = re.compile(
    r"adaptation|film|television|series|characters?|fictional|lists?\b|awards?|winners|novellas|comics|manga|anime|"
    r"video game|stage|plays|musicals|operas|screenplay|illustrat|writers|authors|publishers|translat|"
    r"sequels|trilog|franchise|universe|merchandise|parod|fan fiction|templates|stubs|wikiproject|articles|"
    r"images|books about|bibliograph|characters|settings|by author|children|young adult|erotic|pornograph|"
    r"short stor|poetry|non-fiction|nonfiction|essays|plays|radio|audio|graphic|comic", re.I)
BAD_TITLE = re.compile(
    r"^(List of|Lists of|Category:|Template:|Portal:|Outline of)|"
    r"\((film|TV|miniseries|series|disambiguation|franchise|comics|manga|anime|video game|play|musical|opera|"
    r"album|song|band|character|fictional)", re.I)


def members(cat):
    subs, pages, cont = [], [], {}
    while True:
        d = wiki({"action": "query", "list": "categorymembers", "cmtitle": cat, "cmlimit": 500,
                  "cmtype": "page|subcat", **cont})
        if not d or "query" not in d:
            break
        for m in d["query"].get("categorymembers", []):
            (subs if m.get("ns") == 14 else pages).append(m["title"])
        if "continue" in d:
            cont = d["continue"]
        else:
            break
    return subs, pages


def crawl():
    seen, pages = set(), {}
    q = deque((s, 0) for s in SEEDS)
    while q and len(seen) < MAX_CATS:
        cat, depth = q.popleft()
        if cat in seen:
            continue
        seen.add(cat)
        subs, pgs = members(cat)
        for t in pgs:
            if not BAD_TITLE.search(t):
                pages[t] = 1
        if depth < MAX_DEPTH:
            for s in subs:
                if not SKIP_CAT.search(s) and s not in seen:
                    q.append((s, depth + 1))
        if len(seen) % 50 == 0:
            log(f"  categories: {len(seen)}, novels found so far: {len(pages)}")
        time.sleep(0.05)
    log(f"Crawl done: {len(seen)} categories, {len(pages)} candidate pages.")
    return list(pages)


# ---------------------------------------------------------------- 2. fame (page views)
def rank_by_views(titles):
    views = {}
    for i in range(0, len(titles), 50):
        d = wiki({"action": "query", "prop": "pageviews", "pvipdays": 60, "redirects": 1,
                  "titles": "|".join(titles[i:i + 50])})
        if d and "query" in d:
            for p in d["query"].get("pages", []):
                pv = p.get("pageviews") or {}
                total = sum(v for v in pv.values() if v)
                if total:
                    views[p["title"]] = max(views.get(p["title"], 0), total)
        if (i // 50) % 40 == 0:
            log(f"  page views: {i}/{len(titles)}")
        time.sleep(0.05)
    ranked = sorted(views.items(), key=lambda kv: -kv[1])
    log(f"Ranked {len(ranked)} pages by views.")
    return ranked


# ---------------------------------------------------------------- 3. the article must have a real plot
EN_KEYS = re.compile(r"\b(plot|synopsis|summary|contents?|overview|premise|story|narrative|themes?|"
                     r"main ideas?|ideas|structure|chapters?|argument|thesis|characters?|description)\b", re.I)
EN_SKIP = re.compile(r"adaptation|film|television|reception|legacy|influence|critic|award|sequel|cultural|"
                     r"publication|translation|see also|references", re.I)
HEAD = re.compile(r"^(={2,6})\s*(.+?)\s*\1\s*$", re.M)


def plot_info(title):
    d = wiki({"action": "query", "prop": "extracts", "explaintext": 1, "redirects": 1, "titles": title})
    try:
        p = d["query"]["pages"][0]
        text = p.get("extract") or ""
    except Exception:  # noqa: BLE001
        return None
    if not text:
        return None
    ms = list(HEAD.finditer(text))
    lead = text[:ms[0].start()] if ms else text
    if not re.search(r"\bnovel", lead[:600], re.I):
        return None                      # not a novel (non-fiction, short stories, series page...)
    body_len, words, cur, parts = 0, 0, None, []
    for i, m in enumerate(ms):
        level, head = len(m.group(1)), m.group(2).strip()
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        chunk = text[m.end():end].strip()
        if level == 2:
            cur = bool(EN_KEYS.search(head) and not EN_SKIP.search(head))
        if cur:
            body_len += len(chunk)
            words += len(chunk.split())
    if body_len < 1200:
        return None
    return {"wiki_en": p["title"], "en_words": words}


# ---------------------------------------------------------------- 4. Persian names (Wikipedia + Wikidata)
def strip_paren(s):
    return re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()


def wikidata(params):
    return api("www.wikidata.org", params)


def enrich(items):
    """Adds title_fa/wiki_fa (Persian Wikipedia link) and author_en/author_fa (Wikidata P50)."""
    qid, fa_link = {}, {}
    titles = [it["wiki_en"] for it in items]
    for i in range(0, len(titles), 50):
        d = wiki({"action": "query", "prop": "langlinks|pageprops", "lllang": "fa", "lllimit": "max",
                  "ppprop": "wikibase_item", "redirects": 1, "titles": "|".join(titles[i:i + 50])})
        if d and "query" in d:
            for p in d["query"].get("pages", []):
                if (p.get("pageprops") or {}).get("wikibase_item"):
                    qid[p["title"]] = p["pageprops"]["wikibase_item"]
                for ll in p.get("langlinks") or []:
                    if ll.get("lang") == "fa":
                        fa_link[p["title"]] = ll.get("title") or ll.get("*")
        time.sleep(0.05)
    log(f"Wikidata ids: {len(qid)}, Persian Wikipedia links: {len(fa_link)}")

    author_of = {}
    qids = sorted(set(qid.values()))
    for i in range(0, len(qids), 50):
        d = wikidata({"action": "wbgetentities", "ids": "|".join(qids[i:i + 50]), "props": "claims"})
        for q, ent in ((d or {}).get("entities") or {}).items():
            try:
                for c in (ent.get("claims") or {}).get("P50", []):
                    a = c["mainsnak"]["datavalue"]["value"]["id"]
                    author_of[q] = a
                    break
            except (KeyError, TypeError):
                continue
        time.sleep(0.05)

    labels = {}
    need = sorted(set(qid.values()) | set(author_of.values()))
    for i in range(0, len(need), 50):
        d = wikidata({"action": "wbgetentities", "ids": "|".join(need[i:i + 50]), "props": "labels",
                      "languages": "fa|en"})
        for q, ent in ((d or {}).get("entities") or {}).items():
            lab = ent.get("labels") or {}
            labels[q] = {"fa": (lab.get("fa") or {}).get("value", ""), "en": (lab.get("en") or {}).get("value", "")}
        time.sleep(0.05)

    for it in items:
        t = it["wiki_en"]
        q = qid.get(t)
        it["wiki_fa"] = fa_link.get(t, "")
        title_fa = strip_paren(it["wiki_fa"]) if it["wiki_fa"] else ""
        if not title_fa and q:
            title_fa = labels.get(q, {}).get("fa", "")
        a = author_of.get(q)
        it["title_en"] = strip_paren(t)
        it["title_fa"] = title_fa
        it["author_en"] = labels.get(a, {}).get("en", "") if a else ""
        it["author_fa"] = labels.get(a, {}).get("fa", "") if a else ""
    return items


# ---------------------------------------------------------------- 5. Gemini only for what is still missing
def gemini_fill(batch):
    """batch: list of items missing title_fa / author_en / author_fa. Fills them in place. Returns count filled."""
    rows = [{"i": i, "title_en": it["title_en"], "author_en": it.get("author_en", ""),
             "title_fa": it.get("title_fa", ""), "author_fa": it.get("author_fa", "")} for i, it in enumerate(batch)]
    prompt = f"""For each famous novel below give its Persian title (the form commonly used in Iran), the author's name in
English and the author's name in Persian (the form commonly used in Iran). Keep a value that is already given. If you
are not sure about a value, return "" for it. Never invent.
Return JSON {{"items": [{{"i": 0, "title_fa": "", "author_en": "", "author_fa": ""}}, ...]}}.
Novels: {json.dumps(rows, ensure_ascii=False)}"""
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}
    headers = {"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"}
    for attempt in range(6):
        for model in GEMINI_MODELS:
            try:
                r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                  headers=headers, json=body, timeout=(30, 120))
                if r.status_code == 200:
                    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    data = json.loads(text)
                    out = data["items"] if isinstance(data, dict) else data
                    n = 0
                    for o in out:
                        it = batch[int(o["i"])]
                        for k in ("title_fa", "author_en", "author_fa"):
                            if not it.get(k) and str(o.get(k, "")).strip():
                                it[k] = str(o[k]).strip()
                                n += 1
                    return n
                log(f"  Gemini {model}: {r.status_code}")
                if r.status_code == 429 and "PerDay" in r.text:
                    return -1                 # daily quota finished: stop asking
            except Exception as e:  # noqa: BLE001
                log(f"  Gemini {model}: {e}")
            time.sleep(10)
        time.sleep(20 * (attempt + 1))
    return 0


def complete(it):
    return all(it.get(k) for k in ("title_en", "title_fa", "author_en", "author_fa"))


def main():
    log("1/5 Collecting novels from Wikipedia categories...")
    titles = crawl()
    if len(titles) < 200:
        log("Too few candidates: the Wikipedia API may be unreachable.")
        sys.exit(1)
    log("2/5 Ranking by fame (page views)...")
    ranked = rank_by_views(titles)[:POOL]
    log(f"3/5 Checking the plot section of the top {len(ranked)} (stops at {int(TARGET * 1.3)} good ones)...")
    good = []
    for n, (t, v) in enumerate(ranked):
        info = plot_info(t)
        if info:
            info["views"] = v
            good.append(info)
        if n % 100 == 0:
            log(f"  checked {n}, good {len(good)}")
        if len(good) >= int(TARGET * 1.3):
            break
        time.sleep(0.05)
    log(f"{len(good)} novels have a usable plot.")
    log("4/5 Persian names from Persian Wikipedia and Wikidata...")
    good = enrich(good)
    missing = [it for it in good if not complete(it)]
    log(f"{len(missing)} novel(s) still miss a Persian title or an author name.")
    if missing and GEMINI_KEY:
        log("5/5 Asking Gemini only for the missing names...")
        for i in range(0, len(missing), 30):
            r = gemini_fill(missing[i:i + 30])
            log(f"  batch {i // 30 + 1}: {r} value(s) filled")
            if r < 0:
                break
            time.sleep(5)
    final, seen = [], set()
    for it in good:
        key = it["title_en"].lower()
        if not complete(it) or key in seen:
            continue
        seen.add(key)
        final.append({k: it[k] for k in ("title_en", "title_fa", "author_en", "author_fa", "wiki_en", "wiki_fa")}
                     | {"views": it["views"]})
        if len(final) >= TARGET:
            break
    log(f"Final list: {len(final)} novels.")
    if len(final) < min(50, TARGET):
        log("Too few complete novels: nothing written.")
        sys.exit(1)
    OUT.write_text(json.dumps(final, ensure_ascii=False, indent=0), encoding="utf-8")
    log(f"Written {OUT.name} ({date.today().isoformat()}).")


if __name__ == "__main__":
    main()
