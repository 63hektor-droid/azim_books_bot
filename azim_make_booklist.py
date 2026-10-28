"""
azim_make_booklist.py  -  builds azim_books_list.json: the most famous NOVELS (up to 2000), ordered by fame.

It runs by itself inside the video workflow when the list does not exist yet (about 10-25 minutes), or by hand with
the workflow "azim-make-booklist". It uses almost no Gemini quota.

  1. Candidates: ONE Wikidata query = all novels that have an English Wikipedia article, ordered by the number of
     Wikipedia languages (a good measure of world fame). If that query does not work, the old way is used
     (Wikipedia categories + page views), now with parallel requests.
  2. A novel is kept only if its English article has a real "Plot" section of 350-900 words
     (the Wikipedia guideline: about 400-700 words). Articles are read 25 at a time, 4 requests in parallel.
  3. Persian title: Persian Wikipedia language link (or Wikidata label); author: Wikidata (property P50).
     Articles that Wikipedia marked Good or Featured get a small boost in the order.
  4. Only what is still missing (a few Persian names) is asked from Gemini, in small batches.

Output: a JSON list of {title_en, title_fa, author_en, author_fa, wiki_en, wiki_fa, score, badge}.
"""
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).parent
OUT = ROOT / "azim_books_list.json"
TARGET = int(os.environ.get("AZIM_BOOKS_TARGET") or 2000)
POOL = int(os.environ.get("AZIM_BOOKS_POOL") or 9000)              # most candidates that are checked
MIN_SITELINKS = int(os.environ.get("AZIM_BOOKS_MIN_SITELINKS") or 6)  # a novel needs articles in this many languages
PLOT_MIN = int(os.environ.get("AZIM_PLOT_MIN") or 350)             # words of the Plot section
PLOT_MAX = int(os.environ.get("AZIM_PLOT_MAX") or 900)
WORKERS = int(os.environ.get("AZIM_WORKERS") or 4)                 # parallel requests (Wikipedia asks to stay low)
BATCH = 25                                                         # articles read per request
MAX_CATS = int(os.environ.get("AZIM_BOOKS_MAX_CATS") or 1500)      # only for the fallback crawl
MAX_DEPTH = int(os.environ.get("AZIM_BOOKS_DEPTH") or 3)
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODELS = ["gemini-flash-lite-latest", "gemini-flash-latest"]

REPO = os.environ.get("GITHUB_REPOSITORY", "")
UA = "azim-booklist/2.0 (educational book summaries; https://github.com/" + (REPO or "azim") + ")"
FEATURED, GOOD = "Q17437796", "Q17437798"      # Wikidata badges of Featured / Good articles

# the Plot section: level-2 headings such as "Plot", "Plot summary", "Plot introduction", "Synopsis", "Summary", "Story"
PLOT_HEAD = re.compile(r"^(plot|synopsis|summary|story|storyline)\b", re.I)
PLOT_SKIP = re.compile(r"adaptation|film|television|reception|translation|publication|criticism|history", re.I)
HEAD = re.compile(r"^(={2,6})\s*(.+?)\s*\1\s*$", re.M)
BAD_TITLE = re.compile(
    r"^(List of|Lists of|Category:|Template:|Portal:|Outline of)|"
    r"\((film|TV|miniseries|series|disambiguation|franchise|comics|manga|anime|video game|play|musical|opera|"
    r"album|song|band|character|fictional)", re.I)


def log(*a):
    print(*a, flush=True)


def pmap(fn, items):
    """fn over items, WORKERS at a time, results in the same order."""
    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=max(1, WORKERS)) as ex:
        return list(ex.map(fn, items))


