#!/bin/bash
# Frees disk space on the server. Removes ONLY what is safe to remove.
#
#   sudo bash deploy/cleanup.sh          # report: what would go and how much (nothing is deleted)
#   sudo bash deploy/cleanup.sh --yes    # delete exactly that
#
# Removed (only these):
#   - the apt package cache and packages nothing needs any more (old kernels etc.)
#   - the system journal above 200 MB, old rotated logs in /var/log (*.gz, *.1 ...), crash dumps
#   - disabled old snap revisions
#   - pip / npm download caches
#   - browser temp folders left by killed browsers (not used by a running process)
#   - noVNC / Xvfb logs older than 7 days, leftovers of e2e test runs in /tmp
# Never touched: the app database and settings (server/data/app.db, .env), saved logins
# (they are in the database), the code and its .venv, the Camoufox browser
# (~/.cache/camoufox), workspaces of API-department runs, your own files, Docker.
set -u
YES=0; [ "${1:-}" = "--yes" ] && YES=1
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }
SERVER_DIR="$(cd "$(dirname "$0")/../server" && pwd)"
RUN_USER="${SUDO_USER:-$(stat -c %U "$SERVER_DIR")}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
TOTAL=0

human() { numfmt --to=iec --suffix=B "${1:-0}" 2>/dev/null || echo "${1:-0} B"; }
size_of() { local s=0 n; for p in "$@"; do [ -e "$p" ] && { n=$(du -sb "$p" 2>/dev/null | cut -f1); s=$((s + ${n:-0})); }; done; echo "$s"; }
item() {  # item "description" bytes
  printf '  %-62s %10s\n' "$1" "$(human "$2")"
  TOTAL=$((TOTAL + $2))
}
in_use() { pgrep -f -- "$1" >/dev/null 2>&1; }

echo "Disk before:"; df -h / | tail -1
echo
echo "$([ $YES = 1 ] && echo Removing || echo 'Would remove (run with --yes to delete):')"

# 1. apt cache
if command -v apt-get >/dev/null; then
  n=$(size_of /var/cache/apt/archives); item "apt package cache" "$n"
  [ $YES = 1 ] && apt-get clean -qq
  count=$(apt-get -s autoremove 2>/dev/null | grep -c '^Remv ')
  if [ "$count" -gt 0 ]; then
    item "$count packages nothing needs (apt autoremove; size after removal)" 0
    [ $YES = 1 ] && DEBIAN_FRONTEND=noninteractive apt-get autoremove --purge -y -qq >/dev/null
  fi
fi

# 2. journal above 200 MB
if command -v journalctl >/dev/null; then
  used=$(du -sb /var/log/journal 2>/dev/null | cut -f1); used=${used:-0}
  limit=$((200 * 1024 * 1024))
  [ "$used" -gt "$limit" ] && item "system journal above 200 MB" $((used - limit))
  [ $YES = 1 ] && journalctl --vacuum-size=200M >/dev/null 2>&1
fi

# 3. rotated logs and crash dumps
mapfile -t OLD_LOGS < <(find /var/log -type f \( -name '*.gz' -o -name '*.xz' -o -regex '.*\.[0-9]+$' \) 2>/dev/null)
item "old rotated logs in /var/log (${#OLD_LOGS[@]} files)" "$(size_of "${OLD_LOGS[@]}")"
[ $YES = 1 ] && [ ${#OLD_LOGS[@]} -gt 0 ] && rm -f -- "${OLD_LOGS[@]}"
mapfile -t DUMPS < <(find /var/crash /var/lib/systemd/coredump -mindepth 1 -maxdepth 1 2>/dev/null)
item "crash dumps" "$(size_of "${DUMPS[@]}")"
[ $YES = 1 ] && [ ${#DUMPS[@]} -gt 0 ] && rm -rf -- "${DUMPS[@]}"

# 4. disabled snap revisions
if command -v snap >/dev/null; then
  mapfile -t SNAPS < <(snap list --all 2>/dev/null | awk '/disabled/{print $1" "$3}')
  if [ ${#SNAPS[@]} -gt 0 ]; then
    s=0; for x in "${SNAPS[@]}"; do set -- $x; s=$((s + $(size_of "/var/lib/snapd/snaps/$1_$2.snap"))); done
    item "old snap revisions (${#SNAPS[@]})" "$s"
    if [ $YES = 1 ]; then for x in "${SNAPS[@]}"; do set -- $x; snap remove "$1" --revision="$2" >/dev/null 2>&1; done; fi
  fi
fi

# 5. download caches (they are re-downloaded when needed)
CACHES=("$RUN_HOME/.cache/pip" /root/.cache/pip "$RUN_HOME/.npm/_cacache" /root/.npm/_cacache)
item "pip / npm download caches" "$(size_of "${CACHES[@]}")"
[ $YES = 1 ] && rm -rf -- "${CACHES[@]}"

# 6. browser temp leftovers (only folders no running process uses, older than an hour)
TMPS=()
for dir in "$SERVER_DIR/data/tmp" /tmp; do
  [ -d "$dir" ] || continue
  while IFS= read -r p; do
    in_use "$p" || TMPS+=("$p")
  done < <(find "$dir" -mindepth 1 -maxdepth 1 -user "$RUN_USER" -mmin +60 \( -name 'playwright*' -o \
           -name 'rust_mozprofile*' -o -name '*camoufox*' -o -name 'tmp*_profile*' -o -name 'mozprofile*' -o \
           -name 'aidd-test-*' -o -name 'aidd-pool-*' \) 2>/dev/null)
done
[ -d /tmp/aidd-e2e ] && ! in_use /tmp/aidd-e2e && TMPS+=(/tmp/aidd-e2e)
item "leftover browser / test temp folders (${#TMPS[@]})" "$(size_of "${TMPS[@]}")"
[ $YES = 1 ] && [ ${#TMPS[@]} -gt 0 ] && rm -rf -- "${TMPS[@]}"

# 7. old noVNC / Xvfb logs of the app
mapfile -t VNCLOGS < <(find "$SERVER_DIR/data/vnc/logs" -type f -name '*.log' -mtime +7 2>/dev/null)
item "noVNC / Xvfb logs older than 7 days" "$(size_of "${VNCLOGS[@]}")"
[ $YES = 1 ] && [ ${#VNCLOGS[@]} -gt 0 ] && rm -f -- "${VNCLOGS[@]}"

echo
echo "  total: $(human $TOTAL)"
echo
if [ $YES = 1 ]; then
  echo "Disk after:"; df -h / | tail -1
else
  echo "For your decision (not deleted by this script) - the biggest folders:"
  du -xh --max-depth=2 / 2>/dev/null | sort -rh | head -12 | sed 's/^/  /'
  if [ -d "$SERVER_DIR/data/workspaces" ]; then
    echo "  API-department run folders: $(du -sh "$SERVER_DIR/data/workspaces" 2>/dev/null | cut -f1)" \
         "(delete old runs in the app: Отдел -> Запуски)"
  fi
  command -v docker >/dev/null && echo "  Docker: see 'docker system df' (not touched)"
fi
