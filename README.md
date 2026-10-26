# Bob's Booth

Brewers World Series radio, called by Bob: an AI play-by-play announcer with a cloned voice, built as a tribute to Bob Uecker.

Permanent page: https://willardwonder.github.io/bobs-booth/

## How it works

- **`index.html`** is the front end, served by GitHub Pages. When Bob's studio is running it plays his broadcast; when it isn't, it shows the World Series schedule and a live scoreboard straight from MLB's public Stats API.
- **Bob's studio** runs in a Google Colab notebook on a free T4 GPU (`colab/`). It reads MLB pitch-by-pitch data, has Gemini (Colab's built-in `google.colab.ai`) write Bob's lines, and voices them with OmniVoice using two cloned voices (calm and excited).
- Each time the studio starts it gets a new Cloudflare quick-tunnel address and posts it to the ntfy.sh topic `bobs-booth-mke-4q7v2k`. The page reads the newest address from there, so the Pages link never changes.
- At game time the studio tunes itself to the live game (from 30 minutes before first pitch).

## Colab notebook cells (`colab/`)

| Cell | File | What it does |
| --- | --- | --- |
| 1 | `cell1_setup.py` | GPU check, installs OmniVoice + Flask, downloads cloudflared |
| 2 | `cell2_voices.py` | Loads OmniVoice, builds Bob's excited and calm voices from the uploaded samples, plays test lines |
| 3 | `cell3_booth.py` | The studio: commentary engine, Flask API, embedded page, tunnel, signpost |

`cell3_booth.py` is generated: edit `engine_source.py` or `index.html`, then run `python colab/build_cell3.py`.

Before running the notebook, upload Bob's voice files with Colab's Files panel: `bobnew_11m30-14m15.mp3` and `bob_calm_1.wav` to `bob_calm_5.wav` (kept privately on Josh's PC, not in this repo; `colab/make_calm_samples.py` recreates the calm files from `BobNew.mp3`).

Bob is not Bob Uecker, and these are not Uecker's words or voice. This fan project is not connected to MLB or the Milwaukee Brewers. All artwork is original.