def api(host, params, tries=6):
    """One MediaWiki API call. Waits as long as the server asks when it says 'too many requests'."""
    p = dict(params)
    p.update({"format": "json", "formatversion": 2})
    for t in range(tries):
        try:
            r = requests.get(f"https://{host}/w/api.php", params=p, headers={"User-Agent": UA}, timeout=90)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                wait = r.headers.get("Retry-After", "")
                time.sleep(min(int(wait), 60) if wait.isdigit() else 5 * (t + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(3 * (t + 1))
    return None


def wiki(params):
    return api("en.wikipedia.org", params)


def wikidata(params):
    return api("www.wikidata.org", params)


# ---------------------------------------------------------------- 1. candidates
SPARQL = ("SELECT ?title ?sl WHERE { "
          "?item wdt:P31 wd:Q8261 ; wikibase:sitelinks ?sl . FILTER(?sl >= %d) "
          "?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> ; schema:name ?title . } "
          "ORDER BY DESC(?sl) LIMIT %d")


def sparql_candidates():
    """[(English article title, number of Wikipedia languages), ...] of all novels, most famous first."""
    query = SPARQL % (MIN_SITELINKS, POOL)
    for t in range(3):
        try:
            r = requests.get("https://query.wikidata.org/sparql", params={"query": query, "format": "json"},
                             headers={"User-Agent": UA, "Accept": "application/sparql-results+json"}, timeout=150)
            if r.status_code == 200:
                out, seen = [], set()
                for b in r.json()["results"]["bindings"]:
                    title, sl = b["title"]["value"], int(float(b["sl"]["value"]))
                    if title in seen or BAD_TITLE.search(title):
                        continue
                    seen.add(title)
                    out.append((title, sl))
                return out
            log(f"  Wikidata query: status {r.status_code}")
        except Exception as e:  # noqa: BLE001
            log(f"  Wikidata query error: {str(e)[:100]}")
        time.sleep(20 * (t + 1))
    return []


# fallback: Wikipedia categories of novels + page views (the old way, but with parallel requests)
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


def crawl_candidates():
    seen, pages = set(), set()
    level, depth = list(SEEDS), 0
    while level and len(seen) < MAX_CATS and depth <= MAX_DEPTH:
        batch = [c for c in dict.fromkeys(level) if c not in seen][:MAX_CATS - len(seen)]
        seen.update(batch)
        nxt = []
        for subs, pgs in pmap(members, batch):
            pages.update(t for t in pgs if not BAD_TITLE.search(t))
            if depth < MAX_DEPTH:
                nxt += [s for s in subs if not SKIP_CAT.search(s) and s not in seen]
        level, depth = nxt, depth + 1
        log(f"  categories: {len(seen)}, novels found so far: {len(pages)}")
    return sorted(pages)


def views_batch(titles):
    out = {}
    d = wiki({"action": "query", "prop": "pageviews", "pvipdays": 60, "redirects": 1, "titles": "|".join(titles)})
    for p in ((d or {}).get("query") or {}).get("pages", []):
        total = sum(v for v in (p.get("pageviews") or {}).values() if v)
        if total:
            out[p["title"]] = total
    return out


def crawl_and_rank():
    titles = crawl_candidates()
    log(f"Crawl done: {len(titles)} candidate pages. Ranking by page views...")
    chunks = [titles[i:i + 50] for i in range(0, len(titles), 50)]
    views = {}
    for res in pmap(views_batch, chunks):
        views.update(res)
    ranked = sorted(views.items(), key=lambda kv: -kv[1])[:POOL]
    log(f"Ranked {len(ranked)} pages by views.")
    return ranked


# ---------------------------------------------------------------- 2. the article must have a real Plot section
def clean_wikitext(s):
    """Wikipedia markup -> plain text (good enough to count words)."""
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"<ref[^>/]*/>", "", s)
    s = re.sub(r"<ref[^>]*>.*?</ref>", "", s, flags=re.S)
    prev = None
    while prev != s:                                   # templates can be nested
        prev = s
        s = re.sub(r"\{\{[^{}]*\}\}", "", s)
    s = re.sub(r"\[\[(?:File|Image|Category):[^\]]*\]\]", "", s, flags=re.I)
    s = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"'{2,}", "", s)
    return s


