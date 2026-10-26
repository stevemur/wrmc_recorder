#!/bin/bash
# Installs the WRMC recorder for Avery's shows.
# Each day it reads https://wrmc.middlebury.edu/schedule-weekly, finds
# every show that Avery hosts, and sets a launchd event to record it.
# It also sets a Mac alert and an Apple Reminder 12 hours before each show.
# Recordings go to ~/Music/WRMC as .m4a files.
#
# Install:        bash install_wrmc_recorder.sh
# Next show:      wrmc next      (after you open a new Terminal window)
# See schedule:   wrmc list
# Test 30 sec:    wrmc test
# Remove:         wrmc uninstall

set -e
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
APP_DIR="$HOME/Library/Application Support/WRMC"
AGENTS="$HOME/Library/LaunchAgents"
UPDATE_LABEL="com.murch.wrmc.update"

# 1. Python 3 (comes with the Xcode command line tools).
if ! xcode-select -p >/dev/null 2>&1; then
  echo "Please install the Apple command line tools first:"
  echo "    xcode-select --install"
  echo "Then run this script again."
  exit 1
fi
PY="$(command -v python3)"

# 2. ffmpeg.
if ! command -v ffmpeg >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "Installing ffmpeg with Homebrew..."
    brew install ffmpeg
  else
    echo "Please install Homebrew from https://brew.sh, then run this script again."
    exit 1
  fi
fi

# 3. Remove the older fixed-time version, if it is installed.
launchctl bootout "gui/$(id -u)/com.murch.wrmc-recorder" 2>/dev/null || true
rm -f "$AGENTS/com.murch.wrmc-recorder.plist" "$HOME/Library/Scripts/wrmc_record.sh"

# 4. Install the recorder program.
mkdir -p "$APP_DIR" "$AGENTS" "$HOME/Music/WRMC"
# Copy wrmc.py from the same folder as this installer.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -f "$SCRIPT_DIR/wrmc.py" ]; then
  echo "wrmc.py must be in the same folder as this installer ($SCRIPT_DIR)."
  exit 1
fi
cp "$SCRIPT_DIR/wrmc.py" "$APP_DIR/wrmc.py"
chmod +x "$APP_DIR/wrmc.py"

# 5. A short "wrmc" command for Terminal.
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/wrmc" <<EOF
#!/bin/bash
exec "$PY" "$APP_DIR/wrmc.py" "\$@"
EOF
chmod +x "$HOME/.local/bin/wrmc"
for RC in "$HOME/.zshrc" "$HOME/.bash_profile"; do
  if [ -f "$RC" ] || [ "$RC" = "$HOME/.zshrc" ]; then
    grep -q '.local/bin' "$RC" 2>/dev/null || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$RC"
  fi
done

# 6. Daily schedule check at 06:07, and at each login.
cat > "$AGENTS/$UPDATE_LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$UPDATE_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$APP_DIR/wrmc.py</string>
    <string>update</string>
    <string>--quiet</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>6</integer><key>Minute</key><integer>7</integer></dict>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$HOME/Music/WRMC/recorder.log</string>
  <key>StandardErrorPath</key><string>$HOME/Music/WRMC/recorder.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$UPDATE_LABEL" 2>/dev/null || true

# 7. Read the schedule now (before the daily job starts, so they do not overlap).
echo ""
echo "If macOS asks to let Terminal or python3 control Reminders, click OK."
"$PY" "$APP_DIR/wrmc.py" update || true

# 8. Turn on the daily job.
launchctl bootstrap "gui/$(id -u)" "$AGENTS/$UPDATE_LABEL.plist"

echo ""
echo "Installed. These shows are set to record:"
"$PY" "$APP_DIR/wrmc.py" list
echo ""
echo "Recordings go to ~/Music/WRMC"
echo "To test now (30 seconds): open a new Terminal window and type:  wrmc test"
