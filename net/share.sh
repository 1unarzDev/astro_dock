#!/usr/bin/env bash
set -euo pipefail

if [[ ${1:-} == -h || ${1:-} == --help ]]; then
    echo "Usage: net/share.sh   create a Samba share and mount it via fstab (asks; sudo). Its HOST.local name: ./setup.linux.sh --components mdns"
    exit 0
fi

echo "=== Network Share Setup ==="

# -------------------------------
# Prompt for variables
# -------------------------------
read -rp "Enter the name of the folder/share to create (default: share): " SHARE_NAME
SHARE_NAME=${SHARE_NAME:-share}

read -rp "Enter the Linux username for Samba (default: $(whoami)): " SMB_USER
SMB_USER=${SMB_USER:-$(whoami)}

read -rsp "Enter the password for the Samba user: " SMB_PASS
echo ""

# -------------------------------
# Determine distro and install packages
# -------------------------------
echo "Determining Linux distro..."
. /etc/os-release

if [[ "$ID" == "arch" || "$ID_LIKE" == *"arch"* ]]; then
    sudo pacman -S --needed --noconfirm samba cifs-utils
elif [[ "$ID" == "ubuntu" || "$ID" == "debian" || "$ID_LIKE" == *"debian"* ]]; then
    sudo apt-get update && sudo apt-get install -y samba cifs-utils
else
    echo "Unsupported distro: $ID"
    exit 1
fi

# -------------------------------
# Create network folder and configure Samba
# -------------------------------
SHARE_PATH="/home/$SMB_USER/$SHARE_NAME"
echo "Creating folder at $SHARE_PATH ..."
mkdir -p "$SHARE_PATH"
chmod 775 "$SHARE_PATH"

echo "Backing up existing Samba config..."
sudo cp /etc/samba/smb.conf /etc/samba/smb.conf.bak 2>/dev/null || true

echo "Writing new Samba config..."
sudo tee /etc/samba/smb.conf > /dev/null <<EOF
[global]
   workgroup = WORKGROUP
   server string = Samba Server
   security = user
   map to guest = Bad User
   server min protocol = SMB2
   disable netbios = yes

[$SHARE_NAME]
   path = $SHARE_PATH
   browsable = yes
   read only = no
   guest ok = no
EOF

# -------------------------------
# Create Samba user
# -------------------------------
echo "Creating Samba user '$SMB_USER' ..."
sudo smbpasswd -a "$SMB_USER" <<<"$SMB_PASS"$'\n'"$SMB_PASS"
sudo smbpasswd -e "$SMB_USER"

# -------------------------------
# Start Samba services
# -------------------------------
echo "Enabling and starting smb.service..."
sudo systemctl enable --now smb

# -------------------------------
# Add to /etc/fstab for auto-mount (CIFS client)
# -------------------------------
MOUNT_POINT="/mnt/$SHARE_NAME"
echo "Creating mount point at $MOUNT_POINT ..."
sudo mkdir -p "$MOUNT_POINT"

# Create credentials file
CRED_FILE="$HOME/.smbcredentials"
echo "Storing credentials in $CRED_FILE ..."
tee "$CRED_FILE" > /dev/null <<EOF
username=$SMB_USER
password=$SMB_PASS
EOF
chmod 600 "$CRED_FILE"

# Add to fstab if not already present
if ! grep -q "$SHARE_NAME" /etc/fstab; then
    echo "Adding $SHARE_NAME to /etc/fstab ..."
    echo "//$(hostname -I | awk '{print $1}')/$SHARE_NAME $MOUNT_POINT cifs credentials=$CRED_FILE,vers=3.0,uid=$(id -u),gid=$(id -g),x-systemd.automount,x-systemd.idle-timeout=60 0 0" | sudo tee -a /etc/fstab
fi

echo "Reloading systemd daemon..."
sudo systemctl daemon-reload
sudo mount -a

# -------------------------------
# Display access info
# -------------------------------
HOSTNAME=$(hostname)
IP_ADDR=$(hostname -I | awk '{print $1}')

echo ""
echo "======================================"
echo "✅ Network folder setup completed!"
echo ""
echo "Folder path: $SHARE_PATH"
echo "Mount point: $MOUNT_POINT"
echo ""
echo "Access this folder on your local network from other devices via:"
echo "  SMB: \\\\$IP_ADDR\\$SHARE_NAME"
echo "  mDNS: \\\\$HOSTNAME.local\\$SHARE_NAME (once ./setup.linux.sh --components mdns ran)"
echo ""
echo "You can also access it locally at $MOUNT_POINT"
echo "======================================"
