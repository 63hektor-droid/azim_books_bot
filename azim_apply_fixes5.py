"""
azim_apply_fixes5.py - بعد از azim_apply_fixes4.py یک‌بار اجرا کنید:
    python azim_apply_fixes5.py
اگر حتی یک وصله پیدا نشود، هیچ فایلی تغییر نمی‌کند.
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
BOT = ROOT / "azim_book_video_bot.py"
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


NEW = r'''
# Voice-only writing of the short words that the Gemini voice swallows or merges («در این» -> «دین»/«این»,
# «را از» -> «از»). Only these words get vowel marks, only in the text sent to the voice: subtitles stay clean.
_PL = "\u0600-\u06FF\u200c"
_VOICE_WORDS = (("در", "دَر"), ("این", "اِین"), ("را", "رَا"), ("از", "اَز"))


def gemini_voice_text(t):
    for plain, marked in _VOICE_WORDS:
        t = re.sub(rf"(?<![{_PL}]){plain}(?![{_PL}])", marked, t)
    return t


def gemini_voice_groups(scenes, kind):'''

once("def gemini_voice_groups(scenes, kind):", NEW.lstrip("\n"), "voice-only marks for short words")
once('        gemini_tts("\\n\\n".join(texts), kind, full, GEMINI_TTS_STYLE)',
     '        gemini_tts("\\n\\n".join(gemini_voice_text(t) for t in texts), kind, full, GEMINI_TTS_STYLE)',
     "group text with marks")
once("            gemini_tts(texts[i], kind, alt, GEMINI_TTS_STYLE)",
     "            gemini_tts(gemini_voice_text(texts[i]), kind, alt, GEMINI_TTS_STYLE)", "re-read text with marks")
# one skipped word in a 45-75 word paragraph is 0.98-0.99: the old limit 0.96 would not notice it
once('VOICE_CHECK_MIN = float(os.environ.get("AZIM_VOICE_CHECK_MIN") or 0.96)',
     'VOICE_CHECK_MIN = float(os.environ.get("AZIM_VOICE_CHECK_MIN") or 0.985)', "stricter check")

if errors:
    print("\nNOTHING CHANGED. Problems:")
    for e in errors:
        print(" -", e)
    sys.exit(1)

compile(text, "azim_book_video_bot.py", "exec")
shutil.copy(BOT, ROOT / "azim_book_video_bot.py.bak5")
BOT.write_text(text, encoding="utf-8")
print("Done. (backup: azim_book_video_bot.py.bak5)")
