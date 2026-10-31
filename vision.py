"""Vision check: an AI model looks at the page's images as they are displayed.

Finds what rule-based tools can't see:
  * text inside an image that is cut off at the edges, or cropped by a square thumbnail
  * text inside an image that is too small or too faint to read
  * alt text that leaves out the information the image shows

Provider (set ONE environment variable):
  GEMINI_API_KEY  - Google Gemini (free tier available). Model: VISION_MODEL,
                    default "gemini-flash-latest".
  HF_TOKEN        - Hugging Face Inference. Model: VISION_MODEL,
                    default "Qwen/Qwen2.5-VL-7B-Instruct".
With neither set, the vision check is skipped.
"""

import base64
import io
import json
import os
import re

import requests
from PIL import Image

MAX_IMAGES = int(os.environ.get("VISION_MAX_IMAGES", "6"))

# Visible, reasonably large images. Content images first, then sidebar/other.
FIND_IMAGES_JS = r"""
() => {
  const main = document.querySelector('main, [role=main], article, .entry-content');
  const seen = new Set();
  const out = [];
  document.querySelectorAll('img').forEach((img, i) => {
    const r = img.getBoundingClientRect();
    const src = img.currentSrc || img.src;
    if (!src || seen.has(src) || r.width < 120 || r.height < 80) return;
    const cs = getComputedStyle(img);
    if (cs.visibility === 'hidden' || cs.display === 'none' || img.offsetParent === null) return;
    seen.add(src);
    img.setAttribute('data-vision-id', String(i));
    const fig = img.closest('figure');
    const cap = fig && fig.querySelector('figcaption');
    out.push({
      id: String(i),
      alt: img.hasAttribute('alt') ? img.getAttribute('alt') : null,
      caption: cap ? cap.innerText.trim().slice(0, 200) : '',
      inMain: !!(main && main.contains(img)),
      featured: /wp-post-image|featured|hero/i.test(img.className + ' ' + (img.parentElement?.className || '')),
      width: Math.round(r.width), height: Math.round(r.height),
      naturalWidth: img.naturalWidth, naturalHeight: img.naturalHeight,
      objectFit: cs.objectFit,
      file: src.split('/').pop().split('?')[0].slice(0, 60),
    });
  });
  out.sort((a, b) => (b.featured - a.featured) || (b.inMain - a.inMain));
  return out;
}
"""

PROMPT = """You are a senior accessibility analyst reviewing images on a web page.
Each image is a screenshot of how it is DISPLAYED on the page (so any cropping is visible).
For each image you get its number, alt text, caption, displayed size and whether
it is cropped with object-fit.

For each image, report only real problems you can see:
- "clipped_text": text drawn so close to an edge that letters are cut off, or a
  crop (e.g. a square thumbnail) that cuts words. Quote the affected words/letters.
- "unreadable_text": text in the image that is very small or faint/low-contrast
  (e.g. light grey source lines in charts) and would be hard to read, especially on a phone.
- "alt_missing_info": the image contains meaningful text or data, and the alt text
  (or caption) does not convey it. Say what is missing.
Do not report purely decorative images. Do not guess about things you cannot see.

Write "detail" as one or two plain sentences that say exactly what is wrong,
quoting the words involved. Write "fix" as one plain sentence with a concrete
action (for example "re-export the image with about 60-80px of padding on the left").
No marketing tone, no headings.

Return JSON only, in this shape:
{"images": [{"n": 1, "has_text": true,
             "issues": [{"type": "clipped_text", "detail": "...", "fix": "..."}]}]}
"""

HEADINGS = {
    "clipped_text": ("Text in an image is cut off", 0),
    "unreadable_text": ("Small or faint text in an image", 1),
    "alt_missing_info": ("Alt text leaves out what the image shows", 1),
}


def _jpeg_b64(png_bytes, max_w=1024):
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    if im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def capture(page):
    """Return [(meta, base64_jpeg)] for the most relevant images on the page."""
    shots = []
    for meta in page.evaluate(FIND_IMAGES_JS)[:MAX_IMAGES]:
        try:
            el = page.locator(f'img[data-vision-id="{meta["id"]}"]').first
            el.scroll_into_view_if_needed(timeout=5000)
            shots.append((meta, _jpeg_b64(el.screenshot(timeout=10000))))
        except Exception:
            continue
    return shots


