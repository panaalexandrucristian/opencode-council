#!/usr/bin/env python3
"""Refresh or verify scripts/vendor/jev-handoff, the pinned copy of the jev compact-handoff scripts.

  update_jev_handoff.py --repo PATH --commit SHA   copy the scripts from that jev commit and rewrite VENDOR.json
  update_jev_handoff.py --check                    verify the copy against VENDOR.json (the default)
  --dest DIR                                       work on another copy than scripts/vendor/jev-handoff (tests)

council.sh builds a member's handover note with compact-handoff's make_context.py. That script runs handoff-verify's
prepare.py, so the copy holds the files of compact-handoff plus the import closure of prepare.py, omissions.py and
discover.py (the modules prepare_all.py imports). The closure is computed from the source commit, never listed by hand.
The files keep their skills/<skill>/scripts layout because prepare_all.py finds its sibling skill by that path.
Standard library only; git is needed for --repo."""
import argparse, ast, hashlib, json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(HERE, "jev-handoff")
SOURCE = "https://github.com/panaalexandrucristian/jev-mcp-private"
CH = "skills/compact-handoff/scripts"
HV = "skills/handoff-verify/scripts"
CH_FILES = ("make_context.py", "compact_context.py", "opencode_source.py", "prepare_all.py")
HV_ENTRY = ("prepare", "omissions", "discover")
HV_DATA = ("report.schema.json",)
LICENSE = "LICENSE"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def imports(source, known):
    """-> the modules of `known` that a Python source imports."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = [node.module]
        else:
            continue
        found.update(n.split(".")[0] for n in names if n.split(".")[0] in known)
    return found


def closure(read, known, entry=HV_ENTRY):
    """-> the sorted modules reachable from `entry`. read(name) -> the source text of module `name`."""
    seen, todo = set(), list(entry)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        if name not in known:
            raise SystemExit("module %s.py is not in %s" % (name, HV))
        seen.add(name)
        todo += sorted(imports(read(name), known) - seen)
    return sorted(seen)


def git(repo, *args):
    done = subprocess.run(["git", "-C", repo] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if done.returncode:
        raise SystemExit("git %s failed: %s" % (" ".join(args), done.stderr.decode("utf-8", "replace").strip()))
    return done.stdout


def update(repo, commit, dest=DEST):
    sha = git(repo, "rev-parse", "--verify", commit + "^{commit}").decode().strip()
    show = lambda path: git(repo, "show", "%s:%s" % (sha, path))
    known = {n[:-3] for n in git(repo, "ls-tree", "--name-only", "%s:%s" % (sha, HV)).decode().split() if n.endswith(".py")}
    modules = closure(lambda m: show("%s/%s.py" % (HV, m)).decode("utf-8"), known)
    wanted = ["%s/%s" % (CH, f) for f in CH_FILES] + ["%s/%s.py" % (HV, m) for m in modules] + ["%s/%s" % (HV, f) for f in HV_DATA]
    blobs = {path: show(path) for path in wanted}
    blobs[LICENSE] = show(LICENSE)
    try:  # a file the new commit no longer needs must not stay behind: remove what the old manifest listed, nothing else
        with open(os.path.join(dest, "VENDOR.json"), encoding="utf-8") as fh:
            for path in json.load(fh)["files"]:
                if os.path.isfile(os.path.join(dest, path)):
                    os.remove(os.path.join(dest, path))
    except (OSError, ValueError, KeyError):
        pass
    for path, data in blobs.items():
        target = os.path.join(dest, path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(data)
    manifest = {"source": SOURCE, "commit": sha, "files": {p: sha256(d) for p, d in sorted(blobs.items())}}
    with open(os.path.join(dest, "VENDOR.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)
        fh.write("\n")
    print("vendored %d files from %s at %s" % (len(blobs), SOURCE, sha))
    return 0


def check(dest=DEST):
    """-> the problems found in the copy (an empty list when it is the pinned one)."""
    manifest_path = os.path.join(dest, "VENDOR.json")
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        listed = manifest["files"]
    except (OSError, ValueError, KeyError) as exc:
        return ["VENDOR.json is missing or unreadable (%s)" % exc]
    problems = []
    for path, want in sorted(listed.items()):
        try:
            with open(os.path.join(dest, path), "rb") as fh:
                got = sha256(fh.read())
        except OSError:
            problems.append("missing: %s" % path)
            continue
        if got != want:
            problems.append("changed: %s" % path)
    present = set()
    for root, dirs, names in os.walk(dest):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for n in names:
            present.add(os.path.relpath(os.path.join(root, n), dest))
    for extra in sorted(present - set(listed) - {"VENDOR.json"}):
        problems.append("not in VENDOR.json: %s" % extra)
    return problems


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", help="a checkout of the jev repo (any worktree of it)")
    ap.add_argument("--commit", help="the commit to copy from; with --repo")
    ap.add_argument("--check", action="store_true", help="verify the copy against VENDOR.json (default)")
    ap.add_argument("--dest", default=DEST, help="the copy to refresh or verify (default: scripts/vendor/jev-handoff)")
    args = ap.parse_args(argv)
    if args.repo or args.commit:
        if not (args.repo and args.commit) or args.check:
            ap.error("--repo and --commit go together and exclude --check")
        return update(args.repo, args.commit, args.dest)
    problems = check(args.dest)
    for p in problems:
        print(p)
    print("jev-handoff: %s" % ("OK" if not problems else "%d problem(s)" % len(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
