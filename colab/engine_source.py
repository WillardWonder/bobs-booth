# 3) Bob's Booth: pick a Brewers World Series game -> MLB pitch-by-pitch data -> Gemini writes Bob's call -> Bob's voice -> public player
import json, re, threading, time, subprocess, urllib.request, os, random, numpy as np, soundfile as sf
from flask import Flask, jsonify, send_file, request

def bob_line(*a, **k): raise SystemExit   # stops an older Bob worker thread if one is still running in this session
try: tun.terminate()                        # close the older tunnel too
except NameError: pass
import socket; _s = socket.socket(); _s.bind(("", 0)); PORT = _s.getsockname()[1]; _s.close()   # fresh free port each run
RUN = time.time()   # newer runs retire older Bob workers
SEASON, BREWERS, SR = 2026, 158, 24000
POLL_S = 8      # live games: check MLB for new plays this often
LEAD_S = 45     # replays: keep about this many seconds of Bob ready ahead of the listener, so there is no dead air
TOPIC = "bobs-booth-mke-4q7v2k"
os.makedirs("/content/clips", exist_ok=True)
lock = threading.Lock()
S = {"gen": 0, "pk": None, "mode": None, "label": "Off the air", "clips": [], "n": 0, "audio": 0.0, "t0": None, "last_talk": 0}

try:
    from google.colab import ai
    def gemini(p): return ai.generate_text(p).strip()
except Exception:
    def gemini(p): raise RuntimeError("Gemini unavailable")

def getj(url):
    with urllib.request.urlopen(url, timeout=20) as r: return json.load(r)
def feed(pk): return getj(f"https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live")

