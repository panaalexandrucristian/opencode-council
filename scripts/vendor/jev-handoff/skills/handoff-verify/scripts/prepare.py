#!/usr/bin/env python3
"""Deterministic preparation for handoff-verify (no Jev, no network, never executes handoff code).
usage: prepare.py [SESSION_ID|PATH.jsonl|opencode:ID|opencode-db:/ABS/DB#ID] [--cwd DIR] [--out DIR] [--max-chars N] [--location DIR ...] [--target FILE ...]
Writes into the run directory <session-cwd>/.handoff-verify/<session-id>/<run-id>/work/ (or --out):
  inventory.json         handoff candidates + dispositions (included_by_name / needs_jev_classify / failed / unlinked / excluded / external_unlinked) + aliases/copies; a row per path with its versions (one reconstruction
                         per transcript, `source_file`/`source_kind`; every path is resolved in the cwd RECORDED for the call, a relative one without a recorded cwd is listed in `unplaced_writes`, never placed under the cwd of
                         this process), `provenance`, `ordering` and `blockers`; `--target FILE` adds an explicit note that has no recorded write as `external_unlinked` (no versions)
  handoffs/<basename>-<hash8 of the real path>.v<N>.md  reconstructed versions (sanitized; a path written in several transcripts adds `.<hash8 of the transcript>`); versions[] carry sha256/uuid/timestamp/status/evaluated_against/unpositioned/mixed,
                         `prefix_records` and `prefix_chunks` (the chunks of the version's own transcript that lie entirely before the write's tool_use, by STREAM position (line, block), never by timestamps)
  transcript/chunk-NNNN.txt sanitized transcript chunks, one transcript after the other in stream order, WITHOUT the material no consumer takes for source (omissions.common_exclusions: the skill's and Jev's calls and results,
                         the spans of the verification runs that follow a version, the notes' own mutations and their results); chunks never straddle the tool_use of a version write (a record is cut there) and never mix two transcripts
  coverage.json          chunk registry proving the whole transcript is covered (+ sanitization counters)
Prints a compact JSON summary on stdout."""
import argparse, glob, hashlib, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discover as D, sanitize as S, report as R, omissions as O, versions as V

def block_positions(recs):
    """tool_use id -> (stream line of its record, block index in the record), for every tool_use of the records."""
    return {b["id"]: (d["_line"], j) for d in recs if d.get("type") == "assistant" and isinstance((d.get("message") or {}).get("content"), list)
            for j, b in enumerate(d["message"]["content"]) if isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("id"), str)}

def flatten(path, gone, splits=(), recs=None):
    """The transcript records of one file as text entries for the chunks, in STREAM order. `gone` = the (record, block) pairs no consumer takes for source (omissions.common_exclusions / generated_events): they are not part
    of them. `splits` = {(stream line, block index)} of the tool_use blocks that audit prefixes end at: a record is cut there into entries (`jlo`..`jhi` = the block indexes an entry holds), so that what precedes
    the tool_use inside its own record stays on the before side and what follows it on the after side (the tool_use block itself belongs to the after side)."""
    out, recs = [], D.load_jsonl(path) if recs is None else recs
    for i, d in enumerate(recs):
        if d.get("type") not in ("user", "assistant"): continue
        c = d.get("message", {}).get("content"); parts = []
        if isinstance(c, str):
            if (i, 0) not in gone: parts.append((0, c))
        else:
            for j, b in enumerate(c or []):
                if not isinstance(b, dict) or (i, j) in gone: continue
                if b.get("type") == "text": parts.append((j, b.get("text", "")))
                elif b.get("type") == "tool_use": parts.append((j, "[tool_use %s] %s" % (b["name"], json.dumps(b["input"], ensure_ascii=False))))
                elif b.get("type") == "tool_result":
                    cc = b.get("content"); cc = "".join(x.get("text", "") for x in cc if isinstance(x, dict)) if isinstance(cc, list) else str(cc)
                    parts.append((j, "[tool_result%s] %s" % (" ERROR" if b.get("is_error") else "", cc)))
        cuts = sorted(j for ln, j in splits if ln == d["_line"]); groups = {}
        for j, t in parts: groups.setdefault(sum(1 for x in cuts if x <= j), []).append((j, t))
        for k in sorted(groups):
            g = groups[k]; out.append(dict(uuid=d.get("uuid") or "no-uuid", role=d["type"], text=" ".join(t for _, t in g), line=d["_line"], jlo=g[0][0], jhi=g[-1][0], ts=d.get("timestamp") or "", file=path, parts=list(g)))   # parts = (block index, text) of every block the entry holds (the audit bounds an entry by the window of an evaluation)
    return out

