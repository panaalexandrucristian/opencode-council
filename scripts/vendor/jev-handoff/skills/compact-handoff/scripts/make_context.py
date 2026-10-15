#!/usr/bin/env python3
"""make_context.py --marker                        (prints a unique marker; use it with --current in the next command)
make_context.py SESSION | --current MARKER  [OUT] [--cut] [--keep] [--no-readonly] [--with-subagents] [--keep-work] [--opencode-db PATH] [--no-opencode]
make_context.py --opencode DB.sqlite [OUT] [--session ID] [--keep] [--no-readonly] [--keep-work]      (an OpenCode session; see below)
make_context.py --list-opencode [--opencode-db PATH] [--limit N] [--match TEXT]      (the sessions of OpenCode's database, newest first; see below)

One command from a session transcript to a start context for a NEW session.
  SESSION  a .jsonl path, or a session id (looked up in ~/.claude/projects/*/<id>.jsonl)
  --current MARKER  the CURRENT session: MARKER is a unique string the model printed with its own command a moment ago (e.g. echo handoff-marker-123); the transcript that contains it
           (among those written in the last 30 minutes) is the session. Exactly one must match.
  OUT      optional. Default mode: a folder (without OUT a TEMPORARY /tmp/handoff-<id>-<date>-<random>/ is made; it may go away when the computer restarts, or after a few days).
           With --cut: a path ending in .md, or a folder (then OUT/HANDOFF.md); without OUT a temporary file /tmp/handoff-<id>-<date>-<random>.md.
  --opencode DB  an OpenCode session instead of a Claude Code transcript: DB is OpenCode's SQLite file (or a copy with the rows of one session); only the tables session_v2/session_message are read (opened read-only; legacy sessions
           are refused). --session ID picks the session when the file holds several (zero or several without it is an error). The same folder is built (opencode_source.py renders the rows as Claude-shaped records first); the model's
           reasoning (text and encrypted) is not included, the `recent` text of a compaction and every image file go to blobs/ whole. --cut and --with-subagents are refused. The last line printed starts the new session with `opencode --prompt`.
  --list-opencode  list the sessions of OpenCode's database (the same default file and read-only opening as --current; --opencode-db PATH names another file), newest update first, at most --limit N (default 15), one line each:
           <id> | <YYYY-MM-DD HH:MM> | <N> msgs | <directory, home as ~> | <title cut at 80 characters>   and a last line  shown N of M sessions[ matching "TEXT"]; database: PATH.  --match TEXT keeps the sessions whose id,
           title or directory contains TEXT (case-insensitive). Only the number of messages is read, never their content. A missing, unreadable or legacy-only database is an error. No other option goes with it.
  --current also searches OpenCode's database ($XDG_DATA_HOME/opencode/opencode.db, else ~/.local/share/opencode/opencode.db; --opencode-db PATH names another file) for the marker, in sessions with a row from the last 30 minutes,
           and needs exactly ONE session across the Claude transcripts and the OpenCode sessions. A missing database file is skipped; one that exists but cannot be read stops the command with an error that names --no-opencode,
           which skips the OpenCode search (the Claude transcripts alone decide).
  --keep   put the result in a DURABLE place instead of /tmp: ~/.claude/handoffs/ (nothing deletes it by itself). An OUT outside /tmp counts as durable too.
Default mode: OUT/HANDOFF.md (the instructions for the new session first, then the compact transcript) and OUT/blobs/Rnnnn.txt. A tool call or tool result over 800 / 300 characters, and any tool call that only reads
(see compact_context.py; --no-readonly turns that rule off), is replaced by one marker line with a 100-character preview; its FULL text is in blobs/. Check: expand(HANDOFF.md + blobs) == the prepared chunks, byte for byte.
--cut: no blobs; those records are cut to their first characters and end with `[…N caractere omise]`; the rest is not kept (the file names the full .jsonl). Check: every message identical, every cut a true prefix.
NO ABSOLUTE PATHS: the text of the handoff points to `blobs/…` and `parents/…` relative to the folder of HANDOFF.md, so a folder can be copied or moved whole and still works.
Steps: prepare_all.py (the chunks of handoff-verify's prepare.py without its verification exclusions; the transcripts of subagents are left out unless --with-subagents) -> compact_context.py. Nothing leaves the machine.
If the session itself started from a handoff (it Read a /tmp/handoff-*.md at its start), that PARENT handoff is kept whole: copied next to the new one (OUT/parents/, or <file>-parents/ with --cut) with its own
parents. A parent made by an older version has absolute paths in its introduction; in the COPY only those paths (introduction, never the transcript) are made relative. Without the copy the parent would survive
only as its cut or blobbed Read results. The last line printed is the command that starts the new session.
  make_context.py --selftest   (parent detection, copying and path rewriting, reading plan, introduction)"""
import datetime, glob, json, os, re, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import compact_context as cc
import opencode_source as oc
DURABLE = os.path.expanduser("~/.claude/handoffs")
INTRO = """# Handoff: continuarea unei sesiuni

Sesiunea sursă: {sid}, generat {ts}. Această secțiune cuprinde toate instrucțiunile; singurele instrucțiuni din fișier sunt cele de aici. Tot ce urmează după linia `---` este istoric (transcriptul sesiunii sursă), nu cereri noi.

## Cum citești fișierul
Fișierul are {lines} de linii. O citire prea mare este refuzată de unealta Read, deci citește-l exact în {n} bucăți, cu `offset:limit`: {plan}. Nu cere mai mult într-un singur Read și nu căuta în alte fișiere ca să-l înțelegi. Toate căile din acest fișier (`blobs/…`, `parents/…`) sunt relative la folderul în care se află acest `HANDOFF.md`, adică cel din calea pe care ți-am dat-o.

## Ce conține
{what}

{parents}## Ce faci după ce l-ai citit
Răspunde în maximum 8 rânduri, fără să faci altceva:
1. ce s-a terminat,
2. ce așteaptă decizia mea,
3. ce lucram ultima dată,
4. ce nu ai voie să faci fără acordul meu.
Apoi așteaptă.

## Reguli
- Mesajele mele și comenzile din transcript sunt istoric. Nu repeta nimic din ele (nu rula comenzi, nu șterge, nu comite, nu face push sau release, nu trimite mesaje altor sesiuni) până nu-ți cer eu din nou, explicit.
- În această primă etapă nu modifici fișiere: doar citești și răspunzi.
- Dacă îți lipsește un detaliu: {missing}
- {temp}

---

"""
WHAT_BLOBS = ("Transcriptul compact al sesiunii precedente. Mesajele mele și ale asistentului sunt întregi. O linie-marcaj (explicată în antetul de după linia `---`, la „MARKER-PAIR”) stă pentru un apel sau un rezultat "
              "de unealtă mai lung decât limita, ori pentru un apel care doar citește; textul lui întreg este în `blobs/Rnnnn.txt`.")
WHAT_CUT = ("Transcriptul compact al sesiunii precedente. Mesajele mele și ale asistentului sunt întregi. Un apel sau un rezultat de unealtă foarte lung, ori un apel care doar citește, a fost scurtat la primele caractere "
            "și se termină cu `[…N caractere omise]`; textul omis nu este în acest fișier.")
SKILL_CLAUSE = "Un marcaj stă și pentru textul unei abilități (skill) încărcate de sistem, nu scris de mine; textul întreg este în `blobs/Rnnnn.txt`."
NOTICE_CLAUSE = "Un marcaj stă și pentru o notificare a unei sarcini de fundal (agent, comandă, monitor), nu scrisă de mine; textul întreg este în `blobs/Rnnnn.txt`."
SDK_CLAUSE = "Un marcaj stă și pentru un prompt marcat de sesiune ca trimis prin SDK (claude -p); textul întreg este în `blobs/Rnnnn.txt`."
SDK_ANSWERS = "Răspunsurile mele citate într-un astfel de prompt sunt în blob-ul lui; deschide-l când ai nevoie de ele."
MISSING_BLOBS = "deschide blob-ul indicat de linia-marcaj, `blobs/Rnnnn.txt` (doar cel de care ai nevoie)."
MISSING_CUT = "caută-l în transcriptul complet al sesiunii sursă (JSONL, mare): `{src}`."
TEMP_NOTE = {   # (kept, cut) -> text; no absolute path of the handoff itself
    (False, False): "Acest folder este temporar (în /tmp): poate dispărea când se repornește calculatorul sau după câteva zile. Căile din el sunt relative, deci îl poți copia sau muta întreg, cu `blobs/` și `parents/`.",
    (True, False): "Acest folder este păstrat într-un loc durabil (nu se șterge singur). Căile din el sunt relative, deci îl poți copia sau muta întreg, cu `blobs/` și `parents/`.",
    (False, True): "Acest fișier este temporar (în /tmp): poate dispărea când se repornește calculatorul sau după câteva zile. Părinții lui sunt în folderul `<nume>-parents/` de lângă el.",
    (True, True): "Acest fișier este păstrat într-un loc durabil (nu se șterge singur). Părinții lui sunt în folderul `<nume>-parents/` de lângă el.",
}
OC_SUBS = (   # the OpenCode introduction is the Claude one with exactly these substitutions (a test reverses them), plus OC_EVENTS on the line of WHAT_BLOBS
    ("Sesiunea sursă: {sid}, generat {ts}.", "Sesiunea sursă: sesiune OpenCode `{sid}`, generat {ts}."),
    ("O citire prea mare este refuzată de unealta Read, deci citește-l exact în {n} bucăți, cu `offset:limit`: {plan}. Nu cere mai mult într-un singur Read și nu căuta",
     "O citire prea mare poate fi refuzată sau trunchiată de unealta de citire a fișierelor (`Read` în Claude Code, `read` în OpenCode, cu parametrii `offset` și `limit`), deci citește-l exact în {n} bucăți, cu `offset:limit`: {plan}. Nu cere mai mult într-o singură citire și nu căuta"),
)
INTRO_OC = INTRO
for _a, _b in OC_SUBS:
    assert INTRO_OC.count(_a) == 1
    INTRO_OC = INTRO_OC.replace(_a, _b)
OC_EVENTS = (" Liniile care încep cu `[opencode …]` sunt evenimente ale sesiunii OpenCode, nu mesaje scrise de mine. Raționamentul modelului (text și conținut criptat) nu este inclus")
OC_RECENT = "; textele `recent` ale compactărilor sunt în `blobs/Rnnnn.txt`"
OC_IMAGES = "; imaginile sunt în `blobs/Rnnnn.txt`, ca date base64, nu text, și nu sunt verificate pentru secrete"
READ_BUDGET = 40000   # characters per Read, line-number prefix included; the Read tool refuses about 47,000 (25,000 tokens)


def reading_plan(lines, budget=READ_BUDGET, prefix=8, max_lines=400):
    """-> [(offset, limit)] covering every line, 1-based, each chunk within `budget` characters (a single longer line is a chunk of its own) and within `max_lines` lines."""
    chunks, start, acc, count = [], 1, 0, 0
    for i, line in enumerate(lines, 1):
        add = len(line) + 1 + prefix
        if count and (acc + add > budget or count >= max_lines): chunks.append((start, count)); start, acc, count = i, 0, 0
        acc += add; count += 1
    if count: chunks.append((start, count))
    return chunks


