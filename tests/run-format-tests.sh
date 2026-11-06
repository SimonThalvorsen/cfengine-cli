#!/usr/bin/env bash

set -e
# set -x

echo "These tests expect cfengine CLI to be installed globally or in venv"

echo "Looking for CFEngine CLI:"
cfengine --version

echo "Check that input and expected files exist:"
ls -al tests/format/*.input.cf
ls -al tests/format/*.expected.cf

rm -rf tmp
mkdir -p tmp

echo "Check that input files match expected files:"
ls tests/format/*.input.cf > tmp/format-input-files.log
ls tests/format/*.expected.cf > tmp/format-expected-files.log
sed s/.input.cf/.cf/g tmp/format-input-files.log > tmp/a.log
sed s/.expected.cf/.cf/g tmp/format-expected-files.log > tmp/b.log
diff -u tmp/a.log tmp/b.log

echo "Run formatting tests:"
for file in tests/format/*.input.cf; do
  expected="$(echo $file | sed s/.input.cf/.expected.cf/g)"
  output="$(echo $file | sed s/.input.cf/.output.cf/g)"
  cat $file | cfengine format - > $output
  diff -u $expected $output
  echo "OK: $file -> $expected"
done

echo "All formatting tests successful!"
