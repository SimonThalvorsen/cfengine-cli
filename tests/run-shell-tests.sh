#/usr/bin/env bash

set -e
# set -x
export CFBS_USER_AGENT=CI # this user agent will be excluded from the build modules statistics

echo "These tests expect cfengine CLI to be installed globally or in venv"

echo "Looking for CFEngine CLI:"
cfengine --version

echo "Check that test files are in expected location:"
ls -al tests/shell/*.sh
ls -al tests/shell/00*.sh

rm -rf tmp
mkdir -p tmp

export GIT_CONFIG_GLOBAL="$(pwd)/tmp/gitconfig"
export GIT_CONFIG_SYSTEM=/dev/null
git config --global user.name "test-runner[bot]"
git config --global user.email "test_runner@bot"

echo "Run shell tests:"
for file in tests/shell/*.sh; do
  bash $file
  echo "OK: $file"
done

echo "All shell tests successful!"