def plot_words(wikitext):
    """(words of the Plot section(s), lead text) of an article given as wikitext."""
    t = clean_wikitext(wikitext)
    ms = list(HEAD.finditer(t))
    lead = t[:ms[0].start()] if ms else t
    words, cur = 0, False
    for i, m in enumerate(ms):
        level, head = len(m.group(1)), m.group(2).strip()
        end = ms[i + 1].start() if i + 1 < len(ms) else len(t)
        if level == 2:
            cur = bool(PLOT_HEAD.match(head) and not PLOT_SKIP.search(head))
        if cur:
            words += len(t[m.end():end].split())
    return words, lead


def fetch_wikitext(titles):
    """{requested title: (real article title, wikitext)} for up to 50 articles in one request."""
    d = wiki({"action": "query", "prop": "revisions", "rvprop": "content", "rvslots": "main", "redirects": 1,
              "titles": "|".join(titles)})
    q = (d or {}).get("query") or {}
    norm = {x["from"]: x["to"] for x in q.get("normalized", [])}
    redir = {x["from"]: x["to"] for x in q.get("redirects", [])}
    pages = {}
    for p in q.get("pages", []):
        try:
            pages[p["title"]] = p["revisions"][0]["slots"]["main"]["content"]
        except (KeyError, IndexError, TypeError):
            continue
    out = {}
    for t in titles:
        real = redir.get(norm.get(t, t), norm.get(t, t))
        if real in pages:
            out[t] = (real, pages[real])
    return out


def check_batch(batch, strict_novel=False):
    """batch: [(title, score)]. Returns the novels of it whose Plot section has 350-900 words."""
    got = fetch_wikitext([t for t, _ in batch])
    good = []
    for t, score in batch:
        if t not in got:
            continue
        real, text = got[t]
        n, lead = plot_words(text)
        if not (PLOT_MIN <= n <= PLOT_MAX):
            continue
        if strict_novel and not re.search(r"\bnovel", lead[:600], re.I):
            continue                       # not a novel (non-fiction, short stories, series page...)
        good.append({"wiki_en": real, "plot_words": n, "score": score})
    return good


def check_plots(cands, need, strict_novel):
    batches = [cands[i:i + BATCH] for i in range(0, len(cands), BATCH)]
    window = WORKERS * 2
    good, checked = [], 0
    for w in range(0, len(batches), window):
        chunk = batches[w:w + window]
        for res in pmap(lambda b: check_batch(b, strict_novel), chunk):
            good += res
        checked += sum(len(b) for b in chunk)
        log(f"  checked {checked} of {len(cands)}, good {len(good)}")
        if len(good) >= need:
            break
    return good


# ---------------------------------------------------------------- 3. Persian names, authors, badges (Wikidata)
def strip_paren(s):
    return re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()


def enrich(items):
    """Adds title_fa/wiki_fa (Persian Wikipedia link), author_en/author_fa (Wikidata P50) and the quality badge."""
    qid, fa_link = {}, {}
    titles = [it["wiki_en"] for it in items]

    def langlinks(chunk):
        return wiki({"action": "query", "prop": "langlinks|pageprops", "lllang": "fa", "lllimit": "max",
                     "ppprop": "wikibase_item", "redirects": 1, "titles": "|".join(chunk)})

    for d in pmap(langlinks, [titles[i:i + 50] for i in range(0, len(titles), 50)]):
        if d and "query" in d:
            for p in d["query"].get("pages", []):
                if (p.get("pageprops") or {}).get("wikibase_item"):
                    qid[p["title"]] = p["pageprops"]["wikibase_item"]
                for ll in p.get("langlinks") or []:
                    if ll.get("lang") == "fa":
                        fa_link[p["title"]] = ll.get("title") or ll.get("*")
    log(f"Wikidata ids: {len(qid)}, Persian Wikipedia links: {len(fa_link)}")

    author_of, badge_of = {}, {}

    def entities(chunk):
        return wikidata({"action": "wbgetentities", "ids": "|".join(chunk), "props": "claims|sitelinks",
                         "sitefilter": "enwiki"})

    qids = sorted(set(qid.values()))
    for d in pmap(entities, [qids[i:i + 50] for i in range(0, len(qids), 50)]):
        for q, ent in ((d or {}).get("entities") or {}).items():
            try:
                for c in (ent.get("claims") or {}).get("P50", []):
                    author_of[q] = c["mainsnak"]["datavalue"]["value"]["id"]
                    break
            except (KeyError, TypeError):
                pass
            badges = ((ent.get("sitelinks") or {}).get("enwiki") or {}).get("badges") or []
            badge_of[q] = "FA" if FEATURED in badges else ("GA" if GOOD in badges else "")

    labels = {}
    need = sorted(set(qid.values()) | set(author_of.values()))

    def labs(chunk):
        return wikidata({"action": "wbgetentities", "ids": "|".join(chunk), "props": "labels",
                         "languages": "fa|en"})

    for d in pmap(labs, [need[i:i + 50] for i in range(0, len(need), 50)]):
        for q, ent in ((d or {}).get("entities") or {}).items():
            lab = ent.get("labels") or {}
            labels[q] = {"fa": (lab.get("fa") or {}).get("value", ""), "en": (lab.get("en") or {}).get("value", "")}

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
        it["badge"] = badge_of.get(q, "")
    log(f"Quality badges: {sum(1 for it in items if it['badge'] == 'FA')} featured, "
        f"{sum(1 for it in items if it['badge'] == 'GA')} good.")
    return items


