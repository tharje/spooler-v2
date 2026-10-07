#!/usr/bin/env bash
# Decides which image tags the "Publish Docker image" workflow pushes.
#
#   docker-tags.sh <github ref> <github ref name> <commit sha> <changelog version> [image]
#
# Prints the tags comma-separated, or exits 1 with a reason on stderr.
#
#   push to main            -> :latest and :<sha>                  (what has always been published)
#   tag v2.3.0              -> :2.3.0 and :latest                  (a normal release)
#   tag v2.3.0-beta.1       -> :2.3.0-beta.1 and :beta             (a pre-release; never :latest)
#
# Any version with a hyphen (-beta.1, -rc.1, ...) is a pre-release. The tag must
# look like vMAJOR.MINOR.PATCH[-prerelease] and must match the top entry of
# public/changelog.json (the version the app itself shows), so a tag on the
# wrong commit can't publish an image that says something else.
set -euo pipefail

ref="${1:?ref}"; name="${2:?ref name}"; sha="${3:?sha}"; changelog="${4:?changelog version}"
image="${5:-ghcr.io/tharje/spooler-v2}"

case "$ref" in
  refs/heads/main)
    echo "${image}:latest,${image}:${sha}"
    ;;
  refs/tags/v*)
    if [[ ! "$name" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z]+(\.[0-9A-Za-z]+)*)?$ ]]; then
      echo "Tag '$name' is not of the form vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-prerelease" >&2
      exit 1
    fi
    ver="${name#v}"
    if [[ "$ver" != "$changelog" && "$ver" != "$changelog".* ]]; then
      echo "Tag version '$ver' does not match the version in public/changelog.json ('$changelog')" >&2
      exit 1
    fi
    if [[ "$ver" == *-* ]]; then
      echo "${image}:${ver},${image}:beta"
    else
      echo "${image}:${ver},${image}:latest"
    fi
    ;;
  *)
    echo "Nothing is published for ref '$ref'" >&2
    exit 1
    ;;
esac
