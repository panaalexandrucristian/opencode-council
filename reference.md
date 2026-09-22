# OpenCode v2 HTTP API — notes used by oc.sh

Verified against OpenCode v2.0.11. OpenAPI 3.1 spec: `GET /openapi.json` on the server
(`oc.sh api GET /openapi.json`). All paths below are prefixed by the server URL.

## Server / auth

- Background service: `opencode service start|stop|restart|status|get|set|unset`.
  State file `${XDG_STATE_HOME:-~/.local/state}/opencode/service.json` = `{id, version, url, pid, password}`.
  Config `${XDG_CONFIG_HOME:-~/.config}/opencode/service.json` (`hostname`, `port`, `password`) via `opencode service set`.
  Default managed port 49374 on 127.0.0.1. `opencode api METHOD PATH` and every `opencode` command auto-start it.
- Standalone: `opencode serve [--port N] [--hostname H] [--cors ORIGIN]` prints `server password ...` at startup.
- Auth: HTTP Basic, user `opencode`, password as above. Without it every `/api/*` route answers 401.
- Non-`/api` paths (e.g. `/`, `/doc`) serve the web UI, not the API.

## Location

The service is global; each request targets a *location* (project directory):
- query param `location[directory]=<abs path>` on list endpoints (`/api/model`, `/api/agent`, `/api/vcs/*`, ...),
  URL-encoded as `location%5Bdirectory%5D=...`; `/api/session` list uses `directory=<abs path>`.
- `POST /api/session` body `location: {directory}` fixes the directory for the session.
- `POST /api/location/reload?location[directory]=...` re-scans a directory (e.g. after `git init`).

## Endpoints oc.sh uses

| method & path | body / params | response |
|---|---|---|
| `GET /api/info` | – | `{version, pid, urls[], paths}` (health check) |
| `GET /api/model?location[directory]=D` | – | `{data:[{id, modelID, providerID, name, enabled, status, variants, limit, cost}]}` |
| `GET /api/model/default?...` | – | `{data: Model.Info|null}` |
| `GET /api/agent?...` | – | `{data:[{id, name, mode: primary|subagent|all, hidden, description, permissions}]}` |
| `GET /api/session?directory=D&limit=N&order=desc` | – | `{data:[Session.Info], cursor}` |
| `POST /api/session` | `{title?, agent?, model?: {providerID, id, variant?}, location?: {directory}, permissions?: [{action, resource, effect: allow|deny|ask}]}` | `{data: Session.Info}` (`id` = `ses_...`) |
| `GET /api/session/{sid}` | – | `{data: Session.Info}` (`time.idle`, `outcome`, `location`, `permissions`) |
| `POST /api/session/{sid}/prompt` | `{text, files?, agents?, skills?, delivery?: steer|queue, resume?}` | `{data: inbox item}` (`id` = `msg_...`) |
| `POST /api/experimental/session/{sid}/wait` | – | `204` once the agent loop is idle; **blocks forever while a permission request is pending** |
| `GET /api/session/{sid}/message?order=asc|desc&limit=N&type=...` | – | `{data:[msg], cursor}`; msg types: `user{text}`, `assistant{content:[{type:text,text}|{type:reasoning}|{type:tool,name,state:{status,input,content}}], finish, error}`, `idle{outcome: succeeded|failed|interrupted}`, `system`, ... |
| `GET /api/session/{sid}/permission` | – | `{data:[{id: per_..., action, resources[], message?}]}` |
| `POST /api/session/{sid}/permission/{pid}/reply` | `{decision: once|always|reject, message?}` | 204 |
| `POST /api/session/{sid}/interrupt[?resume=true]` | – | `{interrupted: bool}` |
| `GET /api/session/{sid}/diff[?from=msg_&to=msg_&context=N]` | – | `{data:[{file, status, additions, deletions, patch}]}` (needs turn snapshots) |
| `GET /api/vcs/status?location[directory]=D` | – | `{data:[{file, status, additions, deletions}]}` (git working tree) |

`oc.sh` requires successful curl transport as well as a successful HTTP status. Result commands
inspect structured assistant errors and idle outcomes; a failed or unavailable result remains
printable for inspection but returns exit 1.

Model ref = `Model.Info.id` + `providerID` (e.g. `{"providerID":"opencode","id":"muse-spark-1.3-contributor-free"}`);
`oc.sh` accepts it as `provider/id`.

## Other useful endpoints

- `GET /api/event` — SSE stream of all server events (session/message/permission updates).
- `POST /api/session/{sid}/fork`, `/agent`, `/model`, `/compact`, `/revert/*`, `GET .../context`, `GET .../inbox`
- `POST /api/session/{sid}/shell` — run a shell command inside the session; `POST /api/session/{sid}/command` — slash command.
- `GET /api/skill`, `GET /api/command`, `GET /api/mcp`, `GET /api/provider`, `GET /api/config`, `GET /api/project`
- `GET /api/fs/read/*`, `GET /api/fs/list`, `GET /api/fs/find` — filesystem helpers relative to the location.
- `POST /api/experimental/generate` — one-off text generation without a session.
