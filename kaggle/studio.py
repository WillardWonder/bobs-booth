# Bob's Booth studio, built for a Kaggle notebook (also runs on Colab).
# The notebook downloads this file from the bobs-booth GitHub repo and runs it.
#
# live   (default) finds today's Brewers World Series game, goes on the air 30 minutes before first pitch,
#        calls every pitch in Bob's cloned voice about 15 seconds behind the field (never more than about 20),
#        fills the gaps the way a radio man does, and saves the broadcast as half-inning replay files.
# render re-calls finished games from MLB's pitch-by-pitch data and saves them as replay files.
# test   loads everything, measures voice speed and renders the first two innings of Game 1.
import os, sys, re, json, time, random, threading, subprocess, socket, glob, difflib, shutil, urllib.request, traceback
from datetime import datetime

ON_KAGGLE = os.path.exists("/kaggle/working")
ON_COLAB = not ON_KAGGLE and ("COLAB_RELEASE_TAG" in os.environ or os.path.exists("/content"))
WORK = "/kaggle/working" if ON_KAGGLE else "/content"
MODE = os.environ.get("BOB_MODE", "live").strip().lower()
RENDER = [x for x in os.environ.get("BOB_RENDER", "").replace(" ", "").split(",") if x]
NUM_STEP = int(os.environ.get("BOB_STEPS", "16"))       # voice decoding steps (OmniVoice's default is 32)
DELAY = float(os.environ.get("BOB_DELAY", "15"))        # target seconds behind the field when live
ON_AIR_EARLY = 30 * 60                                  # the pregame show starts this long before first pitch
SEASON, BREWERS, SR = 2026, 158, 24000
TOPIC = "bobs-booth-mke-4q7v2k"
FFMPEG, CLOUDFLARED = "ffmpeg", "cloudflared"
WRITE = CAST = None


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ================================================================ setup
def setup():
    global FFMPEG, CLOUDFLARED
    need = []
    for mod, pkg in (("omnivoice", "omnivoice"), ("flask", "flask"), ("google.genai", "google-genai"), ("num2words", "num2words")):
        try:
            __import__(mod)
        except ImportError:
            need.append(pkg)
    if not shutil.which("ffmpeg"):
        need.append("imageio-ffmpeg")
    if need:
        log("installing", " ".join(need))
        subprocess.run([sys.executable, "-m", "pip", "install", "-q"] + need, check=False)
    if not shutil.which("ffmpeg"):
        import imageio_ffmpeg
        FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
    CLOUDFLARED = shutil.which("cloudflared") or os.path.join(os.path.dirname(WORK), "cloudflared")
    if not os.path.exists(CLOUDFLARED):
        subprocess.run(["wget", "-q", "-O", CLOUDFLARED,
                        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"], check=False)
        os.chmod(CLOUDFLARED, 0o755)


def secret(name):
    if os.environ.get(name):
        return os.environ[name]
    if ON_KAGGLE:
        try:
            from kaggle_secrets import UserSecretsClient
            return UserSecretsClient().get_secret(name) or None
        except Exception as e:
            log(f"secret {name} not available: {str(e)[:120]}")
            return None
    if ON_COLAB:
        try:
            from google.colab import userdata
            return userdata.get(name)
        except Exception:
            return None
    return None


def to_mp3(src, dst):
    return subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", src, "-ac", "1", "-b:a", "48k", dst]).returncode == 0


# ================================================================ MLB data
def getj(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "bobs-booth"}), timeout=20) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 + 2 * i)


def feed(pk):
    return getj(f"https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live")


def ts(d):
    return datetime.fromisoformat(d.replace("Z", "+00:00")).timestamp() if d else None


_games = {"t": 0, "data": []}


