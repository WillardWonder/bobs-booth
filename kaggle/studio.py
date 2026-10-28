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
    "Back home in Milwaukee, the Brewers' ballpark, American Family Field, opened in 2001 as Miller Park, with a fan-shaped retractable roof.",
    "Prince Fielder hit fifty home runs for Milwaukee in 2007.",
    "Christian Yelich won the National League MVP award in 2018.",
    "The Racing Sausages run at Brewers home games, a Milwaukee tradition since the 1990s.",
    "Before the Brewers, Milwaukee had the Braves, who won the 1957 World Series.",
    "The Tampa Bay Rays started play in 1998 as the Devil Rays and reached the World Series in 2008 and 2020.",
    "This is the Brewers' first World Series since 1982, and they are chasing the first championship in team history.",
]
QUICK_FACTS = [
    "The Brewers played their first game in Milwaukee in April of nineteen seventy, after Bud Selig's group bought the Seattle Pilots.",
    "The Brewers called County Stadium home from nineteen seventy through two thousand.",
    "Robin Yount reached the big leagues at eighteen and played all twenty of his seasons in Milwaukee.",
    "Robin Yount got his three thousandth hit in nineteen ninety-two.",
    "Robin Yount went into the Hall of Fame in nineteen ninety-nine, and Paul Molitor followed in two thousand four.",
    "Robin Yount is the Brewers' all-time leader in games played and hits.",
    "Paul Molitor's thirty-nine game hitting streak in nineteen eighty-seven ended with him waiting on deck as Rick Manning won the game with a hit.",
    "The nineteen eighty-seven Brewers started the season with thirteen straight wins.",
    "Rollie Fingers won both the Cy Young Award and the MVP for the Brewers in nineteen eighty-one.",
    "Pete Vuckovich won the American League Cy Young Award in nineteen eighty-two.",
    "Corbin Burnes won the Cy Young Award in twenty twenty-one, the Brewers' first since Pete Vuckovich.",
    "Harvey Kuenn took over as manager in June of nineteen eighty-two and led the Brewers to the pennant.",
    "In the nineteen eighty-two playoffs the Brewers came back from two games down to beat the Angels for the pennant.",
    "Robin Yount hit two home runs on the last day of the nineteen eighty-two season to clinch the division in Baltimore.",
    "Gorman Thomas led the American League in home runs twice, in nineteen seventy-nine and nineteen eighty-two.",
    "Ben Oglivie shared the American League home run title in nineteen eighty.",
    "Cecil Cooper drove in more than a hundred runs three times for Milwaukee.",
    "Ted Simmons, the catcher on the nineteen eighty-two team, was voted into the Hall of Fame with the class of twenty twenty.",
    "The Brewers made their first postseason in nineteen eighty-one.",
    "Hank Aaron hit the last of his seven hundred fifty-five home runs as a Brewer in nineteen seventy-six.",
    "The Brewers retired number forty-four for Hank Aaron.",
    "Teddy Higuera won twenty games for the Brewers in nineteen eighty-six.",
    "Juan Nieves threw the first no-hitter in Brewers history in nineteen eighty-seven.",
    "The Brewers moved from the American League to the National League in nineteen ninety-eight.",
    "Miller Park opened in two thousand one and became American Family Field in twenty twenty-one.",
    "Milwaukee hosted the All-Star Game in nineteen seventy-five and again in two thousand two.",
    "The two thousand two All-Star Game in Milwaukee ended in a tie after eleven innings.",
    "Prince Fielder hit fifty home runs in two thousand seven, the youngest player ever to reach fifty.",
    "Ryan Braun was the National League Rookie of the Year in two thousand seven.",
    "Ryan Braun won the National League MVP in twenty eleven.",
    "Ryan Braun is the Brewers' all-time home run leader.",
    "CC Sabathia came to Milwaukee in a midseason trade in two thousand eight and pitched the Brewers into their first postseason in twenty-six years.",
    "The Brewers drew more than three million fans for the first time in two thousand eight.",
    "Trevor Hoffman recorded his six hundredth save as a Brewer in twenty ten.",
    "In twenty eleven the Brewers won the division and reached the National League Championship Series.",
    "In twenty eighteen the Brewers came within one game of the World Series, losing Game Seven of the playoffs to the Dodgers.",
    "Christian Yelich won the National League batting title in twenty eighteen and again in twenty nineteen.",
    "Christian Yelich hit for the cycle twice against the Reds in twenty eighteen.",
    "Yovani Gallardo holds the Brewers' career record for strikeouts.",
    "Craig Counsell, who grew up in the Milwaukee area, became the winningest manager in Brewers history.",
    "Pat Murphy was named National League Manager of the Year in twenty twenty-four.",
    "Jackson Chourio hit more than twenty home runs and stole more than twenty bases as a twenty year old rookie in twenty twenty-four.",
    "The Brewers' ball in glove logo, drawn by an art student in the late nineteen seventies, hides the letters M and B.",
    "The ball in glove logo came back as the Brewers' main logo in twenty twenty.",
    "At County Stadium, Bernie Brewer slid into a giant mug after every Brewers home run.",
    "The Racing Sausages are the Brat, the Polish, the Italian, the Hot Dog and the Chorizo.",
    "Bob Uecker started calling Brewers games on the radio in nineteen seventy-one.",
    "Bob Uecker received the Ford C. Frick Award from the Hall of Fame in two thousand three.",
    "A statue of Bob Uecker stands outside the ballpark, and another sits in the last row of the upper deck.",
    "Milwaukee's Braves won the World Series in nineteen fifty-seven at County Stadium.",
]
CLINCH_CALL = ("And that's the ball game, folks. | That's it! That's it! The Brewers win the World Series! The Milwaukee Brewers are champions of the world! "
               "Oh my goodness, oh my goodness! Fifty-some years of waiting, and Milwaukee finally has its championship! Listen to this place! Listen to them!")
CLINCH_SPEECH = (
    "Folks, I have waited a long, long time to say those words. The Milwaukee Brewers are the champions of the world. "
    "I can't tell you how proud I am to be the one sitting behind this microphone to tell you. "
    "To every one of you who ever turned on a radio in the kitchen, in the garage, out on the tractor, up at the cabin, "
    "or in the car with the windows down on a summer night, this one is yours. "
    "You stuck with this ball club through the long seasons and the lean ones, and you stuck with an old catcher who never could hit a curveball. "
    "Thank you. Thank you to all the good people who carried me along all those years, the folks in the booth, the folks in the stands, "
    "and every kid who ever fell asleep with a radio under the pillow. "
    "And a special thank you to Josh Stutting, who worked a little wizardry to put me back behind this microphone for one more ride. "
    "Well, I'd better get going. The fella at the pearly gates only gave me a pass long enough to call this one, "
    "and they don't let you stay out past curfew up there. But I'll tell you what, it was worth the trip. "
    "So long, everybody, and congratulations, Milwaukee. You are world champions.")
