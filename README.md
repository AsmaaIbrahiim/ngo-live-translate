# Live Translate — EN / AR / DE

Three-column live transcript + translation display for a single speaker,
built on the Gemini Live API (`gemini-3.5-live-translate-preview`).

Mic audio is streamed to one Gemini session per **visible** language, each set
to translate into that language and echo it back as text if the speaker is
already speaking it. Each session fills one column. No audio is played back —
only text.

## Setup

```bash
cd ~/ngo-live-translate
source venv/bin/activate
```

The venv is already created and dependencies installed. Your API key is in
`.env` (gitignored).

## Run

```bash
cd ~/ngo-live-translate && ./venv/bin/uvicorn server:app --host 127.0.0.1 --port 8000
```

Then open **`http://localhost:8000`**.

> Use `localhost`, not `0.0.0.0` and not a LAN IP. Microphone access requires
> a secure origin; `localhost` and `127.0.0.1` qualify, other addresses do
> not. This is the most common reason the mic silently fails.

Collapse the languages you don't need, click **ابدأ الاستماع**, allow mic
access, drag the window to the external display, then press fullscreen.

## Controls

| Control | What it does |
|---|---|
| ابدأ الاستماع / إيقاف | Start / stop listening |
| ‹‹ on a column header | Collapse that language to a rail; click the rail to bring it back |
| ⛶ | Fullscreen — use on the external display |
| A− / A+ | Transcript text size, saved between sessions |
| 🗑 | Clear all three columns |
| ☾ / ☀ | Light / dark theme, saved between sessions |

Light theme projects better on most beamers; dark suits a large LED screen in
a dim room. Set the font size and collapse state from the back row before you
start — both are saved.

## Live now, cleaned up a moment later

The live model emits a few words at a time and — measured against this model —
**never sends `turn_complete` while audio keeps flowing**, not even across a
2.6 s pause. Painting only that stream is what made a slow speaker read as
literal rubble: `Okay. Uh, Today, we're going to talk … which is that … we
need to see … the situation … of refugees`.

So the column runs two layers:

1. **Live layer.** Every fragment is displayed the instant it arrives, about
   1.5 s behind the speaker, in muted grey with a blinking caret. This is the
   raw feed, hesitations and all, and it is visibly provisional.
2. **Clean layer.** In parallel the server gathers those fragments into a
   sentence, and hands it to a fast text model with the speaker's own words
   for that stretch and the last three published lines as context. The rewrite
   comes back in ~0.6 s and **crossfades over the grey text in place**: it
   arrives in the column's accent colour, then cools into body text. If the
   stretch was only filler it fades away instead.

Nothing settled ever rewrites itself twice, and the audience never waits for
text — they read the live words, and watch them resolve.

The same stretch, before and after:

> **Live layer** — Okay. Uh, Today, we're going to talk about an important
> topic which is that we need to see the situation of refugees in this region.
> … We rolled up our sleeves and we said we have to do something on the ground
> of reality.
>
> **Clean layer** — Today we are going to talk about a very important topic:
> we need to look at the situation of refugees in this region. … We rolled up
> our sleeves and said we must do something on the ground.

Arabic settles as Modern Standard Arabic rather than the colloquial the live
model echoes, because the clean-up pass is told to.

### The rewrite model

Measured over this exact prompt, 8 runs each:

| Model | median | max | Notes |
|---|---|---|---|
| **`gemini-3.1-flash-lite`** (minimal thinking) | **0.61 s** | 0.74 s | **In use.** Gets the idioms and MSA register right |
| `gemini-flash-lite-latest` | 0.68 s | 0.73 s | As quick, but stays literal (`auf dem Boden der Realität`) |
| `gemini-3.6-flash` | 1.27 s | 1.40 s | Same quality, twice the latency |
| `gemini-3.7-flash` | 1.62 s | 4.56 s | Newest but the slowest and least predictable here |

