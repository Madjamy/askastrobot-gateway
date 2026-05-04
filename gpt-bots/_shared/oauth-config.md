# GPT-Builder OAuth configuration (same values for all 4 bots)

In ChatGPT → Edit GPT → Configure → Actions → edit the action → Authentication → choose **OAuth**.

| Field | Value |
|---|---|
| Client ID | `<OAUTH_CLIENT_ID>` (from the gateway's .env) |
| Client Secret | `<OAUTH_CLIENT_SECRET>` (from the gateway's .env) |
| Authorization URL | `https://api.askastrobot.com/oauth/authorize` |
| Token URL | `https://api.askastrobot.com/oauth/token` |
| Scope | `read:astro write:query` |
| Token Exchange Method | **Default (POST request)** |

After saving, ChatGPT shows a redirect URI like
`https://chat.openai.com/aip/g-XXXXXXXX/oauth/callback`. **No manual allowlist
update is needed** — the gateway accepts any URI matching the canonical regex
`^https://(chat\.openai\.com|chatgpt\.com)/aip/g-[A-Za-z0-9_-]+/oauth/callback$`.

## Per-bot fields that DO change

| Bot | Schema servers.url |
|---|---|
| Prashna | `https://api.askastrobot.com/v1/gpt/prashna/query` |
| Horoscope | `https://api.askastrobot.com/v1/gpt/horoscope/query` |
| Career | `https://api.askastrobot.com/v1/gpt/career/query` |
| Marriage | `https://api.askastrobot.com/v1/gpt/marriage/query` |

The full OpenAPI schema for each bot is in `gpt-bots/<bot>/openapi.json`.
