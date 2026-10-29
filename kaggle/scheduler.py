"""Runs every 15 minutes from GitHub Actions.

1. About an hour before a Brewers World Series game, starts Bob's studio on Kaggle (pushing the notebook
   in kaggle/kernel starts a new run of it), unless it's already running.
2. After a game, downloads the replay files the studio saved and commits them to replays/game-N,
   so they play from GitHub Pages.

Needs a KAGGLE_API_TOKEN repository secret (Kaggle > Settings > API Tokens)."""
import hashlib, json, os, shutil, subprocess, sys, time, urllib.request
from datetime import datetime

KERNEL = "joshstutting/bobs-booth-studio"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START_EARLY = 35 * 60           # studio starts about 35 minutes before first pitch; on the air 15 minutes before
ACTION = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower().strip()
GAMES = ""                       # "render:1,2" renders only those games
# Replays made before this time used older calling rules; these games get re-rendered automatically when nothing
# else is going on (no game within 4 hours, studio idle). Change the time or the list to redo replays again.
RENDER_EPOCH = 1793188517        # Oct 28 2026 morning: Bobisms engine
RERENDER = {1}                   # collect the re-rendered Game 1 when it's ready
AUTO_RENDER = False              # GPU hours are tight: never start a re-render on its own this series
if ":" in ACTION:
    ACTION, GAMES = ACTION.split(":", 1)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def games():
    url = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&teamId=158&gameType=W&season=2026"
    with urllib.request.urlopen(url, timeout=30) as r:
        j = json.load(r)
    out = []
    for d in j.get("dates", []):
        for g in d["games"]:
            out.append({"pk": g["gamePk"], "num": g.get("seriesGameNumber"), "state": g["status"]["abstractGameState"],
                        "first": datetime.fromisoformat(g["gameDate"].replace("Z", "+00:00")).timestamp()})
    return sorted(out, key=lambda x: x["num"] or 0)


