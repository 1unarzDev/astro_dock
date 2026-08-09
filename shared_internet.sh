#!/usr/bin/env bash

echo "[INFO] Detecting interface with internet access..."

# Function to detect internet-facing interface
detect_internet_interface() {
    for iface in $(ls /sys/class/net | grep -v lo); do
        # Bring interface up temporarily
        sudo ip link set "$iface" up

        # Test connectivity
        if ping -c 1 -W 1 1.1.1.1 -I "$iface" &> /dev/null; then
            echo "$iface"
            return
        fi
    done
}

INET_IFACE=$(detect_internet_interface)

if [ -z "$INET_IFACE" ]; then
    echo "[ERROR] No internet interface detected!"
    exit 1
fi

echo "[INFO] Internet interface detected: $INET_IFACE"

# Bring interface up and set higher priority (lower metric)
sudo ip link set "$INET_IFACE" up
sudo ip route replace default dev "$INET_IFACE" metric 100

echo "[INFO] Configuring IP forwarding and NAT for boat subnet..."

# Enable IP forwarding
sudo sysctl -w net.ipv4.ip_forward=1

# NAT LAN -> internet
sudo iptables -t nat -A POSTROUTING -s 192.168.0.0/24 -o "$INET_IFACE" -j MASQUERADE

# Forward LAN to internet
sudo iptables -A FORWARD -s 192.168.0.0/24 -o "$INET_IFACE" -j ACCEPT
sudo iptables -A FORWARD -d 192.168.0.0/24 -m state --state ESTABLISHED,RELATED -i "$INET_IFACE" -j ACCEPT

# Persist rules
sudo iptables-save | sudo tee /etc/iptables/iptables.rules
sudo systemctl enable iptables
sudo systemctl start iptables

echo "[INFO] Network setup complete!"