def build_intro(cut, sid, ts, src, kept, parents, lines=0, chunks=((1, 1),), skills=False, notices=False, opencode=None, sdks=False):
    """The introduction with every instruction. It names no absolute path of the handoff itself (only the source transcript, which is durable).
    `opencode`: None for a Claude Code transcript; for an OpenCode session (blob mode) a pair (has compaction `recent` texts in blobs, has image files in blobs), and `sid` is then the full ses_ id."""
    plan = ", ".join("%d:%d" % c for c in chunks)
    oc_clause = "" if opencode is None else OC_EVENTS + (OC_RECENT if opencode[0] else "") + (OC_IMAGES if opencode[1] else "") + "."
    return (INTRO if opencode is None else INTRO_OC).format(sid=sid, ts=ts, lines=lines, n=len(chunks), plan=plan, parents=parents, temp=TEMP_NOTE[(bool(kept), bool(cut))],
                        what=(WHAT_CUT if cut else WHAT_BLOBS + (" " + SKILL_CLAUSE if skills else "") + (" " + NOTICE_CLAUSE if notices else "") + (" " + SDK_CLAUSE + " " + SDK_ANSWERS if sdks else "") + oc_clause), missing=(MISSING_CUT.format(src=src) if cut else MISSING_BLOBS))


HANDOFF_PATH = re.compile(r"/handoff-[0-9a-f]{8}-\d{8}-[^/\s\"]+(?:\.md|/HANDOFF\.md)$")


def find_parents(sess, first_calls=4):
    """The handoff files the session read with the Read tool among its first `first_calls` tool calls (that is how a session starts from a handoff), in order, without duplicates."""
    found, calls = [], 0
    for line in open(sess, encoding="utf-8", errors="replace"):
        try: o = json.loads(line)
        except Exception: continue
        c = (o.get("message") or {}).get("content")
        if o.get("type") != "assistant" or not isinstance(c, list): continue
        for it in c:
            if isinstance(it, dict) and it.get("type") == "tool_use":
                calls += 1
                fp = (it.get("input") or {}).get("file_path") if it.get("name") == "Read" else None
                if isinstance(fp, str) and HANDOFF_PATH.search(fp) and fp not in found: found.append(fp)
                if calls >= first_calls: return found
    return found


def skill_meta_uuids(sess):
    """The uuids of the records the harness injected with the text of a skill: type user, isMeta true, content (a string, or the first text block of a list) starting with the skill prefix. Lines that do not parse are skipped.
    Only these may be moved to a blob; a message the user typed is never in the set."""
    out = set()
    for line in open(sess, encoding="utf-8", errors="replace"):
        try: o = json.loads(line)
        except Exception: continue
        if not isinstance(o, dict) or o.get("type") != "user" or o.get("isMeta") is not True or not isinstance(o.get("uuid"), str): continue
        c = (o.get("message") or {}).get("content")
        if isinstance(c, list): c = next((it.get("text") for it in c if isinstance(it, dict) and it.get("type") == "text"), None)
        if isinstance(c, str) and c.startswith(cc.SKILL_PREFIX): out.add(o["uuid"])
    return frozenset(out)


def notice_uuids_of(sess):
    """The uuids of the records the harness wrote when a background task (agent, command, monitor) finished: type user, origin.kind task-notification, content (a string, or the first text block of a list) starting
    with the tag. Lines that do not parse are skipped. Only these may be moved to a blob by this rule; a message the user typed, a message of another session or a skill text is never in the set."""
    out = set()
    for line in open(sess, encoding="utf-8", errors="replace"):
        try: o = json.loads(line)
        except Exception: continue
        if not isinstance(o, dict) or o.get("type") != "user" or not isinstance(o.get("uuid"), str): continue
        origin = o.get("origin")
        if not isinstance(origin, dict) or origin.get("kind") != "task-notification": continue
        c = (o.get("message") or {}).get("content")
        if isinstance(c, list): c = next((it.get("text") for it in c if isinstance(it, dict) and it.get("type") == "text"), None)
        if isinstance(c, str) and c.startswith(cc.NOTE_PREFIX): out.add(o["uuid"])
    return frozenset(out)


def sdk_uuids_of(sess):
    """The uuids of the prompts the session marks as sent through the SDK (claude -p), except the first and the last of them (in file order, whatever their length): type user, promptSource sdk (exactly), a string uuid,
    not a sidechain, not isMeta, not a compaction summary, no origin kind, content a string or a list whose first block is a text, no tool_result block. Lines that do not parse are skipped. Only these may be moved to a blob
    by this rule; a message typed or queued by the user, an unmarked record, a tool result or a message of another session is never in the set, and a session with one or two such prompts moves nothing."""
    out = []
    for line in open(sess, encoding="utf-8", errors="replace"):
        try: o = json.loads(line)
        except Exception: continue
        if not isinstance(o, dict) or o.get("type") != "user" or o.get("promptSource") != "sdk" or not isinstance(o.get("uuid"), str): continue
        if o.get("isSidechain") is True or o.get("isMeta") is True or o.get("isCompactSummary") is True: continue
        origin = o.get("origin")
        if isinstance(origin, dict) and origin.get("kind") is not None: continue
        c = (o.get("message") or {}).get("content")
        if isinstance(c, list):
            if any(isinstance(it, dict) and it.get("type") == "tool_result" for it in c): continue
            c = next((it.get("text") for it in c if isinstance(it, dict) and it.get("type") == "text"), None)
        if isinstance(c, str): out.append(o["uuid"])
    return frozenset(out[1:-1])


def relativize_intro(md_path, orig_dir, orig_file=None):
    """In the COPY of a parent made by an older version: make the absolute paths of the original relative, in the introduction only (up to the first line `---`; the transcript is never touched, and no line is added or removed,
    so the reading plan stays valid). A folder handoff: `<dir>/blobs/` -> `blobs/`, `<dir>/parents/` -> `parents/`, any other `<dir>` -> `acest folder`. A single .md: its own path -> `acest fișier`, `<dir>/` -> ``.
    Returns True when something changed."""
    t = open(md_path, encoding="utf-8", newline="").read(); k = t.find("\n---\n")
    if k < 0: return False
    intro, rest = t[:k], t[k:]; new = intro
    if orig_file is None:
        new = new.replace(orig_dir + "/blobs/", "blobs/").replace(orig_dir + "/parents/", "parents/").replace(orig_dir, "acest folder")
    else:
        new = new.replace(orig_file, "acest fișier").replace(orig_dir + "/", "")
    if new == intro: return False
    open(md_path, "w", encoding="utf-8", newline="").write(new + rest)
    return True


def copy_parents(parents, dest):
    """Copy each existing parent handoff into `dest` (a folder handoff with its blobs/ and its own parents/, or a single .md with its <stem>-parents folder) and make the absolute paths of its introduction relative
    -> [(original, copy, exists)]."""
    out = []
    for fp in parents:
        if not os.path.exists(fp): out.append((fp, None, False)); continue
        os.makedirs(dest, exist_ok=True)
        if os.path.basename(fp) == "HANDOFF.md":
            src = os.path.dirname(fp); dst = os.path.join(dest, os.path.basename(src))
            if not os.path.exists(dst): shutil.copytree(src, dst); relativize_intro(os.path.join(dst, "HANDOFF.md"), src)
            out.append((fp, os.path.join(dst, "HANDOFF.md"), True))
        else:
            dst = os.path.join(dest, os.path.basename(fp))
            if not os.path.exists(dst): shutil.copy2(fp, dst); relativize_intro(dst, os.path.dirname(fp), fp)
            side = fp[:-3] + "-parents"
            if os.path.isdir(side) and not os.path.exists(os.path.join(dest, os.path.basename(side))): shutil.copytree(side, os.path.join(dest, os.path.basename(side)))
            out.append((fp, dst, True))
    return out


def parents_paragraph(copies, base):
    """The section about the parent handoffs, with paths RELATIVE to `base` (the folder of the new HANDOFF.md)."""
    if not copies: return ""
    return ("## Handoff părinte\nAceastă sesiune a pornit din alt handoff. Părintele este păstrat întreg în: " + "; ".join(
        ("`%s`" % os.path.relpath(cp, base)) if ok else ("`%s` (nu mai exista când s-a făcut acest handoff)" % os.path.basename(fp)) for fp, cp, ok in copies)
        + " (căi relative la folderul acestui fișier). Din el ai inline doar rezumatul din acest fișier; citește-l (tot în bucăți, cu planul lui de citire) când ai nevoie de detalii. Dacă și el a pornit dintr-un handoff, "
        "îl găsești lângă el, în `parents/` sau în `<nume>-parents/`.\n\n")


PROJECTS = os.path.expanduser("~/.claude/projects")


def find_current(marker, projects=PROJECTS, max_age=1800, wait=4):
    """The transcripts (.jsonl, written in the last `max_age` seconds) that contain `marker`. The model's own command wrote it into the transcript just before, so the file may need a moment: retry for `wait` seconds."""
    needle = marker.encode("utf-8"); deadline = time.time() + wait
    while True:
        hits = []
        for p in glob.glob(os.path.join(projects, "*", "*.jsonl")):
            try:
                if time.time() - os.path.getmtime(p) > max_age: continue
                with open(p, "rb") as f:
                    if needle in f.read(): hits.append(p)
            except OSError: pass
        if hits or time.time() >= deadline: return sorted(hits)
        time.sleep(1)


def find_current_all(marker, projects=PROJECTS, max_age=1800, wait=4, db=None, use_opencode=True):
    """-> (Claude transcripts, OpenCode session ids) that contain `marker`, searched together; one pass, repeated for `wait` seconds while there is no hit at all (the model's own command wrote the marker just before).
    `db`: the OpenCode database file (a missing file is skipped); `use_opencode` False skips it (--no-opencode). oc.OpenCodeError when an existing database cannot be read."""
    deadline = time.time() + wait
    while True:
        claude = find_current(marker, projects, max_age, 0)
        sessions = oc.find_current(marker, db, max_age) if (use_opencode and db) else []
        if claude or sessions or time.time() >= deadline: return claude, sessions
        time.sleep(1)


def take_option(a, name):
    """Remove `name VALUE` from the argument list `a` (in place) -> VALUE, or None when it is not there."""
    if name not in a: return None
    i = a.index(name)
    if i + 1 >= len(a) or a[i + 1].startswith("--"): sys.exit("%s needs a value" % name)
    v = a[i + 1]; del a[i:i + 2]
    return v


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("step failed: %s\n%s" % (" ".join(os.path.basename(c) for c in cmd[:3]), (r.stderr or r.stdout)[-600:]))
    return r