def build_parser():
    ap = argparse.ArgumentParser(); ap.add_argument("session", nargs="?"); ap.add_argument("--cwd"); ap.add_argument("--out"); ap.add_argument("--max-chars", type=int, default=9000)
    ap.add_argument("--location", action="append", default=[]); ap.add_argument("--target", action="append", default=[])
    return ap

def explicit_targets(given, base, files, recorded):
    """The `--target FILE` notes (absolute, or relative to `base`; aliases of one canonical path are one target). -> (targets {real: dict(given paths, relocated_from)}, external rows): a target with a recorded
    supported write is an ordinary row; one without (failed writes only, Bash only, no record) is an `external_unlinked` row with no versions, an informational `disk_sha256` (never a version), its `provenance` and
    the precise `blockers`; a verified copy of a written note is that note's copy."""
    targets, rows = {}, []
    for g in given or []:
        path = g if os.path.isabs(g) else os.path.join(base, g); targets.setdefault(os.path.realpath(path), dict(given=[])).setdefault("given", []).append(path)
    for real, info in targets.items():
        if real in recorded: continue
        best = max((V.provenance(f, real) for f, _ in files), key=lambda p: V.RANK[p["state"]])
        if best["state"] == "recorded_write": info["relocated_from"] = best["source_path"]; continue
        try: disk = hashlib.sha256(open(real, "rb").read()).hexdigest()
        except OSError: disk = None
        rows.append(dict(path=real, aliases=sorted(set(info["given"]) - {real}), disposition="external_unlinked", linked=False, versions=[], exists_now=os.path.exists(real), disk_sha256=disk, provenance=best["state"], blockers=[best["blocker"]]))
    return targets, rows

def inspect_session(sp):
    """The transcripts of a session (the session line + its subagents) with their inventory -> (files, items, bash, by_file, reads_of, diags_of)."""
    files = [(sp, "session")] + [(f, "subagent") for f in D.subagent_files(sp)]
    items, bash, by_file, reads_of, diags_of = [], [], {}, {}, {}
    for f, src in files:
        i, b = D.inventory(f, src); items += i; bash += b; by_file[f] = i; reads_of[f] = D.reads(f); diags_of[f] = D.diagnostics(f)
    return files, items, bash, by_file, reads_of, diags_of

