#!/bin/bash

set -e
set -x

tmpdir=$(mktemp -d)
output_file=$(mktemp)
trap "rm -rf $tmpdir $output_file" EXIT

cd "$tmpdir"

# A plain cfbs policy-set project built on the standard community
# masterfiles -- no assert-specific setup needed, `cfengine test` auto-injects
# the assert: promise type into every test run on its own.
cat >cfbs.json <<'EOF'
{
  "name": "Example project",
  "description": "Example description",
  "type": "policy-set",
  "git": false,
  "build": [
    {
      "name": "masterfiles",
      "description": "Official CFEngine Masterfiles Policy Framework (MPF)",
      "url": "https://github.com/cfengine/masterfiles",
      "commit": "1ac0cef8c592bfca1ce0e842744b354f90b5759b",
      "branch": "master",
      "added_by": "cfbs init",
      "steps": ["run ./prepare.sh -y", "copy ./ ./"]
    }
  ]
}
EOF

# A locally-authored module (discovered via */main.cf, same as a real cfbs
# project's own custom modules), and a single test showing the setup(act)/
# assert flow: act by calling into the project's own bundle, then assert on
# the state it left behind.
mkdir -p app_module tests
cat >app_module/main.cf <<'EOF'
bundle agent configure_app
{
  files:
    "/tmp/cfengine_test_suite_app.conf"
      create  => "true",
      content => "listen_port=8080",
      perms   => app_perms;
}

body perms app_perms
{
  mode => "0644";
}
EOF

cat >tests/test_app_config.cf <<'EOF'
bundle agent test_app_config
{
  methods:
    "configure" usebundle => configure_app;

  assert:
    "app config file exists"
      file   => "/tmp/cfengine_test_suite_app.conf",
      exists => "true";

    "app config file is not world-writable (deliberately wrong check)"
      file  => "/tmp/cfengine_test_suite_app.conf",
      perms => "0777";
}
EOF

if cfengine test >"$output_file" 2>&1; then
	cat "$output_file"
	echo "FAIL: expected the cfbs-project suite to fail (it has a deliberately-failing assertion)"
	exit 1
fi
cat "$output_file"

grep -qF "cfengine test: 1 passed, 1 failed, 0 errors" "$output_file"
grep -qF "tests/test_app_config.cf::test_app_config" "$output_file"
grep -qF "FAIL   tests/test_app_config.cf::test_app_config::app config file is not world-writable (deliberately wrong check)  ->  /tmp/cfengine_test_suite_app.conf has permissions 644, expected 777" "$output_file"
