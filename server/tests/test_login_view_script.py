import json
import shutil
import subprocess

import pytest

from tokenwatch import managed_login_view


HARNESS = r"""
const {script} = JSON.parse(require('fs').readFileSync(0, 'utf8'));
class El {
  constructor() { this.listeners = {}; this.value = ''; this.hidden = false; this.focused = false;
    this.textContent = ''; this.src = ''; this.naturalWidth = 200; this.naturalHeight = 100; this.style = {};
    this.clientWidth = 800; this.clientHeight = 600; }
  addEventListener(type, fn) { (this.listeners[type] ??= []).push(fn); }
  emit(type, props = {}) {
    const event = {defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...props};
    for (const fn of this.listeners[type] || []) fn(event);
    return event;
  }
  focus() { if (!this.focused) { this.focused = true; this.emit('focus'); } }
  blur() { if (this.focused) { this.focused = false; this.emit('blur'); } }
  getBoundingClientRect() { return {left: 0, top: 0, width: 100, height: 50}; }
}
const settle = async () => { for (let i = 0; i < 30; i++) await new Promise(r => setImmediate(r)); };
let timers = [];
const tick = async () => { const due = timers.splice(0); for (const fn of due) await fn(); await settle(); };
let posts = [], navigates = [], screenStatus = 200, navigateStatus = 204;
let locationStatus = 200, locationURL = 'https://arkm.com/';
async function open() {
  posts = []; navigates = []; screenStatus = 200; navigateStatus = 204; timers = [];
  const elements = {screen: new El(), input: new El(), address: new El(), addressError: new El(),
    stage: new El()};
  elements.stage.clientHeight = 300;
  const document = {getElementById: id => elements[id === 'address-error' ? 'addressError' : id],
    documentElement: {clientWidth: 800, clientHeight: 600}};
  const window = {addEventListener() {}};
  const fetch = async (url, options = {}) => {
    if (url === 'screen.png') return {ok: screenStatus === 200, status: screenStatus, blob: async () => ({})};
    if (url === 'location') return {ok: locationStatus === 200, status: locationStatus,
      json: async () => ({url: locationURL})};
    if (url === 'navigate') {
      navigates.push(JSON.parse(options.body));
      return {ok: navigateStatus < 400, status: navigateStatus};
    }
    posts.push(JSON.parse(options.body));
    return {ok: true, status: 204};
  };
  const URL = {createObjectURL: () => 'blob:x', revokeObjectURL() {}};
  const api = new Function('document', 'fetch', 'URL', 'setTimeout', 'window',
    script + '\nreturn {refresh};')(document, fetch, URL, fn => timers.push(fn), window);
  await settle();
  return {...elements, refresh: api.refresh};
}
(async () => {
  const out = {};
  let {screen, input} = await open();
  out.initialFocus = input.focused;
  screen.src = 'blob:x';
  screen.emit('load');
  out.fitted = {...screen.style};
  screen.emit('click', {clientX: 10, clientY: 5});
  await settle();
  out.clickFocus = input.focused;
  out.click = posts.splice(0);

  input.value = 'a'; input.emit('input', {isComposing: false}); await settle();
  out.typed = posts.splice(0); out.typedValue = input.value;

  input.value = 'に'; input.emit('input', {isComposing: true}); await settle();
  out.composing = posts.splice(0);
  input.emit('compositionend'); await settle();
  out.composed = posts.splice(0);
  input.emit('input', {isComposing: false}); await settle();
  out.afterComposition = posts.splice(0);

  const paste = input.emit('paste', {clipboardData: {getData: t => t === 'text/plain' ? 'hunter2\n' : ''}});
  await settle();
  out.pasteDefault = paste.defaultPrevented;
  out.paste = posts.splice(0);

  const keys = [
    ['Enter', {key: 'Enter'}], ['printable', {key: 'a'}], ['meta-v', {key: 'v', metaKey: true}],
    ['shift-tab', {key: 'Tab', shiftKey: true}], ['tab', {key: 'Tab'}], ['arrow', {key: 'ArrowDown'}],
    ['ime-enter', {key: 'Enter', isComposing: true}], ['ime-229', {key: 'Process', keyCode: 229}],
  ];
  out.keys = [];
  for (const [name, props] of keys) {
    const event = input.emit('keydown', props);
    out.keys.push([name, event.defaultPrevented]);
  }
  await settle();
  out.keyPosts = posts.splice(0);
  input.emit('keydown', {key: 'Escape'});
  out.escapeBlur = input.focused;

  const wheel = screen.emit('wheel', {deltaX: 0, deltaY: 500, deltaMode: 0});
  await settle();
  out.wheelDefault = wheel.defaultPrevented;
  out.wheel = posts.splice(0);

  const long = 'a'.repeat(1023) + '\u{1F600}' + 'b'.repeat(10);
  input.emit('paste', {clipboardData: {getData: () => long}}); await settle();
  out.longPaste = posts.splice(0);

  input.emit('paste', {clipboardData: {getData: () => 'x'.repeat(1024 * 65)}}); await settle();
  out.tooLargePosts = posts.splice(0).length;

  input.emit('paste', {clipboardData: {getData: () => 'a\u0007\u0085b\ud800c'}}); await settle();
  out.cleaned = posts.splice(0);

  const page = await open();
  screenStatus = 404;
  await page.refresh();
  await settle();
  page.input.value = 'after-end'; page.input.emit('input', {isComposing: false});
  await settle();
  out.afterEndPosts = posts.splice(0);
  out.afterEndValue = page.input.value;

  locationURL = 'https://arkm.com/start';
  const bar = await open();
  out.committedShown = bar.address.value;
  out.addressPosts = posts.splice(0);

  bar.address.focus();
  bar.address.value = 'draft'; bar.address.emit('input'); await settle();
  locationURL = 'https://arkm.com/moved';
  await tick();
  out.editedKept = bar.address.value;
  bar.address.blur();
  await tick();
  out.editedAfterBlur = bar.address.value;

  bar.address.value = 'arkm.com/login'; bar.address.emit('input');
  const enter = bar.address.emit('keydown', {key: 'Enter'});
  out.enterPrevented = enter.defaultPrevented;
  await settle();
  out.navigates = navigates.splice(0);
  out.afterNavigateValue = bar.address.value;
  out.navigatePosts = posts.splice(0);

  out.rejectMessages = [];
  for (const bad of ['javascript:alert(1)', 'file:///etc/passwd', 'https://user:pw@arkm.com/', 'http://',
      '/relative', 'a b', 'ftp://arkm.com/']) {
    bar.address.value = bad; bar.address.emit('keydown', {key: 'Enter'}); await settle();
    out.rejectMessages.push(bar.addressError.textContent);
  }
  out.rejectNavigates = navigates.splice(0);

  navigateStatus = 422;
  bar.address.value = 'https://arkm.com/x'; bar.address.emit('keydown', {key: 'Enter'}); await settle();
  out.serverError = bar.addressError.textContent;
  out.serverErrorValue = bar.address.value;
  navigateStatus = 204;

  bar.address.blur();
  locationURL = 'https://arkm.com/final';
  await tick();
  out.followedCommitted = bar.address.value;

  console.log(JSON.stringify(out));
})().catch(error => { console.error(error); process.exit(1); });
"""


