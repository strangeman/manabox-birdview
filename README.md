# ManaBox Collection birdview

## What is it

The main idea behind this script is to get a way to manage my ManaBox MTG collection from a bird's-eye view. It is hard to figure out how the cards are distributed across boxes and binders (in terms of color, rarity and set) from the ManaBox interface, so I wrote this visualizer. It takes a CSV export from ManaBox and renders a static HTML page with the needed information.

![sample screenshot](./examples/screenshot.png)

## Usage

- Export CSV from your ManaBox (by default the script expects the file name `ManaBox_Collection.csv`)
- Install the required dependencies from `requirements.txt`
- Run `python report.py`
- Open the resulting `collection_report.html` in a browser

## Useful docs

- [spec.md](spec.md) - project specification
- [CLAUDE.md](CLAUDE.md) - implementation instructions for Claude Code (but can be useful for humans too)
- [collection_report.html](./examples/collection_report.html) - sample report
