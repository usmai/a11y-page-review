"""Accessibility Page Review - Hugging Face Space.

Enter a URL. The app opens it in a headless browser, runs axe-core plus a few
extra checks (duplicate IDs, table captions, small text, ad blocks, image text
near edges is left to a human), and returns a plain-language, priority-ordered
list of what to fix.

Optional: set an HF_TOKEN secret in the Space to have an LLM polish the report
into friendlier prose. Without it, the rule-based report is shown as is.
"""

import os
import re
from collections import defaultdict

import gradio as gr
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Plain-language explanations for the axe rules seen most often.
# ---------------------------------------------------------------------------
PLAIN = {
    "button-name": (
        "Some buttons have no accessible name",
        "These buttons show only an icon, with no text and no aria-label, so a "
        "screen reader just says “button”.",
        "Give each one an aria-label that says what it does, for example "
        "“Open menu”, “Search” or “Close”.",
    ),
    "link-name": (
        "Some links have no accessible name",
        "These links have no text a screen reader can announce.",
        "Add visible link text, or an aria-label on icon-only links.",
    ),
    "image-alt": (
        "Images are missing alt text",
        "Screen-reader users get no description of these images.",
        "Add an alt attribute that says what the image shows, or alt=\"\" if "
        "it is purely decorative.",
    ),
    "color-contrast": (
        "Text has low colour contrast",
        "This text is too light against its background to read easily.",
        "Darken the text or lighten the background to reach at least 4.5:1 "
        "(3:1 for large text).",
    ),
    "label": (
        "Form fields have no label",
        "These inputs have no label a screen reader can announce.",
        "Add a visible <label for=…>, or at least an aria-label.",
    ),
    "landmark-unique": (
        "Landmarks share the same label",
        "Two or more landmarks of the same type have the same name, so users "
        "can’t tell them apart in a landmarks list.",
        "Give each one a distinct aria-label, for example “Main "
        "navigation” and “Mobile navigation”.",
    ),
    "duplicate-id-aria": (
        "Duplicate IDs used by ARIA",
        "An ID referenced by ARIA or a label appears more than once, so the "
        "reference may point to the wrong element.",
        "Make every ID unique.",
    ),
    "document-title": (
        "The page has no title",
        "The <title> is missing or empty, so users can’t identify the tab.",
        "Add a descriptive <title>.",
    ),
    "html-has-lang": (
        "The page language is not set",
        "Screen readers may use the wrong pronunciation.",
        "Add lang=\"en\" (or the right language) to the <html> element.",
    ),
    "heading-order": (
        "Heading levels skip",
        "Heading levels jump (for example h2 to h4), which makes the outline "
        "harder to follow.",
        "Use heading levels in order.",
    ),
    "region": (
        "Content sits outside landmarks",
        "Some content isn’t inside a landmark such as <main> or <nav>.",
        "Wrap page sections in the right landmark elements.",
    ),
    "aria-allowed-attr": (
        "Invalid ARIA attributes",
        "These elements use ARIA attributes their role doesn’t allow.",
        "Remove the attribute or change the role.",
    ),
    "list": (
        "Lists are built incorrectly",
        "A <ul> or <ol> contains something other than <li> elements.",
        "Keep only <li>, <script> or <template> directly inside lists.",
    ),
    "frame-title": (
        "Frames have no title",
        "Embedded frames (often ads or videos) have no title, so screen "
        "readers can’t say what they contain.",
        "Add a title attribute to each iframe.",
    ),
}

IMPACT_ORDER = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}

