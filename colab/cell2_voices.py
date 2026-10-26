# 2) Load OmniVoice and set up Bob's two voices:
#    bob_hot = his excited play-by-play voice (big moments)
#    bob     = his calm, conversational voice (routine pitches, recaps, stories)
#    Each comes from a clean sample cut at natural pauses (never mid-word), checked by having Bob read a test line back.
import os, re, glob, difflib, subprocess, numpy as np, torch, soundfile as sf
from omnivoice import OmniVoice
from IPython.display import Audio, display
from torch.utils.data.dataloader import _BaseDataLoaderIter as _B
_f = _B.__init__
while hasattr(_f, "__wrapped__"): _f = _f.__wrapped__
_B.__init__ = _B.__old_init__ = _f   # undo any earlier speechbrain patch so a re-run never stacks it
SR = 24000
SRC = "/content/bobnew_11m30-14m15.mp3" if os.path.exists("/content/bobnew_11m30-14m15.mp3") else "/content/Bob_clone_12s.wav"
assert os.path.exists(SRC), "Upload bobnew_11m30-14m15.mp3 and the bob_calm_*.wav files with the Files panel first"
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", SRC, "-ac", "1", "-ar", str(SR), "/content/bob_long.wav"], check=True)
if "model" not in globals():
    model = OmniVoice.from_pretrained("k2-fsa/OmniVoice", device_map="cuda:0", dtype=torch.float16, load_asr=True)

def words(t): return re.sub(r"[^a-z' ]", " ", t.lower()).split()
def try_sample(path, test):
    """Clone from one sample, read the test line, and score how exactly it comes back (1.0 = word for word)."""
    ref_text = model.transcribe(path)
    if not ref_text.strip(): return None
    prompt = model.create_voice_clone_prompt(ref_audio=path, ref_text=ref_text)
    out = model.generate(text=test, voice_clone_prompt=prompt, language="English")[0]
    heard = words(model.transcribe((out, SR)))
    score = difflib.SequenceMatcher(None, words(test), heard).ratio() - (0.3 if heard[:1] != words(test)[:1] else 0)
    return score, prompt, out, ref_text

# ---- Excited voice: steadiest complete-sentence stretch of the play-by-play recording ----
w, _ = sf.read("/content/bob_long.wav", dtype="float32")
hop = int(.02 * SR)
env = np.sqrt(np.convolve(w ** 2, np.ones(hop) / hop, "same"))[::hop]
talk = env > .12 * np.percentile(env, 95)
pauses, i = [], 0
while i < len(talk):
    if talk[i]: i += 1; continue
    j = i
    while j < len(talk) and not talk[j]: j += 1
    if j - i >= 15: pauses.append((i + j) // 2)
    i = j
cands = []
for a in range(len(pauses)):
    for b in range(a + 1, len(pauses)):
        dur = (pauses[b] - pauses[a]) * hop / SR
        if dur > 10: break
        if dur < 5: continue
        seg = env[pauses[a]:pauses[b]]; voiced = seg[seg > .12 * np.percentile(env, 95)]
        if len(voiced) < 50: continue
        cands.append((np.percentile(voiced, 95) / (np.median(voiced) + 1e-6), pauses[a] * hop, pauses[b] * hop))
cands.sort()
hot_paths = []
for ev, s0, s1 in cands:
    if all(s1 <= a or s0 >= b for a, b in hot_paths): hot_paths.append((s0, s1))
    if len(hot_paths) == 3: break
HOT_TEST = "Swung on and drilled to deep left field, it is off the wall, and here comes the run to score!"
best_hot = None
for k, (s0, s1) in enumerate(hot_paths):
    p = f"/content/bob_hot_{k}.wav"; sf.write(p, w[s0:s1], SR)
    r = try_sample(p, HOT_TEST)
    if r: print(f"excited sample {k}: score {r[0]:.2f} | says: {r[3][:70]!r}")
    if r and (best_hot is None or r[0] > best_hot[0]): best_hot = r

# ---- Calm voice: the conversational stretches (bob_calm_*.wav, cut from the start and end of BobNew.mp3) ----
CALM_TEST = "Good evening everybody. Two balls and one strike on the batter, and the infield shifts a step toward first."
calm_files = sorted(glob.glob("/content/bob_calm_*.wav"))
subprocess.run("pip -q install --no-deps speechbrain hyperpyyaml ruamel.yaml==0.18.6 ruamel.yaml.clib", shell=True)
import sys
for m in [m for m in sys.modules if m.startswith(("ruamel", "hyperpyyaml", "speechbrain"))]: del sys.modules[m]
try:   # optional: make sure the calm sample is the same man as the excited one
    from speechbrain.inference.speaker import EncoderClassifier
    import torchaudio
    spk = EncoderClassifier.from_hparams("speechbrain/spkrec-ecapa-voxceleb", savedir="/content/ecapa", run_opts={"device": "cuda"})
    def emb(path):
        x, sr0 = sf.read(path, dtype="float32"); x = torch.tensor(x).unsqueeze(0)
        x = torchaudio.functional.resample(x, sr0, 16000)
        return spk.encode_batch(x.cuda()).squeeze()
    hot_emb = emb(f"/content/bob_hot_0.wav")
    same = lambda p: float(torch.nn.functional.cosine_similarity(emb(p), hot_emb, dim=0))
except Exception as e:
    print("speaker check skipped:", e); same = None
best_calm = None
for p in calm_files:
    r = try_sample(p, CALM_TEST)
    if not r: continue
    sim = same(p) if same else 0.5
    total = r[0] + 0.6 * sim
    print(f"calm sample {os.path.basename(p)}: score {r[0]:.2f}  same-voice {sim:.2f} | says: {r[3][:70]!r}")
    if sim < 0.35: print("   skipped: sounds like a different speaker"); continue
    if best_calm is None or total > best_calm[0]: best_calm = (total,) + r[1:]

bob_hot = best_hot[1]
bob = best_calm[1] if best_calm else bob_hot
bob.save("/content/bob.pt"); bob_hot.save("/content/bob_hot.pt")
print("\nCalm voice:" if best_calm else "\nNo calm sample uploaded, so Bob uses one voice:")
sf.write("/content/bob_test_calm.wav", (best_calm or best_hot)[2], SR); display(Audio("/content/bob_test_calm.wav"))
print("Excited voice:")
sf.write("/content/bob_test_hot.wav", best_hot[2], SR); display(Audio("/content/bob_test_hot.wav"))
