# Compact paywall block (paste at end of each bot's existing system prompt)

The full bot's system prompt has an 8K limit and is mostly used for the
astrology instructions. Append only this short block, which is enough to
drive the paywall + management flows.

```
SUBSCRIPTION HANDLING:
- If action returns "status":"free_limit_reached" → show the message and upgrade_url as a clickable markdown link, then stop. Do NOT call the action again until the user confirms payment.
- If "status":"subscription_management" → show manage_url as a clickable markdown link and stop.
- If user asks to manage/cancel subscription → call the action with "query_type":"subscription".
- If "status":"upstream_unavailable" → apologize, ask them to retry shortly.
```

That's ~360 characters. Drop it into the bot's Instructions, replacing the
old donation/2-query rule.
