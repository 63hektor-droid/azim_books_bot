"""Diagnostic: tries Pocket TTS Farsi v2 with several reference-voice variants and a short sentence."""
import base64
import subprocess
import traceback
from pathlib import Path

import azim_voices_data as d
from sokhanvar import Sokhanvar

OUT = Path("azim_test_out")
OUT.mkdir(exist_ok=True)
TEXT = "امروز هوا خوب است."


def ff(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


refs = {}
for kind, b64 in (("female", d.FEMALE_FLAC_B64), ("male", d.MALE_FLAC_B64)):
    flac = OUT / f"ref_{kind}.flac"
    flac.write_bytes(base64.b64decode(b64))
    full = OUT / f"ref_{kind}_full.wav"
    ff("-i", str(flac), "-ac", "1", "-ar", "24000", str(full))
    refs[f"{kind}_full"] = full
    first4 = OUT / f"ref_{kind}_4s.wav"
    ff("-i", str(flac), "-t", "4", "-ac", "1", "-ar", "24000", str(first4))
    refs[f"{kind}_4s"] = first4
    # variant: skip the first 0.5 s (in case the clip starts in the middle of a sound) and add a short fade
    cut = OUT / f"ref_{kind}_cut.wav"
    ff("-ss", "0.5", "-i", str(flac), "-t", "4.5", "-af", "afade=t=in:d=0.1,afade=t=out:st=4.3:d=0.2",
       "-ac", "1", "-ar", "24000", str(cut))
    refs[f"{kind}_cut"] = cut

for name, ref in refs.items():
    cfg = OUT / f"{name}.yaml"
    cfg.write_text(
        "backend: pocket_tts_farsi\n"
        "model: mehdi-hf/pocket-tts-farsi-v2\n"
        "language: fa\n"
        f"reference_audio: {ref.resolve()}\n"
        f"output_dir: {(OUT / 'out').resolve()}\n", encoding="utf-8")
    out = OUT / f"result_{name}.wav"
    print(f"=== {name}", flush=True)
    try:
        sp = Sokhanvar.from_yaml(str(cfg))
        sp.synthesize(TEXT, str(out))
        dur = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(out)])
        print(f"RESULT {name}: OK, duration {float(dur):.2f}s", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"RESULT {name}: FAILED: {e}", flush=True)
        traceback.print_exc()