def series_games(force=False):
    if not force and time.time() - _games["t"] < 60:
        return _games["data"]
    out = []
    try:
        j = getj(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&teamId={BREWERS}&gameType=W&season={SEASON}&hydrate=linescore,team")
        for d in j.get("dates", []):
            for g in d["games"]:
                a, h = g["teams"]["away"], g["teams"]["home"]
                ls = g.get("linescore", {})
                out.append({"pk": g["gamePk"], "num": g.get("seriesGameNumber") or len(out) + 1, "date": g["gameDate"],
                            "state": g["status"]["abstractGameState"], "detail": g["status"]["detailedState"],
                            "away": a["team"]["name"], "home": h["team"]["name"],
                            "awayAb": a["team"].get("abbreviation", ""), "homeAb": h["team"].get("abbreviation", ""),
                            "awayRuns": a.get("score"), "homeRuns": h.get("score"),
                            "inning": (ls.get("inningHalf", "") + " " + str(ls.get("currentInningOrdinal", ""))).strip()})
        out.sort(key=lambda x: x["num"])
        _games.update(t=time.time(), data=out)
    except Exception as e:
        log("schedule error:", e)
    return _games["data"]


# ================================================================ what Bob knows and how he talks
FACTS = [
    "The Brewers began life as the Seattle Pilots in 1969 and moved to Milwaukee for the 1970 season.",
    "The 1982 Brewers, nicknamed Harvey's Wallbangers after manager Harvey Kuenn, led the majors in home runs.",
    "The 1982 World Series against St. Louis was called the Suds Series, and Milwaukee lost it in seven games.",
    "Robin Yount won two MVP awards, one as a shortstop in 1982 and one as a center fielder in 1989.",
    "Paul Molitor put together a thirty-nine game hitting streak in 1987.",
    "Hank Aaron finished his career in Milwaukee, playing his last two seasons for the Brewers in 1975 and 1976.",
    "The Brewers switched from the American League to the National League in 1998.",
    "American Family Field opened in 2001 as Miller Park, with a fan-shaped retractable roof.",
    "Prince Fielder hit fifty home runs for Milwaukee in 2007.",
    "Christian Yelich won the National League MVP award in 2018.",
    "The Racing Sausages run at Brewers home games, a Milwaukee tradition since the 1990s.",
    "Before the Brewers, Milwaukee had the Braves, who won the 1957 World Series.",
    "The Tampa Bay Rays started play in 1998 as the Devil Rays and reached the World Series in 2008 and 2020.",
    "This is the Brewers' first World Series since 1982, and they are chasing the first championship in team history.",
]
STYLES = ["Open with the pitcher.", "Open with the batter.", "Open with the count or the situation.",
          "Keep it brisk and punchy.", "Let it breathe a little: warm and unhurried.",
          "Work in one dry, self-deprecating joke about yourself.", "Use a vivid verb for the pitch or the contact.",
          "Mention the score or the runners naturally.", "Sound like you are leaning toward the microphone."]
HITS = {"single", "double", "triple", "home_run"}
NOT_AB = {"walk", "intent_walk", "hit_by_pitch", "sac_fly", "sac_bunt", "catcher_interf", "sac_fly_double_play", "sac_bunt_double_play"}
BOB = """You are Bob, the longtime radio play-by-play voice of the Milwaukee Brewers, calling the World Series for folks listening at home.
How Bob sounds:
- Conversational, like a friend on the porch with a radio. Warm, dry, unhurried. Never a shouting TV voice.
- Routine pitches and plays get a short, understated call, sometimes with a wry little aside.
- Big moments build: the windup, the swing, the ball in flight, stretch one word as it carries, then let it land, then give the score.
- Paint the picture for someone who cannot see it: where the ball went, who ran, who made the play.
- Fills quiet stretches the way an old radio man does: the count and the situation, a note about the hitter or pitcher, a story.
- Self-deprecating humor about Bob's own made-up playing days as a forgettable backup catcher in the low minors: splinters from the bench, a batting average nobody should repeat, managers who forgot his name.
- Folksy, old-fashioned phrasing. Talks to the listener directly now and then.
- Never repeats himself: vary sentence openings, verbs, and jokes.
Rules: use ONLY the game facts you are given; never invent stats, names, injuries, weather, crowd size or events about real people.
The only made-up material allowed is Bob's own harmless tall tales about himself. Do not use any real broadcaster's catchphrases.
Keep it clean and family-friendly, like a broadcast grandparents and kids enjoy together. Never use words or phrasing that could read as a double meaning;
prefer plain baseball verbs such as throws, deals, fires, delivers, swings, lines, lifts, grounds, rolls.
Spell out all numbers as words. Plain spoken text only: no emojis, stage directions, quotation marks, lists or markdown.
When Bob genuinely tickles himself with one of his own jokes, you may write [laughter] right after it, but rarely."""
RISKY = re.compile(r"\b(blow|blows|blew|blown|pump|pumps|pumped|pumping|stroke|stroked|strokes|wood|sticks it|slides it in|hard one|climax|thrust|erect|shaft|nuts)\b", re.I)

BIG = (" This is a big moment. Write it in two parts separated by a single | character. Before the |: calm and conversational, "
       "the setup and the key pitch. After the |: excited, the result and the immediate reaction, one or two short sentences.")
PITCH = ("Call the pitch or pitches below as they happen, like live radio: the pitch and the count first, in a few words. "
         "If the length allows, add a little color: the situation, the hitter, the pitcher, or a dry aside.")
RESULT = ("Call how this plate appearance ended, like live radio: the decisive pitch, the result with energy that fits how big it is, "
          "then the score or situation if it changed. A routine out gets a brisk call; a hit, run or strikeout gets more.")
ACTION = "Something happened on the field between pitches. Tell listeners briefly and naturally, then come back to the game."
CHANGE = ("There is a pitching change. Tell listeners who is coming in, with one or two details from the background, "
          "and set up the situation he walks into.")
BREAK = ("That ends the half inning. Wrap it up warmly: a quick recap of the half and the score, then share the item to share "
         "in your own words if there is one, then lead into the next half.")
FILL = ("There is a quiet stretch with nothing new on the field (a break, a visit to the mound, a pitching change, or a pause). "
        "Write {n} separate short segments to fill it the way a veteran radio man does, separated by a line with only ||. "
        "Each segment picks a different ONE of: a short tall tale about your own forgettable playing days, a note about the hitter "
        "or pitcher from the background, the situation and what each side wants here, the series, or the item to share. "
        "Each segment ends by coming back to the game. Each about {w} words. Do not invent any new play.")
CATCHUP = ("Several things happened while you were talking. Catch listeners up quickly and smoothly, most important play first, "
           "then the score and the situation now.")


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def bases_text(b):
    names = [n for n, on in zip(("first", "second", "third"), b) if on]
    return "bases empty" if not names else ("bases loaded" if len(names) == 3 else "runners on " + " and ".join(names))


def bio(g, pid):
    p = g["gameData"]["players"].get(f"ID{pid}", {})
    bits = [p.get("fullName", "")]
    if p.get("currentAge"):
        bits.append(f"age {p['currentAge']}")
    place = ", ".join(x for x in (p.get("birthCity"), p.get("birthStateProvince") or p.get("birthCountry")) if x)
    if place:
        bits.append("born in " + place)
    if (p.get("primaryPosition") or {}).get("name"):
        bits.append(p["primaryPosition"]["name"].lower())
    if p.get("height"):
        bits.append(p["height"].replace("' ", " foot ").replace('"', ""))
    if p.get("mlbDebutDate"):
        bits.append("big-league debut in " + p["mlbDebutDate"][:4])
    if p.get("nickName"):
        bits.append("nickname " + p["nickName"])
    return ", ".join(b for b in bits if b)


def clean(t):
    t = re.sub(r"\[(?!laughter\]|sigh\])[^\]]*\]", "", t)          # keep only the two sounds Bob may make
    t = re.sub(r"[*_#\"“”]", "", t)
    return re.sub(r"[ \t]+", " ", t).strip()


def caption(t):
    return re.sub(r"\s+", " ", re.sub(r"\s*\|\s*", " ", clean(t)).replace("[laughter]", "").replace("[sigh]", "")).strip()


def words_of(t):
    return re.sub(r"[^a-z' ]", " ", t.lower()).split()


# ================================================================ the writer (Gemini)
class Writer:
    def __init__(self):
        self.client, self.colab, self.models, self.cool, self.cfg = None, None, [], {}, {}
        self.count, self.fail, self.lat = 0, 0, 2.0
        key = secret("GEMINI_API_KEY")
        if key:
            from google import genai
            self.client = genai.Client(api_key=key)
            self.models = self.pick_models()
        elif ON_COLAB:
            try:
                from google.colab import ai
                self.colab = ai
                log("writer: Colab's built-in Gemini")
            except Exception:
                pass
        if self.client is None and self.colab is None:
            log("WARNING: no GEMINI_API_KEY secret, so Bob can only read the plain play descriptions.")

    def pick_models(self):
        names = []
        try:
            names = [m.name.split("/")[-1] for m in self.client.models.list()]
        except Exception as e:
            log("could not list Gemini models:", e)
        ok = [n for n in names if "flash" in n and not re.search(r"image|tts|audio|live|embed|exp|thinking|8b", n)]

        def ver(n):
            m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
            return float(m.group(1)) if m else 0
        order = sorted(ok, key=lambda n: ("latest" not in n, "preview" in n, -ver(n), len(n)))
        full = [n for n in order if "lite" not in n][:2] + [n for n in order if "lite" in n][:2]
        if not full:
            full = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-flash-lite-latest", "gemini-2.5-flash-lite"]
        log("writer: Gemini models, in order of preference:", full)
        return full

    def configs(self, m):
        from google.genai import types
        out = []
        try:
            out.append(types.GenerateContentConfig(temperature=1.0, max_output_tokens=1024,
                                                   thinking_config=types.ThinkingConfig(thinking_budget=0)))
        except Exception:
            pass
        out.append(types.GenerateContentConfig(temperature=1.0, max_output_tokens=4096))
        k = self.cfg.get(m)
        return out[k:] if k is not None else out          # once a config works for a model, start with it

    def __call__(self, prompt):
        t0 = time.time()
        if self.colab is not None:
            t = self.colab.generate_text(prompt).strip()
            self.count += 1; self.lat = .8 * self.lat + .2 * (time.time() - t0)
            return t
        if self.client is None:
            raise RuntimeError("no writer available")
        last = None
        for m in self.models:
            if self.cool.get(m, 0) > time.time():
                continue
            cfgs = self.configs(m)
            for k, cfg in enumerate(cfgs):
                try:
                    r = self.client.models.generate_content(model=m, contents=prompt, config=cfg)
                    t = (r.text or "").strip()
                    if t:
                        if m not in self.cfg:
                            self.cfg[m] = k
                        self.count += 1; self.lat = .8 * self.lat + .2 * (time.time() - t0)
                        return t
                except Exception as e:
                    s, last = str(e), e
                    if "429" in s or "RESOURCE_EXHAUSTED" in s or "quota" in s.lower():
                        day = "PerDay" in s or "per day" in s.lower()
                        self.cool[m] = time.time() + (3 * 3600 if day else 40)
                        log(f"writer: {m} is rate limited ({'for the day' if day else 'for a minute'}); trying the next model")
                        break
                    if "thinking" in s.lower() or "INVALID_ARGUMENT" in s:
                        continue
                    if "503" in s or "UNAVAILABLE" in s or "overloaded" in s.lower() or "500" in s:
                        self.cool[m] = time.time() + 30          # busy right now; use the next model for a bit
                        log(f"writer: {m} is busy; trying the next model")
                        break
                    log("writer error:", s[:200])
                    break
        self.fail += 1
        raise RuntimeError(f"writer unavailable: {str(last)[:120]}")


def ask(task, facts, mem, fallback, words=None):
    recent = ("Your last lines on the air. Do NOT reuse their openings, phrases or jokes:\n" + "\n".join("- " + x for x in mem[-6:]) + "\n\n") if mem else ""
    size = f" Aim for about {int(words)} words in total." if words else ""
    p = f"{BOB}\n\n{recent}Style for this one: {random.choice(STYLES)}\n\nTask: {task}{size}\n\nFacts:\n{facts}"
    try:
        t = clean(WRITE(p))
        if t and RISKY.search(t):
            t = clean(WRITE(p + "\n\nRewrite this with plainer, family-friendly wording and no possible double meaning: " + t))
        if t and not RISKY.search(t):
            return t
    except Exception as e:
        log("writer:", str(e)[:160])
    return fallback


# ================================================================ the voice (OmniVoice)
def spoken(text):
    """Text as Bob should say it: numbers in words, only the two allowed sounds, a clean ending."""
    try:
        from num2words import num2words
        text = re.sub(r"\b(\d{1,4})\b", lambda m: num2words(int(m.group(1))), text)
    except Exception:
        pass
    text = re.sub(r"\s+", " ", clean(text)).strip()
    if text and text[-1] not in ".!?]":
        text += "."
    return text


class Voice:
    def __init__(self):
        import torch
        from omnivoice import OmniVoice
        n = torch.cuda.device_count()
        self.models = []
        for i in range(max(1, min(2, n))):
            dev = f"cuda:{i}" if n else "cpu"
            log(f"loading OmniVoice on {dev}")
            self.models.append(OmniVoice.from_pretrained("k2-fsa/OmniVoice", device_map=dev,
                                                         dtype=torch.float16 if n else torch.float32, load_asr=(i == 0)))
        self.locks = [threading.Lock() for _ in self.models]
        self.asr_lock = threading.Lock()
        self.prompts = [dict() for _ in self.models]
        self.rtf = 0.6          # seconds of work per second of speech, measured as we go
        self.wps = 2.6          # Bob's words per second, measured as we go

    # ---- choosing Bob's two reference samples
    @staticmethod
    def find(name):
        hits = glob.glob(f"/kaggle/input/**/{name}", recursive=True) + glob.glob(f"/content/{name}") + glob.glob(f"{WORK}/{name}")
        return hits[0] if hits else None

    def try_sample(self, path, test):
        m = self.models[0]
        ref_text = m.transcribe(path)
        if not ref_text.strip():
            return None
        prompt = m.create_voice_clone_prompt(ref_audio=path, ref_text=ref_text)
        out = m.generate(text=test, voice_clone_prompt=prompt, language="English", num_step=NUM_STEP)[0]
        heard = words_of(m.transcribe((out, SR)))
        score = difflib.SequenceMatcher(None, words_of(test), heard).ratio() - (0.3 if heard[:1] != words_of(test)[:1] else 0)
        return score, ref_text, out

    def choose(self):
        import numpy as np, soundfile as sf
        vd = os.path.join(WORK, "voices"); os.makedirs(vd, exist_ok=True)
        src = self.find("bobnew_11m30-14m15.mp3")
        if not src:
            raise SystemExit("Bob's voice samples are missing. Attach the private bob-voice dataset to this notebook.")
        long_wav = os.path.join(vd, "bob_long.wav")
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", src, "-ac", "1", "-ar", str(SR), long_wav], check=True)
        w, _ = sf.read(long_wav, dtype="float32")
        # excited voice: the steadiest complete-sentence stretches of the play-by-play, cut in real pauses
        hop = int(.02 * SR)
        env = np.sqrt(np.convolve(w ** 2, np.ones(hop) / hop, "same"))[::hop]
        talk = env > .12 * np.percentile(env, 95)
        pauses, i = [], 0
        while i < len(talk):
            if talk[i]:
                i += 1; continue
            j = i
            while j < len(talk) and not talk[j]:
                j += 1
            if j - i >= 15:
                pauses.append((i + j) // 2)
            i = j
        cands = []
        for a in range(len(pauses)):
            for b in range(a + 1, len(pauses)):
                dur = (pauses[b] - pauses[a]) * hop / SR
                if dur > 10:
                    break
                if dur < 5:
                    continue
                seg = env[pauses[a]:pauses[b]]; voiced = seg[seg > .12 * np.percentile(env, 95)]
                if len(voiced) >= 50:
                    cands.append((np.percentile(voiced, 95) / (np.median(voiced) + 1e-6), pauses[a] * hop, pauses[b] * hop))
        cands.sort()
        hot = []
        for ev, s0, s1 in cands:
            if all(s1 <= a or s0 >= b for a, b in hot):
                hot.append((s0, s1))
            if len(hot) == 3:
                break
        best_hot = None
        for k, (s0, s1) in enumerate(hot):
            p = os.path.join(vd, f"bob_hot_{k}.wav"); sf.write(p, w[s0:s1], SR)
            r = self.try_sample(p, "Swung on and drilled to deep left field, it is off the wall, and here comes the run to score!")
            if r:
                log(f"excited sample {k}: score {r[0]:.2f} | {r[1][:60]!r}")
                if best_hot is None or r[0] > best_hot[0]:
                    best_hot = (r[0], p, r[1], r[2])
        # calm voice: bob_calm_3 matched the excited voice best in the speaker check (0.40), bob_calm_4 next (0.35)
        calm_files = [f for f in (self.find("bob_calm_3.wav"), self.find("bob_calm_4.wav")) if f]
        best_calm = None
        for p in calm_files:
            r = self.try_sample(p, "Good evening everybody. Two balls and one strike on the batter, and the infield shifts a step toward first.")
            if r:
                log(f"calm sample {os.path.basename(p)}: score {r[0]:.2f} | {r[1][:60]!r}")
                if best_calm is None or r[0] > best_calm[0] + 0.05:
                    best_calm = (r[0], p, r[1], r[2])
        best_calm = best_calm or best_hot
        sf.write(os.path.join(vd, "test_excited.wav"), best_hot[3], SR)
        sf.write(os.path.join(vd, "test_calm.wav"), best_calm[3], SR)
        json.dump({"excited": best_hot[1:3], "calm": best_calm[1:3]}, open(os.path.join(vd, "voices.json"), "w"), indent=1)
        for i, m in enumerate(self.models):
            self.prompts[i]["hot"] = m.create_voice_clone_prompt(ref_audio=best_hot[1], ref_text=best_hot[2])
            self.prompts[i]["calm"] = m.create_voice_clone_prompt(ref_audio=best_calm[1], ref_text=best_calm[2])
        log("voices ready:", os.path.basename(best_calm[1]), "(calm) and", os.path.basename(best_hot[1]), "(excited)")

    # ---- making audio
    @staticmethod
    def tidy(w):
        import numpy as np
        w = w.cpu().numpy() if hasattr(w, "cpu") else np.asarray(w)
        w = w.astype(np.float32).squeeze()
        if w.size and np.abs(w).max() > 0:
            thr = 0.03 * float(np.abs(w).max()); idx = np.where(np.abs(w) > thr)[0]
            w = w[max(0, idx[0] - int(.05 * SR)): idx[-1] + int(.12 * SR)]
        return w

    def leak_guard(self, w, text):
        """If the clone starts with a stray word that isn't in the line (like "here"), cut it off."""
        try:
            with self.asr_lock:
                out = self.models[0]._asr_pipe({"array": w, "sampling_rate": SR}, return_timestamps="word")
            chunks = out.get("chunks") or []
            heard = [(words_of(c["text"]) or [""])[0] for c in chunks[:4]]
            want = words_of(text)[:2]
            if not heard or not want or heard[0] == want[0]:
                return w
            for k in range(1, len(heard)):
                if heard[k] == want[0] and (len(want) < 2 or k + 1 >= len(heard) or heard[k + 1] == want[1]):
                    start = chunks[k]["timestamp"][0]
                    if start:
                        log("  trimmed stray audio:", " ".join(heard[:k]))
                        return w[max(0, int((start - .06) * SR)):]
        except Exception as e:
            log("leak guard skipped:", str(e)[:120])
        return w

    def say(self, text, hot=False):
        """Calm voice for everything; a big moment's setup stays calm and its payoff (after the |) is excited.
        With two GPUs, the sentences of a line are voiced on both at once."""
        import numpy as np
        parts = [spoken(t) for t in text.split("|") if t.strip()]
        if hot and len(parts) == 1:
            s = re.split(r"(?<=[.!?])\s+", parts[0])
            parts = [" ".join(s[:-1]), s[-1]] if len(s) > 1 else parts
        segs = []                                   # (sentence, voice)
        for i, part in enumerate(p for p in parts if p):
            kind = "hot" if hot and i == len(parts) - 1 else "calm"
            sents = re.split(r"(?<=[.!?])\s+", part) if len(self.models) > 1 else [part]
            segs += [(s, kind) for s in sents if s.strip()]
        if not segs:
            return np.zeros(int(.3 * SR), np.float32)
        groups = [[] for _ in self.models]
        load = [0] * len(self.models)
        for idx, (s, k) in enumerate(segs):          # balance the words across GPUs
            gi = load.index(min(load)); groups[gi].append(idx); load[gi] += len(s.split())
        res = [None] * len(segs)
        t0 = time.time()

        def run(gi):
            if not groups[gi]:
                return
            with self.locks[gi]:
                m = self.models[gi]
                outs = m.generate(text=[segs[i][0] for i in groups[gi]], voice_clone_prompt=[self.prompts[gi][segs[i][1]] for i in groups[gi]],
                                  language="English", num_step=NUM_STEP)
            for i, o in zip(groups[gi], outs):
                res[i] = self.tidy(o)
        th = [threading.Thread(target=run, args=(gi,)) for gi in range(1, len(self.models))]
        [t.start() for t in th]
        run(0)
        [t.join() for t in th]
        res = [self.leak_guard(r, s[0]) if i == 0 or segs[i - 1][1] != s[1] else r for i, (r, s) in enumerate(zip(res, segs))]
        gap = np.zeros(int(.16 * SR), np.float32)
        seq = []
        for r in res:
            seq += [r, gap]
        w = np.concatenate(seq[:-1] + [np.zeros(int(.08 * SR), np.float32)])
        dur = len(w) / SR
        if dur > 1:
            self.rtf = .7 * self.rtf + .3 * ((time.time() - t0) / dur)
            n = len(words_of(" ".join(s for s, _ in segs)))
            if n >= 5:
                self.wps = .8 * self.wps + .2 * (n / dur)
        return w


# ================================================================ the scoreboard
class Box:
    """Running scoreboard built play by play, so a broadcast never reveals what has not happened yet."""

    def __init__(s, g):
        t = g["gameData"]["teams"]
        s.ab = {k: t[k].get("abbreviation") or t[k]["name"][:3].upper() for k in ("away", "home")}
        s.team = {k: t[k].get("teamName") or t[k]["name"] for k in ("away", "home")}
        s.line = {"away": [], "home": []}; s.hits = {"away": 0, "home": 0}; s.score = {"away": 0, "home": 0}
        s.cur = None; s.start = dict(s.score); s.half_hits = 0; s.outs = 0; s.bases = [0, 0, 0]
        s.bat = {}; s.pc = {}; s.inning = 1; s.half = "top"; s.batter = ""; s.pitcher = ""; s.balls = s.strikes = 0
        s.started = False

    @staticmethod
    def side(half):
        return "away" if half == "top" else "home"

    def enter(s, p):
        a = p["about"]; half, inn = a["halfInning"], a["inning"]; side = s.side(half)
        s.started = True
        if (inn, half) != s.cur:
            s.cur = (inn, half); s.outs = 0; s.bases = [0, 0, 0]; s.start = dict(s.score); s.half_hits = 0
            while len(s.line[side]) < inn:
                s.line[side].append(None)
            s.line[side][inn - 1] = 0
        s.inning, s.half = inn, half
        m = p.get("matchup", {})
        s.batter, s.pitcher = m.get("batter", {}).get("fullName", ""), m.get("pitcher", {}).get("fullName", "")

    def pitch(s, e):
        c = e.get("count") or {}
        s.balls, s.strikes = c.get("balls", s.balls), c.get("strikes", s.strikes)

    def apply(s, p):
        s.enter(p)
        a, r, m = p["about"], p["result"], p.get("matchup", {})
        side = s.side(a["halfInning"]); inn = a["inning"]
        bid, pid = m.get("batter", {}).get("id"), m.get("pitcher", {}).get("id")
        before = {"outs": s.outs, "bases": list(s.bases), "score": dict(s.score), "bat": s.bat.get(bid, (0, 0, 0)), "pc": s.pc.get(pid, 0)}
        s.score = {"away": r.get("awayScore", s.score["away"]), "home": r.get("homeScore", s.score["home"])}
        s.line[side][inn - 1] = s.score[side] - s.start[side]
        hit = r.get("eventType") in HITS
        if hit:
            s.hits[side] += 1; s.half_hits += 1
        h, ab, pa = before["bat"]; s.bat[bid] = (h + hit, ab + (r.get("eventType") not in NOT_AB), pa + 1)
        s.pc[pid] = before["pc"] + sum(1 for e in p.get("playEvents", []) if e.get("isPitch"))
        s.outs = p.get("count", {}).get("outs", s.outs)
        s.bases = [0, 0, 0] if s.outs >= 3 else [1 if m.get(k) else 0 for k in ("postOnFirst", "postOnSecond", "postOnThird")]
        s.balls = s.strikes = 0
        return before

    def sc(s, score=None):
        score = score or s.score
        return f"{s.team['away']} {score['away']}, {s.team['home']} {score['home']}"

    def situation(s):
        return (f"{s.half.title()} of the {ordinal(s.inning)}, {min(s.outs, 3)} out, {bases_text(s.bases)}, score {s.sc()}, "
                f"count {s.balls} and {s.strikes}, {s.batter or 'the batter'} batting against {s.pitcher or 'the pitcher'}.")

    def board(s):
        if not s.started:
            return None
        return json.loads(json.dumps({"ab": s.ab, "team": s.team, "line": s.line, "hits": s.hits, "score": s.score, "outs": min(s.outs, 3),
                "bases": s.bases, "inning": s.inning, "half": s.half, "batter": s.batter, "pitcher": s.pitcher,
                "balls": s.balls, "strikes": s.strikes}))          # a frozen copy: later plays must not change it

    def key(s):
        return f"{'t' if s.half == 'top' else 'b'}{s.inning}"


# ================================================================ reading the game as a stream of events
SKIP_ACTIONS = {"game_advisory", "batter_timeout"}


def pitch_line(e):
    d = e.get("details", {})
    spd = (e.get("pitchData") or {}).get("startSpeed"); c = e.get("count", {})
    t = f"{(d.get('type') or {}).get('description', 'pitch')}{f' {round(spd)} mph' if spd else ''}, {(d.get('description') or '').lower()}"
    if not d.get("isInPlay"):
        t += f" (count now {c.get('balls')} and {c.get('strikes')})"
    hd = e.get("hitData")
    if hd:
        bits = [f"off the bat at {round(hd['launchSpeed'])} mph" if hd.get("launchSpeed") else "",
                f"traveled {round(hd['totalDistance'])} feet" if hd.get("totalDistance") else "",
                (hd.get("trajectory") or "").replace("_", " ")]
        t += ". Contact: " + ", ".join(b for b in bits if b)
    return t


def events_of(g, seen, done, hold_in_play=True):
    """What is new since last time, in order: pitches, actions (mound visits, pitching changes, steals...) and
    finished plate appearances. `seen` and `done` remember what was already reported."""
    out = []
    for p in g["liveData"]["plays"]["allPlays"]:
        ai = p["about"].get("atBatIndex")
        complete = p["about"].get("isComplete")
        for e in p.get("playEvents", []):
            k = (ai, e.get("index"))
            if k in seen:
                continue
            d = e.get("details", {})
            when = ts(e.get("endTime") or e.get("startTime"))
            if e.get("isPitch"):
                if not d.get("description"):
                    continue
                c = e.get("count") or {}
                ends_it = d.get("isInPlay") or c.get("balls", 0) >= 4 or c.get("strikes", 0) >= 3 or "hit by pitch" in d["description"].lower()
                if ends_it and not complete and hold_in_play:
                    continue                                   # the pitch that ends an at-bat is called with its result
                seen.add(k); out.append({"kind": "pitch", "p": p, "e": e, "t": when})
            else:
                seen.add(k)
                if (d.get("eventType") or "") in SKIP_ACTIONS or e.get("type") == "stepoff" or not d.get("description"):
                    continue
                out.append({"kind": "action", "p": p, "e": e, "t": when})
        if complete and ai not in done:
            done.add(ai)
            out.append({"kind": "result", "p": p, "e": None, "t": ts(p["about"].get("endTime")) or (out[-1]["t"] if out else None)})
    return out


def big_moment(p, before, box):
    et = p["result"].get("eventType", "")
    if et in ("home_run", "triple", "triple_play") or p["about"].get("isScoringPlay"):
        return True
    if et in ("grounded_into_double_play", "double_play", "strikeout_double_play"):
        return True
    risp = before["bases"][1] or before["bases"][2]
    if risp and et in ("single", "double"):
        return True
    return bool(risp and before["outs"] == 2 and box.outs >= 3)


def etype(e):
    return ((e.get("e") or {}).get("details") or {}).get("eventType") or ""


# ================================================================ the broadcast
class Broadcast:
    """One shared broadcast. Every clip has a place on a single timeline (`at`, in epoch seconds), so every
    listener hears the same moment. With a recorder attached, it is also saved as replay files."""

    def __init__(self, sim=None):
        self.lock = threading.Lock()
        self.clips, self.n, self.air_end = [], 0, 0.0
        self.on, self.status, self.label, self.game, self.board = False, "Bob's studio is warming up.", "", None, None
        self.dir = os.path.join(WORK, "live", "clips"); os.makedirs(self.dir, exist_ok=True)
        self.recorder = None
        self.sim = sim                  # render mode: a function giving the simulated time

    def now(self):
        return self.sim() if self.sim else time.time()

    def add(self, wav, text, kind, board, chunk, not_before=0.0, pause=0.35, evt=None):
        import soundfile as sf
        with self.lock:
            self.n += 1; n = self.n
            at = max(not_before or 0.0, self.air_end + pause, self.now() + (0.0 if self.sim else 0.8))
            dur = len(wav) / SR
            self.air_end = at + dur
        path = os.path.join(self.dir, f"{n}.wav")
        sf.write(path, wav, SR)
        url = None
        if not self.sim:
            url = f"clip/{n}.mp3" if to_mp3(path, os.path.join(self.dir, f"{n}.mp3")) else f"clip/{n}.wav"
        c = {"id": n, "text": caption(text), "kind": kind, "board": board, "at": round(at, 3), "dur": round(dur, 3), "chunk": chunk, "url": url,
             "evt": round(evt, 2) if evt else None}
        with self.lock:
            self.clips.append(c); self.clips = self.clips[-400:]
            if board:
                self.board = board
        if self.recorder:
            self.recorder.add(c, path)
        when = time.strftime('%H:%M:%S', time.gmtime(at)) if self.sim else f"in {at - self.now():4.1f}s" + (f", {at - evt:4.1f}s behind" if evt else "")
        log(f"[{n}] {when} {dur:4.1f}s {kind}: {c['text']}")
        return c

    def backlog(self):
        return max(0.0, self.air_end - self.now())

    def snapshot(self, since=0):
        now = time.time()
        with self.lock:
            clips = [{k: c[k] for k in ("id", "text", "kind", "board", "at", "dur", "url")}
                     for c in self.clips if c["id"] > since and c["at"] + c["dur"] > now - 30]
            return {"on": self.on, "status": self.status, "label": self.label, "game": self.game, "now": now,
                    "delay": DELAY, "board": self.board, "clips": clips}


def chunk_label(key):
    if key in ("pregame", "postgame"):
        return key.title()
    m = re.match(r"([tb])(\d+)", key)
    return f"{'Top' if m.group(1) == 't' else 'Bottom'} {ordinal(int(m.group(2)))}" if m else key


class Recorder:
    """Saves the broadcast as replay files: one MP3 per half inning (plus pregame and postgame) and a game.json
    timeline with every line, where it starts in its file, and the scoreboard at that moment."""

    def __init__(self, root, meta):
        self.root = root; os.makedirs(root, exist_ok=True)
        self.meta = dict(meta, chunks=[], complete=False)
        self.cur, self.parts, self.lock = None, [], threading.Lock()

    def add(self, c, wav_path):
        with self.lock:
            if self.cur and c["chunk"] != self.cur:
                self._flush()
            if self.cur is None:
                self.cur, self.parts = c["chunk"], []
            self.parts.append((c, wav_path))

    def _flush(self):
        import numpy as np, soundfile as sf
        parts, key = self.parts, self.cur
        self.cur, self.parts = None, []
        if not parts:
            return
        audio, lines, t = [], [], 0.0
        prev_end = parts[0][0]["at"]
        for c, path in parts:
            gap = min(max(0.0, c["at"] - prev_end), 2.5)          # natural pauses, never long dead air
            if gap > 0.05:
                audio.append(np.zeros(int(gap * SR), np.float32)); t += gap
            w, _ = sf.read(path, dtype="float32")
            lines.append({"t": round(t, 2), "dur": round(len(w) / SR, 2), "text": c["text"], "kind": c["kind"], "board": c["board"]})
            audio.append(w); t += len(w) / SR
            prev_end = c["at"] + c["dur"]
        audio.append(np.zeros(int(.6 * SR), np.float32)); t += .6
        name = f"{len(self.meta['chunks']):02d}-{key}"
        wav = os.path.join(self.root, name + ".wav")
        sf.write(wav, np.concatenate(audio), SR)
        to_mp3(wav, os.path.join(self.root, name + ".mp3"))
        os.remove(wav)
        self.meta["chunks"].append({"file": name + ".mp3", "key": key, "label": chunk_label(key), "dur": round(t, 2), "lines": lines})
        self.meta["duration"] = round(sum(ch["dur"] for ch in self.meta["chunks"]), 1)
        self._save()
        log(f"saved replay part {name} ({t / 60:.1f} min)")

    def _save(self):
        json.dump(self.meta, open(os.path.join(self.root, "game.json"), "w"), separators=(",", ":"))

    def close(self):
        with self.lock:
            self._flush()
            self.meta["complete"] = True
            self._save()


def game_meta(game, g):
    t = g["gameData"]["teams"]
    return {"pk": game["pk"], "num": game["num"], "date": game["date"], "away": t["away"]["name"], "home": t["home"]["name"],
            "awayAb": t["away"].get("abbreviation", ""), "homeAb": t["home"].get("abbreviation", "")}


# ================================================================ the director: what Bob says next
class Director:
    """Live: reads MLB's feed as it updates and keeps Bob about DELAY seconds behind the field.
    Render: re-calls a finished game, with the real gaps between pitches known ahead of time."""

    def __init__(self, g, game, cast, voice, render=False):
        self.g, self.game, self.cast, self.voice, self.render = g, game, cast, voice, render
        self.box = Box(g); self.mem = []; self.people = set(); self.facts_left = list(FACTS)
        self.seen, self.done = set(), set()
        self.chunk = "pregame"; self.cur_p = None; self.hint = "pitch"; self.pool = []; self.pool_key = None

    # ---- speaking
    def max_secs(self):
        """Longest line that can still be written and voiced inside the delay."""
        if self.render:
            return 30
        return max(4.0, (DELAY - 4.0 - WRITE.lat) / max(self.voice.rtf, 0.25))

    def words_for(self, secs):
        return max(5, min(75, min(secs, self.max_secs()) * self.voice.wps))

    def say(self, task, facts, fallback, kind, secs, hot=False, event_t=None, pause=0.35, text=None):
        text = text or ask(task + (BIG if hot else ""), facts, self.mem, fallback, words=self.words_for(secs))
        if not text:
            return None
        wav = self.voice.say(text, hot)
        self.mem.append(caption(text)); del self.mem[:-8]
        if self.render:
            not_before = (event_t + 1.5) if event_t else 0.0
        else:
            not_before = (event_t + DELAY) if event_t else 0.0
        return self.cast.add(wav, text, kind, self.box.board(), self.chunk, not_before, pause=pause, evt=event_t)

    # ---- context
    def series_so_far(self):
        num = self.game["num"]
        done = [x for x in series_games() if x["state"] == "Final" and x["num"] < num]
        if not done:
            return "This is the first game of the series."
        mil = sum(1 for x in done if (x["homeRuns"] > x["awayRuns"]) == ("Brewers" in x["home"]))
        return f"Series so far before this game: Brewers {mil} wins, Rays {len(done) - mil} wins. " + " ".join(
            f"Game {x['num']}: {x['away']} {x['awayRuns']}, {x['home']} {x['homeRuns']}." for x in done)

    def lineup(self, side):
        try:
            ids = self.g["liveData"]["boxscore"]["teams"][side].get("battingOrder") or []
            return ", ".join(self.g["gameData"]["players"][f"ID{i}"]["fullName"] for i in ids[:9])
        except Exception:
            return ""

    def game_facts(self):
        gd = self.g["gameData"]; t = gd["teams"]; pp = gd.get("probablePitchers") or {}
        f = [f"World Series Game {self.game['num']}: {t['away']['name']} at {t['home']['name']}, {(gd.get('venue') or {}).get('name', '')}.",
             self.series_so_far()]
        sp = [f"{pp[k]['fullName']} for the {self.box.team[k]}" for k in ("away", "home") if pp.get(k)]
        if sp:
            f.append("Starting pitchers: " + ", ".join(sp))
        return f

    def background(self, out, p=None, chance=0.5):
        m = (p or self.cur_p or {}).get("matchup", {})
        for who in (m.get("batter", {}).get("id"), m.get("pitcher", {}).get("id")):
            if who and who not in self.people and random.random() < chance:
                self.people.add(who); out.append("Background you may use one detail from: " + bio(self.g, who))

    def share_item(self, chance=1.0):
        if self.facts_left and random.random() < chance:
            return ["Item to share: " + self.facts_left.pop(random.randrange(len(self.facts_left)))]
        return []

    # ---- the pregame show
    def pregame(self, first_pitch, short=False):
        gd = self.g["gameData"]; pp = gd.get("probablePitchers") or {}
        segs = [("intro", "Open the broadcast: welcome listeners to World Series Game {n}, set the scene and the series. Do not hint at how it ends.", []),
                ("pregame", "Talk about the two starting pitchers using the background.",
                 ["Background on a starter: " + bio(self.g, pp[k]["id"]) for k in ("away", "home") if pp.get(k) and pp[k].get("id")]),
                ("pregame", "Go over the {away} batting order tonight, name by name, with a word about the top of the order.",
                 ["{away} batting order: " + (self.lineup("away") or "not posted yet; say so and move on")]),
                ("pregame", "Go over the {home} batting order tonight, name by name, with a word about the top of the order.",
                 ["{home} batting order: " + (self.lineup("home") or "not posted yet; say so and move on")]),
                ("pregame", "Share a Brewers history item in your own words, then tie it to tonight.", "item"),
                ("pregame", "Tell one short tall tale about your own forgettable playing days, then come back to tonight's game.", []),
                ("pregame", "Give listeners the stakes of tonight's game in the series and what each side needs.", [])]
        if short:
            segs = [segs[0], segs[1]]
        names = {"n": self.game["num"], "away": self.box.team["away"], "home": self.box.team["home"]}
        k = 0
        while True:
            now = self.cast.now()
            left = first_pitch - now
            if left < 40 or (short and k >= len(segs)):
                break
            if not self.render and self.cast.backlog() > 3:
                time.sleep(1); continue
            if k < len(segs):
                kind, task, extra = segs[k]
            else:
                kind, task, extra = random.choice(segs[4:] + [("pregame", "Talk about what to watch for early in tonight's game.", []),
                                                             ("pregame", "Share a gentle remark about life in the booth on a World Series night.", [])])
            extra = self.share_item() if extra == "item" else [x.format(**names) for x in extra]
            fallback = f"Hello everybody, Bob here with World Series Game {self.game['num']}." if k == 0 else ""
            secs = 24 if left > 120 else max(8, left / 3)
            self.say("The game has not started yet, so do not describe any pitch, count or play. " + task.format(**names),
                     "\n".join(self.game_facts() + extra), fallback, kind, secs, pause=0.35 if k == 0 else 3.0)
            k += 1
            if not self.render:
                # a relaxed pregame: a segment roughly every minute, leaving room for the next to be ready in time
                gap = 25 if left > 600 else 6
                while time.time() < self.cast.air_end + gap - 8 and first_pitch - time.time() > 40:
                    time.sleep(1)

    # ---- calling the game
    def handle(self, evs, eta_next=None):
        """Turn new events into Bob's next line(s). eta_next = seconds until the next event (known when rendering)."""
        if not evs:
            return
        behind = 0 if self.render else max(0.0, self.cast.backlog() - DELAY)
        results = [e for e in evs if e["kind"] == "result"]
        if not self.render and (len(results) >= 2 or behind > 8):
            lines = []
            for e in evs:
                if e["kind"] == "result":
                    self.box.apply(e["p"]); self.cur_p = e["p"]
                    lines.append(f"{e['p']['about']['halfInning'].title()} of the {ordinal(e['p']['about']['inning'])}: {e['p']['result'].get('description', '')}")
                elif e["kind"] == "pitch":
                    self.box.enter(e["p"]); self.box.pitch(e["e"]); self.cur_p = e["p"]
                    lines.append("Pitch: " + pitch_line(e["e"]))
            self.chunk = self.box.key()
            self.say(CATCHUP, "\n".join(lines[-8:]) + "\nNow: " + self.box.situation(), f"It's {self.box.sc()}.", "catchup", 12, event_t=evs[-1]["t"])
            self.hint = "result" if results else "pitch"
            self.inning_end()
            return
        group = []
        for e in evs:
            group.append(e)
            if e["kind"] == "result":
                self.call(group, eta_next if e is evs[-1] else 25, behind); group = []
        if group:
            self.call(group, eta_next, behind)

    def call(self, group, eta_next, behind):
        res = next((e for e in group if e["kind"] == "result"), None)
        pitches = [e for e in group if e["kind"] == "pitch"]
        actions = [e for e in group if e["kind"] == "action"]
        last = group[-1]
        self.cur_p = last["p"]
        self.box.enter(last["p"])
        for e in pitches:
            self.box.pitch(e["e"])
        self.chunk = self.box.key()
        change = [a for a in actions if etype(a) == "pitching_substitution"]
        if eta_next is None:
            eta_next = 28 if res else (100 if change else 17)
        room = max(3.0, eta_next - 1.5 - behind)
        if change and not res:
            a = change[-1]; pid = (a["e"].get("player") or {}).get("id")
            desc = a["e"]["details"].get("description", "")
            facts = desc + (("\nBackground on the new pitcher: " + bio(self.g, pid)) if pid else "") + "\nSituation: " + self.box.situation()
            self.say(CHANGE, facts, desc, "change", min(room, 20), event_t=a["t"])
            self.hint = "change"
            rest = [x for x in group if x is not a and x["kind"] != "action"]
            if rest:
                self.call(rest, eta_next, behind)
            return
        if res:
            p = res["p"]
            before = self.box.apply(p)
            hot = big_moment(p, before, self.box)
            self.say(RESULT, self.result_facts(p, before, actions), p["result"].get("description", ""), "play",
                     min(room, 24 if hot else 15), hot=hot, event_t=res["t"])
            self.hint = "result"
            self.inning_end()
            return
        lines = [pitch_line(e["e"]) for e in pitches]
        acts = [a["e"]["details"].get("description", "") for a in actions]
        facts = [f"Situation: {self.box.situation()}"]
        if lines:
            facts.append("Pitches: " + "; ".join(lines))
        if acts:
            facts.append("Also happened: " + " ".join(acts))
        self.background(facts)
        if any(etype(a) == "mound_visit" for a in actions):
            self.hint = "visit"
        else:
            self.hint = "pitch"
        self.say(PITCH if pitches else ACTION, "\n".join(facts), "; ".join(lines) or " ".join(acts),
                 "pitch" if pitches else "action", min(room, 13), event_t=last["t"])

    def result_facts(self, p, before, actions):
        a, r, m = p["about"], p["result"], p.get("matchup", {})
        h, ab, pa = before["bat"]
        today = "first trip to the plate today" if pa == 0 else f"{h} for {ab} so far today"
        f = [f"{a['halfInning'].title()} of the {ordinal(a['inning'])}. Before the play: {before['outs']} out, {bases_text(before['bases'])}, score {self.box.sc(before['score'])}.",
             f"Batter: {m.get('batter', {}).get('fullName')} of the {self.box.team[Box.side(a['halfInning'])]}, bats {m.get('batSide', {}).get('description', '').lower()}, {today}.",
             f"Pitcher: {m.get('pitcher', {}).get('fullName')}, throws {m.get('pitchHand', {}).get('description', '').lower()}, {before['pc']} pitches so far tonight.",
             "Pitches in this at-bat: " + "; ".join(pitch_line(e) for e in p.get("playEvents", []) if e.get("isPitch"))[-700:],
             f"Result: {r.get('description', '')}",
             f"After the play: {min(self.box.outs, 3)} out, {bases_text(self.box.bases)}, score {self.box.sc()}."]
        if r.get("rbi"):
            f.append(f"Runs batted in on the play: {r['rbi']}.")
        f += ["Also happened: " + x["e"]["details"].get("description", "") for x in actions]
        self.background(f, p, 0.6)
        return "\n".join(f)

    def inning_end(self):
        if self.box.outs < 3:
            return
        side = Box.side(self.box.half); team = self.box.team[side]
        runs = self.box.line[side][self.box.inning - 1] or 0
        f = [f"That ends the {self.box.half} of the {ordinal(self.box.inning)}. The {team} scored {runs} and had {self.box.half_hits} hits that half.",
             f"Score: {self.box.sc()}. Hits so far: {self.box.team['away']} {self.box.hits['away']}, {self.box.team['home']} {self.box.hits['home']}."]
        f += self.share_item(0.5)
        self.say(BREAK, "\n".join(f), f"That's the {self.box.half} of the {ordinal(self.box.inning)}. {self.box.sc()}.", "break", 16)
        self.hint = "break"
        self.pool = []

    def filler(self, secs):
        """Fill a quiet stretch, sized to how long it should last. One request writes a few segments at once."""
        key = (self.box.key(), self.box.outs, self.hint)
        if not self.pool or self.pool_key != key:
            facts = ["Situation: " + self.box.situation(), self.series_so_far()] + self.share_item(0.35)
            self.background(facts, chance=0.8)
            n = 3 if self.hint in ("break", "change") else 1
            w = self.words_for(min(secs, 14))
            text = ask(FILL.format(n=n, w=int(w)), "\n".join(facts), self.mem, "", words=None)
            self.pool = [s.strip() for s in re.split(r"\n\s*\|\|\s*\n|\s\|\|\s", text or "") if len(s.split()) >= 4]
            self.pool_key = key
        if not self.pool:
            return None
        return self.say(None, None, None, "story", secs, text=self.pool.pop(0), pause=0.8)

    def postgame(self, plays):
        self.chunk = "postgame"
        last = plays[-1]["result"].get("description", "") if plays else ""
        self.say("The game is over. Give the final score, one line on who made the difference based on the facts, and sign off warmly as Bob.",
                 f"Final: {self.box.sc()}. Hits: {self.box.team['away']} {self.box.hits['away']}, {self.box.team['home']} {self.box.hits['home']}. Last play: {last}",
                 f"And that's the ball game. Final score, {self.box.sc()}. This has been Bob.", "final", 22)


# ================================================================ live and render
def run_live(game, voice):
    cast = CAST
    pk = game["pk"]
    g = feed(pk)
    first_pitch = ts(game["date"])
    d = Director(g, game, cast, voice)
    cast.game = {k: game[k] for k in ("pk", "num", "away", "home", "date")}
    cast.label = f"Game {game['num']}: {game['away']} at {game['home']} - live"
    cast.recorder = Recorder(os.path.join(WORK, "replays", f"game-{game['num']}"), game_meta(game, g))
    if g["gameData"]["status"]["abstractGameState"] == "Preview":
        while time.time() < first_pitch - ON_AIR_EARLY:
            cast.status = f"warming up; on the air at {datetime.utcfromtimestamp(first_pitch - ON_AIR_EARLY).strftime('%Y-%m-%dT%H:%M:%SZ')}"
            time.sleep(10)
        g = feed(pk); d.g = g
        cast.on = True; cast.status = "On the air: pregame"
        d.pregame(first_pitch)
    cast.on = True; cast.status = "On the air"
    g = feed(pk); d.g = g
    first = events_of(g, d.seen, d.done)
    if any(e["kind"] == "result" for e in first):            # joining a game already under way
        for e in first:
            if e["kind"] == "result":
                d.box.apply(e["p"]); d.cur_p = e["p"]
        d.chunk = d.box.key()
        d.say("You are joining this game live. Welcome listeners and catch them up on the game so far.",
              "\n".join(d.game_facts() + ["Right now: " + d.box.situation()]), f"Bob here, joining the game. It's {d.box.sc()}.", "intro", 20)
    last_poll, last_evt = 0.0, time.time()
    while True:
        try:
            if time.time() - last_poll >= 2.0:
                last_poll = time.time()
                g = feed(pk); d.g = g
                evs = events_of(g, d.seen, d.done)
                if evs:
                    last_evt = time.time()
                    d.handle(evs)
                    continue
                done_plays = sum(1 for p in g["liveData"]["plays"]["allPlays"] if p["about"].get("isComplete"))
                if g["gameData"]["status"]["abstractGameState"] == "Final" and len(d.done) >= done_plays:
                    d.postgame(g["liveData"]["plays"]["allPlays"])
                    break
            # fill quiet stretches: how long until something happens, judged by what just happened
            idle = time.time() - last_evt
            expect = {"break": 150, "change": 130, "visit": 45, "result": 30}.get(d.hint, 18)
            detail = g["gameData"]["status"].get("detailedState", "")
            if "Delay" in detail or "Suspend" in detail or "Review" in detail or "challenge" in detail.lower():
                expect = max(expect, idle + 60)
            remaining = expect - idle
            ready_in = voice.rtf * 10 + WRITE.lat + 1.5          # start the next line early enough to land on time
            if cast.backlog() < ready_in and remaining - cast.backlog() > 10 and idle > 4:
                d.filler(min(remaining - cast.backlog() - 3, 14))
            time.sleep(0.4)
        except Exception:
            traceback.print_exc(); time.sleep(3)
    cast.status = "Final. Thanks for listening."
    time.sleep(max(0, cast.air_end - time.time()) + 2)
    cast.recorder.close()
    cast.on = False
    log("broadcast over; replay saved in", cast.recorder.root)


def run_render(game, voice, innings=None):
    """Re-call a finished game from MLB data, sized to the real gaps between pitches, and save it as replay files."""
    clock = {"t": 0.0}
    cast = Broadcast(sim=lambda: clock["t"])
    g = feed(game["pk"])
    root = os.path.join(WORK, "replays", f"game-{game['num']}")
    cast.recorder = Recorder(root, game_meta(game, g))
    d = Director(g, game, cast, voice, render=True)
    evs = [e for e in events_of(g, set(), set()) if e["t"]]
    evs.sort(key=lambda e: (e["t"], e["kind"] == "result"))
    if innings:
        evs = [e for e in evs if e["p"]["about"]["inning"] <= innings]
    first = evs[0]["t"]
    clock["t"] = first - 120
    cast.air_end = clock["t"]
    d.pregame(first, short=True)
    i = 0
    while i < len(evs):
        batch = [evs[i]]; i += 1
        # a pitch put in play is called together with its result
        while i < len(evs) and evs[i]["kind"] == "result" and batch[-1]["p"] is evs[i]["p"]:
            batch.append(evs[i]); i += 1
        clock["t"] = max(clock["t"], batch[-1]["t"])
        nxt = evs[i]["t"] if i < len(evs) else batch[-1]["t"] + 30
        d.handle(batch, eta_next=max(3.0, nxt - batch[-1]["t"]))
        clock["t"] = max(clock["t"], cast.air_end)
        # quiet stretches between 20 s and 2 minutes get one filler; longer breaks are trimmed in the replay anyway
        gap = nxt - cast.air_end
        if 20 < gap and d.hint not in ("break",):
            d.filler(min(gap - 5, 14))
            clock["t"] = max(clock["t"], cast.air_end)
    clock["t"] = cast.air_end
    d.postgame([e["p"] for e in evs])
    cast.recorder.close()
    log(f"rendered Game {game['num']} to {root}: {cast.recorder.meta.get('duration', 0) / 60:.1f} minutes")
    return root


# ================================================================ the studio server
def serve():
    from flask import Flask, jsonify, send_file, request
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app = Flask("bob")

    @app.after_request
    def _cors(r):
        r.headers["Access-Control-Allow-Origin"] = "*"
        r.headers.setdefault("Cache-Control", "no-store")
        return r

    @app.route("/")
    def index():
        return "<p>Bob's Booth studio. Listen at <a href='https://willardwonder.github.io/bobs-booth/'>willardwonder.github.io/bobs-booth</a>.</p>"

    @app.route("/live")
    def live():
        return jsonify(CAST.snapshot(int(request.args.get("since", 0) or 0)))

    @app.route("/state")
    def state():
        s = CAST.snapshot(10 ** 9)
        return jsonify({"on": s["on"], "label": s["label"], "status": s["status"], "pk": (s["game"] or {}).get("pk")})

    @app.route("/games")
    def games():
        return jsonify(series_games())

    @app.route("/clip/<name>")
    def clip(name):
        if not re.fullmatch(r"\d+\.(mp3|wav)", name):
            return ("", 404)
        path = os.path.join(CAST.dir, name)
        if not os.path.exists(path):
            return ("", 404)
        r = send_file(path, mimetype="audio/mpeg" if name.endswith("mp3") else "audio/wav")
        r.headers["Cache-Control"] = "public, max-age=3600"
        return r

    s = socket.socket(); s.bind(("", 0)); port = s.getsockname()[1]; s.close()
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port, threaded=True), daemon=True).start()
    threading.Thread(target=tunnel, args=(port,), daemon=True).start()