# ---------------------------------------------------------------- 4. Gemini only for what is still missing
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


def boosted(it):
    """Fame score; articles that Wikipedia marked Featured / Good are moved up a little."""
    return it["score"] * {"FA": 1.5, "GA": 1.25}.get(it.get("badge", ""), 1.0)


def main():
    t0 = time.time()
    log("1/5 Candidates: all novels with an English Wikipedia article, most famous first (one Wikidata query)...")
    cands = sparql_candidates()
    strict = False
    if len(cands) < 300:
        log("The Wikidata query did not work: using Wikipedia categories and page views instead.")
        cands = crawl_and_rank()
        strict = True
    if len(cands) < 200:
        log("Too few candidates: the Wikipedia API may be unreachable.")
        sys.exit(1)
    log(f"{len(cands)} candidates ({int(time.time() - t0)} s).")
    log(f"2/5 Reading the Plot section of the candidates, most famous first "
        f"(keeps {PLOT_MIN}-{PLOT_MAX} words; stops at {int(TARGET * 1.4)} good ones)...")
    good = check_plots(cands, int(TARGET * 1.4), strict)
    log(f"{len(good)} novels have a Plot section of the right size ({int(time.time() - t0)} s).")
    log("3/5 Persian names, authors and quality badges from Persian Wikipedia and Wikidata...")
    good = enrich(good)
    missing = [it for it in good if not complete(it)]
    log(f"{len(missing)} novel(s) still miss a Persian title or an author name.")
    if missing and GEMINI_KEY:
        log("4/5 Asking Gemini only for the missing names...")
        for i in range(0, len(missing), 30):
            r = gemini_fill(missing[i:i + 30])
            log(f"  batch {i // 30 + 1}: {r} value(s) filled")
            if r < 0:
                break
            time.sleep(5)
    log("5/5 Final order and saving...")
    good.sort(key=boosted, reverse=True)
    final, seen = [], set()
    for it in good:
        key = it["title_en"].lower()
        if not complete(it) or key in seen:
            continue
        seen.add(key)
        final.append({k: it[k] for k in ("title_en", "title_fa", "author_en", "author_fa", "wiki_en", "wiki_fa")}
                     | {"score": it["score"], "badge": it.get("badge", "")})
        if len(final) >= TARGET:
            break
    log(f"Final list: {len(final)} novels ({int(time.time() - t0)} s).")
    if len(final) < min(50, TARGET):
        log("Too few complete novels: nothing written.")
        sys.exit(1)
    OUT.write_text(json.dumps(final, ensure_ascii=False, indent=0), encoding="utf-8")
    log(f"Written {OUT.name} ({date.today().isoformat()}).")


if __name__ == "__main__":
    main()
