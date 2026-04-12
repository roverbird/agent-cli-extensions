#!/usr/bin/env bash

# This deploys Himalaya mail and sets-up mailo.com mailbox
set -euo pipefail

echo "=== Himalaya + Mailo CLI Setup (Precompiled, Fixed) ==="

# -----------------------------
# Ask for credentials
# -----------------------------
read -rp "Enter your Mailo email address: " MAILO_EMAIL
read -rsp "Enter your Mailo password: " MAILO_PASS
echo

# -----------------------------
# Install dependencies
# -----------------------------
echo "Installing dependencies..."
sudo apt update
sudo apt install -y curl tar

# -----------------------------
# Detect system
# -----------------------------
SYSTEM=$(uname -s | tr '[:upper:]' '[:lower:]')
MACHINE=$(uname -m | tr '[:upper:]' '[:lower:]')

case $SYSTEM in
    linux|freebsd)
        case $MACHINE in
            x86_64) TARGET=x86_64-linux;;
            i386|i686) TARGET=i686-linux;;
            aarch64|arm64) TARGET=aarch64-linux;;
            armv6l) TARGET=armv6l-linux;;
            armv7l) TARGET=armv7l-linux;;
            *) echo "Unsupported architecture $MACHINE"; exit 1;;
        esac;;
    *)
        echo "Unsupported system $SYSTEM"; exit 1;;
esac

# -----------------------------
# Download Himalaya
# -----------------------------
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

echo "Downloading Himalaya binary..."
curl -sLo "$TMPDIR/himalaya.tgz" \
  "https://github.com/pimalaya/himalaya/releases/latest/download/himalaya.$TARGET.tgz"

mkdir -p "$HOME/.local/bin"
tar -xzf "$TMPDIR/himalaya.tgz" -C "$TMPDIR"
cp -f "$TMPDIR/himalaya" "$HOME/.local/bin/himalaya"

# -----------------------------
# Ensure PATH
# -----------------------------
if ! grep -q '.local/bin' "$HOME/.bashrc"; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
fi

export PATH="$HOME/.local/bin:$PATH"

echo "Installed: $(himalaya --version)"

# -----------------------------
# Write config
# -----------------------------
CONFIG_DIR="$HOME/.config/himalaya"
mkdir -p "$CONFIG_DIR"
CONFIG_FILE="$CONFIG_DIR/config.toml"

echo "Writing config to $CONFIG_FILE..."

cat > "$CONFIG_FILE" <<EOF
[accounts.mailo]
default = true

email = "$MAILO_EMAIL"

backend.type = "imap"
backend.host = "mail.mailo.com"
backend.port = 993
backend.encryption.type = "tls"
backend.login = "$MAILO_EMAIL"
backend.auth.type = "password"
backend.auth.raw = "$MAILO_PASS"

message.send.backend.type = "smtp"
message.send.backend.host = "mail.mailo.com"
message.send.backend.port = 465
message.send.backend.encryption.type = "tls"
message.send.backend.login = "$MAILO_EMAIL"
message.send.backend.auth.type = "password"
message.send.backend.auth.raw = "$MAILO_PASS"

folder.aliases.inbox = "INBOX"
folder.aliases.sent = "sent"
folder.aliases.drafts = "drafts"
folder.aliases.trash = "trash"
EOF

# -----------------------------
# Test connection
# -----------------------------
echo "Testing IMAP connection..."
if himalaya folder list > /dev/null 2>&1; then
    echo "✔ Mailbox connection successful"
else
    echo "✖ Connection failed. Run: himalaya --debug folder list"
    exit 1
fi

# -----------------------------
# Done
# -----------------------------
echo
echo "=== Setup complete ==="
echo "List folders:      himalaya folder list"
echo "List emails:       himalaya envelope list INBOX"
echo "Read email:        himalaya message read <ID>"
echo "Send email:"
echo '  echo "body" | himalaya message send --to user@example.com --subject "test"'

#ToDo:
#
#Replace:
#
#backend.auth.raw = "$MAILO_PASS"
#
#With:
#
#backend.auth.cmd = "pass show mailo"
#
#👉 then use pass (Unix password manager)