def derive(sp, cwd_arg, targets_arg=(), max_chars=9000, workdir=None):
    """The deterministic preparation of ONE session, shared by `main` and by the full-report audit (audit.py), so that both see exactly the same versions, exclusions, stream boundaries and chunks. With `workdir` (a path, or a
    callable taking the first recorded cwd and returning the path) the artifacts are written there; with None nothing is written (a pure derivation). -> dict(files, items, bash, by_file, diags_of, cwd0, cwds, work, cls, targets, external,
    unplaced, handoff_reals, flat_of, flat_all, san_total, inv_h, chunks_meta, chunk_texts); every chunk of `chunks_meta` carries `index`, `file`, `sha256` (of the chunk text as written, UTF-8) and its stream positions."""
    files, items, bash, by_file, reads_of, diags_of = inspect_session(sp)
    cwd0 = next((i["cwd"] for i in items if i.get("cwd")), cwd_arg or os.getcwd())
    cwds = list(dict.fromkeys(d["cwd"] for f, _ in files for d in D.load_jsonl(f) if isinstance(d.get("cwd"), str) and d["cwd"]))   # every cwd the selected session and its subagents recorded, in order of appearance (not only the Write/Edit calls)
    work = workdir(cwd0) if callable(workdir) else workdir
    if work: os.makedirs(os.path.join(work, "handoffs"), exist_ok=True); os.makedirs(os.path.join(work, "transcript"), exist_ok=True)
    cls = D.classify_candidates(items)
    # group by real path (resolved in the cwd recorded for the call); identical copies share analysis, but every write keeps its own temporal evaluation
    by_real, unplaced = {}, [dict(path=i["path"], tool_use_id=i["tool_use_id"], op=i["op"], source_file=i["file"], reason="a relative path with no cwd recorded for the call: its place is not demonstrated (the cwd of this process is never substituted)")
                             for i in items if i["success"] and D.real_of(i["path"], i.get("cwd")) is None]
    for it in cls["included_by_name"] + cls["needs_jev_classify"]:
        real = D.real_of(it["path"], it.get("cwd"))
        if real is not None: by_real.setdefault(real, []).append(it)
    recorded = {D.real_of(i["path"], i.get("cwd")) for i in items if i["success"]} - {None}
    targets, external = explicit_targets(targets_arg, cwd_arg or cwd0, files, recorded)
    handoff_reals = sorted(set(by_real) | {r for r in targets if r in recorded})
    flat_of, wpos = {}, {}   # per transcript, in stream order: the entries without what no consumer takes for source (the same exclusions as the source window), cut at the tool_use of every note write
    for f, src in files:
        mine = [r for r in handoff_reals if any(i["success"] and D.real_of(i["path"], i.get("cwd")) == r for i in by_file[f])]; recs = D.load_jsonl(f); wpos[f] = block_positions(recs)
        gone = set().union(*(O.common_exclusions(recs, r) for r in mine)) if mine else O.generated_events(recs)
        flat_of[f] = flatten(f, gone, {wpos[f][i["tool_use_id"]] for i in by_file[f] if i["success"] and D.real_of(i["path"], i.get("cwd")) in mine and i["tool_use_id"] in wpos[f]}, recs)
    san_total = {"redactions": {}, "ambiguous_redacted": 0, "canary_leaks": []}
    inv_h, used = [], {}
    def artifact(real, f, v, multi):
        """handoffs/<basename>-<hash8 of the real path>[.<hash8 of the transcript>].v<N>.md: the hash is extended (report_names rule) when another artifact already holds the name; an artifact is never overwritten."""
        disc = ".%s" % hashlib.sha256(D.canon(f).encode()).hexdigest()[:8] if multi else ""; key = (real, f, v["version"]); extra = 0
        while True:
            fn = "handoffs/%s-%s%s.v%d.md" % (os.path.basename(real), R.short_hash(real, extra), disc, v["version"])
            if used.get(fn, key) == key: used[fn] = key; return fn
            extra += 1
            if extra > 24: raise RuntimeError("no free artifact name for %s version %d" % (real, v["version"]))
    for real in handoff_reals:
        writers = [(f, src) for f, src in files if any(i["success"] and D.real_of(i["path"], i.get("cwd")) == real for i in by_file[f])]
        streams = [(f, src, V.reconstruct_stream(f, src, real, items=by_file[f], reads=reads_of[f], diagnostics=diags_of[f], mixed_with=[g for g, _ in writers if g != f])) for f, src in writers]
        multi = len(streams) > 1; vrows, blockers = [], []
        for f, src, vs in streams:
            for v in vs:
                txt, rep = (S.sanitize(v["content"]) if v["content"] is not None else (None, {"redactions": {}, "ambiguous_redacted": 0}))
                for k, n in rep["redactions"].items(): san_total["redactions"][k] = san_total["redactions"].get(k, 0) + n
                san_total["ambiguous_redacted"] += rep["ambiguous_redacted"]
                fn = None
                if txt is not None:
                    fn = artifact(real, f, v, multi)
                    if work: open(os.path.join(work, fn), "w", encoding="utf-8", newline="").write(txt)
                vrows.append(dict(version=v["version"], file=fn, sha256=v["sha256"], tool_use_id=v["write_tool_use_id"], uuid=v["uuid"], timestamp=v["timestamp"], status=v["status"], reason=v["reason"], op=v["op"],
                                  write_pos=[v.get("timestamp") or "", src, v["line"]], write_line=v["line"], write_block=wpos[f].get(v["write_tool_use_id"], (v["line"], 0))[1], sanitized_ambiguous=rep["ambiguous_redacted"], source_file=f, source_kind=src, unpositioned=v["unpositioned"], mixed=v["mixed"],
                                  evaluated_against=None if multi else ("session_end" if v is vs[-1] else "prefix"),   # several transcripts: no demonstrated common order, so no "last version" and no window
                                  prefix_records=sum(1 for r in flat_of[f] if (r["line"], r["jhi"]) < wpos[f].get(v["write_tool_use_id"], (v["line"], 0))), prefix_source_file=f, prefix_chunks=[]))   # by the STREAM position of the write's tool_use (line, block) in the version's own transcript (filled in below); never timestamps
        if multi:
            blockers.append("%s was written in several streams (transcripts: %s): no common order between independent streams is demonstrated, so the combined history, the last version and the windows that depend on their order are unresolved (the bytes of each full Write are kept per transcript; an Edit whose base the other stream may have changed is not recoverable)" % (real, ", ".join(os.path.basename(f) for f, _, _ in streams)))
        elif streams and streams[0][1] == "subagent":
            blockers.append("%s was written only in a subagent transcript (%s): it is not a version of the parent session; prepare and verify that transcript as its own session (pass it as the source)" % (real, streams[0][0]))
        for v in (x for _, _, vs in streams for x in vs):
            if v["unpositioned"]: blockers.append("%s: %s" % (real, V.unpositioned_reason(v))); break
        inv_h.append(dict(path=real, aliases=sorted({i["path"] for i in items if D.real_of(i["path"], i.get("cwd")) == real and i["path"] != real}),
                          disposition=("included_by_name" if D.auto_name(real) else "needs_jev_classify") if real in by_real else "included_by_target", linked=True, versions=vrows, exists_now=os.path.exists(real),
                          streams=[dict(source_file=f, source_kind=src, versions=len(vs)) for f, src, vs in streams], ordering=dict(demonstrated=not multi, reason="independent transcripts have no demonstrated common order" if multi else None),
                          provenance="recorded_write", blockers=blockers, **({"explanation": D.TARGET_EXPLANATION} if real in targets and not D.is_text_note(real) else {})))
    for row in external:
        if not D.is_text_note(row["path"]): row["explanation"] = D.TARGET_EXPLANATION
    inv_h += external
    by_path = {h["path"]: h for h in inv_h}
    for real, info in targets.items():   # a verified copy of a written note is a copy of it, not a note of its own
        if info.get("relocated_from") in by_path: by_path[info["relocated_from"]].setdefault("copies", []).extend(info["given"])
    # chunk each transcript in stream order; force a break at every handoff write position (its stream line) so prefixes are chunk-aligned; a chunk never mixes two transcripts
    chunks_meta, chunk_texts, buf, size, n = [], [], [], 0, 0
    def flush():
        nonlocal buf, size, n
        if buf:
            fn = "transcript/chunk-%04d.txt" % n; text = "".join(x[1] for x in buf)
            if work: open(os.path.join(work, fn), "w", encoding="utf-8", newline="").write(text)
            chunk_texts.append(text)
            ents, at = [], 0
            for x in buf:   # where every piece lies in the chunk text (offsets), for the audit; `eoff` = the offset of the piece inside its (sanitized) entry; nothing of this is written to the artifacts
                ents.append(dict(uuid=x[0], role=x[7]["role"], line=x[4], jlo=x[5], jhi=x[6], start=at, end=at + len(x[1]), eoff=x[8], entry_text=x[9], parts=x[7]["parts"])); at += len(x[1])
            chunks_meta.append(dict(index=n, file=fn, sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), source_file=buf[0][3], uuids=[x[0] for x in buf], first_pos=list(buf[0][2]), last_pos=list(buf[-1][2]), first_line=buf[0][4], last_line=buf[-1][4], first_block=buf[0][5], last_block=buf[-1][6], chars=size, entries=ents)); buf, size, n = [], 0, n + 1
    for f, src in files:
        marks = sorted((v["write_line"], v["write_block"]) for h in inv_h for v in h["versions"] if v["source_file"] == f); mi = 0
        for r in flat_of[f]:
            while mi < len(marks) and marks[mi] <= (r["line"], r["jlo"]): flush(); mi += 1
            t, rep = S.sanitize("[%s %s] %s\n" % (r["uuid"], r["role"], r["text"]))
            for k, c in rep["redactions"].items(): san_total["redactions"][k] = san_total["redactions"].get(k, 0) + c
            san_total["ambiguous_redacted"] += rep["ambiguous_redacted"]
            for off, piece in [(i, t[i:i + max_chars]) for i in range(0, len(t), max_chars)] or [(0, t)]:
                if size + len(piece) > max_chars: flush()
                buf.append((r["uuid"], piece, (r["ts"], src, r["line"]), f, r["line"], r["jlo"], r["jhi"], r, off, t)); size += len(piece)
        flush()
    for h in inv_h:
        for v in h["versions"]: v["prefix_chunks"] = [c["file"] for c in chunks_meta if c["source_file"] == v["source_file"] and (c["last_line"], c["last_block"]) < (v["write_line"], v["write_block"])]
    return dict(files=files, items=items, bash=bash, by_file=by_file, diags_of=diags_of, cwd0=cwd0, cwds=cwds, work=work, cls=cls, targets=targets, external=external, unplaced=unplaced, handoff_reals=handoff_reals,
                flat_of=flat_of, flat_all=[r for f, _ in files for r in flat_of[f]], san_total=san_total, inv_h=inv_h, chunks_meta=chunks_meta, chunk_texts=chunk_texts)

