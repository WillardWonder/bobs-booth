"""Runs every 15 minutes from GitHub Actions.

1. About an hour before a Brewers World Series game, starts Bob's studio on Kaggle (pushing the notebook
   in kaggle/kernel starts a new run of it), unless it's already running.
2. After a game, downloads the replay files the studio saved and commits them to replays/game-N,
   so they play from GitHub Pages.

Needs a KAGGLE_API_TOKEN repository secret (Kaggle > Settings > API Tokens)."""
import json, os, shutil, subprocess, sys, time, urllib.request
from datetime import datetime

KERNEL = "joshstutting/bobs-booth-studio"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START_EARLY = 70 * 60           # start the studio this long before first pitch (on the air at 30 minutes)
ACTION = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()


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


def have_replay(num):
    p = os.path.join(ROOT, "replays", f"game-{num}", "game.json")
    if not os.path.exists(p):
        return False
    try:
        return bool(json.load(open(p)).get("complete"))
    except Exception:
        return False


def start(mode="live"):
    """Pushing the notebook starts a new background run of it. mode: live, test or render."""
    log(f"starting Bob's studio on Kaggle ({mode})")
    src = os.path.join(ROOT, "kaggle", "kernel")
    tmp = os.path.join(ROOT, ".kernel-push")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp)
    nb = os.path.join(tmp, "bobs-booth-studio.ipynb")
    text = open(nb).read().replace('setdefault(\\"BOB_MODE\\", \\"live\\")', f'setdefault(\\"BOB_MODE\\", \\"{mode}\\")')
    open(nb, "w").write(text)
    code, out = kaggle("kernels", "push", "-p", tmp)
    shutil.rmtree(tmp, ignore_errors=True)
    log(out[-400:])
    return code == 0


def collect():
    """Copy finished replays from the studio's latest run into replays/. Returns the games added."""
    tmp = os.path.join(ROOT, ".kaggle-output")
    shutil.rmtree(tmp, ignore_errors=True)
    code, out = kaggle("kernels", "output", KERNEL, "-p", tmp)
    if code != 0:
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
        if have_replay(m["num"]):
            continue
        dst = os.path.join(ROOT, "replays", name)
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
            log("no game within the next 70 minutes")
    if ACTION in ("test", "render") and not running:
        start(ACTION)
        return
    if ACTION in ("auto", "collect") and not running:
        missing = [g for g in gs if g["state"] == "Final" and not have_replay(g["num"])]
        if missing or ACTION == "collect":
            added = collect()
            with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as f:
                f.write(f"added={' '.join(map(str, added))}\n")


if __name__ == "__main__":
    main()
