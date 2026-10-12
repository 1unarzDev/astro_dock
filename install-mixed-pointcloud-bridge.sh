#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./install-mixed-pointcloud-bridge.sh [--dry-run]

Install the per-user systemd services that relay the LARGE_DATA lidar stream
into the normal DDS graph used by RViz and Nav2.
EOF
}

dry_run=false
while (($#)); do
  case "$1" in
    --dry-run) dry_run=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
unit_dir="$HOME/.config/systemd/user"
env_file="$HOME/.config/astro-pointcloud-bridge.env"

quote_command() {
  printf '  $'
  printf ' %q' "$@"
  printf '\n'
}

run() {
  quote_command "$@"
  $dry_run || "$@"
}

if ! $dry_run && ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl is required for the persistent bridge services." >&2
  exit 1
fi

run mkdir -p "$unit_dir" "${env_file%/*}"
run install -m 0644 "$repo_root/.devcontainer/systemd/astro-pointcloud-ingest.service" "$unit_dir/"
run install -m 0644 "$repo_root/.devcontainer/systemd/astro-pointcloud-publish.service" "$unit_dir/"
if [[ ! -e "$env_file" ]]; then
  run install -m 0644 "$repo_root/.devcontainer/systemd/astro-pointcloud-bridge.env.example" "$env_file"
fi

run systemctl --user daemon-reload
run systemctl --user enable --now astro-pointcloud-publish.service astro-pointcloud-ingest.service

if $dry_run; then
  echo "Enable user lingering after installation so the bridge starts before login:"
  echo "  sudo loginctl enable-linger $USER"
  exit 0
fi

if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || true)" != "yes" ]]; then
  echo "Enable user lingering so the bridge starts before login: sudo loginctl enable-linger $USER" >&2
fi

# The services intentionally retry while the container is stopped or being
# recreated, so a transient inactive state is not an installation failure.
systemctl --user --no-pager --full status \
  astro-pointcloud-publish.service astro-pointcloud-ingest.service || true