def _run_script():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the browser script test")
    result = subprocess.run([node, "-e", HARNESS], input=json.dumps({"script": managed_login_view.SCRIPT}),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_login_view_has_no_visible_text_controls():
    html = managed_login_view.HTML
    assert "Text to send" not in html and "Send text" not in html and "Click the browser" not in html
    assert '<textarea id="input"' in html and 'type="password"' not in html
    assert "<button" not in html
    assert "<p" not in html and 'id="status"' not in html and "role=\"status\"" not in html


def test_login_view_has_single_address_field_above_viewport():
    html = managed_login_view.HTML
    assert html.count("<input") == 1 and '<input id="address" type="text"' in html
    assert 'aria-label="Address"' in html and 'aria-live="polite"' in html
    assert html.index('id="address"') < html.index('id="screen"')


def test_login_view_is_full_viewport_with_noVNC_background_and_no_toolbar():
    html = managed_login_view.HTML
    assert "background:#313131" in html and "height:100%" in html and "overflow:hidden" in html
    assert "toolbar" not in html.lower() and "control" not in html.lower()
    assert "no-store" in managed_login_view.HEADERS["Cache-Control"]


def test_login_view_fits_viewport_proportionally_below_address_bar():
    out = _run_script()
    # 200x100 image into an 800x300 stage (the viewport minus the address bar): scale = min(4, 3) = 3.
    assert out["fitted"] == {"width": "600px", "height": "300px"}


def test_login_view_direct_keyboard_mouse_and_paste_behaviour():
    out = _run_script()
    assert out["initialFocus"] is False
    assert out["clickFocus"] is True
    assert out["click"] == [{"kind": "click", "x": 20, "y": 10}]
    assert out["typed"] == [{"kind": "text", "text": "a"}] and out["typedValue"] == ""
    assert out["composing"] == [] and out["composed"] == [{"kind": "text", "text": "に"}]
    assert out["afterComposition"] == []
    assert out["pasteDefault"] is True and out["paste"] == [{"kind": "text", "text": "hunter2"}]
    assert out["keys"] == [["Enter", True], ["printable", False], ["meta-v", False], ["shift-tab", True],
                           ["tab", True], ["arrow", True], ["ime-enter", False], ["ime-229", False]]
    assert out["keyPosts"] == [{"kind": "key", "key": "Enter"}, {"kind": "key", "key": "Shift+Tab"},
                               {"kind": "key", "key": "Tab"}, {"kind": "key", "key": "ArrowDown"}]
    assert out["escapeBlur"] is False
    assert out["wheelDefault"] is True and out["wheel"] == [{"kind": "wheel", "delta_x": 0, "delta_y": 500}]


def test_login_view_splits_long_paste_in_order_and_refuses_oversize():
    out = _run_script()
    chunks = [item["text"] for item in out["longPaste"]]
    assert [len(chunk) for chunk in chunks] == [1023, 11]
    assert all(len(chunk) <= 1024 and "\ud800" not in chunk for chunk in chunks)
    assert "".join(chunks) == "a" * 1023 + "\U0001F600" + "b" * 10
    assert out["tooLargePosts"] == 0
    assert out["cleaned"] == [{"kind": "text", "text": "abc"}]


def test_login_view_sends_nothing_after_browser_ends():
    out = _run_script()
    assert out["afterEndPosts"] == [] and out["afterEndValue"] == ""


def test_login_view_address_shows_committed_url_and_follows_it_when_idle():
    out = _run_script()
    assert out["committedShown"] == "https://arkm.com/start"
    assert out["addressPosts"] == []
    assert out["followedCommitted"] == "https://arkm.com/final"


def test_login_view_address_keeps_unsent_edits_over_server_updates():
    out = _run_script()
    assert out["editedKept"] == "draft"
    assert out["editedAfterBlur"] == "draft"


def test_login_view_address_enter_posts_normalized_url_to_relative_navigate():
    out = _run_script()
    assert out["enterPrevented"] is True
    assert out["navigates"] == [{"url": "https://arkm.com/login"}]
    assert out["afterNavigateValue"] == "https://arkm.com/login"
    assert out["navigatePosts"] == []


def test_login_view_address_refuses_unsafe_or_invalid_input_without_posting():
    out = _run_script()
    assert out["rejectNavigates"] == []
    assert all(message for message in out["rejectMessages"])
    assert out["rejectMessages"][0] == "Only http and https addresses are allowed."
    assert out["rejectMessages"][1] == "Only http and https addresses are allowed."
    assert out["rejectMessages"][2] == "Addresses with credentials are not allowed."
    assert out["rejectMessages"][5] == "Enter a valid web address."
    assert out["rejectMessages"][6] == "Only http and https addresses are allowed."


def test_login_view_address_server_error_is_shown_and_keeps_draft():
    out = _run_script()
    assert out["serverError"] == "Address not allowed."
    assert out["serverErrorValue"] == "https://arkm.com/x"
