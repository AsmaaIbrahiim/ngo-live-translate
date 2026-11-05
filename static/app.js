// Stand-in portrait, used only until a real image is dropped at /static/dr-khaled.png
const FALLBACK_AVATAR =
  "data:image/svg+xml;utf8," +
  encodeURIComponent(`
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
      <rect width="100" height="100" fill="#0d1b2a"/>
      <path d="M43 50h14v16H43z" fill="#e3c1aa"/>
      <path d="M50 64 18 75q-7 3-7 25h78q0-22-7-25z" fill="#15171d"/>
      <path d="M50 64l-9 6 3 30h12l3-30z" fill="#f4f6fa"/>
      <path d="M50 64l-9 6 4 30h-9l-6-22zM50 64l9 6-4 30h9l6-22z" fill="#1d2029"/>
      <path d="M44 62l6 6 6-6-2-2-4 4-4-4z" fill="#fff"/>
      <path d="M50 70l-8-4v8zM50 70l8-4v8z" fill="#0a0b0f"/>
      <circle cx="50" cy="70" r="2.2" fill="#0a0b0f"/>
      <ellipse cx="50" cy="38" rx="16" ry="19" fill="#f0d5c1"/>
      <path d="M50 15c-13 0-19 9-19 22v13h5V36c5 3 10 1 14 1s10-1 14 1v14h5V37c0-13-6-22-19-22z" fill="#101116"/>
      <path d="M31 33c3-10 10-14 19-14s16 4 19 14c-4-6-11-8-19-8s-15 2-19 8z" fill="#101116"/>
      <ellipse cx="43" cy="40" rx="2.3" ry="3" fill="#1b1c24"/>
      <ellipse cx="57" cy="40" rx="2.3" ry="3" fill="#1b1c24"/>
    </svg>
  `);

document.querySelectorAll(".brand-avatar").forEach((img) => {
  img.addEventListener("error", () => {
    if (img.src !== FALLBACK_AVATAR) img.src = FALLBACK_AVATAR;
  });
});

/* ---------- boot sequence ----------
   Nothing shows until webfonts settle, so text never repaints in a fallback
   face first. The wait is capped in case the venue blocks the font CDN. */

const fontsSettled = document.fonts
  ? Promise.race([
      document.fonts.ready.catch(() => {}),
      new Promise((r) => setTimeout(r, 1500)),
    ])
  : Promise.resolve();

fontsSettled
  .then(() => {
    document.body.classList.add("revealed");
    return new Promise((r) => setTimeout(r, 1900));
  })
  .then(() => document.body.classList.add("booted"));

const LANGS = ["en", "ar", "de"];
const EMPTY_TEXT = {
  en: "Waiting for speech…",
  ar: "في انتظار الكلام…",
  de: "Warte auf Sprache…",
};
// Shown when a column is opened mid-talk: its session was never started, so
// there is nothing to display until listening is restarted.
const OFF_TEXT = {
  en: "Not running — stop and start again to include English.",
  ar: "غير مُفعّلة — أوقف الاستماع ثم ابدأ من جديد لتشغيل العربية.",
  de: "Nicht aktiv — Stopp und erneut Start drücken, um Deutsch aufzunehmen.",
};

// Cap the transcript so a two-hour talk can't grow the DOM without bound.
const MAX_LINES = 300;

const columns = {};
const wraps = {};
LANGS.forEach((l) => {
  columns[l] = document.getElementById("col-" + l);
  wraps[l] = document.getElementById("col-wrap-" + l);
});

const statusEl = document.getElementById("status");
const statusText = document.getElementById("statusText");
const toggleBtn = document.getElementById("toggleBtn");
const toggleLabel = document.getElementById("toggleLabel");
const barEls = Array.from(document.querySelectorAll("#bars i"));

const activeTimers = { en: null, ar: null, de: null };
const following = { en: true, ar: true, de: true };
const scrollQueued = { en: false, ar: false, de: false };
// One element per sentence, keyed by the server's sentence id.
const lines = { en: new Map(), ar: new Map(), de: new Map() };
const ready = new Set();
let running = [];

