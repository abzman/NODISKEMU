#!/usr/bin/env python3
"""Run fixtests.py against NODISKEMU 1.2.0 and against this tree, side by side.

    python3 testcode/fixes-1.2.0/before_after.py              # 1.2.0 vs. this tree
    python3 testcode/fixes-1.2.0/before_after.py --per-commit # every commit in between

The "before" tree is checked out from git into a temporary worktree
(default: commit 2b10add, "Release 1.2.0"); nothing in your working copy
is changed. Needs git, python3 and gcc.

Exit code 0 means: every test fails before and passes after.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTS = HERE / 'fixtests.py'
RELEASE_1_2_0 = '2b10add78a6dbae3403787e6ecbd0f235da0bab9'


def git(*args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def run_tests(root):
    out = subprocess.run([sys.executable, str(TESTS), '--root', str(root), '--json'],
                         capture_output=True, text=True)
    if out.returncode not in (0, 1) or not out.stdout.strip():
        sys.exit(f'fixtests.py failed on {root}:\n{out.stderr}')
    return json.loads(out.stdout)


def checkout(repo, ref, path):
    git('worktree', 'add', '--detach', '-f', str(path), ref, cwd=repo)


def remove(repo, path):
    subprocess.run(['git', 'worktree', 'remove', '--force', str(path)], cwd=repo, capture_output=True)


def side_by_side(before, after, before_ref):
    w = max(len(t[1]) for t in after) + 6
    print(f'{"test":<{w}} {"1.2.0":6} {"fixed":6}')
    print('-' * (w + 14))
    good = True
    for b, a in zip(before, after):
        label = f'#{str(a[0]):<4} {a[1]}'
        print(f'{label:<{w}} {b[2]:6} {a[2]:6}')
        if a[2] == 'INFO':
            print(f'{"":<6} {a[3]}')
            continue
        if b[2] != 'PASS':
            print(f'{"":<6} before: {b[3]}')
        if a[2] != 'PASS':
            print(f'{"":<6} AFTER:  {a[3]}')
        good &= a[2] == 'PASS' and b[2] == 'FAIL'
    n = sum(t[2] != 'INFO' for t in after)
    print('-' * (w + 14))
    print(f'1.2.0 ({before_ref[:7]}): {sum(t[2] == "PASS" for t in before)} of {n} pass   '
          f'fixed: {sum(t[2] == "PASS" for t in after)} of {n} pass')
    print('RESULT: every fixed bug fails before and passes after' if good else
          'RESULT: unexpected outcome, see above')
    return good


def per_commit(repo, before_ref, after_ref):
    commits = [before_ref] + git('rev-list', '--reverse', f'{before_ref}..{after_ref}', cwd=repo).split()
    rows = []
    with tempfile.TemporaryDirectory(prefix='nodiskemu-steps-') as tmp:
        wt = Path(tmp) / 'tree'
        checkout(repo, commits[0], wt)
        try:
            for c in commits:
                git('checkout', '-q', '--detach', c, cwd=wt)
                subj = git('log', '-1', '--format=%s', c, cwd=repo)
                rows.append((c, subj, run_tests(wt)))
                print(f'  tested {c[:7]} {subj[:60]}', file=sys.stderr)
        finally:
            remove(repo, wt)
    sym = {'PASS': 'P', 'FAIL': '.', 'INFO': 'i', 'ERROR': 'E', 'SKIP': 's'}
    print('\nOne column per commit (first column = 1.2.0). P = pass, . = fail\n')
    for i, c in enumerate(commits):
        print(f'  col {i:2}: {c[:7]} {rows[i][1][:70]}')
    print()
    for t in range(len(rows[0][2])):
        commit, title = rows[0][2][t][0], rows[0][2][t][1]
        seq = ''.join(sym[r[2][t][2]] for r in rows)
        print(f'#{str(commit):<4} {title[:52]:53} {seq}')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--before', default=RELEASE_1_2_0, help='git ref of the unfixed tree (default: 1.2.0)')
    ap.add_argument('--after', default=None, help='git ref of the fixed tree (default: working copy)')
    ap.add_argument('--per-commit', action='store_true', help='also test every commit in between')
    a = ap.parse_args()
    repo = Path(git('rev-parse', '--show-toplevel', cwd=HERE))
    before_ref = git('rev-parse', a.before, cwd=repo)

    with tempfile.TemporaryDirectory(prefix='nodiskemu-before-') as tmp:
        wt = Path(tmp) / 'tree'
        checkout(repo, before_ref, wt)
        try:
            before = run_tests(wt)
        finally:
            remove(repo, wt)
    if a.after:
        with tempfile.TemporaryDirectory(prefix='nodiskemu-after-') as tmp:
            wt = Path(tmp) / 'tree'
            checkout(repo, a.after, wt)
            try:
                after = run_tests(wt)
            finally:
                remove(repo, wt)
    else:
        after = run_tests(repo)

    good = side_by_side(before, after, before_ref)
    if a.per_commit:
        per_commit(repo, before_ref, a.after or 'HEAD')
    return 0 if good else 1


if __name__ == '__main__':
    sys.exit(main())
