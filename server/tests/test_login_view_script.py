import json
import shutil
import subprocess

import pytest

from tokenwatch import managed_login_view


HARNESS = r"""
const {script} = JSON.parse(require('fs').readFileSync(0, 'utf8'));
class El {
  constructor() { this.listeners = {}; this.value = ''; this.hidden = false; this.focused = false;
    this.textContent = ''; this.src = ''; this.naturalWidth = 200; this.naturalHeight = 100; this.style = {}; }
  addEventListener(type, fn) { (this.listeners[type] ??= []).push(fn); }
  emit(type, props = {}) {
    const event = {defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...props};
    for (const fn of this.listeners[type] || []) fn(event);
    return event;
  }
  focus() { if (!this.focused) { this.focused = true; this.emit('focus'); } }
  blur() { this.focused = false; }
  getBoundingClientRect() { return {left: 0, top: 0, width: 100, height: 50}; }
}
const settle = async () => { for (let i = 0; i < 30; i++) await new Promise(r => setImmediate(r)); };
let posts = [], screenStatus = 200;
async function open() {
  posts = []; screenStatus = 200;
  const elements = {screen: new El(), input: new El()};
  const document = {getElementById: id => elements[id],
    documentElement: {clientWidth: 800, clientHeight: 600}};
  const window = {addEventListener() {}};
  const fetch = async (url, options = {}) => {
    if (url === 'screen.png') return {ok: screenStatus === 200, status: screenStatus, blob: async () => ({})};
    posts.push(JSON.parse(options.body));
    return {ok: true, status: 204};
  };
  const URL = {createObjectURL: () => 'blob:x', revokeObjectURL() {}};
  const api = new Function('document', 'fetch', 'URL', 'setTimeout', 'window',
    script + '\nreturn {refresh};')(document, fetch, URL, () => 0, window);
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
    assert "<button" not in html and "<input" not in html
    assert "<p" not in html and 'id="status"' not in html and "role=\"status\"" not in html


def test_login_view_is_full_viewport_with_noVNC_background_and_no_toolbar():
    html = managed_login_view.HTML
    assert "background:#313131" in html and "height:100%" in html and "overflow:hidden" in html
    assert "toolbar" not in html.lower() and "control" not in html.lower()
    assert "no-store" in managed_login_view.HEADERS["Cache-Control"]


def test_login_view_fits_viewport_proportionally():
    out = _run_script()
    # 200x100 image into an 800x600 viewport: scale = min(4, 6) = 4, so 800x400 and centred by flexbox.
    assert out["fitted"] == {"width": "800px", "height": "400px"}


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
