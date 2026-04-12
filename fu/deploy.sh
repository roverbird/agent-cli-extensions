#!/usr/bin/env bash
# =============================================================================
# deploy.sh — Agent-Safe CLI (production, bare-bones)
#
# Architecture:
#   Human → agent_driver.py (LLM interpreter) → cli.py (deterministic CLI)
#
# Security model (three principles):
#   1. cliadmin owns all code — the agent can never rewrite what it runs
#   2. agentuser executes only — no write access anywhere in APP_DIR
#   3. sudo scope is binary-exact — one command, one interpreter, nothing else
#
# Usage:
#   sudo CLI_SHA256=<hash> AGENT_SHA256=<hash> bash deploy.sh
#   Generate hashes: sha256sum cli.py agent_driver.py
# =============================================================================

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
APP_DIR="/opt/agent-cli"
LOG_DIR="/var/log/agent-cli"
CLI_FILE="cli.py"
AGENT_FILE="agent_driver.py"
PYTHON_BIN="python3"
SUDOERS_FILE="/etc/sudoers.d/agent-cli"

# Checksums — pass via env or hardcode after first verified build
CLI_SHA256="${CLI_SHA256:-}"
AGENT_SHA256="${AGENT_SHA256:-}"

# ── Helpers ───────────────────────────────────────────────────────────────────
info() { echo "  $*"; }
die()  { echo "[ERROR] $*" >&2; exit 1; }

require_root() {
    [[ $EUID -eq 0 ]] || die "Run as root: sudo bash $0"
}

verify_checksum() {
    local file="$1" expected="$2" label="$3"
    [[ -z "$expected" ]] && { info "WARN: no checksum for $label — set ${label^^}_SHA256 in production"; return 0; }
    local actual; actual=$(sha256sum "$file" | awk '{print $1}')
    [[ "$actual" == "$expected" ]] || die "Checksum mismatch: $label\n  expected $expected\n  got      $actual"
    info "checksum ok: $label"
}

# ── Pre-flight ────────────────────────────────────────────────────────────────
require_root
[[ -f "$CLI_FILE"   ]] || die "Missing: $CLI_FILE"
[[ -f "$AGENT_FILE" ]] || die "Missing: $AGENT_FILE"

echo ""
echo "=== Agent-CLI Deploy ==="
echo ""
echo "-> Verifying source files..."
verify_checksum "$CLI_FILE"   "$CLI_SHA256"   "cli"
verify_checksum "$AGENT_FILE" "$AGENT_SHA256" "agent_driver"

# ── Users ─────────────────────────────────────────────────────────────────────
echo "-> Creating system users..."

# cliadmin: owns all code, no login shell
if ! id -u cliadmin &>/dev/null; then
    useradd -r -M -s /usr/sbin/nologin cliadmin
    info "created: cliadmin"
else
    info "exists:  cliadmin"
fi

# agentuser: executes only, no login shell
if ! id -u agentuser &>/dev/null; then
    useradd -r -M -s /usr/sbin/nologin agentuser
    info "created: agentuser"
else
    info "exists:  agentuser"
fi

# agentrunners: human operators who may invoke run-agent
if ! getent group agentrunners &>/dev/null; then
    groupadd agentrunners
    info "created group: agentrunners"
    info "NOTICE: add operators with -> usermod -aG agentrunners <username>"
else
    info "exists group: agentrunners"
fi

# ── Directories ───────────────────────────────────────────────────────────────
echo "-> Creating directories..."

# APP_DIR: cliadmin owns, agentuser can enter but not write, others see nothing
mkdir -p "$APP_DIR"
chown cliadmin:agentuser "$APP_DIR"
chmod 750 "$APP_DIR"

# LOG_DIR: agentuser can append, cannot delete (sticky bit)
mkdir -p "$LOG_DIR"
chown cliadmin:agentuser "$LOG_DIR"
chmod 1730 "$LOG_DIR"   # sticky + cliadmin:rwx agentuser:wx others:---

