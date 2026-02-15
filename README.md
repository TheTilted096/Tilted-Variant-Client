# Tilted-Variant-Client

Client to play chess variants on Chess.com against friends using UCI-compatible engines.

**NOT FOR CHEATING** - This is for casual games with friends using variant engines like Chaturanga!

## Features

- Launches Edge browser with remote debugging on port 9223
- Connects to Chess.com variants via Chrome DevTools Protocol
- Terminal-based UCI move input interface
- Drag-and-drop move execution for natural piece movement
- Supports standard chess variants (8x8 board, no piece drops)
- Make moves by typing UCI notation (e.g., `d2d3`, `e2e4`)

## How It Works

1. **Browser Launch**: Starts Edge as a subprocess with `--remote-debugging-port=9223`
2. **Connection**: Selenium connects to the running Edge instance via the debugging port
3. **Move Execution**: Uses ActionChains to simulate drag-and-drop movements
4. **Coordinate Detection**: Finds board squares using Chess.com's square class names
5. **UCI Protocol**: Parses standard UCI move notation for piece movements

## Requirements

- Python 3.7+
- Microsoft Edge browser
- Edge WebDriver (auto-installed via webdriver-manager)

## Installation

1. Clone the repository:
```bash
git clone https://github.com/TheTilted096/Tilted-Variant-Client.git
cd Tilted-Variant-Client
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

## Usage

### Starting the Client

Run the client:
```bash
python run_client.py
```

### Workflow

1. Run the client with `python run_client.py`
2. Edge browser opens automatically and navigates to chess.com/variants
3. Log in to your Chess.com account in the browser
4. Start a variant game with a friend (e.g., Chaturanga)
5. Press Enter in the terminal when you're in the game
6. Type moves in UCI format (e.g., `e2e4`, `d2d3`)
7. Watch as the client executes the moves via drag-and-drop!

### Terminal Commands

- `e2e4` - Make a move (UCI format: source square + destination square)
- `debug` - Inspect board structure (useful for troubleshooting)
- `quit` - Exit the client

### UCI Move Format

Moves are in standard UCI notation:
- Source square (file + rank) + destination square (file + rank)
- Examples:
  - `e2e4` - Pawn from e2 to e4
  - `g1f3` - Knight from g1 to f3
  - `d7d8q` - Pawn promotion to queen

## Project Structure

```
Tilted-Variant-Client/
├── src/
│   ├── __init__.py
│   ├── variants_client.py      # Main client logic
│   ├── browser_launcher.py     # Edge browser automation
│   ├── chesscom_interface.py   # Chess.com interaction
│   └── uci_handler.py          # UCI protocol handling
├── engines/                     # Drop your engine executables here (coming soon)
├── run_client.py               # Launcher script
├── requirements.txt
└── README.md
```

## Future Enhancements

- Engine integration (drop your Chaturanga engine in `engines/` folder)
- Auto-play mode with engine communication
- Support for piece drops and non-standard variants
- Game state synchronization
- Move validation and feedback

## Troubleshooting

### Browser doesn't open
- Ensure Microsoft Edge is installed
- Close any existing Edge windows before starting
- Check that no other process is using port 9223
- On Linux, ensure microsoft-edge is in your PATH
- Try running with administrator/sudo privileges if needed

### Moves don't work
- Make sure you're in an active game
- Verify it's your turn to move
- Check that the move is legal in the current position
- Ensure the browser window is not minimized

### WebDriver errors
- Update Edge browser to the latest version
- Clear the webdriver-manager cache: `pip uninstall webdriver-manager && pip install webdriver-manager`

## Sister Project

Based on the structure of wilted-chesscom-client.

## License

See LICENSE file for details.

## Disclaimer

This tool is for educational purposes and casual play with friends only. Do not use this to cheat in rated games or tournaments.
