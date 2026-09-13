#!/usr/bin/env bash
# Script one-shot per pubblicare loki-cli su GitHub.
# Presuppone: gh CLI installato e gia' autenticato (gh auth login fatto).
# Lancia con:   bash push-to-github.sh
set -e

REPO_NAME="${REPO_NAME:-loki}"
VISIBILITY="${VISIBILITY:-public}"   # oppure: private
GIT_NAME="${GIT_NAME:-outlust}"
GIT_EMAIL="${GIT_EMAIL:-m4tt3ovaona@gmail.com}"

cd "$(dirname "$0")"

echo "▶ 1/5 · Verifica gh auth"
if ! gh auth status >/dev/null 2>&1; then
  echo "  ✗ gh non autenticato. Lancia: gh auth login"
  exit 1
fi
GH_USER="$(gh api user --jq .login)"
echo "  ✓ autenticato come $GH_USER"

echo "▶ 2/5 · git init (se serve)"
if [ ! -d .git ]; then
  git init -b main
  echo "  ✓ repo inizializzato (branch main)"
else
  echo "  ✓ repo git gia' presente"
fi

echo "▶ 3/5 · Identity locale al repo"
git config user.name  "$GIT_NAME"
git config user.email "$GIT_EMAIL"
echo "  ✓ name=$GIT_NAME  email=$GIT_EMAIL"

echo "▶ 4/5 · Add + commit"
git add .
if git diff --cached --quiet; then
  echo "  ✓ niente da committare"
else
  git commit -m "initial commit: Loki shell-agent CLI"
  echo "  ✓ commit creato"
fi

echo "▶ 5/5 · Crea repo $VISIBILITY su GitHub e push"
if gh repo view "$GH_USER/$REPO_NAME" >/dev/null 2>&1; then
  echo "  ! repo $GH_USER/$REPO_NAME esiste gia', aggiungo solo il remote e faccio push"
  git remote remove origin 2>/dev/null || true
  git remote add origin "https://github.com/$GH_USER/$REPO_NAME.git"
  git branch -M main
  git push -u origin main
else
  gh repo create "$REPO_NAME" --"$VISIBILITY" --source=. --push \
    --description "Loki - CLI shell-agent AI locale (Ollama + prompt_toolkit)"
fi

echo
echo "════════════════════════════════════════════"
echo "  ✓ Pubblicato: https://github.com/$GH_USER/$REPO_NAME"
echo "════════════════════════════════════════════"
