"""
azim_apply_fixes2.py - بعد از azim_apply_fixes.py یک‌بار اجرا کنید:
    python azim_apply_fixes2.py
اگر وصله‌ای پیدا نشود هیچ فایلی تغییر نمی‌کند.
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


once('pure illustration"',
     'pure illustration, main subject centered in the middle horizontal band of the frame, '
     'nothing important near the top or bottom edges"', "centered composition")
once("data = gemini_json(prompt, minutes=15, models=WRITE_MODELS)",
     "data = gemini_json(prompt, minutes=15, models=WRITE_MODELS, read_timeout=300)", "write_scenes timeout 300")
once("out = gemini_json(prompt, minutes=10, models=WRITE_MODELS)",
     "out = gemini_json(prompt, minutes=10, models=WRITE_MODELS, read_timeout=300)", "review timeout 300")
once("def strike(model):", "def strike(model, weight=1):", "strike weight 1")
once("STRIKES[model] = STRIKES.get(model, 0) + 1", "STRIKES[model] = STRIKES.get(model, 0) + weight", "strike weight 2")
once('if "timed out" in str(e).lower():\n                    strike(model)',
     'if "timed out" in str(e).lower():\n                    strike(model, 2 if read_timeout >= 300 else 1)', "long timeout skips at once")

if errors:
    print("\nNOTHING CHANGED. Problems:")
    for e in errors:
        print(" -", e)
    sys.exit(1)
shutil.copy(BOT, ROOT / "azim_book_video_bot.py.bak2")
compile(text, "azim_book_video_bot.py", "exec")
BOT.write_text(text, encoding="utf-8")
print("Done. (backup: azim_book_video_bot.py.bak2)")