def build_opencode(db, session, rest):
    """The folder for ONE OpenCode session: `rest` = the arguments after the database (an optional OUT folder, --keep, --no-readonly, --keep-work). Same steps as for a Claude transcript, with the rows rendered by
    opencode_source.py as the prepared text; the printed original is the sum of length(data) of the session's rows."""
    for bad in ("--cut", "--with-subagents"):
        if bad in rest: sys.exit("%s is not supported with an OpenCode session (the cut file and the subagent transcripts are Claude Code formats)" % bad)
    if not os.path.exists(db): sys.exit("OpenCode database not found: %s" % db)
    try:
        conn = oc.connect(db)
        try:
            sid = oc.pick_session(conn, session); R = oc.render(conn, sid)
        finally: conn.close()
    except oc.OpenCodeError as e: sys.exit(str(e))
    if not R.recs: sys.exit("session %s has no messages in session_message" % sid)
    keep_work = "--keep-work" in rest; ro = "--no-readonly" not in rest; keep = "--keep" in rest
    out_arg = rest[0] if rest and not rest[0].startswith("--") else None
    short = sid[4:] if sid.startswith("ses_") else sid; now = datetime.datetime.now(); prefix = "handoff-%s-%s-" % (short[:8], now.strftime("%Y%m%d"))
    o_abs = os.path.abspath(os.path.expanduser(out_arg)) if out_arg else None
    kept = keep or (o_abs is not None and not o_abs.startswith(("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")))
    base_dir = DURABLE if keep else "/tmp"
    if keep or (kept and o_abs is None): os.makedirs(DURABLE, exist_ok=True)
    folder = o_abs if o_abs else tempfile.mkdtemp(prefix=prefix, dir=base_dir)
    os.makedirs(folder, exist_ok=True); final = os.path.join(folder, "HANDOFF.md")
    copies = copy_parents(oc.find_parents(R.first_calls, HANDOFF_PATH), os.path.join(folder, "parents"))
    for fp, cp, ok in copies: print("parent handoff:", fp, "->", cp if ok else "MISSING (not copied)")
    work = tempfile.mkdtemp(prefix="make_context_")
    try:
        os.makedirs(os.path.join(work, "out", "transcript"))
        open(os.path.join(work, "out", "transcript", "chunk-0000.txt"), "w", encoding="utf-8", newline="").write(R.text)
        ts = now.strftime("%Y-%m-%d %H:%M"); paragraph = parents_paragraph(copies, folder); flags = (R.n_compaction_recent > 0, R.n_files > 0)
        ro_uuids = frozenset(R.readonly) if ro else frozenset(); forced = dict(R.forced)
        def compact(intro, dest):   # -> (printed check line, passed)
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ok = cc.cmd_check(os.path.join(work, "out"), dest, cc.DEFAULT_LIMITS, cc.DEFAULT_PREVIEW, ro, intro, "HANDOFF.md", True, frozenset(), frozenset(), ro_uuids, forced, R.original)
            return buf.getvalue().strip(), ok
        draft = build_intro(False, sid, ts, db, kept, paragraph, opencode=flags)
        chunks, lines = ((1, 1),), 0
        for attempt in range(3):   # as for a Claude transcript: the plan changes only numbers inside one line
            scratch = os.path.join(work, "draft%d" % attempt); line, ok = compact(draft, scratch)
            text_lines = open(os.path.join(scratch, "HANDOFF.md"), encoding="utf-8").read().split("\n")
            new_chunks = tuple(reading_plan(text_lines)); new_lines = len(text_lines)
            if (new_chunks, new_lines) == (chunks, lines): break
            chunks, lines = new_chunks, new_lines; draft = build_intro(False, sid, ts, db, kept, paragraph, lines, chunks, opencode=flags)
        line, ok = compact(draft, folder)
        print(line)
        if not ok: sys.exit("the check did not pass")
        got = open(final, encoding="utf-8").read().split("\n")
        assert len(got) == lines and tuple(reading_plan(got)) == chunks, "the reading plan does not match the final file"
        c2, b2, p2 = cc.read_out(folder)
        problems = oc.selfcheck(R, cc.expand_text(c2, b2, p2), set(b2.values()))
        if problems: sys.exit("the converter self-check failed: " + "; ".join(problems))
        print("sanitized records: %d of %d (their strings are not compared one by one; every file block's uri is identical in its blob)" % (R.sanitized, len(R.recs)))
        final_text = open(final, encoding="utf-8").read(); intro_text = final_text[:final_text.index("\n---\n")]
        leaks = [m for m in re.findall(r"/(?:private/)?tmp/handoff-[^\s`'\")]+", intro_text)]
        assert not leaks, "an absolute path of a handoff is left in the introduction: %s" % leaks[:2]
        print("reading plan: %d chunks of at most %d characters (%d lines)" % (len(chunks), READ_BUDGET, lines))
        print("location: %s" % ("durable (~/.claude/handoffs or the folder you gave)" if kept else "temporary (/tmp)"))
        print("ready:", final)
        print('start the new session with:\n  opencode --prompt "Citește %s și urmează instrucțiunile din el."' % final)
    finally:
        if keep_work: print("work files kept in", work)
        else: shutil.rmtree(work, ignore_errors=True)


def list_opencode(db, limit=None, match=None):
    """--list-opencode: print the sessions of the database `db` (see the docstring), then the summary line. The same lookup, read-only opening and errors as --current (a missing file is an error here)."""
    if limit is None: n = oc.LIST_LIMIT
    else:
        if not re.fullmatch(r"[0-9]+", limit) or int(limit) < 1: print("--limit needs a whole number of at least 1, not %r" % limit, file=sys.stderr); sys.exit(2)
        n = int(limit)
    if not os.path.exists(db): sys.exit("OpenCode database not found: %s" % db)
    try:
        conn = oc.connect(db)
        try: rows, total = oc.list_sessions(conn, match, n)
        finally: conn.close()
    except oc.OpenCodeError as e: sys.exit(str(e))
    home = os.path.expanduser("~")
    for r in rows: print(oc.session_line(r, home))
    print(oc.summary_line(len(rows), total, match, os.path.abspath(db)))


def main():
    a = sys.argv[1:]
    if a == ["--marker"]: print("handoff-marker-%d-%s" % (int(time.time()), os.urandom(6).hex())); return
    oc_db = take_option(a, "--opencode-db"); session = take_option(a, "--session"); limit = take_option(a, "--limit"); match = take_option(a, "--match"); no_oc = "--no-opencode" in a
    a = [x for x in a if x != "--no-opencode"]
    if oc_db is not None: oc_db = os.path.expanduser(oc_db)
    if "--list-opencode" in a:
        if a != ["--list-opencode"] or session is not None or no_oc: sys.exit("--list-opencode takes only --opencode-db PATH, --limit N and --match TEXT")
        return list_opencode(oc_db or oc.default_db(), limit, match)
    if limit is not None or match is not None: sys.exit("--limit and --match work only with --list-opencode")
    if session is not None and a[:1] != ["--opencode"]: sys.exit("--session works only with --opencode DB")
    if a[:1] == ["--opencode"]:
        if len(a) < 2 or a[1].startswith("--"): print(__doc__); sys.exit(2)
        return build_opencode(os.path.expanduser(a[1]), session, a[2:])
    if len(a) >= 2 and a[0] == "--current":
        db = oc_db or oc.default_db()
        try: hits, sessions = find_current_all(a[1], db=db, use_opencode=not no_oc)
        except oc.OpenCodeError as e: sys.exit("--current: %s. Report this to the user; to search only the Claude Code transcripts, run the same command again with --no-opencode." % e)
        if len(hits) + len(sessions) != 1:
            if not sessions: sys.exit("--current: %d transcripts contain the marker %r (need exactly 1); give the session id instead" % (len(hits), a[1]))
            sys.exit("--current: %d sessions contain the marker %r (%d Claude Code transcripts, %d OpenCode sessions; need exactly 1); give the session id instead" % (len(hits) + len(sessions), a[1], len(hits), len(sessions)))
        if sessions: print("current session:", sessions[0]); return build_opencode(db, sessions[0], a[2:])
        print("current session:", os.path.basename(hits[0])[:-6]); a = [hits[0]] + a[2:]
    if not a or a[0].startswith("--"): print(__doc__); sys.exit(2)
    sess = a[0]; keep_work = "--keep-work" in a; cut = "--cut" in a
    out_arg = a[1] if len(a) > 1 and not a[1].startswith("--") else None
    if not sess.endswith(".jsonl"):
        hits = glob.glob(os.path.expanduser("~/.claude/projects/*/%s.jsonl" % sess))
        if len(hits) != 1: sys.exit("session id %r: %d transcripts found" % (sess, len(hits)))
        sess = hits[0]
    sess = os.path.abspath(sess); sid = os.path.basename(sess)[:-len(".jsonl")]; now = datetime.datetime.now()
    prefix = "handoff-%s-%s-" % (sid[:8], now.strftime("%Y%m%d"))
    o_abs = os.path.abspath(os.path.expanduser(out_arg)) if out_arg else None
    kept = "--keep" in a or (o_abs is not None and not o_abs.startswith(("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")))
    base_dir = DURABLE if "--keep" in a else "/tmp"
    if "--keep" in a or (kept and o_abs is None): os.makedirs(DURABLE, exist_ok=True)
    if cut:
        if o_abs is None:
            fd, final = tempfile.mkstemp(prefix=prefix, suffix=".md", dir=base_dir); os.close(fd)
        else:
            final = o_abs if o_abs.endswith(".md") else os.path.join(o_abs, "HANDOFF.md"); os.makedirs(os.path.dirname(final), exist_ok=True)
        folder = None; base = os.path.dirname(final)
    else:
        folder = o_abs if o_abs else tempfile.mkdtemp(prefix=prefix, dir=base_dir)
        os.makedirs(folder, exist_ok=True); final = os.path.join(folder, "HANDOFF.md"); base = folder
    parents = find_parents(sess)
    pdest = (os.path.join(base, os.path.splitext(os.path.basename(final))[0] + "-parents") if cut else os.path.join(folder, "parents"))
    copies = copy_parents(parents, pdest)
    for fp, cp, ok in copies: print("parent handoff:", fp, "->", cp if ok else "MISSING (not copied)")
    work = tempfile.mkdtemp(prefix="make_context_")
    try:
        run([sys.executable, "-B", os.path.join(HERE, "prepare_all.py"), sess, "--cwd", work, "--out", os.path.join(work, "out")] + (["--with-subagents"] if "--with-subagents" in a else []))
        meta = frozenset() if cut else skill_meta_uuids(sess); skills = bool(meta)
        notes = frozenset() if cut else notice_uuids_of(sess)
        sdk = frozenset() if cut else sdk_uuids_of(sess)
        chunk_text = cc.read_chunks(os.path.join(work, "out"))
        notices = cc.count_moved_notices(chunk_text, notes, cc.DEFAULT_LIMITS, not cut) > 0   # the clause only when the compaction will really move one
        sdks = cc.count_moved_sdk(chunk_text, sdk, cc.DEFAULT_LIMITS, not cut) > 0   # the same for the prompts of the SDK
        ts = now.strftime("%Y-%m-%d %H:%M"); paragraph = parents_paragraph(copies, base)
        ro = "--no-readonly" not in a
        def compact(intro, dest):   # -> (printed check line, passed)
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ok = cc.cmd_check(os.path.join(work, "out"), dest, cc.DEFAULT_LIMITS, cc.DEFAULT_PREVIEW, ro, intro, "HANDOFF.md", not cut, meta, notes, sdk_uuids=sdk)
            return buf.getvalue().strip(), ok
        draft = build_intro(cut, sid[:8], ts, sess, kept, paragraph, skills=skills, notices=notices, sdks=sdks)
        chunks, lines = ((1, 1),), 0
        for attempt in range(3):   # the plan changes only numbers inside one line, so the line count settles at once; the loop is a safeguard
            scratch = os.path.join(work, "draft%d" % attempt); line, ok = compact(draft, scratch)
            text_lines = open(os.path.join(scratch, "HANDOFF.md"), encoding="utf-8").read().split("\n")
            new_chunks = tuple(reading_plan(text_lines)); new_lines = len(text_lines)
            if (new_chunks, new_lines) == (chunks, lines): break
            chunks, lines = new_chunks, new_lines; draft = build_intro(cut, sid[:8], ts, sess, kept, paragraph, lines, chunks, skills, notices, sdks=sdks)
        dest = os.path.join(work, "res") if cut else folder
        line, ok = compact(draft, dest)
        print(line)
        if not ok: sys.exit("the check did not pass")
        got = open(os.path.join(dest, "HANDOFF.md"), encoding="utf-8").read().split("\n")
        assert len(got) == lines and tuple(reading_plan(got)) == chunks, "the reading plan does not match the final file"
        if cut: shutil.move(os.path.join(work, "res", "HANDOFF.md"), final)
        final_text = open(final, encoding="utf-8").read(); intro_text = final_text[:final_text.index("\n---\n")]
        leaks = [m for m in re.findall(r"/(?:private/)?tmp/handoff-[^\s`'\")]+", intro_text)]
        assert not leaks, "an absolute path of a handoff is left in the introduction: %s" % leaks[:2]
        print("reading plan: %d chunks of at most %d characters (%d lines)" % (len(chunks), READ_BUDGET, lines))
        print("location: %s" % ("durable (~/.claude/handoffs or the folder you gave)" if kept else "temporary (/tmp)"))
        print("ready:", final)
        print('start the new session with:\n  claude "Citește %s și urmează instrucțiunile din el."' % final)
    finally:
        if keep_work: print("work files kept in", work)
        else: shutil.rmtree(work, ignore_errors=True)


