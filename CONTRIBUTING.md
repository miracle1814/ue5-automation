# Contributing

Thanks for your interest!

## Report bugs

Open an issue with:

- Engine version (e.g. `5.3.1`) and editor UI language
- What you asked the agent / which command you ran
- The full error text (from the tool response or `Saved/Logs/<Project>.log`)

Version-specific build issues for the C++ bridge: include your compiler/toolchain
versions and the output of `bridge/Source/build_for_engine.bat`.

## Development

The offline test suite runs without an editor:

```cmd
python -m pytest skill/ue5-automation/tests --ignore=skill/ue5-automation/tests/test_analyzer_phase2.py
```

Core rules (enforced by review):

1. **Never report false success.** Every write must be read back and compared;
   a mismatch is a loud error, not a warning.
2. All `unreal.*` access from request threads goes through the GameThread
   queue/tick-worker (`ue5_bridge.py`). Never touch engine objects directly.
3. Host-side Python must stay compatible with the oldest supported embedded
   interpreter (UE 5.1 ships Python 3.9).
4. New commands: whitelist entry + docs entry + tests, or they don't exist.

## License

Contributions are licensed under Apache-2.0 together with the project.
