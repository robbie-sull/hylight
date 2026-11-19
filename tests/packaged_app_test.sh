#!/bin/zsh
# End-to-end test of a built HyLight.app (macOS), launched through LaunchServices like a
# double-click. Isolated HOME + null keyring: never touches the real Keychain, config or
# Dexcom account. The real button may light briefly (it's the same USB device).
#
#   zsh tests/packaged_app_test.sh path/to/HyLight.app
#
# Quit your own copy of HyLight first: the test app needs port 8765.
APP="${1:?usage: zsh tests/packaged_app_test.sh path/to/HyLight.app}"
APP="${APP:A}"
URL=http://127.0.0.1:8765
EXE="$APP/Contents/MacOS/HyLight"
[ -x "$EXE" ] || { echo "no app at $APP"; exit 2; }
curl -s -o /dev/null --max-time 2 $URL/status && { echo "Something is already answering on $URL -- quit HyLight first."; exit 2; }
ISO=$(mktemp -d -t hylight-test)
LOG="$ISO/.dexcom_led_button/hylight.log"
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; }
pids() { pgrep -f "$EXE" | tr '\n' ' ' | sed 's/ *$//'; }
wait_up()   { for i in {1..60}; do curl -s -o /dev/null $URL/status && return 0; sleep 0.5; done; return 1; }
wait_gone() { for i in {1..${1:-30}}; do [ -z "$(pids)" ] && return 0; sleep 0.5; done; return 1; }
launch() { open --env HOME="$ISO" --env PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring --env BROWSER=true "$@" "$APP"; }
count() { local n; n=$(grep -c "$1" "$LOG" 2>/dev/null); echo ${n:-0}; }

echo "##### 1. launch via LaunchServices"
launch; wait_up && pass "server up" || { fail "server never came up"; exit 1; }
P1=$(pids); echo "pid: $P1"
grep -q "Cocoa bindings unavailable" "$LOG" && fail "Cocoa import failed in frozen build" || pass "Cocoa event loop loaded (no fallback warning)"
grep -q "Watching for sleep/wake" "$LOG" && pass "sleep/wake watcher registered" || fail "sleep/wake watcher not registered"
sleep 3
[ "$(count 'App reopened')" = "0" ] && pass "no spurious 'reopen' event on first launch" || fail "first launch fired a reopen event ($(count 'App reopened'))"

curl -s $URL/status | grep -q '"on_call_until"' && pass "build has the on-demand window (status reports on_call_until)" || fail "status has no on_call_until -- old code in this build?"
curl -s -o /dev/null -w "%{redirect_url}" $URL/ | grep -q "/setup/1" && pass "fresh install opens the setup tour" || fail "fresh install did not redirect to the tour"
curl -s $URL/setup/5 | grep -q "Catch the rise early" && pass "tour pages render inside the bundle" || fail "tour page missing from the bundle"
curl -s $URL/setup/1 | grep -q "cover-logo" && pass "tour cover (logo + login) renders" || fail "tour cover missing"
curl -s -o /dev/null -X POST $URL/setup/exit; curl -s $URL/ | grep -q 'href="/setup/2">Take the setup tour' && pass "sign-in page links to the tour (no account needed)" || fail "sign-in page has no tour link"

echo "##### 2. double-click again while running (reopen event)"
open "$APP"; sleep 3
[ "$(pids)" = "$P1" ] && pass "no duplicate process" || fail "process list changed: [$(pids)]"
[ "$(count 'App reopened')" = "1" ] && pass "reopen event delivered -> settings page re-open requested" || fail "reopen not delivered (count $(count 'App reopened'))"

echo "##### 3. Restart button -> relaunch through LaunchServices"
curl -s -X POST $URL/restart -o /dev/null
wait_gone 20 && pass "old process exited" || fail "old process still alive"
wait_up && pass "server came back up" || fail "server did not come back"
P2=$(pids)
[ -n "$P2" ] && [ "$P2" != "$P1" ] && pass "new process ($P2) replaced old ($P1)" || fail "no new process (pids: [$P2])"
[ "$(count 'Settings UI at')" = "2" ] && pass "log shows two startups" || fail "startup count $(count 'Settings UI at')"
[ "$(cat $ISO/.dexcom_led_button/app.lock)" = "$P2" ] && pass "lock holds the new pid" || fail "lock wrong: $(cat $ISO/.dexcom_led_button/app.lock)"

echo "##### 4. Dock > Quit (Apple Event)"
osascript -e 'with timeout of 15 seconds' -e 'tell application "HyLight" to quit' -e 'end timeout' 2>&1
wait_gone 20 && pass "app exited on Apple Event quit" || fail "app STILL running after Apple Event quit"
grep -q "Quit requested by macOS" "$LOG" && pass "terminate handler ran" || fail "terminate handler did not run"
[ -e "$ISO/.dexcom_led_button/app.lock" ] && fail "lock left behind" || pass "lock released"
curl -s -o /dev/null --max-time 2 $URL/status && fail "port still serving" || pass "port freed"

echo "##### 5. Quit button on the page"
launch; wait_up || fail "no relaunch for quit test"
curl -s -X POST $URL/quit -o /dev/null
wait_gone 30 && pass "exited after Quit button (6s grace period)" || fail "still alive after Quit button"
[ -e "$ISO/.dexcom_led_button/app.lock" ] && fail "lock left behind" || pass "lock released"

echo "##### 6. SIGTERM (killall / Activity Monitor)"
launch; wait_up || fail "no relaunch for SIGTERM test"
BEFORE=$(count 'Received SIGTERM'); kill -TERM $(pids | awk '{print $1}')
wait_gone 20 && pass "exited on SIGTERM" || fail "alive after SIGTERM"
[ "$(count 'Received SIGTERM')" -gt "$BEFORE" ] && pass "SIGTERM handler ran" || fail "SIGTERM handler did not run"
[ -e "$ISO/.dexcom_led_button/app.lock" ] && fail "lock left behind" || pass "lock released"

echo "##### final: stray processes"
[ -z "$(pids)" ] && pass "no stray processes" || { fail "stray: $(pids)"; pkill -f "$EXE"; }
rm -rf "$ISO"
