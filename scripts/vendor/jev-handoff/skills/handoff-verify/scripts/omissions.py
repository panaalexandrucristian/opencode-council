#!/usr/bin/env python3
"""Omission evidence (R03). Stdlib only, read-only: it parses Claude Code JSONL transcripts and files and never executes anything it reads.

A `lost_detail` (a useful detail of the session that the handoff note lost) is confirmed only by a PAIR of Jev verify checks on the SAME write of the note (write_tool_use_id + sha256 +
evaluated_against, `versions.py`): a SOURCE check (the supplied source passage states the detail) and an ABSENCE check (R04: the claim is the bare detail and the complete supplied handoff material gets the
real verdict `unsupported`; R03, historical: the claim "... neither states nor implies this detail: <detail>" verified). Both claims are canonical (built here from one `detail`), the source quote must be exact in the eligible source of the evaluated version, and the evidence given to the absence call must equal the
canonical MATERIAL of the version (the note itself + its direct references, one level, delimited, in order). The CLI `prepare` prints all of it so the model copies, never types, the values; the validator
(jevref.validate_finding) re-derives everything. This module decides nothing semantic: Jev judges the claims; here only provenance, eligibility and completeness are deterministic.

CLI: omissions.py prepare --source ID|PATH.jsonl|opencode:ID|opencode-db:/ABS/DB#ID --file HANDOFF --write-id ID --evaluated-against prefix|session_end --detail TEXT --source-quote QUOTE [--cwd DIR] [--location DIR ...] [--run ID]
  --run = the verification run a session_end evaluation is about (the tool_use id that starts it, listed by `versions.py list` and by this command when several runs follow the write); it is printed in `version_ref.run` and must be copied into every check.
  exit 0 = ready (JSON on stdout), 3 = something is ambiguous or not recoverable (JSON with `reasons`; the omission stays UNRESOLVED)."""
import argparse, ast, collections, hashlib, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refs as R, sanitize as S

SOURCE_PREFIX = "The supplied source passage states this detail: "
ABSENCE_PREFIX = "The complete supplied handoff material neither states nor implies this detail: "
EVALUATED = ("prefix", "session_end")
WRITERS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
BLOCK_SEP = "\n\u0000\n"   # joins the eligible records; a quote can never span two records
SKILL_SCRIPTS = ("omissions.py", "versions.py", "jevref.py", "report.py", "discover.py", "kit.py", "prepare.py", "slice.py", "refs.py", "sanitize.py", "scope.py", "advice.py", "checks.py", "ledger.py", "audit.py", "orchestrate.py")   # the skill's own scripts (verification activity)
SKILL_REFERENCES = ("prepare.md", "opencode-sessions.md", "jev-and-audit.md", "candidates.md", "report.md")   # the five reference files of the split skill, named by SKILL.md's loading rules (never every file under reference/)
JEV_PREFIX = ("mcp__jev__", "mcp__plugin_jev_jev__")   # direct MCP config, or the server shipped by the jev Claude Code plugin

def sha256_text(t): return hashlib.sha256(t.encode("utf-8")).hexdigest()
def wsnorm(s): return " ".join(str(s if s is not None else "").split())

_CLEAN = {}
def clean_text(t):
    """The text under the shared sanitization policy (sanitize.sanitize_material); memoized by text."""
    if t not in _CLEAN:
        if len(_CLEAN) > 200000: _CLEAN.clear()
        _CLEAN[t] = S.sanitize_material(t)[0]
    return _CLEAN[t]

_INFO = {}
def info_of(t):
    """The `info` of sanitize.sanitize_material for the text; memoized by text (a pure function of the text)."""
    if t not in _INFO:
        if len(_INFO) > 200000: _INFO.clear()
        _INFO[t] = S.sanitize_material(t)[1]
    return _INFO[t]

def redaction_dependent(t):
    """Does the text hold a secret that the policy would redact, or a redaction marker? Such a detail, quote or passage cannot be audited: what is hidden is not demonstrated."""
    return S.altered(t)["redaction_dependent"]

def claims(detail, contract="R04"):
    """-> (source_claim, absence_claim) for one detail (whitespace-normalized): the two canonical claims, never typed by the model. SOURCE claim = SOURCE_PREFIX + detail in every contract. ABSENCE claim: R04 = the BARE detail
    (the form measured as `b` in the R03 probes: the call asks whether the complete material states the detail and a real `unsupported` means it does not); R03 = ABSENCE_PREFIX + detail (kept for the historical evaluator)."""
    import jevref as J
    c = J.contract_of(contract); d = wsnorm(detail)
    if c == "R02": raise ValueError("the R02 evaluator has no omission claims")
    return SOURCE_PREFIX + d, (d if c == "R04" else ABSENCE_PREFIX + d)

def material(note_text, references=()):
    """Canonical material of a note version: the note and each direct reference (ref text, sha256, content), delimited, in order of appearance. `references` = [(ref, content)]."""
    out = "=== HANDOFF NOTE (sha256: %s) ===\n%s\n=== END HANDOFF NOTE ===\n" % (sha256_text(note_text), note_text)
    for ref, content in references: out += "=== DIRECT REFERENCE %s (sha256: %s) ===\n%s\n=== END DIRECT REFERENCE ===\n" % (ref, sha256_text(content), content)
    return out

def identity_dependent(*vals, role=None):
    """Would the shared policy alter an identity field that leaves the preparation (a path, a location, a resolution)? Such an identity is never altered and never echoed: the material that carries it is not ready. `role` = "tool_use_id"
    for a recorded write / run / stage id (sanitize.identity_altered: a recognized credential-free id is kept byte-for-byte); the default is the strict policy."""
    return any(S.identity_altered(v, role)["redaction_dependent"] for v in vals if isinstance(v, str))

ID_WITHHELD = "[REDACTED:identity]"   # DISPLAY only: a refused recorded id is shown as this marker; it is never an identity (a marker is refused by every consumer, see sanitize.identity_altered)

def safe_list(vals, role=None):
    """Diagnostic copy of a list of paths (or, with role="tool_use_id", of recorded ids): the shared policy applied, clean values byte-identical. A path the strict policy alters is rendered by that policy; an id that the role-aware policy
    refuses is rendered as `ID_WITHHELD` (the generic sanitizer would miss a credential behind a provider prefix, so it never renders an id), never as a replacement identity."""
    def one(v):
        if not isinstance(v, str) or not S.identity_altered(v, role)["redaction_dependent"]: return v
        return ID_WITHHELD if role == S.ID_ROLE else S.sanitize(v)[0]
    return [one(v) for v in vals]

def id_text(v):
    """A recorded or given id for the PROSE of a diagnostic: itself when the id-role policy keeps it, else `ID_WITHHELD` (never echoed)."""
    return ID_WITHHELD if isinstance(v, str) and S.identity_altered(v, S.ID_ROLE)["redaction_dependent"] else v

def shown(v, role=None):
    """A value the CALLER gave (a selector, a run, a report path) for a diagnostic: itself when the shared identity policy keeps it, else None (never echoed, never a placeholder)."""
    return None if identity_dependent(v, role=role) else v

def bad_location(loc):
    """None when `loc` is a usable work location (an absolute path of an existing directory), else "relative" / "missing"."""
    if not isinstance(loc, str) or not os.path.isabs(loc): return "relative"
    return None if os.path.isdir(loc) else "missing"

def check_locations(locs):
    """The user-given work locations (`--location`): -> (list, None) or (None, reason). Order and strings are kept as given."""
    for loc in locs or ():
        if identity_dependent(loc): return None, "a work location holds a secret or a redaction marker: an identity that needs redaction is not used (UNRESOLVED; the value is not echoed)"
        w = bad_location(loc)
        if w == "relative": return None, "work location %r is not an absolute path" % (loc,)
        if w: return None, "work location %r is not an existing directory" % (loc,)
    return list(locs or ()), None

def material_bases(note_path, version, locations=()):
    """The ordered bases of the material, the SAME for `prepare` and the report re-derivation: the directory of the note, then the cwd recorded in the transcript for THIS write of the note (when it has one; never the process cwd or --cwd),
    then the work locations the user gave (`--location`, in the given order); build_material adds the git root of each."""
    out = []
    for b in [os.path.dirname(note_path) if note_path else None, (version or {}).get("cwd")] + list(locations or ()):
        if isinstance(b, str) and b and b not in out: out.append(b)
    return out

def build_material(note_text, bases=()):
    """-> (material | None, manifest, reason): `build_material_ex` without the unresolved reference."""
    return build_material_ex(note_text, bases)[:3]

def build_material_ex(note_text, bases=()):
    """-> (material | None, manifest, reason, missing_reference | None). Direct references (refs.py: one level, existing files; the git roots of the bases are extra bases) found in the note are part of the eligible material; a reference that cannot be resolved or read means that
    completeness is not demonstrated (None, reason). A note without references: the note alone. The material is sent to Jev, so the shared sanitization policy (sanitize.py) applies to it: the references are resolved from the note as written, a reference that is an excluded file
    (.env family, also through an alias) is never opened, and a note or reference that holds a secret or a redaction marker makes the material unavailable (fail closed, UNRESOLVED; what a hidden value says is not demonstrated, and the raw text is never returned).
    Material that needs no redaction is exactly what it always was (the delimiters carry the sha256 of the note and of each reference as read)."""
    return _build_material(note_text, bases)[:4]

def _redaction_reason(what, info):
    kinds = ", ".join("%s x%d" % (k, n) for k, n in sorted(info["redactions"].items())) + (", ambiguous x%d" % info["ambiguous_redacted"] if info["ambiguous_redacted"] else "")
    return "%s holds redacted or redaction-dependent content (%s): what it hides is not demonstrated, so the complete material is not demonstrated (UNRESOLVED; nothing is sent)" % (what, kinds or "an existing redaction marker")

_MAT = {}   # (sha256 of the note, bases, ((reference as written, resolved path, resolution, sha256 of the content read)...)) -> (material, manifest, found) of a material that needs no redaction

def _build_material(note_text, bases=()):
    """-> (material | None, manifest, reason, missing_reference | None, found [(ref, text)]). On EVERY call the references are found in the note, resolved, checked (excluded files, remote links, availability) and read again; only
    when the ordered result (reference spelling, resolved path, resolution, sha256 of the content as read) and the note are exactly what a previous construction saw is that canonical material reused
    (`STATS` material_hits); any difference constructs it again (`material_builds`). The caller gets independent copies."""
    manifest, found, bases = [], [], R.with_git_roots(bases)
    if identity_dependent(*bases): return None, manifest, "a work location holds a secret or a redaction marker: an identity that needs redaction is not used, so the complete material is not demonstrated (UNRESOLVED; nothing is sent)", dict(ref=None, reason="redaction", kind="redaction", searched=safe_list(bases)), found
    info = info_of(note_text)
    if info["redaction_dependent"]: return None, manifest, _redaction_reason("the handoff note", info), dict(ref=None, reason="redaction", kind="redaction", searched=bases), found
    facts = []
    for ref in R.direct_refs(note_text, bases):
        if any(S.is_excluded_file(x) for x in R.path_forms(ref)): return None, manifest, "direct reference %r is an excluded file (.env family): it is never opened or sent, so the complete material is not demonstrated" % ref, dict(ref=ref, reason="excluded file", kind="excluded", searched=bases), found
        real, how = R.resolve_info(ref, bases)
        if real is None:
            if R.is_remote(ref): return None, manifest, "direct reference %r is a remote link: it is not fetched, its content is unavailable, so the complete material is not demonstrated" % ref, dict(ref=ref, reason=how, kind="remote_url", searched=[]), found
            return None, manifest, "direct reference %r cannot be resolved (%s): the complete material is not demonstrated" % (ref, how), dict(ref=ref, reason=how, searched=bases), found
        if S.is_excluded_file(real): return None, manifest, "direct reference %r resolves to an excluded file (.env family): it is never opened or sent, so the complete material is not demonstrated" % ref, dict(ref=ref, reason="excluded file", kind="excluded", searched=bases), found
        if identity_dependent(real, how): return None, manifest, "direct reference %r resolves to a path that holds a secret or a redaction marker: an identity that needs redaction is not used, so the complete material is not demonstrated (UNRESOLVED; nothing is sent)" % ref, dict(ref=ref, reason="redaction", kind="redaction", searched=bases), found
        try: txt = open(real, encoding="utf-8", newline="").read()   # newline="": no newline translation, the text (and its sha256) is exactly the bytes presented
        except (OSError, UnicodeDecodeError): return None, manifest, "direct reference %r cannot be read: the complete material is not demonstrated" % ref, dict(ref=ref, reason="cannot be read (%s)" % real, searched=bases), found
        info = info_of(txt)
        if info["redaction_dependent"]: return None, manifest, _redaction_reason("the direct reference %r" % ref, info), dict(ref=ref, reason="redaction", kind="redaction", searched=bases), found
        facts.append((ref, real, how, txt))
    key = (sha256_text(note_text), tuple(bases), tuple((ref, real, how, sha256_text(txt)) for ref, real, how, txt in facts))
    if key not in _MAT:
        if len(_MAT) > 2000: _MAT.clear()
        STATS["material_builds"] += 1
        _MAT[key] = (material(note_text, [(ref, txt) for ref, _, _, txt in facts]), [dict(ref=ref, path=real, sha256=sha256_text(txt), resolution=how) for ref, real, how, txt in facts], [(ref, txt) for ref, _, _, txt in facts])
    else: STATS["material_hits"] += 1
    mat, man, fnd = _MAT[key]
    return mat, [dict(m) for m in man], None, None, list(fnd)

def _blocks_of(content):
    if isinstance(content, str): return [content]
    out = []
    for b in content if isinstance(content, list) else []:
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str): out.append(b["text"])
        elif isinstance(b, dict) and b.get("type") == "tool_result":
            c = b.get("content"); out.append(c if isinstance(c, str) else "".join(x.get("text", "") for x in c if isinstance(x, dict)) if isinstance(c, list) else "")
    return out

_STEMS = tuple(x[:-3] for x in SKILL_SCRIPTS)
_PATH_FIELDS = ("file_path", "path", "notebook_path")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON = re.compile(r"^(?:python|pypy)[0-9.]*$")
_SHELLS = ("sh", "bash", "zsh", "dash", "ksh")
_READERS = ("cat", "head", "tail", "less", "more", "bat", "nl", "tac", "od", "xxd", "strings", "wc")                # every operand is a file that is read
_PATTERN_FIRST = ("grep", "egrep", "fgrep", "rg", "ag", "sed", "awk")                                              # the first operand is a pattern / script, the others are files that are read
_WRAPPERS = ("env", "time", "nohup", "command", "exec", "nice", "timeout", "sudo")                                  # run the next command word as it is
_INERT = ("echo", "printf", "ls", "git", "gh", "cp", "mv", "rm", "mkdir", "rmdir", "touch", "chmod", "chown", "ln", "stat", "file", "test", "[", "[[", "true", "false", "pwd", "diff", "find", "realpath",
          "dirname", "basename", "export", "unset", "set", "alias", "type", "which", "tee", "du", "df", "date", "sleep", "wait", "read", "npm", "pip", "pip3", "curl", "wget", "tar", "zip", "unzip")   # they name a path, they do not run or read it as a skill script

