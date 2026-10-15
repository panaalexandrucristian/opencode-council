#!/usr/bin/env python3
"""prepare_all.py SESSION.jsonl --cwd DIR --out DIR [--with-subagents]   (otherwise the options of handoff-verify's prepare.py)

prepare.py builds the chunks for VERIFICATION: it leaves out what a verifier must not count as source (the skill's own calls and results, the 'runs' that follow a note, the note's own edits). For a start context that continues the work those exclusions delete real conversation: every turn in which the skill's files were read, run or edited disappears with its answer. This wrapper switches the two exclusion functions off in this process only and then runs prepare.py unchanged (sanitization and chunking stay). It changes no file of the skill.
prepare.py appends the transcripts of the session's subagents after the main conversation, so the END of the chunks is then subagent work, not the last thing the main conversation did, and their text
(for example the payload of a batch they sent to a tool) is mostly noise for whoever continues. What a subagent found comes back to the main conversation as its result. So they are left out unless --with-subagents."""
import glob, os, sys
def _hv_scripts():
    """handoff-verify's scripts: HV_SCRIPTS, else the sibling skill of this one (the plugin and the repo have both under skills/), else the newest installed copy of the jev plugin."""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.environ.get("HV_SCRIPTS"), os.path.join(here, "..", "..", "handoff-verify", "scripts")]
    cands += sorted(glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/jev/*/skills/handoff-verify/scripts")), reverse=True)
    for c in cands:
        if c and os.path.exists(os.path.join(c, "prepare.py")): return os.path.abspath(c)
    sys.exit("handoff-verify's scripts (prepare.py) not found: set HV_SCRIPTS to their folder")
sys.path.insert(0, _hv_scripts())
import omissions as O
import discover
if "--with-subagents" in sys.argv: sys.argv.remove("--with-subagents")
else: discover.subagent_files = lambda session_path: []
O.common_exclusions = lambda records, note=None: set()
O.generated_events = lambda records: set()
import prepare
sys.exit(prepare.main())