def tunnel(port):
    """Cloudflare quick tunnel; each new address goes to Bob's ntfy signpost so the permanent page finds it.
    If the tunnel ever drops, it starts a new one."""
    while True:
        try:
            tun = subprocess.Popen([CLOUDFLARED, "tunnel", "--url", f"http://localhost:{port}", "--no-autoupdate"],
                                   stderr=subprocess.PIPE, text=True)
            url = None
            for line in tun.stderr:
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                if m and not url:
                    url = m.group(0)
                    log("BOB'S STUDIO:", url)
                    threading.Thread(target=announce, args=(url,), daemon=True).start()
            tun.wait()
        except Exception as e:
            log("tunnel error:", e)
        log("tunnel closed; starting a new one")
        time.sleep(5)


def announce(url):
    time.sleep(8)            # a new address takes a few seconds to start answering
    for _ in range(6):
        try:
            urllib.request.urlopen(urllib.request.Request("https://ntfy.sh/" + TOPIC, data=url.encode(),
                                   headers={"Title": "Bob's studio is up", "Click": url, "Tags": "radio"}), timeout=15)
            log("posted the address to https://ntfy.sh/" + TOPIC)
            return
        except Exception as e:
            log("signpost error:", e); time.sleep(5)


# ================================================================ main
def pick_game():
    gs = series_games(force=True)
    now = time.time()
    live = next((x for x in gs if x["state"] == "Live"), None)
    if live:
        return live
    soon = [x for x in gs if x["state"] == "Preview" and -3 * 3600 < ts(x["date"]) - now < 3 * 3600]
    return soon[0] if soon else None