let ws = null;
let audioCtx = null;
let micStream = null;
let workletNode = null;
let analyser = null;
let meterRAF = null;
let listening = false;

/* ---------- theme (initial value applied pre-paint in index.html) ---------- */

document.getElementById("themeBtn").onclick = () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("lt-theme", next);
};

/* ---------- font size ---------- */

let fontRem = parseFloat(localStorage.getItem("lt-font")) || 2.1;
function applyFont() {
  fontRem = Math.min(4.2, Math.max(1.2, fontRem));
  document.documentElement.style.setProperty("--col-font", fontRem.toFixed(2) + "rem");
  localStorage.setItem("lt-font", fontRem);
  LANGS.forEach(autoScroll);
}
applyFont();
document.getElementById("fontUp").onclick = () => { fontRem += 0.2; applyFont(); };
document.getElementById("fontDown").onclick = () => { fontRem -= 0.2; applyFont(); };

/* ---------- collapsing a column ----------
   A collapsed column is not just hidden: its language is dropped from the
   session list, so Gemini is not billed for a feed nobody is reading. */

let collapsed = new Set();
try {
  const saved = JSON.parse(localStorage.getItem("lt-collapsed") || "[]");
  if (Array.isArray(saved)) collapsed = new Set(saved.filter((l) => LANGS.includes(l)));
} catch (e) {}
if (collapsed.size === LANGS.length) collapsed.delete(LANGS[0]);

function shown() {
  return LANGS.filter((l) => !collapsed.has(l));
}

function applyCollapsed() {
  LANGS.forEach((l) => {
    const open = !collapsed.has(l);
    wraps[l].classList.toggle("collapsed", !open);
    const btn = wraps[l].querySelector(".col-fold");
    btn.setAttribute("aria-expanded", String(open));
  });
  localStorage.setItem("lt-collapsed", JSON.stringify([...collapsed]));
  shown().forEach(autoScroll);
}

function setCollapsed(lang, value) {
  // Never let the last column close — there would be nothing left to read.
  if (value && shown().length === 1) return;
  if (value) collapsed.add(lang);
  else collapsed.delete(lang);
  applyCollapsed();

  if (listening) {
    send({ action: "langs", langs: shown() });
    if (value) {
      running = running.filter((l) => l !== lang);
      ready.delete(lang);
    } else if (!running.includes(lang)) {
      running.push(lang);
    }
  }
  // Whatever the column already holds stays. Only the placeholder changes: a
  // column that was skipped at start now has a session of its own.
  if (!value) {
    const empty = columns[lang].querySelector(".col-empty");
    if (empty) empty.textContent = EMPTY_TEXT[lang];
  }
}

LANGS.forEach((l) => {
  const btn = wraps[l].querySelector(".col-fold");
  btn.onclick = (e) => {
    e.stopPropagation();
    setCollapsed(l, !collapsed.has(l));
  };
  // The whole rail is a target once collapsed — it is a small button otherwise.
  wraps[l].querySelector(".col-head").onclick = () => {
    if (collapsed.has(l)) setCollapsed(l, false);
  };
});
applyCollapsed();

/* ---------- status ---------- */

function setStatus(text, state) {
  statusText.textContent = text;
  statusEl.classList.toggle("is-live", state === "live");
  statusEl.classList.toggle("is-err", state === "err");
}

/* ---------- scrolling ----------
   The view follows the newest text, but stops following the moment the user
   scrolls up to re-read something, and resumes when they return to the end. */

LANGS.forEach((l) => {
  columns[l].addEventListener("scroll", () => {
    const el = columns[l];
    following[l] = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  });
});

function autoScroll(lang) {
  if (!following[lang] || scrollQueued[lang]) return;
  scrollQueued[lang] = true;
  requestAnimationFrame(() => {
    scrollQueued[lang] = false;
    const el = columns[lang];
    el.scrollTop = el.scrollHeight;
  });
}

