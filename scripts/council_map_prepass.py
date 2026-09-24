#!/usr/bin/env python3
"""Offline validation and capture preparation for a council navigation pre-pass."""
import argparse
import json
import os
from pathlib import Path
import sys


def _safe_path(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    p = Path(value)
    return "\\" not in value and not p.is_absolute() and all(part not in ("", ".", "..") for part in value.split("/"))


def validate_mapper_output(raw, max_output_bytes=65536):
    """Return (validated result, errors); retain valid selectors when individual items fail."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > max_output_bytes:
        return {"status": "unavailable", "candidates": [], "unresolved": [],
                "stopped_reason": "oversized", "coverage": "unknown"}, ["response exceeds max_output_bytes"]
    if not raw.strip():
        return {"status": "unavailable", "candidates": [], "unresolved": [],
                "stopped_reason": "empty", "coverage": "unknown"}, ["empty mapper response"]
    try:
        text = raw.decode("utf-8")
        stripped = text.strip()
        if stripped.startswith("```json") and stripped.endswith("```"):
            stripped = stripped[len("```json"): -3].strip()
        obj = json.loads(stripped)
    except (UnicodeDecodeError, ValueError) as exc:
        return {"status": "unavailable", "candidates": [], "unresolved": [],
                "stopped_reason": "malformed", "coverage": "unknown"}, ["malformed mapper JSON: {}".format(exc)]
    if not isinstance(obj, dict):
        return {"status": "unavailable", "candidates": [], "unresolved": [],
                "stopped_reason": "malformed", "coverage": "unknown"}, ["response must be one JSON object"]
    errors, candidates, unresolved = [], [], []
    source = obj.get("candidates")
    if not isinstance(source, list):
        errors.append("candidates must be an array")
        source = []
    for n, item in enumerate(source):
        if not isinstance(item, dict) or not _safe_path(item.get("path")):
            errors.append("candidate {} has an unsafe or missing project-relative path".format(n))
            continue
        clean = {"path": item["path"]}
        if "lines" in item:
            pair = item["lines"]
            if not (isinstance(pair, list) and len(pair) == 2
                    and all(isinstance(v, int) and not isinstance(v, bool) for v in pair)
                    and 1 <= pair[0] <= pair[1]):
                errors.append("candidate {} lines must be inclusive positive [first,last] integers".format(n))
                continue
            clean["lines"] = pair
        candidates.append(clean)
    unresolved_source = obj.get("unresolved", [])
    if not isinstance(unresolved_source, list):
        errors.append("unresolved must be an array")
        unresolved_source = []
    for n, item in enumerate(unresolved_source):
        if not isinstance(item, dict) or not isinstance(item.get("target"), str) or not isinstance(item.get("reason"), str):
            errors.append("unresolved item {} needs string target and reason".format(n))
            continue
        unresolved.append({"target": item["target"], "reason": item["reason"]})
    stopped = obj.get("stopped_reason")
    if not isinstance(stopped, str) or not stopped:
        errors.append("stopped_reason must be a non-empty string")
        stopped = "invalid_schema"
    status = "ok" if candidates and not errors else ("partial" if candidates else "unavailable")
    stop_kind = stopped.strip().lower() if isinstance(stopped,str) else ""
    if stop_kind not in ("done", "complete", "completed", "finished"):
        status="partial" if candidates else "unavailable"
    return {"status": status, "candidates": candidates, "unresolved": unresolved,
            "stopped_reason": stopped, "coverage": "unknown"}, errors


def prepare_captures(result, root, run_dir=None):
    """Prepare selector records. Every in-project path is eligible (repository metadata and the
    run directory included); a selector whose resolved target leaves the project is rejected."""
    root = Path(root).resolve()
    kept, errors = [], []
    for item in result.get("candidates", []):
        path = item["path"]
        full = root / path
        try:
            resolved = full.resolve(strict=True)
            resolved.relative_to(root)
            if not resolved.is_file():
                raise ValueError("not a regular file")
        except (OSError, ValueError):
            errors.append("unavailable or escaping selector: {}".format(path))
            continue
        kept.append(dict(item))
    return kept, [], errors


def _inside(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def scan_escapes(root):
    """Return the sorted in-project symlinks that must be blocked before mapper access.

    The walk covers the whole project (metadata and run directory included) and never follows a
    link, so it cannot loop. A link is escaping when its fully resolved target (chains followed)
    lies outside the resolved project root. An in-project directory link is recorded too when its
    target subtree contains an escaping or recorded link: every alias path through it reaches an
    escape, and a deny on the alias covers all of them. Any OS error raises: an incomplete scan is
    never proof of a clean project."""
    base = os.path.realpath(root)
    links = []  # (relative path, real location, resolved target, target is a directory)

    def fail(exc):
        raise exc
    for current, dirs, files in os.walk(base, onerror=fail, followlinks=False):
        for name in sorted(dirs + files):
            full = os.path.join(current, name)
            if not os.path.islink(full):
                continue
            target = os.path.realpath(full)
            links.append((os.path.relpath(full, base), full, target, os.path.isdir(target)))
    blocked = {rel: target for rel, _, target, _ in links if not _inside(target, base)}
    changed = True
    while changed:  # fixpoint over a finite set of directory aliases
        changed = False
        for rel, full, target, is_dir in links:
            if rel in blocked or not is_dir:
                continue
            if any(_inside(os.path.join(base, other), target) for other in blocked):
                blocked[rel] = target
                changed = True
    return [{"path": rel, "target": blocked[rel], "escapes": not _inside(blocked[rel], base)}
            for rel in sorted(blocked)]


def _swapped_case(name):
    return name.swapcase() if name.swapcase() != name else None


def probe_case_insensitive(root, links=()):
    """True when the project filesystem ignores letter case, probed without writing.

    The first recorded link whose name has a cased letter is looked up again with its letter case
    swapped (else the resolved project root, else its nearest cased ancestor); the filesystem
    ignores case when both names lead to the same device and inode. With nothing cased to probe the
    answer is True: case-folded denies may over-match, never under-match."""
    base = os.path.realpath(root)
    candidates = [os.path.join(base, item["path"]) for item in links]
    path = base
    while True:
        candidates.append(path)
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    for full in candidates:
        head, name = os.path.split(full)
        swapped = _swapped_case(name)
        if not swapped:
            continue
        first = os.lstat(full)
        try:
            other = os.lstat(os.path.join(head, swapped))
        except (FileNotFoundError, NotADirectoryError):
            return False
        return (first.st_dev, first.st_ino) == (other.st_dev, other.st_ino)
    return True


def fold_case_pattern(path):
    """OpenCode wildcard form of `path` that matches every case (and Unicode normalization) variant:
    each cased ASCII letter becomes `?`, each non-ASCII character `*`, anything else stays."""
    out = []
    for ch in path:
        if ord(ch) > 127:
            out.append("*")
        elif ch.isalpha():
            out.append("?")
        else:
            out.append(ch)
    return "".join(out)


def boundary_scan(root):
    """The persisted pre-dispatch boundary: the escaping links, whether the filesystem ignores case,
    and for each link the relative pattern its denies use (case-folded when case is ignored)."""
    links = scan_escapes(root)
    folded = probe_case_insensitive(root, links)
    for item in links:
        item["pattern"] = fold_case_pattern(item["path"]) if folded else item["path"]
    return {"case_insensitive": folded, "links": links}


def write_coverage(path, parent_id, mapping_digest, selector_digest, captures,
                   exclusions, failures, telemetry_complete=False):
    payload = {"schema_version": 1, "parent_id": parent_id,
               "mapping_input_digest": mapping_digest,
               "selector_artifact_digest": selector_digest,
               "captured_versions_ranges": captures,
               "capture_statuses": [x.get("status", "unknown") for x in captures],
               "exclusions": exclusions, "resource_schema_failures": failures,
               "telemetry_complete": bool(telemetry_complete), "coverage": "unknown"}
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["scan-escapes"]:
        s = argparse.ArgumentParser(prog="council_map_prepass.py scan-escapes")
        s.add_argument("--root", required=True)
        a = s.parse_args(argv[1:])
        if not os.path.isdir(a.root):
            print("project root is not a directory: {}".format(a.root), file=sys.stderr)
            return 1
        try:
            found = boundary_scan(a.root)
        except OSError as exc:
            print("symlink scan incomplete: {}".format(exc), file=sys.stderr)
            return 1
        print(json.dumps(found, sort_keys=True))
        return 0
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("raw")
    p.add_argument("--max-output-bytes", type=int, default=65536)
    p.add_argument("--root")
    p.add_argument("--run-dir")
    p.add_argument("--selectors")
    a = p.parse_args(argv)
    raw = Path(a.raw).read_bytes()
    result, errors = validate_mapper_output(raw, a.max_output_bytes)
    if a.root:
        original_count = len(result.get("candidates", []))
        candidates, exclusions, capture_errors = prepare_captures(result, a.root, a.run_dir)
        errors.extend(capture_errors)
        result["candidates"] = candidates
        stopped = str(result.get("stopped_reason", "")).strip().lower()
        if stopped not in ("done", "complete", "completed", "finished"):
            result["status"] = "partial" if candidates else "unavailable"
        elif not candidates:
            result["status"] = "unavailable"
        elif errors or len(candidates) < original_count:
            result["status"] = "partial"
        result["exclusions"] = exclusions
    if a.selectors:
        Path(a.selectors).write_text(json.dumps(result["candidates"], indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result": result, "errors": errors}, sort_keys=True))
    return 0 if result["status"] in ("ok", "partial") else 1


if __name__ == "__main__":
    sys.exit(main())
