#!/usr/bin/env bash
# Read-only test: can a GitHub App read the Projects board owned by Patrick's
# personal account? Runs on the VM as tw-git, the only user that can read the
# Apps' keys:
#
#   sudo -u tw-git bash scripts/check-board-access.sh <app id> <installation id> <key file>
#
# for example, the workers' App:
#
#   sudo -u tw-git bash scripts/check-board-access.sh 5547094 177393064 /home/tw-git/apps/workers.pem
#
# It signs a ten-minute App token with the key (the key never leaves this
# process and nothing is printed from it), trades it for an installation
# token, asks GitHub for Patrick's Projects boards, prints their numbers and
# titles or GitHub's refusal, and revokes the installation token. It writes
# nothing to any repo or board.
set -euo pipefail

app_id="${1:?usage: check-board-access.sh <app id> <installation id> <key file>}"
installation="${2:?usage: check-board-access.sh <app id> <installation id> <key file>}"
key="${3:?usage: check-board-access.sh <app id> <installation id> <key file>}"
owner="EvilPatrick06"

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

query='query($login:String!){ user(login:$login){ projectsV2(first:20){ totalCount nodes{ number title } } } }'
answer=$(jq -n --arg q "$query" --arg login "$owner" '{query:$q, variables:{login:$login}}' |
  curl -sS -X POST -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
    --data-binary @- https://api.github.com/graphql)

if jq -e '.errors' >/dev/null <<<"$answer"; then
  echo "RESULT: the App cannot read $owner's boards. GitHub said:"
  jq -r '.errors[].message' <<<"$answer"
  exit 2
fi
count=$(jq -r '.data.user.projectsV2.totalCount' <<<"$answer")
echo "RESULT: the App reads $count of $owner's boards:"
jq -r '.data.user.projectsV2.nodes[] | "  #\(.number) \(.title)"' <<<"$answer"
if [[ "$count" == "0" ]]; then
  echo "(0 can also mean GitHub hides user-owned boards from Apps rather than refusing; treat it as no access.)"
fi
