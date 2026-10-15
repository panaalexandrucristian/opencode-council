"""Direct references only: one level, file must exist, passage must contain the detail; recursive references never count.
Current rules: a backticked path with spaces stays whole; a command names its files anywhere in it (wider extension list); a tracked-name target must lie inside its repository root; `git ls-files` runs once per root and its failure is the reason.
0.7.10 (measured on the 9 real notes verified on this machine, 8 of them blocked by an unresolved reference): a backticked command is not itself a reference, its path tokens are;
a shorthand fragment ('-T1b.md', '...-R08.json') is not a reference; '~' is expanded; the git root of each base is an extra base; a bare name (no '/') not found from the bases resolves to
the one tracked file of that name in those repositories, only when exactly one exists (`resolution` says how). A reference that still cannot be resolved blocks the material, as before."""
import hashlib, os, re, shlex, subprocess

KNOWN_EXTS = ("md", "txt", "json", "py", "sh", "yml", "yaml", "toml", "js", "mjs", "ts")   # the one list of known extensions (a standalone path and a path inside a command are recognized with the SAME list)
EXT = r"\.(?:%s)" % "|".join(KNOWN_EXTS)
CMD_EXT = EXT + "$"                                  # a file named inside a command
LINE_ANN = r":\d+(?::\d+)?(?:-\d+(?::\d+)?)?"       # path.ext:120  path.ext:10-20  path.ext:3:5
FRAG_ANN = r"#[^\s#`()\[\]<>|;&$*?{}\\]+"           # path.ext#decisions  path.ext#L10-L20
ANNOT_RX = re.compile(r"^(.+?%s)((?:%s)?(?:%s)?)$" % (EXT, LINE_ANN, FRAG_ANN))
LINK_EXTS = ("md", "txt")                            # a parenthesized name keeps counting without the Markdown link syntax only for these (unchanged); the other known extensions need `](target)`
SPAN_RX = re.compile(r"`([^`\n]+)`|\(([^)\s`]+)\)")
SHELL_RX = re.compile(r"[<>|;&$*?{}\\]")
ASSIGN_RX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
COMMAND_WORDS = frozenset("sh bash zsh dash ksh fish python python2 python3 node nodejs deno bun ruby perl php npx npm yarn pnpm uv uvx pip make env sudo time nohup source cd cat less more head tail cp mv rm ls vi vim nano code open git grep rg sed awk diff chmod mkdir touch xargs".split())

REMOTE_RX = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")

def is_remote(ref):
    """A link to another machine (`scheme://...`): never a local file, never fetched."""
    return bool(REMOTE_RX.match(ref or ""))

def split_annotation(ref):
    """-> (path, annotation): `docs/design.md:120` -> ("docs/design.md", ":120"); a reference without a line / range / fragment annotation (or whose name merely looks annotated but ends in a known extension) is (ref, "")."""
    m = ANNOT_RX.match(ref or "")
    return (m.group(1), m.group(2)) if m and m.group(2) else (ref, "")

def path_forms(ref):
    """The file names a reference may stand for, the reference as written first (a genuine file name wins), then the annotation-stripped path."""
    path, ann = split_annotation(ref)
    return [ref] + ([path] if ann else [])

def is_ref_text(p):
    """A path text that names a file reference: it ends in a known extension, or in one followed by a line / range / fragment annotation."""
    return bool(re.search(EXT + "$", p) or split_annotation(p)[1])

def _link_target(t, linked):
    """Is the parenthesized token `t` a reference? A name with a Markdown / text extension always counted (local or remote); a name with another known extension counts in the Markdown link syntax `](target)` (local file, or a remote link that blocks as unavailable)."""
    path, _ = split_annotation(t)
    if re.search(r"\.(?:%s)$" % "|".join(LINK_EXTS), path): return True
    return linked and bool(re.search(CMD_EXT, path))   # Markdown link syntax: a local file or a remote link (it blocks as unavailable, never fetched) of any known extension

def _is_command(p):
    first = p.split()[0]
    return bool(SHELL_RX.search(p) or ASSIGN_RX.match(first) or first in COMMAND_WORDS or re.fullmatch(r"python[\d.]+", first))

