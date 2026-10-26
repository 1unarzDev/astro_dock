#!/usr/bin/env bash
set -euo pipefail

echo "=== Network Scan Folder & mDNS Setup Script ==="

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
    echo "[Arch-based distro] Installing required packages..."
    sudo pacman -Syu --noconfirm samba cifs-utils avahi nss-mdns

    NSS_LINE="hosts: mymachines resolve [!UNAVAIL=return] files myhostname mdns_minimal [NOTFOUND=return] dns"
elif [[ "$ID" == "ubuntu" || "$ID" == "debian" || "$ID_LIKE" == *"debian"* ]]; then
    echo "[Debian-based distro] Installing required packages..."
    sudo apt update
    sudo apt install -y samba cifs-utils avahi-daemon avahi-utils libnss-mdns

    NSS_LINE="hosts: files mdns4_minimal [NOTFOUND=return] dns"
else
    echo "Unsupported distro: $ID"
    exit 1
fi

# -------------------------------
# Setup Avahi (mDNS)
# -------------------------------
echo "Enabling and starting Avahi daemon..."
sudo systemctl enable --now avahi-daemon

echo "Creating Avahi SSH service..."
sudo mkdir -p /etc/avahi/services
sudo tee /etc/avahi/services/ssh.service > /dev/null <<EOF
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">%h</name>
  <service>
    <type>_ssh._tcp</type>
    <port>22</port>
  </service>
</service-group>
EOF

sudo systemctl restart avahi-daemon

echo "Configuring /etc/nsswitch.conf for mDNS..."
sudo cp /etc/nsswitch.conf /etc/nsswitch.conf.bak
sudo sed -i "/^hosts:/c\\$NSS_LINE" /etc/nsswitch.conf

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
echo "  mDNS: \\\\$HOSTNAME.local\\$SHARE_NAME"
echo ""
echo "You can also access it locally at $MOUNT_POINT"
echo "======================================"
