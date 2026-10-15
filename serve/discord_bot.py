"""Discord front end for Strata: talk to the model, and let it use its tools, from a phone (the Discord app) - the way Claude's
remote sessions work, on this server.  Optional: it does nothing unless the config has a "discord" section with "enabled": true.

How it works: the bot logs in with the user's own bot token and listens for messages from the people in `allowed_user_ids` (and
nobody else - anything from another account is ignored without a reply).  Each message goes to this server's own
/v1/chat/completions as a normal chat request (the conversation of that Discord channel or DM is kept in
`<sessions folder>/../discord/<channel>.json`), and the answer is written back by editing one message as it streams.  Tools run
as in the web app, but never above the "ask" mode: reading is automatic, every change and every command shows Allow / Deny
buttons in Discord.  The protected Windows folders and the no-read list of the config apply as always.

Config (in the Strata config file):
  "discord": {"enabled": true,
              "allowed_user_ids": [123456789012345678],      required: your Discord user id (Developer Mode > Copy User ID)
              "token_env": "STRATA_DISCORD_TOKEN",           the bot token is read from this environment variable, or from
              "token_file": "<work>/discord-token.txt",   this file (first line) - never from the config itself
              "channel_ids": [],                             optional server channels it also answers in (needs the
                                                             "Message Content Intent" switched on in the Developer Portal)
              "permission": "ask",                           off | read | ask   (edit and full are not available from Discord)
              "thinking": "medium"}                          off | low | medium | high
Commands in the chat: !new  !stop  !status  !mode off|read|ask  !think off|low|medium|high  !help
"""
import asyncio
import base64
import json
import os
import re
import threading
import time
from pathlib import Path

PERMISSIONS = ("off", "read", "ask")           # what a phone may be given: never edit / full access
THINKING = ("off", "low", "medium", "high")
HELP = ("**Strata on Discord**\n`!new` start a new conversation   `!stop` stop the answer   `!status` model and PC state\n"
        "`!chats` your latest chats (the same ones the Strata app lists)   `!open 2` continue one of them here\n"
        "`!mode off|read|ask` tools: none / look only / look + ask before changes   `!think off|low|medium|high`\n"
        "`!files` files in the workspace folder   `!send NAME` send one here (the model can offer files too; you confirm each)\n"
        "Send a picture or a text / code file with your message to ask about it. Anything that changes files or runs a command waits for your button.")
MAX_MESSAGE = 1900                              # Discord's limit is 2000 characters
MAX_HISTORY_MESSAGES = 40
MAX_IMAGE_BYTES = 8 * 2 ** 20
MAX_TEXT_FILE_BYTES = 200_000                       # one text / code attachment
MAX_TEXT_TOTAL_CHARS = 120_000                      # all of a message's files together (about 30K tokens)
TEXT_EXTENSIONS = {".txt", ".md", ".py", ".json", ".csv", ".tsv", ".log", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".xml", ".yaml",
                   ".yml", ".ini", ".cfg", ".toml", ".c", ".h", ".cpp", ".hpp", ".cu", ".java", ".rs", ".go", ".sh", ".ps1", ".bat",
                   ".cmd", ".sql", ".diff", ".patch", ".env", ".gradle", ".kt"}


def is_text_attachment(name: str, content_type: str) -> bool:
    return (content_type or "").split(";")[0].startswith("text/") or os.path.splitext(name or "")[1].lower() in TEXT_EXTENSIONS


async def collect_attachments(attachments) -> tuple:
    """A message's attachments -> (image data URLs for the model, text-file blocks for the prompt, names that could not be used and why).
    Pictures go to the vision encoder; text and code files are read into the prompt; video, audio, PDFs and anything else are named back."""
    images, files, skipped, chars = [], [], [], 0
    for a in attachments or []:
        name, ctype, size = a.filename or "file", (a.content_type or ""), int(a.size or 0)
        if ctype.startswith("image/"):
            if size > MAX_IMAGE_BYTES:
                skipped.append(f"{name} (picture over {MAX_IMAGE_BYTES // 2 ** 20} MB)")
            else:
                images.append(f"data:{ctype};base64," + base64.b64encode(await a.read()).decode())
        elif is_text_attachment(name, ctype):
            if size > MAX_TEXT_FILE_BYTES:
                skipped.append(f"{name} (text file over {MAX_TEXT_FILE_BYTES // 1000} KB)")
                continue
            body = (await a.read()).decode("utf-8", errors="replace")
            room = MAX_TEXT_TOTAL_CHARS - chars
            if room <= 0:
                skipped.append(f"{name} (too much text in one message)")
                continue
            cut = len(body) > room
            body, chars = body[:room], chars + min(len(body), room)
            fence = "````" if "```" in body else "```"
            files.append(f"[file: {name}]\n{fence}\n{body}\n{fence}" + ("\n[the rest of this file was cut]" if cut else ""))
        elif ctype.startswith("video/"):
            skipped.append(f"{name} (video is not supported)")
        elif ctype.startswith("audio/"):
            skipped.append(f"{name} (audio is not supported)")
        else:
            skipped.append(f"{name} (this file type is not supported; text and code files and pictures are)")
    return images, files, skipped
