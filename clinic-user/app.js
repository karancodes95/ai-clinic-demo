// Bluebell patient assistant — chat UI logic, adapted for the ai_integration
// backend: creates a per-patient /session (vertical=healthcare) then streams
// the answer from /chat over SSE.

// ---- Config -------------------------------------------------------------
// BASE_URL: where ai_integration_python runs. API_KEY must match the backend's
// API_KEY in .env. This is the low-value bot-gate key (not a real secret);
// for a public deploy, proxy the call instead of shipping any key to the page.
const BASE_URL = "https://demo-api.bytcra.com/clinic";  // hosted backend (GitHub Pages serves this page cross-origin)
const API_KEY = "7ea2b12e2dc0289f77373ac47cd34d7998ec13ec977de8e624491b1374d6488b";
const VERTICAL = "healthcare";
// -------------------------------------------------------------------------

const msgs = document.getElementById("chat-msgs");
const input = document.getElementById("chat-text");
const sendBtn = document.getElementById("chat-send");
let sessionId = null;

// Minimal, injection-safe Markdown: escape HTML first, then a small subset.
function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function renderMarkdown(text) {
  let s = escapeHtml(text);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");  // **bold**
  s = s.replace(/(^|\n)\s*[-*]\s+/g, "$1• ");                // "- item" -> "• item"
  s = s.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");            // *italic*
  return s;                                                   // newlines kept; .msg has white-space: pre-wrap
}

function addMsg(text, who) {
  const el = document.createElement("div");
  el.className = "msg " + who;
  el.innerHTML = renderMarkdown(text);
  msgs.appendChild(el);
  msgs.scrollTop = msgs.scrollHeight;
  return el;
}

function setBusy(busy) {
  input.disabled = busy;
  sendBtn.disabled = busy;
  if (!busy) input.focus();
}

// Reuse (or lazily create) this browser's patient session.
async function ensureSession() {
  if (sessionId) return sessionId;
  const res = await fetch(`${BASE_URL}/session?vertical=${VERTICAL}`, {
    method: "POST",
    headers: { "X-API-Key": API_KEY },
  });
  if (!res.ok) throw new Error(`session ${res.status}`);
  sessionId = (await res.json()).session_id;
  return sessionId;
}

// Parse one SSE record ("event: X\ndata: ...\ndata: ...") into {event, data}.
function parseSse(raw) {
  let event = "message";
  const dataLines = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
  }
  return { event, data: dataLines.join("\n") };
}

async function send() {
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  addMsg(text, "user");
  setBusy(true);
  const botEl = addMsg("…", "bot typing");
  let answer = "";
  try {
    const sid = await ensureSession();
    const res = await fetch(`${BASE_URL}/chat`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": API_KEY,
        "X-Session-Id": sid,
      },
      body: JSON.stringify({ question: text }),
    });

    if (res.status === 429) {
      await res.json().catch(() => ({}));
      botEl.className = "msg bot";
      botEl.innerHTML = renderMarkdown(
        "You've reached today's message limit for this demo. See more of my work at " +
        "https://karancodes95.github.io or connect on " +
        "https://www.linkedin.com/in/karanjtv/"
      );
      return;
    }
    if (!res.ok || !res.body) {
      botEl.className = "msg bot";
      botEl.textContent = "Sorry, something went wrong. Please try again.";
      return;
    }

    // Stream the SSE body, appending each answer token live.
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let sep;
      while ((sep = buffer.indexOf("\n\n")) !== -1) {
        const { event, data } = parseSse(buffer.slice(0, sep));
        buffer = buffer.slice(sep + 2);
        if (event === "status" && !answer) {
          // Live progress during the (sometimes slow) SQL step.
          const labels = {
            generating_sql: "Looking that up…",
            running_query: "Checking your records…",
            writing_answer: "Writing your answer…",
          };
          botEl.className = "msg bot typing";
          botEl.textContent = labels[data] || "Working…";
        } else if (event === "token") {
          answer += data;
          botEl.className = "msg bot";
          botEl.innerHTML = renderMarkdown(answer);
          msgs.scrollTop = msgs.scrollHeight;
        } else if (event === "error") {
          botEl.className = "msg bot";
          botEl.textContent = "Sorry, I hit an error. Please try again.";
        }
        // sql / rows / usage / done are not shown by this simple UI.
      }
    }
    if (!answer) botEl.textContent = "Sorry, I didn't catch that — try again?";
  } catch (e) {
    botEl.className = "msg bot";
    botEl.textContent = "Sorry, I couldn't reach the server. Please try again.";
  } finally {
    setBusy(false);
  }
}

sendBtn.addEventListener("click", send);
input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
input.focus();
