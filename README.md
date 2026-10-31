---
title: Accessibility Page Review
emoji: ♿
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# Accessibility Page Review

Enter a page URL. The app opens it in a headless browser, runs axe-core plus extra checks
(duplicate IDs, table captions, small text, search-label consistency, large ad blocks,
mid-word breaks), and returns a plain-language list under "What to fix, in priority order".

Optional: add an `HF_TOKEN` secret in the Space settings to have an LLM rewrite the
report in the same style (model can be changed with an `LLM_MODEL` variable).
Without it the rule-based report is shown, which costs nothing.
