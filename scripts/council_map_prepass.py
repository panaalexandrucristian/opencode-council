#!/usr/bin/env python3
"""Offline validation and capture preparation for a council navigation pre-pass."""
import argparse
import json
import os
import re
from pathlib import Path
import sys


def _safe_path(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    p = Path(value)
    return "\\" not in value and not p.is_absolute() and all(part not in ("", ".", "..") for part in value.split("/"))


_BARE_KEY = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?=\s*:)')
_TOP_KEYS = ("candidates", "unresolved", "stopped_reason")


def _repair_json_syntax(text):
    """Repair punctuation and keys outside strings; accept only after strict validation."""
    out, repairs, i, n, depth = [], [], 0, len(text), 0
    top = 1 if text.startswith("{") else 0

    def before_key(name):
        prev = next((s for s in reversed(out) if not s.isspace()), "")
        if depth == top and name in _TOP_KEYS and prev[-1:] not in ("", "{", ","):
            out.append(",")
            repairs.append("inserted missing comma before key {}".format(name))
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            k = j + 1
            while k < n and text[k].isspace():
                k += 1
            if k < n and text[k] == ":":
                before_key(text[i + 1:j])
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if c == ",":
            k = i + 1
            while k < n and text[k].isspace():
                k += 1
            prev = next((s for s in reversed(out) if not s.isspace()), "")
            if k < n and text[k] in "}]" and prev[-1:] not in ("", "{", "[", ",", ":"):
                repairs.append("removed trailing comma before {}".format(text[k]))
                i += 1
                continue
        m = _BARE_KEY.match(text, i)
        if m and not (i and (text[i - 1].isalnum() or text[i - 1] == "_")):
            before_key(m.group(0))
            out.append('"{}"'.format(m.group(0)))
            repairs.append("quoted key {}".format(m.group(0)))
            i = m.end()
            continue
        depth += {"{": 1, "[": 1, "}": -1, "]": -1}.get(c, 0)
        out.append(c)
        i += 1
    fixed = "".join(out).strip()
    if fixed.startswith('"'):
        fixed = "{" + fixed + "}"
        repairs.append("added missing outer braces")
    return fixed, repairs


def _unique_keys(pairs):
    if len({k for k, _ in pairs}) != len(pairs):
        raise ValueError("duplicate key")
    return dict(pairs)


def _reject_constant(value):
    raise ValueError("non-JSON constant: " + value)


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
        repairs = []
        if stripped.startswith("```json") and stripped.endswith("```"):
            stripped = stripped[len("```json"): -3].strip()
            repairs.append("removed JSON code fence")
        try:
            obj = json.loads(stripped, object_pairs_hook=_unique_keys, parse_constant=_reject_constant)
        except json.JSONDecodeError as exc:
            fixed, syntax_repairs = _repair_json_syntax(stripped)
            repairs.extend(syntax_repairs)
            try:
                obj = json.loads(fixed, object_pairs_hook=_unique_keys, parse_constant=_reject_constant)
            except ValueError:
                raise exc
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
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
    result = {"status": status, "candidates": candidates, "unresolved": unresolved,
              "stopped_reason": stopped, "coverage": "unknown"}
    if repairs:
        result["repairs"] = repairs
    return result, errors


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
    for note in result.get("repairs", []):
        print("mapper JSON syntax repaired: {}".format(note), file=sys.stderr)
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