MAX_HISTORY_CHARS = 60000
EDIT_EVERY_S = 1.3                              # Discord rate-limits message edits


# ----------------------------------------------------------------------------------------------- pure helpers (tested)
def clamp_permission(value, default="ask") -> str:
    return value if value in PERMISSIONS else default


def clamp_thinking(value, default="medium") -> str:
    return value if value in THINKING else default


def is_allowed(user_id, cfg) -> bool:
    """Only the listed accounts; an empty or missing list allows nobody."""
    ids = cfg.get("allowed_user_ids")
    if not isinstance(ids, list):
        return False
    return str(user_id) in {str(i) for i in ids}


def parse_command(text: str):
    """"!mode read" -> ("mode", "read"); anything else -> None."""
    m = re.match(r"^\s*!(\w+)\s*(.*?)\s*$", text or "", re.S)
    return (m.group(1).lower(), m.group(2)) if m else None


def split_message(text: str, limit: int = MAX_MESSAGE) -> list:
    """Cut a long answer into Discord-sized pieces, at a line or a space where possible, and keep a ``` block open across the cut."""
    text = (text or "").strip()
    if not text:
        return []
    out, fence = [], False
    while text:
        if len(text) <= limit:
            piece, text = text, ""
        else:
            cut = text.rfind("\n", 0, limit)
            if cut < limit // 2:
                cut = text.rfind(" ", 0, limit)
            if cut < limit // 2:
                cut = limit
            piece, text = text[:cut], text[cut:].lstrip("\n")
        if fence:
            piece = "```\n" + piece
        fence = piece.count("```") % 2 == 1
        if fence and text:
            piece += "\n```"
        out.append(piece.strip())
    return [p for p in out if p]


def trim_history(history: list) -> list:
    """The newest messages that fit: at most MAX_HISTORY_MESSAGES and MAX_HISTORY_CHARS, starting on a user message."""
    h = [m for m in history if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)]
    h = h[-MAX_HISTORY_MESSAGES:]
    while h and (sum(len(m["content"]) for m in h) > MAX_HISTORY_CHARS or h[0]["role"] != "user"):
        h = h[1:]
    return h


def describe_call(name: str, arguments, limit: int = 900) -> str:
    """The tool call as the Allow / Deny message shows it: a command in full, other arguments as compact JSON."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            pass
    if isinstance(arguments, dict):
        cmd = arguments.get("command") or arguments.get("code")
        if isinstance(cmd, str):
            body = cmd
        else:
            body = json.dumps(arguments, ensure_ascii=False)
    else:
        body = str(arguments)
    body = body.replace("```", "'''")
    if len(body) > limit:
        body = body[:limit] + " ..."
    return f"`{name}`\n```\n{body}\n```"


def parse_sse_line(line: str):
    """One line of a streamed chat response -> None (not data / [DONE]) or the decoded JSON chunk."""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if not data or data == "[DONE]":
        return None
    try:
        return json.loads(data)
    except ValueError:
        return None


def read_token(cfg: dict, base_dir: Path | None = None):
    """The bot token: the environment variable named by token_env, else the first line of token_file.  None when neither exists."""
    name = cfg.get("token_env") or "STRATA_DISCORD_TOKEN"
    tok = (os.environ.get(name) or "").strip()
    if tok:
        return tok
    path = cfg.get("token_file")
    if path:
        p = Path(path)
        if not p.is_absolute() and base_dir:
            p = base_dir / p
        try:
            first = p.read_text(encoding="utf-8-sig").strip().splitlines()
            if first and first[0].strip():
                return first[0].strip()
        except OSError:
            pass
    return None


def api_history(messages) -> list:
    """The chat messages of a saved session (the web app's format: {"role", "text", ...}) as the plain {"role", "content"} list a request takes."""
    out = []
    for m in messages if isinstance(messages, list) else []:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("text"), str) and m["text"].strip() and not m.get("partial"):
            out.append({"role": m["role"], "content": m["text"]})
    return trim_history(out)


