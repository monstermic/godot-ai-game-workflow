# Workflow repository instructions

This repository publishes the portable workflow itself. `src/aigame` owns executable behavior; `src/aigame/schemas` owns public data contracts; `game-template` is the Godot runtime fixture; `.github/workflows` owns independent CI and release gates.

Use Python 3.11+, write behavior tests before implementation, and run:

```text
python -m unittest discover -s tests -v
godot --headless --path game-template --script res://tests/run_tests.gd
```

All external GitHub Actions must remain pinned to full 40-character commits. The workflow template must never contain Git LFS objects. Do not weaken human merge, independent review, environment approval, media provenance, or same-artifact release controls.