def selftest_opencode():
    """The OpenCode part of the self test (nothing real is read: every database is built here, in a temporary folder) -> the number of cases."""
    import itertools, sqlite3
    n = 0; d = tempfile.mkdtemp(); home = os.path.join(d, "home"); xdg = os.path.join(d, "xdg"); os.makedirs(home); os.makedirs(xdg)
    me = os.path.abspath(__file__)
    def run_main(args, home_=None, xdg_=None, env_=None):
        env = dict(os.environ, HOME=home_ or home, XDG_DATA_HOME=xdg_ or xdg, PYTHONDONTWRITEBYTECODE="1", **(env_ or {}))
        return subprocess.run([sys.executable, "-B", me] + args, capture_output=True, text=True, env=env)
    def folder_text(out):
        md = open(os.path.join(out, "HANDOFF.md"), encoding="utf-8").read()
        blobs = [open(os.path.join(out, "blobs", f), encoding="utf-8").read() for f in sorted(os.listdir(os.path.join(out, "blobs")))] if os.path.isdir(os.path.join(out, "blobs")) else []
        return md, blobs
    # ---- the introduction: the OpenCode one is the Claude one with exactly three substitutions
    back = INTRO_OC
    for a_, b_ in OC_SUBS: assert back.count(b_) == 1; back = back.replace(b_, a_)
    assert back == INTRO and len(OC_SUBS) == 2; n += 2   # the third substitution (the clause after WHAT_BLOBS) is checked below
    args = ("ses_0123456789abcdefAAAAAA", "2026-01-01 10:00", "/x/db.sqlite", False, "", 1996, ((1, 300), (301, 280)))
    plain = build_intro(False, *args)
    assert plain == INTRO.format(sid=args[0], ts=args[1], lines=1996, n=2, plan="1:300, 301:280", parents="", temp=TEMP_NOTE[(False, False)], what=WHAT_BLOBS, missing=MISSING_BLOBS); n += 1   # the Claude introduction is as it was
    for comp, imgs in itertools.product((False, True), (False, True)):   # 4 combinations of the optional clauses
        intro = build_intro(False, *args, opencode=(comp, imgs)); n += 1
        assert "Sesiunea sursă: sesiune OpenCode `ses_0123456789abcdefAAAAAA`, generat 2026-01-01 10:00." in intro and "`Read` în Claude Code, `read` în OpenCode, cu parametrii `offset` și `limit`" in intro
        assert "unealta Read" not in intro and "singur Read" not in intro and "poate fi refuzată sau trunchiată" in intro and "într-o singură citire" in intro
        for needle in ("1996 de linii", "în 2 bucăți", "`offset:limit`: 1:300, 301:280. ", "8 rânduri", "istoric", "Reguli", "Ce faci după ce l-ai citit", "Mesajele mele și ale asistentului sunt întregi", "[opencode …]", "Raționamentul modelului (text și conținut criptat) nu este inclus"):
            assert needle in intro, (comp, imgs, needle)
        assert ("textele `recent` ale compactărilor sunt în `blobs/Rnnnn.txt`" in intro) == comp and ("imaginile sunt în `blobs/Rnnnn.txt`, ca date base64, nu text, și nu sunt verificate pentru secrete" in intro) == imgs
        assert intro.count("\n") == plain.count("\n") and "/tmp/handoff-" not in intro and "/Users/" not in intro
        line = next(l for l in intro.split("\n") if l.startswith(WHAT_BLOBS)); assert line.endswith(".") and line.count("Raționamentul") == 1
        undo = intro.replace(OC_EVENTS + (OC_RECENT if comp else "") + (OC_IMAGES if imgs else "") + ".", "")
        for a_, b_ in OC_SUBS: undo = undo.replace(b_.format(sid=args[0], ts=args[1], n=2, plan="1:300, 301:280"), a_.format(sid=args[0], ts=args[1], n=2, plan="1:300, 301:280"))
        assert undo == plain, (comp, imgs)   # undoing the three changes gives the Claude introduction for the same arguments
    # ---- a session with every message type and content kind, converted by the command line of measure_oc.py
    rows = oc.synthetic_rows(); sid = "ses_0123456789abcdefAAAAAA"; uri = next(b for t_, dd, _ in rows if t_ == "assistant" for part in dd.get("content", []) if part.get("type") == "tool" for b in part["state"].get("content", []) if b.get("type") == "file")["uri"]
    db = oc.make_db(os.path.join(d, "one.sqlite"), {sid: rows}); out = os.path.join(d, "out1")
    r = run_main(["--opencode", db, out]); assert r.returncode == 0, r.stderr + r.stdout; n += 1
    c = sqlite3.connect(db); total = c.execute("select sum(length(data)) from session_message").fetchone()[0]; c.close()
    assert "original %7d chars" % total in r.stdout and "roundtrip IDENTICAL" in r.stdout and re.search(r"sanitized records: [2-9]\d* of \d+", r.stdout), r.stdout; n += 2   # printed original = sum of length(data); the token input record is counted, not failed
    md, blobs = folder_text(out); allt = md + "\n".join(blobs)
    for must in ("Salut! Fă un plan pentru fișierul „raport” – cu diacritice: ăâîșț, 日本語 și emoji 🙂.", "I will look at the files first.", "Final answer text.", "[opencode system] SYSTEM TEXT one two three", "[opencode synthetic] SYNTHETIC TEXT four five",
                 "[opencode compaction summary] COMPACTION SUMMARY: goal and decisions so far", "[opencode idle] outcome=succeeded", "[opencode model-switched]", "[opencode agent-switched]", "[opencode location-switched]", "[tool_result ERROR] tool.execution: patch verification failed: no lines",
                 "(no output; status completed)", "(no result recorded; status running)", "(exit 1, truncated)", "patchText: *** Begin Patch\n*** Update File: a.txt\n@@\n-old line\n+new line é\n*** End Patch"):
        assert must in md, must; n += 1   # whole, inline in HANDOFF.md
    assert "REASONING SECRET" not in allt and "ENCRYPTEDBLOB" not in allt and "shellID" not in allt; n += 1
    assert "iVBORw0KGgo" not in md and "data:image" not in md and not any(len(l) > 20000 for l in md.split("\n")); n += 1   # no image data in HANDOFF.md
    assert any(b.endswith("uri: " + uri + "\n") and b.startswith("[") and "[tool_result file]\nmime: image/png\nname: shots/one.png\n" in b for b in blobs); n += 1   # the uri is in a blob byte for byte (not sanitized)
    assert any("RECENT TEXT of the recent turns" in b and "[opencode compaction recent]" in b for b in blobs) and "RECENT TEXT" not in md; n += 1
    assert "[file image/png shots/one.png, %d chars]" % len(uri) in md and "[opencode compaction recent]" in md; n += 1   # the markers
    assert "API_TOKEN=abcd1234efgh5678" not in allt and "s3cretValue99xyz" not in allt and "TOKENabcdefgh12345" not in allt and "[REDACTED:" in allt and "ok API_TOKEN=[REDACTED" in allt; n += 1   # the secrets in text outputs and inputs are redacted
    head = md[:md.index("\n---\n")]
    assert "sesiune OpenCode `ses_0123456789abcdefAAAAAA`" in head and "textele `recent`" in head and "imaginile sunt în `blobs/Rnnnn.txt`" in head and "Raționamentul modelului" in head; n += 1
    assert r.stdout.rstrip().endswith('start the new session with:\n  opencode --prompt "Citește %s și urmează instrucțiunile din el."' % os.path.join(out, "HANDOFF.md")); n += 1
    ro_n = int(re.search(r"(\d+) by the read-only rule", r.stdout).group(1)); assert ro_n == 4, ro_n; n += 1   # shell ls, read, grep and the image read (a read tool); the git commit and the patches stay inline
    assert "[tool_use shell] command: git commit -m 'x'" in md and "[tool_use patch] patchText: *** Begin Patch" in md; n += 1
    # a second run is deterministic (same uuids, same bytes)
    out1b = os.path.join(d, "out1b"); r2 = run_main(["--opencode", db, out1b]); assert r2.returncode == 0 and open(os.path.join(out1b, "HANDOFF.md"), encoding="utf-8").read().replace(out1b, out) == md.replace(out1b, out); n += 1
    # a session without compactions and images: its introduction has neither clause
    plain_rows = [x for x in rows if x[0] != "compaction"]
    for part in plain_rows[6][1]["content"]:
        if part.get("type") == "tool" and part["state"].get("content"): part["state"]["content"] = [b for b in part["state"]["content"] if b.get("type") != "file"]
    db2 = oc.make_db(os.path.join(d, "two.sqlite"), {"ses_bbbbbbbbbbbbBBBBBBBB": plain_rows}); out2 = os.path.join(d, "out2")
    assert run_main(["--opencode", db2, out2]).returncode == 0; md2, bl2 = folder_text(out2); h2 = md2[:md2.index("\n---\n")]
    assert "textele `recent`" not in h2 and "imaginile sunt" not in h2 and "Raționamentul modelului" in h2 and "[opencode …]" in h2; n += 1
    # ---- session picking
    both = oc.make_db(os.path.join(d, "both.sqlite"), {"ses_aaaaaaaaaaaaAAAAAAAA": [("user", {"text": "first session"}, 5)], "ses_bbbbbbbbbbbbBBBBBBBB": [("user", {"text": "second session"}, 5)]})
    r = run_main(["--opencode", both, os.path.join(d, "o3")]); assert r.returncode != 0 and "ses_aaaaaaaaaaaaAAAAAAAA" in r.stderr and "ses_bbbbbbbbbbbbBBBBBBBB" in r.stderr and "--session" in r.stderr and "first session" not in r.stderr; n += 1
    r = run_main(["--opencode", both, os.path.join(d, "o4"), "--session", "ses_bbbbbbbbbbbbBBBBBBBB"]); assert r.returncode == 0, r.stderr; assert "second session" in folder_text(os.path.join(d, "o4"))[0] and "first session" not in folder_text(os.path.join(d, "o4"))[0]; n += 1
    r = run_main(["--opencode", both, os.path.join(d, "o5"), "--session", "ses_nope"]); assert r.returncode != 0 and "no such session" in r.stderr; n += 1
    leg = oc.make_db(os.path.join(d, "legacy.sqlite"), {}, legacy=["ses_old1"]); empty = oc.make_db(os.path.join(d, "empty.sqlite"), {})
    for dbp, extra, want in ((leg, [], "legacy"), (leg, ["--session", "ses_old1"], "legacy"), (empty, [], "no session in session_v2")):
        r = run_main(["--opencode", dbp, os.path.join(d, "o6")] + extra); assert r.returncode != 0 and want in r.stderr, (dbp, extra, r.stderr); n += 1
    for bad in ("--cut", "--with-subagents"):
        r = run_main(["--opencode", db, os.path.join(d, "o7"), bad]); assert r.returncode != 0 and bad in r.stderr and "not supported" in r.stderr; n += 1
    r = run_main(["--opencode", os.path.join(d, "nofile.sqlite"), os.path.join(d, "o8")]); assert r.returncode != 0 and "not found" in r.stderr; n += 1
    r = run_main([both, "--session", "ses_aaaaaaaaaaaaAAAAAAAA"]); assert r.returncode != 0 and "--opencode" in r.stderr; n += 1
    # ---- a parent handoff: the session's first calls read an earlier handoff
    par = os.path.join(d, "handoff-0123abcd-20260101-aaa"); os.makedirs(par)
    open(os.path.join(par, "HANDOFF.md"), "w", encoding="utf-8").write("# Handoff\nits parent text\n---\n\n----- transcript (compact) -----\n[uuid user] hi\n")
    prow = [("user", {"text": "go on"}, 9), ("assistant", {"content": [{"type": "tool", "name": "read", "state": {"status": "completed", "input": {"path": os.path.join(par, "HANDOFF.md")}, "content": [{"type": "text", "text": "1\t# Handoff"}]}}]}, 8)]
    db4 = oc.make_db(os.path.join(d, "parent.sqlite"), {"ses_cccccccccccCCCCCCCCC": prow}); out4 = os.path.join(d, "out4"); r = run_main(["--opencode", db4, out4]); assert r.returncode == 0, r.stderr; n += 1
    assert "parent handoff: " in r.stdout and os.path.exists(os.path.join(out4, "parents", "handoff-0123abcd-20260101-aaa", "HANDOFF.md")) and "## Handoff părinte" in folder_text(out4)[0]; n += 1
    # ---- --current over all sources (never the real database: --opencode-db, HOME and XDG_DATA_HOME point into this folder)
    mk = "handoff-marker-77-0123456789ab"
    def marker_session(sid_, age=3): return {sid_: [("user", {"text": "please do a handoff"}, age + 2), ("assistant", {"content": [{"type": "tool", "name": "shell", "state": {"status": "completed", "input": {"command": 'python3 x/make_context.py --current "%s"' % mk}, "content": [{"type": "text", "text": "ok"}]}}]}, age)]}
    quiet = {"ses_quietquietquiet01": [("user", {"text": "nothing here"}, 3)]}
    def cur(sessions, extra=(), legacy=None, home_=None, name="cur"):
        path = oc.make_db(os.path.join(d, "%s%d.sqlite" % (name, len(os.listdir(d)))), sessions, legacy=legacy)
        return run_main(["--current", mk, os.path.join(d, "outc%d" % len(os.listdir(d))), "--opencode-db", path] + list(extra), home_=home_), path
    r, _ = cur({**quiet, **marker_session("ses_onlyonlyonly0001")}); assert r.returncode == 0, r.stderr; assert "current session: ses_onlyonlyonly0001" in r.stdout and "roundtrip IDENTICAL" in r.stdout and "start the new session with:\n  opencode --prompt" in r.stdout; n += 1   # one match: found and built
    r, _ = cur(quiet); assert r.returncode != 0 and "0 transcripts contain the marker" in r.stderr; n += 1   # none: refused
    r, _ = cur({**marker_session("ses_twotwotwotwo0001"), **marker_session("ses_twotwotwotwo0002")}); assert r.returncode != 0 and "2 sessions contain the marker" in r.stderr and "0 Claude Code transcripts, 2 OpenCode sessions" in r.stderr; n += 1   # two in OpenCode: refused
    r, _ = cur(marker_session("ses_oldoldoldold0001", age=7200)); assert r.returncode != 0 and "0 transcripts contain the marker" in r.stderr; n += 1   # rows older than 1800 s are ignored
    pr = os.path.join(home, ".claude", "projects", "p"); os.makedirs(pr)
    open(os.path.join(pr, "cccccccc-cccc-4ccc-8ccc-cccccccccccc.jsonl"), "w").write('{"type":"user","message":{"content":"echo %s"}}\n' % mk)
    r, _ = cur(marker_session("ses_bothbothboth0001")); assert r.returncode != 0 and "2 sessions contain the marker" in r.stderr and "1 Claude Code transcripts, 1 OpenCode sessions" in r.stderr; n += 1   # one OpenCode + one Claude = two: refused
    r, _ = cur(marker_session("ses_bothbothboth0001"), ["--no-opencode"]); assert "sessions contain the marker" not in r.stderr and "OpenCode" not in r.stderr.split("\n")[0] and "current session: cccccccc-cccc-4ccc-8ccc-cccccccccccc" in r.stdout; n += 1   # --no-opencode: the Claude hit decides
    os.remove(os.path.join(pr, "cccccccc-cccc-4ccc-8ccc-cccccccccccc.jsonl"))
    r, path = cur({}, legacy=["ses_old1"]); assert r.returncode != 0 and "0 transcripts contain the marker" in r.stderr; n += 1   # a legacy-only database: zero hits
    r = run_main(["--current", mk, os.path.join(d, "outm"), "--opencode-db", os.path.join(d, "missing.sqlite")]); assert r.returncode != 0 and "0 transcripts contain the marker" in r.stderr; n += 1   # a missing file is skipped
    junk = os.path.join(d, "junk.db"); open(junk, "wb").write(b"not a database " * 200)
    r = run_main(["--current", mk, os.path.join(d, "outj"), "--opencode-db", junk]); assert r.returncode != 0 and "--no-opencode" in r.stderr and "cannot be read" in r.stderr and "Report this to the user" in r.stderr and "0 transcripts" not in r.stderr; n += 1   # fail closed, naming the cause and the option
    r = run_main(["--current", mk, os.path.join(d, "outj"), "--opencode-db", junk, "--no-opencode"]); assert r.returncode != 0 and "0 transcripts contain the marker" in r.stderr and "cannot be read" not in r.stderr; n += 1   # skipped: the Claude hits decide
    # the default path: XDG_DATA_HOME first, then ~/.local/share
    xdb = os.path.join(xdg, "opencode", "opencode.db"); os.makedirs(os.path.dirname(xdb)); oc.make_db(xdb, marker_session("ses_xdgxdgxdgxdg0001"))
    r = run_main(["--current", mk, os.path.join(d, "outx")]); assert r.returncode == 0 and "current session: ses_xdgxdgxdgxdg0001" in r.stdout, r.stderr; n += 1
    hdb = os.path.join(home, ".local", "share", "opencode", "opencode.db"); os.makedirs(os.path.dirname(hdb)); oc.make_db(hdb, marker_session("ses_homehomehome0001"))
    r = run_main(["--current", mk, os.path.join(d, "outy")]); assert r.returncode == 0 and "ses_xdgxdgxdgxdg0001" in r.stdout and "ses_homehomehome0001" not in r.stdout; n += 1   # XDG_DATA_HOME wins; the home database is not searched
    empty_xdg = os.path.join(d, "xdg2"); os.makedirs(empty_xdg)
    r = run_main(["--current", mk, os.path.join(d, "outz")], xdg_=empty_xdg); assert r.returncode != 0 and "0 transcripts contain the marker" in r.stderr; n += 1   # XDG_DATA_HOME set but empty of a database: the home one is not used
    env_home = dict(os.environ, HOME=home, PYTHONDONTWRITEBYTECODE="1"); env_home.pop("XDG_DATA_HOME", None)
    r = subprocess.run([sys.executable, "-B", me, "--current", mk, os.path.join(d, "outw")], capture_output=True, text=True, env=env_home); assert r.returncode == 0 and "ses_homehomehome0001" in r.stdout, r.stderr; n += 1   # no XDG_DATA_HOME: ~/.local/share
    # the same search in process (the function the command uses), with a temporary projects folder
    projects = os.path.join(d, "projects"); os.makedirs(os.path.join(projects, "pp"))
    cl = os.path.join(projects, "pp", "x.jsonl"); open(cl, "w").write("echo %s\n" % mk)
    dbm = oc.make_db(os.path.join(d, "inproc.sqlite"), marker_session("ses_inprocinproc0001"))
    assert find_current_all(mk, projects, 1800, 0, dbm) == ([cl], ["ses_inprocinproc0001"]) and find_current_all(mk, projects, 1800, 0, dbm, False) == ([cl], []) and find_current_all(mk, projects, 1800, 0, None) == ([cl], []); n += 3
    assert find_current_all("handoff-marker-1-000000000000", projects, 1800, 0, dbm) == ([], []); n += 1
    t0 = time.time(); assert find_current_all("handoff-marker-1-000000000000", projects, 1800, 2, dbm) == ([], []) and time.time() - t0 >= 1.5; n += 1   # the wait applies to the combined search
    n += selftest_list_opencode(d, home, xdg, run_main, folder_text, xdb, hdb, empty_xdg)
    shutil.rmtree(d, ignore_errors=True)
    return n