Newer is not faster: 3.7 was the worst of the four for this job. A rewrite call
runs once per sentence per open column, so its latency is what decides how
quickly grey text turns black.

### When a sentence is cut

A sentence is cut where the model lands a full stop (past 30 characters).
Failing that, so the rewrite never trails far behind the live words:

| Condition | Default |
|---|---|
| Speaker pauses, with enough gathered | 3 s / 45 chars — `SEG_PAUSE_S` |
| Sentence still running — cut at a comma | 5 s / 55 chars — `SEG_SOFT_S` |
| Still running — cut at any pause | 13 s — `SEG_HARD_S` |
| Runaway sentence | 300 chars |
| Speaker has simply stopped | 6 s of silence |

All of these can be set in `.env`, along with `GEMINI_REFINE_MODEL`,
`GEMINI_REFINE_THINKING` (`minimal` / `low` / `high`), and `REFINE=0` to turn
the clean layer off entirely and show only the raw live feed.

Lower `SEG_PAUSE_S` and `SEG_SOFT_S` for rewrites that land sooner in shorter
pieces; raise them for longer, more complete sentences.

## Collapsing a language

A collapsed column is not just hidden — its language is dropped from the
session list and its Gemini session is torn down, so **nothing is billed for a
language nobody is reading**. Collapsing and re-opening works mid-talk: the
server starts and stops that language's session as you go, and the column
keeps the text it already had.

Showing only Arabic while an Arabic speaker talks gives a cleaned-up live
transcript rather than a translation — `echo_target_language` handles that, so
one open column is always either translating or transcribing.

At least one column always stays open.

## How the display behaves

- **Grey text with a caret** is the live feed — still being said, not final.
  **Accent-coloured text** has just been rewritten and cools into body text
  over about two seconds, so the audience can see where the speaker is.
- **The view follows the newest text** automatically. If you scroll up to
  re-read something it stops following, and resumes when you scroll back to
  the bottom.
- **The transcript is capped at 300 lines per column** so a long talk can't
  slow the page down. Older text scrolls away permanently — this is a live
  display, not a record.

## Running for two hours

Gemini caps audio-only sessions at ~15 minutes and the socket at ~10
minutes. The server handles this: each language keeps a session-resumption
handle and silently reconnects when its session ends, carrying conversation
state across. The audience sees continuous text; the reconnect is invisible.

Verified: a session resumed from its handle continued mid-sentence rather
than restarting.

If reconnects ever fail more than 40 times in a row, the column reports the
error in the status bar instead of dying quietly.

## Cost

Roughly **$11–13 for two hours** with all three columns open:

| | Rate | 2 hrs × 3 sessions |
|---|---|---|
| Audio in | $0.0053/min | ~$1.90 |
| Audio out | $0.0315/min | ~$9–11 |

Most of that is audio output that is generated and thrown away — the model
only emits speech, and we use its transcript. Collapse a column and its share
of that disappears; two languages cost about two thirds, one about a third.

The clean-up pass adds well under $1 over two hours (a few hundred short
`gemini-3.1-flash-lite` calls per language), so it is noise next to the live
audio.

## Before the event

- Test all three languages with the real mic, in the real room.
- Check mic pickup distance and latency with your actual setup.
- Watch how long grey text stays grey at your speaker's pace, and lower
  `SEG_PAUSE_S` / `SEG_SOFT_S` if it lingers.
- Test on the venue network — outbound WSS must be allowed.
- Let it run 15+ minutes once, to watch a real session reconnect happen.
- Collapse the languages you won't use before you press start.
- Add the avatar at `static/dr-khaled.png` (a stand-in shows until you do).

## Fallback

If the preview model becomes unavailable, set
`GEMINI_LIVE_MODEL=gemini-3.1-flash-live-preview` in `.env`. That path needs
a code change in `server.py` — a single session with
`response_modalities=["TEXT"]` and a system instruction, instead of one
translation session per visible language.
