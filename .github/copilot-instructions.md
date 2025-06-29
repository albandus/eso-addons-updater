# ESO Addons Updater - GitHub Copilot Instructions

## Project Overview
This is an Elder Scrolls Online (ESO) addon updater tool written in Python. It
automatically check installed plugins versions against those available on ESOUI
website and updates them if necessary.

## Coding Guidelines

Do not bother with imports order / unused, just run:
`poetry run ruff format` and `poetry run ruff check --fix`
Check command output results, but in most cases ruff fixes all issues.

## Command Line Usage

```bash
poetry run main --action [list|update]
```