def is_jev(b): return str((b or {}).get("name") or "").startswith(JEV_PREFIX)

def _resolve(p, base):
    """Absolute normalized path of `p` (relative ones against the absolute `base`), else None. Pure string work: nothing is read, nothing is resolved through the filesystem."""
    if not isinstance(p, str) or not p or "\x00" in p or p.startswith("~") or "$" in p: return None
    if os.path.isabs(p): return os.path.normpath(p)
    return os.path.normpath(os.path.join(base, p)) if isinstance(base, str) and os.path.isabs(base) else None

def _is_skill_file(path):
    """`.../handoff-verify/scripts/<one of the skill's scripts>`, `.../handoff-verify/reference/<one of its five reference files>` or `.../handoff-verify/SKILL.md`, by path components (never a substring or a bare basename)."""
    parts = [x for x in path.split(os.sep) if x]
    return (len(parts) >= 3 and parts[-3:-1] == ["handoff-verify", "scripts"] and parts[-1] in SKILL_SCRIPTS) or (len(parts) >= 3 and parts[-3:-1] == ["handoff-verify", "reference"] and parts[-1] in SKILL_REFERENCES) \
        or (len(parts) >= 2 and parts[-2] == "handoff-verify" and parts[-1] == "SKILL.md")

def _is_scripts_dir(path):
    parts = [x for x in (path or "").split(os.sep) if x]
    return parts[-2:] == ["handoff-verify", "scripts"]

def _named(p):
    """Does the path text name a script of the skill (its basename), its SKILL.md (directly inside a `handoff-verify` component) or one of its five reference files (inside `handoff-verify/reference`: a bare `report.md` proves nothing, as a bare SKILL.md)?
    Only the name: where it is, is `_path_class`'s question."""
    if not isinstance(p, str) or not p: return False
    parts = [x for x in p.split(os.sep) if x]
    return bool(parts) and (parts[-1] in SKILL_SCRIPTS or (parts[-1] == "SKILL.md" and len(parts) >= 2 and parts[-2] == "handoff-verify") or (len(parts) >= 3 and parts[-3:-1] == ["handoff-verify", "reference"] and parts[-1] in SKILL_REFERENCES))

def _path_class(p, base):
    """yes / no / unknown for ONE path argument: yes = it resolves to a script of the skill, one of its five reference files or its SKILL.md; unknown = a path that names a script of the skill but cannot be resolved (relative without a recorded
    absolute base, `$VAR`, `~`): no resolution is ever invented from the components of the text (`..` or a leading `skills/handoff-verify/scripts/` do not make a path absolute); `no` for anything else."""
    r = _resolve(p, base)
    if r is not None: return "yes" if _is_skill_file(r) else "no"
    return "unknown" if _named(p) else "no"

_MENTION = re.compile(r"(?<![\w.-])(?:%s)(?![\w-])" % "|".join(re.escape(x) for x in SKILL_SCRIPTS))
def _mentions_skill(text): return "handoff-verify" in text or bool(_MENTION.search(text))

_PUNCT = "();<>|&"

def _unquoted_newlines(cmd, mark=None):
    """The command with every newline outside quotes turned into `;` (a newline separates commands like `;` does); a backslash-newline is a continuation. With `mark` (a dict operator character -> placeholder text) every
    operator character that is quoted or backslash-escaped is replaced by its placeholder, so that the tokeniser cannot take it for an operator (it is an ordinary character of a word, restored afterwards)."""
    out, q, i = [], None, 0
    cmd = cmd.replace("\\\n", " ")
    mark = mark or {}
    while i < len(cmd):
        ch = cmd[i]
        if q is None and ch == "\\" and i + 1 < len(cmd): out.append(mark.get(cmd[i + 1], cmd[i:i + 2])); i += 2; continue             # an escaped operator is a plain character (shlex drops the backslash as well)
        if q == '"' and ch == "\\" and i + 1 < len(cmd): out.append(cmd[i] + mark[cmd[i + 1]] if cmd[i + 1] in mark else cmd[i:i + 2]); i += 2; continue
        if ch in "'\"" and (q is None or q == ch): q = None if q else ch
        out.append(" ; " if ch == "\n" and q is None else mark[ch] if q is not None and ch in mark else ch); i += 1
    return "".join(out)

_OPS = re.compile(r"&&|\|\||[;&|()]")

def _shell_items(cmd):
    """The command as an ordered list of ("seg", before, after, words, io) / ("push", before) / ("pop", after): `before` / `after` are the operators (`;`, `&&`, `||`, `&`, `|`, None at the ends) around a simple command, or, for
    a group `( ... )`, the operator in front of its `(` and the one behind its `)`; `io` = dict(outs [(target, append)], ins [target]) holds the LITERAL words after `>` / `>>` / `>|` / `&>` and after `<` (a descriptor
    duplication `>&`, a here-document / here-string and a process substitution name no file). Redirections and their targets are not words of the command. Only an UNQUOTED, unescaped operator character is an operator:
    `echo '>' f`, `echo ">" f`, `echo x \\> f` and `echo 'a;b'` have no redirect and no separator (the quoted characters travel as two-character placeholders built on an escape character, and the escape character of the command itself
    travels as `\ue000` + `0`: no character of the command can collide with them, however many private-use characters it holds, and nothing is exhausted; they come back into the words). Raises ValueError when the command cannot be tokenised."""
    import shlex
    esc = "\ue000"; mark = {ch: esc + str(k) for k, ch in enumerate(_PUNCT, 1)}; back = {str(k): ch for k, ch in enumerate(_PUNCT, 1)}; back["0"] = esc
    restore = lambda t: re.sub(esc + "(.)", lambda m: back[m.group(1)], t)
    lex = shlex.shlex(_unquoted_newlines(cmd.replace(esc, esc + "0"), mark), posix=True, punctuation_chars=True); lex.whitespace_split = True; toks = list(lex)
    items, cur, before, mode, outs, ins = [], [], None, None, [], []
    def finish(after):
        nonlocal cur, before, outs, ins
        if cur: items.append(("seg", before, after, cur, dict(outs=outs, ins=ins)))
        cur, outs, ins = [], [], []; before = after
    for t in toks:
        if t and all(ch in _PUNCT for ch in t):
            if "<" in t or ">" in t:
                mode = ("in",) if t == "<" else ("out", t.endswith(">>")) if t in (">", ">>", ">|", "&>", "&>>") else ("skip",)
                continue
            mode = None
            for op in _OPS.findall(t):
                if op == "(": group = before; finish(";"); items.append(("push", group)); before = ";"
                elif op == ")": finish(";"); items.append(("pop", None)); before = ";"
                else:
                    if not cur and items and items[-1] == ("pop", None): items[-1] = ("pop", op)
                    finish(op)
            continue
        t = restore(t)
        if mode:
            if mode[0] == "out": outs.append((t, mode[1]))
            elif mode[0] == "in": ins.append(t)
            mode = None; continue
        cur.append(t)
    finish(None)
    return items

def _plan(items, cls="always", rest_ok=True):
    """What each simple command of `_shell_items` demonstrably did, aligned with `items`: (class, tail) for a "seg", None for the rest. The class: "always" = it ran whatever the exit statuses (the first command of a list,
    or one behind `;` / a newline, not in the background, in a group that itself ran); "ok" = it ran whenever the WHOLE command exited 0 (it follows `&&` and every operator from it to the end of the command, the ends of
    the groups included, is `&&`: the last `&&` chain); None = not demonstrated (behind `||` / `&`, followed by `;` / `||` / `&`, in a group that is not demonstrated). The members of a pipeline share the class of the
    pipeline. `tail` = the exit status of the command is the status of the whole command (only `&&` follows it). The global success of a call is never taken as proof of a command that a status may have skipped. `cls` and
    `rest_ok` start the plan inside another command (`bash -c '...'` under the class and tail of the command around it)."""
    root, stack, opened = [], [], []; cur = root
    for k, it in enumerate(items):
        if it[0] == "seg": cur.append(dict(k=k, before=it[1], after=it[2], kids=None))
        elif it[0] == "push":
            nd = dict(k=k, before=it[1], after=None, kids=[]); cur.append(nd); stack.append(cur); opened.append(nd); cur = nd["kids"]
        elif stack:
            if cur: cur[-1]["after"] = None                               # the end of the group is not an operator
            cur = stack.pop(); opened.pop()["after"] = it[1]
    if root and root[-1]["after"] == ";": root[-1]["after"] = None        # a trailing `;`
    out = [None] * len(items)
    def level(nodes, cls, rest_ok):
        units, i = [], 0
        while i < len(nodes):
            j = i
            while nodes[j]["after"] == "|" and j + 1 < len(nodes): j += 1
            units.append((i, j)); i = j + 1
        tail = [False] * (len(units) + 1); tail[len(units)] = rest_ok
        for u in range(len(units) - 1, -1, -1): tail[u] = nodes[units[u][1]]["after"] in ("&&", None) and tail[u + 1]
        for u, (a, b) in enumerate(units):
            before, after = (nodes[a - 1]["after"] if a else None), nodes[b]["after"]
            local = None if after == "&" else "always" if before in (None, ";") else "ok" if before == "&&" and tail[u] else None
            c = None if local is None or cls is None else "ok" if "ok" in (cls, local) else "always"
            for m in range(a, b + 1):
                nd = nodes[m]; last = tail[u] and m == b
                if nd["kids"] is None: out[nd["k"]] = (c, last)
                else: level(nd["kids"], c, last)
    level(root, cls, rest_ok)
    return out

_VALUE_OPTS = {   # options that take a value (separate, or attached to a short option / after `=` on a long one), by command family
    "grep": ("-e", "-f", "-m", "-A", "-B", "-C", "-d", "-D", "--regexp", "--file", "--max-count", "--after-context", "--before-context", "--context", "--directories", "--devices", "--include", "--exclude", "--exclude-dir", "--exclude-from", "--label", "--binary-files"),
    "rg": ("-e", "-f", "-m", "-A", "-B", "-C", "-g", "-t", "-T", "-j", "-M", "-E", "--regexp", "--file", "--max-count", "--glob", "--iglob", "--type", "--type-not", "--threads", "--context", "--after-context", "--before-context", "--max-columns", "--encoding", "--ignore-file"),
    "sed": ("-e", "-f", "-l", "--expression", "--file", "--line-length"),
    "awk": ("-f", "-v", "-F", "--file", "--assign", "--field-separator"),
}
_PATTERN_OPTS = ("-e", "--regexp", "--expression")      # the value is a pattern / script text, not a file
_FILE_OPTS = ("-f", "--file")                            # the value is a file that is READ (patterns, a script, an awk program)

def _file_operands(b, args):
    """The paths a grep / egrep / fgrep / rg / ag / sed / awk command READS: the values of -f / --file and the operands after the pattern (script, program); the pattern given by -e / --regexp / --expression, the first operand
    when no option gave it, and the values of every other option are not files. `--` ends the options."""
    fam = "grep" if b in ("grep", "egrep", "fgrep") else "rg" if b in ("rg", "ag") else b; vopts = _VALUE_OPTS.get(fam, ()); files, have, pos, i = [], False, [], 0
    while i < len(args):
        a = args[i]; i += 1
        if a == "--": pos += args[i:]; break
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name in _PATTERN_OPTS: have = True; i += 0 if eq else 1
            elif name in _FILE_OPTS:
                have = True
                if not eq and i < len(args): val = args[i]
                if not eq: i += 1
                files.append(val)
            elif name in vopts and not eq: i += 1
            continue
        if a.startswith("-") and a != "-":
            letters = a[1:]
            for k, ch in enumerate(letters):
                opt = "-" + ch
                if opt not in vopts: continue
                val = letters[k + 1:]
                if not val and i < len(args): val = args[i]
                if not letters[k + 1:]: i += 1
                if opt in _PATTERN_OPTS: have = True
                elif opt in _FILE_OPTS: have = True; files.append(val)
                break
            continue
        pos.append(a)
    if not have and pos: pos = pos[1:]
    return [f for f in files if f] + pos

class _Frame(dict):
    """The local names of a function that `_PayloadFlow` runs; `glob` / `nonl` = the names it declared `global` / `nonlocal` (their bindings go to the outer maps)."""
    glob = nonl = frozenset()

