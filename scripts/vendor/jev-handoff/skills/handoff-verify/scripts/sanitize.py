"""Sanitize BEFORE any Jev call: exclude .env, redact demonstrable secrets, keep SHAs/ids. Ambiguous high-entropy values -> redacted + dependent check UNRESOLVED."""
import base64, binascii, os, re

REDACTED = "[REDACTED:%s]"
MARKER = re.compile(r"\[REDACTED:[a-z_]+\]")
not_marker = lambda v: not MARKER.fullmatch(v)   # a value that is ONLY a marker is not redacted again (the policy is idempotent); a value that merely STARTS with one still holds what follows it, so it is redacted whole
_QUOTE = r"\\*[\"']?"            # an opening/closing quote, possibly escaped (JSON-encoded text carries \" for ")
# (kind, regex, group that holds the SECRET): only that group is replaced, the surrounding text stays; None = the whole match
def _basic_credentials(v):
    """Is the token valid (padded or not) base64 of `user:password`?"""
    try: raw = base64.b64decode(v + "=" * (-len(v) % 4), altchars=b"-_" if re.search(r"[-_]", v) else None, validate=True)
    except (binascii.Error, ValueError): return False
    return b":" in raw

_ENDS = {}
def _quoted_end(text, pos, bs, q):
    """Index of the closing quote of a quoted value that starts at `pos`, or None (unterminated). The closing quote is the one at the same escaping depth as the opening one (`bs` backslashes + `q`); an escaped
    inner quote (`bs bs \\ q`) does not close. Linear: the pattern is searched, never backtracked, and nothing is bounded or cut at a newline, so no suffix of the value can stay behind."""
    rx = _ENDS.get((bs, q))
    if rx is None: rx = _ENDS[(bs, q)] = re.compile(r"(?P<inner>" + re.escape(bs) * 2 + r"\\" + re.escape(q) + r")|(?P<close>" + re.escape(bs) + re.escape(q) + ")")
    while True:
        m = rx.search(text, pos)
        if m is None: return None
        if m.lastgroup == "close": return m.start()
        pos = m.end()

def _redact_quoted(text, head, counts, kind):
    """NAME = "value": the WHOLE value is redacted, up to its closing quote, whatever it holds (spaces, escaped quotes, newlines, any length, a leading marker that an earlier pass made). Fail closed: a quote that never
    closes redacts to the END of the text (the end of the value is unknown, so none of the rest can be kept). A value of fewer than 8 characters is no secret and stays, and so does a value that is only a marker. The closing quote stays."""
    out, i = [], 0
    while True:
        m = head.search(text, i)
        if m is None: break
        start = m.end(); close = _quoted_end(text, start, m.group(2), m.group(3)); end = len(text) if close is None else close
        if end - start < 8 or not not_marker(text[start:end]): out.append(text[i:start]); i = start; continue
        out.append(text[i:start] + REDACTED % kind); i = end; counts[kind] = counts.get(kind, 0) + 1
    out.append(text[i:])
    return "".join(out)

DEMONSTRABLE = [
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), None),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), None),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), None),
    ("bearer", re.compile(r"(?i)\bBearer\s+(?:[A-Za-z0-9._~+/=-]{16,}|\[REDACTED:[a-z_]+\][A-Za-z0-9._~+/=-]+)"), None),   # also a token whose head an earlier pass turned into a marker, with what follows it
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), None),
    # NAME = "value" (quoted): not a regex but a scanner (`_redact_quoted`, above); the entry only names the kind and the head that opens a quoted value
    ("assignment", re.compile(r"(?i)\b([A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD|PASSWD)[A-Z0-9_]*)" + _QUOTE + r"\s*[:=]\s*(\\*)([\"'])"), _redact_quoted),
    # NAME = value, also JSON-quoted names (`"API_KEY": v`) and escaped quotes (`API_KEY=\\"v\\"` inside an encoded command); unquoted: the value ends at whitespace or a quote (a backslash only when it escapes a quote)
    ("assignment", re.compile(r"(?i)\b([A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD|PASSWD)[A-Z0-9_]*)" + _QUOTE + r"\s*[:=]\s*" + _QUOTE + r"((?:[^\s\"'\\]|\\(?![\"'])){8,})"), 2, not_marker),
    # scheme://user:PASSWORD@host: only the password
    ("url_userinfo", re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@\"'\\]+:([^\s/@\"'\\]+)@"), 1, not_marker),
    # Authorization: Basic <base64>, also quoted/escaped; a short token counts when it is valid base64 of `user:password`
    ("basic_auth", re.compile(r"(?i)\bAuthorization" + _QUOTE + r"\s*[:=]\s*" + _QUOTE + r"\s*Basic\s+([A-Za-z0-9+/=_-]{4,})"), 1, lambda v: len(v) >= 8 or _basic_credentials(v)),
]
SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
AMBIGUOUS = re.compile(r"(?<![/\w.-])(?=[A-Za-z0-9+_-]*[A-Z])(?=[A-Za-z0-9+_-]*[a-z])(?=[A-Za-z0-9+_-]*\d)[A-Za-z0-9+_-]{32,}(?![/\w.-])")  # path segments are facts, not secrets

