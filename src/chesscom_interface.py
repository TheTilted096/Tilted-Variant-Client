"""Chess.com interface for interacting with the game board."""
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains


class ChessComInterface:
    """Handles interaction with chess.com game interface."""

    def __init__(self, driver):
        """
        Initialize the chess.com interface.

        Args:
            driver: Selenium WebDriver instance
        """
        self.driver = driver
        self.wait = WebDriverWait(driver, 10)

    def is_board_flipped(self):
        """
        Detect if the board is flipped (Black on bottom).

        Returns:
            bool: True if board is flipped (playing as Black), False otherwise
        """
        js_script = """
        // Method 1: Check for flipped class
        const board = document.querySelector('.TheBoard-squares') ||
                     document.querySelector('[class*="Board-squares"]') ||
                     document.querySelector('.board');

        if (board && board.className.includes('flipped')) {
            return true;
        }

        // Method 2: Check player color - look for evaluation bar or player info
        const playerBottom = document.querySelector('[class*="player-bottom"]') ||
                            document.querySelector('[class*="player"][class*="white"]');

        if (playerBottom && playerBottom.className.includes('black')) {
            return true;
        }

        // Method 3: Check coordinates if visible
        const coordSquares = document.querySelectorAll('[class*="coordinate"]');
        for (let coord of coordSquares) {
            if (coord.textContent === '1' && coord.className.includes('top')) {
                return true; // rank 1 on top means flipped
            }
        }

        // Default to not flipped (White on bottom)
        return false;
        """

        try:
            return self.driver.execute_script(js_script)
        except Exception as e:
            print(f"[ChessCom] Error detecting board orientation: {e}")
            return False  # Default to not flipped

    def debug_board(self):
        """Debug helper to inspect the board structure."""
        js_script = """
        const boards = document.querySelectorAll('[class*="board"]');
        const squares = document.querySelectorAll('[class*="square"]');
        const pieces = document.querySelectorAll('[class*="piece"]');

        return {
            boardCount: boards.length,
            squareCount: squares.length,
            pieceCount: pieces.length,
            boardClasses: boards.length > 0 ? boards[0].className : 'none',
            sampleSquareClasses: squares.length > 0 ?
                Array.from(squares).slice(0, 5).map(s => s.className) : [],
            samplePieceClasses: pieces.length > 0 ?
                Array.from(pieces).slice(0, 3).map(p => p.className) : []
        };
        """
        try:
            info = self.driver.execute_script(js_script)
            print("[Debug] Board inspection:")
            print(f"  Boards found: {info.get('boardCount', 0)}")
            print(f"  Squares found: {info.get('squareCount', 0)}")
            print(f"  Pieces found: {info.get('pieceCount', 0)}")
            print(f"  Board classes: {info.get('boardClasses', 'none')}")
            print(f"  Sample square classes: {info.get('sampleSquareClasses', [])}")
            print(f"  Sample piece classes: {info.get('samplePieceClasses', [])}")
            return info
        except Exception as e:
            print(f"[Debug] Error inspecting board: {e}")
            return None

    def get_square_coordinates(self, square):
        """
        Get the pixel coordinates of a square on the chess.com board.

        Args:
            square: Square in UCI format (e.g., 'e2', 'd4')

        Returns:
            dict: {'x': x_coord, 'y': y_coord} or None if not found
        """
        file_letter = square[0]
        rank_number = square[1]

        # Convert file letter to number (a=1, b=2, ..., h=8)
        file_num = ord(file_letter) - ord('a') + 1

        # Calculate coordinates from board dimensions
        # Chess.com variants uses 'square light wb' / 'square dark wb' without coordinate info

        # Detect board orientation
        is_flipped = self.is_board_flipped()

        js_script = f"""
        // Find the chess board
        const board = document.querySelector('.TheBoard-squares') ||
                     document.querySelector('[class*="Board-squares"]') ||
                     document.querySelector('.board') ||
                     document.querySelector('[class*="board"]');

        if (!board) {{
            console.log('Could not find board element');
            return null;
        }}

        const rect = board.getBoundingClientRect();
        const squareSize = rect.width / 8;

        // Calculate position based on board orientation
        const isFlipped = {str(is_flipped).lower()};

        let fileIndex, rankIndex;

        if (isFlipped) {{
            // Black on bottom: h-file is left (0), a-file is right (7)
            fileIndex = 8 - {file_num};  // h=0, g=1, ..., a=7
            // Rank 8 at bottom (7), rank 1 at top (0)
            rankIndex = {rank_number} - 1;  // 1=0, 2=1, ..., 8=7
        }} else {{
            // White on bottom: a-file is left (0), h-file is right (7)
            fileIndex = {file_num} - 1;  // a=0, b=1, ..., h=7
            // Rank 8 at top (0), rank 1 at bottom (7)
            rankIndex = 8 - {rank_number};  // 8=0, 7=1, ..., 1=7
        }}

        const x = rect.left + (fileIndex * squareSize) + (squareSize / 2);
        const y = rect.top + (rankIndex * squareSize) + (squareSize / 2);

        return {{
            x: x,
            y: y,
            method: 'calculated',
            flipped: isFlipped,
            debug: {{
                boardRect: {{ left: rect.left, top: rect.top, width: rect.width, height: rect.height }},
                squareSize: squareSize,
                fileIndex: fileIndex,
                rankIndex: rankIndex
            }}
        }};
        """

        try:
            coords = self.driver.execute_script(js_script)
            if coords:
                method = coords.get('method', 'unknown')
                print(f"[ChessCom] Found {square} using {method} method")
            return coords
        except Exception as e:
            print(f"[ChessCom] Error getting coordinates for {square}: {e}")
            return None

    def make_move(self, uci_move):
        """
        Make a move on the chess.com board using UCI notation.
        Uses drag-and-drop to simulate natural piece movement.

        Args:
            uci_move: Move in UCI format (e.g., 'e2e4', 'd7d5')

        Returns:
            bool: True if move was successful, False otherwise
        """
        print(f"[ChessCom] Attempting to make move: {uci_move}")

        try:
            # Parse UCI move
            from_square = uci_move[:2]
            to_square = uci_move[2:4]

            print(f"[ChessCom] Move: {from_square} -> {to_square}")

            # Get coordinates for both squares
            from_coords = self.get_square_coordinates(from_square)
            to_coords = self.get_square_coordinates(to_square)

            if not from_coords or not to_coords:
                print(f"[ChessCom] Could not find board squares")
                return False

            print(f"[ChessCom] From coords: {from_coords}")
            print(f"[ChessCom] To coords: {to_coords}")

            # Use ActionChains with absolute viewport coordinates
            # This is more reliable than element-relative offsets
            actions = ActionChains(self.driver)

            # Method 1: Try using absolute coordinates via JavaScript click simulation
            js_move_script = f"""
                // Simulate drag and drop with mouse events
                const fromX = {from_coords['x']};
                const fromY = {from_coords['y']};
                const toX = {to_coords['x']};
                const toY = {to_coords['y']};

                // Find element at source position
                const fromElement = document.elementFromPoint(fromX, fromY);
                const toElement = document.elementFromPoint(toX, toY);

                if (!fromElement || !toElement) {{
                    return {{success: false, error: 'Could not find elements at coordinates'}};
                }}

                // Create and dispatch mouse events
                const mouseDown = new MouseEvent('mousedown', {{
                    bubbles: true,
                    cancelable: true,
                    clientX: fromX,
                    clientY: fromY,
                    button: 0
                }});

                const mouseMove = new MouseEvent('mousemove', {{
                    bubbles: true,
                    cancelable: true,
                    clientX: toX,
                    clientY: toY,
                    button: 0
                }});

                const mouseUp = new MouseEvent('mouseup', {{
                    bubbles: true,
                    cancelable: true,
                    clientX: toX,
                    clientY: toY,
                    button: 0
                }});

                fromElement.dispatchEvent(mouseDown);

                // Small delay for drag to register
                setTimeout(() => {{
                    toElement.dispatchEvent(mouseMove);
                    toElement.dispatchEvent(mouseUp);
                }}, 100);

                return {{
                    success: true,
                    from: fromElement.className,
                    to: toElement.className
                }};
            """

            # Try JavaScript-based move first
            result = self.driver.execute_script(js_move_script)
            print(f"[ChessCom] JS move result: {result}")

            # Also try Selenium ActionChains as fallback
            # Use move_by_offset from origin (0,0) instead of element-relative
            actions.move_by_offset(int(from_coords['x']), int(from_coords['y']))
            actions.click_and_hold()
            actions.pause(0.3)
            # Reset to origin and move to destination
            actions.move_by_offset(
                int(to_coords['x'] - from_coords['x']),
                int(to_coords['y'] - from_coords['y'])
            )
            actions.pause(0.3)
            actions.release()

            try:
                actions.perform()
            except Exception as action_error:
                print(f"[ChessCom] ActionChains error (expected, JS handles move): {action_error}")

            # Reset mouse position
            actions = ActionChains(self.driver)
            actions.move_by_offset(-int(to_coords['x']), -int(to_coords['y']))
            try:
                actions.perform()
            except:
                pass  # Ignore reset errors

            time.sleep(0.5)  # Wait for move to register

            print(f"[ChessCom] Move {uci_move} executed successfully")
            return True

        except Exception as e:
            print(f"[ChessCom] Error making move: {e}")
            import traceback
            traceback.print_exc()
            return False

