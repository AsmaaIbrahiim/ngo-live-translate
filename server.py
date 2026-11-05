import asyncio
import contextlib
import json
import os
import time
from collections import deque
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

API_KEY = os.environ.get("GEMINI_API_KEY")
MODEL = os.environ.get("GEMINI_LIVE_MODEL", "gemini-3.5-live-translate-preview")

# Small, fast text model that re-translates each finished sentence in context.
# Measured over this prompt (8 runs, median / max): 3.1-flash-lite 0.61 / 0.74 s,
# flash-lite-latest 0.68 / 0.73 s, 3.6-flash 1.27 / 1.40 s, 3.7-flash 1.62 /
# 4.56 s. 3.1-flash-lite matches the big models on idiom and Arabic register at
# half their latency; flash-lite-latest is as quick but stays literal.
REFINE_MODEL = os.environ.get("GEMINI_REFINE_MODEL", "gemini-3.1-flash-lite")
REFINE_THINKING = os.environ.get("GEMINI_REFINE_THINKING", "minimal")
REFINE = os.environ.get("REFINE", "1").lower() not in ("0", "off", "false", "no")

LANGUAGES = ("en", "ar", "de")
LANG_NAMES = {"en": "English", "ar": "Arabic", "de": "German"}

# Gemini caps audio-only sessions at ~15 min and the socket at ~10 min, so a
# long talk depends on resuming rather than on any single session surviving.
MAX_RECONNECTS = 40
MAX_QUEUED_CHUNKS = 200  # ~20 s of audio; guards memory if a session is down

# --- how the live stream is cut into sentences -------------------------------
#
# Fragments go to the browser the moment they arrive, so the audience keeps the
# live edge. In parallel they are gathered into sentences here; each finished
# sentence is re-translated in context and sent back to replace the raw text in
# place. The live model emits a few words at a time and (measured against this
# model) never sends turn_complete while audio keeps flowing — not even across
# 2.6 s pauses — so the cuts below are what decides when a rewrite lands.
SENTENCE_END = ".!?…۔؟！？。"
CLAUSE_END = SENTENCE_END + ",;:،؛"

# All times in seconds. A sentence is normally cut where the model lands a full
# stop; everything else below just stops a rewrite from lagging the live text.
SEG_MIN_CHARS = 30  # below this a full stop is an abbreviation, not an ending
SEG_PAUSE_CHARS = 45  # enough to be worth cleaning up when the speaker breathes
SEG_SOFT_CHARS = 55  # enough to break a sentence that keeps running
SEG_MAX_CHARS = 300  # hard cut for a speaker who never lands a full stop
SEG_PAUSE_S = float(os.environ.get("SEG_PAUSE_S", "3"))  # a breath: clean up now
SEG_SOFT_S = float(os.environ.get("SEG_SOFT_S", "5"))  # then settle for a comma
SEG_HARD_S = float(os.environ.get("SEG_HARD_S", "13"))  # then take any pause
SEG_FLUSH_S = 6.0  # nothing more is coming for this one
SEG_IDLE_S = 1.0  # what counts as a pause in the outgoing text
SEG_TICK = 0.2

REFINE_TIMEOUT = 8.0
REFINE_CONTEXT = 3  # settled sentences shown to the refiner as context
REFINE_BACKLOG = 2  # queued sentences before we start letting drafts stand
HEARD_CAP = 700  # chars of the speaker's own words kept as rolling context

REFINE_SYSTEM = """You are the {language} feed of a live talk, projected on a \
screen for the audience.

You are given one stretch of speech at a time:
- DRAFT is the raw real-time machine output for it. It arrives a few words at a \
time, so it is full of hesitations, false starts, repeated words and literal \
word-for-word phrasing — especially when the speaker talks slowly. DRAFT alone \
defines how much of the talk you cover.
- HEARD is a rolling window of the speaker's own recent words, in their own \
language. It is only loosely aligned: it may start before DRAFT and may run past \
the end of it. Use it to work out what DRAFT was trying to say — never render \
the parts of it that DRAFT does not cover.
- PREVIOUS is the {language} text already on screen just before this.

Write what the speaker meant, as clean, cohesive {language} sentences.

Rules:
- Convey the meaning, not the word order. Repair literalisms, restore idioms, \
and join fragments into whole grammatical sentences.
- Delete every filler, hesitation, stutter, false start and self-correction \
("uh", "erm", "you know", "I mean", repeated words). Keep only the finished thought.
- Cover exactly the span DRAFT covers: add nothing, drop no real content, never \
summarise, never comment, never answer the speaker.
- Continue on from PREVIOUS without repeating any of it, and without re-opening \
a sentence PREVIOUS already finished.
- If DRAFT is already in {language}, the speaker is speaking it: you are \
cleaning up a live transcript rather than translating, so keep their words and \
only remove the disfluencies.
- Keep the speaker's register. When the output is Arabic, write Modern Standard \
Arabic, not colloquial.
- Keep names, numbers and technical terms exactly as they are.
- If there is no real content in DRAFT — only filler, a stray word, or noise — \
output nothing at all.
- Output ONLY the finished text: no labels, no quotes, no notes."""

