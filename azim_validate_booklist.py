"""azim_validate_booklist.py - one-time, FREE check of azim_books_list.json (only the Wikipedia API: no Gemini, no Cloudflare).

For every book it does exactly what the bot does before a video (azim_book_video_bot.fetch_wiki): finds the English Wikipedia
article, takes its Plot section and checks the length (PLOT_MIN_WORDS..PLOT_MAX_WORDS of the bot). Books that pass are written to
azim_books_list_verified.json (same order, same format) with
  - "wiki_en": the real title of the article that was found,
  - "title_fa": the title of the PERSIAN Wikipedia article of the book (when there is one; else the old value),
  - "author_fa": the Persian Wikipedia name of the author (when there is one; else the old value),
  - "plot_words": the length of the Plot section.
Books that do not pass are listed in azim_books_list_dropped.json and azim_books_list_report.txt (with the reason), so nothing is
lost silently. Results are cached in azim_booklist_cache.json: when it is stopped, the next run continues where it stopped.

Run:  python azim_validate_booklist.py            (all books)
      python azim_validate_booklist.py 50         (only the first 50: a quick test)
Then replace azim_books_list.json by azim_books_list_verified.json.
"""
import json
import os
import sys
import time
from pathlib import Path

for k, v in (("BOT_TOKEN", "x"), ("CHANNEL_ID", "x"), ("GEMINI_API_KEY", "x")):    # the bot module needs them only to be set
    os.environ.setdefault(k, v)
import azim_book_video_bot as bot  # noqa: E402

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("AZIM_LIST_IN") or HERE / "azim_books_list.json")
OUT = HERE / "azim_books_list_verified.json"
DROPPED = HERE / "azim_books_list_dropped.json"
REPORT = HERE / "azim_books_list_report.txt"
CACHE = HERE / "azim_booklist_cache.json"
PAUSE = float(os.environ.get("AZIM_VALIDATE_PAUSE") or 0.25)       # seconds between books: polite to Wikipedia


def check(b):
    """The verdict for one book: {"ok": bool, ...}."""
    book = {"title_en": b["title_en"], "author_en": b["author_en"], "wikipedia_title_en": b.get("wiki_en") or ""}
    en = None
    for attempt in range(2):                           # one more try: a network hiccup must not drop a good book
        en = bot.fetch_wiki(book)
        if en:
            break
        time.sleep(2)
    if not en or not en["plot"]:
        return {"ok": False, "why": "no English Wikipedia article with a Plot section"}
    pw = en["plot_words"]
    if pw < bot.PLOT_MIN_WORDS or pw > bot.PLOT_MAX_WORDS:
        return {"ok": False, "why": f"Plot section has {pw} words (allowed {bot.PLOT_MIN_WORDS}-{bot.PLOT_MAX_WORDS})"}
    return {"ok": True, "wiki_en": en["title"], "plot_words": pw,
            "title_fa": bot.wiki_title_fa(en["title"]) or "", "author_fa": ""}


def author_fa(b, cache):
    """The Persian name of an author is looked up once per author."""
    name = b["author_en"]
    if name not in cache["authors"]:
        book = {"title_en": b["title_en"], "author_en": name, "author_fa": ""}
        bot.fix_author_fa(book)
        cache["authors"][name] = book.get("author_fa") or ""
        time.sleep(PAUSE)
    return cache["authors"][name]


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
    books = json.loads(SRC.read_text(encoding="utf-8"))
    if limit:
        books = books[:limit]
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {"books": {}, "authors": {}}
    ok, dropped = [], []
    for n, b in enumerate(books, 1):
        key = b["title_en"]
        if key not in cache["books"]:
            try:
                cache["books"][key] = check(b)
            except Exception as e:  # noqa: BLE001
                print(f"[{n}/{len(books)}] {key}: error ({e}), will be tried again on the next run", flush=True)
                continue
            time.sleep(PAUSE)
            if n % 25 == 0:
                CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        r = cache["books"][key]
        if r["ok"]:
            nb = dict(b)
            nb["wiki_en"] = r["wiki_en"]
            nb["plot_words"] = r["plot_words"]
            if r.get("title_fa"):
                nb["title_fa"] = r["title_fa"]
            fa = author_fa(b, cache) or b.get("author_fa") or ""
            if fa:
                nb["author_fa"] = fa
            ok.append(nb)
            print(f"[{n}/{len(books)}] OK   {key} ({r['plot_words']} words) | {nb.get('title_fa') or '-'}", flush=True)
        else:
            dropped.append({**b, "why": r["why"]})
            print(f"[{n}/{len(books)}] DROP {key}: {r['why']}", flush=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    for i, e in enumerate(ok):                          # the scores keep the order of the list
        e["score"] = 3000000 - i * 1000
    OUT.write_text(json.dumps(ok, ensure_ascii=False, indent=1), encoding="utf-8")
    DROPPED.write_text(json.dumps(dropped, ensure_ascii=False, indent=1), encoding="utf-8")
    with_fa_t = sum(1 for e in ok if e.get("title_fa"))
    with_fa_a = sum(1 for e in ok if e.get("author_fa"))
    lines = [f"checked: {len(ok) + len(dropped)} | usable: {len(ok)} | dropped: {len(dropped)}",
             f"Persian title known: {with_fa_t} | Persian author known: {with_fa_a}", ""]
    lines += [f"DROPPED {d['title_en']} - {d['why']}" for d in dropped]
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print("\n" + "\n".join(lines[:2]), flush=True)


if __name__ == "__main__":
    main()
