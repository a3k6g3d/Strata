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
              "token_file": "J:/Strata-work/discord-token.txt",   this file (first line) - never from the config itself
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
        "`!mode off|read|ask` tools: none / look only / look + ask before changes   `!think off|low|medium|high`\n"
        "Send a picture with your message to ask about it. Anything that changes files or runs a command waits for your button.")
MAX_MESSAGE = 1900                              # Discord's limit is 2000 characters
MAX_HISTORY_MESSAGES = 40
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


def chat_body(history: list, text: str, images: list, thinking: str, permission: str, model=None) -> dict:
    content = text
    if images:
        content = [{"type": "text", "text": text or "What is in this picture?"}] + \
                  [{"type": "image_url", "image_url": {"url": u}} for u in images]
    body = {"messages": trim_history(history) + [{"role": "user", "content": content}], "stream": True,
            "reasoning_effort": thinking}
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


class StrataDiscordBot:
    def __init__(self, cfg: dict, base_url: str, api_key: str = "", history_dir: Path | None = None, token: str | None = None):
        self.cfg, self.base, self.api_key, self.token = cfg, base_url.rstrip("/"), api_key, token
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
            st.history = self._load(channel_id)
        return st

    def _file(self, channel_id: int):
        return self.history_dir / f"{channel_id}.json" if self.history_dir else None

    def _load(self, channel_id: int) -> list:
        f = self._file(channel_id)
        try:
            return trim_history(json.loads(f.read_text(encoding="utf-8"))) if f and f.exists() else []
        except (OSError, ValueError):
            return []

    def _save(self, channel_id: int, history: list) -> None:
        f = self._file(channel_id)
        if not f:
            return
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, f)
        except OSError:
            pass

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
        images = []
        for a in msg.attachments:
            if (a.content_type or "").startswith("image/") and a.size <= 8 * 2 ** 20:
                images.append(f"data:{a.content_type};base64," + base64.b64encode(await a.read()).decode())
        if not text and not images:
            return
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
            st.history = []
            self._save(msg.channel.id, [])
            return await send("New conversation started.")
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
                         f"This chat: {len(st.history)} messages, tools **{st.permission}**, thinking **{st.thinking}**"]
                return await send("\n".join(x for x in lines if x))
            except Exception as e:  # noqa: BLE001
                return await send(f"Strata is not answering: {type(e).__name__}")
        return await send("Unknown command. " + HELP)

    # -- one answer
    async def answer(self, msg, st: ChannelState, text: str, images: list):
        async with st.lock:
            channel = msg.channel
            note = await channel.send("💭 …")
            typing = asyncio.ensure_future(self._typing(channel))
            content, tools, last_edit, status = "", {}, 0.0, "💭 thinking …"
            body = chat_body(st.history, text, images, st.thinking, st.permission)

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
            if content.strip() and not error:
                st.history = trim_history(st.history + [{"role": "user", "content": text or "[picture]"},
                                                        {"role": "assistant", "content": content.strip()}])
                self._save(channel.id, st.history)

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
    bot = StrataDiscordBot(cfg, base_url, api_key, history, token)

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
