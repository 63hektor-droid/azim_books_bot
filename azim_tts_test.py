"""Diagnostic v2: real narration sentences x several Pocket TTS settings. Prints a summary table at the end."""
import base64
import subprocess
import time
from pathlib import Path

import azim_voices_data as d
from sokhanvar import Sokhanvar

OUT = Path("azim_test_out")
OUT.mkdir(exist_ok=True)

TEXTS = {
    "t1_greeting": "به پادکست ما خوش آمدید.",
    "t2_question": "آیا ثروت، ضامن خوشبختی در ازدواج است؟ رمان با این جمله ماندگار شد.",
    "t3_ezafe": "الیزابت از زبان دوستی مشترک باخبر می‌شود که دارسی مانع ازدواج خواهر او شده است.",
    "t4_simple": "دارسی به کنت می‌آید و مدام به دیدار الیزابت می‌رود.",
    "t5_history": "جین آستین این اثر را در اواخر قرن هجدهم نوشت و در سال هزار و هشتصد و سیزده منتشر کرد.",
}

CONFIGS = {
    "A_default": {},
    "B_eos-4": {"eos_threshold": -4.0},
    "C_eos-6": {"eos_threshold": -6.0},
    "D_eos-4_budget30": {"eos_threshold": -4.0, "token_budget": 30},
    "E_eos-4_temp0.6": {"eos_threshold": -4.0, "temperature": 0.6},
    "F_eos-4_commas": {"eos_threshold": -4.0, "split_at_commas": "true"},
}


def ff(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


flac = OUT / "ref_female.flac"
flac.write_bytes(base64.b64decode(d.FEMALE_FLAC_B64))
ref = OUT / "ref_female.wav"
ff("-i", str(flac), "-ac", "1", "-ar", "24000", str(ref))

summary = {}
for cname, extra in CONFIGS.items():
    cfg = OUT / f"{cname}.yaml"
    lines = [
        "backend: pocket_tts_farsi",
        "model: mehdi-hf/pocket-tts-farsi-v2",
        "language: fa",
        f"reference_audio: {ref.resolve()}",
        "reference_seconds: 5.0",
        "seed: 42",
        "retries: 1",
        f"output_dir: {(OUT / 'out').resolve()}",
    ] + [f"{k}: {v}" for k, v in extra.items()]
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"=== config {cname}: {extra}", flush=True)
    try:
        sp = Sokhanvar.from_yaml(str(cfg))
    except Exception as e:  # noqa: BLE001
        print(f"RESULT {cname}: cannot load config: {e}", flush=True)
        summary[cname] = "config error"
        continue
    res = []
    for tname, text in TEXTS.items():
        out = OUT / f"result_{cname}_{tname}.wav"
        t0 = time.time()
        try:
            sp.synthesize(text, str(out))
            dur = float(subprocess.check_output(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(out)]))
            res.append(f"{tname}=OK({dur:.1f}s)")
            print(f"RESULT {cname} {tname}: OK {dur:.1f}s (took {time.time() - t0:.0f}s)", flush=True)
        except Exception as e:  # noqa: BLE001
            msg = str(e)[:70]
            res.append(f"{tname}=FAIL")
            print(f"RESULT {cname} {tname}: FAILED: {msg}", flush=True)
    summary[cname] = " ".join(res)

print("\n===== SUMMARY =====", flush=True)
for k, v in summary.items():
    print(f"SUMMARY {k}: {v}", flush=True)