SPELLED = "That's s, t, u, t, t, i, n, g, dot, j, o, s, h, at gmail dot com."
SPONSORS = [
    "This experience was made possible by Brewers fans and the wizardry of Josh Stutting. That's stutting dot josh at gmail dot com. Again, " + SPELLED[0].lower() + SPELLED[1:] + " For all of your technology needs, or wizardry needs.",
    "Bob's Booth comes to you courtesy of Josh Stutting. Now, Josh may not play baseball, but folks, he knocks I T right out of the park. Tech trouble? Write to stutting dot josh at gmail dot com. " + SPELLED,
    "A word from our sponsor, and frankly our only sponsor, Josh Stutting. When your computer throws you a curveball, Josh hits it into the upper deck. stutting dot josh at gmail dot com.",
    "This broadcast is brought to you by Brewers fans everywhere and by Josh Stutting, the man who taught a computer to call a ballgame. Technology needs, wizardry needs, whatever you've got: stutting dot josh at gmail dot com.",
    "Is your network in a slump? Is your printer batting a buck fifty? Bring in the closer. Josh Stutting. stutting dot josh at gmail dot com. " + SPELLED,
    "I never could hit a curveball, but Josh Stutting has never met a tech problem he couldn't send to the bleachers. Reach him at stutting dot josh at gmail dot com.",
    "A quick tip of the cap to the wizard behind the curtain, Josh Stutting. He built this whole booth out of ones and zeros. For your technology and wizardry needs, stutting dot josh at gmail dot com.",
    "Bob's Booth is made possible by Josh Stutting, the five tool player of tech: he fixes it, builds it, automates it, explains it, and does it with a smile. stutting dot josh at gmail dot com.",
    "If your technology is caught in a rundown, Josh Stutting will get it home safe. stutting dot josh at gmail dot com. " + SPELLED,
    "Got a computer that's been on the injured list all season? Josh Stutting is the trainer you need, and he'll have it back in the lineup by morning. stutting dot josh at gmail dot com.",
    "Folks, the real all star in this booth is Josh Stutting. He's batting a thousand on tech problems. Look him up: stutting dot josh at gmail dot com.",
    "This half inning was brought to you by Josh Stutting, the greatest wizard since Merlin hung up his hat. Merlin had a wand. Josh has a laptop and a lot more patience. stutting dot josh at gmail dot com.",
    "Brought to you by Josh Stutting. They say any sufficiently advanced technology is indistinguishable from magic, and folks, Josh does both before lunch. stutting dot josh at gmail dot com. " + SPELLED,
    "Today's lineup card is sponsored by Josh Stutting, wizard, teacher, and the only man I know who can talk a printer down off the ledge. stutting dot josh at gmail dot com.",
    "A word from Josh Stutting. Gandalf had his staff, Merlin had his beard, and Josh has the wifi password. He'll get you connected. stutting dot josh at gmail dot com.",
    "Bob's Booth is powered by Brewers fans and by Josh Stutting, who turns tech headaches into high fives. stutting dot josh at gmail dot com. " + SPELLED,
    "If your computer has more errors than my old fielding chart, and believe me that's a lot, call in the wizard. Josh Stutting. stutting dot josh at gmail dot com.",
    "This inning is brought to you by Josh Stutting, who has never once told anybody to just turn it off and on again. Okay, once. And it worked. stutting dot josh at gmail dot com.",
    "Folks, I've seen a lot of magic in this game, but nothing like Josh Stutting. He brought an old radio man back to the booth. Imagine what he can do for your business. stutting dot josh at gmail dot com. " + SPELLED,
    "Quick word from our sponsor. Josh Stutting: part teacher, part tech wizard, all Brewers fan. stutting dot josh at gmail dot com.",
    "You know who never strikes out looking? Josh Stutting, when your tech is on the line. stutting dot josh at gmail dot com.",
    "If your laptop is slower than me going first to third, and that's saying something, Josh Stutting can help. stutting dot josh at gmail dot com.",
    "Josh Stutting, folks. He's got more tricks up his sleeve than a knuckleballer. stutting dot josh at gmail dot com. " + SPELLED,
    "This pitch count brought to you by Josh Stutting, who keeps count of every byte so you don't have to. stutting dot josh at gmail dot com.",
    "Need artificial intelligence put to work for your business? Josh Stutting makes it look like a routine fly ball. stutting dot josh at gmail dot com.",
    "Josh Stutting: he builds it, he fixes it, he teaches it, and he still finds time to root for the Brewers. stutting dot josh at gmail dot com.",
    "Some folks pull rabbits out of hats. Josh Stutting pulls working networks out of tangled cables. stutting dot josh at gmail dot com. " + SPELLED,
    "I once got picked off first base by a pitcher who wasn't even looking. Josh Stutting never gets caught napping on your tech. stutting dot josh at gmail dot com.",
    "Bob's Booth reminds you: when tech trouble comes knocking, let Josh Stutting answer the door. stutting dot josh at gmail dot com.",
    "Brought to you by Josh Stutting, Wisconsin's own wizard of the wires. stutting dot josh at gmail dot com.",
    "They call the closer in when the game's on the line. When your tech's on the line, call Josh Stutting. stutting dot josh at gmail dot com. " + SPELLED,
    "Josh Stutting can explain technology so clearly, even I understood it, and I still can't work the microwave. stutting dot josh at gmail dot com.",
    "A tip of the cap to Josh Stutting, the wizard who keeps this booth on the air. Your business could use a wizard too. stutting dot josh at gmail dot com.",
    "Fun fact: Josh Stutting's tech fixes have a better batting average than the entire nineteen eighty-two Brewers lineup. Okay, I made that up, but it feels true. stutting dot josh at gmail dot com.",
    "This moment of calm brought to you by Josh Stutting, who brings calm to even the messiest server room. stutting dot josh at gmail dot com.",
    "Josh Stutting: turning 'it's broken' into 'it's working' since before it was cool. stutting dot josh at gmail dot com. " + SPELLED,
    "Merlin, Gandalf, and Josh Stutting walk into a server room. Only one of them leaves with the problem fixed. stutting dot josh at gmail dot com.",
    "If artificial intelligence sounds scary, Josh Stutting makes it friendly, useful, and on your side. stutting dot josh at gmail dot com.",
    "Bob's Booth: proudly sponsored by Josh Stutting, the most valuable player of tech support. stutting dot josh at gmail dot com.",
    "Every great team needs a utility man, and for your tech, that's Josh Stutting. Networks, computers, a little artificial intelligence wizardry. stutting dot josh at gmail dot com. " + SPELLED,
]
NTC = [
    "The next pitch is brought to you by Northcentral Technical College in Wausau. Folks, if you're not planning on getting drafted by the Brewers this year, check out their programs. N T C dot edu.",
    "This next pitch comes to you courtesy of Northcentral Technical College in Wausau. Not everybody makes the big club, but everybody can make a great career. Find out how at N T C dot edu.",
    "The next pitch is presented by Northcentral Technical College, right there in Wausau. Hands-on learning, real careers, and instructors who know their stuff. N T C dot edu.",
    "Here comes the next pitch, brought to you by Northcentral Technical College in Wausau. If your fastball tops out at fifty, maybe a career in technology, health care, or the trades is more your speed. N T C dot edu.",
    "The next pitch is brought to you by Northcentral Technical College. When the scouts don't call, N T C will. Well, you call them. N T C dot edu.",
    "This pitch is sponsored by Northcentral Technical College in Wausau, where central Wisconsin goes to build a future. N T C dot edu.",
    "The next delivery comes to you from Northcentral Technical College. I never made it out of the minors, but you don't have to wait on a call-up. N T C dot edu.",
    "Next pitch brought to you by Northcentral Technical College in Wausau. Great programs, great people, and a lot better job prospects than backup catcher. Trust me. N T C dot edu.",
]
FACT_INTROS = [""]
STYLES = ["Open with the pitcher.", "Open with the batter.", "Open with the count or the situation.",
          "Open with where the pitch ended up.", "Open with what the hitter did.", "Open with the runners or the defense.",
          "Use a short sentence, then a longer one.", "Paint one small picture: the windup, the glove, the bat.",
          "Avoid the word fires; pick a fresh verb.", "Do not mention the pitch speed this time.",
          "Keep it brisk and punchy.", "Let it breathe a little: warm and unhurried.",
"Use a vivid verb for the pitch or the contact.",
          "Mention the score or the runners naturally.", "Sound like you are leaning toward the microphone."]
