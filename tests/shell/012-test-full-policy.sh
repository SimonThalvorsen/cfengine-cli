#!/bin/bash

set -e
set -x

tmpdir=$(mktemp -d)
output_file=$(mktemp)
trap "rm -rf $tmpdir $output_file" EXIT

cp -r tests/shell/fixtures/012-test-full-policy/. "$tmpdir/"
cd "$tmpdir"

# The project's real policy (services/init.cf, wired into the default
# bundlesequence via def.json's control_common_bundlesequence_end) writes a
# file whose contents depend on a variable with no project-level default --
# only a per-test fixture supplies it. By default, the real policy converges
# fresh inside each test's own container, using that test's own fixture.
# With --test-only, that bundle only runs once, during the shared
# setup/bootstrap step, with no fixture in scope, so the variable is never
# resolved.

# --- Default: the real bundle converges fresh per test file, using that
# test's own fixture, so both pass. ---
if ! cfengine test >"$output_file" 2>&1; then
	cat "$output_file"
	echo "FAIL: expected the suite to pass by default (full policy run)"
	exit 1
fi
cat "$output_file"
grep -qF "cfengine test: 2 passed, 0 failed, 0 errors" "$output_file"

# --- With --test-only: the real bundle only ran once, with no per-test
# fixture in scope, so neither test's expected content matches. ---
if cfengine test --test-only >"$output_file" 2>&1; then
	cat "$output_file"
	echo "FAIL: expected the suite to fail with --test-only"
	exit 1
fi
cat "$output_file"
grep -qF "cfengine test: 0 passed, 2 failed, 0 errors" "$output_file"