SAFETY_OFF = [
    types.SafetySetting(category=c, threshold=types.HarmBlockThreshold.OFF)
    for c in (
        types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
    )
]

app = FastAPI()
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/")
async def index():
    return FileResponse(str(BASE_DIR / "static" / "index.html"))


async def _race(*coros):
    """Run coroutines until the first finishes, then cancel the rest."""
    tasks = [asyncio.create_task(c) for c in coros]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            exc = t.exception()
            if exc is not None:
                raise exc
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _drop_backlog(queue: asyncio.Queue):
    """Discard audio buffered while a session was down, keeping the stop flag."""
    stop_seen = False
    while True:
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if item is None:
            stop_seen = True
    if stop_seen:
        queue.put_nowait(None)


def _command(text: str) -> dict:
    """Accept both {"action": ...} and the bare "start"/"stop" strings."""
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {"action": text}
    return parsed if isinstance(parsed, dict) else {"action": text}


def _wanted(command: dict) -> list[str]:
    """The languages a display is actually showing, in a fixed order."""
    asked = command.get("langs")
    if not isinstance(asked, list):
        return list(LANGUAGES)
    return [c for c in LANGUAGES if c in asked]


class _Out:
    """Serialises the sends of every session sharing one browser socket."""

    def __init__(self, ws: WebSocket):
        self._ws = ws
        self._lock = asyncio.Lock()

    async def send(self, payload: dict):
        async with self._lock:
            await self._ws.send_json(payload)


class _Ids:
    """Hands out sentence ids for one language.

    It outlives the session, so re-opening a collapsed column continues the
    numbering instead of restarting at 1 and overwriting lines already read.
    """

    def __init__(self):
        self.n = 0

    def next(self) -> int:
        self.n += 1
        return self.n


class _Sentences:
    """Collects the live fragments of one language into whole sentences.

    Every fragment is forwarded as it arrives, so the column keeps up with the
    speaker. When the sentence closes it goes to the refiner, whose clean
    rewrite replaces those fragments in place.
    """

    def __init__(self, code: str, out: _Out, inbox: asyncio.Queue, ids: _Ids):
        self.code = code
        self.out = out
        self.inbox = inbox
        self.ids = ids
        self.n = 0
        self.open = False
        self.parts: list[str] = []
        self.size = 0
        self.heard: deque[str] = deque()
        self.opened = 0.0
        self.touched = 0.0

    async def chunk(self, text: str):
        now = time.monotonic()
        if not self.open:
            self.n = self.ids.next()
            self.open = True
            self.opened = now
            self.parts.clear()
            self.size = 0
        self.parts.append(text)
        self.size += len(text)
        self.touched = now
        await self.out.send(
            {"type": "chunk", "lang": self.code, "seg": self.n, "text": text}
        )
        if self._cut(now, text):
            await self.close()

    def source(self, text: str):
        # A rolling window rather than a per-sentence buffer: the speaker's
        # transcript and the translation of it do not arrive on the same
        # boundaries, so pinning one to the other would mismatch them. The
        # refiner is told this window is only loosely aligned.
        self.heard.append(text)
        while len(self.heard) > 1 and sum(len(p) for p in self.heard) > HEARD_CAP:
            self.heard.popleft()

    def _cut(self, now: float, tail: str) -> bool:
        if self.size == 0:
            return False
        tail = tail.rstrip()
        age = now - self.opened
        idle = now - self.touched

        if self.size >= SEG_MAX_CHARS:
            return True
        if self.size >= SEG_MIN_CHARS and tail.endswith(tuple(SENTENCE_END)):
            return True
        # A sentence that keeps running has to be broken somewhere, or its
        # rewrite would trail far behind the live words. Prefer a comma; after
        # even longer, take any pause.
        if age >= SEG_SOFT_S and self.size >= SEG_SOFT_CHARS:
            if tail.endswith(tuple(CLAUSE_END)):
                return True
            if age >= SEG_HARD_S and idle >= SEG_IDLE_S:
                return True
        # The speaker drew breath and there is enough to work with: clean it up
        # now rather than making the audience read raw words any longer.
        if idle >= SEG_PAUSE_S and self.size >= SEG_PAUSE_CHARS:
            return True
        return idle >= SEG_FLUSH_S

    async def close(self):
        if not self.open:
            return
        self.open = False
        draft = "".join(self.parts).strip()
        heard = "".join(self.heard).strip()
        self.parts.clear()
        self.size = 0
        if not draft:
            await self.out.send({"type": "drop", "lang": self.code, "seg": self.n})
            return
        self.inbox.put_nowait({"id": self.n, "heard": heard, "draft": draft})

    async def tick(self):
        if self.open and self._cut(time.monotonic(), ""):
            await self.close()