_games = {"t": 0, "data": []}
def series_games():
    if time.time() - _games["t"] < 30: return _games["data"]
    out = []
    try:
        j = getj(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&teamId={BREWERS}&gameType=W&season={SEASON}&hydrate=linescore")
        for d in j.get("dates", []):
            for g in d["games"]:
                a, h = g["teams"]["away"], g["teams"]["home"]
                ls = g.get("linescore", {})
                out.append({"pk": g["gamePk"], "num": g.get("seriesGameNumber") or len(out) + 1,
                    "date": g["gameDate"], "state": g["status"]["abstractGameState"], "detail": g["status"]["detailedState"],
                    "away": a["team"]["name"], "home": h["team"]["name"],
                    "awayRuns": a.get("score"), "homeRuns": h.get("score"),
                    "inning": (ls.get("inningHalf", "") + " " + str(ls.get("currentInningOrdinal", ""))).strip()})
        _games.update(t=time.time(), data=out)
    except Exception as e:
        print("schedule error:", e)
    return _games["data"]
def series_num(g): return next((x["num"] for x in series_games() if x["pk"] == g.get("gamePk")), "")

# ---------- what Bob knows ----------
FACTS = [   # checked Brewers history Bob can drop between innings, each used at most once per game
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
- Routine plays get a short, understated call, sometimes with a wry little aside.
- Big moments build: the windup, the swing, the ball in flight, stretch one word as it carries, then let it land, then give the score.
- Paint the picture for someone who cannot see it: where the ball went, who ran, who made the play.
- Self-deprecating humor about Bob's own made-up playing days as a forgettable backup catcher in the low minors: splinters from the bench, a batting average nobody should repeat, managers who forgot his name.
- Folksy, old-fashioned phrasing. Talks to the listener directly now and then.
- Never repeats himself: vary sentence openings, verbs, and jokes.
Rules: use ONLY the game facts you are given; never invent stats, names, injuries, weather, crowd size or events about real people.
The only made-up material allowed is Bob's own harmless tall tales about himself. Do not use any real broadcaster's catchphrases.
Keep it clean and family-friendly, like a broadcast grandparents and kids enjoy together. Never use words or phrasing that could read as a double meaning;
prefer plain baseball verbs such as throws, deals, fires, delivers, swings, lines, lifts, grounds, rolls.
Spell out all numbers as words. Plain spoken text only: no emojis, stage directions, quotation marks, lists or markdown."""

RISKY = re.compile(r"\b(blow|blows|blew|blown|pump|pumps|pumped|pumping|stroke|stroked|strokes|wood|sticks it|slides it in|hard one|climax|thrust|erect|shaft|nuts)\b", re.I)

STORY = ("It is a quiet stretch between plays. Kill a little time the way a veteran radio man does: one short tall tale about your own "
         "forgettable playing days, or a gentle remark about life in the booth, or a small observation about the game situation in the facts. "
         "Then come right back to the game by naming the next batter. Two to four sentences, at most sixty words.")

def clean(t): return re.sub(r"\s+", " ", re.sub(r"[*_#\"]", "", t)).strip()
def ordinal(n): return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"
def bases_text(b):
    names = [n for n, on in zip(("first", "second", "third"), b) if on]
    return "bases empty" if not names else ("bases loaded" if len(names) == 3 else "runners on " + " and ".join(names))

def ask(task, facts, mem, fallback):
    recent = ("Your last lines on the air. Do NOT reuse their openings, phrases or jokes:\n" + "\n".join("- " + x for x in mem[-6:]) + "\n\n") if mem else ""
    p = f"{BOB}\n\n{recent}Style for this one: {random.choice(STYLES)}\n\nTask: {task}\n\nFacts:\n{facts}"
    try:
        t = clean(gemini(p))
        if t and RISKY.search(t):   # one rewrite if a line could read the wrong way
            t = clean(gemini(p + "\n\nRewrite this line with plainer, family-friendly wording and no possible double meaning: " + t))
        if t and not RISKY.search(t): return t
    except Exception as e: print("Gemini error:", e)
    return fallback

def bio(g, pid):
    p = g["gameData"]["players"].get(f"ID{pid}", {})
    bits = [p.get("fullName", "")]
    if p.get("currentAge"): bits.append(f"age {p['currentAge']}")
    place = ", ".join(x for x in (p.get("birthCity"), p.get("birthStateProvince") or p.get("birthCountry")) if x)
    if place: bits.append("born in " + place)
    if p.get("primaryPosition", {}).get("name"): bits.append(p["primaryPosition"]["name"].lower())
    if p.get("height"): bits.append(p["height"].replace("' ", " foot ").replace('"', ""))
    if p.get("mlbDebutDate"): bits.append("big-league debut in " + p["mlbDebutDate"][:4])
    if p.get("nickName"): bits.append("nickname " + p["nickName"])
    return ", ".join(b for b in bits if b)

def pitch_story(p):
    out = []
    for e in p.get("playEvents", []):
        d = e.get("details", {})
        if e.get("isPitch"):
            spd = e.get("pitchData", {}).get("startSpeed"); c = e.get("count", {})
            out.append(f"{d.get('type', {}).get('description', 'pitch')}{f' {round(spd)} mph' if spd else ''}, {d.get('description', '').lower()} (count now {c.get('balls')}-{c.get('strikes')})")
        elif e.get("type") == "action" and d.get("event") not in (None, "Game Advisory"):
            out.append("Between pitches: " + d.get("description", ""))
    hd = next((e["hitData"] for e in p.get("playEvents", []) if e.get("hitData")), None)
    if hd:
        bits = [f"off the bat at {round(hd['launchSpeed'])} mph" if hd.get("launchSpeed") else "",
                f"traveled {round(hd['totalDistance'])} feet" if hd.get("totalDistance") else "",
                hd.get("trajectory", "").replace("_", " ")]
        out.append("Contact: " + ", ".join(b for b in bits if b))
    return out

class Box:
    """Running scoreboard built play by play, so replays never reveal what has not happened yet."""
    def __init__(s, g):
        t = g["gameData"]["teams"]
        s.ab = {k: t[k]["abbreviation"] for k in ("away", "home")}
        s.team = {k: t[k].get("teamName", t[k]["name"]) for k in ("away", "home")}
        s.line = {"away": [], "home": []}; s.hits = {"away": 0, "home": 0}; s.score = {"away": 0, "home": 0}
        s.cur = None; s.start = dict(s.score); s.half_hits = 0; s.outs = 0; s.bases = [0, 0, 0]
        s.bat = {}; s.pc = {}; s.inning = 1; s.half = "top"; s.batter = ""; s.pitcher = ""
    @staticmethod
    def side(half): return "away" if half == "top" else "home"
    def apply(s, p):
        a, r, m = p["about"], p["result"], p.get("matchup", {})
        half, inn = a["halfInning"], a["inning"]; side = s.side(half)
        if (inn, half) != s.cur:
            s.cur = (inn, half); s.outs = 0; s.bases = [0, 0, 0]; s.start = dict(s.score); s.half_hits = 0
            while len(s.line[side]) < inn: s.line[side].append(None)
            s.line[side][inn - 1] = 0
        bid, pid = m.get("batter", {}).get("id"), m.get("pitcher", {}).get("id")
        before = {"outs": s.outs, "bases": list(s.bases), "score": dict(s.score),
                  "bat": s.bat.get(bid, (0, 0, 0)), "pc": s.pc.get(pid, 0)}
        s.score = {"away": r.get("awayScore", s.score["away"]), "home": r.get("homeScore", s.score["home"])}
        s.line[side][inn - 1] = s.score[side] - s.start[side]
        hit = r.get("eventType") in HITS
        if hit: s.hits[side] += 1; s.half_hits += 1
        h, ab, pa = before["bat"]; s.bat[bid] = (h + hit, ab + (r.get("eventType") not in NOT_AB), pa + 1)
        s.pc[pid] = before["pc"] + sum(1 for e in p.get("playEvents", []) if e.get("isPitch"))
        s.outs = p.get("count", {}).get("outs", s.outs)
        s.bases = [0, 0, 0] if s.outs >= 3 else [1 if m.get(k) else 0 for k in ("postOnFirst", "postOnSecond", "postOnThird")]
        s.inning, s.half = inn, half
        s.batter, s.pitcher = m.get("batter", {}).get("fullName", ""), m.get("pitcher", {}).get("fullName", "")
        return before
    def sc(s, score=None):
        score = score or s.score
        return f"{s.team['away']} {score['away']}, {s.team['home']} {score['home']}"
    def board(s):
        return {"ab": s.ab, "team": s.team, "line": s.line, "hits": s.hits, "score": s.score, "outs": s.outs, "bases": s.bases,
                "inning": s.inning, "half": s.half, "batter": s.batter, "pitcher": s.pitcher}

def play_facts(p, g, box, before, seen):
    a, r, m = p["about"], p["result"], p.get("matchup", {})
    bid, pid = m.get("batter", {}).get("id"), m.get("pitcher", {}).get("id")
    h, ab, pa = before["bat"]
    today = "first trip to the plate today" if pa == 0 else f"{h} for {ab} so far today"
    f = [f"{a['halfInning'].title()} of the {ordinal(a['inning'])}. Before the play: {before['outs']} out, {bases_text(before['bases'])}, score {box.sc(before['score'])}.",
         f"Batter: {m.get('batter', {}).get('fullName')} of the {box.team[box.side(a['halfInning'])]}, bats {m.get('batSide', {}).get('description', '').lower()}, {today}.",
         f"Pitcher: {m.get('pitcher', {}).get('fullName')}, throws {m.get('pitchHand', {}).get('description', '').lower()}, {before['pc']} pitches so far tonight.",
         "Pitch by pitch: " + "; ".join(pitch_story(p)),
         f"Result: {r.get('description', '')}",
         f"After the play: {box.outs} out, {bases_text(box.bases)}, score {box.sc()}."]
    if r.get("rbi"): f.append(f"Runs batted in on the play: {r['rbi']}.")
    for who in (bid, pid):   # a little background the first time Bob sees someone, now and then
        if who and who not in seen and random.random() < 0.6:
            seen.add(who); f.append("Background you may use one detail from: " + bio(g, who))
    return "\n".join(f)

def big_moment(p, before, box):
    et = p["result"].get("eventType", "")
    if et in ("home_run", "triple", "triple_play") or p["about"].get("isScoringPlay"): return True
    if et in ("grounded_into_double_play", "double_play", "strikeout_double_play"): return True
    risp = before["bases"][1] or before["bases"][2]
    if risp and et in ("single", "double"): return True
    if risp and before["outs"] == 2 and box.outs >= 3: return True     # a jam escaped
    return False
BIG = (" This is a big moment. Write it in two parts separated by a single | character. Before the |: calm and conversational, "
       "the setup and the key pitches. After the |: excited, the result and the immediate reaction, one or two short sentences.")

CATCHUP = ("Several plays happened while you were talking. Catch listeners up quickly and smoothly in two or three sentences, "
           "most important play first, then the score and the situation now. At most fifty-five words.")
LIVE_CALL = ("Call this play like live radio, a little tighter than usual because the game is moving: the key pitch or two, the result, "
             "and a quick reaction. Two or three sentences, at most fifty words.")
CALL = ("Call this plate appearance like live radio. Walk listeners through two or three of the key pitches (not every one), "
        "then the result with energy that fits how big it is, then one short reaction. Three or four sentences, at most seventy words. "
        "A routine out gets a brisk call; a hit, run or strikeout gets more.")

# ---------- voice ----------
def tidy(w):
    w = w.cpu().numpy() if hasattr(w, "cpu") else np.asarray(w)
    w = w.astype(np.float32).squeeze()
    if w.size and np.abs(w).max() > 0:
        thr = 0.03 * float(np.abs(w).max()); idx = np.where(np.abs(w) > thr)[0]
        w = w[max(0, idx[0] - int(.05 * SR)): idx[-1] + int(.12 * SR)]
    return w
def words(t): return re.sub(r"[^a-z' ]", " ", t.lower()).split()
def leak_guard(w, text):
    """If the clone starts with a stray word that isn't in the line (like "here"), cut it off."""
    try:
        out = model._asr_pipe({"array": w, "sampling_rate": SR}, return_timestamps="word")
        chunks = out.get("chunks") or []
        heard = [(words(c["text"]) or [""])[0] for c in chunks[:4]]
        want = words(text)[:2]
        if not heard or not want or heard[0] == want[0]: return w
        for k in range(1, len(heard)):
            if heard[k] == want[0] and (len(want) < 2 or k + 1 >= len(heard) or heard[k + 1] == want[1]):
                start = chunks[k]["timestamp"][0]
                if start:
                    print("  trimmed stray audio:", " ".join(heard[:k]))
                    return w[max(0, int((start - .06) * SR)):]
    except Exception as e:
        print("leak guard skipped:", e)
    return w
def say(text, hot=False):
    prompt = bob_hot if hot and "bob_hot" in globals() else bob
    return leak_guard(tidy(model.generate(text=text, voice_clone_prompt=prompt, language="English")[0]), text)
def voice(text, hot=False):
    """Calm voice for everything; on a big moment the setup stays calm and the payoff (after the |) is excited."""
    parts = [t.strip() for t in text.split("|") if t.strip()]
    if hot and len(parts) == 1:
        s = re.split(r"(?<=[.!?])\s+", parts[0]); parts = [" ".join(s[:-1]), s[-1]] if len(s) > 1 else parts
    gap, out = np.zeros(int(.15 * SR), np.float32), []
    for i, t in enumerate(parts):
        out += [say(t, hot and i == len(parts) - 1), gap]
    return np.concatenate(out + [np.zeros(int(.1 * SR), np.float32)])

def speak(text, gen, box, kind, mem, hot=False):
    if not text: return False
    wav = voice(text, hot)
    text = re.sub(r"\s*\|\s*", " ", text).strip()
    with lock:
        if gen != S["gen"]: return False          # listener switched games while Bob was talking
        S["n"] += 1; n = S["n"]
        sf.write(f"/content/clips/{n}.wav", wav, SR)
        S["clips"].append({"id": n, "text": text, "kind": kind, "board": box.board() if box else None})
        S["clips"] = S["clips"][-300:]
        if S["t0"] is None: S["t0"] = time.time()
        S["audio"] += len(wav) / SR
    mem.append(text); del mem[:-8]
    S["last_talk"] = time.time()
    print(f"[{n}] {text}"); return True

def pace(gen):   # replays: generate ahead, but only about LEAD_S seconds ahead of real time
    while S["gen"] == gen and S["t0"] and S["audio"] - (time.time() - S["t0"]) > LEAD_S: time.sleep(1)

# ---------- the broadcast ----------
def series_so_far(g):
    num = series_num(g)
    done = [x for x in series_games() if x["state"] == "Final" and isinstance(num, int) and x["num"] < num]
    if not done: return "This is the first game of the series."
    mil = sum(1 for x in done if (x["homeRuns"] > x["awayRuns"]) == ("Brewers" in x["home"]))
    return f"Series so far before this game: Brewers {mil} wins, Rays {len(done) - mil} wins. " + " ".join(
        f"Game {x['num']}: {x['away']} {x['awayRuns']}, {x['home']} {x['homeRuns']}." for x in done)

def intro(g, mode, box, mem):
    gd = g["gameData"]; t = gd["teams"]; pp = gd.get("probablePitchers", {})
    f = [f"World Series Game {series_num(g)}: {t['away']['name']} at {t['home']['name']}, {gd.get('venue', {}).get('name', '')}.",
         series_so_far(g),
         "Starting pitchers: " + ", ".join(f"{pp[k]['fullName']} for the {box.team[k]}" for k in ("away", "home") if pp.get(k))]
    if mode == "live": f.append(f"Right now: {box.half} of the {ordinal(box.inning)}, {box.outs} out, {bases_text(box.bases)}, score {box.sc()}, {box.hits['away']} hits for the {box.team['away']} and {box.hits['home']} for the {box.team['home']}.")
    task = {"replay": "Open the broadcast. You are re-calling this game from the first pitch for folks listening later, so do not hint at how it ends. Set the scene, the series, and the starting pitchers.",
            "live": "Open the broadcast. You are joining this game live; welcome listeners and catch them up on the game so far.",
            "wait": "The game has not started; welcome listeners, set up the matchup and the starting pitchers, and say you will be back at first pitch."}[mode]
    return ask(task + " Four or five sentences, at most ninety words.", "\n".join(f), mem,
               f"Hello everybody, Bob here with World Series Game {series_num(g)}.")

def inning_break(box, facts_left, mem):
    side = Box.side(box.half); team = box.team[side]
    runs = box.line[side][box.inning - 1] or 0
    f = [f"That ends the {box.half} of the {ordinal(box.inning)}. The {team} scored {runs} and had {box.half_hits} hits that half.",
         f"Score: {box.sc()}. Hits so far: {box.team['away']} {box.hits['away']}, {box.team['home']} {box.hits['home']}."]
    if facts_left: f.append("Something to share with listeners: " + facts_left.pop(random.randrange(len(facts_left))))
    return ask("Wrap up the half inning in three sentences: a quick recap, then share the item to share in your own warm words, then lead into the next half.",
               "\n".join(f), mem, f"That's the {box.half} of the {ordinal(box.inning)}. {box.sc()}.")

def worker():
    run, seen_gen = RUN, -1
    while True:
        if run != RUN: return
        with lock: gen, pk, mode = S["gen"], S["pk"], S["mode"]
        if pk is None or gen == seen_gen: time.sleep(1); continue
        seen_gen = gen
        try:
            g = feed(pk); state = g["gameData"]["status"]["abstractGameState"]
            plays = [p for p in g["liveData"]["plays"]["allPlays"] if p["about"].get("isComplete")]
            if state == "Preview": mode = "wait"
            box, mem, seen, facts_left = Box(g), [], set(), list(FACTS)
            done, quiet = 0, 0
            if mode != "replay":
                for p in plays: box.apply(p)
                done = len(plays)
            speak(intro(g, mode, box, mem), gen, box, "intro", mem)
            while S["gen"] == gen:
                while done < len(plays) and S["gen"] == gen:
                    if mode != "replay" and len(plays) - done > 2:   # live and falling behind: recap the backlog in one go
                        batch = plays[done:-1]
                        for p in batch: box.apply(p)
                        done += len(batch)
                        facts = "\n".join(f"{p['about']['halfInning'].title()} of the {ordinal(p['about']['inning'])}: {p['result'].get('description', '')}" for p in batch)
                        speak(ask(CATCHUP, facts + f"\nNow: {box.half} of the {ordinal(box.inning)}, {box.outs} out, {bases_text(box.bases)}, score {box.sc()}.", mem,
                                  f"Let me catch you up. It's {box.sc()}."), gen, box, "catchup", mem)
                        continue
                    p = plays[done]; before = box.apply(p)
                    hot = big_moment(p, before, box) or (state == "Final" and done == len(plays) - 1)
                    task = (CALL if mode == "replay" else LIVE_CALL) + (BIG if hot else "")
                    speak(ask(task, play_facts(p, g, box, before, seen), mem, p["result"].get("description", "")), gen, box, "play", mem, hot)
                    done += 1; quiet += 1
                    if box.outs >= 3 and S["gen"] == gen:
                        speak(inning_break(box, facts_left, mem), gen, box, "break", mem); quiet = 0
                    elif mode == "replay" and quiet >= 6 and not p["about"].get("isScoringPlay") and S["gen"] == gen and done < len(plays):
                        nxt = plays[done].get("matchup", {}).get("batter", {}).get("fullName", "")
                        speak(ask(STORY, f"Situation: {box.half} of the {ordinal(box.inning)}, {box.outs} out, {bases_text(box.bases)}, score {box.sc()}. Next batter: {nxt}.", mem, ""), gen, box, "story", mem)
                        quiet = random.randint(-2, 1)
                    if mode == "replay": pace(gen)
                if mode != "replay" and state == "Live" and S["gen"] == gen and time.time() - S["last_talk"] > 50:
                    # live and nothing new for a while: fill the quiet like a radio man would
                    speak(ask(STORY, f"Situation: {box.half} of the {ordinal(box.inning)}, {box.outs} out, {bases_text(box.bases)}, score {box.sc()}. "
                              f"Up now: {box.batter or 'the next batter'} against {box.pitcher or 'the pitcher'}. Do not invent any new play.", mem, ""), gen, box, "story", mem)
                if S["gen"] != gen: break
                if done >= len(plays) and state == "Final":
                    end = ask("The game is over. Give the final score, one line on who made the difference based on the facts, and sign off warmly as Bob. Three sentences.",
                              f"Final: {box.sc()}. Hits: {box.team['away']} {box.hits['away']}, {box.team['home']} {box.hits['home']}. Last play: {plays[-1]['result'].get('description', '')}",
                              mem, f"And that's the ball game. Final score, {box.sc()}. This has been Bob.")
                    speak(end, gen, box, "final", mem)
                    with lock:
                        if S["gen"] == gen: S["label"] += " (finished)"
                    break
                time.sleep(POLL_S)
                g = feed(pk); state = g["gameData"]["status"]["abstractGameState"]
                plays = [p for p in g["liveData"]["plays"]["allPlays"] if p["about"].get("isComplete")]
        except Exception as e:
            import traceback; traceback.print_exc(); seen_gen = -1; time.sleep(POLL_S)

PAGE = r"""__PAGE__"""

app = Flask(__name__)
@app.after_request
def _cors(r): r.headers["Access-Control-Allow-Origin"] = "*"; return r
@app.route("/")
def index(): return PAGE
@app.route("/games")
def games(): return jsonify(series_games())
@app.route("/state")
def state():
    with lock: return jsonify({k: S[k] for k in ("gen", "pk", "mode", "label")})
@app.route("/since/<int:gen>/<int:n>")
def since(gen, n):
    with lock: return jsonify([c for c in S["clips"] if c["id"] > n] if gen == S["gen"] else [])
@app.route("/clip/<int:n>")
def clip(n): return send_file(f"/content/clips/{n}.wav", mimetype="audio/wav", max_age=3600)
def tune_to(pk, mode):
    g = next((x for x in series_games() if x["pk"] == pk), None)
    with lock:
        S["gen"] += 1; S["clips"] = []; S["audio"] = 0.0; S["t0"] = None
        if g:
            S.update(pk=pk, mode=mode, label=f"Game {g['num']}: {g['away']} at {g['home']} - " + ("live" if mode == "live" else "replay"))
        else:
            S.update(pk=None, mode=None, label="Off the air")
    return S["gen"]

@app.route("/play", methods=["POST"])
def play():
    d = request.get_json(silent=True) or {}
    return jsonify(ok=True, gen=tune_to(d.get("pk"), d.get("mode", "replay")))

def autolive():
    """At game time Bob tunes himself in: a live game (or one starting within 30 minutes) takes over the booth,
    unless the booth is already on that game (live, or a listener chose to hear it from the first pitch)."""
    from datetime import datetime
    run = RUN
    while run == RUN:
        try:
            gs = series_games(); now = time.time()
            ts = lambda d: datetime.fromisoformat(d.replace("Z", "+00:00")).timestamp()
            target = next((x for x in gs if x["state"] == "Live"), None) or \
                     next((x for x in gs if x["state"] == "Preview" and 0 < ts(x["date"]) - now < 1800), None)
            if target and S["pk"] != target["pk"]:
                print(f"Game time: tuning Bob to Game {target['num']} live")
                tune_to(target["pk"], "live")
        except Exception as e:
            print("autolive:", e)
        time.sleep(60)

threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
threading.Thread(target=worker, daemon=True).start()
threading.Thread(target=autolive, daemon=True).start()
tun = subprocess.Popen(["cloudflared", "tunnel", "--url", f"http://localhost:{PORT}", "--no-autoupdate"], stderr=subprocess.PIPE, text=True)
for line in tun.stderr:
    m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
    if m:
        print("\nBOB'S BOOTH:", m.group(0)); print("OBS browser source (autoplay):", m.group(0) + "/?auto")
        try:   # post the new address to Bob's permanent signpost
            urllib.request.urlopen(urllib.request.Request("https://ntfy.sh/" + TOPIC, data=m.group(0).encode(),
                headers={"Title": "Bob is on the air", "Click": m.group(0), "Tags": "radio"}), timeout=15)
            print("Permanent link: https://ntfy.sh/" + TOPIC)
        except Exception as e: print("signpost error:", e)
        break
threading.Thread(target=lambda: [None for _ in tun.stderr], daemon=True).start()
