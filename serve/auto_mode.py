"""Auto mode: the permission mode that asks only when something looks risky - the way Claude's auto mode works.

Looking (read, search, fetch) and ordinary changes (write, edit, create) run by themselves, as in "Allow edits".  What differs:
a command or a Python program, and the tools that delete or move things, are read first.  Routine ones (list a folder, run a
script, query the GPU, delete a file in the temp folder) run without a click; the ones below stop and ask, with the reason
shown on the approval card.  Protected folders and the no-read list work exactly as in every other mode and are checked before
this.  The rules are plain text patterns, not a model: fast, the same every time, and they err on the side of asking.

Asks for: deleting outside the scratch folders, disk formatting, registry and system / security / service settings, anything run
as administrator, code built at run time (iex, encoded commands, download-and-run), sending data off the PC (POST, upload, scp),
installing or removing software, git pushes and other history rewrites, and stopping programs.
"""
import os
import re
import tempfile

_FLAGS = re.I

RISKY = [(re.compile(p, _FLAGS), why) for p, why in (
    (r"\b(format-volume|diskpart|clear-disk|remove-partition|initialize-disk|cipher\s+/w|mkfs)\b|\bformat\s+[a-z]:", "formats or wipes a disk"),
    (r"\breg(\.exe)?\s+(add|delete|import|load|unload|copy|save|restore)\b|\b(set|new|remove|clear)-itemproperty\b[^\n]*\bhk(lm|cu|cr|u|cc)\b|\bregedit\b",
     "changes the registry"),
    (r"\b(set|add|remove)-mppreference\b|\bbcdedit\b|\bnetsh\b|\bschtasks\b|\bsc(\.exe)?\s+(config|create|delete|stop|start)\b|"
     r"\bset-executionpolicy\b|\b(new|set|stop|remove|restart)-service\b|\b(restart|stop)-computer\b|\bshutdown(\.exe)?\s+/|"
     r"\bnet\s+(user|localgroup|stop|start)\b|\bicacls\b|\btakeown\b|\bwevtutil\s+cl\b|\bclear-eventlog\b|"
     r"\b(new|set|remove|enable|disable)-netfirewallrule\b|\benable-psremoting\b|\bdisable-windowsoptionalfeature\b|\bcmdkey\b|"
     r"\bvaultcmd\b|\bmimikatz\b|\bset-netconnectionprofile\b|\bdisable-computerrestore\b|\bvssadmin\b",
     "changes system, security or service settings"),
    (r"-verb\s+runas|\brunas\b", "runs something as administrator"),
    (r"\b(iwr|irm|invoke-webrequest|invoke-restmethod|curl|wget|bitsadmin)\b[^\n]*(\||;)\s*(iex|sh|bash|powershell|pwsh|cmd)\b|"
     r"\biex\b|invoke-expression|-enc(odedcommand)?\s|frombase64string", "runs code it downloads, decodes or builds at run time"),
    (r"-method\s+(post|put|patch|delete)\b|\bcurl\b[^\n]*(\s-d\b|\s--data|\s-t\b|\s--upload-file|\s-f\b|\s-x\s+(post|put|delete|patch))|"
     r"\bscp\b|\bsftp\b|\bftp\b|\bsend-mailmessage\b|-infile\b|\bncat?\s|\brequests\.(post|put|patch|delete)\b|\bsmtplib\b|\bftplib\b|"
     r"\bparamiko\b|\bsocket\.socket\b", "sends data off this PC"),
    (r"\b(winget|choco|scoop)\s+(install|uninstall|upgrade|remove)\b|\b(pip3?|python\s+-m\s+pip|py\s+-m\s+pip)\s+(install|uninstall)\b|"
     r"\bnpm\s+(install|i|uninstall|remove)\b[^\n]*(\s-g\b|--global)|\bmsiexec\b", "installs or removes software"),
    (r"\bgit\s+(push|reset\s+--hard|clean\b|checkout\s+--|branch\s+-d|rebase|filter-branch|remote\s+(add|set-url|remove))|"
     r"\bgh\s+(pr|repo|release|issue)\s+(create|delete|merge|close|edit)\b", "changes a git remote or discards work"),
    (r"\b(stop-process|taskkill|pkill|killall)\b|\bkill\s+-", "stops running programs"),
    (r"\bwinreg\b|\bctypes\b[^\n]*\bwindll\b", "uses low-level Windows interfaces"),
    (r"\\\\[.?]\\(physicaldrive\d|harddisk\d|volume\{|globalroot)", "reads a raw disk or volume device (it skips the file rules)"),
)]

