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
<style>html,body{height:100%;margin:0;overflow:hidden;background:#313131}body{display:flex;flex-direction:column}#bar{display:flex;align-items:center;gap:8px;box-sizing:border-box;flex:none;height:36px;padding:4px 8px}#address{flex:1;min-width:0;box-sizing:border-box;height:28px;margin:0;padding:0 8px;border:1px solid #555;border-radius:4px;background:#252525;color:#eee;font:13px system-ui,sans-serif}#address-error{flex:none;max-width:50%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#ff8a80;font:12px system-ui,sans-serif}#stage{flex:1;min-height:0;display:flex;align-items:center;justify-content:center}img{display:block;cursor:crosshair}img[hidden]{display:none}#input{position:fixed;left:0;top:0;width:1px;height:1px;opacity:0;border:0;padding:0;margin:0;resize:none;overflow:hidden}</style>
<div id="bar"><input id="address" type="text" inputmode="url" aria-label="Address" aria-describedby="address-error" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false"><span id="address-error" aria-live="polite"></span></div>
<div id="stage"><img id="screen" tabindex="0" alt="Arkham browser viewport" hidden></div>
<textarea id="input" tabindex="-1" aria-label="Browser keyboard input" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false"></textarea>
<script src="browser.js" defer></script></html>"""

SCRIPT = r"""const screen = document.getElementById('screen');
const input = document.getElementById('input');
const address = document.getElementById('address');
const addressError = document.getElementById('address-error');
const stage = document.getElementById('stage');
const KEYS = new Set(["Enter", "Tab", "Shift+Tab", "Backspace", "Delete", "Home", "End",
  "PageUp", "PageDown", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"]);
let imageURL, ended = false, busy = false;
let committed = null, addressEdited = false, addressFocused = false, addressChain = Promise.resolve();
const pending = [];
function fit() {
  // Scale the viewport to the largest size that fits the stage below the address field, keeping its aspect ratio.
  if (!screen.naturalWidth || !screen.naturalHeight) return;
  const scale = Math.min(stage.clientWidth / screen.naturalWidth, stage.clientHeight / screen.naturalHeight);
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
      if (reply.status === 404 || reply.status === 410) end();
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
const ADDRESS_LIMIT = 2048;
const NAVIGATE_ERRORS = {409: 'Browser starting.', 422: 'Address not allowed.', 502: 'Browser unavailable.'};
function end() {
  ended = true;
  pending.length = 0;
  input.value = '';
  input.blur();
  showError('Browser closed.');
}
function showError(message) { addressError.textContent = message; }
function normalizeAddress(raw) {
  // Accepts http and https URLs and bare hostnames (sent as https). Credentials and other schemes are refused.
  const text = raw.trim();
  if (!text || text.length > ADDRESS_LIMIT || /[\s\p{Cc}]/u.test(text)) throw new Error('Enter a valid web address.');
  const explicit = /^[a-z][a-z0-9+.-]*:\/\//i.test(text);
  if (!explicit && /^[a-z][a-z0-9+.-]*:(?!\d)/i.test(text)) throw new Error('Only http and https addresses are allowed.');
  if (!explicit && text.startsWith('/')) throw new Error('Enter a valid web address.');
  let url;
  try { url = new URL(explicit ? text : 'https://' + text); } catch { throw new Error('Enter a valid web address.'); }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') throw new Error('Only http and https addresses are allowed.');
  if (url.username || url.password) throw new Error('Addresses with credentials are not allowed.');
  return url.href;
}
function applyCommitted() {
  // The server's committed URL fills the field only while the user is neither focused on it nor has unsent edits.
  if (committed === null || addressFocused || addressEdited) return;
  address.value = committed;
}
async function syncLocation() {
  if (ended) return;
  try {
    const reply = await fetch('location', {cache:'no-store'});
    if (reply.status === 404 || reply.status === 410) { end(); return; }
    if (!reply.ok) return;
    const data = await reply.json();
    if (typeof data.url === 'string' && data.url !== committed) {
      committed = data.url;
      applyCommitted();
    }
  } catch { /* the next poll retries */ }
}
async function trackLocation() {
  await syncLocation();
  if (!ended) setTimeout(trackLocation, 1000);
}
async function navigate(text, url) {
  if (ended) return;
  try {
    const reply = await fetch('navigate', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({url})});
    if (reply.ok) {
      showError('');
      if (address.value === text) { address.value = url; addressEdited = false; }
      return;
    }
    if (reply.status === 404 || reply.status === 410) { end(); return; }
    showError(NAVIGATE_ERRORS[reply.status] || 'Address could not be opened.');
  } catch { showError('Browser unavailable.'); }
}
function submitAddress() {
  if (ended) { showError('Browser closed.'); return; }
  const text = address.value;
  let url;
  try { url = normalizeAddress(text); } catch (error) { showError(error.message); return; }
  // Navigations run one at a time, in the order they were submitted.
  addressChain = addressChain.then(() => navigate(text, url));
}
address.addEventListener('focus', () => { addressFocused = true; });
address.addEventListener('blur', () => { addressFocused = false; applyCommitted(); });
address.addEventListener('input', () => { addressEdited = address.value !== committed; showError(''); });
address.addEventListener('keydown', event => {
  if (event.key !== 'Enter' || event.isComposing || event.keyCode === 229) return;
  event.preventDefault();
  submitAddress();
});
refresh();
trackLocation();"""

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