async def _refine_once(client: genai.Client, code: str, seg: dict, prev: deque) -> str:
    parts = []
    if prev:
        parts.append("PREVIOUS (already on screen, do not repeat):\n" + " ".join(prev))
    if seg["heard"]:
        parts.append(
            "HEARD (rolling window of the speaker's own words, loosely aligned):\n"
            + seg["heard"]
        )
    parts.append("DRAFT (raw live output to clean up):\n" + seg["draft"])

    response = await client.aio.models.generate_content(
        model=REFINE_MODEL,
        contents="\n\n".join(parts),
        config=types.GenerateContentConfig(
            system_instruction=REFINE_SYSTEM.format(language=LANG_NAMES[code]),
            temperature=0.2,
            max_output_tokens=1000,
            thinking_config=types.ThinkingConfig(thinking_level=REFINE_THINKING),
            safety_settings=SAFETY_OFF,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        ),
    )
    return (response.text or "").strip()


async def _refiner(code: str, client: genai.Client, out: _Out, inbox: asyncio.Queue):
    """Turn each gathered sentence into clean text, in the order it was spoken."""
    prev: deque[str] = deque(maxlen=REFINE_CONTEXT)

    while True:
        seg = await inbox.get()

        # Never fall behind the speaker: if sentences have piled up, publish the
        # older drafts as they are and spend the call on the newest one.
        while inbox.qsize() > REFINE_BACKLOG:
            await _publish(out, code, seg["id"], seg["draft"])
            prev.append(seg["draft"])
            seg = inbox.get_nowait()

        text = ""
        if REFINE:
            try:
                text = await asyncio.wait_for(
                    _refine_once(client, code, seg, prev), REFINE_TIMEOUT
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[{code}] refine failed ({type(e).__name__}: {e})", flush=True)
                text = seg["draft"]  # a rough line beats a blank column
        else:
            text = seg["draft"]

        await _publish(out, code, seg["id"], text)
        if text:
            prev.append(text)


async def _publish(out: _Out, code: str, seg_id: int, text: str):
    """Replace a placeholder with its finished line, or take it away."""
    kind = "line" if text else "drop"
    with contextlib.suppress(Exception):
        await out.send({"type": kind, "lang": code, "seg": seg_id, "text": text})


async def _run_session(
    code: str,
    client: genai.Client,
    out: _Out,
    queue: asyncio.Queue,
    stopping: asyncio.Event,
    seg: _Sentences,
):
    """Keep one language translating, reconnecting across Gemini's session cap.

    The resumption handle carries conversation state to the next socket, so the
    audience sees continuous text rather than a restart every ~10 minutes.
    """
    handle = None
    attempt = 0

    while not stopping.is_set():
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            # The speaker's own words, used as context when cleaning up the
            # translation. Free with the session we are already paying for.
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            translation_config=types.TranslationConfig(
                target_language_code=code,
                echo_target_language=True,
            ),
            session_resumption=types.SessionResumptionConfig(handle=handle),
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow()
            ),
        )

        try:
            async with client.aio.live.connect(model=MODEL, config=config) as session:
                resumed = handle is not None
                attempt = 0
                await out.send({"type": "ready", "lang": code, "resumed": resumed})

                async def sender():
                    while True:
                        chunk = await queue.get()
                        if chunk is None:
                            stopping.set()
                            return
                        await session.send_realtime_input(
                            audio=types.Blob(
                                data=chunk, mime_type="audio/pcm;rate=16000"
                            )
                        )

                async def ticker():
                    while True:
                        await asyncio.sleep(SEG_TICK)
                        await seg.tick()

                async def receiver():
                    nonlocal handle
                    async for response in session.receive():
                        update = response.session_resumption_update
                        if update is not None and update.resumable and update.new_handle:
                            handle = update.new_handle

                        if response.go_away is not None:
                            print(
                                f"[{code}] go_away in {response.go_away.time_left}",
                                flush=True,
                            )

                        sc = response.server_content
                        if sc is None:
                            continue
                        heard = getattr(sc, "input_transcription", None)
                        if heard and heard.text:
                            seg.source(heard.text)
                        tr = getattr(sc, "output_transcription", None)
                        if tr and tr.text:
                            await seg.chunk(tr.text)
                        if getattr(sc, "turn_complete", False):
                            await seg.close()

                await _race(sender(), receiver(), ticker())

        except (WebSocketDisconnect, asyncio.CancelledError):
            raise
        except Exception as e:
            if stopping.is_set():
                return
            attempt += 1
            print(
                f"[{code}] reconnect {attempt} after {type(e).__name__}: {e}",
                flush=True,
            )
            if attempt > MAX_RECONNECTS:
                with contextlib.suppress(Exception):
                    await out.send(
                        {
                            "type": "error",
                            "error": f"[{code}] reconnect failed: {type(e).__name__}",
                        }
                    )
                raise
            await asyncio.sleep(min(0.25 * 2**attempt, 4.0))

        # Whatever was mid-sentence when the socket dropped will never be
        # continued, so publish it rather than gluing it to the next utterance.
        with contextlib.suppress(Exception):
            await seg.close()

        if stopping.is_set():
            return

        # Audio that piled up during the gap is stale; sending it would put the
        # column permanently behind the speaker.
        _drop_backlog(queue)


