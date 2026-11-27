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
<style>html,body{height:100%;margin:0;overflow:hidden;background:#313131}body{display:flex;align-items:center;justify-content:center}img{display:block;cursor:crosshair}img[hidden]{display:none}#input{position:fixed;left:0;top:0;width:1px;height:1px;opacity:0;border:0;padding:0;margin:0;resize:none;overflow:hidden}</style>
<img id="screen" tabindex="0" alt="Arkham browser viewport" hidden>
<textarea id="input" tabindex="-1" aria-label="Browser keyboard input" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false"></textarea>
<script src="browser.js" defer></script></html>"""

SCRIPT = r"""const screen = document.getElementById('screen');
const input = document.getElementById('input');
const KEYS = new Set(["Enter", "Tab", "Shift+Tab", "Backspace", "Delete", "Home", "End",
  "PageUp", "PageDown", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"]);
let imageURL, ended = false, busy = false;
const pending = [];
function fit() {
  // Scale the viewport to the largest size that fits the page, keeping its aspect ratio.
  if (!screen.naturalWidth || !screen.naturalHeight) return;
  const scale = Math.min(document.documentElement.clientWidth / screen.naturalWidth,
    document.documentElement.clientHeight / screen.naturalHeight);
  screen.style.width = Math.floor(screen.naturalWidth * scale) + 'px';
  screen.style.height = Math.floor(screen.naturalHeight * scale) + 'px';
}
screen.addEventListener('load', fit);
window.addEventListener('resize', fit);
async function refresh() {
  try {
    const reply = await fetch('screen.png', {cache:'no-store'});
    if (reply.ok) {
      const url = URL.createObjectURL(await reply.blob());
      screen.src = url;
      screen.hidden = false;
      if (imageURL) URL.revokeObjectURL(imageURL);
      imageURL = url;
      fit();
    } else {
      ended = reply.status === 404 || reply.status === 410;
      if (ended) { pending.length = 0; input.value = ''; input.blur(); }
      screen.hidden = true;
    }
  } catch { screen.hidden = true; }
  if (!ended) setTimeout(refresh, 1000);
}
async function drain() {
  if (busy) return;
  busy = true;
  while (!ended && pending.length) {
    try {
      const reply = await fetch('input', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify(pending.shift())});
      if (!reply.ok) break;
    } catch { break; }
  }
  pending.length = 0;
  busy = false;
}
function queue(items) {
  if (ended || screen.hidden || pending.length + items.length > 64) return false;
  pending.push(...items);
  drain();
  return true;
}
screen.addEventListener('click', event => {
  const box = screen.getBoundingClientRect();
  if (!screen.naturalWidth || !box.width || !box.height) return;
  input.focus({preventScroll:true});
  queue([{kind:'click', x:Math.floor((event.clientX-box.left)*screen.naturalWidth/box.width),
    y:Math.floor((event.clientY-box.top)*screen.naturalHeight/box.height)}]);
});
screen.addEventListener('wheel', event => {
  event.preventDefault();
  const scale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 400 : 1;
  const clamp = n => Math.max(-1200, Math.min(1200, Math.round(n*scale)));
  queue([{kind:'wheel', delta_x:clamp(event.deltaX), delta_y:clamp(event.deltaY)}]);
}, {passive:false});
input.addEventListener('keydown', event => {
  if (event.metaKey || event.ctrlKey || event.altKey || event.isComposing || event.keyCode === 229) return;
  if (event.key === 'Escape') { input.blur(); return; }
  const key = event.shiftKey && event.key === 'Tab' ? 'Shift+Tab' : event.key;
  if (!KEYS.has(key)) return;
  event.preventDefault();
  queue([{kind:'key', key}]);
});
function clean(value) { return value.replace(/[\p{Cc}\p{Cs}]/gu, ''); }
function chunks(value) {
  const parts = [];
  let part = '';
  for (const char of value) {
    if (part.length + char.length > 1024) { parts.push(part); part = ''; }
    part += char;
  }
  if (part) parts.push(part);
  return parts;
}
function typeText(value) {
  const parts = chunks(clean(value));
  if (parts.length) queue(parts.map(text => ({kind:'text', text})));
}
function flushInput() {
  const value = input.value;
  input.value = '';
  typeText(value);
}
input.addEventListener('input', event => { if (!event.isComposing) flushInput(); });
input.addEventListener('compositionend', flushInput);
input.addEventListener('paste', event => {
  event.preventDefault();
  typeText(event.clipboardData ? event.clipboardData.getData('text/plain') : '');
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
