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

    def focus_browser(self):
        """Bring the browser window to focus."""
        try:
            # Maximize and focus the window
            self.driver.maximize_window()
            # Execute JavaScript to focus the window
            self.driver.execute_script("window.focus();")
            # Small delay to ensure focus is transferred
            time.sleep(0.3)
            print("[ChessCom] Browser window focused")
        except Exception as e:
            print(f"[ChessCom] Warning: Could not focus browser: {e}")

    def get_turn(self):
        """
        Detect whose turn it is to move.

        Returns:
            str: 'white', 'black', or 'unknown'
        """
        js_script = """
        // Method 1: Look for turn indicator classes
        const board = document.querySelector('.TheBoard') ||
                     document.querySelector('[class*="board"]');

        if (board) {
            // Check if board has turn indicator
            if (board.className.includes('white')) return 'white';
            if (board.className.includes('black')) return 'black';
        }

        // Method 2: Look for highlighted/active player
        const playerActive = document.querySelector('[class*="player"][class*="active"]') ||
                            document.querySelector('[class*="player"][class*="turn"]');

        if (playerActive) {
            if (playerActive.className.includes('white')) return 'white';
            if (playerActive.className.includes('black')) return 'black';
        }

        // Method 3: Check for move input indicators
        const moveInput = document.querySelector('[class*="move-input"]');
        if (moveInput && moveInput.className.includes('white')) return 'white';
        if (moveInput && moveInput.className.includes('black')) return 'black';

        return 'unknown';
        """

        try:
            turn = self.driver.execute_script(js_script)
            return turn
        except Exception as e:
            print(f"[ChessCom] Error detecting turn: {e}")
            return 'unknown'

    def get_board_orientation(self):
        """
        Detect the board's orientation (which rank is at the top).

        This is SEPARATE from player color - in variants like Racing Kings,
        both colors can be on the bottom ranks.

        Returns:
            dict: {
                'is_flipped': bool,  # True if rank 1 at top (black perspective)
                'method': str,        # How it was detected
                'detail': str,        # Human-readable detail
                'debug': dict         # Debug info
            }
        """
        import time

        js_script = """
        const debug = { allLabels: [], nearLabels: [], boardInfo: null };

        // STEP 1: Find the MAIN game board
        const board = document.querySelector('.TheBoard-squares') ||
                     document.querySelector('[class*="Board-squares"]') ||
                     document.querySelector('.board') ||
                     document.querySelector('[class*="board"]');

        if (!board) {
            return {
                is_flipped: false,
                method: 'none',
                detail: 'board element not found',
                debug: debug
            };
        }

        const boardRect = board.getBoundingClientRect();
        debug.boardInfo = {
            top: Math.round(boardRect.top),
            left: Math.round(boardRect.left),
            width: Math.round(boardRect.width),
            height: Math.round(boardRect.height),
            hasFlippedClass: board.classList.contains('flipped')
        };

        // METHOD 1: Check for 'flipped' CSS class (like Wilted-Chess-Client)
        if (board.classList.contains('flipped')) {
            return {
                is_flipped: true,
                method: 'css-class',
                detail: 'board has "flipped" class',
                debug: debug
            };
        }

        // METHOD 2: Analyze coordinate labels
        const margin = 40; // Coordinate labels are right next to the board
        const allElements = Array.from(document.querySelectorAll('*'));
        const coordinates = [];

        for (let el of allElements) {
            const text = el.textContent?.trim();

            if ((text === '1' || text === '8') && el.textContent.length <= 3) {
                const rect = el.getBoundingClientRect();

                if (rect.width > 0 && rect.height > 0) {
                    const className = String(el.className || '').toLowerCase();
                    const labelInfo = {
                        text: text,
                        top: Math.round(rect.top),
                        left: Math.round(rect.left),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height),
                        className: el.className
                    };

                    // Track ALL labels for debugging
                    debug.allLabels.push(labelInfo);

                    // FILTER 1: Exclude UI elements (notifications, icons, badges)
                    const isUIElement = className.includes('notification') ||
                                       className.includes('icon') ||
                                       className.includes('badge') ||
                                       className.includes('button') ||
                                       className.includes('menu');

                    if (isUIElement) {
                        labelInfo.excluded = 'UI element';
                        continue;
                    }

                    // FILTER 2: Must be near the board (within 40px)
                    const nearBoard =
                        Math.abs(rect.left - boardRect.left) < margin ||
                        Math.abs(rect.right - boardRect.right) < margin ||
                        Math.abs(rect.top - boardRect.top) < margin ||
                        Math.abs(rect.bottom - boardRect.bottom) < margin;

                    if (nearBoard) {
                        debug.nearLabels.push(labelInfo);
                        coordinates.push({
                            text: text,
                            top: rect.top,
                            left: rect.left
                        });
                    }
                }
            }
        }

        // Determine orientation from topmost label near board
        if (coordinates.length >= 2) {
            coordinates.sort((a, b) => a.top - b.top);
            const topmost = coordinates[0];
            const bottommost = coordinates[coordinates.length - 1];

            debug.topmost = { text: topmost.text, top: Math.round(topmost.top) };
            debug.bottommost = { text: bottommost.text, top: Math.round(bottommost.top) };

            if (topmost.text === '1') {
                return {
                    is_flipped: true,
                    method: 'coordinate-labels',
                    detail: 'rank 1 at top (black perspective)',
                    debug: debug
                };
            } else if (topmost.text === '8') {
                return {
                    is_flipped: false,
                    method: 'coordinate-labels',
                    detail: 'rank 8 at top (white perspective)',
                    debug: debug
                };
            }
        }

        // Fallback: assume not flipped
        return {
            is_flipped: false,
            method: 'default',
            detail: 'assumed not flipped (no labels found)',
            debug: debug
        };
        """

        try:
            result = self.driver.execute_script(js_script)
            is_flipped = result.get('is_flipped', False)
            method = result.get('method', 'unknown')
            detail = result.get('detail', '')
            debug = result.get('debug', {})

            # Print detailed debug info
            print(f"\n{'='*60}")
            print(f"[Board Orientation Debug]")
            print(f"{'='*60}")

            board_info = debug.get('boardInfo')
            if board_info:
                print(f"Board element:")
                print(f"  Position: ({board_info['left']}, {board_info['top']})")
                print(f"  Size: {board_info['width']}x{board_info['height']}")
                print(f"  Has 'flipped' class: {board_info['hasFlippedClass']}")

            all_labels = debug.get('allLabels', [])
            print(f"\nAll '1' or '8' labels found: {len(all_labels)}")
            for i, label in enumerate(all_labels[:10], 1):  # Show first 10
                class_name = str(label.get('className', ''))[:40]
                excluded = label.get('excluded', '')
                status = f" [EXCLUDED: {excluded}]" if excluded else ""
                print(f"  {i}. '{label['text']}' at ({label['left']}, {label['top']}) "
                      f"[{label['width']}x{label['height']}] class='{class_name}'{status}")
            if len(all_labels) > 10:
                print(f"  ... and {len(all_labels) - 10} more")

            near_labels = debug.get('nearLabels', [])
            print(f"\nLabels NEAR board (within 40px): {len(near_labels)}")
            for i, label in enumerate(near_labels, 1):
                print(f"  {i}. '{label['text']}' at ({label['left']}, {label['top']})")

            topmost = debug.get('topmost')
            bottommost = debug.get('bottommost')
            if topmost and bottommost:
                print(f"\nTopmost label: '{topmost['text']}' at y={topmost['top']}")
                print(f"Bottommost label: '{bottommost['text']}' at y={bottommost['top']}")

            print(f"\n{'─'*60}")
            print(f"🎯 Board Orientation: {'FLIPPED' if is_flipped else 'NORMAL'} ({method})")
            print(f"   Detail: {detail}")

            # Show inferred player color (works for standard chess)
            player_color = 'BLACK' if is_flipped else 'WHITE'
            player_emoji = '♟️' if is_flipped else '♙'
            print(f"{player_emoji}  Player Color (inferred): {player_color}")
            print(f"{'='*60}\n")

            return result

        except Exception as e:
            print(f"[Board] Error detecting orientation: {e}")
            import traceback
            traceback.print_exc()
            return {
                'is_flipped': False,
                'method': 'error',
                'detail': str(e),
                'debug': {}
            }

    def get_player_color(self):
        """
        Detect which color the user is playing as.

        NOTE: In standard chess, this is the same as board orientation.
        But in variants (Racing Kings, etc.), it might be different.

        Returns:
            str: 'white', 'black', or 'unknown'
        """
        # For now, infer from board orientation (works for standard chess)
        orientation = self.get_board_orientation()

        if orientation['is_flipped']:
            print(f"♟️  [Player Color] You are playing: BLACK")
            return 'black'
        else:
            print(f"♙  [Player Color] You are playing: WHITE")
            return 'white'

    def is_board_flipped(self):
        """
        Detect if the board is flipped (rank 1 at top).

        Returns:
            bool: True if board is flipped, False otherwise
        """
        orientation = self.get_board_orientation()
        return orientation['is_flipped']

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

    def get_square_coordinates(self, square, is_flipped=None):
        """
        Get the pixel coordinates of a square on the chess.com board.
        Automatically adjusts for board flip (playing as Black).

        Args:
            square: Square in UCI format (e.g., 'e2', 'd4')
            is_flipped: Optional pre-computed board flip state (True/False).
                       If None, will detect automatically.

        Returns:
            dict: {'x': x_coord, 'y': y_coord} or None if not found
        """
        file_letter = square[0]
        rank_number = square[1]

        # Convert file letter to number (a=1, b=2, ..., h=8)
        file_num = ord(file_letter) - ord('a') + 1

        # Detect board orientation (only if not provided)
        if is_flipped is None:
            is_flipped = self.is_board_flipped()

        js_script = f"""
        // Find the chess board
        const board = document.querySelector('.TheBoard-squares') ||
                     document.querySelector('[class*="Board-squares"]') ||
                     document.querySelector('.board') ||
                     document.querySelector('[class*="board"]');

        if (!board) {{
            console.log('[Coords] Could not find board element');
            return null;
        }}

        const rect = board.getBoundingClientRect();
        const squareSize = rect.width / 8;
        const isFlipped = {str(is_flipped).lower()};

        let fileIndex, rankIndex;

        if (isFlipped) {{
            // BLACK ON BOTTOM (flipped board)
            // Visual layout: h8 h7 h6... (bottom-left) to a8 a7 a6... (bottom-right)
            //                h1 h2 h3... (top-left) to a1 a2 a3... (top-right)
            // Pixel coords (0,0) at top-left
            fileIndex = 8 - {file_num};  // h=0, g=1, f=2, ..., a=7
            rankIndex = {rank_number} - 1;  // 1=0, 2=1, ..., 8=7
            console.log('[Coords] FLIPPED: square={square} -> fileIndex=' + fileIndex + ', rankIndex=' + rankIndex);
        }} else {{
            // WHITE ON BOTTOM (normal board)
            // Visual layout: a1 b1 c1... (bottom-left) to h1 (bottom-right)
            //                a8 b8 c8... (top-left) to h8 (top-right)
            // Pixel coords (0,0) at top-left
            fileIndex = {file_num} - 1;  // a=0, b=1, c=2, ..., h=7
            rankIndex = 8 - {rank_number};  // 8=0, 7=1, ..., 1=7
            console.log('[Coords] NORMAL: {square} -> fileIndex=' + fileIndex + ', rankIndex=' + rankIndex);
        }}

        const x = rect.left + (fileIndex * squareSize) + (squareSize / 2);
        const y = rect.top + (rankIndex * squareSize) + (squareSize / 2);

        console.log('[Coords] Square {square}: (' + x.toFixed(1) + ', ' + y.toFixed(1) + ')');

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
            return coords
        except Exception as e:
            print(f"[ChessCom] Error getting coordinates for {square}: {e}")
            return None

    def make_move_cdp(self, uci_move):
        """
        Make a move using Chrome DevTools Protocol (CDP) input events.
        This is the EXACT equivalent of Puppeteer's page.mouse API.
        Works in background without window focus!

        Args:
            uci_move: Move in UCI format (e.g., 'e2e4', 'd7d5')

        Returns:
            bool: True if move was successful, False otherwise
        """
        try:
            # Parse UCI move
            from_square = uci_move[:2]
            to_square = uci_move[2:4]

            print(f"[ChessCom] Move: {from_square} → {to_square}")

            # Detect board orientation once (cache for this move)
            is_flipped = self.is_board_flipped()

            # Get coordinates for both squares (pass cached flip state)
            from_coords = self.get_square_coordinates(from_square, is_flipped)
            to_coords = self.get_square_coordinates(to_square, is_flipped)

            if not from_coords or not to_coords:
                print(f"[ChessCom] ✗ Could not find board squares")
                return False

            # Use CDP Input.dispatchMouseEvent (same as Puppeteer's page.mouse)
            try:
                # Move to source position
                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mouseMoved',
                    'x': from_coords['x'],
                    'y': from_coords['y']
                })
                time.sleep(0.03)  # 30ms like Puppeteer

                # Mouse down
                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mousePressed',
                    'x': from_coords['x'],
                    'y': from_coords['y'],
                    'button': 'left',
                    'clickCount': 1
                })
                time.sleep(0.05)

                # Drag to destination with steps (like Puppeteer's steps: 3)
                steps = 3
                for i in range(1, steps + 1):
                    x = from_coords['x'] + (to_coords['x'] - from_coords['x']) * i / steps
                    y = from_coords['y'] + (to_coords['y'] - from_coords['y']) * i / steps
                    self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                        'type': 'mouseMoved',
                        'x': x,
                        'y': y,
                        'button': 'left'
                    })
                    time.sleep(0.01)

                # Mouse up at destination
                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mouseReleased',
                    'x': to_coords['x'],
                    'y': to_coords['y'],
                    'button': 'left',
                    'clickCount': 1
                })

            except Exception as cdp_error:
                print(f"[ChessCom] ✗ CDP error: {cdp_error}")
                return False

            # Wait for move to process
            time.sleep(0.15)

            print(f"[ChessCom] ✓ Move executed")
            return True

        except Exception as e:
            print(f"[ChessCom] Error making move: {e}")
            import traceback
            traceback.print_exc()
            return False

    def make_move_js(self, uci_move):
        """
        Make a move using pure JavaScript event dispatch (Puppeteer-style).
        This method does NOT require window focus and can work in the background.

        Args:
            uci_move: Move in UCI format (e.g., 'e2e4', 'd7d5')

        Returns:
            bool: True if move was successful, False otherwise
        """
        print(f"[ChessCom] Attempting move with JS dispatch (background mode): {uci_move}")

        try:
            # Check whose turn it is
            turn = self.get_turn()
            print(f"[ChessCom] Current turn: {turn}")

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

            # Execute move using JavaScript event dispatch
            # This works WITHOUT window focus (like Puppeteer)
            js_move_script = f"""
                const fromX = {from_coords['x']};
                const fromY = {from_coords['y']};
                const toX = {to_coords['x']};
                const toY = {to_coords['y']};

                // Find elements at coordinates
                const fromElement = document.elementFromPoint(fromX, fromY);
                const toElement = document.elementFromPoint(toX, toY);

                if (!fromElement || !toElement) {{
                    return {{success: false, error: 'Elements not found'}};
                }}

                // Helper to create mouse event with all required properties
                function createMouseEvent(type, x, y, element) {{
                    return new MouseEvent(type, {{
                        view: window,
                        bubbles: true,
                        cancelable: true,
                        clientX: x,
                        clientY: y,
                        screenX: x,
                        screenY: y,
                        button: 0,
                        buttons: type === 'mouseup' ? 0 : 1,
                        relatedTarget: element
                    }});
                }}

                // Helper to create pointer event (some sites use this instead)
                function createPointerEvent(type, x, y, element) {{
                    return new PointerEvent(type, {{
                        view: window,
                        bubbles: true,
                        cancelable: true,
                        clientX: x,
                        clientY: y,
                        screenX: x,
                        screenY: y,
                        pointerId: 1,
                        pointerType: 'mouse',
                        isPrimary: true,
                        button: 0,
                        buttons: type === 'pointerup' ? 0 : 1,
                        relatedTarget: element
                    }});
                }}

                // Dispatch full event sequence
                // Some chess sites need both mouse AND pointer events
                try {{
                    // 1. Start at source square
                    fromElement.dispatchEvent(createPointerEvent('pointerdown', fromX, fromY, fromElement));
                    fromElement.dispatchEvent(createMouseEvent('mousedown', fromX, fromY, fromElement));

                    // 2. Small delay (simulate human timing)
                    setTimeout(() => {{
                        // 3. Move events
                        fromElement.dispatchEvent(createPointerEvent('pointermove', fromX, fromY, toElement));
                        fromElement.dispatchEvent(createMouseEvent('mousemove', fromX, fromY, toElement));

                        // 4. Arrive at destination
                        toElement.dispatchEvent(createPointerEvent('pointermove', toX, toY, toElement));
                        toElement.dispatchEvent(createMouseEvent('mousemove', toX, toY, toElement));

                        // 5. Release at destination
                        toElement.dispatchEvent(createPointerEvent('pointerup', toX, toY, toElement));
                        toElement.dispatchEvent(createMouseEvent('mouseup', toX, toY, toElement));

                        // 6. Click event (some sites need this)
                        toElement.dispatchEvent(new MouseEvent('click', {{
                            view: window,
                            bubbles: true,
                            cancelable: true,
                            clientX: toX,
                            clientY: toY
                        }}));
                    }}, 50);

                    return {{
                        success: true,
                        from: fromElement.className,
                        to: toElement.className
                    }};
                }} catch (error) {{
                    return {{success: false, error: error.message}};
                }}
            """

            result = self.driver.execute_script(js_move_script)
            print(f"[ChessCom] JS dispatch result: {result}")

            # Wait for move to process
            time.sleep(0.6)

            # Validate: check if turn changed
            new_turn = self.get_turn()
            print(f"[ChessCom] Turn after move: {new_turn}")

            if turn != 'unknown' and new_turn != 'unknown' and turn != new_turn:
                print(f"[ChessCom] ✓ Move successful - turn changed from {turn} to {new_turn}")
                return True
            elif turn == new_turn and turn != 'unknown':
                print(f"[ChessCom] ⚠ Warning: Turn did not change (still {turn})")
                print(f"[ChessCom] Move may not have been registered")
                return False
            else:
                print(f"[ChessCom] Move {uci_move} executed (turn detection unavailable)")
                # If we can't detect turn, assume success based on JS result
                return result.get('success', False)

        except Exception as e:
            print(f"[ChessCom] Error making move: {e}")
            import traceback
            traceback.print_exc()
            return False

    def make_move(self, uci_move):
        """
        Make a move on the chess.com board using UCI notation.

        This is the main entry point that tries multiple methods:
        1. CDP Input.dispatchMouseEvent (exact Puppeteer equivalent)
        2. JavaScript DOM event dispatch (backup)
        3. ActionChains (requires focus - last resort)

        Args:
            uci_move: Move in UCI format (e.g., 'e2e4', 'd7d5')

        Returns:
            bool: True if move was successful, False otherwise
        """
        # Try CDP first (works in background)
        success = self.make_move_cdp(uci_move)
        if success:
            return True

        # Fallback to JS events
        print("[ChessCom] Trying JS fallback...")
        success = self.make_move_js(uci_move)
        if success:
            return True

        # Last resort: ActionChains (requires focus)
        print("[ChessCom] Trying ActionChains fallback...")
        return self.make_move_actionchains(uci_move)

    def make_move_actionchains(self, uci_move):
        """
        Make a move using Selenium ActionChains (requires window focus).
        This is the fallback method when JavaScript dispatch doesn't work.

        Args:
            uci_move: Move in UCI format (e.g., 'e2e4', 'd7d5')

        Returns:
            bool: True if move was successful, False otherwise
        """
        print(f"[ChessCom] Using ActionChains method: {uci_move}")

        try:
            # Focus the browser window first (required for ActionChains!)
            print("[ChessCom] Focusing browser window...")
            self.focus_browser()

            # Check whose turn it is
            turn = self.get_turn()
            print(f"[ChessCom] Current turn: {turn}")

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

            # Use Selenium ActionChains for physical drag-and-drop
            # This actually moves the mouse and triggers real browser events
            print("[ChessCom] Executing drag-and-drop with ActionChains...")

            # Method 1: Try to find square elements by position
            # Chess.com uses divs for squares, try to get the actual elements
            from_square_element = self.driver.execute_script(f"""
                const fromX = {from_coords['x']};
                const fromY = {from_coords['y']};
                return document.elementFromPoint(fromX, fromY);
            """)

            to_square_element = self.driver.execute_script(f"""
                const toX = {to_coords['x']};
                const toY = {to_coords['y']};
                return document.elementFromPoint(toX, toY);
            """)

            if from_square_element and to_square_element:
                print(f"[ChessCom] Found square elements")
                # Try direct drag and drop between elements
                actions = ActionChains(self.driver)
                actions.drag_and_drop(from_square_element, to_square_element)
                try:
                    actions.perform()
                    print("[ChessCom] Direct drag_and_drop executed")
                except Exception as e:
                    print(f"[ChessCom] Direct drag_and_drop failed: {e}")

                    # Fallback: manual click and hold sequence
                    print("[ChessCom] Trying manual click-hold-move-release...")
                    actions = ActionChains(self.driver)
                    actions.move_to_element(from_square_element)
                    actions.pause(0.1)
                    actions.click_and_hold(from_square_element)
                    actions.pause(0.3)
                    actions.move_to_element(to_square_element)
                    actions.pause(0.3)
                    actions.release(to_square_element)
                    actions.perform()
                    print("[ChessCom] Manual sequence executed")
            else:
                print(f"[ChessCom] Could not find square elements, using coordinates")
                return False

            # Wait for move to register
            time.sleep(0.5)

            # Validate: check if turn changed
            new_turn = self.get_turn()
            print(f"[ChessCom] Turn after move: {new_turn}")

            if turn != 'unknown' and new_turn != 'unknown' and turn != new_turn:
                print(f"[ChessCom] ✓ Move successful - turn changed from {turn} to {new_turn}")
                return True
            elif turn == new_turn and turn != 'unknown':
                print(f"[ChessCom] ⚠ Warning: Turn did not change (still {turn})")
                print(f"[ChessCom] Move may not have been registered by chess.com")
                return False
            else:
                print(f"[ChessCom] Move {uci_move} executed (turn detection unavailable)")
                return True

        except Exception as e:
            print(f"[ChessCom] Error making move: {e}")
            import traceback
            traceback.print_exc()
            return False

