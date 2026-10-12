#!/usr/bin/env bash
set -euo pipefail

unit_dir="$HOME/.config/systemd/user"
env_file="$HOME/.config/astro-pointcloud-bridge.env"
mkdir -p "$unit_dir" "${env_file%/*}"
install -m 0644 .devcontainer/systemd/astro-pointcloud-ingest.service "$unit_dir/"
install -m 0644 .devcontainer/systemd/astro-pointcloud-publish.service "$unit_dir/"
if [[ ! -e "$env_file" ]]; then
  install -m 0644 .devcontainer/systemd/astro-pointcloud-bridge.env.example "$env_file"
fi

systemctl --user daemon-reload
systemctl --user enable --now astro-pointcloud-publish.service astro-pointcloud-ingest.service
if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || true)" != "yes" ]]; then
  echo "Enable user lingering so the bridge starts before login: sudo loginctl enable-linger $USER" >&2
fi
systemctl --user --no-pager --full status \
  astro-pointcloud-publish.service astro-pointcloud-ingest.service