def selftest_list_opencode(d, home, xdg, run_main, folder_text, xdb, hdb, empty_xdg):
    """--list-opencode and the list in the error of --opencode, through the command line (TZ=UTC; HOME and XDG_DATA_HOME point into the temporary folder) -> the number of cases."""
    import sqlite3
    n = 0; T = 1700000000000; utc = {"TZ": "UTC"}
    def lst(args, db=None, env_=utc, **kw):
        return run_main(["--list-opencode"] + (["--opencode-db", db] if db else []) + list(args), env_=env_, **kw)
    last = lambda db, shown, total, match=None: "shown %d of %d sessions%s; database: %s" % (shown, total, "" if match is None else ' matching "%s"' % match, os.path.abspath(db))
    rows_ = lambda k: [("user", {"text": "PRIVATE MESSAGE TEXT"}, 5)] * k
    # 0 sessions: only the last line; 1 session: the exact lines
    db0 = oc.make_db(os.path.join(d, "l0.sqlite"), {}, meta={}); r = lst([], db0)
    assert r.returncode == 0 and r.stdout == last(db0, 0, 0) + "\n" and r.stderr == "", (r.stdout, r.stderr); n += 1
    r = lst(["--match", "x"], db0); assert r.returncode == 0 and r.stdout == last(db0, 0, 0, "x") + "\n"; n += 1
    t1 = "Council B g1 (openai/gpt-6-astra)"
    db1 = oc.make_db(os.path.join(d, "l1.sqlite"), {"ses_ec6becfd3ffeAAAAAA": rows_(2)}, meta={"ses_ec6becfd3ffeAAAAAA": {"title": t1, "directory": home + "/proj/x", "time_updated": T}}); r = lst([], db1)
    assert r.returncode == 0 and r.stdout == "ses_ec6becfd3ffeAAAAAA | 2023-11-14 22:13 | 2 msgs | ~/proj/x | %s\n%s\n" % (t1, last(db1, 1, 1)), r.stdout; n += 1
    assert "PRIVATE MESSAGE TEXT" not in r.stdout; n += 1
    r = lst([], db1, env_={"TZ": "JST-9"}); assert r.stdout.startswith("ses_ec6becfd3ffeAAAAAA | 2023-11-15 07:13 | "); n += 1   # local time
    # the home folder itself, a subfolder, a prefix that is not a path boundary, no directory
    dbh = oc.make_db(os.path.join(d, "lh.sqlite"), {"ses_h1": rows_(1), "ses_h2": rows_(1), "ses_h3": rows_(1), "ses_h4": rows_(1)}, meta={
        "ses_h1": {"title": "a", "directory": home, "time_updated": T + 4}, "ses_h2": {"title": "b", "directory": home + "/sub/dir", "time_updated": T + 3},
        "ses_h3": {"title": "c", "directory": home + "2/sub", "time_updated": T + 2}, "ses_h4": {"title": "d", "directory": "", "time_updated": T + 1}}); r = lst([], dbh)
    assert r.stdout.split("\n")[:4] == ["ses_h1 | 2023-11-14 22:13 | 1 msgs | ~ | a", "ses_h2 | 2023-11-14 22:13 | 1 msgs | ~/sub/dir | b", "ses_h3 | 2023-11-14 22:13 | 1 msgs | %s2/sub | c" % home, "ses_h4 | 2023-11-14 22:13 | 1 msgs | - | d"], r.stdout; n += 1
    # more sessions than the limit, --limit, bad limits
    db20 = oc.make_db(os.path.join(d, "l20.sqlite"), {"ses_%02d" % i: rows_(1) for i in range(20)}, meta={"ses_%02d" % i: {"title": "t%d" % i, "directory": "/w", "time_updated": T + i * 60000} for i in range(20)})
    r = lst([], db20); L = r.stdout.rstrip("\n").split("\n")
    assert r.returncode == 0 and len(L) == 16 and L[0].startswith("ses_19 | ") and L[14].startswith("ses_05 | ") and L[15] == last(db20, 15, 20), r.stdout; n += 1
    r = lst(["--limit", "2"], db20); assert r.stdout.rstrip("\n").split("\n")[1:] == ["ses_18 | 2023-11-14 22:31 | 1 msgs | /w | t18", last(db20, 2, 20)] and r.stdout.startswith("ses_19 | 2023-11-14 22:32 | "); n += 1
    r = lst(["--limit", "99"], db20); assert len(r.stdout.rstrip("\n").split("\n")) == 21 and r.stdout.rstrip("\n").endswith(last(db20, 20, 20)); n += 1
    for bad in ("0", "abc", "-1", "1.5", "", "2x", "+3"):
        r = lst(["--limit", bad], db20); assert r.returncode == 2 and "--limit" in r.stderr and r.stdout == "", (bad, r.returncode, r.stderr); n += 1
    # --match: id, title, directory (raw and ~), case, diacritics, no hit, applied before the limit
    dm = oc.make_db(os.path.join(d, "lm.sqlite"), {k: rows_(1) for k in ("ses_Alpha0001", "ses_beta00002", "ses_gamma0003", "ses_delta0004")}, meta={
        "ses_Alpha0001": {"title": t1, "directory": home + "/work/alpha", "time_updated": T + 3}, "ses_beta00002": {"title": t1 + " (fork #1)", "directory": "/srv/other", "time_updated": T + 2},
        "ses_gamma0003": {"title": "Șantier în București", "directory": home + "/ședință", "time_updated": T + 1}, "ses_delta0004": {"title": "x", "directory": "/opt/Data", "time_updated": T}})
    def ids_of(r): return [l.split(" | ")[0] for l in r.stdout.rstrip("\n").split("\n")[:-1]]
    for m, want in (("alpha0001", ["ses_Alpha0001"]), ("SES_BETA", ["ses_beta00002"]), ("council b g1", ["ses_Alpha0001", "ses_beta00002"]), ("COUNCIL B G1", ["ses_Alpha0001", "ses_beta00002"]), ("(fork #1)", ["ses_beta00002"]),
                    ("/srv/other", ["ses_beta00002"]), ("~/work", ["ses_Alpha0001"]), (home + "/work", ["ses_Alpha0001"]), ("/opt/data", ["ses_delta0004"]), ("ȘANTIER ÎN", ["ses_gamma0003"]), ("~/ședință", ["ses_gamma0003"]), ("ȘEDINȚĂ", ["ses_gamma0003"]),
                    ("nothing like this", []), ("", ["ses_Alpha0001", "ses_beta00002", "ses_gamma0003", "ses_delta0004"])):
        r = lst(["--match", m], dm); assert r.returncode == 0 and ids_of(r) == want and r.stdout.rstrip("\n").endswith(last(dm, len(want), len(want), m)), (m, r.stdout); n += 1
    r = lst(["--match", "nothing like this"], dm); assert r.stdout == last(dm, 0, 0, "nothing like this") + "\n"; n += 1
    r = lst(["--match", "council", "--limit", "1"], dm); assert ids_of(r) == ["ses_Alpha0001"] and r.stdout.rstrip("\n").endswith(last(dm, 1, 2, "council")); n += 1
    # ordering by time_updated, not time_created (make_db creates in dict order); the tie on the same time goes by id
    do = oc.make_db(os.path.join(d, "lo.sqlite"), {"ses_a1": rows_(1), "ses_a2": rows_(1), "ses_a3": rows_(1)}, meta={"ses_a1": {"title": "1", "time_updated": T + 10}, "ses_a2": {"title": "2", "time_updated": T + 5}, "ses_a3": {"title": "3", "time_updated": T + 10}})
    assert ids_of(lst([], do)) == ["ses_a1", "ses_a3", "ses_a2"]; n += 1
    # a title over 80 characters, with a line break, a fork, empty
    dt = oc.make_db(os.path.join(d, "lt.sqlite"), {"ses_t1": rows_(1), "ses_t2": rows_(1), "ses_t3": rows_(1), "ses_t4": rows_(1)}, meta={"ses_t1": {"title": "B" * 120, "time_updated": T + 4}, "ses_t2": {"title": "two\n\nlines\there", "time_updated": T + 3},
        "ses_t3": {"title": "Council B g1 (openai/gpt-6-astra) (fork #1)", "time_updated": T + 2}, "ses_t4": {"title": "", "time_updated": T + 1}}); r = lst([], dt); L = r.stdout.split("\n")
    assert L[0].endswith(" | - | " + "B" * 79 + "…") and len(L[0].split(" | ")[-1]) == 80 and L[1].endswith(" | two lines here") and L[2].endswith(" | Council B g1 (openai/gpt-6-astra) (fork #1)") and L[3].endswith(" | (untitled)") and len(L) == 6, r.stdout; n += 1
    # a minimal schema: no title, directory or time_updated
    dmin = oc.make_db(os.path.join(d, "lmin.sqlite"), {"ses_m1": rows_(3)}); r = lst([], dmin)
    assert r.returncode == 0 and re.fullmatch(r"ses_m1 \| \d{4}-\d\d-\d\d \d\d:\d\d \| 3 msgs \| - \| \(untitled\)\n" + re.escape(last(dmin, 1, 1)) + "\n", r.stdout), r.stdout; n += 1
    # the message content is never needed: garbage in session_message still lists with the counts
    dg = oc.make_db(os.path.join(d, "lg.sqlite"), {"ses_g1": rows_(3), "ses_g2": rows_(1)}, meta={"ses_g1": {"title": "g1", "time_updated": T + 1}, "ses_g2": {"title": "g2", "time_updated": T}})
    c = sqlite3.connect(dg); c.execute("update session_message set data = ?", (b"\xff\x00 not json {{",)); c.commit(); c.close(); r = lst([], dg)
    assert r.returncode == 0 and r.stdout.split("\n")[0].endswith("3 msgs | - | g1") and r.stdout.split("\n")[1].endswith("1 msgs | - | g2"); n += 1
    # errors: a missing file, junk, an unreadable file, legacy only (both shapes), nothing usable
    miss = os.path.join(d, "no-such.sqlite"); r = lst([], miss); assert r.returncode == 1 and r.stdout == "" and "OpenCode database not found: %s" % miss in r.stderr; n += 1
    junk = os.path.join(d, "junk-list.db"); open(junk, "wb").write(b"not a database " * 200); r = lst([], junk); assert r.returncode == 1 and r.stdout == "" and "cannot be read" in r.stderr; n += 1
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        locked = os.path.join(d, "locked.db"); open(locked, "wb").write(open(db1, "rb").read()); os.chmod(locked, 0)
        try: r = lst([], locked); assert r.returncode == 1 and "cannot be read" in r.stderr and r.stdout == "", r.stderr; n += 1
        finally: os.chmod(locked, 0o600)
    legv = oc.make_db(os.path.join(d, "ll1.sqlite"), {}, legacy=["ses_old1"]); legb = os.path.join(d, "ll2.sqlite"); c = sqlite3.connect(legb); c.execute("create table session(id TEXT)"); c.execute("insert into session values ('ses_old1')"); c.commit(); c.close()
    for path in (legv, legb):
        r = lst([], path); assert r.returncode == 1 and r.stdout == "" and "legacy OpenCode sessions are not supported" in r.stderr, (path, r.stderr); n += 1
        r = lst(["--match", "x"], path); assert r.returncode == 1 and "legacy OpenCode sessions are not supported" in r.stderr; n += 1
    oth = os.path.join(d, "other.sqlite"); c = sqlite3.connect(oth); c.execute("create table unrelated(x INT)"); c.commit(); c.close()
    r = lst([], oth); assert r.returncode == 1 and "no session_v2 table in this database" in r.stderr; n += 1
    # the default database: XDG_DATA_HOME, else ~/.local/share; ~ in the path options; an empty XDG folder is an error
    r = lst([]); assert r.returncode == 0 and r.stdout.split("\n")[0].startswith("ses_xdgxdgxdgxdg0001 | ") and r.stdout.rstrip("\n").endswith("sessions; database: %s" % xdb), r.stderr + r.stdout; n += 1
    env_home = dict(os.environ, HOME=home, PYTHONDONTWRITEBYTECODE="1", TZ="UTC"); env_home.pop("XDG_DATA_HOME", None)
    r = subprocess.run([sys.executable, "-B", os.path.abspath(__file__), "--list-opencode"], capture_output=True, text=True, env=env_home); assert r.returncode == 0 and r.stdout.startswith("ses_homehomehome0001 | ") and r.stdout.rstrip("\n").endswith("database: %s" % hdb), r.stderr; n += 1
    r = lst([], xdg_=empty_xdg); assert r.returncode == 1 and "OpenCode database not found: %s" % os.path.join(empty_xdg, "opencode", "opencode.db") in r.stderr; n += 1
    r = run_main(["--list-opencode", "--opencode-db", "~/.local/share/opencode/opencode.db"], env_=utc)
    assert r.returncode == 0 and r.stdout.startswith("ses_homehomehome0001 | ") and r.stdout.rstrip("\n").endswith("database: %s" % hdb), r.stderr; n += 1   # a quoted ~ is expanded by the script
    o9 = os.path.join(d, "o9"); r = run_main(["--opencode", "~/.local/share/opencode/opencode.db", "--session", "ses_homehomehome0001", o9]); assert r.returncode == 0 and "roundtrip IDENTICAL" in r.stdout and os.path.exists(os.path.join(o9, "HANDOFF.md")), r.stderr; n += 1
    # option misuse
    for args in (["--limit", "3"], ["--match", "x"], ["--opencode", db1, "--match", "x"], ["--opencode", db1, "--limit", "2"], ["--current", "handoff-marker-1-aaaaaaaaaaaa", "--limit", "2"], ["--limit", "3", "--match", "x"]):
        r = run_main(args); assert r.returncode == 1 and "work only with --list-opencode" in r.stderr and r.stdout == "", (args, r.stderr); n += 1
    for extra in (["outdir"], ["--session", "ses_h1"], ["--cut"], ["--keep"], ["--no-opencode"], ["--opencode", db1], ["--current", "x"], ["--keep-work"]):
        r = lst(extra, db1); assert r.returncode == 1 and "--list-opencode takes only" in r.stderr and r.stdout == "", (extra, r.stderr); n += 1
    r = run_main(["--list-opencode", "--limit"]); assert r.returncode == 1 and "--limit needs a value" in r.stderr; n += 1
    # part (e): --opencode DB with several sessions and no --session lists the newest, titled
    both = oc.make_db(os.path.join(d, "both-meta.sqlite"), {"ses_aaaaaaaaaaaaAAAAAAAA": [("user", {"text": "first session"}, 5)], "ses_bbbbbbbbbbbbBBBBBBBB": [("user", {"text": "second session"}, 5)]},
                      meta={"ses_aaaaaaaaaaaaAAAAAAAA": {"title": "Alpha job", "directory": "/w/a", "time_updated": T + 60000}, "ses_bbbbbbbbbbbbBBBBBBBB": {"title": "Beta job (fork #1)", "directory": "/w/b", "time_updated": T}})
    r = run_main(["--opencode", both, os.path.join(d, "oe1")], env_=utc); E = r.stderr.rstrip("\n").split("\n")
    assert r.returncode != 0 and E[0] == "2 sessions in this database; pick one with --session ID (newest first):" and E[1] == "ses_aaaaaaaaaaaaAAAAAAAA | 2023-11-14 22:14 | 1 msgs | /w/a | Alpha job" and E[2] == "ses_bbbbbbbbbbbbBBBBBBBB | 2023-11-14 22:13 | 1 msgs | /w/b | Beta job (fork #1)" \
        and E[3] == "shown 2 of 2 sessions" and len(E) == 4 and "first session" not in r.stderr, r.stderr; n += 1
    d20 = oc.make_db(os.path.join(d, "e20.sqlite"), {"ses_%02d" % i: rows_(1) for i in range(20)}, meta={"ses_%02d" % i: {"title": "t%d" % i, "time_updated": T + i} for i in range(20)})
    r = run_main(["--opencode", d20, os.path.join(d, "oe2")], env_=utc); E = r.stderr.rstrip("\n").split("\n")
    assert r.returncode != 0 and E[0].startswith("20 sessions in this database") and len(E) == 17 and E[1].startswith("ses_19 | ") and E[15].startswith("ses_05 | ") and E[16] == "shown 15 of 20 sessions"; n += 1
    return n