_DELETE_PY = re.compile(r"\bshutil\.rmtree\b|\bos\.(remove|unlink|rmdir|removedirs)\b|\.unlink\(|\.rmdir\(|\brmtree\(", _FLAGS)
_DELETE_SHELL = re.compile(r"\b(remove-item|ri|rm|del|erase|rmdir|rd|unlink|shred)\b", _FLAGS)
_PATH = re.compile(r"[a-z]:[\\/][^\s\"'`;|<>*?]*|%(?:temp|tmp)%[^\s\"'`;|<>*?]*|\$env:(?:temp|tmp)[^\s\"'`;|<>*?]*", _FLAGS)
_DELETE_TOOL = re.compile(r"delete|remove|(^|[^a-z])rm([^a-z]|$)|rmdir|unlink|kill|format|wipe|purge|drop|truncate|reset|send|post|"
                          r"publish|deploy|push|merge|commit|install|clear|shutdown", _FLAGS)


def scratch_roots(extra=()):
    """Folders where deleting is routine: the temp folders, plus the config's mcp.auto_scratch_paths."""
    roots = {os.path.normcase(os.path.realpath(p)).rstrip("\\/") for p in
             [tempfile.gettempdir(), os.environ.get("TEMP", ""), os.environ.get("TMP", ""), *extra] if p}
    return sorted(r for r in roots if len(r) > 3)       # never a whole drive


def _inside(path: str, roots) -> bool:
    p = os.path.normcase(os.path.realpath(os.path.expandvars(path))).rstrip("\\/")
    return any(p == r or p.startswith(r + os.sep) for r in roots)


def _texts(arguments) -> str:
    out = []

    def walk(v, depth=0):
        if depth > 6:
            return
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x, depth + 1)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x, depth + 1)
    walk(arguments)
    return "\n".join(out)


def assess_exec(arguments, scratch=()):
    """None when a command or program looks routine, else the reason it should ask."""
    text = _texts(arguments)
    for rx, why in RISKY:
        if rx.search(text):
            return why
    is_python = isinstance(arguments, dict) and "code" in arguments and "command" not in arguments   # `del x` is Python, not a file delete
    if _DELETE_PY.search(text) or (not is_python and _DELETE_SHELL.search(text)):
        paths = _PATH.findall(text)
        roots = scratch_roots(scratch)
        if not paths:
            return "deletes files (it does not say which)"
        for p in paths:
            if re.match(r"(%(temp|tmp)%|\$env:(temp|tmp))", p, _FLAGS):
                continue
            if not _inside(p, roots):
                return "deletes files outside the temp folders"
    return None


def assess_tool(name: str, paths, scratch=()):
    """For a tool of kind "danger" that is not a command (a file server's delete / move): None when routine, else the reason.
    `paths` are the call's resolved paths (serve.mcp.arg_paths); a delete runs by itself only inside the scratch folders."""
    if not _DELETE_TOOL.search(name or ""):
        return None                                      # a move, a rename, a copy: an edit like any other
    roots = scratch_roots(scratch)
    paths = list(paths)
    if paths and all(p is not None and _inside(p, roots) for p in paths):
        return None
    return f"{name} deletes, sends or changes things beyond the temp folders"
