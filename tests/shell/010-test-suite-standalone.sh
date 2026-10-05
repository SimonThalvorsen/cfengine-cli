#!/bin/bash

set -e
set -x

tmpdir=$(mktemp -d)
output_file=$(mktemp)
trap "rm -rf $tmpdir $output_file" EXIT

cp -r tests/shell/fixtures/010-test-suite-standalone/. "$tmpdir/"
cd "$tmpdir"

# --- Auto-discovery: bare `cfengine test`, no arguments ---
# Two files, each with two check bundles sharing the same acted-upon data
if cfengine test >"$output_file" 2>&1; then
	cat "$output_file"
	echo "FAIL: expected the suite to fail (it has deliberately-failing assertions)"
	exit 1
fi
cat "$output_file"

grep -qF "cfengine test: 5 passed, 2 failed, 0 errors" "$output_file"

expected_rows=(
	"tests/test_service_config.cf::test_service_config_contents"
	"tests/test_service_config.cf::test_service_config_permissions"
	"tests/test_computed_metrics.cf::test_computed_metrics_equality"
	"tests/test_computed_metrics.cf::test_computed_metrics_comparison"
)
for row in "${expected_rows[@]}"; do
	grep -qF "$row" "$output_file" || {
		echo "FAIL: expected report row not found: $row"
		exit 1
	}
done

expected_fails=(
	"FAIL   tests/test_service_config.cf::test_service_config_permissions::config file is not world-writable (deliberately wrong check)  ->  /tmp/cfengine_test_suite_service.conf has permissions 640, expected 777"
	"FAIL   tests/test_computed_metrics.cf::test_computed_metrics_comparison::half life doubled is less than five (deliberately wrong)  ->  7.0 is not less than 5.0"
)
for line in "${expected_fails[@]}"; do
	grep -qF "$line" "$output_file" || {
		echo "FAIL: expected line not found in output: $line"
		exit 1
	}
done

# --- Explicit directory argument ---
if cfengine test tests/ >"$output_file" 2>&1; then
	cat "$output_file"
	echo "FAIL: expected the suite to fail when passed as an explicit directory too"
	exit 1
fi
grep -qF "cfengine test: 5 passed, 2 failed, 0 errors" "$output_file"