/* ---------- transcript rendering ----------
   Two layers. Raw fragments land the moment they arrive, gathered into one
   provisional line, so the column keeps the speaker's edge. When the server
   has re-translated that sentence in context, the clean version crossfades
   over the fragments in place. */

function resetColumn(lang, message) {
  columns[lang].innerHTML = "";
  lines[lang].clear();
  following[lang] = true;
  const empty = document.createElement("div");
  empty.className = "col-empty";
  empty.textContent = message;
  columns[lang].appendChild(empty);
}

function markActive(lang) {
  wraps[lang].classList.add("active");
  clearTimeout(activeTimers[lang]);
  activeTimers[lang] = setTimeout(() => wraps[lang].classList.remove("active"), 1400);
}

function trimColumn(lang) {
  const col = columns[lang];
  while (col.childElementCount > MAX_LINES) {
    const first = col.firstElementChild;
    lines[lang].delete(Number(first.dataset.seg));
    first.remove();
  }
}

function slot(lang, seg) {
  let el = lines[lang].get(seg);
  if (el) return el;
  const empty = columns[lang].querySelector(".col-empty");
  if (empty) empty.remove();
  el = document.createElement("p");
  el.className = "line line--draft";
  el.dataset.seg = String(seg);
  el.appendChild(document.createElement("span")).className = "caret";
  columns[lang].appendChild(el);
  lines[lang].set(seg, el);
  trimColumn(lang);
  return el;
}

function appendChunk(lang, seg, text) {
  const el = slot(lang, seg);
  if (!el.classList.contains("line--draft")) return; // already rewritten
  const span = document.createElement("span");
  span.className = "chunk";
  span.textContent = text;
  el.insertBefore(span, el.lastElementChild);
  autoScroll(lang);
  markActive(lang);
}

function settleLine(lang, seg, text) {
  const el = slot(lang, seg);
  el.className = "line line--in";
  el.textContent = text;
  trimColumn(lang);
  autoScroll(lang);
  markActive(lang);
}

function dropLine(lang, seg) {
  const el = lines[lang].get(seg);
  if (!el) return;
  lines[lang].delete(seg);
  // The fragments were filler; let them fade rather than blink out.
  el.className = "line line--out";
  setTimeout(() => {
    el.remove();
    if (!columns[lang].childElementCount) resetColumn(lang, EMPTY_TEXT[lang]);
  }, 320);
}

document.getElementById("clearBtn").onclick = () =>
  LANGS.forEach((l) => resetColumn(l, EMPTY_TEXT[l]));

/* ---------- websocket ---------- */

function send(payload) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(payload));
}

function connectWS() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.binaryType = "arraybuffer";

  ws.onopen = () => setStatus("جاهز · اضغط ابدأ", null);
  ws.onclose = () => {
    setStatus("انقطع الاتصال", "err");
    if (listening) stop();
  };
  ws.onerror = () => setStatus("خطأ في الاتصال", "err");

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);

    switch (msg.type) {
      case "error":
        setStatus(msg.error, "err");
        if (listening) stop();
        return;

      case "ready":
        ready.add(msg.lang);
        // A resume is a silent hand-off mid-talk; don't flash "preparing".
        if (msg.resumed) return;
        if (ready.size >= running.length) setStatus("يستمع الآن…", "live");
        else setStatus(`جارٍ تجهيز الجلسات… ${ready.size}/${running.length}`, null);
        return;

      case "chunk":
        if (columns[msg.lang]) appendChunk(msg.lang, msg.seg, msg.text);
        return;

      case "line":
        if (columns[msg.lang]) settleLine(msg.lang, msg.seg, msg.text);
        return;

      case "drop":
        if (columns[msg.lang]) dropLine(msg.lang, msg.seg);
        return;

      default:
        return;
    }
  };
}

/* ---------- mic level bars ---------- */