def main():
    global WRITE, CAST
    log(f"Bob's Booth studio starting in {MODE} mode ({'Kaggle' if ON_KAGGLE else 'Colab or local'})")
    setup()
    game = None
    if MODE == "live":
        game = pick_game()
        if not game:
            nxt = next((x for x in series_games() if x["state"] == "Preview"), None)
            log("No Brewers game within three hours, so the studio is closing." + (f" Next: Game {nxt['num']} at {nxt['date']}." if nxt else ""))
            return
        log(f"Tonight: Game {game['num']}, {game['away']} at {game['home']}, first pitch {game['date']}")
    WRITE = Writer()
    CAST = Broadcast()
    if MODE == "live":
        serve()                     # the page finds the studio (and says it's warming up) while the voices load
    voice = Voice()
    voice.choose()
    t0 = time.time()
    w = voice.say("Two balls and one strike. Here's the pitch, a slider low and away, and he holds up for ball three. The crowd is on its feet.")
    log(f"voice speed: {time.time() - t0:.1f}s of work for {len(w) / SR:.1f}s of speech (steps {NUM_STEP}, GPUs {len(voice.models)})")
    t0 = time.time()
    try:
        WRITE("Say hello in five words.")
        log(f"writer check: {time.time() - t0:.1f}s")
    except Exception as e:
        log("writer check failed:", e)
    if MODE == "live":
        run_live(game, voice)
        shutil.rmtree(os.path.join(WORK, "live"), ignore_errors=True)       # keep only the replay files
    elif MODE == "test":
        g1 = next(x for x in series_games() if x["num"] == 1)
        run_render(g1, voice, innings=int(os.environ.get("BOB_INNINGS", "2")))
    else:
        gs = series_games()
        todo = [x for x in gs if str(x["pk"]) in RENDER or str(x["num"]) in RENDER] or [x for x in gs if x["state"] == "Final"]
        for gm in todo:
            run_render(gm, voice)
    log(f"done. writer requests {WRITE.count}, failures {WRITE.fail}, writer {WRITE.lat:.1f}s, voice rtf {voice.rtf:.2f}, words/s {voice.wps:.2f}")


if __name__ == "__main__":
    main()
