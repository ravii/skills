#!/usr/bin/env bash
# Installs what scripts/confluence.py needs, on Ubuntu / Debian (WSL included).
#
#   bash scripts/install_deps.sh                 everything
#   bash scripts/install_deps.sh --no-plantuml   only the Python part (no sudo)
#
# 1. <skill>/.venv with the pinned "markdown" package (no sudo). confluence.py
#    switches to this venv by itself when the calling python lacks the package.
# 2. plantuml for the diagram syntax check (sudo): Java runtime + Graphviz from
#    apt, and the pinned upstream jar (the Ubuntu package is years older),
#    checksum-verified, behind /usr/local/bin/plantuml.
# Safe to run again: finished steps are skipped.

set -euo pipefail

MARKDOWN_VERSION="3.11.3"
PLANTUML_VERSION="1.2026.10"
PLANTUML_SHA256="e9bc9e6b2eb715e8c0c929f3dcb5fc1923be13f37599b09c293c66d354e88686"
PLANTUML_DIR=/opt/plantuml

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$SKILL_DIR/.venv"
with_plantuml=1
[[ "${1:-}" == "--no-plantuml" ]] && with_plantuml=0

# ---- 1. Python ---------------------------------------------------------------
if ! "$VENV/bin/python" -c "import markdown" 2>/dev/null; then
    if ! python3 -m venv "$VENV" 2>/dev/null; then
        echo "python3-venv is missing: sudo apt-get install -y python3-venv, then run again" >&2
        exit 1
    fi
    "$VENV/bin/pip" install --quiet --disable-pip-version-check "markdown==$MARKDOWN_VERSION"
fi
echo "python:   $VENV/bin/python (markdown $("$VENV/bin/python" -c 'import markdown; print(markdown.__version__)'))"

# ---- 2. PlantUML -------------------------------------------------------------
[[ $with_plantuml -eq 1 ]] || exit 0
jar="$PLANTUML_DIR/plantuml-$PLANTUML_VERSION.jar"
if echo "$PLANTUML_SHA256  $jar" | sha256sum -c --status 2>/dev/null && command -v java >/dev/null &&
    command -v dot >/dev/null && [[ -x /usr/local/bin/plantuml ]]; then
    echo "plantuml: $(plantuml -version | head -1)"
    exit 0
fi

echo "installing Java, Graphviz and PlantUML $PLANTUML_VERSION (needs sudo) ..."
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    default-jre-headless graphviz curl ca-certificates
if ! echo "$PLANTUML_SHA256  $jar" | sha256sum -c --status 2>/dev/null; then
    tmp="$(mktemp)"
    curl -fsSL --retry 3 -o "$tmp" \
        "https://github.com/plantuml/plantuml/releases/download/v$PLANTUML_VERSION/plantuml-$PLANTUML_VERSION.jar"
    echo "$PLANTUML_SHA256  $tmp" | sha256sum -c --quiet
    sudo install -D -m 644 "$tmp" "$jar"
    rm -f "$tmp"
fi
printf '#!/bin/sh\nexec java -Djava.awt.headless=true -jar %s "$@"\n' "$jar" |
    sudo tee /usr/local/bin/plantuml >/dev/null
sudo chmod 755 /usr/local/bin/plantuml
echo "plantuml: $(plantuml -version | head -1)"
