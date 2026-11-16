# Paging (`obi page`)

obi can send pages through a paging service's HTTP API (a uAPI-style endpoint taking a username,
password, recipients and message). The `obi page` verbs are operator-only. The automated
dashboard pager needs two more explicit opt-ins (`[paging.alerts] enabled` and
`[dash.pager] enabled`).

## Configure

```toml
[paging]
url = "https://pager.example.net/api.php"   # required: no built-in endpoint
username = "<account>"
password = "<secret>"

[paging.slack]                     # optional: let allowlisted Slack users page via DM
recipients = ["Ops On-Call"]       # the COMPLETE pageable allowlist (people or groups)
channels = ["C0XXXXXXXXX"]         # channel whose members may ask
```

## Use

```sh
uv run obi page whoami                         # verify credentials; never sends
uv run obi page refresh                        # snapshot the people/group roster locally
uv run obi page recipients ops                 # search the local roster (offline)
uv run obi page send "Ops On-Call" "router down at the hub"
```

A refusal (an ambiguous name, or an over-length message) transmits nothing.
