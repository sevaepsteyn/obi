"""Per-channel reply formatting (feature 082).

obi's model emits standard **markdown** (``**bold**``, ``# heading``, ``- bullet``). Different
surfaces render different things, so each channel adapts the one canonical form at its delivery
boundary instead of shipping raw markdown that doesn't render. This module is the shared,
dependency-light (stdlib ``re`` only) core of that layer:

- :func:`to_slack_mrkdwn` — markdown → Slack *mrkdwn* (single-``*`` bold, ``<url|text>`` links,
  ``•`` bullets). Applied to obi's Slack DM replies and the watcher's threaded report body.
- :func:`to_plain_text` — markdown → speakable plain prose, for the voice/TTS path.

Both are pure, idempotent on their own output, and a no-op on text with no markup. The terminal
renders with ``rich`` and the web renders to HTML in the browser (``talk.js``); those live with
their surfaces, not here.

Design note (single-``*`` ambiguity): standard markdown ``*x*`` is *italic* while Slack ``*x*`` is
*bold*. :func:`to_slack_mrkdwn` therefore does **not** rewrite single-``*`` spans — it only
collapses ``**bold**`` to ``*bold*`` and leaves ``_italic_`` (valid in both) alone. That keeps the
function idempotent (its ``*bold*`` output is never re-read as italic) and means it is only ever
fed known-canonical markdown, never hand-composed mrkdwn.
"""

from __future__ import annotations

import re

# A private sentinel wrapping stashed spans (code, links, converted emphasis) so later passes never
# reformat inside them. Real messages never contain NUL, so this can't collide with content.
_TOKEN = "\x00{}\x00"
_TOKEN_RE = re.compile("\x00(\\d+)\x00")


def to_slack_mrkdwn(md: str) -> str:
    """Convert standard markdown to Slack *mrkdwn*.

    Input is assumed to be canonical markdown (obi's answer text or the watcher's rendered report),
    never already-mrkdwn. Idempotent on its own output; a no-op on plain, unformatted text.
    """

    if not md:
        return md

    stash: list[str] = []

    def keep(value: str) -> str:
        stash.append(value)
        return _TOKEN.format(len(stash) - 1)

    text = md
    # Protect code first — never reformat inside it (Slack renders ``` and ` as-is, so keep them).
    text = re.sub(r"```.*?```", lambda m: keep(m.group(0)), text, flags=re.DOTALL)
    text = re.sub(r"`[^`]*`", lambda m: keep(m.group(0)), text)
    # Links [text](url) -> <url|text>.
    text = re.sub(
        r"\[([^\]]+)\]\(([^)\s]+)\)",
        lambda m: keep(f"<{m.group(2)}|{m.group(1)}>"),
        text,
    )
    # Bold-italic first (***x*** / ___x___ -> *_x_*), then bold (**x** / __x__ -> *x*), then
    # strikethrough (~~x~~ -> ~x~). Stash each so the results aren't re-touched.
    text = re.sub(r"\*\*\*(.+?)\*\*\*", lambda m: keep(f"*_{m.group(1)}_*"), text)
    text = re.sub(r"___(.+?)___", lambda m: keep(f"*_{m.group(1)}_*"), text)
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: keep(f"*{m.group(1)}*"), text)
    text = re.sub(r"__(.+?)__", lambda m: keep(f"*{m.group(1)}*"), text)
    text = re.sub(r"~~(.+?)~~", lambda m: keep(f"~{m.group(1)}~"), text)

    # Block constructs, line by line: ATX headings -> a bold line; -/*/+ bullets -> a "• " bullet.
    # Ordered lists and "> " quotes are left as-is (Slack renders them).
    out_lines: list[str] = []
    for line in text.split("\n"):
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", line)
        if heading is not None:
            content = heading.group(1).strip()
            out_lines.append(keep(f"*{content}*") if content else "")
            continue
        bullet = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        if bullet is not None:
            out_lines.append(f"{bullet.group(1)}• {bullet.group(2)}")
            continue
        out_lines.append(line)
    text = "\n".join(out_lines)

    # Restore stashed spans high-index-first so a span that nests a lower-index token (e.g. a bold
    # wrapping a link) expands correctly.
    for i in range(len(stash) - 1, -1, -1):
        text = text.replace(_TOKEN.format(i), stash[i])
    return text


def to_plain_text(md: str) -> str:
    """Strip markdown to speakable plain prose (for the voice/TTS path).

    Removes emphasis/strikethrough/heading/code markers (keeping the inner text), turns
    ``[text](url)`` into ``text``, drops list/quote markers, and collapses blank runs. Surrounding
    whitespace is trimmed. Idempotent; a no-op (aside from trimming) on text with no markup.
    """

    if not md:
        return md

    text = md
    # Fenced code: keep the code text, drop the fences.
    text = re.sub(r"```[^\n]*\n?(.*?)```", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    # Links / images: keep the visible text.
    text = re.sub(r"!?\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Emphasis / strikethrough: strip the markers, keep the inner text.
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"___(.+?)___", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    # Single-delimiter emphasis (*x* / _x_) not glued to surrounding word characters — so device
    # names like core_gw_1 and arithmetic keep their symbols.
    text = re.sub(r"(?<!\w)[*_](?=\S)(.+?)(?<=\S)[*_](?!\w)", r"\1", text)
    # Block markers at line start: headings, quotes, bullets, ordered items.
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s{0,3}>\s?", "", text)
    text = re.sub(r"(?m)^(\s*)[-*+]\s+", r"\1", text)
    text = re.sub(r"(?m)^(\s*)\d+\.\s+", r"\1", text)
    # Collapse runs of blank lines.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
