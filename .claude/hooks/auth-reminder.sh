#!/usr/bin/env bash
# PreToolUse hook fired before Edit / Write / MultiEdit / NotebookEdit.
# Doesn't block the tool — just injects an authorization-check reminder into
# Claude's context at the exact moment it matters. See behavior.md
# "Investigation and analysis" rule.
cat <<'EOF'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"AUTHORIZATION CHECK before editing files. Re-read the user's MOST RECENT message. Did it contain 'implement', 'fix it', 'do it', 'go ahead', 'make the change', 'code it up', 'ship it', or a clear direct equivalent? If NO — STOP, do not call this tool, reply with analysis and ask first. Reports of unsolved issues ('still broken', 'didn''t work', 'unchanged', 'X is wrong', screenshots showing problems) are NOT authorizations to edit."}}
EOF
