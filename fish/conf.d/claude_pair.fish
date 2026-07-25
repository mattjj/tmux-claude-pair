# claude-pair shell hook: report each finished command (with exit status and
# duration) to the watcher, giving it a precise "command completed" signal —
# a failed command triggers a suggestion immediately, no debounce guessing.
# Install: copy or symlink into ~/.config/fish/conf.d/

function __claude_pair_preexec --on-event fish_preexec
    set -g __claude_pair_cmd_start (date +%s)
end

function __claude_pair_postexec --on-event fish_postexec
    set -l st $status
    set -l now (date +%s)
    set -l dur 0
    if set -q __claude_pair_cmd_start
        set dur (math "$now - $__claude_pair_cmd_start")
        set -e __claude_pair_cmd_start
    end
    set -l cache_home ~/.cache
    if set -q XDG_CACHE_HOME
        set cache_home $XDG_CACHE_HOME
    end
    set -l dir $cache_home/claude-pair
    mkdir -p $dir
    # format: ts / exit status / duration seconds / command (may be multiline)
    printf '%s\n%s\n%s\n%s\n' $now $st $dur "$argv[1]" >$dir/shell_event
end