def web_message(role: str, text: str, via: str = "") -> dict:
    """One message in the web app's saved format (so the chat opens in the Strata app as if it had been typed there)."""
    m = {"role": role, "text": text, "images": [], "files": [], "time": str(int(time.time() * 1000))}
    if role == "assistant":
        m.update(reasoning="", tools=[], meta=via)
    return m


def session_title(first_text: str) -> str:
    t = re.sub(r"\s+", " ", first_text or "").strip()
    return "Discord · " + (t[:50] + "…" if len(t) > 50 else t or "chat")


def new_session_id(channel_id, now=None) -> str:
    return f"s-discord-{channel_id}-{int(now if now is not None else time.time())}"


MAX_SEND_BYTES = 8 * 2 ** 20                        # one file sent to Discord
SEND_MARKER = re.compile(r"\[\[send:\s*([^\]\n]+?)\s*\]\]", re.I)
MAX_SENDS_PER_ANSWER = 3
SEND_HINT = ("You are answering in Discord. To send the user a file you made (a picture, a text file, a log), put "
             "[[send: relative/path/to/file]] on its own line in your answer. Only files inside the workspace folder can be sent, "
             "each up to 8 MB, and the user confirms every one with a button. You cannot send anything else.")


def resolve_sendable(path_text: str, root) -> tuple:
    """A path the model or the user named -> (the real Path, None) if it may be sent, else (None, why not).  Only a file inside `root`
    (the workspace folder), found after links and `..` are resolved, up to 8 MB, and not on the no-read list."""
    if root is None:
        return None, "sending files is not set up (no workspace folder)"
    text = (path_text or "").strip().strip("`'\"")
    if not text:
        return None, "give a file name"
    try:
        base = Path(os.path.realpath(root))
        real = Path(os.path.realpath(base / text if not os.path.isabs(text) else text))
    except (OSError, ValueError):
        return None, "that path cannot be read"
    try:
        inside = os.path.commonpath([os.path.normcase(str(base)), os.path.normcase(str(real))]) == os.path.normcase(str(base))
    except ValueError:                                  # another drive
        inside = False
    if not inside or real == base:
        return None, "only files inside the workspace folder can be sent"
    if not real.is_file():
        return None, "no such file in the workspace folder"
    try:
        from serve import mcp as _mcp
        if _mcp.BlockList(["@defaults"]).hit(os.path.normcase(str(real))):
            return None, "that file is on the no-read list"
    except ImportError:
        pass
    if real.stat().st_size > MAX_SEND_BYTES:
        return None, f"the file is over {MAX_SEND_BYTES // 2 ** 20} MB"
    return real, None


def chat_body(history: list, text: str, images: list, thinking: str, permission: str, model=None, system: str | None = None) -> dict:
    content = text
    if images:
        content = [{"type": "text", "text": text or "What is in this picture?"}] + \
                  [{"type": "image_url", "image_url": {"url": u}} for u in images]
    body = {"messages": ([{"role": "system", "content": system}] if system else []) + trim_history(history) + [{"role": "user", "content": content}],
            "stream": True, "reasoning_effort": thinking}
    if model:
        body["model"] = model
    if permission != "off":
        body["strata_mcp"] = True
        body["strata_permission"] = permission
    return body


# ----------------------------------------------------------------------------------------------------------- the bot
class ChannelState:
    def __init__(self, thinking, permission):
        self.history: list = []
        self.thinking, self.permission = thinking, permission
        self.lock = asyncio.Lock()
        self.tasks: set = set()                # the answers being written or waiting (so !stop can cancel them)
        self.question = None                   # (approval token) of an ask_user question that waits for a typed reply
        self.session = None                    # the Strata chat (sessions/<id>) this channel continues; None: the next message starts one
        self.listing: list = []                # the chats !chats showed, for !open N


