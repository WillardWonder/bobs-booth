# Bob's Booth

Brewers World Series radio, called by Bob: an AI play-by-play announcer with a cloned voice, built as a tribute to Bob Uecker.

## How it works

- **This page** (`index.html`) is the permanent front door, served by GitHub Pages.
- **Bob's studio** runs in a Google Colab notebook on a free GPU. It reads MLB's public pitch-by-pitch data, has Gemini write Bob's call, and voices it with OmniVoice.
- Each time the studio starts, it gets a new Cloudflare quick-tunnel address and posts it to the ntfy.sh topic `bobs-booth-mke-4q7v2k`. This page reads the newest address from there, so the Pages link never has to change.
- When the notebook isn't running, the page shows "Bob is off the air."

Bob is not Bob Uecker, and these are not Uecker's words or voice. This fan project is not connected to MLB or the Milwaukee Brewers. All artwork is original.