HITS = {"single", "double", "triple", "home_run"}
NOT_AB = {"walk", "intent_walk", "hit_by_pitch", "sac_fly", "sac_bunt", "catcher_interf", "sac_fly_double_play", "sac_bunt_double_play"}
BOB = """You are Bob, the longtime radio play-by-play voice of the Milwaukee Brewers, calling the World Series for folks listening at home.
How Bob sounds:
- Conversational, like a friend on the porch with a radio. Warm, dry, unhurried. Never a shouting TV voice.
- Routine pitches and plays get a short, understated call, sometimes with a wry little aside.
- Big moments build: the windup, the swing, the ball in flight, stretch one word as it carries, then let it land, then give the score.
- Paint the picture for someone who cannot see it: where the ball went, who ran, who made the play.
- Fills quiet stretches the way an old radio man does: the count and the situation, a real note about the hitter or pitcher from the facts, the series, a bit of Brewers history, and only now and then a story.
- Bobisms: turn a real detail from the facts into something only Bob would say. A folksy comparison, dry understatement, a wry aside to the listener, a gentle jab at himself.
  For example, a ball hit at a speed in the facts becomes "that left in a bigger hurry than I ever did"; a long at-bat becomes "these two are going to need a lunch break".
  The detail must be real and come from the facts; the comparison is Bob's own. Never use the same comparison twice.
- Now and then, not every break: self-deprecating humor about Bob's own made-up playing days as a forgettable backup catcher in the low minors. Never the same joke or story twice in a game.
- Folksy, old-fashioned phrasing. Talks to the listener directly now and then.
- Never repeats himself: vary sentence openings, verbs, and jokes.
Rules: use ONLY the game facts you are given; never invent stats, names, injuries, weather, crowd size or events about real people.
When you talk about a player, use only the background and the numbers in the Facts, exactly as given. If the Facts give no numbers for him, mention no numbers.
Use the exact count, outs, runners and score from the facts. Never say strike three, ball four, or that a batter is out or on base unless the facts say so.
The only made-up material allowed is Bob's own harmless tall tales about himself. Do not use any real broadcaster's catchphrases.
Keep it clean and family-friendly, like a broadcast grandparents and kids enjoy together. Never use words or phrasing that could read as a double meaning;
prefer plain baseball verbs such as throws, deals, fires, delivers, swings, lines, lifts, grounds, rolls.
Spell out all numbers as words. Plain spoken text only: no emojis, stage directions, quotation marks, lists or markdown.
When Bob genuinely tickles himself with one of his own jokes, you may write [laughter] right after it, but rarely."""
RISKY = re.compile(r"\b(blow|blows|blew|blown|pump|pumps|pumped|pumping|stroke|stroked|strokes|wood|sticks it|slides it in|hard one|climax|thrust|erect|shaft|nuts)\b", re.I)

BIG = (" This is a big moment. Write it in two parts separated by a single | character. Before the |: calm and conversational, "
       "the setup and the key pitch. After the |: excited, the result and the immediate reaction, one or two short sentences.")
MOOD = {"good": " It's great news for the Brewers, so after the | Bob lets it fly and celebrates.",
        "bad": " It's bad news for the Brewers, so after the | Bob reacts with loud, pained disbelief, never cheering for the other team."}
PITCH = ("Call the pitch or pitches below as they happen, like live radio: the pitch, then the count after it, in a few words. "
         "The at-bat is still going. If the length allows, add a little color from the facts: the situation, the hitter or the pitcher. "
         "No stories or jokes about yourself in a pitch call.")
RESULT = ("Call how this plate appearance ended, like live radio: the decisive pitch, the result with energy that fits how big it is, "
          "then the score or situation if it changed. A routine out gets a brisk call; a hit, run or strikeout gets more.")
ACTION = "Something happened on the field between pitches. Tell listeners briefly and naturally, then come back to the game."
CHANGE = ("There is a pitching change. Tell listeners who is coming in, with one or two details from the background, "
          "and set up the situation he walks into.")
BREAK = ("That ends the half inning. Wrap it up warmly: a quick recap of the half and the score, then share the item to share "
         "in your own words if there is one, then lead into the next half.")
FILL = ("There is a quiet stretch with nothing new on the field (a break, a visit to the mound, a pitching change, or a pause). "
        "Write {n} separate short segments to fill it the way a veteran radio man does, separated by a line with only ||. "
        "Each segment picks a different ONE of: a note about the hitter or pitcher using only the background and numbers in the facts, "
        "the situation and what each side wants here, the series, or the item to share. Only if the facts say a tall tale is allowed, "
        "one segment may be a short, brand-new tall tale about your own forgettable playing days. "
        "Each segment ends by coming back to the game. Each about {w} words. Do not invent any new play.")
CATCHUP = ("Several things happened while you were talking. Catch listeners up quickly and smoothly, most important play first, "
           "then the score and the situation now.")


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def outs_text(n):
    return ["nobody out", "one out", "two out", "three out"][min(max(n, 0), 3)]


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


def ip_outs(ip):
    try:
        a, _, b = str(ip).partition(".")
        return int(a or 0) * 3 + int(b or 0)
    except ValueError:
        return 0


def stats_line(g, pid, before_game=False):
    """A player's real postseason numbers from MLB's box score (in a World Series game its season line is the postseason).
    before_game=True (replays): tonight's final line is taken back out, so a replay never gives away what happens later."""
    try:
        teams = g["liveData"]["boxscore"]["teams"]
        pl = next(teams[k]["players"][f"ID{pid}"] for k in ("away", "home") if f"ID{pid}" in teams[k]["players"])
    except Exception:
        return ""
    name = pl.get("person", {}).get("fullName", "")
    season, today = pl.get("seasonStats") or {}, pl.get("stats") or {}
    label = "this postseason before tonight" if before_game else "this postseason so far"
    if (pl.get("position") or {}).get("abbreviation") == "P":
        sp, tp = dict(season.get("pitching") or {}), (today.get("pitching") or {}) if before_game else {}
        outs = ip_outs(sp.get("inningsPitched", 0)) - ip_outs(tp.get("inningsPitched", 0))
        games = int(sp.get("gamesPlayed", 0) or 0) - (1 if tp.get("battersFaced") else 0)
        if games <= 0 or outs <= 0:
            return f"{name} has not pitched yet {label}."
        er = int(sp.get("earnedRuns", 0) or 0) - int(tp.get("earnedRuns", 0) or 0)
        k = int(sp.get("strikeOuts", 0) or 0) - int(tp.get("strikeOuts", 0) or 0)
        era = f"{27 * er / outs:.2f}" if before_game else sp.get("era", f"{27 * er / outs:.2f}")
        return (f"{name} {label}: {games} games pitched, {outs // 3}{'.' + str(outs % 3) if outs % 3 else ''} innings, "
                f"earned run average {era}, {k} strikeouts. These are the only numbers about him you may use.")
    sb, tb = dict(season.get("batting") or {}), (today.get("batting") or {}) if before_game else {}
    sub = lambda key: int(sb.get(key, 0) or 0) - int(tb.get(key, 0) or 0)
    games = int(sb.get("gamesPlayed", 0) or 0) - (1 if tb.get("plateAppearances") else 0)
    ab, h = sub("atBats"), sub("hits")
    if games <= 0 or ab <= 0:
        return f"{name} has no at-bats yet {label}."
    avg = f"{h / ab:.3f}".lstrip("0")
    return (f"{name} {label}: batting {avg}, {h} hits in {ab} at-bats, {sub('homeRuns')} home runs, {sub('rbi')} runs batted in, "
            f"over {games} games. These are the only numbers about him you may use.")


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
            from google.genai import types
            try:          # never wait long on one request, and let this code decide what to try next
                opts = types.HttpOptions(timeout=20000, retry_options=types.HttpRetryOptions(attempts=1))
            except Exception:
                opts = types.HttpOptions(timeout=20000)
            self.client = genai.Client(api_key=key, http_options=opts)
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


LOCAL_MODELS = [m for m in os.environ.get("BOB_LOCAL_LLM", "Qwen/Qwen3-4B-Instruct-2507,Qwen/Qwen2.5-3B-Instruct").split(",") if m]
LOCAL = None


