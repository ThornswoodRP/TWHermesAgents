#!/usr/bin/env bash
# The node the VM runs, one pinned release from nodejs.org, checked by its
# SHA-256 before anything of it is unpacked (Thornswood-Ops #275 and #264).
#
#   deploy/node.sh <folder>
#
# Puts node-v<version>-linux-<arch> in <folder>, unless it is already there
# at this version, and prints its bin folder. deploy/install.sh links
# /usr/local/bin/node to it, and the VM's job in guard-rail-tests.yml puts the
# same folder first on PATH, so the suite runs the node the VM has.
#
# Why not Ubuntu's nodejs: noble's is 18, and Thornswood's gamemode builder
# (scripts/dev/Build-Gamemode.js) needs node:module's stripTypeScriptTypes,
# which node has from 22.13. Under 18 it stops at "stripTypeScriptTypes is not
# a function", so no Bot could build game/gamemode.js in its tree. Thornswood's
# CI runs node 24, so this is the same major, and moves with it.
#
# The hashes are nodejs.org's SHASUMS256.txt for this release.
set -euo pipefail

version=24.22.3
case "$(uname -m)" in
  x86_64) arch=x64; sha=4f2b175a6d53a5c617a80dba2676238a0688cef28c6da4eba35c7d844e66fcfb ;;
  aarch64|arm64) arch=arm64; sha=6e51a2ada00f0011cc266f65d98922b403d7ad88f809ca2aa615a5fe1ce6e86c ;;
  *) echo "STOP: no node is pinned for $(uname -m)." >&2; exit 1 ;;
esac

[[ $# -eq 1 && -n "$1" ]] || { echo "STOP: name the folder node goes in." >&2; exit 1; }
dest="$1"
name="node-v$version-linux-$arch"
dir="$dest/$name"

if [[ -x "$dir/bin/node" && "$("$dir/bin/node" --version 2>/dev/null)" == "v$version" ]]; then
  echo "$dir/bin"
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -fsSL --proto '=https' --tlsv1.2 -o "$tmp/$name.tar.xz" "https://nodejs.org/dist/v$version/$name.tar.xz" \
  || { echo "STOP: $name.tar.xz could not be fetched from nodejs.org." >&2; exit 1; }
got="$(sha256sum "$tmp/$name.tar.xz" | cut -d' ' -f1)"
[[ "$got" == "$sha" ]] || { echo "STOP: $name.tar.xz has SHA-256 $got, not the pinned $sha; nothing was unpacked." >&2; exit 1; }
tar -xJf "$tmp/$name.tar.xz" -C "$tmp" --no-same-owner
[[ "$("$tmp/$name/bin/node" --version)" == "v$version" ]] || { echo "STOP: the unpacked node is not v$version." >&2; exit 1; }
mkdir -p "$dest"
rm -rf "$dir"
mv -T "$tmp/$name" "$dir"
echo "$dir/bin"