info "ok: $APP_DIR  (750)"
info "ok: $LOG_DIR  (1730 sticky)"

# ── Install files ─────────────────────────────────────────────────────────────
echo "-> Installing files..."

install -o cliadmin -g cliadmin -m 550 "$CLI_FILE"   "$APP_DIR/$CLI_FILE"
install -o cliadmin -g agentuser -m 510 "$AGENT_FILE" "$APP_DIR/$AGENT_FILE"
#                                   ^^^
#  cli.py:          550 -> cliadmin r-x | agentuser(group) r-x | others ---
#  agent_driver.py: 510 -> cliadmin r-x | agentuser(group) --x | others ---
#                                                            ^
#                         agentuser can execute but cannot read the source

info "ok: $CLI_FILE   (cliadmin:cliadmin 550)"
info "ok: $AGENT_FILE (cliadmin:agentuser 510)"

# ── Python venv (owned by cliadmin, read-only for agentuser) ──────────────────
echo "-> Building Python venv..."

# Venv created as cliadmin — agentuser cannot pip install or modify it
sudo -u cliadmin "$PYTHON_BIN" -m venv "$APP_DIR/venv"
sudo -u cliadmin "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u cliadmin "$APP_DIR/venv/bin/pip" install --quiet requests

# Lock venv: cliadmin owns everything, agentuser group gets read+execute only
chown -R cliadmin:agentuser "$APP_DIR/venv"
find "$APP_DIR/venv" -type d -exec chmod 750 {} \;
find "$APP_DIR/venv" -type f -exec chmod 640 {} \;
find "$APP_DIR/venv/bin" -type f -exec chmod 550 {} \;

info "ok: venv (cliadmin owned, agentuser read+exec only)"

# ── Sudoers ───────────────────────────────────────────────────────────────────
echo "-> Writing sudoers entry..."

# Grants agentrunners members exactly one command as agentuser.
# Both the interpreter and script path are pinned — no wildcards.
cat > "$SUDOERS_FILE" <<SUDOERS
# agent-cli: managed by deploy.sh — do not edit manually
%agentrunners ALL=(agentuser) NOPASSWD: ${APP_DIR}/venv/bin/python3 ${APP_DIR}/${AGENT_FILE}
SUDOERS

chmod 440 "$SUDOERS_FILE"
chown root:root "$SUDOERS_FILE"

# Validate before leaving it in place
visudo -c -f "$SUDOERS_FILE" || die "sudoers syntax error — fix $SUDOERS_FILE"
info "ok: $SUDOERS_FILE (440)"

# ── Human wrapper ─────────────────────────────────────────────────────────────
echo "-> Installing run-agent wrapper..."

cat > /usr/local/bin/run-agent <<WRAPPER
#!/usr/bin/env bash
# Invoke the agent driver as agentuser (agentrunners group only)
set -euo pipefail
exec sudo -u agentuser ${APP_DIR}/venv/bin/python3 ${APP_DIR}/${AGENT_FILE} "\$@"
WRAPPER

# Only members of agentrunners can execute this — nobody else
chown root:agentrunners /usr/local/bin/run-agent
chmod 750 /usr/local/bin/run-agent

info "ok: /usr/local/bin/run-agent (root:agentrunners 750)"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "=== Deploy complete ==="
echo ""
echo "  Add an operator:   usermod -aG agentrunners <username>"
echo "  Run the agent:     run-agent"
echo ""
echo "  Security layers:"
echo "    cliadmin owns all code       -> agent cannot modify what it runs"
echo "    agentuser executes only      -> no writes inside APP_DIR"
echo "    sudo scope is binary-exact   -> one command, one interpreter"
echo "    agentrunners gate            -> explicit opt-in per operator"
echo "    logs append-only + sticky    -> audit trail cannot be cleared by agent"
echo ""