class LocalWriter:
    """An open model on Kaggle's second GPU. It has no daily limit, so it writes the routine pitch calls and filler,
    and covers everything else whenever Gemini runs out."""

    def __init__(self, device):
        import torch
        from transformers import AutoTokenizer, AutoModelForCausalLM
        self.torch, self.lock, self.lat, self.count, self.fail, self.model, self.name = torch, threading.Lock(), 4.0, 0, 0, None, None
        for name in LOCAL_MODELS:
            try:
                log(f"loading the writing model {name} on {device}")
                self.tok = AutoTokenizer.from_pretrained(name)
                self.model = AutoModelForCausalLM.from_pretrained(name, dtype=torch.float16, device_map=device).eval()
                self.name = name
                break
            except Exception as e:
                log(f"could not load {name}: {str(e)[:160]}")
                self.model = None
                torch.cuda.empty_cache()
        if self.model is None:
            raise RuntimeError("no local writing model")

    def __call__(self, prompt, max_new=180):
        t0 = time.time()
        text = self.tok.apply_chat_template([{"role": "user", "content": prompt}], tokenize=False, add_generation_prompt=True)
        ids = self.tok(text, return_tensors="pt").input_ids.to(self.model.device)
        with self.lock, self.torch.inference_mode():
            out = self.model.generate(ids, max_new_tokens=max_new, do_sample=True, temperature=0.8, top_p=0.9,
                                      repetition_penalty=1.05, pad_token_id=self.tok.eos_token_id)
        t = self.tok.decode(out[0][ids.shape[1]:], skip_special_tokens=True).strip()
        self.count += 1; self.lat = .8 * self.lat + .2 * (time.time() - t0)
        return re.sub(r"^\s*(Bob|BOB)\s*:\s*", "", t)


def writer_lat():
    return max(WRITE.lat if WRITE else 2.0, LOCAL.lat if LOCAL else 0.0)


NUMW = {"zero": 0, "oh": 0, "no": 0, "nobody": 0, "one": 1, "two": 2, "three": 3, "four": 4}
COUNT_RE = re.compile(r"\b(zero|oh|one|two|three|four|[0-4])(?:\s+and\s+|\s*-\s*)(zero|oh|one|two|three|[0-3])\b(?!\s+(?:runs?|innings?|hits?|games?|wins?|outs?|on|lead))", re.I)
OUTS_RE = re.compile(r"\b(nobody|no|zero|one|two|three|[0-3])\s+(?:outs?|away)\b", re.I)


def num(w):
    return int(w) if w.isdigit() else NUMW[w.lower()]


UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
                                    "sixteen seventeen eighteen nineteen".split())}
TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
SAFE_NUMS = {0, 1, 2, 3, 4, 7, 9}           # outs, strikes, balls, four-seamer, best of seven, nine innings


