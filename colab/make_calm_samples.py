"""Recreate Bob's calm voice samples from the full BobNew.mp3 (on Josh's Desktop).
Almost all of BobNew.mp3 is excited play-by-play (median pitch ~280-350 Hz); the only calm,
conversational stretches are near the start and end (~150-175 Hz). These cut points start and end
inside real pauses, so no sample ends mid-word. Needs ffmpeg.  Usage: python make_calm_samples.py BobNew.mp3"""
import subprocess, sys
src = sys.argv[1] if len(sys.argv) > 1 else "BobNew.mp3"
CUTS = {  # seconds into BobNew.mp3; speaker match vs. the excited voice measured in Colab (ECAPA cosine)
    "bob_calm_1.wav": (4.4, 10.8),      # 0.31 (rejected, < 0.35)
    "bob_calm_2.wav": (975.7, 984.3),   # 0.19 (rejected)
    "bob_calm_3.wav": (1065.5, 1072.6), # 0.40 (chosen as Bob's calm voice)
    "bob_calm_4.wav": (61.1, 67.9),     # 0.35
    "bob_calm_5.wav": (109.6, 116.2),   # 0.27 (rejected)
}
for name, (a, b) in CUTS.items():
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-ss", str(a), "-to", str(b), "-ac", "1", "-ar", "24000", name], check=True)
    print("wrote", name)
