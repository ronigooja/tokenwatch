import json

from fastapi import HTTPException, Request
from fastapi.responses import Response

from .browser.manager import ManagerError


HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
                               "connect-src 'self'; img-src blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
}

HTML = """<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Arkham browser</title>
<style>body{font:14px system-ui;margin:8px}img{display:block;max-width:100%;margin-top:8px;cursor:crosshair}img[hidden]{display:none}</style>
<p id="status" role="status">Opening browser…</p>
<label>Text to send <input id="text" type="password" autocomplete="off"></label>
<button id="send" type="button">Send text</button>
<p>Click the browser image to focus it. Use the text field to send typed or pasted text to the focused browser field.</p>
<img id="screen" tabindex="0" alt="Arkham browser viewport" hidden>
<script src="browser.js" defer></script></html>"""

SCRIPT = """const screen = document.getElementById('screen');
const status = document.getElementById('status');
const text = document.getElementById('text');
let imageURL, ended = false, busy = false;
const pending = [];
async function refresh() {
  try {
    const reply = await fetch('screen.png', {cache:'no-store'});
    if (reply.ok) {
      const url = URL.createObjectURL(await reply.blob());
      screen.src = url;
      screen.hidden = false;
      if (imageURL) URL.revokeObjectURL(imageURL);
      imageURL = url;
      status.textContent = 'Browser open';
    } else {
      ended = reply.status === 404 || reply.status === 410;
      if (ended) { pending.length = 0; text.value = ''; }
      screen.hidden = true;
      status.textContent = ended ? 'Browser closed' : 'Waiting for browser…';
    }
  } catch { screen.hidden = true; status.textContent = 'Browser unavailable'; }
  if (!ended) setTimeout(refresh, 1000);
}
async function drain() {
  if (busy) return;
  busy = true;
  while (!ended && pending.length) {
    try {
      const reply = await fetch('input', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify(pending.shift())});
      if (!reply.ok) { status.textContent = 'Input not accepted'; break; }
    } catch { status.textContent = 'Browser unavailable'; break; }
  }
  pending.length = 0;
  busy = false;
}
function send(data) {
  if (ended || screen.hidden || pending.length >= 64) return false;
  pending.push(data);
  drain();
  return true;
}
screen.addEventListener('click', event => {
  const box = screen.getBoundingClientRect();
  if (!screen.naturalWidth || !box.width || !box.height) return;
  screen.focus({preventScroll:true});
  send({kind:'click', x:Math.floor((event.clientX-box.left)*screen.naturalWidth/box.width),
    y:Math.floor((event.clientY-box.top)*screen.naturalHeight/box.height)});
});
screen.addEventListener('wheel', event => {
  event.preventDefault();
  const scale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 400 : 1;
  const clamp = n => Math.max(-1200, Math.min(1200, Math.round(n*scale)));
  send({kind:'wheel', delta_x:clamp(event.deltaX), delta_y:clamp(event.deltaY)});
}, {passive:false});
screen.addEventListener('keydown', event => {
  if (event.metaKey || event.ctrlKey || event.altKey || event.isComposing) return;
  if (event.key === 'Escape') { screen.blur(); return; }
  const key = event.shiftKey && event.key === 'Tab' ? 'Shift+Tab' : event.key;
  const data = key.length === 1 ? {kind:'text',text:key} : {kind:'key',key};
  event.preventDefault(); send(data);
});
function sendText() {
  if (text.value && send({kind:'text',text:text.value})) {
    text.value = ''; screen.focus();
  }
}
document.getElementById('send').addEventListener('click', sendText);
text.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.isComposing) { event.preventDefault(); sendText(); }
});
refresh();"""

KEYS = frozenset({"Enter", "Tab", "Shift+Tab", "Backspace", "Delete", "Home", "End",
                  "PageUp", "PageDown", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"})


def _valid_input(data):
    if not isinstance(data, dict):
        return False
    kind = data.get("kind")
    if kind == "click":
        return set(data) == {"kind", "x", "y"} and all(type(data[k]) is int and 0 <= data[k] < 16384 for k in ("x", "y"))
    if kind == "wheel":
        return set(data) == {"kind", "delta_x", "delta_y"} and all(
            type(data[k]) is int and abs(data[k]) <= 1200 for k in ("delta_x", "delta_y"))
    if kind == "key":
        return set(data) == {"kind", "key"} and isinstance(data["key"], str) and data["key"] in KEYS
    if kind == "text":
        return (set(data) == {"kind", "text"} and isinstance(data["text"], str) and 0 < len(data["text"]) <= 1024
                and all(ord(c) >= 32 and not 127 <= ord(c) <= 159 and not 0xd800 <= ord(c) <= 0xdfff for c in data["text"]))
    return False


def _check(logins, ticket):
    if not logins.manager_mode or not logins.valid(ticket):
        raise HTTPException(404, "Not found", headers=HEADERS)
    session = logins.current
    if session.state in ("closed", "failed"):
        raise HTTPException(410, "Browser closed", headers=HEADERS)
    return session


async def _operate(logins, ticket, data=None):
    session = _check(logins, ticket)
    if session.state != "open":
        raise HTTPException(409, "Browser starting", headers=HEADERS)
    try:
        result = await logins.upstream.login_view(session.account_id, data)
    except (ManagerError, TimeoutError):
        _check(logins, ticket)
        raise HTTPException(502, "Browser unavailable", headers=HEADERS) from None
    _check(logins, ticket)
    return result


async def asset(logins, ticket, name):
    _check(logins, ticket)
    if name == "browser.html":
        return Response(HTML, media_type="text/html", headers=HEADERS)
    if name == "browser.js":
        return Response(SCRIPT, media_type="text/javascript", headers=HEADERS)
    if name == "screen.png":
        return Response(await _operate(logins, ticket), media_type="image/png", headers=HEADERS)
    raise HTTPException(404, "Not found", headers=HEADERS)


def install(app):
    @app.post("/login-view/{ticket}/input", include_in_schema=False)
    async def browser_input(ticket: str, request: Request):
        logins = app.state.logins
        _check(logins, ticket)
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
            raise HTTPException(415, "Expected JSON", headers=HEADERS)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 16384:
                raise HTTPException(413, "Input too large", headers=HEADERS)
            body.extend(chunk)
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):
            raise HTTPException(422, "Invalid browser input", headers=HEADERS) from None
        if not _valid_input(data):
            raise HTTPException(422, "Invalid browser input", headers=HEADERS)
        async with logins.lock:
            await _operate(logins, ticket, data)
        return Response(status_code=204, headers=HEADERS)
