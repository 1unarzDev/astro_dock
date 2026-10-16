#!/usr/bin/env bash
# Image build step shared by every Astro image: the runtime user, its device and
# sudo access, and a shell that loads dev.bashrc however the container started.
set -euo pipefail
username="${1:-roboboat}"
uid="${2:-1000}"
gid="${3:-1000}"
existing_user="$(getent passwd "$uid" | cut -d: -f1 || true)"
if [[ -n "$existing_user" && "$existing_user" != "$username" ]]; then userdel "$existing_user"; fi
# Look up the group only now: userdel can remove the old user's group with it.
existing_group="$(getent group "$gid" | cut -d: -f1 || true)"
if [[ -n "$existing_group" && "$existing_group" != "$username" ]]; then groupdel "$existing_group"; fi
getent group "$username" >/dev/null || groupadd --gid "$gid" "$username"
id "$username" >/dev/null 2>&1 || useradd --uid "$uid" --gid "$gid" --create-home --shell /bin/bash "$username"
usermod -aG dialout,video,sudo "$username"
/usr/local/sbin/configure-zed-access "$username"
printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$username" > "/etc/sudoers.d/$username"
chmod 0440 "/etc/sudoers.d/$username"
touch "/home/$username/.sudo_as_admin_successful"
mkdir -p "${ROS_WS}" /workspace
# The mounted workspace copy wins so edits apply without a rebuild; the copy
# baked into the image covers containers started without the repository.
printf '%s\n' \
    'astro_bashrc="${ROS_WS:-$HOME/roboboat_ws}/.devcontainer/dev.bashrc"' \
    '[[ -f "$astro_bashrc" ]] || astro_bashrc=/etc/astro/dev.bashrc' \
    'source "$astro_bashrc"; unset astro_bashrc' >> "/home/$username/.bashrc"
chown -R "$username:$username" /workspace "/home/$username"