class StrataDiscordBot:
    def __init__(self, cfg: dict, base_url: str, api_key: str = "", history_dir: Path | None = None, token: str | None = None,
                 send_root: Path | None = None):
        self.cfg, self.base, self.api_key, self.token = cfg, base_url.rstrip("/"), api_key, token
        self.send_root = Path(send_root) if send_root else None     # the only folder files may be sent from
        self.history_dir = Path(history_dir) if history_dir else None
        self.default_thinking = clamp_thinking(cfg.get("thinking"))
        self.default_permission = clamp_permission(cfg.get("permission"))
        self.channels: dict = {}
        self.approval_timeout = float(cfg.get("approval_timeout_s") or 3600)

    # -- state
    def state(self, channel_id: int) -> ChannelState:
        st = self.channels.get(channel_id)
        if st is None:
            st = self.channels[channel_id] = ChannelState(self.default_thinking, self.default_permission)
            st.history, st.session = self._load(channel_id)
        return st

    def _file(self, channel_id: int):
        return self.history_dir / f"{channel_id}.json" if self.history_dir else None

    def _load(self, channel_id: int):
        """(the local copy of the conversation, the Strata chat id it belongs to).  The file was a bare list before chats were shared."""
        f = self._file(channel_id)
        try:
            data = json.loads(f.read_text(encoding="utf-8")) if f and f.exists() else []
        except (OSError, ValueError):
            return [], None
        if isinstance(data, dict):
            sid = data.get("session")
            return trim_history(data.get("history") or []), sid if isinstance(sid, str) else None
        return trim_history(data), None

    def _save(self, channel_id: int, history: list, session=None) -> None:
        f = self._file(channel_id)
        if not f:
            return
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps({"session": session, "history": history}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, f)
        except OSError:
            pass

    # -- the chats of the Strata app (GET/POST /sessions): what the Discord conversation is saved to and continued from
    async def session_get(self, sid: str):
        try:
            r = await self.http.get(f"{self.base}/sessions/{sid}", headers=self.headers(), timeout=30)
            return r.json() if r.status_code == 200 else None
        except Exception:  # noqa: BLE001 - no sessions folder, or the server is busy: the local copy is used
            return None

    async def session_save(self, sid: str, title: str, messages: list, custom: bool = True) -> bool:
        return await self.post(self.http, f"/sessions/{sid}", {"title": title, "custom_title": custom, "messages": messages})

    async def session_list(self) -> list:
        try:
            r = await self.http.get(self.base + "/sessions", headers=self.headers(), timeout=30)
            return (r.json().get("sessions") or []) if r.status_code == 200 else []
        except Exception:  # noqa: BLE001
            return []

    def headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = "Bearer " + self.api_key
        return h

    # -- talking to this server
    async def post(self, http, path: str, body: dict) -> bool:
        try:
            r = await http.post(self.base + path, json=body, headers=self.headers(), timeout=30)
            return r.status_code == 200
        except Exception:  # noqa: BLE001 - the server went away: the stream reports it
            return False

    # -- discord
    async def run(self):
        import discord                                   # installed on demand: pip install discord.py
        import httpx
        intents = discord.Intents.default()
        intents.dm_messages = True
        if self.cfg.get("channel_ids"):
            intents.message_content = True              # server channels need the privileged intent (Developer Portal)
        client = discord.Client(intents=intents)
        self.discord, self.client, self.http = discord, client, httpx.AsyncClient(timeout=None)

        @client.event
        async def on_ready():
            print(f"[strata] Discord bot online as {client.user} (answers {len(self.cfg.get('allowed_user_ids') or [])} "
                  f"allowed account(s); tools: {self.default_permission})", flush=True)

        @client.event
        async def on_message(msg):
            try:
                await self.handle(msg)
            except Exception as e:  # noqa: BLE001 - one bad message must not stop the bot
                print(f"[strata] Discord: {type(e).__name__}: {e}", flush=True)
                try:
                    await msg.channel.send(f"Something went wrong: {type(e).__name__}: {e}"[:1900])
                except Exception:  # noqa: BLE001
                    pass

        await client.start(self.token)

    def wanted(self, msg) -> bool:
        """A message this bot answers: from an allowed account, in a DM or an allowed channel (where it is addressed)."""
        if msg.author.bot or not is_allowed(msg.author.id, self.cfg):
            return False
        if msg.guild is None:
            return True
        ids = {int(i) for i in self.cfg.get("channel_ids") or []}
        return msg.channel.id in ids and (self.client.user in msg.mentions or not self.cfg.get("require_mention", True))

    async def handle(self, msg):
        if not self.wanted(msg):
            return
        text = re.sub(rf"<@!?{self.client.user.id}>", "", msg.content or "").strip()
        st = self.state(msg.channel.id)
        cmd = parse_command(text)
        if cmd:
            return await self.command(msg, st, *cmd)
        if st.question:                                       # the model asked something and waits for the typed answer
            token, st.question = st.question, None
            await self.post(self.http, "/mcp/answer", {"approval": token, "text": text})
            return await msg.add_reaction("✅")
        images, files, skipped = await collect_attachments(msg.attachments)
        note = ("Could not use: " + "; ".join(skipped)) if skipped else ""
        if note:
            await msg.channel.send("⚠️ " + note)
        if not (text or images or files):
            return                                            # nothing but unusable attachments: the notice is the whole answer
        if note:
            text = (text + "\n\n" if text else "") + f"[{note}]"
        if files:
            text = (text + "\n\n" if text else "") + "\n\n".join(files)
        if st.lock.locked():
            await msg.add_reaction("⏳")                      # it waits its turn (the model answers one request at a time)
        task = asyncio.ensure_future(self.answer(msg, st, text, images))
        st.tasks.add(task)
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            st.tasks.discard(task)

    async def command(self, msg, st: ChannelState, name: str, arg: str):
        send = msg.channel.send
        if name in ("help", "start"):
            return await send(HELP)
        if name == "new":
            st.history, st.session = [], None
            self._save(msg.channel.id, [], None)
            return await send("New conversation started. The earlier one stays in the Strata app's chat list.")
        if name == "chats":
            chats = [c for c in await self.session_list() if isinstance(c, dict) and c.get("id")][:8]
            st.listing = [c["id"] for c in chats]
            if not chats:
                return await send("No saved chats yet.")
            rows = [f"{i + 1}. {'▶ ' if c['id'] == st.session else ''}{c.get('title') or c['id']} ({c.get('messages', 0)} messages)" for i, c in enumerate(chats)]
            return await send("**Your latest chats** (`!open 2` continues one here):\n" + "\n".join(rows))
        if name == "open":
            sid = None
            if arg.isdigit() and 1 <= int(arg) <= len(st.listing):
                sid = st.listing[int(arg) - 1]
            elif arg.startswith("s-"):
                sid = arg
            if not sid:
                return await send("Use `!chats` first, then `!open 2` (a number from that list).")
            data = await self.session_get(sid)
            if not data:
                return await send("That chat could not be opened.")
            st.session, st.history = sid, api_history(data.get("messages"))
            self._save(msg.channel.id, st.history, sid)
            return await send(f"Continuing **{data.get('title') or sid}** ({len(data.get('messages') or [])} messages). Your next message goes on from there.")
        if name == "stop":
            running = [t for t in st.tasks if not t.done()]
            if running:
                for t in running:
                    t.cancel()
                return await send("Stopping.")
            return await send("Nothing is running.")
        if name == "mode":
            if arg in PERMISSIONS:
                st.permission = arg
                return await send(f"Tools: **{arg}**.")
            return await send(f"Tools are **{st.permission}**. Use `!mode off`, `read` or `ask` (edit and full access are not available from Discord).")
        if name == "think":
            if arg in THINKING:
                st.thinking = arg
                return await send(f"Thinking: **{arg}**.")
            return await send(f"Thinking is **{st.thinking}**. Use `!think off|low|medium|high`.")
        if name == "status":
            try:
                r = await self.http.get(self.base + "/v1/status", headers=self.headers(), timeout=10)
                s = r.json()
                t = s.get("last_timings") or {}
                speed = t.get("predicted_per_second")
                m = (await self.http.get(self.base + "/metrics", headers=self.headers(), timeout=10)).json()
                c = m.get("commit") or {}
                lines = [f"Model: `{s.get('model')}` (engine {s.get('engine')}), loaded: {s.get('loaded')}",
                         f"Last answer: {speed:.1f} tok/s" if isinstance(speed, (int, float)) else "No answer yet",
                         f"Memory commit: {c.get('used_gib')} of {c.get('limit_gib')} GiB ({c.get('level')})" if c else "",
                         f"This chat: {len(st.history)} messages{' (saved in the Strata app)' if st.session else ''}, tools **{st.permission}**, thinking **{st.thinking}**"]
                return await send("\n".join(x for x in lines if x))
            except Exception as e:  # noqa: BLE001
                return await send(f"Strata is not answering: {type(e).__name__}")
        if name == "files":
            if self.send_root is None:
                return await send("No workspace folder is set up, so there is nothing to send.")
            found = sorted((p for p in self.send_root.rglob("*") if p.is_file() and resolve_sendable(str(p), self.send_root)[0]),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:20]
            if not found:
                return await send("The workspace folder has no files to send yet.")
            return await send("Files you can send (newest first), with `!send NAME`:\n" + "\n".join(
                f"`{p.relative_to(self.send_root).as_posix()}`  {p.stat().st_size / 1024:.0f} KB" for p in found))
        if name == "send":
            real, why = resolve_sendable(arg, self.send_root)
            if real is None:
                return await send(f"Cannot send that: {why}.")
            try:
                return await send(content=f"`{real.name}`", file=self.discord.File(str(real)))
            except Exception as e:  # noqa: BLE001 - too big for this server, no permission to attach files
                return await send(f"Discord refused the file: {type(e).__name__}.")
        return await send("Unknown command. " + HELP)

    async def confirm(self, channel, text: str) -> bool:
        """A yes / no question with Allow and Deny buttons; only an allowed account's click counts; no click in time means no."""
        discord = self.discord
        done = asyncio.get_event_loop().create_future()
        view = discord.ui.View(timeout=self.approval_timeout)

        def button(label, style, allow):
            b = discord.ui.Button(label=label, style=style)

            async def clicked(interaction):
                if not is_allowed(interaction.user.id, self.cfg):
                    return await interaction.response.send_message("Not for you.", ephemeral=True)
                if not done.done():
                    done.set_result(allow)
                view.stop()
                await interaction.response.edit_message(content=text + ("\n✅ sent" if allow else "\n🚫 not sent"), view=None)
            b.callback = clicked
            return b

        async def on_timeout():
            if not done.done():
                done.set_result(False)
        view.on_timeout = on_timeout
        view.add_item(button("Send it", discord.ButtonStyle.success, True))
        view.add_item(button("Don't send", discord.ButtonStyle.danger, False))
        await channel.send(text[:MAX_MESSAGE], view=view)
        return await done

    async def send_marked_files(self, channel, content: str) -> str:
        """The model asked for files with [[send: path]]: take the markers out of the answer; for each one that resolves inside the
        workspace folder, ask with a button and upload it.  A path that does not resolve is named in the channel, never sent."""
        paths = SEND_MARKER.findall(content or "")
        cleaned = SEND_MARKER.sub("", content or "").strip()
        for path in paths[:MAX_SENDS_PER_ANSWER]:
            real, why = resolve_sendable(path, self.send_root)
            if real is None:
                await channel.send(f"⚠ Strata wanted to send `{path[:120]}`, but {why}.")
                continue
            if await self.confirm(channel, f"**Strata wants to send you a file:** `{real.name}` ({real.stat().st_size / 1024:.0f} KB, from the workspace folder)"):
                try:
                    await channel.send(content=f"`{real.name}`", file=self.discord.File(str(real)))
                except Exception as e:  # noqa: BLE001
                    await channel.send(f"Discord refused the file: {type(e).__name__}.")
        if len(paths) > MAX_SENDS_PER_ANSWER:
            await channel.send(f"⚠ Only the first {MAX_SENDS_PER_ANSWER} files were offered; the other {len(paths) - MAX_SENDS_PER_ANSWER} were not.")
        return cleaned

    # -- one answer
    async def answer(self, msg, st: ChannelState, text: str, images: list):
        async with st.lock:
            channel = msg.channel
            note = await channel.send("💭 …")
            typing = asyncio.ensure_future(self._typing(channel))
            content, tools, last_edit, status = "", {}, 0.0, "💭 thinking …"
            saved = await self.session_get(st.session) if st.session else None     # the chat as the Strata app has it now (it may have been continued there)
            if saved is not None:
                st.history = api_history(saved.get("messages"))
            body = chat_body(st.history, text, images, st.thinking, st.permission, system=SEND_HINT if self.send_root else None)

            def view():
                lines = [f"{'⏳' if t['state'] in ('running', 'waiting') else '✅' if t['state'] == 'done' else '❌'} `{t['name']}`"
                         for t in list(tools.values())[-6:]]
                shown = content[-1500:] if content else ""
                return ("\n".join(lines) + "\n" if lines else "") + (shown or status)

            async def edit(final=False):
                nonlocal last_edit
                if not final and time.time() - last_edit < EDIT_EVERY_S:
                    return
                last_edit = time.time()
                try:
                    await note.edit(content=(view() or "…")[:MAX_MESSAGE])
                except Exception:  # noqa: BLE001 - rate limit or deleted message: the next edit tries again
                    pass

            error = None
            try:
                async with self.http.stream("POST", self.base + "/v1/chat/completions", json=body, headers=self.headers()) as r:
                    if r.status_code != 200:
                        raise RuntimeError((await r.aread()).decode("utf-8", "replace")[:300])
                    async for line in r.aiter_lines():
                        j = parse_sse_line(line)
                        if j is None:
                            continue
                        if j.get("error"):
                            raise RuntimeError((j["error"] or {}).get("message") or "the engine reported an error")
                        ev = j.get("strata_mcp")
                        if ev:
                            await self.on_event(channel, st, ev, tools)
                        d = ((j.get("choices") or [{}])[0].get("delta")) or {}
                        if d.get("content"):
                            content += d["content"]
                        elif d.get("reasoning_content"):
                            status = "💭 thinking …"
                        await edit()
            except asyncio.CancelledError:
                error = "⏹ stopped"
            except Exception as e:  # noqa: BLE001
                error = f"⚠ {e}"[:400]
            finally:
                typing.cancel()
            raw = content
            marked = self.send_root is not None and bool(SEND_MARKER.search(raw))
            if marked:
                content = SEND_MARKER.sub("", raw).strip()          # the [[send: ...]] lines are not part of the answer
            pieces = split_message(content)
            try:
                if pieces:
                    await note.edit(content=pieces[0])
                    for p in pieces[1:]:
                        await channel.send(p)
                    if error:
                        await channel.send(error)
                else:
                    await note.edit(content=error or "(no answer)")
            except Exception:  # noqa: BLE001
                pass
            if marked and not error:
                try:
                    await self.send_marked_files(channel, raw)
                except Exception:  # noqa: BLE001 - a failed upload must not lose the saved chat below
                    pass
            if content.strip() and not error:
                st.history = trim_history(st.history + [{"role": "user", "content": text or "[picture]"},
                                                        {"role": "assistant", "content": content.strip()}])
                # save it as a chat of the Strata app: it appears in the chat list there, and can be continued there or here
                web = list(saved.get("messages") or []) if saved is not None else []
                web += [web_message("user", text or "[picture]"), web_message("assistant", content.strip(), "via Discord")]
                sid = st.session or new_session_id(channel.id)
                title = (saved or {}).get("title") if saved else None
                if await self.session_save(sid, title or session_title(next((m.get("text") for m in web if m.get("role") == "user"), "")), web):
                    st.session = sid
                self._save(channel.id, st.history, st.session)

    async def _typing(self, channel):
        try:
            while True:
                await channel.typing()
                await asyncio.sleep(8)
        except Exception:  # noqa: BLE001 - cancelled or no permission
            pass

    # -- tool events
    async def on_event(self, channel, st: ChannelState, ev: dict, tools: dict):
        kind = ev.get("event")
        if kind == "compacting":
            return
        tid = ev.get("id")
        if kind == "call":
            tools[tid] = {"name": ev.get("name") or "tool", "arguments": ev.get("arguments"), "state": "running"}
        elif kind == "result" and tid in tools:
            tools[tid]["state"] = "done" if ev.get("ok") else "error"
        elif kind == "approval" and tid in tools:
            tools[tid]["state"] = "waiting"
            asyncio.ensure_future(self.ask_approval(channel, tools[tid], ev))
        elif kind == "question":
            asyncio.ensure_future(self.ask_question(channel, st, ev))

    async def ask_approval(self, channel, tool: dict, ev: dict):
        discord = self.discord
        warn = []
        if ev.get("protected"):
            warn.append("⚠ touches a protected Windows folder")
        if ev.get("leak"):
            warn.append("⚠ may send local data over the network")
        if ev.get("exec"):
            warn.append("runs a command on your PC")
        text = "**Strata wants to:** " + describe_call(tool["name"], tool.get("arguments")) + ("\n" + " · ".join(warn) if warn else "")
        done = asyncio.get_event_loop().create_future()
        view = discord.ui.View(timeout=self.approval_timeout)

        def button(label, style, allow):
            b = discord.ui.Button(label=label, style=style)

            async def clicked(interaction):
                if not is_allowed(interaction.user.id, self.cfg):
                    return await interaction.response.send_message("Not for you.", ephemeral=True)
                if not done.done():
                    done.set_result(allow)
                view.stop()
                await interaction.response.edit_message(content=text + ("\n✅ allowed" if allow else "\n🚫 denied"), view=None)
            b.callback = clicked
            return b

        async def on_timeout():
            if not done.done():
                done.set_result(False)
        view.on_timeout = on_timeout
        view.add_item(button("Allow", discord.ButtonStyle.success, True))
        view.add_item(button("Deny", discord.ButtonStyle.danger, False))
        await channel.send(text[:MAX_MESSAGE], view=view)
        allow = await done
        await self.post(self.http, "/mcp/approve", {"approval": ev.get("approval"), "allow": allow, "always": False})

    async def ask_question(self, channel, st: ChannelState, ev: dict):
        discord = self.discord
        options = [str(o) for o in (ev.get("options") or [])][:5]
        text = "**Strata asks:** " + str(ev.get("question") or "")
        token = ev.get("approval")
        if not options:
            st.question = token
            return await channel.send((text + "\n(Reply with your answer.)")[:MAX_MESSAGE])
        done = asyncio.get_event_loop().create_future()

        class Choices(discord.ui.View):
            def __init__(inner):
                super().__init__(timeout=self.approval_timeout)
                for o in options:
                    b = discord.ui.Button(label=o[:80])
                    b.callback = (lambda opt: (lambda interaction: inner.pick(interaction, opt)))(o)
                    inner.add_item(b)

            async def pick(inner, interaction, opt):
                if not is_allowed(interaction.user.id, self.cfg):
                    return await interaction.response.send_message("Not for you.", ephemeral=True)
                if not done.done():
                    done.set_result(opt)
                inner.stop()
                await interaction.response.edit_message(content=text + f"\n→ {opt}", view=None)

            async def on_timeout(inner):
                if not done.done():
                    done.set_result("")

        await channel.send(text[:MAX_MESSAGE], view=Choices())
        await self.post(self.http, "/mcp/answer", {"approval": token, "text": await done})