class _PayloadFlow:
    """A `python -c` payload read in the order and the context it would run (stdlib `ast`; NOTHING is executed): the statements of the module in order, a definition only when it is called (by name, with the `sys.path` of
    that moment). A name is what it was bound to at that point: `import_module` is `importlib.import_module` only through `import importlib [as x]` / `from importlib import import_module [as y]` (and plain aliases of
    them), `__import__` only while nothing shadows it, `sys.path.insert/append` only through a bound `sys`. What may or may not run (an `if` whose test is not a constant, loops, handlers, cases, `a and b`, `x if c else y`,
    comprehensions, a definition used as a value or decorated, the methods of a class that is used) is MAYBE, and so is every binding it makes: the alternatives are read apart from each other, each from the state before
    the statement (a loop also after one and two rounds, a handler from a state the body may or may not have reached), and the names they bind are joined conservatively afterwards (`("alt", values)`; an alternative that
    did not bind the name contributes "undefined"), so a branch never installs a certain binding and one branch never sees what another one bound. Inside a function or lambda the names it assigns (`:=` included), imports or defines anywhere are
    local from its start and `unbound` until then (`global` / `nonlocal` send the binding to the module / the enclosing function): a call through a name that is not bound yet demonstrates nothing and is never resolved
    from the outer scope (a MAYBE import when it names a script of the skill). CREATING a callable or an iterator is not running it: a lambda or generator expression that is only built runs nothing (but its defaults / its
    first iterable), a generator function (or a lambda holding `yield`) that is called and discarded runs nothing; when its result is used the body MAY run. Values: ("mod", name), ("def", node, scope, is_async), ("class", methods), ("import",),
    ("import_module",), ("syspath",), ("exec",), ("other",), ("unbound",), ("alt", values). Results in `imports` = (literal directories on the path then, whether the path is not demonstrated, sure)."""
    LIMIT, STEPS = 8, 4000
    OTHER, UNBOUND, UNDEF = ("other",), ("unbound",), ("undef",)
    IMPORT, IMPORT_MODULE, SYSPATH = ("import",), ("import_module",), ("syspath",)
    def __init__(self):
        self.dirs, self.unknown, self.imports, self.exec, self.active, self.escaped, self.steps, self.incomplete = [], False, [], False, [], set(), 0, False
        self.scans = {}

    def lit(self, n): return n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) else None

    # --- the state: one dict per scope, innermost first (a ChainMap), the module a plain dict -------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def maps(env): return env.maps if isinstance(env, collections.ChainMap) else [env]
    def snap(self, env): return [dict(m) for m in self.maps(env)]
    def restore(self, env, st):
        for m, s in zip(self.maps(env), st): m.clear(); m.update(s)
    def same(self, a, b):
        if a is b: return True
        if a[0] != b[0]: return False
        if a[0] == "def": return a[1] is b[1] and a[2] is b[2]
        return False if a[0] == "class" else a == b
    def join(self, vals):
        """The value of a name that is one of `vals` (alternatives) afterwards: the same value when they agree, else ("alt", the distinct values)."""
        out = []
        for v in vals:
            for m in (v[1] if v[0] == "alt" else (v,)):
                if not any(self.same(m, o) for o in out): out.append(m)
        return out[0] if len(out) == 1 else ("alt", tuple(out))
    def merge(self, env, states):
        for i, m in enumerate(self.maps(env)):
            new = {}
            for nm in set().union(*(s[i] for s in states)):
                v = self.join([s[i].get(nm, self.UNDEF) for s in states])
                if v != self.UNDEF: new[nm] = v
            m.clear(); m.update(new)
    def alts(self, env, branches, skip=False):
        """Run each alternative from the state before it (`skip` adds the alternative where none of them ran) and join what they bound."""
        self.steps += 1
        if self.steps > self.STEPS: self.incomplete = True; return
        pre = self.snap(env); states = []
        for b in branches: self.restore(env, pre); b(); states.append(self.snap(env))
        self.restore(env, pre)
        if skip: states.append(pre)
        self.merge(env, states)
    def maybe(self, env, fn): self.alts(env, [fn], True)
    def bind(self, env, name, v):
        fr = env.maps[0] if isinstance(env, collections.ChainMap) else None
        if isinstance(fr, _Frame):
            if name in fr.glob: env.maps[-1][name] = v; return
            if name in fr.nonl:
                for m in env.maps[1:-1]:
                    if name in m: m[name] = v; return
                return                                                       # not modelled: nothing is installed
        env[name] = v
    def lookup(self, env, name):
        v = env.get(name); d = self.IMPORT if name == "__import__" else ("exec",) if name in ("exec", "eval", "compile") else None
        if v is None: return d
        return self.join([(d or self.OTHER) if m == self.UNDEF else m for m in v[1]]) if v[0] == "alt" else v
    def attr(self, base, attr):
        def one(b):
            if b == ("mod", "importlib") and attr == "import_module": return self.IMPORT_MODULE
            if b == ("mod", "sys") and attr == "path": return self.SYSPATH
            return self.UNBOUND if b == self.UNBOUND else self.OTHER
        v = self.join([one(b) for b in (base[1] if base[0] == "alt" else (base,))])
        return None if v == self.OTHER else v
    def kind(self, n, env):
        """The value an expression names without evaluating it (a name, an attribute of a bound module, a lambda) or None."""
        if isinstance(n, ast.Name): return self.lookup(env, n.id)
        if isinstance(n, ast.Lambda): return ("def", n, env, False)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            base = self.lookup(env, n.value.id)
            return self.attr(base, n.attr) if base is not None else None
        return None
    def scan(self, fn):
        """(names bound in the body of a function or lambda, its `global` names, its `nonlocal` names, whether it is a generator): nested definitions and classes bind only their name."""
        got = self.scans.get(id(fn))
        if got: return got
        bound, glob, nonl, gen = set(), set(), set(), False
        stack = [fn.body] if isinstance(fn, ast.Lambda) else list(fn.body)
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(n.name); stack.extend(n.decorator_list)
                stack.extend(n.bases + [k.value for k in n.keywords] if isinstance(n, ast.ClassDef) else n.args.defaults + [d for d in n.args.kw_defaults if d]); continue
            if isinstance(n, ast.Lambda): stack.extend(n.args.defaults + [d for d in n.args.kw_defaults if d]); continue
            if isinstance(n, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):                      # the targets of a comprehension are its own
                stack.extend([n.key, n.value] if isinstance(n, ast.DictComp) else [n.elt])
                for g in n.generators: stack.append(g.iter); stack.extend(g.ifs)
                continue
            if isinstance(n, ast.Global): glob.update(n.names)
            elif isinstance(n, ast.Nonlocal): nonl.update(n.names)
            elif isinstance(n, (ast.Yield, ast.YieldFrom)): gen = True
            elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)): bound.add(n.id)
            elif isinstance(n, (ast.Import, ast.ImportFrom)): bound.update((al.asname or al.name.split(".")[0]) for al in n.names if al.name != "*")
            elif isinstance(n, ast.ExceptHandler) and n.name: bound.add(n.name)
            elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name: bound.add(n.name)
            elif isinstance(n, ast.MatchMapping) and n.rest: bound.add(n.rest)
            stack.extend(ast.iter_child_nodes(n))
        got = self.scans[id(fn)] = (frozenset(bound - glob - nonl), frozenset(glob), frozenset(nonl), gen)
        return got

    # --- definitions and calls ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------
    def defaults(self, fn, env, sure):
        """The default values and decorators of a definition are evaluated when it is defined."""
        for x in fn.args.defaults + [d for d in fn.args.kw_defaults if d] + getattr(fn, "decorator_list", []): self.expr(x, env, sure)
    def record(self, name, sure):
        if isinstance(name, str) and name.split(".")[0] in _STEMS: self.imports.append((tuple(self.dirs), self.unknown, sure))
    def escape(self, v):
        """A definition (or a class) used as a value may be called by whoever received it: its body is MAYBE."""
        if v is None: return
        if v[0] == "alt":
            for m in v[1]: self.escape(m)
            return
        if v[0] not in ("def", "class") or id(v[1]) in self.escaped: return
        self.escaped.add(id(v[1]))
        for m in (v[1] if v[0] == "class" else [v]): self.invoke(m, False)
    def invoke(self, fn, sure):
        node, scope = fn[1], fn[2]
        if node in self.active: return                                   # the recursion adds nothing: its effects are already being counted
        self.steps += 1
        if len(self.active) >= self.LIMIT or self.steps > self.STEPS: self.incomplete = True; return
        a = node.args; names = [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs] + [x.arg for x in (a.vararg, a.kwarg) if x]
        frame = _Frame({n: self.OTHER for n in names}); sure = sure and not fn[3]
        bound, frame.glob, frame.nonl, _ = self.scan(node)                    # a lambda too: `:=` in its body makes the name local in the whole lambda
        for nm in bound:
            if nm not in frame: frame[nm] = self.UNBOUND                      # local from the start of the function, not the outer name, until it is assigned
        env = collections.ChainMap(frame, *self.maps(scope)); outer = None if sure else collections.ChainMap(*self.maps(scope)); pre = outer is not None and self.snap(outer)
        self.active.append(node)
        try:
            if isinstance(node, ast.Lambda): self.expr(node.body, env, sure)
            else: self.block(node.body, env, sure)
        finally:
            self.active.pop()
            if outer is not None: self.merge(outer, [pre, self.snap(outer)])    # what a call that may not happen bound in the enclosing scopes is only maybe bound

    # --- statements -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
    def block(self, body, env, sure):
        res = None
        for st in body:
            r = self.stmt(st, env, sure)
            if r == "ret": return "ret" if sure else "mret"
            if r == "mret": sure, res = False, "mret"
        return res
    def targets(self, t, env, value=None, sure=True):
        if isinstance(t, ast.Name): self.bind(env, t.id, value or self.OTHER)
        elif isinstance(t, (ast.Tuple, ast.List, ast.Starred)):
            for x in (t.elts if not isinstance(t, ast.Starred) else [t.value]): self.targets(x, env, None, sure)
        else: self.expr(t, env, sure); self.escape(value)                    # stored into an object: whoever reads it may call it
    def stmt(self, st, env, sure):
        T = ast
        if isinstance(st, T.Import):
            for al in st.names:
                self.record(al.name, sure); self.bind(env, al.asname or al.name.split(".")[0], ("mod", al.name if al.asname else al.name.split(".")[0]))
        elif isinstance(st, T.ImportFrom):
            if not st.level: self.record(st.module, sure)
            for al in st.names:
                nm = al.asname or al.name; ok = not st.level
                if al.name == "*":                                           # the public names of the module: importlib's and builtins' carry the authentic callables
                    if ok and st.module == "importlib": self.bind(env, "import_module", self.IMPORT_MODULE); self.bind(env, "__import__", self.IMPORT)
                    elif ok and st.module == "builtins": self.bind(env, "__import__", self.IMPORT)
                    continue
                self.bind(env, nm, self.IMPORT_MODULE if (ok and st.module == "importlib" and al.name == "import_module") else self.IMPORT if (ok and st.module == "builtins" and al.name == "__import__") else self.OTHER)
        elif isinstance(st, (T.FunctionDef, T.AsyncFunctionDef)):
            self.defaults(st, env, sure); v = ("def", st, env, isinstance(st, T.AsyncFunctionDef))
            if st.decorator_list: self.escape(v); self.bind(env, st.name, self.OTHER)
            else: self.bind(env, st.name, v)
        elif isinstance(st, T.ClassDef):
            for x in st.bases + [k.value for k in st.keywords] + st.decorator_list: self.expr(x, env, sure)
            cenv = collections.ChainMap({}, *self.maps(env)); methods = []
            for sub in st.body:
                if isinstance(sub, (T.FunctionDef, T.AsyncFunctionDef)):
                    self.defaults(sub, cenv, sure)
                    methods.append(("def", sub, env, isinstance(sub, T.AsyncFunctionDef))); cenv[sub.name] = self.OTHER
                else: self.stmt(sub, cenv, sure)
            v = ("class", methods); self.bind(env, st.name, v)
            if st.decorator_list: self.escape(v)
        elif isinstance(st, T.Return):
            if st.value is not None: self.expr(st.value, env, sure)
            return "ret"
        elif isinstance(st, T.Expr):
            v = st.value
            if isinstance(v, T.Call): self.call(v, env, sure, True)             # the result is thrown away
            elif isinstance(v, T.Lambda): self.defaults(v, env, sure)            # created and thrown away: its body does not run
            elif isinstance(v, T.GeneratorExp): self.expr(v.generators[0].iter, env, sure)        # only the first iterable is evaluated when the generator is created
            else: self.expr(v, env, sure)
        elif isinstance(st, T.Assign):
            k = self.kind(st.value, env)
            if isinstance(st.value, T.Lambda): self.defaults(st.value, env, sure)
            elif not (isinstance(st.value, (T.Name, T.Attribute)) and k is not None): self.expr(st.value, env, sure)
            for t in st.targets: self.targets(t, env, k, sure)
        elif isinstance(st, (T.AnnAssign, T.AugAssign)):
            if st.value is not None: self.expr(st.value, env, sure)
            self.targets(st.target, env, None, sure)
        elif isinstance(st, T.If):
            if isinstance(st.test, T.Constant): return self.block(st.body if st.test.value else st.orelse, env, sure)
            self.expr(st.test, env, sure); res = []
            self.alts(env, [lambda: res.append(self.block(st.body, env, False)), lambda: res.append(self.block(st.orelse, env, False))])
            return "mret" if any(res) else None
        elif isinstance(st, (T.For, T.AsyncFor, T.While)):
            loop = isinstance(st, T.While); self.expr(st.test if loop else st.iter, env, sure)
            def once():
                if not loop: self.targets(st.target, env, None, False)
                self.block(st.body, env, False)
            def twice(): once(); (self.expr(st.test, env, False) if loop else None); once()
            def els(): self.block(st.orelse, env, False)
            self.alts(env, [els, once, lambda: (once(), els()), twice, lambda: (twice(), els())], True)         # no round, one, two (the second sees what the first bound); `break` skips the else
        elif isinstance(st, (T.With, T.AsyncWith)):
            for it in st.items:
                self.expr(it.context_expr, env, sure)
                if it.optional_vars is not None: self.targets(it.optional_vars, env, None, sure)
            return self.block(st.body, env, sure)
        elif isinstance(st, (T.Try, getattr(T, "TryStar", T.Try))):
            pre = self.snap(env); r = self.block(st.body, env, sure); post = self.snap(env)
            if st.handlers: self.merge(env, [pre, post])                    # a handler starts from a state the body may or may not have reached
            start = self.snap(env); states = []
            self.restore(env, post); self.block(st.orelse, env, False); states.append(self.snap(env))
            for h in st.handlers:
                self.restore(env, start)
                if h.name: self.bind(env, h.name, self.OTHER)
                if h.type is not None: self.expr(h.type, env, False)
                self.block(h.body, env, False); states.append(self.snap(env))
            self.merge(env, states); f = self.block(st.finalbody, env, sure)
            return f or r
        elif isinstance(st, T.Assert): self.expr(st.test, env, sure); (self.maybe(env, lambda: self.expr(st.msg, env, False)) if st.msg is not None else None)
        elif isinstance(st, T.Raise):
            for x in (st.exc, st.cause):
                if x is not None: self.expr(x, env, sure)
        elif isinstance(st, T.Delete):
            for x in st.targets:
                if isinstance(x, T.Name): self.bind(env, x.id, self.OTHER)
                else: self.expr(x, env, sure)
        elif isinstance(st, T.Match):
            self.expr(st.subject, env, sure)
            def case(c):
                def run():
                    for n in ast.walk(c.pattern):
                        for nm in (getattr(n, "name", None), getattr(n, "rest", None)):
                            if isinstance(nm, str): self.bind(env, nm, self.OTHER)
                    if c.guard is not None: self.expr(c.guard, env, False)
                    self.block(c.body, env, False)
                return run
            self.alts(env, [case(c) for c in st.cases], True)
        elif isinstance(st, (T.Global, T.Nonlocal, T.Pass, T.Break, T.Continue)): pass
        else:
            def rest():
                for ch in ast.iter_child_nodes(st):
                    if isinstance(ch, T.expr): self.expr(ch, env, False)
                    elif isinstance(ch, T.stmt): self.stmt(ch, env, False)
            self.maybe(env, rest)
        return None

    # --- expressions ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
    def expr(self, n, env, sure):
        T = ast
        if isinstance(n, T.Call): return self.call(n, env, sure)
        if isinstance(n, T.Name):
            if isinstance(n.ctx, T.Load): self.escape(self.lookup(env, n.id))
            return
        if isinstance(n, T.Lambda): self.defaults(n, env, sure); return self.escape(("def", n, env, False))     # used as a value: whoever receives it may call it
        if isinstance(n, T.IfExp):
            self.expr(n.test, env, sure); self.alts(env, [lambda: self.expr(n.body, env, False), lambda: self.expr(n.orelse, env, False)]); return
        if isinstance(n, T.BoolOp):
            self.expr(n.values[0], env, sure)
            for v in n.values[1:]: self.maybe(env, lambda v=v: self.expr(v, env, False))
            return
        if isinstance(n, (T.ListComp, T.SetComp, T.GeneratorExp, T.DictComp)):
            def comp():
                cenv = collections.ChainMap({}, *self.maps(env))
                for k, g in enumerate(n.generators):
                    self.expr(g.iter, cenv, sure and k == 0); self.targets(g.target, cenv, None, False)
                    for c in g.ifs: self.expr(c, cenv, False)
                for e in ([n.key, n.value] if isinstance(n, T.DictComp) else [n.elt]): self.expr(e, cenv, False)
            self.maybe(env, comp); return
        if isinstance(n, T.NamedExpr): self.expr(n.value, env, sure); self.targets(n.target, env, self.kind(n.value, env), sure); return
        for ch in ast.iter_child_nodes(n):
            if isinstance(ch, T.expr): self.expr(ch, env, sure)
            elif isinstance(ch, T.keyword): self.expr(ch.value, env, sure)

    def call(self, n, env, sure, discard=False):
        """A call: the callee is what its name is bound to NOW (when the alternatives differ every one is tried, as MAYBE; when they all import, it is an import); `discard` = the result is thrown away, so a generator
        function that is called creates its generator and nothing else (when the result is used, the body may run)."""
        f = n.func; k = self.kind(f, env); ks = list(k[1] if k is not None and k[0] == "alt" else (k,))
        if isinstance(f, ast.Attribute) and f.attr in ("insert", "append"):
            bk = self.kind(f.value, env); bs = [] if bk is None else list(bk[1] if bk[0] == "alt" else (bk,))
            if self.UNBOUND in bs: self.unknown = True                       # `sys` is not bound yet: the path may have changed
            if self.SYSPATH in bs: ks = [("path", f.attr) if b == self.SYSPATH else self.OTHER for b in bs]
        ks = [m for m in ks if m is not None]
        if not ks or all(m[0] == "other" for m in ks):
            if not isinstance(f, ast.Name): self.expr(f, env, sure)
        elif any(m[0] in ("def", "class") for m in ks) and not isinstance(f, (ast.Name, ast.Lambda)): self.expr(f, env, sure)
        if isinstance(f, ast.Lambda): self.defaults(f, env, sure)
        for x in n.args + [kw.value for kw in n.keywords]: self.expr(x, env, sure)
        imports = len(ks) > 1 and all(m[0] in ("import", "import_module") for m in ks)
        if len(ks) > 1 and not imports: sure = False                          # alternatives that differ: whichever one ran, none is demonstrated
        for m in ks[:1] if imports else ks: self.apply(m, n, sure, discard)
    def apply(self, k, n, sure, discard):
        if k[0] in ("import", "import_module", "unbound"):
            arg = n.args[0] if n.args else next((kw.value for kw in n.keywords if kw.arg == "name"), None); self.record(self.lit(arg), sure and k[0] != "unbound")
        elif k[0] == "path":
            d = self.lit(n.args[-1]) if (n.args and (k[1] == "append" or len(n.args) == 2)) else None
            if d is None or not sure: self.unknown = True
            elif os.path.isabs(d): self.dirs.append(d)
        elif k[0] == "exec":
            if any(isinstance(c, ast.Constant) and isinstance(c.value, str) and (_mentions_skill(c.value) or (lambda r: bool(r and r[0]))(_payload_imports(c.value))) for x in n.args for c in ast.walk(x)): self.exec = True
        elif k[0] == "def":
            if self.scan(k[1])[3]:          # a generator function (or a lambda holding `yield`): the call creates the generator and runs no line of the body
                if not discard: self.invoke(k, False)                          # the generator may be consumed by whoever receives it
            else: self.invoke(k, sure)
        elif k[0] == "class": self.escape(k)

