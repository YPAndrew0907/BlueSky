# Bluesky app password (local dev)

- Stored in macOS Keychain as Generic Password: service `bluesky_app_password`, account `$USER` (login keychain).
- Auto-loaded into `BLUESKY_APP_PASSWORD` via `~/.zshrc` (reads Keychain on shell start / `source ~/.zshrc`).

## Local env file (optional)

If you prefer a repo-local env file for development:

```sh
cp .env.local.example .env.local
$EDITOR .env.local
set -a; source .env.local; set +a
```

Never commit `.env.local` (it is ignored by `.gitignore`).

## Set/update
```sh
security add-generic-password -a "$USER" -s bluesky_app_password -U -w
source ~/.zshrc
```

## Access
```sh
echo "$BLUESKY_APP_PASSWORD"
# or (bypass .zshrc)
security find-generic-password -a "$USER" -s bluesky_app_password -w
```

## Save session tokens to `.env.local` (optional)

If you want to persist `accessJwt` / `refreshJwt` locally (for short-lived sessions):

```sh
export BLUESKY_IDENTIFIER="you.bsky.social"
# BLUESKY_APP_PASSWORD must be set (prefer Keychain)
bash scripts/save_session_env.sh
set -a; source .env.local; set +a
```

Notes:
- `BLUESKY_ACCESS_JWT` expires quickly; `BLUESKY_REFRESH_JWT` lasts longer.
- These are secrets; do not commit them. If you pasted tokens into chat/logs, rotate them.

## Data collection (read-only feed fairness study)

This repo includes a Python 3.11 read-only, resumable, idempotent collection pipeline for a cross-sectional snapshot of Bluesky’s open feed ecosystem (feed generators + starter packs + popular feeds + one snapshot per feed).

- Code: `bsky_fair_collect/`
- Entry: `python3.11 -m bsky_fair_collect run-all --out-dir <OUT_DIR> --auth-mode unauth`
- Outputs: all analysis-ready CSVs land in `OUT_DIR/csv/` (plus `OUT_DIR/logs/run.log` and `OUT_DIR/state/state.db`)
- Resume: re-run the same command with the same `--out-dir` (it will continue without duplicating rows)
- Read-only: does not call interaction/write endpoints (no likes/reposts/follows/blocks; no `app.bsky.feed.sendInteractions`)

Notes:
- Starter pack discovery uses relay enumeration (`app.bsky.graph.starterpack`) + AppView hydration (`app.bsky.graph.getActorStarterPacks` + `app.bsky.graph.getStarterPack`) because `app.bsky.graph.searchStarterPacks` is not available on all AppView deployments.
- Run outputs are ignored by git via `out/` in `.gitignore`.

## Examples (XRPC via curl)

These examples assume you also set `BLUESKY_IDENTIFIER` (your handle/email/DID) and keep `BLUESKY_APP_PASSWORD` secret.

### Fetch timeline (latest posts)
```sh
# Requires: jq
export BLUESKY_IDENTIFIER="you.bsky.social"

APPVIEW="${BLUESKY_APPVIEW:-https://bsky.social}"

session="$(
  jq -n --arg id "$BLUESKY_IDENTIFIER" --arg pw "$BLUESKY_APP_PASSWORD" \
    '{identifier:$id,password:$pw}' \
  | curl -sS -X POST "$APPVIEW/xrpc/com.atproto.server.createSession" \
      -H "Content-Type: application/json" \
      -d @-
)"

accessJwt="$(jq -r '.accessJwt' <<<"$session")"
pds="$(jq -r '.didDoc.service[] | select(.type=="AtprotoPersonalDataServer") | .serviceEndpoint' <<<"$session")"

curl -sS -H "Authorization: Bearer $accessJwt" \
  "$APPVIEW/xrpc/app.bsky.feed.getTimeline?limit=5" \
| jq -r '.feed[].post.record.text'
```

### Export timeline/author feed to CSV
```sh
# Requires: jq
# BLUESKY_IDENTIFIER + BLUESKY_APP_PASSWORD must be set

bash scripts/export_feed_csv.sh timeline --limit 25 --out ./timeline.csv
bash scripts/export_feed_csv.sh author --actor "$BLUESKY_IDENTIFIER" --limit 25 --out ./author_feed.csv
```

### Create a post
```sh
# Requires: jq (and a valid `session`/`accessJwt` from above)
did="$(jq -r '.did' <<<"$session")"

record="$(
  jq -n --arg text "Hello from curl" --arg createdAt "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{ "$type":"app.bsky.feed.post", text:$text, createdAt:$createdAt }'
)"

payload="$(
  jq -n --arg repo "$did" --arg collection "app.bsky.feed.post" --argjson record "$record" \
    '{repo:$repo, collection:$collection, record:$record}'
)"

curl -sS -X POST "$pds/xrpc/com.atproto.repo.createRecord" \
  -H "Authorization: Bearer $accessJwt" \
  -H "Content-Type: application/json" \
  -d "$payload"
```