def is_excluded_file(path):
    b = os.path.basename(path).lower()
    return b == ".env" or b.startswith(".env.") or b.endswith(".env")

def sanitize(text, canaries=(), ambiguous=True):
    """returns (clean_text, report) ; report: redactions [{kind,count}], ambiguous count, canary_leaks (must be 0). `ambiguous=False` skips ONLY the generic ambiguous-token rule (see `identity_altered`); nothing else."""
    redactions, out = {}, text
    def sub(kind, rx, grp, ok=None):
        nonlocal out
        if callable(grp): out = grp(out, rx, redactions, kind); return
        def f(m):
            if ok is not None and not ok(m.group(grp)): return m.group(0)
            redactions[kind] = redactions.get(kind, 0) + 1
            if grp is None: return REDACTED % kind
            a, b = m.span(grp); o = m.start()
            return m.group(0)[:a - o] + REDACTED % kind + m.group(0)[b - o:]
        out = rx.sub(f, out)
    for kind, rx, grp, *ok in DEMONSTRABLE:
        sub(kind, rx, grp, *ok)
    # keep uuids and git SHAs: protect them from the ambiguous-secret rule
    protected = {}
    def protect(m):
        k = "\x00P%d\x00" % len(protected); protected[k] = m.group(0); return k
    out = UUID.sub(protect, out); out = SHA.sub(protect, out)
    amb = 0
    def amb_sub(m):
        nonlocal amb; amb += 1; return REDACTED % "ambiguous"
    if ambiguous: out = AMBIGUOUS.sub(amb_sub, out)
    for k, v in protected.items():
        out = out.replace(k, v)
    leaks = [c for c in canaries if c and c in out]
    return out, {"redactions": redactions, "ambiguous_redacted": amb, "canary_leaks": leaks,
                 "dependent_checks_unresolved": amb > 0}

def sanitize_material(text):
    """THE shared policy of every canonical payload (omission material, source passage, scope context, details) and of its re-derivation: -> (clean text, info). info = dict(redactions {kind: count}, ambiguous_redacted,
    changed (clean != text), redaction_dependent (the clean text holds a redaction marker: a new one, or one that was already in the text)). A redaction-dependent payload is never a Jev-ready payload: what a hidden value
    says is not demonstrated, so the check that needs it stays UNRESOLVED (omissions.context / scope.scope_of fail closed; the validators refuse findings and exclusions that depend on a marker)."""
    clean, rep = sanitize(text)
    return clean, dict(redactions=rep["redactions"], ambiguous_redacted=rep["ambiguous_redacted"], changed=clean != text, redaction_dependent=bool(MARKER.search(clean)))

def redaction_dependent(text):
    """Does the text hold a redaction marker (it was sanitized, or it quotes sanitized text)?"""
    return bool(MARKER.search(text if isinstance(text, str) else ""))

def altered(text):
    """Would the shared policy change this text (a secret in it) or does it depend on a marker? A detail or quote like this is refused, never echoed."""
    return sanitize_material(text if isinstance(text, str) else "")[1]

# A RECORDED identifier (role "tool_use_id": a write id, a verification run id, a stage id) is not a secret to hide: the provider or the OpenCode adapter issued it and every binding compares it byte-for-byte. Only these forms are recognized: a
# provider prefix and an alphanumeric body, optionally followed by the adapter's duplicate suffix `~n` (n >= 2, opencode.py `uid`) and/or patch-section suffix `#k` (k >= 1). A long mixed-case body would be redacted by the generic ambiguous-token
# rule, so a recognized identity is exempt from THAT rule alone, and only in this role; a demonstrable credential (also one hidden behind the prefix, e.g. `call_AKIA...`) and a redaction marker are refused as everywhere else.
ID_ROLE = "tool_use_id"
RECORDED_ID = re.compile(r"(?:srvtoolu_|toolu_bdrk_|toolu_vrtx_|toolu_|call_)(?P<body>[A-Za-z0-9]{1,200})(?:~(?:[2-9]|[1-9][0-9]+))?(?:#[1-9][0-9]*)?")

def identity_altered(value, role=None):
    """Like `altered` for an IDENTITY field of the given role (None = the strict default, which every path, location, source selector and resolution keeps). -> the same info dict; `redaction_dependent` True = the identity is
    refused (never echoed, never replaced)."""
    base = altered(value)
    m = RECORDED_ID.fullmatch(value) if role == ID_ROLE and isinstance(value, str) else None
    if m is None: return base
    redactions = {}
    for x in (value, m.group("body")):
        for k, n in sanitize(x, ambiguous=False)[1]["redactions"].items(): redactions[k] = redactions.get(k, 0) + n
    if redactions or MARKER.search(value): return dict(redactions=redactions, ambiguous_redacted=0, changed=True, redaction_dependent=True)
    return dict(redactions={}, ambiguous_redacted=0, changed=False, redaction_dependent=False)