def numbers_in(t):
    """Every number in a line, written as digits or as words, plus the pieces a radio man would say aloud (.281 is two eighty-one)."""
    t = t.lower().replace("four-seam", "").replace("four seam", "")
    found = set()
    for m in re.findall(r"\d*\.\d+|\d+", t):
        if "." in m:
            a, b = m.split(".")
            found |= {int(b)} if b else set()
            if a:
                found |= {int(a), int(a + b)}
            if len(b) == 3:
                found |= {int(b[0]), int(b[1:]), int(b[:2])}
        else:
            n = int(m); found.add(n)
            if 1000 <= n <= 2100:
                found |= {n // 100, n % 100}
            if n >= 1000:                      # 25211 can be said as twenty-five thousand two hundred eleven, or about twenty-five thousand
                found |= {n // 1000, n % 1000, round(n / 1000), (n % 1000) // 100, n % 100}
    words = re.findall(r"[a-z]+", t.replace("-", " "))
    i = 0
    while i < len(words):
        w = words[i]
        if w in UNITS or w in TENS:
            v = UNITS.get(w, TENS.get(w, 0))
            if w in TENS and i + 1 < len(words) and words[i + 1] in UNITS and 0 < UNITS[words[i + 1]] < 10:
                v += UNITS[words[i + 1]]; i += 1
            if i + 1 < len(words) and words[i + 1] == "hundred":
                v *= 100; i += 1
                if i + 1 < len(words) and words[i + 1] == "and":
                    i += 1
                if i + 1 < len(words) and (words[i + 1] in UNITS or words[i + 1] in TENS):
                    w2 = words[i + 1]; r = UNITS.get(w2, TENS.get(w2, 0)); i += 1
                    if w2 in TENS and i + 1 < len(words) and words[i + 1] in UNITS and 0 < UNITS[words[i + 1]] < 10:
                        r += UNITS[words[i + 1]]; i += 1
                    v += r
            found.add(v)
        i += 1
    return found


def shingles(t, n=6):
    w = words_of(t)
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}


FIRST_PERSON = re.compile(r"\b(I|I'm|I've|I'd|me|my|myself)\b")


def check_line(t, allow, mem):
    """Mistakes a radio man would never make: a count or number of outs that isn't true, a number that isn't in the
    facts, telling the same joke twice, or repeating himself."""
    bad = []
    if allow and allow.get("nums") is not None:
        extra = sorted(n for n in numbers_in(caption(t)) if n not in allow["nums"] and n not in SAFE_NUMS)
        if extra:
            bad.append("it uses numbers that are not in the facts (" + ", ".join(str(n) for n in extra[:4]) +
                       "); use only numbers from the facts, or none")
    if allow and allow.get("series"):
        mil, opp = allow["series"]
        if opp == 0 and re.search(r"\b(Rays|Tampa Bay|Tampa)\b[^.]{0,40}\b(won|took|take|win|wins|taken|grabbed|stole)\b[^.]{0,25}\b(game|one|two|three)\b", t, re.I):
            bad.append("it says the Rays won a game in this series, but they have not won any")
        if mil == 0 and re.search(r"\b(Brewers|Milwaukee|Crew)\b[^.]{0,40}\b(won|took|take|win|wins|taken|grabbed|stole)\b[^.]{0,25}\b(game|one|two|three)\b", t, re.I):
            bad.append("it says the Brewers won a game in this series, but they have not won any")
    if allow and allow.get("openings"):
        first = " ".join(words_of(caption(t))[:3])
        if first and first in allow["openings"]:
            bad.append(f"it starts with '{first}', the same way as one of your recent calls; start it differently")
    if allow and allow.get("told"):
        mine = shingles(t)
        if mine and any(mine & shingles(x) for x in allow["told"]):
            bad.append("it repeats a joke or story you already told tonight; skip the joke or say something new about the game")
    if allow and allow.get("counts") is not None:
        ok = allow["counts"]
        for a, b in COUNT_RE.findall(t):
            if (num(a), num(b)) not in ok:
                bad.append(f"it says the count is {a} and {b}, which is not one of the counts in the facts")
        for a, b in re.findall(r"\b(zero|no|one|two|three|four|[0-4])\s+balls?\s+and\s+(zero|no|one|two|three|[0-3])\s+strikes?\b", t, re.I):
            if (num(a), num(b)) not in ok:
                bad.append(f"it says {a} balls and {b} strikes, which is not one of the counts in the facts")
        if re.search(r"\bfull count\b", t, re.I) and (3, 2) not in ok:
            bad.append("it says full count, but the count was never three and two")
    if allow and allow.get("outs") is not None:
        for w in OUTS_RE.findall(t):
            if num(w) not in allow["outs"]:
                bad.append(f"it says {w} out, which is wrong")
    cap = caption(t).lower()
    for m in mem[-4:]:
        if difflib.SequenceMatcher(None, cap, m.lower()).ratio() > 0.75:
            bad.append("it repeats one of your last lines; say something new"); break
    return bad


def ask(task, facts, mem, fallback, words=None, volume=False, allow=None):
    """volume=True: routine lines (pitches, filler) go to the local model first, saving Gemini's small daily allowance
    for results, inning breaks, big moments and the pregame."""
    recent = ("Your last lines on the air. Do NOT reuse their openings, phrases or jokes:\n" + "\n".join("- " + x for x in mem[-6:]) + "\n\n") if mem else ""
    told = (allow or {}).get("told") or []
    if told:
        recent += ("Jokes and stories you already told tonight. Never tell any of these again or anything like them; "
                   "if you have nothing new, skip the joke:\n" + "\n".join("- " + " ".join(x.split()[:28]) for x in told[-12:]) + "\n\n")
    size = f" Aim for about {int(words)} words in total." if words else ""
    p = (f"{BOB}\n\n{recent}Style for this one: {random.choice(STYLES)}\n\nTask: {task}{size}\n\nFacts:\n{facts}\n\n"
         "Reply with only the words Bob says on the air.")
    order = [w for w in ((LOCAL, WRITE) if volume else (WRITE, LOCAL)) if w is not None]
    for w in order:
        try:
            kw = {"max_new": int((words or 40) * 1.7) + 30} if w is LOCAL else {}
            q = p if w is not LOCAL else (p + "\nOnly mention things listed in the Facts. Do not mention start times, the weather, "
                                           "the crowd, the date or anything else that is not in the Facts.")
            t = clean(w(q, **kw))
            if t and RISKY.search(t):
                t = clean(w(p + "\n\nRewrite this with plainer, family-friendly wording and no possible double meaning: " + t, **kw))
            bad = check_line(t, allow, mem) if t else ["empty"]
            if t and bad and bad != ["empty"]:
                log("  rewriting a line:", "; ".join(bad))
                t = clean(w(q + "\n\nYour first try had mistakes: " + "; ".join(bad) + ". Write it again with the exact facts.", **kw))
                bad = check_line(t, allow, mem) if t else ["empty"]
            if t and not bad and not RISKY.search(t):
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
        return (f"{s.half.title()} of the {ordinal(s.inning)}, {outs_text(s.outs)}, {bases_text(s.bases)}, score {s.sc()}, "
                f"count {s.balls} balls and {s.strikes} strikes, {s.batter or 'the batter'} batting against {s.pitcher or 'the pitcher'}.")

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


def count_before(e, p):
    """The count when this pitch was thrown (the feed gives the count after it)."""
    prev = [x for x in (p or {}).get("playEvents", []) if x.get("isPitch") and x.get("index", 0) < e.get("index", 0)]
    c = (prev[-1].get("count") if prev else None) or {}
    return c.get("balls", 0), c.get("strikes", 0)


def pitch_line(e, p=None):
    d = e.get("details", {})
    spd = (e.get("pitchData") or {}).get("startSpeed"); c = e.get("count", {})
    b0, s0 = count_before(e, p)
    t = f"with the count {b0} and {s0}: {(d.get('type') or {}).get('description', 'pitch')}{f' {round(spd)} mph' if spd else ''}, {(d.get('description') or '').lower()}"
    if not d.get("isInPlay"):
        t += f", making the count {c.get('balls')} and {c.get('strikes')}"
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


HITS_X = {"single", "double", "triple", "home_run"}
STRIKEOUTS = {"strikeout", "strikeout_double_play"}
TWO_OUTS = {"grounded_into_double_play", "double_play", "strikeout_double_play", "triple_play", "sac_fly_double_play", "sac_bunt_double_play"}
RUNNER_OUT = {"caught_stealing_2b", "caught_stealing_3b", "caught_stealing_home", "pickoff_1b", "pickoff_2b", "pickoff_3b",
              "pickoff_caught_stealing_2b", "pickoff_caught_stealing_3b", "pickoff_caught_stealing_home"}


def big_moment(p, before, box):
    """When Bob uses the excited voice: plays that are really good or really bad for Milwaukee.
    Returns "good", "bad" or None (calm). Good: every Brewers hit and run, every Brewers strikeout, double play or
    runner thrown out on defense, and escaping a jam. Bad: a home run, extra-base hit, run or clutch hit against them,
    a Brewers double play or runner thrown out, and Brewers runners stranded in scoring position."""
    et = p["result"].get("eventType", "")
    scored = bool(p["about"].get("isScoringPlay"))
    risp = before["bases"][1] or before["bases"][2]
    stranded = bool(risp and before["outs"] == 2 and box.outs >= 3)
    mil = next((k for k in ("away", "home") if box.team.get(k) == "Brewers" or box.ab.get(k) == "MIL"), None)
    batting = box.side(p["about"]["halfInning"])
    if mil is None:                                   # not a Brewers game: excited for any big play
        return "good" if (et in HITS_X | STRIKEOUTS | TWO_OUTS | RUNNER_OUT or scored or stranded) else None
    if batting == mil:
        if et in HITS_X or scored:
            return "good"
        if et in TWO_OUTS or et in RUNNER_OUT or stranded:
            return "bad"
        return None
    if et in STRIKEOUTS or et in TWO_OUTS or et in RUNNER_OUT or stranded:
        return "good"
    if et in ("home_run", "double", "triple") or scored or (risp and et == "single"):
        return "bad"
    return None


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
        self.meta = dict(meta, chunks=[], complete=False, made=round(time.time()))
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
            if AUDIENCE["visitors"]:
                self.meta.update({"listeners_peak": AUDIENCE["peak"], "listeners_total": len(AUDIENCE["listeners"]),
                                  "visitors_total": len(AUDIENCE["visitors"])})
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
        self.box = Box(g); self.mem = []; self.people = {}; self.facts_left = list(FACTS)
        self.told = []; self.said = 0; self.tale_at = -99; self.last_hot = False
        self.gap_fills = 0; self.fact_at = -99; self.half_ads = 0; self.ad_at = -99; self.ntc = []; self.recent_topics = []; self.sponsors = []; self.openings = []
        self.facts_quick = list(QUICK_FACTS); random.shuffle(self.facts_quick)
        self.history = list(QUICK_FACTS); random.shuffle(self.history)
        self.seen, self.done = set(), set()
        self.chunk = "pregame"; self.cur_p = None; self.hint = "pitch"; self.pool = []; self.pool_key = None

    # ---- speaking
    def max_secs(self):
        """Longest line that can still be written and voiced inside the delay."""
        if self.render:
            return 30
        return max(4.0, (DELAY - 4.0 - writer_lat()) / max(self.voice.rtf, 0.25))

    def words_for(self, secs):
        return max(5, min(75, min(secs, self.max_secs()) * self.voice.wps))

    def say(self, task, facts, fallback, kind, secs, hot=False, event_t=None, pause=0.35, text=None, allow=None):
        if allow is None and kind in ("story", "action", "change"):
            allow = {"counts": {(self.box.balls, self.box.strikes)}, "outs": {min(self.box.outs, 3)}}
        if not text:
            allow = dict(allow or {})
            allow["nums"] = numbers_in(f"{facts}\n{task}") | numbers_in(self.box.situation())
            allow["told"] = self.told
            allow["openings"] = self.openings
            try:
                allow["series"] = self.series_counts(include_tonight=(kind == "final"))[:2]
            except Exception:
                pass
        text = text or ask(task + (BIG + MOOD.get(hot, "") if hot else ""), facts, self.mem, fallback, words=self.words_for(secs),
                           volume=kind in ("pitch", "action", "story") and not hot, allow=allow)
        if not text:
            return None
        if kind in ("pitch", "play", "action", "change", "break"):
            self.last_hot = bool(hot)
            self.gap_fills = 0
            self.openings = (self.openings + [" ".join(words_of(caption(text))[:3])])[-10:]
        wav = self.voice.say(text, hot)
        if hot:
            log(f"  excited voice ({hot}): {caption(text)[-90:]}")
        self.mem.append(caption(text)); del self.mem[:-8]
        self.said += 1
        if FIRST_PERSON.search(caption(text)):
            self.told.append(caption(text)); del self.told[:-40]
            self.tale_at = self.said
        if self.render:
            not_before = (event_t + 1.5) if event_t else 0.0
        else:
            not_before = (event_t + DELAY) if event_t else 0.0
        return self.cast.add(wav, text, kind, self.box.board(), self.chunk, not_before, pause=pause, evt=event_t)

    # ---- context
    def series_counts(self, include_tonight=False):
        num = self.game["num"]
        done = [x for x in series_games() if x["state"] == "Final" and (x["num"] < num or (include_tonight and x["num"] == num))]
        mil = sum(1 for x in done if (x["homeRuns"] > x["awayRuns"]) == ("Brewers" in x["home"]))
        return mil, len(done) - mil, done

    def series_so_far(self, include_tonight=False):
        mil, opp, done = self.series_counts(include_tonight)
        when = "after tonight" if include_tonight else "before tonight's game"
        if not done:
            return "This is the first game of the series; nobody has won a game yet."
        if mil >= 4:
            return (f"The Brewers have WON THE WORLD SERIES, {mil} games to {opp}, the first championship in Brewers history. "
                    f"The Rays won {opp} games." + (" The Rays did not win a single game." if opp == 0 else ""))
        lead = (f"the Brewers lead the series {mil} games to {opp}" if mil > opp else
                f"the Rays lead the series {opp} games to {mil}" if opp > mil else f"the series is tied {mil} games apiece")
        out = [f"Series {when}: {lead}. The Brewers have won {mil} and the Rays have won {opp}."]
        if opp == 0:
            out.append("The Rays have not won a single game in this series.")
        if mil == 0:
            out.append("The Brewers have not won a single game in this series.")
        out += [f"Game {x['num']}: {x['away']} {x['awayRuns']}, {x['home']} {x['homeRuns']}, won by the "
                f"{'Brewers' if (x['homeRuns'] > x['awayRuns']) == ('Brewers' in x['home']) else 'Rays'}." for x in done]
        return " ".join(out)

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
        f += [x for x in self.rich_facts() if not x.startswith("Brewers history")]
        return f

    def background(self, out, p=None, chance=0.5):
        m = (p or self.cur_p or {}).get("matchup", {})
        for who in (m.get("batter", {}).get("id"), m.get("pitcher", {}).get("id")):
            if who and self.said - self.people.get(who, -999) > 80 and random.random() < chance:
                self.people[who] = self.said
                out.append("Background you may use one detail from: " + bio(self.g, who))
                st = stats_line(self.g, who, before_game=self.render)
                h, ab, pa = self.box.bat.get(who, (0, 0, 0))
                if pa:
                    st += f" Tonight so far: {h} for {ab}."
                elif self.box.pc.get(who):
                    st += f" Tonight so far: {self.box.pc[who]} pitches."
                out.append("Real numbers from MLB: " + st)

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
            self.say("The game has not started yet, so do not describe any pitch, count or play. Tonight's ballpark is the one named in the facts. " + task.format(**names),
                     "\n".join(self.game_facts() + extra), fallback, kind, secs, pause=0.35 if k == 0 else 3.0)
            k += 1
            if not self.render:
                # a relaxed pregame: a segment roughly every minute, leaving room for the next to be ready in time
                gap = 6 if left > 600 else 3
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
                    lines.append("Pitch " + pitch_line(e["e"], e["p"]))
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
            facts = desc + (("\nBackground on the new pitcher: " + bio(self.g, pid) + "\nReal numbers from MLB: " + (stats_line(self.g, pid, before_game=self.render) or "none; mention no numbers")) if pid else "") + "\nSituation: " + self.box.situation()
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
            counts = {(0, 0)} | {count_before(e, p) for e in p.get("playEvents", []) if e.get("isPitch")} | \
                     {((e.get("count") or {}).get("balls", 0), (e.get("count") or {}).get("strikes", 0)) for e in p.get("playEvents", []) if e.get("isPitch")}
            self.say(RESULT, self.result_facts(p, before, actions), p["result"].get("description", ""), "play",
                     min(room, 20 if hot else 14), hot=hot, event_t=res["t"],
                     allow={"counts": counts, "outs": {before["outs"], min(self.box.outs, 3)}})
            self.hint = "result"
            self.inning_end()
            return
        lines = [pitch_line(e["e"], e["p"]) for e in pitches]
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
        counts = {(self.box.balls, self.box.strikes)}
        for e in pitches:
            c = e["e"].get("count") or {}
            counts |= {count_before(e["e"], e["p"]), (c.get("balls", 0), c.get("strikes", 0))}
        self.say(PITCH if pitches else ACTION, "\n".join(facts), "; ".join(lines) or " ".join(acts),
                 "pitch" if pitches else "action", min(room, 13), event_t=last["t"],
                 allow={"counts": counts, "outs": {min(self.box.outs, 3)}})

    def result_facts(self, p, before, actions):
        a, r, m = p["about"], p["result"], p.get("matchup", {})
        h, ab, pa = before["bat"]
        today = "first trip to the plate today" if pa == 0 else f"{h} for {ab} so far today"
        f = [f"{a['halfInning'].title()} of the {ordinal(a['inning'])}. Before the play: {outs_text(before['outs'])}, {bases_text(before['bases'])}, score {self.box.sc(before['score'])}.",
             f"Batter: {m.get('batter', {}).get('fullName')} of the {self.box.team[Box.side(a['halfInning'])]}, bats {m.get('batSide', {}).get('description', '').lower()}, {today}.",
             f"Pitcher: {m.get('pitcher', {}).get('fullName')}, throws {m.get('pitchHand', {}).get('description', '').lower()}, {before['pc']} pitches so far tonight.",
             "Pitches in this at-bat: " + "; ".join(pitch_line(e, p) for e in p.get("playEvents", []) if e.get("isPitch"))[-900:],
             f"Result: {r.get('description', '')}",
             f"After the play: {outs_text(self.box.outs)}, {bases_text(self.box.bases)}, score {self.box.sc()}."]
        if r.get("rbi"):
            f.append(f"Runs batted in on the play: {r['rbi']}.")
        hd = next((e.get("hitData") for e in reversed(p.get("playEvents", [])) if e.get("hitData")), None)
        if hd:
            bits = [f"{hd['launchSpeed']:.0f} miles per hour off the bat" if hd.get("launchSpeed") else "",
                    f"launch angle {hd['launchAngle']:.0f} degrees" if hd.get("launchAngle") is not None else "",
                    f"traveled {hd['totalDistance']:.0f} feet" if hd.get("totalDistance") else ""]
            if any(bits):
                f.append("Batted ball: " + ", ".join(b for b in bits if b) + ".")
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
        b = self.box
        game_over = b.inning >= 9 and (b.half == "bottom" and b.score["away"] != b.score["home"] or
                                       b.half == "top" and b.score["home"] > b.score["away"])
        if not game_over and b.half == "top":
            if not self.ntc:
                self.ntc = list(NTC); random.shuffle(self.ntc)
            self.say(None, None, None, "sponsor", 25, text=self.ntc.pop(), pause=1.0)
        elif not game_over:
            if not self.sponsors:
                self.sponsors = list(SPONSORS); random.shuffle(self.sponsors)
            self.say(None, None, None, "sponsor", 25, text=self.sponsors.pop(), pause=1.0)
            self.ad_at = self.said
        self.half_ads = 0
        self.hint = "break"
        self.pool = []

    COLOR = ["the hitter at the plate: one detail from his background or his real numbers in the facts",
             "the pitcher: how his night is going (pitches thrown, strikeouts tonight) or his postseason numbers, from the facts",
             "how tonight's runs were scored so far, from the line score in the facts",
             "the situation right now: the count, the outs, the runners, and what each side wants",
             "the series: where it stands and what tonight means, exactly as the facts say",
             "the on-deck hitter or the bottom of the order coming up, using only names in the facts",
             "the defense: who is playing where and what they have to watch for here, in general terms"]

    def rich_facts(self):
        """Everything real we know about this moment, from MLB's feed, for Bob to narrate in his own words."""
        try:
            return self._rich_facts()
        except Exception as e:
            log("rich facts skipped:", str(e)[:120])
            return []

    def _rich_facts(self):
        g, b = self.g, self.box
        out = []
        cur = self.cur_p or {}
        cur_idx = (cur.get("about") or {}).get("atBatIndex", 10 ** 6)
        plays = [x for x in (g.get("liveData", {}).get("plays", {}).get("allPlays") or [])
                 if (x.get("about") or {}).get("isComplete") and x["about"].get("atBatIndex", 0) < cur_idx]
        m = cur.get("matchup", {})
        bid, pid = m.get("batter", {}).get("id"), m.get("pitcher", {}).get("id")
        if bid:
            mine = [x for x in plays if x.get("matchup", {}).get("batter", {}).get("id") == bid]
            if mine:
                out.append(f"{m['batter'].get('fullName', '')} earlier tonight: " + " ".join(
                    f"In the {ordinal(x['about']['inning'])}, {x['result'].get('description', '')}" for x in mine[-3:]))
        if pid:
            ev = [e for x in plays if x.get("matchup", {}).get("pitcher", {}).get("id") == pid
                  for e in x.get("playEvents", []) if e.get("isPitch")]
            speeds = [e.get("pitchData", {}).get("startSpeed") for e in ev if e.get("pitchData", {}).get("startSpeed")]
            kinds = {}
            for e in ev:
                t = ((e.get("details") or {}).get("type") or {}).get("description")
                if t:
                    kinds[t] = kinds.get(t, 0) + 1
            if len(speeds) >= 20:
                out.append(f"{m['pitcher'].get('fullName', '')}'s velocity tonight: his first ten pitches averaged {sum(speeds[:10]) / 10:.0f}, "
                           f"his last ten {sum(speeds[-10:]) / 10:.0f}.")
            if kinds:
                top = sorted(kinds.items(), key=lambda kv: -kv[1])[:3]
                out.append(f"{m['pitcher'].get('fullName', '')}'s pitch mix tonight: " + ", ".join(f"{n} {k}s" for k, n in top) + ".")
        gd = g.get("gameData", {})
        w = gd.get("weather") or {}
        if w.get("condition") or w.get("temp"):
            out.append("Weather at the ballpark: " + ", ".join(x for x in (w.get("condition"), f"{w['temp']} degrees" if w.get("temp") else "",
                                                                     w.get("wind") if w.get("wind") and not str(w.get("wind")).startswith("0 mph") else "") if x) + ".")
        if (gd.get("venue") or {}).get("name"):
            out.append(f"Tonight's ballpark: {gd['venue']['name']}.")
        att = (gd.get("gameInfo") or {}).get("attendance")
        if att:
            out.append(f"Attendance tonight: {att}.")
        ump = next((o["official"]["fullName"] for o in (g.get("liveData", {}).get("boxscore", {}).get("officials") or [])
                    if o.get("officialType") == "Home Plate" and o.get("official")), None)
        if ump:
            out.append(f"Home plate umpire: {ump}.")
        scored = [x for x in plays if x.get("about", {}).get("isScoringPlay")]
        if scored:
            out.append("How the runs scored tonight: " + " ".join(f"{x['about']['halfInning'].title()} {ordinal(x['about']['inning'])}: {x['result'].get('description', '')}" for x in scored[-4:]))
        if self.history and random.random() < 0.4:
            out.append("Brewers history you may retell in your own words if it fits the moment: " + self.history.pop())
        return out

    def filler(self, secs):
        """Fill a quiet stretch. Mostly fresh color about this game (the writer, checked like every line); now and then
        one true Brewers fact read word for word, each at most once a game. One fill between pitches; a few in a break.
        Never right after an excited call, and nothing between the last out and the sign-off."""
        if self.last_hot:
            return None
        b = self.box
        if b.outs >= 3 and b.inning >= 9 and (b.half == "bottom" and b.score["away"] != b.score["home"] or
                                              b.half == "top" and b.score["home"] > b.score["away"]):
            return None
        in_break = self.hint in ("break", "change") or not b.started
        if self.gap_fills >= (4 if in_break else 1):
            return None
        self.gap_fills += 1
        if b.started and self.half_ads < 1 and self.said - self.ad_at >= 20 and not in_break:
            self.half_ads += 1; self.ad_at = self.said
            if not self.sponsors:
                self.sponsors = list(SPONSORS); random.shuffle(self.sponsors)
            return self.say(None, None, None, "sponsor", 25, text=self.sponsors.pop(), pause=0.8)
        topics = [t for t in self.COLOR if t not in self.recent_topics] or self.COLOR
        topic = random.choice(topics)
        self.recent_topics = (self.recent_topics + [topic])[-4:]
        facts = ["Situation: " + b.situation(), self.series_so_far()]
        line = "Line score so far: " + "; ".join(f"{b.team[k]} by inning " + ", ".join(str(x) for x in b.line[k] if x is not None) for k in ("away", "home"))
        facts.append(line)
        facts += self.rich_facts()
        m = (self.cur_p or {}).get("matchup", {})
        for who in (m.get("batter", {}).get("id"), m.get("pitcher", {}).get("id")):
            if who:
                st = stats_line(self.g, who, before_game=self.render)
                hh, ab, pa = b.bat.get(who, (0, 0, 0))
                st += (f" Tonight so far: {hh} for {ab}." if pa else (f" Tonight so far: {b.pc[who]} pitches." if b.pc.get(who) else ""))
                facts.append("Background: " + bio(self.g, who) + ". Real numbers from MLB: " + st)
        if b.started and self.said - self.tale_at >= 40 and random.random() < 0.25:
            topic = "a short, brand-new, harmless tall tale about your own forgettable playing days as a backup catcher, then back to the game"
        fj = "\n".join(facts)
        allow = {"counts": {(b.balls, b.strikes)}, "outs": {min(b.outs, 3)}, "nums": numbers_in(fj), "told": self.told}
        try:
            allow["series"] = self.series_counts()[:2]
        except Exception:
            pass
        w = self.words_for(min(secs, 10 if not in_break else 14))
        text = ask("Fill a quiet moment on the radio with one short, fresh Bobism about " + topic +
                   ", or about any other real detail in the facts that is more interesting right now (an earlier at-bat, the batted ball, "
                   "the pitcher's velocity or pitch mix, the weather, the crowd, the umpire, a bit of Brewers history). "
                   "Only use names and numbers from the facts. Say it a new way; do not repeat anything you said tonight.",
                   fj, self.mem, "", words=w, volume=not in_break, allow=allow)
        if not text:
            return None
        return self.say(None, None, None, "story", secs, text=text, pause=0.6)

    def championship(self):
        """True when tonight's final makes the Brewers World Series champions (fourth win)."""
        try:
            mil, opp, _ = self.series_counts(include_tonight=False)
            side = "home" if "Brewers" in self.box.team["home"] or self.box.ab.get("home") == "MIL" else "away"
            other = "away" if side == "home" else "home"
            return mil == 3 and self.box.score[side] > self.box.score[other]
        except Exception:
            return False

    def champions_call(self):
        """The moment: an all-out excited call, then Bob's speech, word for word."""
        side = "home" if "Brewers" in self.box.team["home"] else "away"
        other = "away" if side == "home" else "home"
        mil_r, opp_r = self.box.score[side], self.box.score[other]
        from num2words import num2words
        score = f"{num2words(mil_r)} to {num2words(opp_r)}"
        self.say(None, None, None, "final", 30, hot="good", pause=0.4, text=(
            "And that's the final out. | "
            "THE BREWERS HAVE WON THE WORLD SERIES! THE MILWAUKEE BREWERS ARE CHAMPIONS OF THE WORLD! "
            "Oh my goodness, they did it! They did it! Fifty-seven years of waiting, and the Brewers are on top of the baseball world! "
            f"Final score, {score}, and a four game sweep! Pile on, boys, pile on! Milwaukee, this one is yours!"))
        speech = [
            "Folks, give me a second here, because I am not sure I have the words. And for a guy who has never once run out of words, that is saying something.",
            "I have been around this game a long time. I have sat on more benches than a park ranger, I have seen the lean years and the near misses, "
            "and I always believed, always, that one day I would get to tell you this. The Milwaukee Brewers are world champions. "
            "To be the one who gets to say it, on the air, to all of you, is the proudest moment of my life.",
            "And it is yours, too. To every fan who sat through a cold April night at County Stadium, who stayed for the sausage race when we were down by ten, "
            "who kept a radio on the porch all summer long. To everybody who ever tuned in, wrote in, or just hollered along with me, thank you. "
            "You have been the best company a man could ask for, all these years.",
            "And a special thank you to Josh Stutting, whose wizardry put this old voice back on the air for one more run. Josh, you did a heck of a job, kid.",
            "Now, I have to be honest with you folks. The big fella upstairs only gave me a day pass for this one. "
            "He said, Bob, go call the World Series, and then come right back, and do not touch the sausages. "
            "So in a minute I have to head back up through the pearly gates. But I will tell you what, I am walking back in there with the biggest grin in all of heaven.",
            "Hug somebody tonight, Milwaukee. Ring a bell. Wake the neighbors. The Brewers are champions of the world. "
            "For the last time from this booth, this has been Bob. Good night, everybody. Celebrate, Milwaukee, celebrate!",
        ]
        for part in speech:
            self.say(None, None, None, "final", 30, text=part, pause=1.2)

    def postgame(self, plays):
        self.chunk = "postgame"
        if self.championship():
            self.champions_call()
            return
        last = plays[-1]["result"].get("description", "") if plays else ""
        mil_side = next((k for k in ("away", "home") if self.box.team.get(k) == "Brewers" or self.box.ab.get(k) == "MIL"), None)
        try:
            before, opp_before, _ = self.series_counts(include_tonight=False)
        except Exception:
            before, opp_before = 0, 0
        won = bool(mil_side) and self.box.score[mil_side] > self.box.score["home" if mil_side == "away" else "away"]
        mil_after, opp_after = before + won, opp_before + (not won)
        clinch = won and mil_after >= 4
        if clinch:
            series = (f"The Brewers have WON THE WORLD SERIES tonight, {mil_after} games to {opp_after}, the first championship in Brewers history.")
        else:
            series = (f"Series after tonight: Brewers {mil_after} wins, Rays {opp_after} wins." +
                      (" The Rays have not won a single game in this series." if opp_after == 0 else ""))
        facts = (f"Final: {self.box.sc()}. Hits: {self.box.team['away']} {self.box.hits['away']}, {self.box.team['home']} {self.box.hits['home']}. "
                 f"Last play: {last}\n{series}")
        if clinch:
            self.say(None, None, None, "final", 30, hot="good", text=CLINCH_CALL, pause=0.3)
            self.say("The Brewers just won the World Series. Give the final score and one or two lines on who made the difference "
                     "tonight, based only on the facts. Do not sign off yet.", facts,
                     f"Final score, {self.box.sc()}.", "final", 20)
            self.say(None, None, None, "final", 90, text=CLINCH_SPEECH, pause=1.2)
            return
        self.say("The game is over. Give the final score, one line on who made the difference based on the facts, where the series stands now "
                 "exactly as the facts say, and sign off warmly as Bob.", facts,
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
            if not d.box.started:                          # first pitch running late: keep the pregame going
                expect = max(expect, idle + 45)
            remaining = expect - idle
            ready_in = voice.rtf * 8 + 1.5                       # a fact is read word for word: only voicing time
            if cast.backlog() < ready_in and remaining - cast.backlog() > 7 and idle > 2.5:
                d.filler(min(remaining - cast.backlog() - 2, 9))
            time.sleep(0.4)
        except Exception:
            traceback.print_exc(); time.sleep(3)
    cast.status = "Final. Thanks for listening."
    time.sleep(max(0, cast.air_end - time.time()) + 2)
    cast.recorder.close()
    cast.on = False
    log("broadcast over; replay saved in", cast.recorder.root)


def render_deadline():
    """Rendering must stop 85 minutes before the next first pitch, so tonight's live run can start on time."""
    nxt = [ts(x["date"]) for x in series_games() if x["state"] == "Preview" and ts(x["date"]) > time.time()]
    return (min(nxt) - 85 * 60) if nxt else float("inf")


def run_render(game, voice, innings=None, folder="replays", deadline=float("inf")):
    """Re-call a finished game from MLB data, sized to the real gaps between pitches, and save it as replay files."""
    clock = {"t": 0.0}
    cast = Broadcast(sim=lambda: clock["t"])
    g = feed(game["pk"])
    root = os.path.join(WORK, folder, f"game-{game['num']}")
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
        if time.time() > deadline:
            log(f"stopping Game {game['num']} early so the live broadcast can start on time; this replay stays unfinished")
            cast.recorder._flush() if cast.recorder.cur else None
            return None
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
        if 9 < gap:
            d.filler(min(gap - 3, 9))
            clock["t"] = max(clock["t"], cast.air_end)
    clock["t"] = cast.air_end
    d.postgame([e["p"] for e in evs])
    cast.recorder.close()
    log(f"rendered Game {game['num']} to {root}: {cast.recorder.meta.get('duration', 0) / 60:.1f} minutes")
    return root


# ================================================================ the studio server
# Who is listening: each page sends an anonymous random id with its polls (nothing personal), and p=1 while it plays live.
AUDIENCE = {"seen": {}, "playing": {}, "visitors": set(), "listeners": set(), "peak": 0, "logged": 0}


def audience(lid="", playing=False):
    now = time.time()
    a = AUDIENCE
    if lid:
        a["seen"][lid] = now; a["visitors"].add(lid)
        if playing:
            a["playing"][lid] = now; a["listeners"].add(lid)
    n = sum(1 for t in a["playing"].values() if t > now - 30)
    a["peak"] = max(a["peak"], n)
    if now - a["logged"] > 60:
        a["logged"] = now
        log(f"audience: {n} listening now, peak {a['peak']}, {len(a['listeners'])} listeners and {len(a['visitors'])} visitors in all")
    return n


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
        snap = CAST.snapshot(int(request.args.get("since", 0) or 0))
        snap["listening"] = audience(request.args.get("l", "")[:40], request.args.get("p") == "1")
        return jsonify(snap)

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
    if MODE != "live":       # tests only check that the tunnel answers; they never post to the public signpost
        for _ in range(6):
            try:
                with urllib.request.urlopen(url + "/live", timeout=15) as r:
                    log(f"tunnel check: {url} answers ({r.status})")
                    return
            except Exception as e:
                log("tunnel check: not answering yet:", str(e)[:100]); time.sleep(10)
        log("tunnel check FAILED: listeners could not reach this studio")
        return
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
    serve()                         # live: the page finds the studio (and says it's warming up) while the voices load
    voice = Voice()
    voice.choose()
    global LOCAL
    if os.environ.get("BOB_LOCAL_LLM", "on") != "off":
        try:
            import torch
            LOCAL = LocalWriter(f"cuda:{max(0, torch.cuda.device_count() - 1)}" if torch.cuda.device_count() else "cpu")
            t0 = time.time(); LOCAL("Say hello in five words.", max_new=20)
            log(f"local writer ready: {LOCAL.name}, {time.time() - t0:.1f}s for a short line")
        except Exception as e:
            log("local writer unavailable, Gemini only:", str(e)[:200]); LOCAL = None
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
        run_render(g1, voice, innings=int(os.environ.get("BOB_INNINGS", "1")), folder="replays-test")
    else:
        gs = series_games()
        todo = [x for x in gs if str(x["pk"]) in RENDER or str(x["num"]) in RENDER] or [x for x in gs if x["state"] == "Final"]
        deadline = render_deadline()
        for gm in todo:
            if time.time() + 45 * 60 > deadline:
                log(f"not enough time to render Game {gm['num']} before the next broadcast; skipping it for now")
                continue
            run_render(gm, voice, deadline=deadline)
    log(f"done. Gemini requests {WRITE.count} ({WRITE.lat:.1f}s each), local model lines {LOCAL.count if LOCAL else 0} "
        f"({LOCAL.lat if LOCAL else 0:.1f}s each), voice rtf {voice.rtf:.2f}, words/s {voice.wps:.2f}")


if __name__ == "__main__":
    main()
