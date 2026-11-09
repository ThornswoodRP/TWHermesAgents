#!/usr/bin/env bash
# Read-only test: can a GitHub App read Thornswood's board, the Projects board
# the ThornswoodRP organization owns (https://github.com/orgs/ThornswoodRP/projects/1)?
# Runs on the VM as tw-git, the only user that can read the Apps' keys:
#
#   sudo -u tw-git bash scripts/check-board-access.sh <app id> <installation id> <key file>
#
# for example, the workers' App (organization projects: read, since 2 Nov):
#
#   sudo -u tw-git bash scripts/check-board-access.sh 5547094 177393064 /etc/thornswood/apps/thornswood-workers.pem
#
# On 8 Nov 2026 it read the board's title, "Thornswood To Do", and its 1,356
# items with their Status. The board moved from Patrick's personal account to
# the organization with the repositories, so the old question (can an App read
# a user-owned board) no longer applies.
#
# It signs a ten-minute App token with the key (the key never leaves this
# process and nothing is printed from it), trades it for an installation
# token, asks GitHub for the board's title, item count and the first items'
# Status, prints them or GitHub's refusal, and revokes the installation token.
# It writes nothing to any repo or board.
set -euo pipefail

app_id="${1:?usage: check-board-access.sh <app id> <installation id> <key file>}"
installation="${2:?usage: check-board-access.sh <app id> <installation id> <key file>}"
key="${3:?usage: check-board-access.sh <app id> <installation id> <key file>}"
org="ThornswoodRP"
number=1

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
now=$(date +%s)
header=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
payload=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((now - 60))" "$((now + 540))" "$app_id" | b64url)
signature=$(printf '%s.%s' "$header" "$payload" | openssl dgst -sha256 -sign "$key" -binary | b64url)
jwt="$header.$payload.$signature"

token=$(curl -sS -X POST -H "Authorization: Bearer $jwt" -H "Accept: application/vnd.github+json" \
  "https://api.github.com/app/installations/$installation/access_tokens" | jq -r '.token // empty')
if [[ -z "$token" ]]; then
  echo "RESULT: no installation token; GitHub refused the App's own sign-in, so nothing about the board was tested."
  exit 1
fi
revoke() {
  curl -sS -o /dev/null -X DELETE -H "Authorization: Bearer $token" "https://api.github.com/installation/token"
}
trap revoke EXIT

query='query($login:String!, $number:Int!){ organization(login:$login){ projectV2(number:$number){ title items(first:5){ totalCount nodes{ fieldValueByName(name:"Status"){ ... on ProjectV2ItemFieldSingleSelectValue{ name } } } } } } }'
answer=$(jq -n --arg q "$query" --arg login "$org" --argjson number "$number" '{query:$q, variables:{login:$login, number:$number}}' |
  curl -sS -X POST -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
    --data-binary @- https://api.github.com/graphql)

if jq -e '.errors' >/dev/null <<<"$answer"; then
  echo "RESULT: the App cannot read $org's board $number. GitHub said:"
  jq -r '.errors[].message' <<<"$answer"
  exit 2
fi
title=$(jq -r '.data.organization.projectV2.title' <<<"$answer")
count=$(jq -r '.data.organization.projectV2.items.totalCount' <<<"$answer")
echo "RESULT: the App reads $org's board $number, \"$title\", with $count items. The first items' Status:"
jq -r '.data.organization.projectV2.items.nodes[] | "  \(.fieldValueByName.name // "no Status")"' <<<"$answer"
