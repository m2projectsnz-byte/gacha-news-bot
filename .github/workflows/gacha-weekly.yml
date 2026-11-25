# Place this file at: .github/workflows/gacha-weekly.yml   (replace the old one)
name: Gacha news bot

on:
  schedule:
    - cron: "0 3 * * *"   # every day 03:00 UTC (12:00 JST). Collects once per week, posts a share each day.
  workflow_dispatch:
    inputs:
      command:
        description: "daily = normal run / check = only test the RSS feeds in feeds.txt"
        type: choice
        options:
          - daily
          - check
        default: daily
      dry_run:
        description: "Print posts only (nothing is posted to Bluesky)"
        type: boolean
        default: true

permissions:
  contents: write

concurrency:
  group: gacha-bot
  cancel-in-progress: false

jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - run: pip install -r requirements.txt

      - name: Run bot
        run: python gacha_bot_rss.py "$COMMAND"
        env:
          COMMAND: ${{ inputs.command || 'daily' }}
          BSKY_HANDLE: ${{ secrets.BSKY_HANDLE }}
          BSKY_APP_PASSWORD: ${{ secrets.BSKY_APP_PASSWORD }}
          # Repository variable. Leave unset (or "true") while reviewing; set to "false" for full automation.
          REQUIRE_APPROVAL: ${{ vars.REQUIRE_APPROVAL || 'true' }}
          DRY_RUN: ${{ github.event_name == 'workflow_dispatch' && inputs.dry_run || 'false' }}

      - name: Save state
        if: always()
        run: |
          git config user.name "gacha-bot"
          git config user.email "gacha-bot@users.noreply.github.com"
          git add state
          if ! git diff --cached --quiet; then
            git commit -m "Update bot state [skip ci]"
            git pull --rebase
            git push
          fi
