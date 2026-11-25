#!/usr/bin/env bash
# usage-guard: sensor. Reads the statusline JSON from stdin and saves the usage limits
# to $CLAUDE_CONFIG_DIR/usage-guard/state.json. Never fails (always exits 0).
#
# Usage:  statusline.sh           saves the state and prints the line
#         statusline.sh --sensor  only saves the state
#         statusline.sh --line    only prints the line

input=$(cat 2>/dev/null)
config_dir=${CLAUDE_CONFIG_DIR:-$HOME/.claude}
guard_dir=$config_dir/usage-guard
now=$(date +%s)

# Atomic write: temporary file in the same directory + rename.
write_atomic() { # $1 target, $2 content
    local tmp
    tmp=$(mktemp "$1.XXXXXX" 2>/dev/null) || return 0
    if printf '%s\n' "$2" >"$tmp" 2>/dev/null; then
        mv -f "$tmp" "$1" 2>/dev/null || rm -f "$tmp" 2>/dev/null
    else
        rm -f "$tmp" 2>/dev/null
    fi
}

save_state() {
    local state session print seen
    state=$(printf '%s' "$input" | jq -c --argjson now "$now" '
        (.rate_limits // empty) | objects
        | {seven_day_pct: (.seven_day.used_percentage? // null),
           seven_day_resets_at: (.seven_day.resets_at? // null),
           five_hour_pct: (.five_hour.used_percentage? // null),
           five_hour_resets_at: (.five_hour.resets_at? // null),
           read_at: $now}
        | select(.seven_day_pct != null or .five_hour_pct != null)' 2>/dev/null)
    mkdir -p "$guard_dir/sessions" 2>/dev/null || return 0

    # Claude Code also repaints the statusline without having called the API (mode
    # change, idle session...). That reading is old: it is only saved if the session
    # has used API time since the last one. The first time a session is seen it is
    # only noted down, because it may have been idle for hours.
    session=$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null | tr -cd 'A-Za-z0-9_-')
    print=$(printf '%s' "$input" | jq -c '(.cost.total_api_duration_ms // empty) as $d
        | [$d, .rate_limits]' 2>/dev/null)
    if [ -n "$session" ] && [ -n "$print" ]; then
        seen=$(cat "$guard_dir/sessions/$session" 2>/dev/null)
        [ "$seen" = "$print" ] && return 0
        write_atomic "$guard_dir/sessions/$session" "$print"
        if [ -z "$seen" ]; then
            find "$guard_dir/sessions" -type f -mtime +2 -delete 2>/dev/null
            return 0
        fi
    fi
    [ -n "$state" ] || return 0
    write_atomic "$guard_dir/state.json" "$state"
}

[ "$1" != "--line" ] && command -v jq >/dev/null 2>&1 && save_state
[ "$1" = "--sensor" ] && exit 0

pct() { # $1 jq filter; prints the rounded integer or "--"
    local value
    value=$(printf '%s' "$input" | jq -r "($1 // empty) | numbers | round" 2>/dev/null)
    printf '%s' "${value:---}"
}
# today.json is written by the daemon; only valid if it is from today.
today=$(jq -r --argjson now "$now" 'select(.valid_until > $now)
    | "\(.spent | round)/\(.budget | round)"' "$guard_dir/today.json" 2>/dev/null)
printf '5h:%s%% 7d:%s%% today:%s%%' \
    "$(pct '.rate_limits.five_hour.used_percentage?')" \
    "$(pct '.rate_limits.seven_day.used_percentage?')" "${today:---/--}"
exit 0
