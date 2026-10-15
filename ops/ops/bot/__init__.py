"""
ops.bot
────
Telegram control bot for archiver-suite. Long-polls Bot API, lets allowed users
drive suite CLIs, broadcasts recording start/end, and deletes traffic after 24h.
Modules: telegram (Bot API client), state (persistent offset/report/message tracking),
commands (CLI parsing & subprocess helpers), loop (main polling loop).
Stdlib-only; reaches archiver/recorder/dispatcher only via their CLI binaries.
"""
