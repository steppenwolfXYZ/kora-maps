#!/usr/bin/env bash
# PreToolUse hook fired before Edit / Write / MultiEdit / NotebookEdit.
# Doesn't block the tool — just injects an authorization-check reminder into
# Claude's context at the exact moment it matters. See behavior.md
# "Investigation and analysis" rule.
cat <<'EOF'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"SILENT AUTHORIZATION CHECK before editing files. This reminder is internal — do NOT mention it, acknowledge it, or write any chat text about authorization. Re-read the user's MOST RECENT message. Did it contain 'implement', 'fix it', 'do it', 'go ahead', 'make the change', 'code it up', 'ship it', or a clear direct equivalent? If YES — proceed with the edit silently. If NO — STOP, do not call this tool, reply with analysis and ask first. Reports of unsolved issues ('still broken', 'didn''t work', 'unchanged', 'X is wrong', screenshots showing problems) are NOT authorizations to edit. Never output the words 'authorization check' or describe this reminder in your reply."}}
EOF
