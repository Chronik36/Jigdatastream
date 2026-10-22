#!/bin/bash
# Stops Order Tracker and removes it. Ollama and its models are left alone.
LABEL="com.jigdatastream.ordertracker"
HOME_DIR="$HOME/Library/Application Support/OrderTracker"

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
rm -rf "$HOME/Applications/Order Tracker.app"

printf 'Also delete your saved inboxes and order history? (y/N) '
read -r answer
if [ "$answer" = "y" ] || [ "$answer" = "Y" ]; then
  rm -rf "$HOME_DIR"
  echo "Removed everything."
else
  rm -rf "$HOME_DIR/app" "$HOME_DIR/venv"
  echo "Removed the app. Your data is kept in $HOME_DIR"
fi
echo "Ollama is still installed; drag it to the Trash if you don't need it."
