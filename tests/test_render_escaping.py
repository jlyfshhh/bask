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
