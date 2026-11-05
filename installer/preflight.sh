#!/usr/bin/env bash
# Source from install.sh. Refuse preexisting installation paths that could redirect root writes.
prw_safe_parent() {
  local path=$1 part owner mode
  part=$(dirname -- "$path")
  while :; do
    [[ -d $part && ! -L $part ]] || { printf 'Unsafe installation parent: %s\n' "$part" >&2; return 1; }
    owner=$(stat -c %u -- "$part") || return 1
    mode=$(stat -c %a -- "$part") || return 1
    # Permission digits may include a sticky/setgid prefix. Inspect actual mode bits.
    (( owner == 0 && (8#$mode & 0022) == 0 )) || { printf 'Installation parent must be root-owned and not group/other writable: %s\n' "$part" >&2; return 1; }
    [[ $part == / ]] && break
    part=$(dirname -- "$part")
  done
}

prw_safe_target_type() {
  local path=$1 owner mode
  if [[ -e $path || -L $path ]]; then
    [[ ! -L $path && -f $path ]] || { printf 'Installation target must be a regular file: %s\n' "$path" >&2; return 1; }
    owner=$(stat -c %u -- "$path") || return 1
    mode=$(stat -c %a -- "$path") || return 1
    (( owner == 0 && (8#$mode & 0022) == 0 )) || { printf 'Installation target must be root-owned and not group/other writable: %s\n' "$path" >&2; return 1; }
  fi
}

prw_safe_target() {
  prw_safe_parent "$1" && prw_safe_target_type "$1"
}

prw_safe_config() {
  local path=$1 mode group
  prw_safe_target "$path" || return 1
  if [[ -e $path ]]; then
    mode=$(stat -c %a -- "$path") || return 1
    group=$(stat -c %g -- "$path") || return 1
    (( (8#$mode & 0007) == 0 && ((8#$mode & 0070) == 0 || group == 0) )) || { printf 'Configuration must be private or readable only by the root group: %s\n' "$path" >&2; return 1; }
  fi
}

prw_safe_directory() {
  local path=$1 owner mode
  prw_safe_parent "$path" || return 1
  if [[ -e $path || -L $path ]]; then
    [[ ! -L $path && -d $path ]] || { printf 'Installation directory must be a real directory: %s\n' "$path" >&2; return 1; }
    owner=$(stat -c %u -- "$path") || return 1
    mode=$(stat -c %a -- "$path") || return 1
    (( owner == 0 && (8#$mode & 0022) == 0 )) || { printf 'Installation directory must be root-owned and not group/other writable: %s\n' "$path" >&2; return 1; }
  fi
}