async def _language(
    code: str,
    client: genai.Client,
    out: _Out,
    queue: asyncio.Queue,
    stopping: asyncio.Event,
    ids: _Ids,
):
    """One column: its Gemini session plus the refiner that cleans its output."""
    inbox: asyncio.Queue = asyncio.Queue()
    seg = _Sentences(code, out, inbox, ids)
    await _race(
        _run_session(code, client, out, queue, stopping, seg),
        _refiner(code, client, out, inbox),
    )


async def _listen(ws: WebSocket, out: _Out, client: genai.Client, langs: list[str]):
    """Run a session per visible language, following the display as it changes."""
    stopping = asyncio.Event()
    queues: dict[str, asyncio.Queue] = {}
    tasks: dict[str, asyncio.Task] = {}
    ids = {code: _Ids() for code in LANGUAGES}
    broken: asyncio.Future = asyncio.get_running_loop().create_future()

    def enable(code: str):
        if code in tasks or stopping.is_set():
            return
        queue: asyncio.Queue = asyncio.Queue()
        queues[code] = queue
        task = asyncio.create_task(
            _language(code, client, out, queue, stopping, ids[code])
        )
        tasks[code] = task

        def finished(t: asyncio.Task):
            if tasks.get(code) is not t:
                return  # already replaced by a later enable()
            tasks.pop(code, None)
            queues.pop(code, None)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None and not broken.done():
                broken.set_exception(exc)

        task.add_done_callback(finished)

    def disable(code: str):
        # A collapsed column costs nothing: its session is torn down, so the
        # API is not charged for a language nobody is reading.
        queues.pop(code, None)
        task = tasks.pop(code, None)
        if task is not None:
            task.cancel()

    async def pump():
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    raise WebSocketDisconnect()
                data = message.get("bytes")
                if data is not None:
                    for q in list(queues.values()):
                        if q.qsize() < MAX_QUEUED_CHUNKS:
                            q.put_nowait(data)
                    continue
                text = message.get("text")
                if not text:
                    continue
                command = _command(text)
                action = command.get("action")
                if action == "stop":
                    return
                if action == "langs":
                    show = set(_wanted(command))
                    for code in list(tasks):
                        if code not in show:
                            disable(code)
                    for code in show:
                        enable(code)
                    if not tasks:
                        return  # nothing left to listen for
        finally:
            stopping.set()
            for q in list(queues.values()):
                q.put_nowait(None)

    async def watch():
        await broken

    for code in langs:
        enable(code)

    try:
        await _race(pump(), watch())
    finally:
        stopping.set()
        pending = list(tasks.values())
        for code in list(tasks):
            disable(code)
        await asyncio.gather(*pending, return_exceptions=True)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    out = _Out(ws)

    if not API_KEY or API_KEY.startswith("your_"):
        await out.send(
            {
                "type": "error",
                "error": "GEMINI_API_KEY is not set in .env on the server.",
            }
        )
        await ws.close()
        return

    client = genai.Client(api_key=API_KEY)

    # Sessions open on "start" so they cannot time out while the page waits.
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            if not text:
                continue
            command = _command(text)
            if command.get("action") != "start":
                continue

            langs = _wanted(command) or list(LANGUAGES)

            try:
                await _listen(ws, out, client, langs)
                await out.send({"type": "stopped"})
            except WebSocketDisconnect:
                return
            except Exception as e:
                print(f"[session error] {type(e).__name__}: {e}", flush=True)
                with contextlib.suppress(Exception):
                    await out.send(
                        {"type": "error", "error": f"{type(e).__name__}: {str(e)[:300]}"}
                    )
    except WebSocketDisconnect:
        return
