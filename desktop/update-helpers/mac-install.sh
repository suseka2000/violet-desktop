#!/bin/sh
# Runs outside the .app. No sudo, xattr removal, Gatekeeper changes or user-data
# moves. Arguments are separate argv entries, never evaluated as shell code.
set -eu
transaction=$1; target=$2; staged=$3; backup=$4; failed=$5; old_pid=$6; old_start=$7
version=$8; token=$9; shift 9
parent_timeout=$1; ready_timeout=$2; archive=$3; expected_sha=$4
id=${transaction##*/}
status() { printf '%s\n' "$1" > "$transaction/status"; }
fail() { status "$1"; exit 1; }
cancelled() { [ -e "$transaction/cancel" ]; }
field() { /usr/bin/plutil -extract "$2" raw -o - "$1" 2>/dev/null; }
same_parent() {
  current=$(/bin/ps -p "$old_pid" -o lstart= 2>/dev/null | /usr/bin/sed 's/^ *//;s/ *$//') || return 1
  [ "$current" = "$old_start" ]
}
valid_bundle() {
  [ -d "$1" ] && [ ! -L "$1" ] &&
  [ "$(field "$1/Contents/Info.plist" CFBundleIdentifier)" = "com.violet.interior-rd" ]
}
[ -d "$transaction" ] && [ ! -L "$transaction" ] || exit 1
valid_bundle "$target" || fail invalid-installed-bundle
valid_bundle "$staged" || fail invalid-staged-bundle
[ "$(field "$staged/Contents/Info.plist" CFBundleShortVersionString)" = "$version" ] || fail wrong-version
[ ! -e "$backup" ] && [ ! -e "$failed" ] || fail backup-exists
[ "$(dirname "$target")" = "$(dirname "$backup")" ] || fail different-filesystem
[ "$(/usr/bin/shasum -a 256 "$archive" | /usr/bin/awk '{print $1}')" = "$expected_sha" ] || fail changed-archive
cancelled && fail cancelled
printf '{"id":"%s","token":"%s"}\n' "$id" "$token" > "$transaction/armed.json.tmp"
/bin/mv "$transaction/armed.json.tmp" "$transaction/armed.json"
status waiting-for-previous-process
elapsed=0
while same_parent; do
  cancelled && fail cancelled
  [ "$elapsed" -lt "$parent_timeout" ] || fail previous-process-still-running
  /bin/sleep 1
  elapsed=$((elapsed+1))
done
cancelled && fail cancelled
valid_bundle "$target" || fail installed-bundle-changed
valid_bundle "$staged" || fail staged-bundle-changed
[ ! -e "$backup" ] && [ ! -e "$failed" ] || fail backup-exists
status replacing
/bin/mv "$target" "$backup" || fail cannot-preserve-previous-version
if ! /bin/mv "$staged" "$target"; then
  /bin/mv "$backup" "$target" || fail restore-required
  fail replacement-failed
fi
rollback() {
  # Stop only a process whose exact executable is inside the replaced bundle.
  # Never kill an unrelated executable or a process from the user's workspace.
  binary=$(field "$target/Contents/Info.plist" CFBundleExecutable || true)
  app_executable="$target/Contents/MacOS/$binary"
  /bin/ps -axo pid=,comm= | while read -r pid executable; do
    if [ "$executable" = "$app_executable" ]; then /bin/kill -TERM "$pid" 2>/dev/null || true; fi
  done
  /bin/sleep 1
  if [ -e "$target" ]; then /bin/mv "$target" "$failed" || fail restore-required; fi
  /bin/mv "$backup" "$target" || fail restore-required
  status rolled-back
  /usr/bin/open "$target" >/dev/null 2>&1 || true
  exit 1
}
status starting-new-version
if ! /usr/bin/open "$target" >/dev/null 2>&1; then rollback; fi
elapsed=0
while [ "$elapsed" -lt "$ready_timeout" ]; do
  receipt="$transaction/ready.json"
  if [ -f "$receipt" ] && [ ! -L "$receipt" ] &&
      [ "$(field "$receipt" id || true)" = "$id" ] &&
      [ "$(field "$receipt" token || true)" = "$token" ] &&
      [ "$(field "$receipt" version || true)" = "$version" ] &&
      [ "$(field "$receipt" execPath || true)" = "$target/Contents/MacOS/$(field "$target/Contents/Info.plist" CFBundleExecutable)" ]; then
    status ready
    # Keep the previous .app intact for explicit recovery.
    exit 0
  fi
  /bin/sleep 1
  elapsed=$((elapsed+1))
done
rollback