def getj(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def digest(pk):
    """Everything true about one finished game that a recap could use, straight from MLB's feed."""
    g = getj(f"https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live")
    gd, ld = g["gameData"], g["liveData"]
    ls, box = ld["linescore"], ld["boxscore"]
    out = {"pk": pk, "date": gd["datetime"].get("officialDate"), "venue": gd.get("venue", {}).get("name"),
           "away": gd["teams"]["away"]["name"], "home": gd["teams"]["home"]["name"],
           "final": {s: {k: ls["teams"][s].get(k) for k in ("runs", "hits", "errors", "leftOnBase")} for s in ("away", "home")},
           "innings": [{"n": i["num"], "away": i.get("away", {}).get("runs"), "home": i.get("home", {}).get("runs")} for i in ls.get("innings", [])],
           "decisions": {k: v.get("fullName") for k, v in ld.get("decisions", {}).items()},
           "scoring": [], "homers": [], "batters": {}, "pitchers": {}, "last_play": ""}
    plays = ld["plays"]["allPlays"]
    for i in ld["plays"].get("scoringPlays", []):
        p = plays[i]
        out["scoring"].append({"inning": f"{p['about']['halfInning']} {p['about']['inning']}", "desc": p["result"].get("description"),
                               "score": f"{p['result'].get('awayScore')}-{p['result'].get('homeScore')}", "rbi": p["result"].get("rbi")})
    for p in plays:
        if p["result"].get("eventType") == "home_run":
            hd = next((e.get("hitData") for e in reversed(p.get("playEvents", [])) if e.get("hitData")), {}) or {}
            out["homers"].append({"inning": f"{p['about']['halfInning']} {p['about']['inning']}", "batter": p["matchup"]["batter"]["fullName"],
                                  "pitcher": p["matchup"]["pitcher"]["fullName"], "rbi": p["result"].get("rbi"),
                                  "ev": hd.get("launchSpeed"), "dist": hd.get("totalDistance")})
    if plays:
        out["last_play"] = plays[-1]["result"].get("description", "")
    for s in ("away", "home"):
        team = box["teams"][s]
        bats, arms = [], []
        for pid in team.get("batters", []):
            pl = team["players"].get(f"ID{pid}", {})
            b = pl.get("stats", {}).get("batting", {})
            if b and (b.get("atBats") or b.get("baseOnBalls")):
                bats.append({"name": pl["person"]["fullName"], **{k: b.get(k, 0) for k in ("atBats", "hits", "doubles", "triples", "homeRuns", "rbi", "runs", "baseOnBalls", "strikeOuts", "stolenBases")}})
        for pid in team.get("pitchers", []):
            pl = team["players"].get(f"ID{pid}", {})
            b = pl.get("stats", {}).get("pitching", {})
            arms.append({"name": pl["person"]["fullName"], **{k: b.get(k) for k in ("inningsPitched", "hits", "runs", "earnedRuns", "baseOnBalls", "strikeOuts", "pitchesThrown", "homeRuns")}})
        out["batters"][s], out["pitchers"][s] = bats, arms
    return out


def make_digests():
    """extras/digests.json: the true facts of every finished game, for the recaps."""
    path = os.path.join(ROOT, "extras", "digests.json")
    if os.path.exists(path):
        return False
    done = [g for g in games() if g["state"] == "Final"]
    if not done:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {str(g["num"]): digest(g["pk"]) for g in done}
    json.dump(data, open(path, "w"), indent=1)
    log("saved game digests for", ", ".join(data))
    return True


def scripts_hash():
    p = os.path.join(ROOT, "extras", "scripts.json")
    return hashlib.sha1(open(p, "rb").read()).hexdigest()[:12] if os.path.exists(p) else ""


def jload(p, default=None):
    try:
        return json.load(open(p))
    except Exception:
        return default


def collect_extras(h):
    """Copy voiced clips from the studio's latest run into extras/, if they were made from today's scripts."""
    tmp = os.path.join(ROOT, ".kaggle-extras")
    shutil.rmtree(tmp, ignore_errors=True)
    code, out = kaggle("kernels", "output", KERNEL, "-p", tmp)
    found = None
    for root, _, files in os.walk(tmp):
        if "extras.json" in files and (jload(os.path.join(root, "extras.json"), {}) or {}).get("scripts") == h:
            found = root
    if not found:
        shutil.rmtree(tmp, ignore_errors=True)
        log("no finished clips for the current scripts in the latest run")
        return False
    dst = os.path.join(ROOT, "extras")
    for f in os.listdir(found):
        if f.endswith(".mp3") or f == "extras.json":
            shutil.copy2(os.path.join(found, f), os.path.join(dst, f))
    shutil.rmtree(tmp, ignore_errors=True)
    log("added the clips:", ", ".join(sorted(x for x in os.listdir(dst) if x.endswith(".mp3"))))
    return True


def extras(st, force=False):
    """Voice extras/scripts.json once: start a run for new scripts, collect the clips when it's done. Returns True if files changed."""
    h = scripts_hash()
    if not h:
        return False
    made = (jload(os.path.join(ROOT, "extras", "extras.json"), {}) or {}).get("scripts")
    if made == h:
        return False
    tried_p = os.path.join(ROOT, "extras", "started.json")
    tried = jload(tried_p, {}) or {}
    if st in ("running", "queued"):
        log("studio is busy; clips wait")
        return False
    if tried.get("scripts") == h and not force:
        if collect_extras(h):
            return "collected"
        if time.time() - tried.get("t", 0) < 3 * 3600 and st not in ("error", "cancel", "complete"):
            return False
        log("the clip run for these scripts ended without clips; run 'extras' by hand to try again")
        return False
    if start("extras"):
        json.dump({"scripts": h, "t": time.time()}, open(tried_p, "w"))
        return "started"
    return False


def kaggle(*args, check=True):
    r = subprocess.run(["kaggle", *args], capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if check and r.returncode != 0:
        log("kaggle", " ".join(args), "failed:", out[-400:])
    return r.returncode, out


def status():
    code, out = kaggle("kernels", "status", KERNEL, check=False)
    low = out.lower()
    for s in ("running", "queued", "complete", "error", "cancel"):
        if s in low:
            return s
    return "unknown"


def replay_made(num):
    try:
        return float(json.load(open(os.path.join(ROOT, "replays", f"game-{num}", "game.json"))).get("made") or 0)
    except Exception:
        return 0.0


def have_replay(num):
    p = os.path.join(ROOT, "replays", f"game-{num}", "game.json")
    if not os.path.exists(p):
        return False
    try:
        return bool(json.load(open(p)).get("complete"))
    except Exception:
        return False


def start(mode="live", games=""):
    """Pushing the notebook starts a new background run of it. mode: live, test or render."""
    log(f"starting Bob's studio on Kaggle ({mode})")
    src = os.path.join(ROOT, "kaggle", "kernel")
    tmp = os.path.join(ROOT, ".kernel-push")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp)
    nb = os.path.join(tmp, "bobs-booth-studio.ipynb")
    book = json.load(open(nb))
    src = book["cells"][0]["source"]
    src = [l.replace('setdefault("BOB_MODE", "live")', f'setdefault("BOB_MODE", "{mode}")') for l in src]
    sha = os.environ.get("GITHUB_SHA")
    if sha:   # fetch exactly this commit's studio code (the main branch link can lag a few minutes after a push)
        src = [l.replace("/bobs-booth/main/kaggle/studio.py", f"/bobs-booth/{sha}/kaggle/studio.py") for l in src]
    # Kaggle doesn't hand notebook secrets to runs started through its API, so the Gemini key (a GitHub secret)
    # rides along inside this private notebook's run instead. It is never printed.
    if games:
        at = next(i for i, l in enumerate(src) if l.startswith("import os")) + 1
        src.insert(at, f"os.environ.setdefault(\"BOB_RENDER\", {json.dumps(games)})\n")
    if mode == "extras":      # the scripts ride inside the run, so it voices exactly what's in the repo now
        at = next(i for i, l in enumerate(src) if l.startswith("import os")) + 1
        src.insert(at, f"os.environ.setdefault(\"BOB_SCRIPTS\", {json.dumps(open(os.path.join(ROOT, 'extras', 'scripts.json')).read())})\n")
        src.insert(at, f"os.environ.setdefault(\"BOB_SCRIPTS_HASH\", {json.dumps(scripts_hash())})\n")
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        at = next(i for i, l in enumerate(src) if l.startswith("import os")) + 1
        src.insert(at, f"os.environ.setdefault(\"GEMINI_API_KEY\", {json.dumps(key)})\n")
        log("Gemini key included")
    else:
        log("no GEMINI_API_KEY GitHub secret; Bob will use the local writer only")
    book["cells"][0]["source"] = src
    json.dump(book, open(nb, "w"), indent=1)
    code, out = kaggle("kernels", "push", "-p", tmp)
    shutil.rmtree(tmp, ignore_errors=True)
    log(out[-400:])
    return code == 0


COLLECT_FAILED = False


def collect():
    """Copy finished replays from the studio's latest run into replays/. Returns the games added."""
    tmp = os.path.join(ROOT, ".kaggle-output")
    shutil.rmtree(tmp, ignore_errors=True)
    code, out = kaggle("kernels", "output", KERNEL, "-p", tmp)
    if code != 0:
        log("could not download the studio's output; trying again next time")
        global COLLECT_FAILED
        COLLECT_FAILED = True
        return []
    added = []
    src_root = os.path.join(tmp, "replays")
    if not os.path.isdir(src_root):
        log("no replays in the latest run's output")
        return []
    for name in sorted(os.listdir(src_root)):
        src = os.path.join(src_root, name)
        meta = os.path.join(src, "game.json")
        if not os.path.exists(meta):
            continue
        m = json.load(open(meta))
        if not m.get("complete"):
            log(name, "is not complete; skipping")
            continue
        dst = os.path.join(ROOT, "replays", name)
        if have_replay(m["num"]):
            try:
                old_made = json.load(open(os.path.join(dst, "game.json"))).get("made", 0)
            except Exception:
                old_made = 0
            if m.get("made", 0) <= old_made:
                continue
            log(name, "has a newer render; replacing the saved replay")
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)
        added.append(m["num"])
        log("added replay", name, f"({m.get('duration', 0) / 60:.0f} min)")
    shutil.rmtree(tmp, ignore_errors=True)
    return added


