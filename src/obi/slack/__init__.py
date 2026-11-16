"""obi's Slack interface — a thin transport adapter over the chat runtime (feature 009).

This package lets an authorized operator reach obi's read-only chat brain from Slack: it runs
a long-lived Socket Mode listener (outbound WebSocket — no inbound endpoint), authorizes each
DM sender against an explicit allowlist of Slack user IDs, and feeds the message into the
**unchanged** chat runtime (the same six read-only tools). It answers ONLY in DMs, threads each
DM as a session, attributes every audited access to the operator's Slack user ID, and never
posts network data into a channel.

Per Constitution Principle III it holds no business logic — it authorizes, maps a Slack event to
a question, calls ``obi.chat``'s runtime, and posts the answer. ``slack_sdk`` is imported lazily
in :mod:`obi.slack.listener` so importing obi's core/CLI never requires it, and the offline core
keeps working if it is absent.

The package also holds the **outbound** half (feature 015): :mod:`obi.slack.notifier` DMs the log
watcher's findings to the operator. It is the mirror of the listener — outbound ``chat.postMessage``
only, **bot token alone** (no Socket Mode, no app token, no running listener) — and likewise lazily
imports ``slack_sdk`` and posts only in DMs, never to a channel.
"""