# ---------------------------------------------------------------------------
# Extra checks axe doesn't cover (or covers only as best practice).
# ---------------------------------------------------------------------------
EXTRA_JS = r"""
() => {
  const out = {};
  // Duplicate IDs
  const counts = {};
  document.querySelectorAll('[id]').forEach(el => {
    if (el.id) counts[el.id] = (counts[el.id] || 0) + 1;
  });
  out.duplicateIds = Object.entries(counts).filter(([, n]) => n > 1)
                       .map(([id, n]) => ({id, n}));

  // Tables without caption
  out.tablesNoCaption = [...document.querySelectorAll('table')]
    .filter(t => !t.querySelector('caption') && !t.getAttribute('aria-label')
                 && !t.getAttribute('aria-labelledby')
                 && t.offsetParent !== null).length;

  // Small visible text (under 14px), grouped by size
  const small = {};
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  while (walker.nextNode()) {
    const n = walker.currentNode;
    if (!n.textContent.trim()) continue;
    const el = n.parentElement;
    if (!el || seen.has(el) || el.offsetParent === null) continue;
    seen.add(el);
    const size = parseFloat(getComputedStyle(el).fontSize);
    if (size < 14) {
      const key = size.toFixed(0) + 'px';
      small[key] = small[key] || [];
      if (small[key].length < 4)
        small[key].push(n.textContent.trim().slice(0, 50));
    }
  }
  out.smallText = small;

  // Search inputs: which have a visible label
  out.searchInputs = [...document.querySelectorAll(
      'input[type=search], input[name=s], input[role=searchbox]')]
    .map(i => ({
      visibleLabel: !!(i.id && document.querySelector(`label[for="${i.id}"]`)
                       && document.querySelector(`label[for="${i.id}"]`).offsetParent)
                    || !!(i.closest('label') && i.closest('label').offsetParent),
      placeholder: !!i.placeholder,
      ariaLabel: !!i.getAttribute('aria-label'),
    }));

  // Large ad blocks inside main content
  const main = document.querySelector('main, [role=main], article') || document.body;
  out.ads = [...main.querySelectorAll(
      'ins.adsbygoogle, [id*="ad-"], [class*="advert"], [aria-label*="dvert"], iframe[src*="ads"]')]
    .map(el => Math.round(el.getBoundingClientRect().height))
    .filter(h => h >= 250);

  // Text broken mid-word by word-break
  out.wordBreak = [...document.querySelectorAll('a, h1, h2, h3, h4, p')]
    .filter(el => ['break-word', 'break-all'].includes(getComputedStyle(el).wordBreak)
                  && el.offsetParent !== null).length;
  return out;
}
"""


def _targets(nodes, limit=4):
    items = []
    for n in nodes[:limit]:
        t = n.get("target", [""])
        sel = " ".join(str(x) for x in t) if isinstance(t, list) else str(t)
        items.append(f"`{sel}`")
    more = len(nodes) - limit
    return ", ".join(items) + (f" and {more} more" if more > 0 else "")


