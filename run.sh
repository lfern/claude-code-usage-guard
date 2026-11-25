#!/usr/bin/env bash
# Neutral statusline launcher: runs the executable *.sh files of a folder.
#   run.sh FOLDER
# They all get the same JSON on stdin and run in parallel, each in its own process and
# with a time limit. If one fails or hangs, the others still run. Their output is
# printed in alphabetical order. Failures are saved in FOLDER/last-error.log.
# Always exits 0.
dir=$1
limit=${STATUSLINE_TIMEOUT:-5}   # seconds per script
input=$(cat 2>/dev/null)
tmp=$(mktemp -d 2>/dev/null) || exit 0
trap 'rm -rf "$tmp"' EXIT
trap 'exit 0' TERM INT HUP
printf '%s' "$input" >"$tmp/input"

names=()
for script in "$dir"/*.sh; do
    [ -f "$script" ] && [ -x "$script" ] || continue
    name=${script##*/}
    names+=("$name")
    {
        timeout -k 1 "$limit" "$script" <"$tmp/input" >"$tmp/$name.out" 2>"$tmp/$name.err"
        echo $? >"$tmp/$name.code"
    } 2>/dev/null &
done
wait

errors=
for name in "${names[@]}"; do
    output=$(tr -d '\0' 2>/dev/null <"$tmp/$name.out")
    [ -n "$output" ] && printf '%s\n' "$output"
    code=$(cat "$tmp/$name.code" 2>/dev/null)
    if [ "${code:-1}" != 0 ]; then
        errors+="$(date '+%F %T') $name: code ${code:-?} (124 = took longer than ${limit} s)"$'\n'
        errors+="$(head -c 1000 "$tmp/$name.err" 2>/dev/null | tr -d '\0')"$'\n'
    fi
done
if [ -n "$errors" ]; then
    printf '%s' "$errors" >"$tmp/log" 2>/dev/null && mv -f "$tmp/log" "$dir/last-error.log" 2>/dev/null
fi
exit 0