def _payload_imports(code):
    """What a `python -c` payload does that matters for the skill, in the ORDER and CONTEXT it would run (`_PayloadFlow`, stdlib `ast`, nothing is executed): -> (imports, dynamic) or None when the payload is not valid
    Python. Strings and comments never count: `print("example import report")` imports nothing. An import = an `import X` / `from X import ...` statement of a module whose top-level name is a script of the skill, or
    `__import__("X")` / `importlib.import_module("X")` (as bound, see `_PayloadFlow`) with a literal; each one is (the literal absolute directories that `sys.path.insert/append` had put on the path BEFORE it, whether the path
    is not demonstrated at that point (a non-literal change, or one that may not have happened), whether it surely ran). A definition that is never called, a later `sys.path` change, a local callable named
    `import_module`, a lambda / generator expression that is only built and a generator function that is called and discarded demonstrate nothing; a binding made in a branch is only maybe made, and a function's local name
    is not the outer one before its own assignment (see `_PayloadFlow`). `dynamic` = "exec" when an `exec` / `eval` / `compile` of a text that mentions the skill or imports one of its scripts can run (or the analysis was cut short)."""
    try:
        tree = ast.parse(code); flow = _PayloadFlow(); flow.block(tree.body, {}, True)
    except (SyntaxError, ValueError, RecursionError, MemoryError): return None
    return flow.imports, "exec" if (flow.exec or flow.incomplete) else None

def _py_class(args, here, env):
    """`python [flags] SCRIPT` / `-m MODULE` / `-c CODE`: SCRIPT is a path (judged against the directory it runs in); a module or an import is the skill's only when the import path demonstrably holds its scripts directory
    (the directory, a PYTHONPATH entry, a literal `sys.path.insert` / `append` that ran BEFORE the import). The payload is read in order and context (`_payload_imports`): each import is judged against the path of its own
    moment (a later change resolves nothing backwards), an import that surely ran and resolves is `yes`, one that may or may not run (a condition, a loop, a handler, a definition used as a value, a call through a name
    bound only in a branch or not bound yet in its function) and would resolve is `unknown`, a definition that is never called or a callable / iterator that is only created demonstrates nothing (`no`), and an `exec` that can run is `unknown`."""
    i, mod, code, script = 0, None, None, None
    while i < len(args):
        a = args[i]
        if a == "-m": mod = args[i + 1] if i + 1 < len(args) else None; break
        if a == "-c": code = args[i + 1] if i + 1 < len(args) else None; break
        if a in ("-W", "-X", "-Q"): i += 2; continue
        if a.startswith("-m") and len(a) > 2: mod = a[2:]; break
        if a.startswith("-") and a != "-": i += 1; continue
        script = a; break
    def import_dirs(extra=()):
        dirs, unknown = [here] if here else [], here is None
        for d in [x for x in (env.get("PYTHONPATH") or "").split(":") if x] + list(extra):
            r = _resolve(d, here)
            if r is None: unknown = True
            else: dirs.append(r)
        return "yes" if any(_is_scripts_dir(d) for d in dirs) else ("unknown" if unknown else "no")
    if script is not None: return _path_class(script, here) if script != "-" else "no"
    if mod is not None: return import_dirs() if mod.split(".")[0] in _STEMS else "no"
    if code is not None:
        facts = _payload_imports(code)
        if facts is None: return "unknown" if _mentions_skill(code) else "no"      # not valid Python: nothing is demonstrated, and a mention alone is not an import
        imports, dynamic = facts
        if dynamic == "exec": return "unknown"
        best = "no"
        for dirs, path_unknown, sure in imports:                                      # each import against the path of ITS moment
            r = "unknown" if path_unknown else import_dirs(m for m in dirs if os.path.isabs(m))
            if r == "yes" and not sure: r = "unknown"
            if r == "yes": return "yes"
            if r == "unknown": best = "unknown"
        return best
    return "no"

def _split_cmd(words):
    """(environment assignments, command word, arguments) of one simple command with the wrappers (`env`, `time`, `nohup`, ...) and the leading `NAME=value` words removed, or None when no command word is left."""
    env, i = {}, 0
    while i < len(words) and _ASSIGN.match(words[i]): k, v = words[i].split("=", 1); env[k] = v; i += 1
    while i < len(words) and os.path.basename(words[i]) in _WRAPPERS:
        w = os.path.basename(words[i]); i += 1
        while i < len(words) and (words[i].startswith("-") or (w == "env" and _ASSIGN.match(words[i]))):
            if w == "env" and _ASSIGN.match(words[i]): k, v = words[i].split("=", 1); env[k] = v
            i += 2 if words[i] in ("-u", "-n") and w in ("env", "nice") else 1
        if w == "timeout" and i < len(words): i += 1                                  # the duration
    return (env, words[i], words[i + 1:]) if i < len(words) else None

def _walk(items, cwd, cls="always", rest_ok=True):
    """The simple commands of `_shell_items` with the directory each one runs in: -> (words, here, before, after, io, plan) (`plan` = (class, tail) of `_plan`, started with `cls` / `rest_ok`). `cd X` moves the following command only through `&&` (a `;` or a newline runs the next command also when the cd
    failed: the directory is then not demonstrated); behind `||`, `&` or `|`, or as a conditional command, the directory is not demonstrated (None) or unchanged, and a group `( ... )` ends with its parenthesis."""
    here = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else None; stack, blind_next = [], False; plan = _plan(items, cls, rest_ok)
    for k, it in enumerate(items):
        if it[0] == "push": stack.append(here); continue
        if it[0] == "pop":
            if stack: here = stack.pop()
            continue
        _, before, after, words, io = it
        if words[0] == "cd" or words[0] in ("pushd", "popd"):
            target = next((w for w in words[1:] if not w.startswith("-") or w == "-"), None)
            new = _resolve(target, here) if words[0] == "cd" and target not in (None, "-") else None
            if before not in (None, ";", "&&"): here = None                      # a conditional or background cd: it may or may not have happened
            elif after in ("&", "|"): pass                                       # it ran in another process: this directory is unchanged
            elif after == "||": blind_next = True                                # the next command runs only when the cd failed (this directory), later ones may be in either
            elif after == "&&": here = new                                       # the next command runs only when the cd succeeded
            else: here = None                                                    # `;` / a newline runs the next command also when the cd failed: the directory is not demonstrated
            continue
        yield words, here, before, after, io, plan[k]
        if blind_next: here = None; blind_next = False

def _seg_class(words, here):
    """yes / no / unknown for one simple command: what it RUNS or READS decides, not what it merely names (see `_bash_class`)."""
    head = _split_cmd(words)
    if head is None: return "no"
    env, cmd, args = head; b = os.path.basename(cmd)
    if _PYTHON.match(b): return _py_class(args, here, env)
    if b in _SHELLS:
        for k, a in enumerate(args):
            if a.startswith("-") and not a.startswith("--") and "c" in a[1:] and k + 1 < len(args): return _bash_class(args[k + 1], here)
        files = [a for a in args if not a.startswith("-")]
        return _path_class(files[0], here) if files else "no"
    if cmd in ("source", "."):
        return _path_class(args[0], here) if args else "no"
    if b in _READERS or b in _PATTERN_FIRST:
        files = _file_operands(b, args) if b in _PATTERN_FIRST else [a for a in args if not a.startswith("-")]
        out = "no"
        for f in files:
            c = _path_class(f, here)
            if c == "yes": return "yes"
            if c == "unknown": out = "unknown"
        return out
    if b in _INERT and "/" not in cmd: return "no"
    if "/" in cmd or b in SKILL_SCRIPTS: return _path_class(cmd if "/" in cmd else b, here)   # the command word itself is a path (direct execution) or a bare script name
    out = "no"
    for a in args:
        if ("/" in a or a in SKILL_SCRIPTS) and _path_class(a, here) != "no": out = "unknown"   # an unrecognised command that names a skill script: not demonstrable either way
    return out

_VAR = re.compile(r"\$(?:(\w+)|\{(\w+)\})")
_UNSAFE_VALUE = re.compile(r"[\s$`~*?\[\]{}!\\\"']")      # a value that is not one plain word: spaces, another expansion, a glob, a tilde, a quote: nothing is demonstrated
_MUTATORS = frozenset(("eval", "source", ".", "read", "export", "declare", "typeset", "local", "unset", "getopts", "mapfile", "readarray", "set", "shift", "let", "for", "while", "until", "select", "function", "trap"))      # a later command that can change a variable in a way the words do not show

def _dollar_safe(cmd):
    """False when a `$` sits inside single quotes or is escaped: the shell does not expand it, and the tokeniser has already lost the quotes."""
    q, i = None, 0
    while i < len(cmd):
        ch = cmd[i]
        if q == "'":
            if ch == "'": q = None
            elif ch == "$": return False
        elif ch == "\\":
            if cmd[i + 1:i + 2] == "$": return False
            i += 1
        elif q == '"':
            if ch == '"': q = None
        elif ch in "'\"": q = ch
        i += 1
    return True

def _expand(word, table):
    """`word` with every `$NAME` / `${NAME}` replaced by its demonstrated value (the word itself when it holds no `$`); None when a `$` is left that no value explains. The values hold no `$`, so what is left is unexplained."""
    if "$" not in word: return word
    out = _VAR.sub(lambda m: table.get(m.group(1) or m.group(2), m.group(0)), word)
    return None if "$" in out else out

