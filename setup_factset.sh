#!/usr/bin/env bash
# Set up FactSet credentials and verify the connection.
#
#   ./setup_factset.sh
#
# Prompts for the Client ID and Secret shown on developer.factset.com, writes
# them to ~/.factset/config.json with owner-only permissions, and runs the
# connection check. That path is one the client searches by default, so no
# environment variables and no shell profile edits are needed — it keeps
# working in every new terminal window.
#
# The secret is read without echoing and never reaches your shell history.

set -euo pipefail

CONFIG_DIR="${HOME}/.factset"
CONFIG_PATH="${CONFIG_DIR}/config.json"
WELL_KNOWN="https://auth.factset.com/.well-known/openid-configuration"

say()  { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

PY="$(command -v python3 || command -v python || true)"
[ -n "$PY" ] || fail "python3 not found. Install it, then re-run this script."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIENT="${SCRIPT_DIR}/factset_client.py"
[ -f "$CLIENT" ] || fail "factset_client.py not found next to this script."

say "FactSet setup"
say ""
say "From developer.factset.com -> API Authentication, open your application."
say "You need two values: the Client ID, and the Secret shown when you created it."
say ""

if [ -f "$CONFIG_PATH" ]; then
  say "A config already exists at ${CONFIG_PATH}."
  printf 'Replace it? [y/N] '
  read -r reply
  case "$reply" in
    [yY]*) ;;
    *) say "Left it alone. Nothing changed."; exit 0 ;;
  esac
  say ""
fi

printf 'Client ID: '
read -r CLIENT_ID
[ -n "$CLIENT_ID" ] || fail "No Client ID entered."

# -s keeps the secret off the screen; it is never echoed or stored in history.
printf 'Secret (not shown as you type or paste): '
read -rs CLIENT_SECRET
printf '\n\n'
[ -n "$CLIENT_SECRET" ] || fail "No Secret entered."

case "$CLIENT_SECRET" in
  '<'*'>'|'PASTE_'*)
    fail "That looks like a placeholder, not a real secret. Paste the value from the portal." ;;
esac

mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

# Write via python so the values are escaped correctly whatever they contain.
umask 077
CLIENT_ID="$CLIENT_ID" CLIENT_SECRET="$CLIENT_SECRET" WELL_KNOWN="$WELL_KNOWN" \
  "$PY" - "$CONFIG_PATH" <<'PYEOF'
import json, os, sys
config = {
    "name": "Ophir scanner",
    "clientId": os.environ["CLIENT_ID"],
    "clientSecret": os.environ["CLIENT_SECRET"],
    "clientAuthType": "Confidential Client Application - Machine Authorization",
    "wellKnownUri": os.environ["WELL_KNOWN"],
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(config, handle, indent=2)
    handle.write("\n")
PYEOF
chmod 600 "$CONFIG_PATH"
unset CLIENT_SECRET

say "Wrote ${CONFIG_PATH} (readable only by you)."
say ""

if ! "$PY" -c 'import pandas, requests' >/dev/null 2>&1; then
  say "Installing pandas and requests..."
  "$PY" -m pip install --quiet --disable-pip-version-check pandas requests \
    || fail "Could not install dependencies. Try: $PY -m pip install pandas requests"
  say ""
fi

say "Running the connection check..."
say ""
exec "$PY" "$CLIENT" --check