# ------------------------------------------------------------------------------------------------ started by the server
def start(cfg: dict, base_url: str, api_key: str = "", config_dir: Path | None = None):
    """Called by serve/server.py once the server answers.  Returns the thread, or None when Discord is not set up (it says why)."""
    if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
        return None
    if not cfg.get("allowed_user_ids"):
        print("[strata] Discord: not started - \"allowed_user_ids\" is empty (put your Discord user id there)", flush=True)
        return None
    token = read_token(cfg, config_dir)
    if not token:
        print(f"[strata] Discord: not started - no bot token (set {cfg.get('token_env') or 'STRATA_DISCORD_TOKEN'} or "
              "\"token_file\")", flush=True)
        return None
    try:
        import discord  # noqa: F401
        import httpx  # noqa: F401
    except ImportError as e:
        print(f"[strata] Discord: not started - {e.name} is not installed (pip install discord.py httpx)", flush=True)
        return None
    history = (config_dir or Path(".")) / "discord"
    folder = Path(str(cfg.get("send_folder") or "workspace"))              # the only place files can be sent from
    folder = folder if folder.is_absolute() else (config_dir or Path(".")) / folder
    bot = StrataDiscordBot(cfg, base_url, api_key, history, token, send_root=folder if folder.is_dir() else None)

    def run():
        while True:                                       # a dropped connection or a Discord outage: try again later
            try:
                asyncio.run(bot.run())
                return
            except Exception as e:  # noqa: BLE001
                name = type(e).__name__
                if name in ("LoginFailure", "PrivilegedIntentsRequired"):
                    print(f"[strata] Discord: stopped - {name}: {e}", flush=True)
                    return
                print(f"[strata] Discord: {name}: {e}; retrying in 30 s", flush=True)
                time.sleep(30)

    t = threading.Thread(target=run, name="discord-bot", daemon=True)
    t.start()
    return t