function startMeter(stream) {
  const source = audioCtx.createMediaStreamSource(stream);
  analyser = audioCtx.createAnalyser();
  analyser.fftSize = 256;
  analyser.smoothingTimeConstant = 0.75;
  source.connect(analyser);

  const data = new Uint8Array(analyser.frequencyBinCount);
  const n = barEls.length;

  function tick() {
    analyser.getByteFrequencyData(data);
    const per = Math.floor(data.length / n);
    for (let i = 0; i < n; i++) {
      let sum = 0;
      for (let j = 0; j < per; j++) sum += data[i * per + j];
      const avg = sum / per / 255;
      const h = Math.max(4, Math.round(avg * 26));
      barEls[i].style.height = h + "px";
      barEls[i].style.background = h > 5 ? "var(--emerald-2)" : "var(--line)";
    }
    meterRAF = requestAnimationFrame(tick);
  }
  tick();
}

function stopMeter() {
  if (meterRAF) cancelAnimationFrame(meterRAF);
  meterRAF = null;
  barEls.forEach((b) => {
    b.style.height = "4px";
    b.style.background = "var(--line)";
  });
}

/* ---------- start / stop ---------- */

const MIC_ERRORS = {
  NotAllowedError: "تم رفض إذن الميكروفون — اسمح به من إعدادات الموقع",
  NotFoundError: "لا يوجد ميكروفون متصل",
  NotReadableError: "الميكروفون مستخدم من تطبيق آخر",
  SecurityError: "سياق غير آمن — افتح الصفحة عبر http://localhost:8000",
  AbortError: "تعذّر بدء الميكروفون",
};

async function start() {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    setStatus("لم يتم الاتصال بعد…", "err");
    return;
  }

  // getUserMedia only exists on a secure origin. localhost and 127.0.0.1
  // qualify; 0.0.0.0 and LAN IPs do not, which is the usual cause here.
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    setStatus("افتح الصفحة عبر http://localhost:8000 وليس عنوان IP", "err");
    console.error("[mic] insecure context or no mediaDevices:", location.origin);
    return;
  }

  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
  } catch (e) {
    setStatus(MIC_ERRORS[e.name] || `${e.name}: ${e.message}`, "err");
    console.error("[mic]", e.name, e.message);
    return;
  }

  audioCtx = new AudioContext();
  await audioCtx.audioWorklet.addModule("/static/pcm-worklet.js");

  const source = audioCtx.createMediaStreamSource(micStream);
  workletNode = new AudioWorkletNode(audioCtx, "pcm-processor");

  workletNode.port.onmessage = (e) => {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(e.data);
  };

  source.connect(workletNode);
  workletNode.connect(audioCtx.destination);
  startMeter(micStream);

  ready.clear();
  running = shown();
  LANGS.forEach((l) =>
    resetColumn(l, running.includes(l) ? EMPTY_TEXT[l] : OFF_TEXT[l])
  );
  send({ action: "start", langs: running });

  listening = true;
  toggleBtn.classList.remove("pill--start");
  toggleBtn.classList.add("pill--stop");
  toggleLabel.textContent = "إيقاف";
  setStatus("جارٍ تجهيز الجلسات…", null);
}

function stop() {
  send({ action: "stop" });
  ready.clear();

  if (micStream) {
    micStream.getTracks().forEach((t) => t.stop());
    micStream = null;
  }
  if (audioCtx) {
    audioCtx.close();
    audioCtx = null;
  }
  stopMeter();
  // A sentence still being gathered will never be rewritten now; leave the
  // words on screen but stop them looking live.
  LANGS.forEach((l) => {
    lines[l].forEach((el) => el.classList.remove("line--draft"));
  });

  listening = false;
  toggleBtn.classList.remove("pill--stop");
  toggleBtn.classList.add("pill--start");
  toggleLabel.textContent = "ابدأ الاستماع";
  setStatus("متوقف", null);
}

toggleBtn.onclick = () => (listening ? stop() : start());

document.getElementById("fullscreenBtn").onclick = () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen().catch(() => {});
};

connectWS();
