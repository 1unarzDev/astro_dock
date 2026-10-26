#!/usr/bin/env bash

echo "=== Setting up mDNS for system ==="

echo "Determining Linux distro..."
. /etc/os-release

if [[ "$ID" == "arch" || "$ID_LIKE" == *"arch"* ]]; then
    echo "[Arch-based distro] Installing required packages..."

    if ! command -v yay &> /dev/null; then
        echo "Installing AUR helper (yay)..."
        sudo pacman -S --needed --noconfirm base-devel git
        git clone https://aur.archlinux.org/yay.git
        cd yay
        makepkg -si --noconfirm
        cd ..
        rm -rf yay
    fi

    yay -S --noconfirm avahi nss-mdns

    NSS_LINE="hosts: mymachines resolve [!UNAVAIL=return] files myhostname mdns_minimal [NOTFOUND=return] dns"
elif [[ "$ID" == "ubuntu" || "$ID" == "debian" || "$ID_LIKE" == *"debian"* ]]; then
    echo "[Debian-based distro] Installing required packages..."
    sudo apt update
    sudo apt install -y avahi-daemon avahi-utils libnss-mdns

    NSS_LINE="hosts: files mdns4_minimal [NOTFOUND=return] dns"
elif [[ "$ID" == "fedora" || "$ID_LIKE" == *"rhel"* || "$ID_LIKE" == *"fedora"* ]]; then
    echo "[RPM-based distro] Installing required packages..."

    sudo dnf install -y avahi avahi-tools nss-mdns

    NSS_LINE="hosts: files mdns4_minimal [NOTFOUND=return] dns"
else
    echo "Unsupported distro: $ID"
    exit 1
fi

echo "Enabling and starting Avahi daemon..."
sudo systemctl enable --now avahi-daemon

# Create SSH service for Avahi
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

# Backup and update nsswitch.conf
echo "Configuring /etc/nsswitch.conf for mDNS..."
sudo cp /etc/nsswitch.conf /etc/nsswitch.conf.bak
sudo sed -i "/^hosts:/c\\$NSS_LINE" /etc/nsswitch.conf

# Display info
HOSTNAME=$(hostname)
echo ""
echo "======================================"
echo "mDNS setup completed!"
echo "Your device hostname: $HOSTNAME"
echo "You can access this device on your local network via:"
echo "    ssh user@${HOSTNAME}.local"
echo ""
echo "Other devices advertising mDNS services on the network:"
echo "--------------------------------------"

# Format avahi-browse output nicely
avahi-browse -at 2>/dev/null | while read -r line; do
    if [[ "$line" =~ ^\+ ]]; then
        # Split fields: interface, protocol, name, type, domain
        INTERFACE=$(echo "$line" | awk '{print $2}')
        PROTOCOL=$(echo "$line" | awk '{print $3}')
        NAME=$(echo "$line" | awk '{print $4}')
        TYPE=$(echo "$line" | awk '{print $5}')
        DOMAIN=$(echo "$line" | awk '{print $6}')
        printf " - %-20s %-15s %s\n" "$NAME" "$TYPE" "($INTERFACE/$PROTOCOL)"
    fi
done
echo "======================================"