#!/usr/bin/env bash
# Common runtime identity and shell contract for both JetPack generations.
set -euo pipefail
username="${1:-roboboat}"
uid="${2:-1000}"
gid="${3:-1000}"
existing_user="$(getent passwd "$uid" | cut -d: -f1 || true)"
existing_group="$(getent group "$gid" | cut -d: -f1 || true)"
if [[ -n "$existing_user" && "$existing_user" != "$username" ]]; then userdel "$existing_user"; fi
if [[ -n "$existing_group" && "$existing_group" != "$username" ]]; then groupdel "$existing_group"; fi
getent group "$username" >/dev/null || groupadd --gid "$gid" "$username"
id "$username" >/dev/null 2>&1 || useradd --uid "$uid" --gid "$gid" --create-home --shell /bin/bash "$username"
usermod -aG dialout,video,sudo "$username"
/usr/local/sbin/configure-zed-access "$username"
printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$username" > "/etc/sudoers.d/$username"
chmod 0440 "/etc/sudoers.d/$username"
touch "/home/$username/.sudo_as_admin_successful"
mkdir -p "${ROS_WS}" /workspace
printf '%s\n' \
    'source /opt/ros/${ROS_DISTRO}/setup.bash' \
    'for overlay in /opt/zed_ros2 /opt/velodyne /opt/astro-extra; do' \
    '  if [[ -f "$overlay/setup.bash" ]]; then source "$overlay/setup.bash"; fi' \
    'done' \
    'help() { cat "$HOME/.helper.txt"; }' >> "/home/$username/.bashrc"
chown -R "$username:$username" /workspace "/home/$username"