def _describe(n, m):
    alt = "missing" if m["alt"] is None else (f'"{m["alt"]}"' if m["alt"] else "empty (decorative)")
    crop = ", cropped with object-fit: " + m["objectFit"] if m["objectFit"] == "cover" else ""
    where = "featured image" if m["featured"] else ("in the post" if m["inMain"] else "outside the post (e.g. sidebar)")
    return (f"Image {n}: {where}; file {m['file']}; displayed {m['width']}x{m['height']}px"
            f" (original {m['naturalWidth']}x{m['naturalHeight']}){crop}; alt {alt}; "
            f"caption: {m['caption'].rstrip('.') or 'none'}.")


def _parse(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(text)


def _call_gemini(key, intro, shots):
    model = os.environ.get("VISION_MODEL", "gemini-flash-latest")
    parts = [{"text": PROMPT + "\n\n" + intro}]
    for i, (_, b64) in enumerate(shots, 1):
        parts += [{"text": f"Image {i}:"}, {"inline_data": {"mime_type": "image/jpeg", "data": b64}}]
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json={"contents": [{"parts": parts}],
              "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}},
        timeout=90)
    r.raise_for_status()
    return _parse(r.json()["candidates"][0]["content"]["parts"][0]["text"]), f"Gemini ({model})"


def _call_hf(token, intro, shots):
    from huggingface_hub import InferenceClient
    model = os.environ.get("VISION_MODEL", "Qwen/Qwen2.5-VL-7B-Instruct")
    content = [{"type": "text", "text": PROMPT + "\n\n" + intro}]
    for i, (_, b64) in enumerate(shots, 1):
        content += [{"type": "text", "text": f"Image {i}:"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]
    resp = InferenceClient(token=token).chat_completion(
        model=model, messages=[{"role": "user", "content": content}],
        max_tokens=1500, temperature=0.2)
    return _parse(resp.choices[0].message.content), f"Hugging Face ({model})"


def call_model(intro, shots):
    if os.environ.get("GEMINI_API_KEY"):
        return _call_gemini(os.environ["GEMINI_API_KEY"], intro, shots)
    if os.environ.get("HF_TOKEN"):
        return _call_hf(os.environ["HF_TOKEN"], intro, shots)
    return None, None


def enabled():
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("HF_TOKEN"))


def check(page):
    """Run the vision check. Returns (findings, note).

    findings: list of (sort_key, text) in the report's plain style.
    note: one line for the end of the report saying what was checked (or why not).
    """
    if not enabled():
        return [], None
    shots = capture(page)
    if not shots:
        return [], "Vision check: no content images large enough to review."
    intro = "\n".join(_describe(i, m) for i, (m, _) in enumerate(shots, 1))
    try:
        result, who = call_model(intro, shots)
    except Exception as e:
        return [], f"Vision check skipped ({e.__class__.__name__}: {str(e)[:120]})."

    findings = []
    for item in result.get("images", []):
        try:
            meta = shots[int(item["n"]) - 1][0]
        except (KeyError, ValueError, IndexError):
            continue
        label = ("the featured image" if meta["featured"]
                 else f"the image `{meta['file']}`" + ("" if meta["inMain"] else " in the sidebar"))
        for iss in item.get("issues", []):
            head, prio = HEADINGS.get(iss.get("type"), ("Problem in an image", 2))
            detail = iss.get("detail", "").strip().rstrip(".")
            if detail[:2].istitle() or (detail[:1].isupper() and detail[1:2] == " "):
                detail = detail[:1].lower() + detail[1:]
            fix = iss.get("fix", "").strip().rstrip(".")
            if not detail:
                continue
            findings.append((prio, f"{head}. In {label}, {detail}. {fix[:1].upper() + fix[1:]}." if fix
                             else f"{head}. In {label}, {detail}."))
    n = len(shots)
    note = f"Vision check: {n} image{'s' if n != 1 else ''} reviewed by {who}."
    return findings, note