def _expand_vars(items, cmd):
    """A script path built from a shell variable is expanded ONLY when the same command assigns the variable earlier, on the straight line of the command, a value that is one plain absolute word, possibly built from variables assigned the same way (`S=/x; SK=$S/handoff-verify`).
    The assignment must be a plain `NAME=value` statement that certainly ran: at the top level (not in a group), joined by `;` or a newline (by `&&` only while every statement before it is also an assignment), not behind `||`, not in a pipeline or in the background, no redirect.
    Words are expanded in order, with the value in force at that point, so the last assignment wins. The words are then exactly what the literal command would have been, and every consumer classifies them as it classifies the literal path. Nothing else is ever expanded: another variable,
    a relative or computed value (`$(..)`, backticks, `~`, a glob, `${V:-x}`, spaces), a `$` inside single quotes or escaped (the whole command is then left as it is), an assignment that may not have run (the name is withdrawn, so a later use stays `$`), `NAME+=`, `${NAME:=..}`, an
    environment prefix of the same command (the shell expands that command's words with the OLD value) and any later builtin that can change variables without showing it (`export`, `read`, `eval`, `source`, a loop, a function; the table is emptied there). Those stay as they are (`$` in the
    word: `unknown` when it names a skill script)."""
    if not _dollar_safe(cmd): return items
    table, out, depth, straight = {}, [], 0, True
    for it in items:
        if it[0] == "push": depth += 1; out.append(it); continue
        if it[0] == "pop": depth = max(0, depth - 1); out.append(it); continue
        _, before, after, words, io = it
        pure = bool(words) and not io["outs"] and not io["ins"] and all(_ASSIGN.match(w) for w in words)
        if pure:
            ran = depth == 0 and after in (None, ";", "&&") and (before in (None, ";") or (before == "&&" and straight)); ws = []
            for w in words:
                name, val = w.split("=", 1); e = _expand(val, table) if ran else None
                if e is None or not e.startswith("/") or _UNSAFE_VALUE.search(e): table.pop(name, None); ws.append(w)
                else: table[name] = e; ws.append(name + "=" + e)
            out.append((it[0], before, after, ws, io)); continue
        straight = False
        ws = []
        for w in words:
            e = _expand(w, table); ws.append(w if e is None else e)
            m = re.match(r"(\w+)\+=", w)
            if m: table.pop(m.group(1), None)
            for n in [n for n in table if "${" + n + ":" in w or "${" + n + "=" in w]: table.pop(n, None)
        head = _split_cmd(words)
        if head is not None and (os.path.basename(head[1]) in _MUTATORS or head[1].endswith("()")): table.clear()
        out.append((it[0], before, after, ws, io))
    return out

_SUBST_VAR = "$-"      # stands for the unknown output of a command substitution: not a variable name, so no command can assign it, `_VAR` never matches it and a path built from it holds a `$` and cannot be resolved

def _has_heredoc(text):
    """True when `text` holds a here-document operator (`<<` or `<<-`, not the here-string `<<<`) outside quotes and not escaped. Quote and escape handling is the same as in `_flatten_subst`; a `<<` inside a substitution counts (the safe side)."""
    i, q = 0, None
    while i < len(text):
        ch = text[i]
        if q == "'":
            if ch == "'": q = None
        elif ch == "\\": i += 2; continue
        elif ch == "'" and q is None: q = "'"
        elif ch == '"': q = None if q == '"' else '"'
        elif ch == "<" and q is None and text[i + 1:i + 2] == "<":
            if text[i + 2:i + 3] != "<": return True
            i += 3; continue
        i += 1
    return False

def _close_paren(text, i):
    """Index of the `)` that closes the `(` at `i`, respecting quotes, escapes and nested parentheses, or None."""
    depth, q = 0, None
    while i < len(text):
        ch = text[i]
        if q:
            if ch == "\\" and q == '"': i += 2; continue
            if ch == q: q = None
        elif ch == "\\": i += 2; continue
        elif ch in "'\"": q = ch
        elif ch == "(": depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0: return i
        i += 1
    return None

def _flatten_subst(text):
    """The text with every command substitution outside single quotes (`$( ... )`, `$(( ... ))`, backticks; also inside double quotes) replaced by `${__subst__}` -> (new text, number replaced), or None when one is not closed. The tokeniser
    cuts `python3 $(pwd)/scripts/audit.py` into `python3 $`, a group and `/scripts/audit.py`: no segment runs a skill script, and the old answer was `no`. With the substitution as one unresolvable word the path is `unknown`, as a shell variable is."""
    out, i, q, n = [], 0, None, 0
    while i < len(text):
        ch = text[i]
        if q == "'":
            if ch == "'": q = None
            out.append(ch); i += 1; continue
        if ch == "\\" and i + 1 < len(text): out.append(text[i:i + 2]); i += 2; continue
        if ch == "'" and q is None: q = "'"; out.append(ch); i += 1; continue
        if ch == '"': q = None if q == '"' else '"'; out.append(ch); i += 1; continue
        if ch == "$" and text[i + 1:i + 2] == "(":
            j = _close_paren(text, i + 1)
            if j is None: return None
            out.append(_SUBST_VAR); n += 1; i = j + 1; continue
        if ch == "`":
            j = i + 1
            while j < len(text) and text[j] != "`": j += 2 if text[j] == "\\" else 1
            if j >= len(text): return None
            out.append(_SUBST_VAR); n += 1; i = j + 1; continue
        out.append(ch); i += 1
    return "".join(out), n

def _bash_class(cmd, cwd):
    """`_bash_class_text` of the command, and, unless that is already `yes`, of the same command with its command substitutions replaced by one unresolvable word (`_flatten_subst`): the more severe answer wins (`yes`, then `unknown`, then `no`). A command with a real here-document (`_has_heredoc`) is left to the first
    answer (the text after it is data and is not rewritten); a quoted `<<` or a here-string is not one. An unclosed substitution that mentions the skill is `unknown`."""
    best = _bash_class_text(cmd, cwd)
    if best == "yes" or ("$(" not in cmd and "`" not in cmd) or _has_heredoc(cmd): return best
    flat = _flatten_subst(cmd)
    if flat is None: return "unknown" if _mentions_skill(cmd) else best
    if not flat[1]: return best
    return "unknown" if _bash_class_text(flat[0], cwd) in ("yes", "unknown") else best

def _bash_class_text(cmd, cwd):
    """The shell command is split into simple commands (shlex, no execution) and the operators between them. What a command RUNS (an interpreter on a script / module / import, a shell, `source`, the script as the
    command word) or READS (cat, head, grep, ...) decides: a skill path that is only named (echo, printf, git, cp, a redirect target) is not activity. `cd X` moves the following command only through `&&` (a `;` or a newline runs the next command
    also when the cd failed: the directory is then not demonstrated); behind `||`, `&` or `|`, or as a conditional command, the directory is not demonstrated (`unknown`) or unchanged, and a group `( ... )` ends with its parenthesis.
    Grep-like commands read the values of -f / --file and the operands after the pattern; a pattern given by an option (-e, --regexp) is never a file. A quoted or escaped operator character (`echo '|' x`) is an argument, not a
    separator. The activity classification does not depend on the exit statuses (`false && python3 report.py` stays `yes`: the conditions matter for the files a call demonstrably wrote or read, see `tool_io`). A command that cannot be tokenised and names
    a skill script, a relative script name without a recorded directory, or an unrecognised command naming a skill script is `unknown`; anything else is `no`."""
    try: items = _shell_items(cmd)
    except ValueError: return "unknown" if _mentions_skill(cmd) else "no"
    items = _expand_vars(items, cmd); best = "no"
    for words, here, _, _, _, _ in _walk(items, cwd):
        c = _seg_class(words, here)
        if c == "yes": return "yes"
        if c == "unknown": best = "unknown"
    return best

def skill_activity(b, cwd=None):
    """Is the tool_use block `b` verification activity of THIS skill? -> "yes" | "no" | "unknown" (never a guess). yes: the Skill tool with the skill exactly `handoff-verify` or `<plugin>:handoff-verify`; a path field that resolves
    (against the record's cwd) to a script of the skill, one of its five reference files or its SKILL.md by path components; a shell command that runs or reads them (see `_bash_class`). A Jev call is not skill activity (it is excluded on its own, see
    `is_jev`); a mere mention (Write content, a pattern, `toolkit.py`, `echo <path>`) is `no`; a skill-script name that cannot be resolved (relative path or bare module without a recorded cwd, an untokenisable command) is `unknown`."""
    name = str((b or {}).get("name") or ""); inp = b.get("input") if isinstance(b.get("input"), dict) else {}
    if is_jev(b): return "no"
    if name == "Skill":
        s = inp.get("skill"); return "yes" if isinstance(s, str) and (s == "handoff-verify" or (s.count(":") == 1 and s.endswith(":handoff-verify") and not s.startswith(":"))) else "no"
    if name == "Bash": return _bash_class(inp.get("command"), cwd) if isinstance(inp.get("command"), str) else "no"
    out = "no"
    for k in _PATH_FIELDS:
        if isinstance(inp.get(k), str):
            c = _path_class(inp[k], cwd)
            if c == "yes": return "yes"
            if c == "unknown": out = "unknown"
    return out

def _genuine_prompt(d):
    """A real user prompt (not a tool_result, harness notice, meta record or compaction summary): the end of a verification run."""
    import scope
    if d.get("type") != "user" or d.get("isMeta") or d.get("isCompactSummary"): return False
    c = (d.get("message") if isinstance(d.get("message"), dict) else {}).get("content")
    if isinstance(c, str): return scope._prompt_text(c) is not None
    return any(isinstance(b, dict) and b.get("type") == "text" and scope._prompt_text(b.get("text")) is not None for b in c) if isinstance(c, list) else False

def mutation_target(b, cwd):
    """Canonical path (resolved in the cwd RECORDED for the call, never the process cwd) a Write/Edit/MultiEdit/NotebookEdit tool_use mutates, else None."""
    import discover as D
    if not isinstance(b, dict) or b.get("name") not in WRITERS or not isinstance(b.get("input"), dict): return None
    inp = b["input"]; return D.real_of(inp.get("file_path") or inp.get("notebook_path"), cwd)

def _literal(w):
    """Is the shell word a literal path (no variable, command substitution, `~` or glob)? Nothing dynamic is guessed."""
    return isinstance(w, str) and bool(w) and not w.startswith("~") and not any(ch in w for ch in "$`*?[{\x00")

def _reads_of(words, here):
    """The canonical paths ONE simple command READS (resolved in the directory `here` it runs in, also for operands without a slash): the operands of cat / head / tail / ..., the files of grep / sed / awk / rg (the pattern, a script
    given by -e is not one: `_file_operands`), a shell or `source` script. echo, printf and the like name a path without reading it."""
    import discover as D
    head = _split_cmd(words)
    if head is None: return set()
    env, cmd, args = head; b = os.path.basename(cmd)
    if b in _PATTERN_FIRST: files = _file_operands(b, args)
    elif b in _READERS: files = [a for a in args if not a.startswith("-")]
    elif cmd in ("source", ".") or (b in _SHELLS and not any(a.startswith("-") and "c" in a[1:] for a in args)): files = [a for a in args if not a.startswith("-")][:1]
    else: files = []
    return {D.real_of(f, here) for f in files if _literal(f)} - {None}

def tool_io(b, cwd):
    """What a tool_use does with files, by operation: -> dict(reads, reads_always, outs, trunc) of canonical paths (resolved in the cwd recorded for the call, never the process cwd). An ordinary tool names its path fields
    (`reads`; a mutation is `mutation_target`). A shell command (`_walk`, shlex, nothing is executed) READS what its readers read (`_reads_of`) and what a `<` redirect names, and WRITES (`outs`; `trunc` = the ones a `>`
    truncates) the LITERAL target of a `>` / `>>` redirect: a dynamic target is never guessed, a `/dev/` file is no artifact, a quoted `'>'` is an argument. Only a command that DEMONSTRABLY ran counts (`_plan`): `reads` /
    `outs` / `trunc` hold what ran unconditionally or ran whenever the whole call succeeded (the last `&&` chain); `reads_always` only what ran whatever the exit statuses (the caller that knows that the call failed
    uses it). What a status may have skipped (`false && cat f; ...`, behind `||`, in the background) is in none of them, and a redirect in the later part of a pipeline is no artifact. `bash -c '...'` is followed under the
    class of the command around it. Identity only: the call is not a supported Write and gives no version."""
    import discover as D
    if not isinstance(b, dict) or not isinstance(b.get("input"), dict): return dict(reads=frozenset(), reads_always=frozenset(), outs=frozenset(), trunc=frozenset())
    inp = b["input"]; reads, always, outs, trunc = set(), set(), set(), set()
    if b.get("name") == "Bash":
        def go(cmd, base, depth, cls, rest_ok, piped):
            try: items = _shell_items(cmd)
            except ValueError: return
            for words, here, before, after, io, (c, tail) in _walk(items, base, cls, rest_ok):
                if c is None: continue
                got = _reads_of(words, here) | {D.real_of(t, here) for t in io["ins"] if _literal(t)}; reads.update(got)
                if c == "always": always.update(got)
                if before != "|" and not piped:
                    for t, app in io["outs"]:
                        r = D.real_of(t, here) if _literal(t) else None
                        if r and not r.startswith("/dev/"): outs.add(r); (None if app else trunc.add(r))
                head = _split_cmd(words)
                if head and depth < 3 and os.path.basename(head[1]) in _SHELLS:
                    for k, a in enumerate(head[2]):
                        if a.startswith("-") and not a.startswith("--") and "c" in a[1:] and k + 1 < len(head[2]): go(head[2][k + 1], here, depth + 1, c, tail, piped or before == "|"); break
        if isinstance(inp.get("command"), str): go(inp["command"], cwd, 0, "always", True, False)
    else: reads |= {D.real_of(inp.get(k), cwd) for k in _PATH_FIELDS}; always |= reads
    return dict(reads=frozenset(reads - {None}), reads_always=frozenset(always - {None}), outs=frozenset(outs), trunc=frozenset(trunc))

def named_paths(b, cwd):
    """The canonical paths the tool_use `b` READS (`tool_io`): the path fields of an ordinary tool, the files its shell readers open. A path that is only named (echo, printf, a pattern) is not in it."""
    return tool_io(b, cwd)["reads"]

def _events(records):
    """The flat event list of a transcript in order: dict(i, j, kind prompt|text|use|result, pos (tool events: the jevref.timeline position), id, cls (uses: yes|no|unknown|jev), mut (uses: canonical path a writer
    mutates), name / refs / refs_always / outs / trunc (uses: the tool, the canonical paths it demonstrably reads (refs: when the call succeeded; refs_always: whatever the statuses), the literal redirect targets it demonstrably writes and truncates: `tool_io`), err (results: is_error), role, meta)."""
    import scope
    out, n = [], 0
    for i, d in enumerate(records):
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}; c = msg.get("content"); t = d.get("type")
        if t not in ("user", "assistant"): continue
        if isinstance(c, str):
            out.append(dict(i=i, j=0, kind="prompt" if _genuine_prompt(d) else "text", role=t, meta=bool(d.get("isMeta"))))
            continue
        genuine = _genuine_prompt(d); placed = False
        for j, b in enumerate(c if isinstance(c, list) else []):
            if not isinstance(b, dict): continue
            ty = b.get("type")
            if ty == "text" and isinstance(b.get("text"), str):
                out.append(dict(i=i, j=j, kind="prompt" if genuine and t == "user" and scope._prompt_text(b["text"]) is not None else "text", role=t, meta=bool(d.get("isMeta"))))
            elif ty == "tool_use":
                n += 1; io = tool_io(b, d.get("cwd")); out.append(dict(i=i, j=j, kind="use", pos=n, id=b.get("id"), cls="jev" if is_jev(b) else skill_activity(b, d.get("cwd")), mut=mutation_target(b, d.get("cwd")), name=b.get("name"), refs=io["reads"], refs_always=io["reads_always"], outs=io["outs"], trunc=io["trunc"], role=t))
            elif ty == "tool_result":
                n += 1; out.append(dict(i=i, j=j, kind="result", pos=n, id=b.get("tool_use_id"), err=bool(b.get("is_error")), role=t))
    return out