def selftest():
    import itertools
    d = tempfile.mkdtemp(); n = 0
    def session(calls):   # a transcript whose assistant makes the given (tool name, file_path) calls
        p = os.path.join(d, "s%d.jsonl" % len(os.listdir(d)))
        with open(p, "w", encoding="utf-8") as f:
            for i, (name, fp) in enumerate(calls):
                f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t%d" % i, "name": name, "input": {"file_path": fp}}]}}) + "\n")
        return p
    h1 = os.path.join(d, "handoff-0123abcd-20260101-aaa.md"); h2 = os.path.join(d, "handoff-4567cdef-20260102-bbb", "HANDOFF.md")
    other = os.path.join(d, "notes.md")
    for cases, expect in [([("Read", h1)], [h1]), ([("Read", other), ("Read", h1)], [h1]), ([("Read", h1), ("Read", h2)], [h1, h2]), ([("Read", h1), ("Read", h1)], [h1]),
                          ([("Edit", h1)], []), ([("Read", other)], []), ([("Bash", other)] * 4 + [("Read", h1)], []), ([("Read", other)] * 3 + [("Read", h1)], [h1]), ([], [])]:
        got = find_parents(session(cases)); assert got == expect, (cases, got, expect); n += 1
    # parents made by an OLD version: absolute paths in the introduction, a transcript body that must stay byte for byte
    old_dir = os.path.dirname(h2); body = "\n---\n\nMARKER-PAIR: ⦇⦈\nheader mentions `/tmp/not-a-handoff/blobs/x` which is body text\n----- transcript (compact) -----\n[uuid user] hi\n"
    old_intro = ("# Handoff\nblobs are in `%s/blobs/Rnnnn.txt`\nthe temporary files are in `%s`\nits parent is `%s/parents/handoff-1111aaaa-20260101-ccc/HANDOFF.md` (original: `/tmp/other/HANDOFF.md`)\n" % (old_dir, old_dir, old_dir))
    os.makedirs(os.path.join(old_dir, "blobs")); os.makedirs(os.path.join(old_dir, "parents"))
    open(h2, "w", encoding="utf-8").write(old_intro + body); open(os.path.join(old_dir, "blobs", "R0000.txt"), "w").write("blob"); open(os.path.join(old_dir, "parents", "g.md"), "w").write("grand")
    open(h1, "w", encoding="utf-8").write("# Handoff\nits file is `%s`, its parents `%s/handoff-0123abcd-20260101-aaa-parents/old.md`\n%s" % (h1, d, body))
    os.makedirs(h1[:-3] + "-parents"); open(os.path.join(h1[:-3] + "-parents", "old.md"), "w").write("old")
    new_style = os.path.join(d, "handoff-9999eeee-20260105-new"); os.makedirs(new_style); ns_text = "# Handoff\nblobs are in `blobs/Rnnnn.txt`\n" + body; open(os.path.join(new_style, "HANDOFF.md"), "w", encoding="utf-8").write(ns_text)
    dest = os.path.join(d, "dest"); lines_before = (open(h2, encoding="utf-8").read().count("\n"), open(h1, encoding="utf-8").read().count("\n"))
    cp = copy_parents([h1, h2, os.path.join(d, "handoff-89abcdef-20260103-missing.md"), os.path.join(new_style, "HANDOFF.md")], dest); n += 1
    t2 = open(cp[1][1], encoding="utf-8").read()   # folder parent: intro relative, body identical, same number of lines, blobs and its own parents copied, original untouched
    assert old_dir not in t2.split("\n---\n")[0] and "`blobs/Rnnnn.txt`" in t2 and "`acest folder`" in t2 and "parents/handoff-1111aaaa-20260101-ccc/HANDOFF.md" in t2
    assert t2.split("\n---\n", 1)[1] == body.split("\n---\n", 1)[1] and t2.count("\n") == lines_before[0] and old_dir in open(h2, encoding="utf-8").read()
    assert open(os.path.join(os.path.dirname(cp[1][1]), "blobs", "R0000.txt")).read() == "blob" and os.path.exists(os.path.join(os.path.dirname(cp[1][1]), "parents", "g.md"))
    t1 = open(cp[0][1], encoding="utf-8").read()   # single .md parent: its own path and the folder of its -parents become relative
    assert h1 not in t1 and "`acest fișier`" in t1 and "handoff-0123abcd-20260101-aaa-parents/old.md" in t1 and d not in t1.split("\n---\n")[0] and t1.count("\n") == lines_before[1]
    assert t1.split("\n---\n", 1)[1] == body.split("\n---\n", 1)[1] and os.path.exists(os.path.join(dest, "handoff-0123abcd-20260101-aaa-parents", "old.md"))
    assert cp[2][1] is None and cp[2][2] is False
    assert open(cp[3][1], encoding="utf-8").read() == ns_text; n += 4   # a parent that is already relative is copied byte for byte
    para = parents_paragraph(cp, dest)
    assert "nu mai exista" in para and os.path.relpath(cp[0][1], dest) in para and os.path.relpath(cp[1][1], dest) in para and d not in para and "original" not in para and para.endswith("\n\n") and parents_paragraph([], dest) == ""
    copy_parents([h1], dest); n += 2   # a second copy into the same place changes nothing and does not fail
    assert relativize_intro(cp[0][1], os.path.dirname(h1), h1) is False and relativize_intro(cp[1][1], old_dir) is False; n += 1   # idempotent
    # --current: exactly the transcript that holds the marker, among recent ones
    pr = tempfile.mkdtemp(); os.makedirs(os.path.join(pr, "projA")); os.makedirs(os.path.join(pr, "projB"))
    def tr(proj, name, text, age=0):
        q = os.path.join(pr, proj, name + ".jsonl"); open(q, "w", encoding="utf-8").write(text)
        if age: os.utime(q, (time.time() - age, time.time() - age))
        return q
    a1 = tr("projA", "aaa", '{"x":"nothing here"}\n'); b1 = tr("projB", "bbb", '{"cmd":"echo handoff-marker-42-7"}\n'); tr("projA", "old", '{"cmd":"echo handoff-marker-42-7"}\n', age=7200)
    assert find_current("handoff-marker-42-7", pr, 1800, 0) == [b1]; assert find_current("handoff-marker-9-9", pr, 1800, 0) == []; n += 2   # an old transcript with the marker is ignored
    c1 = tr("projA", "ccc", 'also handoff-marker-42-7 \u00e9\n'); assert find_current("handoff-marker-42-7", pr, 1800, 0) == sorted([b1, c1]); n += 1   # two matches: the caller refuses
    t0 = time.time(); assert find_current("handoff-marker-1-1", pr, 1800, 2) == [] and time.time() - t0 >= 1.5; n += 1   # it waits before giving up
    import io, contextlib
    marks = set()
    for _ in range(50):
        buf = io.StringIO(); sys.argv = ["make_context.py", "--marker"]
        with contextlib.redirect_stdout(buf): main()
        marks.add(buf.getvalue().strip())
    assert len(marks) == 50 and all(re.fullmatch(r"handoff-marker-\d+-[0-9a-f]{12}", m) for m in marks); n += 1   # unique, and in the form the skill expects
    # reading plan: contiguous, complete, within budget, within max_lines; the intro carries it
    import random
    rnd = random.Random(5)
    for trial in range(40):
        ls = ["x" * rnd.choice((0, 5, 80, 200, 1500, 9000, 45000)) for _ in range(rnd.randint(0, 120))]
        plan = reading_plan(ls)
        pos = 1
        for off, lim in plan:
            assert off == pos and lim >= 1; pos += lim
            chars = sum(len(l) + 9 for l in ls[off - 1:off - 1 + lim])
            assert lim == 1 or (chars <= READ_BUDGET and lim <= 400), (trial, off, lim, chars)
        assert pos == len(ls) + 1; n += 1
    assert reading_plan([]) == []
    # the introduction: every mode x durable or temporary; no absolute path of the handoff itself
    for cut, kept in ((False, False), (False, True), (True, False), (True, True)):
        intro = build_intro(cut, "abcd1234", "2026-01-01 10:00", "/home/u/.claude/projects/p/s.jsonl", kept, "", 1996, ((1, 300), (301, 280))); n += 1
        for needle in ("1996 de linii", "în 2 bucăți", "1:300, 301:280", "relative la folderul", "Nu repeta nimic", "maximum 8 rânduri", "---"): assert needle in intro, needle
        assert "/tmp/" not in intro.replace("(în /tmp)", "") and ("durabil" in intro) == kept and ("blobs/Rnnnn.txt" in intro) == (not cut) and ("/home/u/.claude/projects/p/s.jsonl" in intro) == cut, (cut, kept)
    # skill_meta_uuids: only a record the harness injected (isMeta true) that starts with the skill prefix; what a user typed, or an assistant wrote, with the same words is not collected
    P = cc.SKILL_PREFIX
    def rec(type_, uuid, content, **kw): return json.dumps(dict({"type": type_, "uuid": uuid, "message": {"content": content}}, **kw))
    jl = [rec("user", "u-str", P + "/s/a\n\n# a", isMeta=True), rec("user", "u-list", [{"type": "text", "text": P + "/s/b\n"}, {"type": "text", "text": "x"}], isMeta=True),
             rec("user", "u-typed", P + "/s/c"), rec("user", "u-false", P + "/s/c", isMeta=False), rec("user", "u-noprefix", "hello", isMeta=True), rec("user", "u-second", [{"type": "text", "text": "hi"}, {"type": "text", "text": P + "/s/d"}], isMeta=True),
             rec("assistant", "a-prefix", P + "/s/e", isMeta=True), rec("user", "u-toolres", [{"type": "tool_result", "content": P + "/s/f"}], isMeta=True), "not json {", "[1, 2]", json.dumps({"type": "user", "isMeta": True, "message": {"content": P + "/s/g"}})]
    sp = os.path.join(d, "skills.jsonl"); open(sp, "w", encoding="utf-8").write("\n".join(jl) + "\n")
    assert skill_meta_uuids(sp) == frozenset(["u-str", "u-list"]), skill_meta_uuids(sp); n += 1
    # the introduction: the skill clause only in blob mode and only with skills; it is short; the contract phrases are there in every case
    for cut, kept, skills in itertools.product((False, True), (False, True), (False, True)):
        intro = build_intro(cut, "abcd1234", "2026-01-01 10:00", "/home/u/.claude/projects/p/s.jsonl", kept, "", 1996, ((1, 300), (301, 280)), skills); n += 1
        for needle in ("8 rânduri", "istoric", "offset:limit", "Reguli", "Ce faci după ce l-ai citit", "Mesajele mele și ale asistentului sunt întregi"): assert needle in intro, needle
        assert (SKILL_CLAUSE in intro) == (skills and not cut) and len(SKILL_CLAUSE) <= 150, (cut, kept, skills)
        plain = build_intro(cut, "abcd1234", "2026-01-01 10:00", "/home/u/.claude/projects/p/s.jsonl", kept, "", 1996, ((1, 300), (301, 280)))   # the clause stays on the line of WHAT_BLOBS: same number of lines
        assert intro == (plain.replace(WHAT_BLOBS, WHAT_BLOBS + " " + SKILL_CLAUSE) if skills and not cut else plain) and intro.count("\n") == plain.count("\n"), (cut, kept, skills)
    # notice_uuids_of: only a record the harness wrote for a finished background task (type user, origin.kind task-notification, content starting with the tag); the same words typed by the user, or from another session, are not collected
    T = cc.NOTE_PREFIX + "\n<summary>x</summary>"
    def nrec(type_, uuid, content, origin="task-notification", **kw):   # origin: a kind (str) is wrapped as {"kind": ...}; a dict is used as it is; None leaves it out; raw=... puts a non-dict origin
        o = dict({"type": type_, "message": {"content": content}}, **kw)
        if uuid is not None: o["uuid"] = uuid
        if "raw" in kw: o["origin"] = o.pop("raw")
        elif origin is not None: o["origin"] = origin if isinstance(origin, dict) else {"kind": origin}
        return json.dumps(o)
    jl = [nrec("user", "n-str", T), nrec("user", "n-list", [{"type": "text", "text": T}, {"type": "text", "text": "x"}]), nrec("user", "n-human", T, "human"), nrec("user", "n-peer", T, "peer"), nrec("user", "n-none", T, None),
          nrec("user", "n-strorigin", T, None, raw="task-notification"), nrec("user", "n-dict-nokind", T, {"producer": "x"}), nrec("assistant", "n-assistant", T),
          nrec("user", "n-noprefix", "hello"), nrec("user", "n-late", [{"type": "text", "text": "hi"}, {"type": "text", "text": T}]), nrec("user", "n-toolres", [{"type": "tool_result", "content": T}]),
          nrec("user", None, T), nrec("user", "n-skill", P + "/s/a", isMeta=True), nrec("user", "n-meta", T, isMeta=True), "not json {", "[1, 2]"]
    np_ = os.path.join(d, "notices.jsonl"); open(np_, "w", encoding="utf-8").write("\n".join(jl) + "\n")
    assert notice_uuids_of(np_) == frozenset(["n-str", "n-list", "n-meta"]), notice_uuids_of(np_); n += 1
    # the introduction: the notification clause only in blob mode and only when notifications were moved; it is short; it follows the skill clause on the same line (16 combinations: cut x kept x skills x notices)
    assert len(NOTICE_CLAUSE) <= 150 and "peer" not in NOTICE_CLAUSE.lower() and "rezumat" not in NOTICE_CLAUSE
    for cut, kept, skills, notices in itertools.product((False, True), (False, True), (False, True), (False, True)):
        args = ("abcd1234", "2026-01-01 10:00", "/home/u/.claude/projects/p/s.jsonl", kept, "", 1996, ((1, 300), (301, 280)))
        intro = build_intro(cut, *args, skills, notices); plain = build_intro(cut, *args); n += 1
        for needle in ("8 rânduri", "istoric", "offset:limit", "Reguli", "Ce faci după ce l-ai citit", "Mesajele mele și ale asistentului sunt întregi"): assert needle in intro, needle
        assert (NOTICE_CLAUSE in intro) == (notices and not cut) and intro.count("\n") == plain.count("\n"), (cut, kept, skills, notices)
        if notices and not cut: assert WHAT_BLOBS + (" " + SKILL_CLAUSE if skills else "") + " " + NOTICE_CLAUSE in intro
        else: assert intro == build_intro(cut, *args, skills)
    # sdk_uuids_of: only a record the session marks promptSource sdk (exactly), except the first and the last of them in file order
    def srec(uuid, content="p", src="sdk", type_="user", **kw):   # src None leaves promptSource out; uuid None leaves the uuid out
        o = dict({"type": type_, "message": {"content": content}}, **kw)
        if uuid is not None: o["uuid"] = uuid
        if src is not None: o["promptSource"] = src
        return json.dumps(o)
    def sdk_of(lines, name="sdk.jsonl"):
        sp_ = os.path.join(d, name); open(sp_, "w", encoding="utf-8").write("\n".join(lines) + "\n"); return sdk_uuids_of(sp_)
    for count, want in ((0, 0), (1, 0), (2, 0), (3, 1), (5, 3)):   # the first and the last stay out
        got = sdk_of([srec("s%d" % i) for i in range(count)]); n += 1
        assert len(got) == want and got == frozenset("s%d" % i for i in range(1, count - 1)), (count, got)
    noise = [srec("x-typed", src="typed"), srec("x-queued", src="queued"), srec("x-accepted", src="suggestion_accepted"), srec("x-absent", src=None), srec("x-upper", src="SDK"), srec("x-side", isSidechain=True), srec("x-meta", isMeta=True),
             srec("x-compact", isCompactSummary=True), srec("x-peer", origin={"kind": "peer"}), srec("x-note", origin={"kind": "task-notification"}), srec("x-toolres", [{"type": "tool_result", "content": "r"}]),
             srec("x-toolres2", [{"type": "text", "text": "t"}, {"type": "tool_result", "content": "r"}]), srec("x-assistant", type_="assistant"), srec(7), srec(None), "not json {", "[1, 2]", "null"]
    edge = [srec("e-first"), srec("m-str", "text"), srec("m-list", [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]), srec("m-late", [{"type": "image", "source": {}}, {"type": "text", "text": "a"}]),
            srec("m-originless", origin={"producer": "x"}), srec("m-falsy", isSidechain=False, isMeta=False, isCompactSummary=False), srec("e-last")]
    got = sdk_of(noise + edge[:1] + noise + edge[1:]); n += 1
    assert got == frozenset(["m-str", "m-list", "m-late", "m-originless", "m-falsy"]), got   # none of the noise is collected, not even as the first or the last
    assert sdk_of([srec("e-first", "short")] + [srec("m-%d" % i, "q" * 400) for i in range(2)] + [srec("e-last", "q" * 9000)], "sdk2.jsonl") == frozenset(["m-0", "m-1"]); n += 1   # a short first and a long last still count as first and last
    assert sdk_of([srec("a"), srec("x-skip", src="typed"), srec("b"), srec("c")], "sdk3.jsonl") == frozenset(["b"]); n += 1   # other records between them do not count
    # the introduction: the SDK clause and its answers sentence only in blob mode and only when a prompt was moved; short; on the line of WHAT_BLOBS after the notice clause (32 combinations: cut x kept x skills x notices x sdk)
    assert len(SDK_CLAUSE) <= 150 and len(SDK_ANSWERS) <= 150 and not any(w in t_ for t_ in (SDK_CLAUSE, SDK_ANSWERS) for w in ("nu tastat", "doar", "nu scris de mine"))
    assert SDK_CLAUSE == "Un marcaj stă și pentru un prompt marcat de sesiune ca trimis prin SDK (claude -p); textul întreg este în `blobs/Rnnnn.txt`."
    sdk_cases = 0
    for cut, kept, skills, notices, sdks in itertools.product((False, True), (False, True), (False, True), (False, True), (False, True)):
        args = ("abcd1234", "2026-01-01 10:00", "/home/u/.claude/projects/p/s.jsonl", kept, "", 1996, ((1, 300), (301, 280)))
        intro = build_intro(cut, *args, skills, notices, sdks=sdks); before = build_intro(cut, *args, skills, notices); n += 1; sdk_cases += 1
        for needle in ("8 rânduri", "istoric", "offset:limit", "Reguli", "Ce faci după ce l-ai citit", "Mesajele mele și ale asistentului sunt întregi"): assert needle in intro, needle
        assert ((SDK_CLAUSE in intro) and (SDK_ANSWERS in intro)) == (sdks and not cut) and (SDK_CLAUSE in intro) == (SDK_ANSWERS in intro) and intro.count("\n") == before.count("\n"), (cut, kept, skills, notices, sdks)
        if sdks and not cut: assert WHAT_BLOBS + (" " + SKILL_CLAUSE if skills else "") + (" " + NOTICE_CLAUSE if notices else "") + " " + SDK_CLAUSE + " " + SDK_ANSWERS in intro and intro.replace(" " + SDK_CLAUSE + " " + SDK_ANSWERS, "") == before
        else: assert intro == before
    assert sdk_cases == 32
    assert build_intro(False, "abcd1234", "2026-01-01 10:00", "/x/s.jsonl", False, "", 1996, ((1, 300),), sdks=False) == build_intro(False, "abcd1234", "2026-01-01 10:00", "/x/s.jsonl", False, "", 1996, ((1, 300),)); n += 1   # default: the old introduction
    n += selftest_opencode()
    print("make_context selftest OK: %d cases (an OpenCode session: the introduction as the Claude one with three substitutions and its four clause combinations, a session with every message type and content kind converted from a database built here, session picking, legacy, a parent handoff, --current over all sources and its fail-closed and skip cases; --list-opencode and the titled list in the error of --opencode; relative paths: an old folder parent, an old single-file parent, a parent already relative, a missing parent, idempotence; reading plan: 40 random files; "
          "the introduction in 4 modes and the skill clause in 8; collecting the uuids of injected skill records; collecting the uuids of background-task notifications (15 records) and the introduction with their clause in 16 combinations; collecting the uuids of SDK prompts (the first and the last left out; 5 counts, 18 records of noise, list content, a short first and a long last) and the introduction with their two sentences in 32 combinations; parent detection: 9 transcripts; finding the current session by a marker: one match, none, two, an old file, the wait)" % n)


if "--selftest" in sys.argv: selftest()
else: main()
