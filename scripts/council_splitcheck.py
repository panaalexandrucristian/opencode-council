#!/usr/bin/env python3
"""Offline standalone-contract validation for user-authored council task splits."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import council_codemap as cm


def _path(root, value, must_exist=False):
    if not isinstance(value, str):
        raise ValueError("path must be a project-relative string")
    display = cm.validated_display_path(value)
    root = os.path.realpath(root)
    full = Path(root) / display
    resolved = Path(cm.safe_resolve(root, display))
    if must_exist and not resolved.exists():
        raise ValueError("missing baseline path: {}".format(display))
    if not must_exist and os.path.lexists(str(full)):
        raise ValueError("create target already exists: {}".format(display))
    if not must_exist:
        root_path = Path(root)
        for ancestor in full.parents:
            if ancestor == root_path:
                break
            if os.path.lexists(str(ancestor)) and not ancestor.is_dir():
                raise ValueError("create target has non-directory ancestor: {}".format(ancestor))
    return full, resolved, display


def _baseline(root, rel):
    _, _, display = _path(root, rel, True)
    cap = cm.bounded_capture(os.path.realpath(root), display)
    if cap.status != "ok":
        raise ValueError("stable baseline capture failed ({}): {}".format(cap.status, cap.reason or "unknown"))
    return cap.sha256, cap.file_identity, cap.resolved_path


def validate_baseline(root, contract, child_id=None, include_writes=True, approved_identities=None,
                      identity_output=None, include_creates=False, allow_authorized_changes=False):
    errors, identities = [], {}
    if not isinstance(contract, dict):
        return ["contract must be a JSON object"]
    children=contract.get("subtasks")
    if not isinstance(children,list):
        return ["contract subtasks must be an array"]
    if approved_identities is not None and not isinstance(approved_identities,dict):
        return ["approved baseline identities must be a JSON object"]
    matched=False
    for child in children:
        if not isinstance(child, dict):
            errors.append("subtask entry must be an object")
            continue
        cid = child.get("id", "?")
        if child_id and cid != child_id:
            continue
        matched=True
        declarations = []
        fields = ("requires", "modifies", "deletes") if include_writes else ("requires",)
        if allow_authorized_changes:
            # Ratification follows the executor's authorized writes. Recheck untouched
            # prerequisites, but don't compare pre-execution digests for declared outputs.
            fields = ("requires",)
        authorized_changed=set()
        if allow_authorized_changes:
            for field in ("modifies","deletes"):
                values=child.get(field,[])
                if isinstance(values,list):
                    for value in values:
                        if isinstance(value,dict) and isinstance(value.get("path"),str):
                            try: authorized_changed.add(cm.validated_display_path(value["path"]))
                            except cm.CaptureError: pass
        for field in fields:
            values = child.get(field, [])
            if not isinstance(values, list):
                errors.append("subtask {} {} must be an array".format(cid, field)); continue
            declarations.extend((field, item) for item in values if isinstance(item, dict))
            if any(not isinstance(item, dict) for item in values):
                errors.append("subtask {} {} entries must be objects".format(cid, field))
        if include_creates:
            creates=child.get("creates",[])
            if not isinstance(creates,list):
                errors.append("subtask {} creates must be an array".format(cid)); creates=[]
            for value in creates:
                if not isinstance(value,str):
                    errors.append("subtask {} create paths must be strings".format(cid)); continue
                try:
                    full, resolved, display = _path(root,value,False)
                    if approved_identities is not None:
                        # A create's existing parent chain must still resolve to the
                        # directories observed during approval.
                        approved=approved_identities.get("{}:@ancestor:{}".format(cid,display))
                        parent=full.parent
                        while not os.path.lexists(str(parent)) and parent != Path(root): parent=parent.parent
                        parent=os.path.realpath(str(parent)); st=os.stat(parent)
                        current={"resolved_path":parent,"device":st.st_dev,"inode":st.st_ino}
                        if approved != current:
                            raise ValueError("create ancestor identity differs from approved identity")
                except (OSError,ValueError,TypeError,cm.CaptureError) as exc:
                    errors.append("subtask {} create target {} is no longer absent/safe: {}".format(cid,value,exc))
        for field, item in declarations:
            path = item.get("path")
            if allow_authorized_changes and field=="requires" and isinstance(path,str):
                try:
                    if cm.validated_display_path(path) in authorized_changed:
                        continue
                except cm.CaptureError:
                    pass
            try:
                digest, inode, canonical = _baseline(root, path)
                if item.get("sha256") != digest:
                    raise ValueError("digest mismatch (expected {}, actual {})".format(item.get("sha256"), digest))
                prior = identities.get((inode[0], inode[1]))
                if prior and prior != path:
                    raise ValueError("aliases existing file also declared as {}".format(prior))
                identities[(inode[0], inode[1])] = path
                identity_key = "{}:{}".format(inode[0], inode[1])
                current = {"sha256": digest, "resolved_path": canonical, "device": inode[0], "inode": inode[1]}
                if approved_identities is not None:
                    approved = approved_identities.get("{}:{}".format(cid, cm.validated_display_path(path)))
                    if approved != current:
                        raise ValueError("baseline identity differs from approved identity")
                if identity_output is not None:
                    identity_output.setdefault("{}:{}".format(cid, cm.validated_display_path(path)), current)
            except (OSError, ValueError, TypeError, cm.CaptureError) as exc:
                errors.append("subtask {} {} {}: missing/unreadable/unsafe baseline: {}".format(cid, field, path, exc))
    if child_id and not matched:
        errors.append("requested child is absent from contract: {}".format(child_id))
    return errors


def validate_contract(contract, root, parent_id=None, map_seed_id=None, identity_output=None):
    errors = []
    if not isinstance(contract, dict):
        return ["contract must be a JSON object"]
    if isinstance(contract.get("schema_version"),bool) or contract.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if not isinstance(contract.get("parent_id"), str) or not contract.get("parent_id") or (parent_id and contract.get("parent_id") != parent_id):
        errors.append("parent_id does not match the original task")
    if not isinstance(contract.get("map_seed_id"), str) or not contract.get("map_seed_id") or (map_seed_id and contract.get("map_seed_id") != map_seed_id):
        errors.append("map_seed_id does not match the reviewed map")
    children = contract.get("subtasks")
    if not isinstance(children, list) or not children:
        return errors + ["subtasks must be a non-empty array"]
    ids, acceptance_ids, writers, creators, child_reads, outputs = set(), set(), {}, {}, {}, {}
    canonical_identity = {}
    for child in children:
        if not isinstance(child, dict):
            errors.append("each subtask must be an object")
            continue
        cid = child.get("id")
        if (not isinstance(cid, str) or not cid or cid in (".", "..")
                or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for ch in cid)
                or cid[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"):
            errors.append("each subtask needs a filesystem-safe id (letters, digits, dot, underscore, hyphen; alphanumeric first)")
            cid = "?"
        if cid in ids:
            errors.append("duplicate subtask id: {}".format(cid))
        ids.add(cid)
        if not isinstance(child.get("text"), str) or not child.get("text"):
            errors.append("subtask {} needs complete non-empty text".format(cid))
        if not isinstance(child.get("execute"), bool):
            errors.append("subtask {} execute must be boolean".format(cid))
        for field in ("requires", "modifies", "deletes", "creates", "acceptance", "unresolved"):
            if not isinstance(child.get(field), list):
                errors.append("subtask {} {} must be an array".format(cid, field))
                child[field] = []
        if child.get("unresolved"):
            errors.append("subtask {} has unresolved inputs".format(cid))
        reads, modifies, deletes, creates = set(), set(), set(), set()
        declarations = set()
        for item in child["requires"]:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not _valid_digest(item.get("sha256")):
                errors.append("subtask {} requires entries need path and sha256".format(cid)); continue
            local_errors=[]
            key = _path_key(root, item["path"], True, local_errors, cid)
            if key is None:
                # Preserve the declared destination identity so a sibling create is named in
                # addition to reporting that it is not a baseline prerequisite.
                key = _path_key(root,item["path"],False,[],cid)
                errors.extend(local_errors)
            if key is None: continue
            if ("requires", key) in declarations: errors.append("subtask {} duplicate requires declaration: {}".format(cid,item["path"]))
            declarations.add(("requires",key)); reads.add(key); outputs[(cid,key)]="baseline"
        for field in ("modifies", "deletes"):
            for item in child[field]:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not _valid_digest(item.get("sha256")):
                    errors.append("subtask {} {} entries need path and sha256".format(cid, field)); continue
                key = _path_key(root,item["path"],True,errors,cid)
                if key is None: continue
                if (field,key) in declarations: errors.append("subtask {} duplicate {} declaration: {}".format(cid,field,item["path"]))
                declarations.add((field,key)); reads.add(key)
                (deletes if field == "deletes" else modifies).add(key)
        if modifies & deletes:
            errors.append("subtask {} declares the same path as modified and deleted: {}".format(cid, ", ".join(sorted(modifies & deletes))))
        for value in child["creates"]:
            if not isinstance(value, str):
                errors.append("subtask {} creates entries must be paths".format(cid)); continue
            key = _path_key(root,value,False,errors,cid)
            if key is None: continue
            if key in creates: errors.append("subtask {} duplicate create declaration: {}".format(cid,value))
            creates.add(key)
            if identity_output is not None:
                full,_,display=_path(root,value,False)
                ancestor=full.parent
                while not os.path.lexists(str(ancestor)) and ancestor != Path(root): ancestor=ancestor.parent
                parent=os.path.realpath(str(ancestor)); st=os.stat(parent)
                identity_output["{}:@ancestor:{}".format(cid,display)]={"resolved_path":parent,"device":st.st_dev,"inode":st.st_ino}
        for rel in reads:
            prior = canonical_identity.get(rel)
            if prior and prior != cid:
                # Shared read-only prerequisites are allowed; write overlap is checked below.
                pass
            canonical_identity[rel] = cid
        writes=modifies|deletes
        writers[cid] = writes | creates
        creators[cid] = creates
        child_reads[cid] = reads
        child["_deleted_keys"] = sorted(deletes)
        for val in child.get("acceptance", []):
            if not isinstance(val, dict) or not isinstance(val.get("id"), str) or not val.get("id") or not isinstance(val.get("description"), str) or not val.get("description"):
                errors.append("subtask {} acceptance needs id and description".format(cid)); continue
            if val["id"] in acceptance_ids:
                errors.append("duplicate acceptance id: {}".format(val["id"]))
            acceptance_ids.add(val["id"])
            argv = val.get("argv")
            if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
                errors.append("subtask {} acceptance {} argv must be a non-empty string array".format(cid, val["id"]))
            if isinstance(val.get("expected_exit"), bool) or not isinstance(val.get("expected_exit"), int):
                errors.append("subtask {} acceptance {} expected_exit must be an integer".format(cid, val["id"]))
            req = val.get("requires")
            if not isinstance(req, list):
                errors.append("subtask {} acceptance {} requires must be an array".format(cid, val["id"]))
                continue
            allowed = reads | creates
            for rel in req:
                if not isinstance(rel,str):
                    errors.append("subtask {} acceptance {} requires entries must be path strings".format(cid,val["id"])); continue
                key = _path_key(root,rel,True,[],cid)
                if key is None:
                    key = _path_key(root,rel,False,[],cid)
                if key in deletes:
                    errors.append("subtask {} acceptance {} cannot use deleted output {}".format(cid,val["id"],rel))
                elif key not in allowed:
                    errors.append("subtask {} acceptance {} input {} is outside baseline plus own outputs".format(cid, val["id"], rel))
        if writes & creates:
            errors.append("subtask {} both creates and modifies/deletes: {}".format(cid, ", ".join(sorted(writes & creates))))
    # Cross-child overlap checks are file-granular; a future sibling output never counts as baseline.
    for a in ids:
        for b in ids:
            if a >= b:
                continue
            overlap = writers.get(a, set()) & writers.get(b, set())
            overlap |= writers.get(a, set()) & child_reads.get(b, set())
            overlap |= writers.get(b, set()) & child_reads.get(a, set())
            for rel in sorted(overlap):
                errors.append("cross-child read/write or write/write overlap: {} and {} at {}".format(a, b, rel))
            for rel in creators.get(b, set()) & child_reads.get(a, set()):
                errors.append("subtask {} requires {} created by sibling {} (not a baseline prerequisite)".format(a, _display_key(root,rel), b))
            for rel in creators.get(a, set()) & child_reads.get(b, set()):
                errors.append("subtask {} requires {} created by sibling {} (not a baseline prerequisite)".format(b, _display_key(root,rel), a))
            for wa in writers.get(a, set()):
                for wb in writers.get(b, set()):
                    if wa != wb and (wa.startswith(wb.rstrip("/") + "/") or wb.startswith(wa.rstrip("/") + "/")):
                        errors.append("cross-child create/write ancestor overlap: {} and {} at {} / {}".format(a, b, wa, wb))
                    if wa.startswith("inode:") or wb.startswith("inode:"):
                        continue
                    pa=wa[5:] if wa.startswith("path:") else wa
                    pb=wb[5:] if wb.startswith("path:") else wb
                    if pa != pb and (pa.startswith(pb.rstrip(os.sep)+os.sep) or pb.startswith(pa.rstrip(os.sep)+os.sep)):
                        errors.append("cross-child canonical ancestor overlap: {} and {} at {} / {}".format(a,b,pa,pb))
    errors.extend(validate_baseline(root, contract, identity_output=identity_output))
    return errors


def _valid_digest(value):
    return isinstance(value,str) and len(value)==64 and all(c in "0123456789abcdef" for c in value)


def _display_key(root,key):
    if key.startswith("path:"):
        return os.path.relpath(key[5:],os.path.realpath(root))
    return key


def _path_key(root,value,must_exist,errors,cid):
    try:
        full,resolved,display=_path(root,value,must_exist)
        if must_exist:
            cap=cm.bounded_capture(os.path.realpath(root),display)
            if cap.status!="ok": raise ValueError("stable identity capture failed: {}".format(cap.reason))
            return "inode:{}:{}".format(*cap.file_identity)
        return "path:"+os.path.normcase(os.path.realpath(str(resolved)))
    except (OSError,ValueError,TypeError,cm.CaptureError) as exc:
        errors.append("subtask {} unsafe or missing path {}: {}".format(cid,value,exc))
        return None


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", required=True)
    p.add_argument("--contract", required=True)
    p.add_argument("--parent-id")
    p.add_argument("--map-seed-id")
    p.add_argument("--baseline-only", action="store_true")
    p.add_argument("--child")
    p.add_argument("--include-writes", action="store_true")
    p.add_argument("--include-creates", action="store_true")
    p.add_argument("--allow-authorized-changes", action="store_true")
    p.add_argument("--identity-file")
    p.add_argument("--identity-output")
    a = p.parse_args(argv)
    try:
        contract = json.loads(Path(a.contract).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("invalid contract: {}".format(exc), file=sys.stderr)
        return 2
    approved = None
    if a.identity_file:
        try: approved=json.loads(Path(a.identity_file).read_text(encoding="utf-8"))
        except (OSError,ValueError) as exc:
            print("approved baseline identity file is unavailable: {}".format(exc),file=sys.stderr); return 1
    identities={}
    errors = (validate_baseline(a.root, contract, a.child, a.include_writes, approved, identities, a.include_creates, a.allow_authorized_changes) if a.baseline_only
              else validate_contract(contract, a.root, a.parent_id, a.map_seed_id, identities))
    if a.identity_output and not errors:
        Path(a.identity_output).write_text(json.dumps(identities,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("contract valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