def _generated(ev):
    """The (record, block) pairs that are verification material in every window: the uses of the skill and of Jev with their results, the harness-loaded skill text right after a skill call, and the assistant narration
    that sits between or after the events of a verification chain that a SKILL event opened (a bare Jev call opens none) without any other tool call in between (up to the next prompt)."""
    vids = {e["id"] for e in ev if e["kind"] == "use" and e["cls"] in ("yes", "jev")}; yids = {e["id"] for e in ev if e["kind"] == "use" and e["cls"] == "yes"}; out = set(); nxt = {}
    ahead = None                                   # None = no tool_use before the next prompt/end; True/False = is the next tool_use verification
    for k in range(len(ev) - 1, -1, -1):
        e = ev[k]
        if e["kind"] == "prompt": ahead = None
        elif e["kind"] == "use": ahead = e["id"] in vids
        nxt[k] = ahead
    chain = False                                  # is the previous tool event part of a chain that a skill event opened
    for k, e in enumerate(ev):
        if e["kind"] == "prompt": chain = False
        elif e["kind"] in ("use", "result"):
            if e["id"] in vids:
                out.add((e["i"], e["j"]))
                if e["id"] in yids: chain = True
            else: chain = False
        elif e["kind"] == "text" and chain:
            if e["role"] == "user" and e.get("meta"): out.add((e["i"], e["j"]))                               # the skill body the harness loads after the Skill call
            elif e["role"] == "assistant" and nxt[k] in (None, True): out.add((e["i"], e["j"]))
    return out

def _reread(ev, seed):
    """The artifacts of a verification and what reads them again. An artifact = a canonical path that a Write/Edit INSIDE `seed` (the generated material and the spans of the verification runs) successfully
    mutated, or that a successful shell command inside `seed` wrote through a LITERAL `>` / `>>` redirect that demonstrably ran (`tool_io`: not behind `||` / `&`, not followed by `;`, not a quoted `'>'`, `bash -c` under the
    condition around it; a dynamic target is never guessed; the success of the call proves only the last `&&` chain, never a command a status may have skipped); a full Write (or such a `>` redirect) of the same path outside
    `seed` ends it (that content is real work). A later tool call, outside `seed`, that READS an artifact (Read, Grep, a shell `cat` / `grep FILE` ... that ran, canonicalised in the cwd recorded for the call: a read a status may have skipped counts only when the call succeeded and it
    ran then; an echo, a printf or a pattern that only names it is no reading) is a re-read: its use and its result are generated material too, wherever they are (after a prompt, in another window). Path identity only: nothing is excluded by adjacency and a bare Jev call
    writes no artifact. -> {(record, block)}."""
    err = {e["id"]: e["err"] for e in ev if e["kind"] == "result"}; art, ids = set(), set()
    for e in ev:
        if e["kind"] != "use": continue
        inside = (e["i"], e["j"]) in seed; ok = err.get(e["id"], True) is False
        if not inside and art & (e["refs"] if ok else e["refs_always"]) and not (e.get("mut") is not None and ok): ids.add(e["id"])      # a successful write of the path is not a reading of it
        if e.get("mut") is not None and ok:
            if inside: art.add(e["mut"])
            elif e.get("name") == "Write": art.discard(e["mut"])
        elif ok and e.get("outs"):
            if inside: art |= e["outs"]
            else: art -= e["trunc"]
    return {(e["i"], e["j"]) for e in ev if e["kind"] in ("use", "result") and e["id"] in ids}

def _generated_all(ev, first=0):
    """`_generated` plus the re-reads of the artifacts that the verification material and the verification runs wrote (`_reread`). The runs that count are those that start after `first` (the position of the first successful
    mutation of the evaluated note, 0 = every run when no note is evaluated): a run before the note existed verifies nothing of it, what that stretch of the session wrote is real work."""
    g = _generated(ev); seed = set(g)
    for r in _runs(ev, first): seed |= _span(ev, r)
    return g | _reread(ev, seed)

def generated_events(records):
    """The verification material of a transcript regardless of any write: -> {(record index, block index)} (the generated material and the re-reads of the artifacts a verification wrote)."""
    return _generated_all(_events(records))

def _own(ev, note):
    """(uses, results) of the mutations of the note `note` (canonical path) as {(record, block)} sets, and the position of the first successful result."""
    if note is None: return set(), set(), 0
    ids = {e["id"] for e in ev if e["kind"] == "use" and e.get("mut") == note}
    uses = {(e["i"], e["j"]) for e in ev if e["kind"] == "use" and e["id"] in ids}; res = [e for e in ev if e["kind"] == "result" and e["id"] in ids]
    ok = [e["pos"] for e in res if not e["err"]]
    return uses, {(e["i"], e["j"]) for e in res}, (min(ok) if ok else None)

def _runs(ev, after):
    """The verification runs of a transcript: dict(id (tool_use id of the first skill event), pos, start, end (event indexes)). A run starts at a skill event (`cls` yes, never a bare Jev call) whose position is
    after `after` and lasts until the next genuine user prompt; the next skill event after such a prompt starts the next run."""
    runs, opened = [], False
    for k, e in enumerate(ev):
        if e["kind"] == "prompt":
            if opened: runs[-1]["end"] = k; opened = False
        elif e["kind"] == "use" and e["cls"] == "yes" and e["pos"] > after and not opened: runs.append(dict(id=e["id"], pos=e["pos"], start=k, end=len(ev))); opened = True
    return runs

def _span(ev, run): return {(e["i"], e["j"]) for e in ev[run["start"]:run["end"]]}

def common_exclusions(records, note=None):
    """What no consumer takes for source of the note `note` (canonical path; None = no note): the verification material (`_generated`) and the re-reads of the artifacts it wrote (`_reread`), the use AND the result of every mutation of the note, and the whole span of every
    verification run that starts after the note's first successful mutation (a run verifies a version of the note: its reads, results and summary are generated material up to the next prompt). The chunks of
    prepare.py use exactly this set; `source_window` adds the evaluated window to it."""
    ev = _events(records); uses, res, first = _own(ev, note); out = set(_generated_all(ev, first if note is not None and first is not None else 0)) | uses | res
    if note is not None and first is not None:
        for r in _runs(ev, first): out |= _span(ev, r)
    return out

def source_window(records, write_id=None, evaluated_against="session_end", run=None):
    """The ONE window of the eligible source (prepare, omissions.context, scope, the report re-derivation and the chunks of prepare.py all use it). -> dict(limit, excluded, ambiguous, kind, runs [dict(id, pos)], run).
    `kind` says why `ambiguous` is set: "run" = the evaluated run is not demonstrated (several candidates without a name, a name that is no candidate, a run named for a prefix, an unresolvable event that could start another run):
    the EVALUATION is then invalid for every consumer (report, gate, omission pair); "events" = an unresolvable event before the limit, "write" = the write is not a recorded writer: only the source is unavailable.
    Common to all: `excluded` = `common_exclusions` of the evaluated note (the note is the path the write `write_id` mutates, resolved in the cwd recorded for the call) = verification material, the note's own mutations and
    the spans of the runs that started after its first successful mutation and before the limit. A RUN is a stretch of skill activity (`skill_activity` yes, never a bare Jev call), it starts at the first such event and lasts
    until the next genuine user prompt. `prefix`: `limit` = the position of the write's tool_use (no run is involved). `session_end`: the candidate runs are those that start after the evaluated write's result and before the
    next successful mutation of the note (every run of the transcript when no write is given); the evaluated run is the one NAMED by `run` (a tool_use id listed in `runs`) or the only candidate; zero candidates = no run
    (`limit` None: the whole transcript); several candidates without a name, or a name that is not a candidate, are `ambiguous` (the evaluated run is not demonstrated: UNRESOLVED, never the last one). `limit` = the
    position of the start of the evaluated run. `ambiguous` is also set when an event that names a script of the skill cannot be resolved (`skill_activity` unknown) before the limit, or, for an unnamed
    selection, anywhere in the candidate region outside the selected run (it could start another run): no source is eligible then."""
    import jevref as J
    ev = _events(records); use_pos, res_pos = J.timeline(records); excluded = set(_generated_all(ev))
    def amb(why, runs=(), kind="run"): return dict(limit=None, excluded=excluded, ambiguous="source window ambiguous: " + why, kind=kind, runs=list(runs), run=None)
    note = pw = None
    if write_id:
        wev = next((e for e in ev if e["kind"] == "use" and e["id"] == write_id), None)
        if wev is None or wev.get("mut") is None: return amb("the write %r is not a Write/Edit recorded in the source transcript" % (id_text(write_id),), kind="write")
        note, pw = wev["mut"], wev["pos"]
    uses, res, first = _own(ev, note); excluded = set(_generated_all(ev, first if note is not None and first is not None else 0)) | uses | res
    allruns = _runs(ev, first if note is not None and first is not None else 0)
    def unknown(lo=None, hi=None): return [e for e in ev if e["kind"] == "use" and e["cls"] == "unknown" and (lo is None or e["pos"] > lo) and (hi is None or e["pos"] < hi)]
    def fail(bad): return "an event names a script of this skill that cannot be resolved (no recorded cwd to resolve it against), so the verification run it belongs to is not demonstrated (position %d)" % bad[0]["pos"]
    if evaluated_against == "prefix" and write_id:
        if run is not None: return amb("a verification run only bounds session_end; the prefix of a write is bounded by the write itself")
        limit = pw
        for r in allruns:
            if r["pos"] < limit: excluded |= _span(ev, r)
        bad = unknown(hi=limit)
        return dict(limit=limit, excluded=excluded, ambiguous=("source window ambiguous: " + fail(bad)) if bad else None, kind="events" if bad else None, runs=[], run=None)
    rw = (res_pos.get(write_id) or pw or 0) if write_id else 0
    done = {e["id"] for e in ev if e["kind"] == "result" and not e["err"]}      # only a SUCCESSFUL mutation changes the note: a failed Write does not end the region of the candidate runs
    nxt = min([e["pos"] for e in ev if e["kind"] == "use" and note is not None and e.get("mut") == note and e["pos"] > pw and e["id"] in done], default=None) if write_id else None
    cand = [r for r in allruns if r["pos"] > rw and (nxt is None or r["pos"] < nxt)]; listed = [dict(id=r["id"], pos=r["pos"]) for r in cand]; names = ", ".join(id_text(r["id"]) for r in cand)   # the prose never holds a refused id (the structured `runs` keep the real ids)
    if run is not None:
        sel = next((r for r in cand if r["id"] == run), None)
        if sel is None: return amb("%r is not a verification run that starts after this write%s" % (id_text(run), " (candidates: %s)" % names if cand else " (the transcript has none)"), listed)
    elif len(cand) > 1: return amb("%d verification runs start after this write (%s): the evaluated run is not demonstrated; name it (`--run ID`, version_ref.run) instead of taking the last one" % (len(cand), names), listed)
    else: sel = cand[0] if cand else None
    limit = sel["pos"] if sel else None
    for r in allruns:
        if sel is None or r["pos"] < sel["pos"]: excluded |= _span(ev, r)
    bad, kind = unknown(hi=limit), "events"
    if not bad and run is None and sel is not None:      # an unresolvable event after the selected run could start another run: the unnamed selection is then not demonstrated
        bad = [e for k, e in enumerate(ev) if k >= sel["end"] and e["kind"] == "use" and e["cls"] == "unknown" and (nxt is None or e["pos"] < nxt)]; kind = "run"
    return dict(limit=limit, excluded=excluded, ambiguous=("source window ambiguous: " + fail(bad)) if bad else None, kind=kind if bad else None, runs=listed, run=sel["id"] if sel else None)

def verification_limit(records):
    """Position (jevref.timeline) of the START of the verification run of the transcript when it has exactly one (`source_window` without a write); None = no run (or several: use `source_window` and name one)."""
    return source_window(records)["limit"]

def eligible_blocks(records, handoff_real, pos=None, excluded=()):
    """Source text blocks eligible to evaluate a version of the note: user/assistant text, tool results and tool inputs of the transcript, EXCLUDING every Write/Edit of the handoff path itself AND its result (the note is
    not its own source; the path is resolved in the cwd recorded for the call), EVERY Jev call (claims and evidence are inputs of a verification, not facts established by the transcript) with its result, and the (record, block)
    pairs in `excluded` (the window of `source_window`: skill activity, verification narration, earlier runs). `pos` = position on the common timeline (jevref.timeline): only what comes strictly before it is eligible. -> [text]."""
    return [t for t, _ in eligible_blocks_indexed(records, handoff_real, pos, excluded)]

def eligible_blocks_indexed(records, handoff_real, pos=None, excluded=()):
    """`eligible_blocks` with the provenance of every block: -> [(text, dict(record, line, block, kind, uuid))]; `record` = index in the parsed records, `line` = line of the JSONL (when the loader recorded it), `block` = index of the
    content block in its record, `kind` = text | tool_use | tool_result. The texts and their order are exactly those of `eligible_blocks`."""
    out, n, skip = [], 0, set()
    for i, d in enumerate(records):
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}; c = msg.get("content"); t = d.get("type")
        if t not in ("user", "assistant"): continue
        def prov(j, kind): return dict(record=i, line=d.get("_line"), block=j, kind=kind, uuid=d.get("uuid"))
        if isinstance(c, str):
            if (pos is None or n < pos) and (i, 0) not in excluded: out.append((c, prov(0, "text")))
            continue
        for j, b in enumerate(c if isinstance(c, list) else []):
            if not isinstance(b, dict): continue
            ty = b.get("type"); gone = (i, j) in excluded
            if ty == "text":
                if isinstance(b.get("text"), str) and (pos is None or n < pos) and not gone: out.append((b["text"], prov(j, "text")))
            elif ty == "tool_use":
                n += 1
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                if is_jev(b): skip.add(b.get("id")); continue
                if mutation_target(b, d.get("cwd")) == handoff_real and handoff_real is not None: skip.add(b.get("id")); continue
                if gone: continue
                if pos is None or n < pos: out.append(("[tool_use %s] %s" % (b.get("name"), json.dumps(inp, ensure_ascii=False)), prov(j, "tool_use")))
            elif ty == "tool_result":
                n += 1
                if b.get("tool_use_id") in skip or gone: continue
                if pos is None or n < pos:
                    x = b.get("content"); out.append((x if isinstance(x, str) else "".join(y.get("text", "") for y in x if isinstance(y, dict)) if isinstance(x, list) else "", prov(j, "tool_result")))
    return out

