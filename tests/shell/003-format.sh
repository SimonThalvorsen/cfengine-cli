#!/bin/bash

set -e
set -x

# Verify that the line length parameter shows up in help
cfengine format -h | grep -- "--line-length"

# Test three common line lengths
for ll in 80 100 120; do
	# Format the following HEREDOC and let wc count the longest line
	l=$(cfengine format --line-length $ll - <<-EOF |
	bundle agent slists
	{
	vars:
	"variable_name"
	slist => { "one", "two", "three", "four", "five", "six" };
	"variable_name"
	slist => { "one", "two", "three", "four", "five", "six", "seven" };
	"variable_name"
	slist => {
	"one", "two", "three", "four", "five", "six", "seven", "eight"
	};
	"variable_name"
	slist => {
	"one","two","three","four","five","six","seven","eight","nine","ten","eleven","twelve","thirteen",
	};
	"variable_name"
	slist => {
	"one","two","three","four","five","six","seven","eight","nine","ten","eleven","twelve","thirteen","fourteen","fifteen","sixteen","seventeen"
	};
	}
	EOF
	wc -L)
	# Verify that the actual longest line has less or equal characters
	[[ $l -le $ll ]]
done