def report():
    """Print the latest run's status and the important lines of its log."""
    st = status()
    log("studio status:", st)
    tmp = os.path.join(ROOT, ".kaggle-log")
    shutil.rmtree(tmp, ignore_errors=True)
    code, out = kaggle("kernels", "output", KERNEL, "-p", tmp, "--file-pattern", r".*\.log$")
    if code != 0:
        code, out = kaggle("kernels", "output", KERNEL, "-p", tmp)
    for root, _, files in os.walk(tmp):
        for f in files:
            if not f.endswith(".log"):
                continue
            text = open(os.path.join(root, f), errors="ignore").read()
            try:
                lines = [x.get("data", "") for x in json.loads(text)]
            except Exception:
                lines = text.splitlines()
            keep = [l.rstrip() for chunk in lines for l in chunk.splitlines()
                    if any(k in l for k in ("mode (", "writer", "local writer", "voice speed", "tunnel", "GPUs", "done.", "rendered",
                                            "Error", "Traceback", "could not", "WARNING", "Tonight", "No Brewers", "BOB'S STUDIO", "] "))]
            log(f"--- {f}: {len(keep)} key lines ---")
            for l in keep[:160]:
                print(l)
    shutil.rmtree(tmp, ignore_errors=True)


def main():
    if ACTION == "status":
        return report()
    gs = games()
    now = time.time()
    st = status()
    log("studio status:", st)
    running = st in ("running", "queued")
    changed = False
    if ACTION in ("auto", "extras"):
        try:
            changed = make_digests()
        except Exception as e:
            log("digest error:", e)
        soon = any(g["state"] != "Final" and g["first"] - now < 4 * 3600 for g in gs)
        if not soon:
            did = extras(st, force=ACTION == "extras")
            changed = changed or bool(did)
            running = running or did == "started"
        if ACTION == "extras":
            with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as f:
                f.write(f"added={'extras' if changed else ''}\n")
            return
    if ACTION in ("auto", "start"):
        target = next((g for g in gs if g["state"] != "Final" and g["first"] - START_EARLY <= now <= g["first"] + 5 * 3600), None)
        if target and not running and not have_replay(target["num"]):
            log(f"Game {target['num']} first pitch in {(target['first'] - now) / 60:.0f} minutes")
            if start():
                running = True
        elif ACTION == "start" and not running:
            start(); running = True
        elif target:
            log(f"Game {target['num']} is close and the studio is {st}")
        else:
            log("no game coming up soon")
    if ACTION in ("test", "render") and not running:
        start(ACTION, GAMES)
        return
    added = []
    if ACTION in ("auto", "collect") and not running:
        missing = [g for g in gs if g["state"] == "Final" and not have_replay(g["num"])]
        stale = [g["num"] for g in gs if g["state"] == "Final" and g["num"] in RERENDER and replay_made(g["num"]) < RENDER_EPOCH]
        if missing or stale or ACTION == "collect":
            added = collect()                 # also picks up a finished re-render (newer replays replace older ones)
        stale = [n for n in stale if replay_made(n) < RENDER_EPOCH]
        soon = any(g["state"] != "Final" and g["first"] - now < 4 * 3600 for g in gs)
        if ACTION == "auto" and AUTO_RENDER and stale and not soon and not (missing and not added) and not COLLECT_FAILED:
            log("re-rendering with the current rules:", stale)
            start("render", ",".join(map(str, stale)))
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as f:
        f.write(f"added={' '.join(map(str, added)) or ('extras' if changed else '')}\n")


if __name__ == "__main__":
    main()