def audit(url: str):
    if not url or not url.strip():
        return "Please enter a URL."
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            try:
                page.goto(url, wait_until="networkidle", timeout=45000)
            except Exception:
                page.goto(url, wait_until="load", timeout=45000)
        except Exception as e:
            browser.close()
            return f"Couldn\u2019t open {url}. Check the address and try again.\n\n_{e.__class__.__name__}_"
        title = page.title()
        axe_results = Axe().run(page).response
        extra = page.evaluate(EXTRA_JS)
        browser.close()

    findings = []  # (sort_key, text) - same plain style as the reference review

    for v in axe_results.get("violations", []):
        rid = v["id"]
        impact = v.get("impact") or "minor"
        heading, what, fix = PLAIN.get(
            rid, (v["help"], v["description"], f"See {v['helpUrl']}"))
        where = _targets(v["nodes"])
        findings.append((IMPACT_ORDER.get(impact, 3),
                         f"{heading}. {what} Affected: {where}. {fix}"))

    if extra["duplicateIds"]:
        ids = ", ".join(f"`{d['id']}`" + (" appears twice" if d["n"] == 2
                        else f" appears {d['n']} times") for d in extra["duplicateIds"][:6])
        findings.append((1, f"Duplicate ID: {ids} on the page. Make every ID "
                         "unique, for example by renaming the copy used in the mobile menu."))

    if extra["tablesNoCaption"]:
        n = extra["tablesNoCaption"]
        word = {1: "The table has", 2: "The two tables have", 3: "The three tables have"}.get(
            n, f"The {n} tables have")
        findings.append((2, f"{word} no `<caption>`. A caption would help "
                         "screen-reader users know which table they\u2019re in."))

    s_in = extra["searchInputs"]
    no_vis = [x for x in s_in if not x["visibleLabel"]]
    if len(s_in) > 1 and 0 < len(no_vis) < len(s_in):
        findings.append((2, f"Search fields: {len(no_vis)} of the {len(s_in)} search "
                         f"inputs {'has' if len(no_vis) == 1 else 'have'} no visible label. That\u2019s acceptable if they "
                         "have an aria-label, but it isn\u2019t consistent with the others."))

    if extra["smallText"]:
        parts = []
        for k, v in sorted(extra["smallText"].items()):
            parts.append(f"{k} (\u201c" + "\u201d, \u201c".join(v[:2]) + "\u201d)")
        findings.append((3, "Small captions and meta text. Some text is under "
                         f"14px: {'; '.join(parts)}. That isn\u2019t a failure, but "
                         "14\u201315px would read better next to 16px body text."))

    if extra["ads"]:
        findings.append((3, f"{max(extra['ads'])}px ad block inside the main content. "
                         "It sits in the reading flow and leaves a large gap, which "
                         "may hurt readability."))

    if extra["wordBreak"]:
        findings.append((3, "Text breaks mid-word. Some titles use "
                         "`word-break: break-word`, so narrow spaces split words "
                         "(\u201cAccessibilit / y\u201d). Remove it, or give the text more width."))

    findings.sort(key=lambda f: f[0])
    lines = ["What to fix, in priority order", ""]
    if not findings:
        lines.append("No automated issues found. Still test with a keyboard and a screen reader.")
    for i, (_, text) in enumerate(findings, 1):
        lines.append(f"{i}. {text}")
    lines += ["", "Check by hand too: text inside images (cut off at the edges, "
              "or too small and light to read), how thumbnails look when cropped "
              "square, and keyboard focus order. Automated tools can\u2019t see those."]
    return polish("\n".join(lines))


def polish(report: str) -> str:
    """Optionally rewrite the report with an LLM if HF_TOKEN is set."""
    token = os.environ.get("HF_TOKEN")
    if not token:
        return report
    try:
        from huggingface_hub import InferenceClient
        client = InferenceClient(token=token)
        resp = client.chat_completion(
            model=os.environ.get("LLM_MODEL", "Qwen/Qwen2.5-72B-Instruct"),
            messages=[
                {"role": "system", "content":
                 "You are a senior accessibility analyst. Rewrite the report "
                 "in exactly this style: first line 'What to fix, in priority "
                 "order', then a numbered list. Each item is plain prose: a "
                 "short problem statement ending in a full stop, what is "
                 "wrong and who it affects, then the fix with a concrete "
                 "example. Use sub-bullets only when one item has several "
                 "parts. No severity labels, no extra headings, no bold. "
                 "Keep every finding and the order. Do not invent issues."},
                {"role": "user", "content": report},
            ],
            max_tokens=2000,
        )
        return resp.choices[0].message.content
    except Exception as e:  # fall back to the rule-based report
        return report + f"\n\n_(LLM polish skipped: {e})_"


with gr.Blocks(title="Accessibility Page Review") as demo:
    gr.Markdown("# Accessibility Page Review\nEnter a page URL to get a "
                "plain-language list of accessibility fixes, in priority order.")
    with gr.Row():
        url_in = gr.Textbox(label="Page URL", placeholder="https://rehanusmani.in/...",
                            scale=4)
        btn = gr.Button("Review page", variant="primary", scale=1)
    out = gr.Markdown(label="Report")
    btn.click(audit, inputs=url_in, outputs=out)
    url_in.submit(audit, inputs=url_in, outputs=out)

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=int(os.environ.get("PORT", 7860)))
