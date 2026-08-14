"""QC-09: hostile values from config or an integration must display as text.

frontend/app.js is plain browser JavaScript with no build step, so these tests
extract the three escaping helpers and exercise them directly, then assert that
no identifier still reaches an inline handler unescaped.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
APP_JS = ROOT / "frontend" / "app.js"
INDEX_HTML = ROOT / "frontend" / "index.html"
SERVICE_WORKER = ROOT / "frontend" / "sw.js"

HOSTILE = [
    "');alert(document.cookie);//",
    '"><script>alert(1)</script>',
    "&#39;);alert(1);//",
    "</script><img src=x onerror=alert(1)>",
    "\\'; fetch('//evil'); //",
]


def extract(name: str) -> str:
    """Pull one top-level function out of app.js by brace matching."""
    text = APP_JS.read_text(encoding="utf-8")
    start = text.index(f"function {name}(")
    depth, index = 0, start
    while index < len(text):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
        index += 1
    raise AssertionError(f"could not extract {name}")


def run_node(script: str) -> str:
    node = shutil.which("node")
    if not node:
        print("SKIP: node is not available for the renderer tests")
        raise SystemExit(0)
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise AssertionError(result.stderr.strip())
    return result.stdout.strip()


def test_identifiers_cannot_break_out_of_an_inline_handler():
    script = f"""
    {extract('idAttr')}
    const out = {json.dumps(HOSTILE)}.map(idAttr);
    console.log(JSON.stringify(out));
    """
    results = json.loads(run_node(script))
    for original, cleaned in zip(HOSTILE, results):
        for forbidden in ["'", '"', "<", ">", "(", ")", ";", "/", "\\\\", "&", " "]:
            assert forbidden not in cleaned, f"{forbidden!r} survived idAttr({original!r}) -> {cleaned!r}"


def test_readings_that_are_not_numbers_never_reach_the_page():
    script = f"""
    {extract('num')}
    const inputs = [{json.dumps("<script>alert(1)</script>")}, "not-a-number", null, undefined,
                    NaN, Infinity, -Infinity, {{}}, [], "88", 88, 21.5];
    console.log(JSON.stringify(inputs.map(v => num(v))));
    """
    results = json.loads(run_node(script))
    for value in results:
        assert value == "—" or re.fullmatch(r"-?\d+(\.\d+)?", value), f"num() produced {value!r}"
    # A genuine number still renders, and a numeric string is normalised.
    assert "88" in results and "21.5" in results


def test_names_are_html_escaped():
    script = f"""
    {extract('esc')}
    console.log(JSON.stringify({json.dumps(HOSTILE)}.map(esc)));
    """
    for cleaned in json.loads(run_node(script)):
        for forbidden in ["<", ">", '"', "'"]:
            assert forbidden not in cleaned, f"{forbidden!r} survived esc(): {cleaned!r}"


def test_no_identifier_reaches_an_inline_handler_unescaped():
    """The regression guard: a new handler added without idAttr fails here."""
    text = APP_JS.read_text(encoding="utf-8")
    offenders = []
    for match in re.finditer(r'on(?:click|change|input)="[^"]*"', text):
        fragment = match.group(0)
        for interpolation in re.finditer(r"\$\{([^}]*)\}", fragment):
            expression = interpolation.group(1)
            # A bare loop counter or a number is fine; anything else that is not
            # already run through idAttr is not.
            if re.fullmatch(r"[a-z]", expression.strip()):
                continue
            if "idAttr(" in expression:
                continue
            offenders.append(f"{fragment[:90]} -> ${{{expression}}}")
    assert not offenders, "unescaped identifiers in inline handlers:\n  " + "\n  ".join(offenders)


def test_alert_delivery_status_is_accessible_and_secret_free():
    hostile = '<img src=x onerror="alert(1)">'
    script = f"""
    {extract('esc')}
    {extract('alertStatusTime')}
    {extract('alertDeliveryMarkup')}
    const status = {{
      enabled: true, pending: 2, retrying: true,
      next_retry_at: 1770000100, last_success_at: null,
      last_error_at: 1770000000, last_error: {json.dumps(hostile)},
      topic: "PRIVATE-TOPIC-MUST-NOT-RENDER",
      server: "https://private.invalid"
    }};
    console.log(JSON.stringify({{
      markup: alertDeliveryMarkup(status),
      never: alertStatusTime(null)
    }}));
    """
    result = json.loads(run_node(script))
    markup = result["markup"]
    assert 'role="status"' in markup and 'aria-live="polite"' in markup
    assert "2</b> alerts are waiting to send and will retry automatically" in markup
    assert "PRIVATE-TOPIC-MUST-NOT-RENDER" not in markup
    assert "private.invalid" not in markup
    assert "<img" not in markup and "&lt;img" in markup
    assert result["never"] == "not yet" and "1970" not in markup


def test_enclosure_cards_are_native_keyboard_controls():
    text = APP_JS.read_text(encoding="utf-8")
    assert text.count('<button type="button" class="enc-card') == 2
    assert '<div class="enc-card' not in text
    assert 'aria-label="Open ${esc(' in text


def test_dialog_keyboard_decisions_wrap_and_close():
    script = f"""
    {extract('dialogKeyAction')}
    const cases = [
      ["Escape", false, 1, 3],
      ["Tab", false, 2, 3],
      ["Tab", true, 0, 3],
      ["Tab", false, -1, 0],
      ["ArrowDown", false, 1, 3]
    ];
    console.log(JSON.stringify(cases.map(args => dialogKeyAction(...args))));
    """
    assert json.loads(run_node(script)) == ["close", "first", "last", "dialog", "none"]


def test_dialogs_have_names_modal_state_and_inert_background_support():
    html = INDEX_HTML.read_text(encoding="utf-8")
    script = APP_JS.read_text(encoding="utf-8")
    for dialog_id in ("detail", "manage", "keeper", "editor", "pair"):
        declaration = re.search(rf'<div id="{dialog_id}"[^>]*>', html, re.DOTALL)
        assert declaration, f"missing #{dialog_id} dialog"
        tag = declaration.group(0)
        assert 'role="dialog"' in tag and 'aria-modal="true"' in tag
        assert ('aria-label="' in tag or 'aria-labelledby="' in tag), f"#{dialog_id} has no name"
        assert 'aria-hidden="true"' in tag and "inert" in tag
    assert html.count('id="detail-title"') == 0, "detail title is injected at runtime"
    assert script.count('id="detail-title"') == 2
    assert 'event.key, event.shiftKey' in script
    assert 'node.getClientRects().length > 0' in script
    assert 'node.inert = true' in script
    assert 'node.id === "toast"' in script
    assert '_dialogOpeners.get(dialog)' in script
    assert 'opener.focus({ preventScroll: true })' in script
    assert 'id="toast" class="toast" role="status" aria-live="polite"' in html


def test_viewport_keeps_browser_zoom_available_and_shell_cache_is_bumped():
    html = INDEX_HTML.read_text(encoding="utf-8")
    viewport = re.search(r'<meta name="viewport" content="([^"]+)">', html)
    assert viewport
    content = viewport.group(1)
    assert "maximum-scale" not in content
    assert "user-scalable=no" not in content
    asset_versions = re.findall(r'(?:style\.css|app\.js)\?v=([^"\']+)', html)
    assert len(asset_versions) == 2 and len(set(asset_versions)) == 1
    assert 'const CACHE = "bask-v6"' in SERVICE_WORKER.read_text(encoding="utf-8")


def test_every_icon_only_dialog_close_button_has_a_name():
    combined = INDEX_HTML.read_text(encoding="utf-8") + APP_JS.read_text(encoding="utf-8")
    close_buttons = re.findall(r'<button class="close-btn"[^>]*>', combined)
    assert close_buttons
    assert all('aria-label="' in button for button in close_buttons)


def main() -> None:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as exc:
                print(f"FAIL {name}: {exc}")
                failures += 1
    if failures:
        print(f"{failures} rendering test(s) failed")
        raise SystemExit(1)
    print("Render escaping tests passed")


if __name__ == "__main__":
    main()
