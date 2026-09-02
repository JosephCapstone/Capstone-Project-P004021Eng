# Capstone-Project-P004021Eng

GitHub repository for DELTA.

## Joseph mapping UI

The mapping-enabled UI is kept separate from the original DeltaUI:

- `QBot_Platform/DeltaUI` is the unchanged original application.
- `QBot_Platform/DeltaUI_Joseph` adds QBot/recording state checks, live 2D
  mapping, map preview, save/cancel controls, and the WSL mapping worker.

Start with [LAB_QUICK_START.md](LAB_QUICK_START.md). The full Windows, WSL,
Jetson, Foxglove, and troubleshooting procedure is in
[docs/live_mapping_lab_guide.md](docs/live_mapping_lab_guide.md), while the
backend and state model are documented in
[docs/delta_ui_mapping.md](docs/delta_ui_mapping.md).

The existing Jetson `run_qbot.sh` and recording workflow remain unchanged.
