"""
azim_apply_fixes.py - یک‌بار اجرا کنید (کنار azim_book_video_bot.py و azim_books_list.json):
    python azim_apply_fixes.py
اگر حتی یکی از وصله‌ها پیدا نشود، هیچ فایلی تغییر نمی‌کند.
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
BOT = ROOT / "azim_book_video_bot.py"
LIST = ROOT / "azim_books_list.json"

text = BOT.read_text(encoding="utf-8")
errors = []


def once(old, new, name):
    global text
    n = text.count(old)
    if n != 1:
        errors.append(f"{name}: found {n} times (must be 1)")
        return
    text = text.replace(old, new)
    print("OK  ", name)


# ---- 1) Cloudflare: send ONLY prompt+steps (seed/width/height cause the 400 error)
a = text.find("_cf_size_ok = True")
b = text.find("def pollinations_image")
if a < 0 or b < 0 or text.count("_cf_size_ok = True") != 1 or text.count("def pollinations_image") != 1:
    errors.append("cf_image block not found")
else:
    new_cf = '''def cf_image(prompt, path, seed=None):
    """Cloudflare Workers AI FLUX.1 schnell (free). The model accepts ONLY "prompt" and "steps":
    seed / width / height give a 400 error, so they are never sent. A 400/401/403/422 cannot be fixed by retrying."""
    url = (f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
           f"/ai/run/@cf/black-forest-labs/flux-1-schnell")
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}", "Content-Type": "application/json"}
    payload = {"prompt": prompt[:1900], "steps": 4}
    for attempt in range(1, 4):
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
            if r.status_code in (400, 401, 403, 422):
                return False
        except Exception as e:  # noqa: BLE001
            print(f"  -> error: {e}", flush=True)
        time.sleep(8 * attempt)
    return False


'''
    text = text[:a] + new_cf + text[b:]
    print("OK   cf_image")

# ---- 2) Gemini: a model that times out / is overloaded twice is skipped for the rest of the run
once("def short_error(text):",
     '''def strike(model):
    """A model that timed out or answered 503 twice is skipped for a while in this run (saves 15-20 minutes)."""
    STRIKES[model] = STRIKES.get(model, 0) + 1
    if STRIKES[model] >= 2:
        SLOW_MODELS.add(model)
        print(f"  -> {model} was too slow or overloaded twice: skipped for now in this run.", flush=True)


def short_error(text):''', "strike()")
once("DEAD_MODELS = set()",
     "SLOW_MODELS = set()      # models that timed out / were overloaded twice (skipped for now)\n"
     "STRIKES = {}\n"
     "DEAD_MODELS = set()", "slow-model sets")
once("        alive = [m for m in models if m not in dead and m not in DEAD_MODELS]\n",
     "        alive = [m for m in models if m not in dead and m not in DEAD_MODELS and m not in SLOW_MODELS]\n"
     "        if not alive and any(m in SLOW_MODELS for m in models):\n"
     "            for m in models:                      # everything was slow: give all models a fresh chance\n"
     "                SLOW_MODELS.discard(m)\n"
     "                STRIKES.pop(m, None)\n"
     "            alive = [m for m in models if m not in dead and m not in DEAD_MODELS]\n", "alive list")
once('                        print(f"Gemini OK with {model}", flush=True)',
     '                        STRIKES.pop(model, None)\n'
     '                        print(f"Gemini OK with {model}", flush=True)', "reset strikes")
once('                    last = f"{model}: {r.status_code} {short_error(r.text)}"\n                    print(last, flush=True)\n',
     '                    last = f"{model}: {r.status_code} {short_error(r.text)}"\n                    print(last, flush=True)\n'
     '                    if r.status_code == 503:\n                        strike(model)\n', "503 strike")
once('                last = f"{model}: {e}"\n                print(last, flush=True)\n',
     '                last = f"{model}: {e}"\n                print(last, flush=True)\n'
     '                if "timed out" in str(e).lower():\n                    strike(model)\n', "timeout strike")

# ---- 3) a run whose image service is down stops early instead of burning hours (the script is kept for the next run)
once("def make_image(prompt, path, seed, scenes_left):",
     '''class ImageServiceDown(RuntimeError):
    """Fast images are not working and the slow fallback would take too long: stop, keep the script, retry next run."""


SLOW_IMAGES = {"on": False}                                   # True while the slow Pollinations fallback is used
RUN_LIMIT = int(os.environ.get("AZIM_RUN_LIMIT_MIN") or 90) * 60   # seconds after which slow images are not started


def make_image(prompt, path, seed, scenes_left):''', "ImageServiceDown")
once("    if USE_CF:\n        if cf_image(prompt, path, seed):",
     '    SLOW_IMAGES["on"] = False\n    if USE_CF:\n        if cf_image(prompt, path, seed):', "slow flag reset")
once('''    needed = (scenes_left + 1) * 50
    if time.time() - START_TIME + needed > JOB_BUDGET:
        raise RuntimeError("Not enough time left for slow image generation")''',
     '''    SLOW_IMAGES["on"] = True
    needed = (scenes_left + 1) * 50
    if time.time() - START_TIME > RUN_LIMIT:
        raise ImageServiceDown("fast images failed and this run is already too long for slow images")
    if time.time() - START_TIME + needed > JOB_BUDGET:
        raise ImageServiceDown("not enough time left for slow image generation")''', "run limit")
once("    make_image(prompt, path, seed, scenes_left)\n    if not IMAGE_CHECK:\n        return",
     '    make_image(prompt, path, seed, scenes_left)\n    if not IMAGE_CHECK or SLOW_IMAGES["on"]:     # no slow redraws on the slow service\n        return',
     "no check on slow images")
once("make_checked_image(prompt, img, seed + i * 10 + j, n_pics - done_pics - 1, sc)\n            except Exception as e:  # noqa: BLE001\n",
     "make_checked_image(prompt, img, seed + i * 10 + j, n_pics - done_pics - 1, sc)\n"
     "            except ImageServiceDown:\n                raise\n"
     "            except Exception as e:  # noqa: BLE001\n", "re-raise ImageServiceDown")
once("    except GeminiQuotaExhausted as exc:",
     '''    except ImageServiceDown as exc:
        print(f"STOP: the image service is not working ({exc}). The finished script is kept; the next run continues.",
              flush=True)
        try:                                   # an outage must not count as a failed attempt of the book
            p = load_json(PENDING_FILE, None)
            if p:
                p["attempts"] = max(0, int(p.get("attempts", 0)) - 1)
                save_json(PENDING_FILE, p)
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)
    except GeminiQuotaExhausted as exc:''', "main handler")

if errors:
    print("\nNOTHING CHANGED. Problems:")
    for e in errors:
        print(" -", e)
    sys.exit(1)

shutil.copy(BOT, ROOT / "azim_book_video_bot.py.bak")
BOT.write_text(text, encoding="utf-8")
compile(text, "azim_book_video_bot.py", "exec")
print("azim_book_video_bot.py patched (backup: azim_book_video_bot.py.bak)")

# ---- 4) the list of novels
if LIST.exists():
    books = json.loads(LIST.read_text(encoding="utf-8"))
    drop = {"The Turner Diaries", "Angel Beats!"}
    fix = {
        "Blood Meridian": {"author_fa": "کورمک مک‌کارتی"},
        "The Crossing": {"author_fa": "کورمک مک‌کارتی"},
        "Hush, Hush": {"author_fa": "بکا فیتزپاتریک"},
        "The Turn of the Screw": {"title_fa": "چرخش پیچ"},
        "Sweet Bean Paste": {"author_en": "Durian Sukegawa", "author_fa": "دوریان سوکه‌گاوا"},
    }
    out = []
    for b in books:
        if b.get("title_en") in drop:
            print("removed:", b["title_en"])
            continue
        if b.get("title_en") in fix:
            b.update(fix[b["title_en"]])
            print("fixed:", b["title_en"])
        out.append(b)
    shutil.copy(LIST, ROOT / "azim_books_list.json.bak")
    LIST.write_text(json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8")
else:
    print("azim_books_list.json not found next to this script: list not changed.")
print("Done.")