def passage_of(blocks, quote):
    """The first eligible block that contains the quote EXACTLY (case-sensitive, internal whitespace and newlines as in the record), else None."""
    return next((b for b in blocks if isinstance(quote, str) and quote.strip() and quote in b), None)

def evidence_raw(call_input):
    """The evidence texts the REAL call was given, in order, unmodified (verify/gate: a string, one item or a list of items)."""
    ev = (call_input or {}).get("evidence")
    if isinstance(ev, str): return [ev]
    if isinstance(ev, dict): return [ev["text"]] if isinstance(ev.get("text"), str) else []
    return [x["text"] if isinstance(x, dict) else x for x in ev if isinstance(x, (dict, str)) and (not isinstance(x, dict) or isinstance(x.get("text"), str))] if isinstance(ev, list) else []

def material_matches(raw_texts, mat):
    """The evidence of the absence call IS the canonical material: exactly the same text, order, delimiters and internal whitespace/newlines (contiguous chunks that concatenate to it are accepted); extra or missing content is not."""
    return mat is not None and bool(raw_texts) and "".join(raw_texts) == mat

def passage_matches(raw_texts, passage):
    """The WHOLE evidence of the source call IS the eligible passage that holds the quote (exactly; contiguous chunks that concatenate to it are accepted): no extra claims, no other text."""
    return passage is not None and bool(raw_texts) and "".join(raw_texts) == passage

TRANSPORT_ERRORS = ("transport", "invalid_response")   # the only errors that allow one identical retry (report.TRANSPORT_ERRORS)

def plan_absence(outcome=None):
    """Scheduling after a successful `prepare`: ABSENCE first. -> dict(disposition, source, finding, reason). A scheduling disposition, never a status; nothing is cleared or confirmed here.
    `outcome` = the recorded result of the single-claim ABSENCE call, as plain facts: bound, error, error_kind, attempts_identical, verdict, confidence, action, aux_ok (the strict auxiliary filter), version_ok (the
    version_ref validated by versions.py), material_complete. None = the call has not been made yet. Dispositions: `call_absence` (make the call, complete material, nothing else); `retry_identical` (a transport /
    invalid_response error, once); `present_resolved` (a resolved `verified`/`contradicted`: the note states or contradicts the detail, no SOURCE call, no finding); `request_source` (a bound, error-free
    `unsupported` with a finite confidence > 0.95 and an explicit `auto`, on a demonstrated version and complete material: the SOURCE call is now needed, and still confirms nothing alone); `unresolved` (anything
    else, including a `verified`/`contradicted` that is not resolved: the obligation stays open, never cleared, no SOURCE call)."""
    import jevref as J
    def plan(d, source=False, why=""): return dict(disposition=d, source=source, finding=False, reason=why)
    if outcome is None: return plan("call_absence", why="make the single-claim ABSENCE call with the complete material first")
    o = outcome if isinstance(outcome, dict) else {}
    if o.get("error"):
        if o.get("error_kind") in TRANSPORT_ERRORS and not o.get("attempts_identical"): return plan("retry_identical", why="transport/invalid_response error: one identical retry")
        return plan("unresolved", why="the ABSENCE call has no usable result")
    if o.get("bound") is not True: return plan("unresolved", why="the ABSENCE result is not bound to a real call")
    if o.get("version_ok") is not True: return plan("unresolved", why="the version identity is not demonstrated")
    if o.get("material_complete") is not True: return plan("unresolved", why="the complete material is not demonstrated")
    verdict, good = str(o.get("verdict")).lower(), J.strict_pass(o.get("confidence"))
    if verdict in ("verified", "contradicted"):
        if good and o.get("aux_ok") is True: return plan("present_resolved", why="the note states or contradicts the detail: no SOURCE call, no finding")
        return plan("unresolved", why="%s without a resolved confidence/auxiliary result: not cleared" % verdict)
    if verdict == "unsupported" and good and J._finite(o.get("confidence")) and o.get("action") == "auto": return plan("request_source", True, "unsupported > 0.95 with an explicit auto: the SOURCE call is needed")
    return plan("unresolved", why="the ABSENCE result is not an unsupported > 0.95 with an explicit auto")

def plan_source(items):
    """The SOURCE calls the candidates need after their ABSENCE calls. items: [dict(absence=<outcome of plan_absence>, source_claim, source_passage, source, file, version_ref={write_tool_use_id, sha256, evaluated_against})].
    -> dict(dispositions=[dict(disposition, group, claim_index)], groups=[dict(source, file, write_tool_use_id, sha256, evaluated_against, passage, claims, items)], planned_source_calls).
    Only a candidate whose `plan_absence` is `request_source` gets a SOURCE call (`no_source` after a resolved present/contradicted, `unresolved` otherwise). Candidates share one call only when the source session, the note,
    the write, its sha256, `evaluated_against`, the evaluated run (`version_ref.run`) and the eligible passage are all identical (one write has one chronology window); a missing identity never merges. Each claim keeps its exact canonical text and its own
    result index (`claim_index`), so every result is bound on its own (the pair validator is unchanged); the same claim twice is one claim."""
    groups, index, disp = [], {}, []
    for k, it in enumerate(items):
        it = it if isinstance(it, dict) else {}
        p = plan_absence(it.get("absence"))
        if p["disposition"] != "request_source": disp.append(dict(disposition="no_source" if p["disposition"] == "present_resolved" else "unresolved", group=None, claim_index=None)); continue
        ref = it.get("version_ref") if isinstance(it.get("version_ref"), dict) else {}
        ident = (it.get("source"), it.get("file"), ref.get("write_tool_use_id"), ref.get("sha256"), ref.get("evaluated_against"), it.get("source_passage"))
        claim = it.get("source_claim")
        if not isinstance(claim, str) or not claim: disp.append(dict(disposition="unresolved", group=None, claim_index=None)); continue
        key = (ident, ref.get("run")) if all(isinstance(x, str) and x for x in ident) else ("ungrouped", k)
        if key not in index:
            index[key] = len(groups)
            groups.append(dict(source=ident[0], file=ident[1], write_tool_use_id=ident[2], sha256=ident[3], evaluated_against=ident[4], run=ref.get("run"), passage=ident[5], claims=[], items=[]))
        g = groups[index[key]]
        if claim not in g["claims"]: g["claims"].append(claim)
        g["items"].append(k); disp.append(dict(disposition="request_source", group=index[key], claim_index=g["claims"].index(claim)))
    return dict(dispositions=disp, groups=groups, planned_source_calls=len(groups))

_MEMO = {}
_SRC = {}       # (transcript content identity, write id, evaluated_against, run, note path) -> the sanitized eligible source with its provenance (the source index of ONE evaluation)
_PREP = {}      # (content identities of the session and its subagent streams, note path, content identity of the note on disk) -> the reconstruction of the versions
STATS = collections.Counter()   # counts only: how many reconstructions / source indexes were built and how many were reused in this process (a run)

def reset_caches():
    """Forget everything reused within a run (the next candidate prepares from scratch)."""
    import refs
    import scope
    import versions
    _MEMO.clear(); _SRC.clear(); _PREP.clear(); _CLEAN.clear(); _INFO.clear(); _MAT.clear(); STATS.clear(); refs.reset_cache(); scope._MEMO.clear(); versions._STREAMS.clear()

def window_for(source_jsonl, write_id, evaluated_against, run=None):
    """-> (`source_window` of the transcript for ONE evaluation | None when the transcript cannot be read, the records). Cached by the CONTENT identity of the transcript (sha256 of its bytes, not size and mtime) and the window; the one derivation behind `context`, the report and the gate."""
    import discover as D
    try: key = D.content_fingerprint(source_jsonl)
    except OSError: return None, None
    if key not in _MEMO: _MEMO[key] = D.load_jsonl(source_jsonl)
    recs = _MEMO[key]; wkey = (key, write_id, evaluated_against, run)
    if wkey not in _MEMO: _MEMO[wkey] = source_window(recs, write_id, evaluated_against, run)
    return _MEMO[wkey], recs

def context(source_jsonl, handoff_real, version, evaluated_against, bases=(), run=None):
    """Everything the validator needs about one version: the eligible source text and the canonical material. -> dict(eligible_source, eligible_blocks, eligible_prov [provenance of every block], material, material_reason, manifest,
    missing_reference, note_text, references [(ref, text)], runs, run). The source is the shared window (`source_window`) of the transcript for THIS write, `evaluated_against` and the evaluated run `run` (version_ref.run; none = the only run after the write); an ambiguous
    window gives no source, and so does a version of a path written in several streams (`mixed`). The source index (window + sanitized blocks) is reused by content identity within a run; the references are re-resolved and re-read every time,
    so a changed reference or resolution always shows (the canonical material itself is reused only while the ordered references and their content are exactly the same, see `_build_material`). The result is the caller's own copy."""
    import discover as D, jevref as J
    def none(why, runs=()): return dict(eligible_source=None, eligible_blocks=None, eligible_prov=None, material=None, material_reason=why, runs=[r["id"] for r in runs], run=None)
    import versions as V
    if version.get("mixed"): return none(V.mixed_reason(version))      # the windows of a path written in several streams depend on an order nobody demonstrates, also retrospectively (the bytes of the full Write stay checkable)
    w, recs = window_for(source_jsonl, version["write_tool_use_id"], evaluated_against, run)
    if w is None: return none("source transcript unreadable")
    if w["ambiguous"]: return none(w["ambiguous"], w["runs"])
    key = (D.content_fingerprint(source_jsonl), version["write_tool_use_id"], evaluated_against, run, handoff_real)
    if key not in _SRC:
        STATS["source_index_builds"] += 1
        ib = eligible_blocks_indexed(recs, handoff_real, w["limit"], w["excluded"])   # the shared sanitization policy: what Jev gets (and what the validator re-derives) is never the raw text
        _SRC[key] = (tuple(clean_text(t) for t, _ in ib), tuple(tuple(sorted(p.items())) for _, p in ib), [r["id"] for r in w["runs"]], w["run"])
    else: STATS["source_index_hits"] += 1
    blocks, prov, runs, run_id = _SRC[key]
    mat, manifest, why, missing, found = _build_material(version["content"], bases)
    return dict(eligible_source=BLOCK_SEP.join(blocks), eligible_blocks=list(blocks), eligible_prov=[dict(p) for p in prov], material=mat, material_reason=why, manifest=manifest, missing_reference=missing,
                note_text=clean_text(version["content"]), references=[(r, clean_text(t)) for r, t in found], runs=list(runs), run=run_id)   # a raw (secret-bearing) text never travels in the context

def _file_sha(path):
    try: return hashlib.sha256(open(path, "rb").read()).hexdigest()
    except OSError: return None

def _session_prep(sp, path):
    """The reconstruction behind one candidate -> (provenance, versions, canonical path, note, relocated-from | None, relocation note). Reused within a run by the CONTENT identity of the session and of its subagent streams
    and of the note on disk (relocation reads it); every call gets its own copy."""
    import copy, discover as D, versions as V
    key = (tuple(D.content_fingerprint(f) for f in [sp] + D.subagent_files(sp)), os.path.realpath(path), _file_sha(path))
    if key in _PREP: STATS["session_prep_hits"] += 1
    else:
        STATS["session_prep_builds"] += 1
        prov = V.provenance(sp, path)   # preflight, before anything version-dependent: a note without a recorded supported write has no write identity or window
        vs = canon = note = reloc = rnote = None
        if prov["state"] == "recorded_write":
            vs, canon, _, note = V.versions_of(sp, path)
            if canon is None:
                reloc, rnote = V.relocation_candidate(sp, path)
                if reloc: vs, canon, _, note = V.versions_of(sp, path, reloc)
        _PREP[key] = (prov, vs, canon, note, reloc, rnote)
    return copy.deepcopy(_PREP[key])

def prepare_one(a, extra=None):
    """The prepare result for one candidate -> (object, exit code); `cmd_prepare` prints it, `cmd_prepare_batch` collects it. The transcript is hashed once per candidate (discover.fingerprint_scope). `extra` (a dict) receives what the
    obligation ledger needs and `prepare` does not print: source (canonical), note_real, evaluated_sha256 (the sanitized eligible source of the evaluation)."""
    import discover as D
    with D.fingerprint_scope(): return _prepare_one(a, {} if extra is None else extra)