def _strip_comment(p):
    """The command without its shell comment. A comment starts at an UNQUOTED, UNESCAPED `#` that begins a WORD, read on the original text: a word begins at the start, after unquoted unescaped whitespace, or after an operator
    (`;|&()<>`). A quoted or escaped name (`cat "#notes.md" x.md`, `cat \\#notes.md`) is a file name, an escaped space (`a\\ #notes.md`) belongs to its word, and a `#` inside a word (`docs/design.md#decisions`, `$#`) is a fragment."""
    q, i, start = None, 0, True
    while i < len(p):
        c, nxt = p[i], False
        if q == "'":
            if c == "'": q = None
        elif q == '"':
            if c == "\\": i += 1
            elif c == '"': q = None
        elif c == "\\": i += 1
        elif c in "'\"": q = c
        elif c == "#" and start: return p[:i]
        elif c.isspace() or c in ";|&()<>": nxt = True
        start = nxt; i += 1
    return p

def _command_tokens(p):
    p = _strip_comment(p)
    try:
        lex = shlex.shlex(p, posix=True, punctuation_chars=True); lex.whitespace_split = True; lex.commenters = ""   # comments were removed on the original text; a `#` inside a word is a fragment, kept as written
        return list(lex)
    except ValueError: return [t.strip("\"'") for t in p.split()]

def direct_refs(handoff_text, bases=()):
    """The direct references of a note, in order of appearance, without repeats. A backticked text without whitespace is a path when it ends in EXT; with whitespace it is, in this order: an existing file (found from `bases`), a command (shell syntax,
    a VAR=value prefix or a known command word first: the files it names, anywhere in it, are the references), a path with spaces (it ends in EXT; kept whole even if it does not exist, so it blocks the material)."""
    refs = []
    def add(p):
        if p and not p.startswith(("-", "...")) and not re.search(r"[\t<>|;&$*?{}\\]", p) and p not in refs: refs.append(p)
    for m in SPAN_RX.finditer(handoff_text):
        if m.group(2) is not None:   # (target): a Markdown link, or a parenthesized name
            if _link_target(m.group(2), handoff_text[m.start() - 1:m.start()] == "]" if m.start() else False): add(m.group(2))
            continue
        p = m.group(1)
        if not p.strip(): continue   # a backticked space (Markdown such as "` `") names nothing
        if not re.search(r"\s", p):
            if is_ref_text(p): add(p)
            continue
        if bases and resolve(p, bases) is not None and p not in refs: refs.append(p)
        elif not _is_command(p) and is_ref_text(p): add(p)
        else:
            for tok in _command_tokens(p):
                if not tok.startswith("-") and "=" not in tok and (re.search(CMD_EXT, tok) or split_annotation(tok)[1]): add(tok)
    return refs

def git_root(path):
    d = os.path.realpath(path if os.path.isdir(path) else os.path.dirname(path) or ".")
    while True:
        if os.path.exists(os.path.join(d, ".git")): return d
        up = os.path.dirname(d)
        if up == d: return None
        d = up

def with_git_roots(bases):
    """The bases, then the git root of each (in order, without repeats)."""
    out = []
    for b in list(bases) + [git_root(b) for b in bases if b]:
        if b and b not in out: out.append(b)
    return out

_TRACKED_CACHE = {}   # canonical repository root -> (identity of its index, sorted tracked paths): `git ls-files` runs once per root while the index is unchanged; a failure is never cached