def main():
    a = build_parser().parse_args()
    locs, why = O.check_locations(a.location)
    if why: print(json.dumps({"error": why})); return 3
    sp, how, ambiguous = D.resolve_session_info(a.session, a.cwd)
    if ambiguous: print(json.dumps({"error": "current session not demonstrated: several recently modified sessions in this project; pass the session id or .jsonl path", "resolution": how})); return 3
    if not sp or not D.source_exists(sp): print(json.dumps({"error": "session not found", "arg": O.shown(a.session)})); return 2
    sid = D.session_label(sp)
    def workdir(cwd0):
        if a.out: return a.out
        rd, run_id = R.new_run_dir(a.cwd or cwd0, sid); return os.path.join(rd, "work")
    P = derive(sp, a.cwd, a.target, a.max_chars, workdir)
    work, files, cls, bash, unplaced, targets, inv_h, chunks_meta, flat_all, san_total, cwd0, cwds, diags_of = (P[k] for k in ("work", "files", "cls", "bash", "unplaced", "targets", "inv_h", "chunks_meta", "flat_all", "san_total", "cwd0", "cwds", "diags_of"))
    items = P["items"]
    import audit as AU   # preparation establishes the session's candidate registry (its location is derived from the source, not chosen by a report): the audit expects it
    try:
        rp = AU.registry_path_of(sp)
        if a.out: reg_state = "not touched (--out writes only its directory): audit.py template / register establishes it"
        else: reg_state = "unavailable: the session records no usable cwd" if rp is None else ("created" if AU.ensure_registry(rp, D.canon(sp)) else "present")
    except (ValueError, OSError) as e: reg_state = "refused: %s" % S.sanitize(str(e))[0][:160]
    covered = {u for c in chunks_meta for u in c["uuids"]}
    cov = dict(records=len(flat_all), chunks=len(chunks_meta), uuids_in_chunks=len(covered), complete=covered == {r["uuid"] for r in flat_all}, sanitization=san_total,
               merged_view="informational: the transcripts one after the other, each in stream order (first_pos/last_pos carry the recorded timestamps, which order nothing); the audit windows are the stream positions up to a write's tool_use (versions[].prefix_chunks), and verification material is not part of it",
               continuation="links not demonstrated: leafUuid only points inside the same file (results/PROTOCOL.md P1); sibling sessions are NOT included",
               chunk_list=[dict(index=c["index"], file=c["file"], sha256=c["sha256"], chars=c["chars"]) for c in chunks_meta],   # no path: a name of work/ and a hash of the text
               audit_registry=reg_state, audit_note="`complete` says the chunks were produced for every record; it is NOT a review. The full-report audit (audit.py) needs a review record per expected chunk (index + sha256 above) and an outcome for each of the nine categories.")
    json.dump(cov, open(os.path.join(work, "coverage.json"), "w"), indent=1)
    inv = dict(session=dict(session_id=sid, jsonl=sp, cwd=cwd0, work_locations=locs, subagents=[f for f, s in files if s == "subagent"], diagnostics=[dict(d, source_file=f) for f in diags_of for d in diags_of[f]]), handoffs=inv_h, unplaced_writes=unplaced,
               needs_jev_classify=[dict(path=i["path"], tool_use_id=i["tool_use_id"], uuid=i["uuid"], excerpt_hint="read handoffs/ file; classify with jev_classify (>0.95 decides)") for i in cls["needs_jev_classify"]],
               failed_writes=[dict(path=i["path"], tool_use_id=i["tool_use_id"], op=i["op"]) for i in cls["failed"]],
               unlinked_bash=[dict(path=b["path"], tool_use_id=b["tool_use_id"], note="unlinked: created via Bash, no Write/Edit link") for b in bash],
               excluded_non_text=[i["path"] for i in cls["excluded_non_text"]], env_excluded=[i["path"] for i in items if S.is_excluded_file(i["path"])])
    json.dump(inv, open(os.path.join(work, "inventory.json"), "w"), indent=1)
    print(json.dumps(dict(work=work, session_id=sid, handoff_files=len(inv_h), versions=sum(len(h["versions"]) for h in inv_h), unlinked=len(inv["unlinked_bash"]), failed_writes=len(inv["failed_writes"]),
                          transcript_chunks=len(chunks_meta), transcript_complete=cov["complete"], needs_jev_classify=len(inv["needs_jev_classify"]), targets=len(targets), blockers=[b for h in inv_h for b in h.get("blockers", [])],
                          session_cwd=cwd0, session_cwds=cwds, note_dirs=sorted({os.path.dirname(h["path"]) for h in inv_h}), work_locations=locs)))
    return 0
if __name__ == "__main__": sys.exit(main())
