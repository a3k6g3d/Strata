# Strata on Discord

Chat with your model, and let it use its tools, from the Discord app on your phone - similar to Claude's remote sessions.
It is optional and off until you set it up. Everything runs on your PC: the bot only relays messages between Discord and your own
Strata server.

## Set up (about 5 minutes)

1. Go to <https://discord.com/developers/applications>, **New Application**, then **Bot**. Click **Reset Token** and copy the token.
   The token is a password: anyone with it controls your bot. Do not paste it into a chat or a config file.
2. Save the token as the first line of a file next to your Strata config, for example `J:\Strata-work\discord-token.txt`
   (or set the environment variable `STRATA_DISCORD_TOKEN`).
3. In Discord, turn on **Settings > Advanced > Developer Mode**, right-click your own name and **Copy User ID**.
4. **OAuth2 > URL Generator**: scope `bot`, no permissions needed. Open the generated link and add the bot to a private server of
   yours (a server with only you in it is fine). Discord only lets you message a bot that shares a server with you.
5. Add this to the Strata config (the file passed with `--config`) and restart Strata:

```json
"discord": {
  "enabled": true,
  "allowed_user_ids": [123456789012345678],
  "token_file": "discord-token.txt",
  "permission": "ask",
  "thinking": "medium"
}
```

6. Open a direct message with the bot and write to it. `!help` lists the commands.

## What it does

- **Only the accounts in `allowed_user_ids` get an answer.** Everyone else is ignored. An empty list starts nothing.
- Each DM (or allowed channel) is one conversation, kept in a `discord` folder next to the config. `!new` starts a fresh one.
- The answer is written into one message as it streams; long answers continue in further messages. Pictures you attach go to the
  model (up to 8 MB each; the vision encoder must be configured). Text and code files (`.txt`, `.md`, `.py`, `.json`, `.csv`, `.log` and
  similar, up to 200 KB each and 120,000 characters per message) are read into the prompt. Anything else (video, audio, PDFs, bigger files)
  is named back to you with the reason, and the model is told it was skipped. The bot sends text only: it cannot attach a picture or a
  file to a reply.
- **Tools never go above `ask`:** looking at files, searching and browsing run by themselves; anything that changes something, and
  every command, shows **Allow / Deny** buttons in Discord (only your account can press them). The protected Windows folders and the
  no-read list from the Strata config apply exactly as in the web app. `edit` and `full` access are not available from Discord.
- If the model asks you something (the `ask_user` tool) you answer with a button or by replying.

## Chats are shared with the Strata app

A Discord conversation is saved as a chat of the Strata app, titled "Discord · ...": it shows up in the app's chat list within a few seconds
and opens there like any other chat. You can continue it from either side: the bot always starts from what the app has saved, so what you added
in the app is part of the next Discord answer, and the app reloads a chat that the bot continued (while you are not typing an answer there).
`!new` starts a fresh chat (the old one stays in the list). `!chats` lists your latest chats and `!open 2` makes the channel continue one of them,
including a chat you started in the app. Continue on one side at a time: both save the whole chat, so answering in both at once can overwrite one.

## Commands

| | |
|---|---|
| `!new` | start a new conversation (the old one stays in the Strata app) |
| `!chats` | your latest chats (the same list as the Strata app) |
| `!open N` | continue chat number N from `!chats` here |
| `!stop` | stop the answer that is being written |
| `!status` | model, last speed, memory headroom, this chat's settings |
| `!mode off\|read\|ask` | tools: none / look only / look and ask before changes |
| `!think off\|low\|medium\|high` | how long the model thinks |

## Server channels (optional)

To also answer in a channel of your server, list its id in `"channel_ids": [...]` and switch on **Message Content Intent** under
**Bot > Privileged Gateway Intents** in the Developer Portal. In channels the bot answers only when you mention it
(`"require_mention": false` removes that). Direct messages need no extra switch.

## If it does not start

The Strata console prints one line starting with `[strata] Discord:` saying why (no token, empty `allowed_user_ids`, library missing,
login refused). The library is `pip install discord.py httpx`.