def _index_identity(root):
    """sha256 of the bytes of the repository's index (what `git ls-files` lists), or None when it cannot be read (then nothing is cached): a file added to or removed from the index changes it, whatever the size and mtime."""
    g = os.path.join(root, ".git")
    try:
        if os.path.isfile(g):
            t = open(g, encoding="utf-8", errors="replace").read().strip()
            if not t.startswith("gitdir:"): return None
            g = t[len("gitdir:"):].strip(); g = g if os.path.isabs(g) else os.path.join(root, g)
        h = hashlib.sha256()
        with open(os.path.join(g, "index"), "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""): h.update(chunk)
        return h.hexdigest()
    except OSError: return None

def reset_cache(): _TRACKED_CACHE.clear()

def _tracked(root):
    """-> (sorted tracked paths, None) or ([], 'git ls-files failed: <why>'): a failure is reported, never read as 'no files'."""
    key = os.path.realpath(root); ident = _index_identity(key)
    if key in _TRACKED_CACHE and ident is not None and _TRACKED_CACHE[key][0] == ident: return _TRACKED_CACHE[key][1], None
    try: p = subprocess.run(["git", "-C", key, "ls-files", "-z"], capture_output=True, timeout=30)
    except subprocess.TimeoutExpired: return [], "git ls-files failed: timeout after 30s in %s" % key
    except (OSError, subprocess.SubprocessError) as e: return [], "git ls-files failed: %s in %s" % (e, key)
    if p.returncode != 0:
        err = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return [], "git ls-files failed: exit %d%s in %s" % (p.returncode, ": " + err[0] if err else "", key)
    files = sorted(x.decode("utf-8", "replace") for x in p.stdout.split(b"\0") if x)
    if ident is not None: _TRACKED_CACHE[key] = (ident, files)
    else: _TRACKED_CACHE.pop(key, None)
    return files, None

def _inside(real, root):
    try: return os.path.commonpath([real, os.path.realpath(root)]) == os.path.realpath(root)
    except ValueError: return False

def resolve_info(ref, bases):
    """-> (real path | None, resolution): 'path' (found from a base), 'unique_tracked_name' (bare name, exactly one tracked file of that name, whose realpath is inside its repository root), or the reason it is not resolved.
    The reference as written is tried first (a genuine file name that looks annotated wins); only then the annotation-stripped path (`docs/design.md:120`, `#decisions`: the whole file). A remote link is never a file."""
    if is_remote(ref): return None, "remote link: not fetched, its content is unavailable"
    real, how = _resolve_literal(ref, bases)
    if real is not None: return real, how
    path, ann = split_annotation(ref)
    return _resolve_literal(path, bases) if ann else (real, how)

def _resolve_literal(ref, bases):
    ref_x = os.path.expanduser(ref)
    for b in bases:
        p = ref_x if os.path.isabs(ref_x) else os.path.join(b, ref_x)
        if os.path.isfile(p): return os.path.realpath(p), "path"
    if "/" in ref or os.sep in ref: return None, "not found from the bases"
    cands, errs = {}, []
    for r in sorted({git_root(b) for b in bases if b} - {None}):
        files, err = _tracked(r)
        if err: errs.append(err)
        for x in files:
            if os.path.basename(x) == ref and os.path.isfile(os.path.join(r, x)):
                real = os.path.realpath(os.path.join(r, x)); cands[real] = (r, _inside(real, r))
    if errs and len(cands) <= 1: return None, "; ".join(errs)   # a failed listing can hide a second file of this name: not resolved
    if len(cands) == 1:
        (real, (r, inside)), = cands.items()
        if inside: return real, "unique_tracked_name"
        return None, "the tracked file of this name resolves outside the repository root (%s)" % r
    return None, "not found from the bases" if not cands else "%d tracked files have this name" % len(cands)

def resolve(ref, bases):
    return resolve_info(ref, bases)[0]

def covers(handoff_path, handoff_text, detail_regex, bases):
    """Coverage from referenced files: only level-1 refs of THIS handoff; never recursive; passage returned for citation."""
    out = []
    for ref in direct_refs(handoff_text, bases):
        real = resolve(ref, bases)
        if real is None:
            out.append({"ref": ref, "exists": False, "covers": False}); continue
        if os.path.realpath(real) == os.path.realpath(handoff_path):
            continue
        try: txt = open(real, encoding="utf-8", errors="replace").read()
        except OSError:
            out.append({"ref": ref, "exists": False, "covers": False}); continue
        m = re.search(detail_regex, txt)
        out.append({"ref": ref, "exists": True, "real": real, "covers": bool(m), "passage": txt[max(0, m.start() - 80):m.end() + 80] if m else None})
    return out
