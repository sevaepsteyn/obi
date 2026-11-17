# Running obi's daemons (launchd)

obi has two always-on processes: the Slack listener (`obi slack run`) and the web server (`obi web serve`). On macOS, obi's own commands supervise each with launchd, so it restarts on crash and returns after a reboot without hand-editing a plist.

```bash
obi web start            # install + load a LaunchAgent (RunAtLoad + KeepAlive), start now
obi slack start          # same for the listener (--no-login skips start-at-login)
obi web status           # launchd state + live /healthz probe + cron-collector freshness
obi slack status         # launchd state + listener heartbeat + collector freshness
obi web restart          # kickstart -k after a deploy
obi web stop             # bootout — NOT restarted until the next `start`
obi web logs --follow    # tail ~/.local/state/obi/web-serve.log
```

## Behaviour

- **Supervision.** `start` writes `~/Library/LaunchAgents/com.obi.<name>.plist`, loads it into your user session and points stdout/stderr at the existing log. `KeepAlive` restarts on crash; `RunAtLoad` starts at login unless `--no-login`.
- **Liveness.** `web status` runs a live `GET /healthz` (200 = healthy). `slack status` reads the listener's heartbeat to tell *connected* from *alive-but-wedged*. Both also show the eleven cron collectors' freshness.
- **No secrets in the plist.** Daemons read `~/.config/obi/config.toml` at runtime; the plist only sets `PATH`. If your API key lives in the environment rather than `[chat]`, add it under `[service].environment`.
- **Primary checkout only.** `start` refuses a daemon whose `obi` lives in a feature worktree (`~/src/obi-worktrees/…`). Override with `--exec`.
- **Cutover.** If a listener is already running from a hand-typed `nohup obi slack`, stop it first (`obi slack start` warns you) so two listeners don't run.

## Config

```toml
# [service]
# run_at_login = true   # default RunAtLoad for `start` (--login/--no-login override)
# environment = { }     # extra env for the daemons (only if a secret lives in env)
```

## Limits

- Log rotation is not handled; the daemon logs are tiny.
- Cron jobs stay on cron. This only supervises the two persistent daemons and *reads* the collectors' freshness.

See also: [chat.md](./chat.md) (Slack listener), [web.md](./web.md) (web server).