def _prepare_one(a, extra):
    import discover as D, versions as V
    def fail(*reasons, **extra):   # a diagnostic never echoes a credential
        if isinstance(extra.get("work_locations"), list): extra["work_locations"] = safe_list(extra["work_locations"])
        if isinstance(extra.get("runs"), list): extra["runs"] = safe_list(extra["runs"], S.ID_ROLE)
        mr = extra.get("missing_reference")
        if isinstance(mr, dict): extra["missing_reference"] = dict(mr, **{k: (S.sanitize(mr[k])[0] if isinstance(mr[k], str) else safe_list(mr[k]) if isinstance(mr[k], list) else mr[k]) for k in ("ref", "reason", "searched") if k in mr})
        return dict(ok=False, reasons=[S.sanitize(r)[0] for r in reasons], **extra), 3
    locs, why = check_locations(getattr(a, "location", None))
    if why: return fail(why)
    if identity_dependent(a.write_id, getattr(a, "run", None), role=S.ID_ROLE): return fail("the write id or the run id holds a secret or a redaction marker: an identity that needs redaction is not used (UNRESOLVED; the value is not echoed)")
    sp, how, amb = D.resolve_session_info(a.source, a.cwd)
    if amb: return fail("source session not demonstrated: several recently modified sessions; pass --source ID or PATH.jsonl")
    if not sp or not D.source_exists(sp): return fail("source session not found")
    if identity_dependent(sp, D.canon(sp)): return fail("the source session path holds a secret or a redaction marker: an identity that needs redaction is not used (UNRESOLVED; the value is not echoed)")
    extra["source"] = D.canon(sp)
    path = a.file if os.path.isabs(a.file) else os.path.join(a.cwd or os.getcwd(), a.file)
    prov, vs, canon, note, reloc, rnote = _session_prep(sp, path)
    if prov["state"] != "recorded_write": return fail(prov["blocker"])
    if canon is None: return fail("%s; relocation: %s" % (note, rnote))
    if identity_dependent(path, canon, reloc): return fail("the handoff path holds a secret or a redaction marker: an identity that needs redaction is not used (UNRESOLVED; the value is not echoed)")
    extra["note_real"] = canon
    v = next((x for x in vs if x["write_tool_use_id"] == a.write_id), None)
    if v is None: return fail("--write-id is not a write of this handoff in the source session (copy it from `versions.py list`)")
    if identity_dependent(v["write_tool_use_id"], role=S.ID_ROLE): return fail("the recorded write id holds a secret or a redaction marker: an identity that needs redaction is not used (UNRESOLVED; the value is not echoed)")
    if v["status"] != "ok" or v["sha256"] is None: return fail("the version is not recoverable: %s" % v["reason"])
    if reloc:
        try: disk = hashlib.sha256(open(path, "rb").read()).hexdigest()
        except OSError: return fail("the copy cannot be read: relocation not verified")
        if disk not in {x["sha256"] for x in vs if x["sha256"]}: return fail("relocated copy: the bytes do not hash to a recoverable version of the written handoff")
    bases = material_bases(path, v, locs); work = R.with_git_roots(bases)
    ctx = context(sp, canon, v, a.evaluated_against, bases, getattr(a, "run", None))
    if identity_dependent(ctx.get("run"), role=S.ID_ROLE): return fail("the selected verification run id holds a secret or a redaction marker: an identity that needs redaction is not used (UNRESOLVED; the value is not echoed)", work_locations=work)
    if ctx["eligible_source"] is not None: extra["evaluated_sha256"] = sha256_text(ctx["eligible_source"])
    if ctx["material"] is None: return fail(ctx["material_reason"] or "material not available", work_locations=work, **({"runs": ctx["runs"]} if ctx.get("runs") else {}), **({"missing_reference": ctx["missing_reference"]} if ctx.get("missing_reference") else {}))
    if not wsnorm(a.detail): return fail("empty detail", work_locations=work)
    for what, val in (("detail", a.detail), ("source quote", a.source_quote)):
        if redaction_dependent(val): return fail("the %s holds a secret or a redaction marker: a redaction-dependent %s cannot be audited (UNRESOLVED; nothing is sent and the value is not echoed)" % (what, what), work_locations=work)
    if wsnorm(a.detail).startswith(SOURCE_PREFIX.strip()) or wsnorm(a.detail).startswith(ABSENCE_PREFIX.strip()): return fail("the detail must be the bare detail, not a canonical claim (the helper builds the claims itself)", work_locations=work)
    passage = passage_of(ctx["eligible_blocks"], a.source_quote)
    if passage is None: return fail("the source quote is not (exactly, case-sensitive, whitespace as in the record, inside ONE transcript record) in the eligible source of this version (%s; session_end stops before the verification activity, Jev calls are never source)" % a.evaluated_against, work_locations=work)
    if redaction_dependent(passage): return fail("the source passage that holds the quote contains redacted content: what it hides is not demonstrated (UNRESOLVED; nothing is sent)", work_locations=work)
    sc, ac = claims(a.detail, "R04")
    out = (dict(ok=True, contract="R04", work_locations=work, detail=wsnorm(a.detail), source_claim=sc, absence_claim=ac, source_passage=passage, material=ctx["material"], material_manifest=ctx.get("manifest", []),
                          version_ref=dict(write_tool_use_id=v["write_tool_use_id"], sha256=v["sha256"], evaluated_against=a.evaluated_against, **({"run": ctx["run"]} if a.evaluated_against == "session_end" and ctx.get("run") else {})), handoff_source_path=reloc,
                          omission_ref_template=dict(detail=wsnorm(a.detail), source_check_id="<id of the check whose call used source_claim and source_passage>"),
                          note="ABSENCE-first, in this order. (1) Call jev_verify with claims=[absence_claim] (the bare detail, the ONLY claim of that call) and evidence=material, verbatim and complete. If it comes back verified or contradicted the note states the detail (or contradicts it): record the check, report NO finding and make NO SOURCE call. (2) Only if (1) is `unsupported` with confidence > 0.95 and action explicitly `auto`, call jev_verify with claims=[source_claim] and evidence=source_passage (verbatim, the WHOLE passage, nothing added); several candidates that print the identical source_passage for the same write may share ONE such call, one source_claim each (omissions.plan_source), never inside a wrapper that hides the inner calls. Both checks carry the same version_ref. Report a finding (type lost_detail) only if (2) is verified > 0.95 with the auxiliary conditions AND (1) was unsupported > 0.95 with action auto: omission_ref, claim = absence_claim, check_id = the absence check, confidence = its confidence, quote_source = the exact quote. An unsupported result alone confirms nothing. Anything else (<= 0.95, review, error, another verdict, incomplete material) stays UNRESOLVED and needs no SOURCE call"), 0)
    import scope as SC
    out[0]["hints"] = build_hints(wsnorm(a.detail), passage, ctx, SC.scope_of(sp, a.write_id, a.evaluated_against, getattr(a, "run", None))[1], D.canon(sp))   # navigation only: nothing above depends on it
    return out

HINT_CAP = 5        # at most this many records per identifier are listed (the rest is counted in `truncated`)
HINT_OFFSETS = 3    # and this many offsets per record
HINT_IDENTIFIERS = 12
HINT_NOTE = "Occurrence hints, for NAVIGATION only: positions of the detail's identifiers in the eligible records, in the requests of the session and in the complete material. A literal match never clears, excludes or proves anything (a negated, quoted, superseded or stale assertion matches too, and so does a different case): the ABSENCE-first plan, the semantic checks and all nine categories stay required. A list that is capped (`truncated`) says nothing about the records it does not list."

def identifiers_of(detail):
    """The identifiers of a detail (numbers, versions, hashes, paths, file names, code names: the DETAIL rule of advice.py), as written, in order, without repeats."""
    import advice as A
    out = []
    for m in A.DETAIL.findall(detail or ""):
        d = m.strip("`").rstrip(".,:;)")
        if len(d) > 1 and d not in out: out.append(d)
    return out[:HINT_IDENTIFIERS]

def _positions(text, ident):
    """-> (exact count, [offsets of the first matches: the exact ones, or the case-insensitive ones when there is no exact match], case-insensitive count)."""
    def scan(t, x):
        offs, i, n = [], t.find(x), 0
        while i != -1:
            n += 1
            if len(offs) < HINT_OFFSETS: offs.append(i)
            i = t.find(x, i + max(len(x), 1))
        return n, offs
    n, offs = scan(text, ident)
    m, foffs = scan(text.lower(), ident.lower()) if ident else (0, [])
    return n, (offs if n else foffs), m

def _where(text, off):
    line = text.count("\n", 0, off) + 1
    return line, off - (text.rfind("\n", 0, off) + 1) + 1

def build_hints(detail, passage, ctx, requests, source):
    """The occurrence hints of one candidate (see HINT_NOTE). source_records: the eligible records of THIS evaluation (`block_index` = index in the printed eligible source, `line`/`block`/`record`/`kind`/`uuid` = exact provenance in the
    transcript, `offsets` = character offsets in the sanitized record text, `case_sensitive` False = only a case-insensitive match); requests: the user's requests of the session window (`request_index` as in scope.py); material_matches:
    where the identifier stands in the complete material (`section` note | reference, `ref`, `line`, `column` 1-based in that file's own text)."""
    ids = identifiers_of(detail); blocks, prov = ctx["eligible_blocks"], ctx["eligible_prov"]
    h = dict(navigation_only=True, note=HINT_NOTE, source=source, identifiers=ids, source_records=[], requests=[], material_matches=[], truncated={}, limits="caps only bound the listing: a missing record is never evidence that an identifier is absent from it, and a literal match is never evidence that it is current",
             quote_block_index=next((i for i, b in enumerate(blocks) if b is passage or b == passage), None))
    for ident in ids:
        hit = 0
        for i, (b, p) in enumerate(zip(blocks, prov)):
            n, offs, m = _positions(b, ident)
            if not m: continue
            hit += 1
            if hit <= HINT_CAP: h["source_records"].append(dict(identifier=ident, block_index=i, line=p.get("line"), record=p["record"], block=p["block"], kind=p["kind"], uuid=p.get("uuid"), case_sensitive=n > 0, count=n or m, offsets=offs))
        if hit > HINT_CAP: h["truncated"][ident] = hit - HINT_CAP
        for k, r in enumerate(requests, 1):
            rt = clean_text(r); n, offs, m = _positions(rt, ident)
            if m: h["requests"].append(dict(identifier=ident, request_index=k, case_sensitive=n > 0, count=n or m, offsets=offs))
        sections = [("note", None, ctx.get("note_text") or "")] + [("reference", ref, txt) for ref, txt in ctx.get("references") or []]
        for sec, ref, txt in sections:
            n, offs, m = _positions(txt, ident)
            if m:
                line, col = _where(txt, offs[0])
                h["material_matches"].append(dict(identifier=ident, section=sec, ref=ref, line=line, column=col, case_sensitive=n > 0, count=n or m))
    return h

def cmd_prepare(a):
    obj, code = prepare_one(a); print(json.dumps(obj, indent=1, ensure_ascii=False)); return code

BATCH_KEYS = ("source", "file", "write_id", "evaluated_against", "cwd", "run")

def cmd_prepare_batch(a):
    """Many candidates in ONE process, also across handoffs and versions: each spec item is {detail, source_quote} plus optional source/file/write_id/evaluated_against/cwd
    overriding the command-line defaults; element i is exactly the object `prepare` prints for item i with those arguments (exit 3 if any element is not ok)."""
    def bad(reason): print(json.dumps(dict(ok=False, reasons=[S.sanitize(reason)[0]]), indent=1, ensure_ascii=False)); return 3
    try: spec = json.load(sys.stdin) if a.spec == "-" else json.load(open(a.spec, encoding="utf-8"))   # `--spec -`: the JSON comes from stdin, no temporary file
    except (OSError, ValueError) as e: return bad("spec unreadable: %s" % ("stdin is not valid JSON" if a.spec == "-" else e))
    items = spec.get("candidates") if isinstance(spec, dict) else spec
    if not isinstance(items, list) or not items: return bad('spec must be a non-empty list (or {"candidates": [...]}) of {"detail", "source_quote"[, %s]}' % ", ".join(BATCH_KEYS))
    args = []
    for i, x in enumerate(items):
        if not isinstance(x, dict) or not isinstance(x.get("detail"), str) or not isinstance(x.get("source_quote"), str): return bad("item %d: detail and source_quote must be strings" % i)
        unknown = set(x) - {"detail", "source_quote"} - set(BATCH_KEYS)
        if unknown: return bad("item %d: unknown keys %s" % (i, sorted(unknown)))
        n = dict(vars(a), **{k: x[k] for k in BATCH_KEYS if k in x}, detail=x["detail"], source_quote=x["source_quote"])
        if n.get("run") is not None and not (isinstance(n["run"], str) and n["run"]): return bad("item %d: run must be a non-empty string (the tool_use id printed by `prepare` / `versions.py list`)" % i)
        if not all(isinstance(n.get(k), str) and n[k] for k in ("file", "write_id", "evaluated_against")): return bad("item %d: file, write_id and evaluated_against are required (item or command line)" % i)
        if n["evaluated_against"] not in EVALUATED: return bad("item %d: evaluated_against must be one of %s" % (i, list(EVALUATED)))
        args.append(argparse.Namespace(**n))
    L = doc = None
    if getattr(a, "ledger", None):   # fail closed BEFORE any work: a malformed ledger is never written over
        import ledger as L
        try: doc = L.load(a.ledger, must_exist=False)
        except L.LedgerError as e: return bad(str(e))
    if L:   # preparation establishes the association BEFORE any row is written: the mandatory audit (audit.py) expects every obligation of a ledger of the session whatever the report names
        import audit as AU, discover as D
        for src, cwd in dict.fromkeys((n.source, n.cwd) for n in args):
            sp, _, amb = D.resolve_session_info(src, cwd)
            if amb or not sp or not D.source_exists(sp): continue      # an unresolvable source: its rows are `unavailable` (kept by the ledger) and no registry exists to associate
            try: AU.associate_session_ledger(sp, a.ledger)
            except ValueError as e: return bad("the ledger cannot be associated with the candidate registry of the session: %s" % e)
    out, code, rows = [], 0, []
    for n in args:
        extra = {}; obj, c = prepare_one(n, extra); out.append(obj); code = max(code, c)
        if L: rows.append(L.row_of(n, obj, extra))
    if L:
        try: L.add_rows(doc, rows); L.save(a.ledger, doc)
        except (L.LedgerError, OSError) as e: return bad(str(e))
    print(json.dumps(out, indent=1, ensure_ascii=False)); return code

def build_parser():
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True); p = sub.add_parser("prepare")
    p.add_argument("--source"); p.add_argument("--file", required=True); p.add_argument("--write-id", required=True, dest="write_id"); p.add_argument("--evaluated-against", choices=EVALUATED, required=True, dest="evaluated_against")
    p.add_argument("--detail", required=True); p.add_argument("--source-quote", required=True, dest="source_quote"); p.add_argument("--cwd"); p.add_argument("--location", action="append", default=[]); p.add_argument("--run")
    b = sub.add_parser("prepare-batch")
    b.add_argument("--source"); b.add_argument("--file"); b.add_argument("--write-id", dest="write_id"); b.add_argument("--evaluated-against", choices=EVALUATED, dest="evaluated_against")
    b.add_argument("--spec", required=True, help="the JSON spec file, or - for stdin"); b.add_argument("--cwd"); b.add_argument("--location", action="append", default=[]); b.add_argument("--run")
    b.add_argument("--ledger", help="add the obligations of this batch to the resumable obligation ledger FILE (created if missing; rows are never dropped)")
    g = sub.add_parser("ledger"); gs = g.add_subparsers(dest="ledger_cmd", required=True)
    r = gs.add_parser("record"); r.add_argument("--ledger", required=True); r.add_argument("--id", required=True); r.add_argument("--stage", required=True, choices=("absence", "source")); r.add_argument("--tool-use-id", required=True, dest="tool_use_id")
    u = gs.add_parser("resume"); u.add_argument("--ledger", required=True); u.add_argument("--session", help="the session that holds the Jev calls (default: the source of each obligation)"); u.add_argument("--cwd")
    return ap

def cmd_ledger(a):
    import ledger as L
    def bad(reason): print(json.dumps(dict(ok=False, reasons=[S.sanitize(reason)[0]]), indent=1, ensure_ascii=False)); return 3
    try:
        doc = L.load(a.ledger)
        if a.ledger_cmd == "record":
            r = L.record(doc, a.id, a.stage, a.tool_use_id); L.save(a.ledger, doc); print(json.dumps(dict(ok=True, id=r["id"], stages=r["stages"], note="recorded only as a reference to re-read: nothing about the result is stored or trusted"), indent=1)); return 0
        print(json.dumps(L.resume(doc, a.session, a.cwd), indent=1, ensure_ascii=False)); return 0
    except (L.LedgerError, OSError) as e: return bad(str(e))

def main():
    a = build_parser().parse_args()
    return cmd_ledger(a) if a.cmd == "ledger" else cmd_prepare_batch(a) if a.cmd == "prepare-batch" else cmd_prepare(a)

if __name__ == "__main__": sys.exit(main())
