"""Chess.com interface for interacting with the game board."""
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from uci_handler import UCIHandler


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
        except Exception as e:
            print(f"[ChessCom] Warning: Could not focus browser: {e}")

    def detect_board_size(self):
        """
        Detect the board size (files x ranks) by analyzing coordinate labels.

        Supports various board sizes: 4x4, 6x6, 8x8, 10x8, 14x14, etc.

        Returns:
            dict: {
                'files': int,  # Number of files (a-h = 8, a-j = 10, etc.)
                'ranks': int,  # Number of ranks (1-8 = 8, 1-4 = 4, etc.)
                'method': str  # Detection method used
            }
        """
        js_script = """
        // Find the main game board
        const board = document.querySelector('.TheBoard-squares') ||
                     document.querySelector('[class*="Board-squares"]') ||
                     document.querySelector('.board') ||
                     document.querySelector('[class*="board"]');

        if (!board) {
            return { files: 8, ranks: 8, method: 'default-no-board' };
        }

        const boardRect = board.getBoundingClientRect();
        const margin = 60; // Area around board where labels appear

        // Collect all coordinate labels near the board
        const allElements = Array.from(document.querySelectorAll('*'));
        const fileLetters = new Set();
        const rankNumbers = new Set();

        for (let el of allElements) {
            const text = el.textContent?.trim();
            if (!text || text.length > 3) continue;

            const rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;

            // Skip elements inside pockets, material counters, player info, etc.
            // Use String() to handle SVGAnimatedString and other non-string className types
            const className = String(el.className || '');
            const parentClasses = String(el.parentElement?.className || '');
            const skipPatterns = ['pocket', 'material', 'player', 'captured', 'score', 'clock', 'timer'];
            if (skipPatterns.some(pattern =>
                className.toLowerCase().includes(pattern) ||
                parentClasses.toLowerCase().includes(pattern))) {
                continue;
            }

            // Check for file letters (a-z) - should be ABOVE or BELOW board
            if (/^[a-z]$/.test(text)) {
                const nearTopOrBottom = (
                    Math.abs(rect.top - boardRect.bottom) < margin ||
                    Math.abs(rect.bottom - boardRect.top) < margin
                );
                if (nearTopOrBottom) {
                    fileLetters.add(text);
                }
            }
            // Check for rank numbers (1-14) - should be LEFT or RIGHT of board
            else if (/^[0-9]+$/.test(text)) {
                const nearLeftOrRight = (
                    Math.abs(rect.left - boardRect.right) < margin ||
                    Math.abs(rect.right - boardRect.left) < margin
                );
                const num = parseInt(text);
                if (nearLeftOrRight && num >= 1 && num <= 14) {
                    rankNumbers.add(num);
                }
            }
        }

        // Determine board size from labels
        let files = 8, ranks = 8;
        let method = 'default';

        if (fileLetters.size > 0 && rankNumbers.size > 0) {
            // Find max file letter
            const maxFile = Array.from(fileLetters).sort().pop();
            files = maxFile.charCodeAt(0) - 'a'.charCodeAt(0) + 1;

            // Find max rank number
            ranks = Math.max(...Array.from(rankNumbers));

            method = 'coordinate-labels';
        }

        return { files, ranks, method };
        """

        try:
            result = self.driver.execute_script(js_script)
            files = result.get('files', 8)
            ranks = result.get('ranks', 8)
            method = result.get('method', 'unknown')

            print(f"[Board] Size detected: {files}x{ranks} (method: {method})")
            return result

        except Exception as e:
            print(f"[Board] Error detecting size, defaulting to 8x8: {e}")
            return {'files': 8, 'ranks': 8, 'method': 'error'}

    def get_fen(self):
        """
        Get the current board position as FEN (Forsyth-Edwards Notation).

        FEN format: position turn castling en-passant halfmove fullmove
        Example: rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1
                 (starting position, white's turn)

        Returns:
            str: FEN string, or None if not available
        """
        js_script = """
        // Try multiple sources for the complete FEN
        let fen = null;

        // Priority 1: window.chessGame.getFEN() - most common
        if (window.chessGame && typeof window.chessGame.getFEN === 'function') {
            try {
                fen = window.chessGame.getFEN();
            } catch (e) {}
        }

        // Priority 2: window.game.getFEN() - alternative location
        if (!fen && window.game && typeof window.game.getFEN === 'function') {
            try {
                fen = window.game.getFEN();
            } catch (e) {}
        }

        // Priority 3: window.gameSetup.fen - used for puzzles/from-position games
        if (!fen && window.gameSetup && window.gameSetup.fen) {
            fen = window.gameSetup.fen;
        }

        return fen;
        """

        try:
            fen = self.driver.execute_script(js_script)
            return fen if fen else None
        except Exception as e:
            print(f"[ChessCom] Error getting FEN: {e}")
            return None


    def get_turn(self):
        """
        Detect whose turn it is using move list parity.

        Strategy: Check the last move in the move list (top-right panel).
        - Count individual moves in .moves-table-cell.moves-move elements
        - If last move index is even (0, 2, 4...) → white moved, black's turn
        - If last move index is odd (1, 3, 5...) → black moved, white's turn
        - If no moves yet → White's turn (starting position)

        This is simple, reliable, and works for all variants regardless of FEN format.

        Returns:
            str: 'white', 'black', or 'unknown'
        """
        js_script = """
        // Find the move table container
        const moveTable = document.querySelector('.moves-table');

        if (!moveTable) {
            return 'unknown';  // Can't find move list
        }

        // Find all move cells - these are individual moves (e4, b5, Bxb5, d5, etc.)
        // Use the specific selector that matches the actual DOM structure
        const moveCells = moveTable.querySelectorAll('.moves-table-cell.moves-move');

        if (!moveCells || moveCells.length === 0) {
            return 'white';  // No moves yet, white starts
        }

        // Filter out empty cells (placeholders for future moves)
        const actualMoves = Array.from(moveCells).filter(cell => {
            const text = cell.textContent.trim();
            return text.length > 0;
        });

        if (actualMoves.length === 0) {
            return 'white';  // No actual moves yet
        }

        // Get the last actual move
        const lastMoveIndex = actualMoves.length - 1;

        // Parity check:
        // Index 0 = first move (white's e4) → black's turn next
        // Index 1 = second move (black's b5) → white's turn next
        // Index 2 = third move (white's Bxb5) → black's turn next
        // Index 3 = fourth move (black's d5) → white's turn next
        // etc.
        if (lastMoveIndex % 2 === 0) {
            return 'black';  // Even index (0, 2, 4...) = white moved, black's turn
        } else {
            return 'white';  // Odd index (1, 3, 5...) = black moved, white's turn
        }
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

        // METHOD 2: Analyze coordinate labels (generalized for any board size)
        const margin = 40; // Coordinate labels are right next to the board
        const allElements = Array.from(document.querySelectorAll('*'));
        const coordinates = [];
        const allRankNumbers = new Set();

        // First pass: find all rank numbers near the board
        for (let el of allElements) {
            const text = el.textContent?.trim();

            // Check if it's a number between 1-14 (support up to 14x14 boards)
            if (/^[0-9]+$/.test(text) && text.length <= 3) {
                const num = parseInt(text);
                if (num >= 1 && num <= 14) {
                    const rect = el.getBoundingClientRect();

                    if (rect.width > 0 && rect.height > 0) {
                        const className = String(el.className || '').toLowerCase();
                        const labelInfo = {
                            text: text,
                            number: num,
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
                            allRankNumbers.add(num);
                            coordinates.push({
                                text: text,
                                number: num,
                                top: rect.top,
                                left: rect.left
                            });
                        }
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

            // Determine min and max ranks
            const minRank = Math.min(...Array.from(allRankNumbers));
            const maxRank = Math.max(...Array.from(allRankNumbers));

            if (topmost.number === minRank) {
                return {
                    is_flipped: true,
                    method: 'coordinate-labels',
                    detail: `rank ${minRank} at top (black perspective)`,
                    debug: debug
                };
            } else if (topmost.number === maxRank) {
                return {
                    is_flipped: false,
                    method: 'coordinate-labels',
                    detail: `rank ${maxRank} at top (white perspective)`,
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

            # Concise orientation summary (detailed debug removed)
            orientation_str = 'FLIPPED' if is_flipped else 'NORMAL'
            print(f"[ChessCom] Board orientation: {orientation_str}")

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

    def get_username_from_page(self, verbose=True):
        """
        Extract the user's username from the page (status bar).

        Args:
            verbose: If True, print detection messages. If False, silent mode.

        Returns:
            str: Username or None if not found
        """
        js_script = """
        const statusBarUsername = document.querySelector('.status-bar-username');
        if (statusBarUsername) {
            return statusBarUsername.textContent.trim();
        }
        return null;
        """

        try:
            username = self.driver.execute_script(js_script)
            if username:
                if verbose:
                    print(f"[Player] Detected username: {username}")
                return username
            else:
                if verbose:
                    print("[Player] Warning: Could not find username in status bar")
                return None
        except Exception as e:
            if verbose:
                print(f"[Player] Error detecting username: {e}")
            return None

    def get_player_position(self, username):
        """
        Find which playerbox (top or bottom) contains the given username.

        Args:
            username: The username to search for

        Returns:
            str: 'top', 'bottom', or 'unknown'
        """
        js_script = f"""
        const username = "{username}";

        // Find playerboxes
        const topBox = document.querySelector('.playerbox-top');
        const bottomBox = document.querySelector('.playerbox-bottom');

        // Check top box
        if (topBox) {{
            const topUserTag = topBox.querySelector('.playerbox-user-tag');
            if (topUserTag && topUserTag.textContent.includes(username)) {{
                return 'top';
            }}
        }}

        // Check bottom box
        if (bottomBox) {{
            const bottomUserTag = bottomBox.querySelector('.playerbox-user-tag');
            if (bottomUserTag && bottomUserTag.textContent.includes(username)) {{
                return 'bottom';
            }}
        }}

        return 'unknown';
        """

        try:
            position = self.driver.execute_script(js_script)
            print(f"[Player] Username '{username}' found in {position} playerbox")
            return position
        except Exception as e:
            print(f"[Player] Error finding player position: {e}")
            return 'unknown'

    def detect_piece_colors(self):
        """
        Analyze the board to determine where white and black pieces are located.

        This looks at piece positions to determine which ranks have white pieces
        and which have black pieces. In standard chess:
        - White pieces start on ranks 1-2
        - Black pieces start on ranks 7-8

        Returns:
            dict: {
                'white_ranks': list,  # Ranks where white pieces are concentrated
                'black_ranks': list,  # Ranks where black pieces are concentrated
                'confidence': str     # 'high', 'medium', 'low'
            }
        """
        js_script = """
        // Find all pieces on the board
        const pieces = document.querySelectorAll('[class*="piece"]');

        if (pieces.length === 0) {
            return { white_ranks: [], black_ranks: [], confidence: 'low', error: 'No pieces found' };
        }

        // Count pieces by color and rank
        const whitePieces = { 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: 0, 8: 0 };
        const blackPieces = { 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: 0, 8: 0 };

        pieces.forEach(piece => {
            const classes = piece.className;

            // Piece classes are like "piece wp" (white pawn), "piece bn" (black knight), etc.
            // First letter after 'piece' indicates color: w=white, b=black
            const isWhite = classes.includes(' w') || classes.includes('white');
            const isBlack = classes.includes(' b') || classes.includes('black');

            // Try to determine which square this piece is on
            // Check parent square element
            let square = piece.parentElement;
            while (square && !square.className.includes('square')) {
                square = square.parentElement;
            }

            if (square) {
                // Square classes often include the square name like "square-11" (a1), "square-88" (h8)
                // Format is typically "square-<file><rank>" where file=1-8 (a-h), rank=1-8
                const match = square.className.match(/square-(\\d)(\\d)/);
                if (match) {
                    const rank = parseInt(match[2]);  // Second digit is the rank

                    if (isWhite) {
                        whitePieces[rank]++;
                    } else if (isBlack) {
                        blackPieces[rank]++;
                    }
                }
            }
        });

        // Find ranks with most white and black pieces
        const whiteRanks = Object.entries(whitePieces)
            .filter(([rank, count]) => count > 0)
            .map(([rank, count]) => ({ rank: parseInt(rank), count }))
            .sort((a, b) => b.count - a.count)
            .map(item => item.rank);

        const blackRanks = Object.entries(blackPieces)
            .filter(([rank, count]) => count > 0)
            .map(([rank, count]) => ({ rank: parseInt(rank), count }))
            .sort((a, b) => b.count - a.count)
            .map(item => item.rank);

        // Determine confidence
        let confidence = 'low';
        if (whiteRanks.length >= 2 && blackRanks.length >= 2) {
            confidence = 'high';
        } else if (whiteRanks.length >= 1 && blackRanks.length >= 1) {
            confidence = 'medium';
        }

        return {
            white_ranks: whiteRanks,
            black_ranks: blackRanks,
            confidence: confidence,
            debug: {
                total_pieces: pieces.length,
                white_pieces: whitePieces,
                black_pieces: blackPieces
            }
        };
        """

        try:
            result = self.driver.execute_script(js_script)

            print(f"[Pieces] Detection confidence: {result.get('confidence', 'unknown')}")
            print(f"[Pieces] White pieces on ranks: {result.get('white_ranks', [])}")
            print(f"[Pieces] Black pieces on ranks: {result.get('black_ranks', [])}")

            if result.get('error'):
                print(f"[Pieces] Warning: {result['error']}")

            return result
        except Exception as e:
            print(f"[Pieces] Error detecting piece colors: {e}")
            import traceback
            traceback.print_exc()
            return {'white_ranks': [], 'black_ranks': [], 'confidence': 'low'}

    def get_player_color(self, username=None, verbose=True):
        """
        Detect which color the user is playing as.

        Uses the data-player attribute in playerbox elements:
        - data-player="0" indicates White
        - data-player="2" indicates Black

        Args:
            username: Optional username to use. If None, will auto-detect from page.
            verbose: If True, print detailed detection info. If False, silent mode.

        Returns:
            str: 'white', 'black', or 'unknown'
        """
        if verbose:
            print(f"\n{'='*60}")
            print(f"[Player Color Detection]")
            print(f"{'='*60}")

        # Step 1: Get username
        if not username:
            username = self.get_username_from_page(verbose=verbose)

        if not username:
            if verbose:
                print("[Player] ⚠ Could not determine username")
            return 'unknown'

        if verbose:
            print(f"[Player] Detected username: {username}")

        # Step 2: Find username's playerbox and get data-player attribute
        js_script = f"""
        const username = "{username}";
        const topBox = document.querySelector('.playerbox-top');
        const bottomBox = document.querySelector('.playerbox-bottom');

        // Check top box
        if (topBox) {{
            const userTag = topBox.querySelector('.playerbox-user-tag');
            if (userTag && userTag.textContent.includes(username)) {{
                const playerDiv = topBox.querySelector('[data-player]');
                const dataPlayer = playerDiv ? playerDiv.getAttribute('data-player') : null;
                return {{ position: 'top', dataPlayer: dataPlayer }};
            }}
        }}

        // Check bottom box
        if (bottomBox) {{
            const userTag = bottomBox.querySelector('.playerbox-user-tag');
            if (userTag && userTag.textContent.includes(username)) {{
                const playerDiv = bottomBox.querySelector('[data-player]');
                const dataPlayer = playerDiv ? playerDiv.getAttribute('data-player') : null;
                return {{ position: 'bottom', dataPlayer: dataPlayer }};
            }}
        }}

        return {{ position: 'unknown', dataPlayer: null }};
        """

        try:
            result = self.driver.execute_script(js_script)
            position = result.get('position', 'unknown')
            data_player = result.get('dataPlayer')

            if verbose:
                print(f"[Player] Username: {username}")
                print(f"[Player] Position: {position} playerbox")
                print(f"[Player] data-player attribute: {data_player}")

            # Map data-player to color
            # data-player="0" = White
            # data-player="2" = Black
            user_color = 'unknown'

            if data_player == "0":
                user_color = 'white'
            elif data_player == "2":
                user_color = 'black'
            else:
                if verbose:
                    print(f"[Player] ⚠ Unexpected data-player value: {data_player}")

            if verbose:
                print(f"\n{'─'*60}")
                if user_color == 'white':
                    print(f"♙  [Player Color] You are playing: WHITE")
                elif user_color == 'black':
                    print(f"♟️  [Player Color] You are playing: BLACK")
                else:
                    print(f"❓ [Player Color] Could not determine (unknown)")
                print(f"{'='*60}\n")

            return user_color

        except Exception as e:
            if verbose:
                print(f"[Player] ✗ Error detecting color: {e}")
                import traceback
                traceback.print_exc()
            return 'unknown'

    def debug_playerbox_structure(self):
        """
        Debug function to inspect the actual playerbox DOM structure.
        Useful for troubleshooting color detection issues.
        """
        print("\n" + "="*60)
        print("[DEBUG] Playerbox Structure Analysis")
        print("="*60)

        js_script = """
        const result = {
            topBox: null,
            bottomBox: null,
            allPlayerElements: []
        };

        // Find top and bottom playerboxes
        const topBox = document.querySelector('.playerbox-top');
        const bottomBox = document.querySelector('.playerbox-bottom');

        if (topBox) {
            result.topBox = {
                found: true,
                innerHTML: topBox.innerHTML.substring(0, 500),
                classes: topBox.className,
                dataPlayer: topBox.getAttribute('data-player'),
                userTag: null,
                allDataPlayers: []
            };

            // Find user tag
            const userTag = topBox.querySelector('.playerbox-user-tag');
            if (userTag) {
                result.topBox.userTag = {
                    text: userTag.textContent,
                    classes: userTag.className
                };
            }

            // Find all elements with data-player attribute
            const dataPlayerElements = topBox.querySelectorAll('[data-player]');
            dataPlayerElements.forEach(el => {
                result.topBox.allDataPlayers.push({
                    tagName: el.tagName,
                    dataPlayer: el.getAttribute('data-player'),
                    classes: el.className,
                    text: el.textContent.substring(0, 50)
                });
            });
        } else {
            result.topBox = { found: false };
        }

        if (bottomBox) {
            result.bottomBox = {
                found: true,
                innerHTML: bottomBox.innerHTML.substring(0, 500),
                classes: bottomBox.className,
                dataPlayer: bottomBox.getAttribute('data-player'),
                userTag: null,
                allDataPlayers: []
            };

            // Find user tag
            const userTag = bottomBox.querySelector('.playerbox-user-tag');
            if (userTag) {
                result.bottomBox.userTag = {
                    text: userTag.textContent,
                    classes: userTag.className
                };
            }

            // Find all elements with data-player attribute
            const dataPlayerElements = bottomBox.querySelectorAll('[data-player]');
            dataPlayerElements.forEach(el => {
                result.bottomBox.allDataPlayers.push({
                    tagName: el.tagName,
                    dataPlayer: el.getAttribute('data-player'),
                    classes: el.className,
                    text: el.textContent.substring(0, 50)
                });
            });
        } else {
            result.bottomBox = { found: false };
        }

        // Find ALL elements with data-player anywhere on the page
        const allPlayerElements = document.querySelectorAll('[data-player]');
        allPlayerElements.forEach(el => {
            result.allPlayerElements.push({
                tagName: el.tagName,
                dataPlayer: el.getAttribute('data-player'),
                classes: el.className,
                text: el.textContent.substring(0, 50),
                parent: el.parentElement ? el.parentElement.className : null
            });
        });

        return result;
        """

        try:
            result = self.driver.execute_script(js_script)

            print("\n[TOP PLAYERBOX]")
            if result['topBox']['found']:
                print(f"  ✓ Found")
                print(f"  Classes: {result['topBox']['classes']}")
                print(f"  data-player on box: {result['topBox']['dataPlayer']}")
                if result['topBox']['userTag']:
                    print(f"  User tag text: {result['topBox']['userTag']['text']}")
                print(f"  Elements with data-player: {len(result['topBox']['allDataPlayers'])}")
                for el in result['topBox']['allDataPlayers']:
                    print(f"    - {el['tagName']}: data-player={el['dataPlayer']}, text={el['text'][:30]}")
            else:
                print("  ✗ Not found")

            print("\n[BOTTOM PLAYERBOX]")
            if result['bottomBox']['found']:
                print(f"  ✓ Found")
                print(f"  Classes: {result['bottomBox']['classes']}")
                print(f"  data-player on box: {result['bottomBox']['dataPlayer']}")
                if result['bottomBox']['userTag']:
                    print(f"  User tag text: {result['bottomBox']['userTag']['text']}")
                print(f"  Elements with data-player: {len(result['bottomBox']['allDataPlayers'])}")
                for el in result['bottomBox']['allDataPlayers']:
                    print(f"    - {el['tagName']}: data-player={el['dataPlayer']}, text={el['text'][:30]}")
            else:
                print("  ✗ Not found")

            print(f"\n[ALL data-player ELEMENTS ON PAGE]")
            print(f"  Total found: {len(result['allPlayerElements'])}")
            for el in result['allPlayerElements']:
                print(f"    - {el['tagName']}: data-player={el['dataPlayer']}")
                print(f"      classes: {el['classes']}")
                print(f"      text: {el['text'][:40]}")
                print(f"      parent: {el['parent']}")
                print()

            print("="*60 + "\n")

        except Exception as e:
            print(f"[DEBUG] ✗ Error: {e}")
            import traceback
            traceback.print_exc()

    def debug_turn_detection(self):
        """
        Debug function to inspect turn detection via move list parity.
        Useful for troubleshooting turn detection issues.
        """
        print("\n" + "="*60)
        print("[DEBUG] Turn Detection Analysis - Move List Parity")
        print("="*60)

        detected_turn = self.get_turn()

        js_script = """
        const result = {
            moveTable: null,
            allMoveCells: [],
            actualMoves: [],
            lastMove: null
        };

        // Find the move table
        const moveTable = document.querySelector('.moves-table');

        if (!moveTable) {
            result.moveTable = { found: false };
            return result;
        }

        result.moveTable = {
            found: true,
            classes: moveTable.className
        };

        // Find all move cells
        const moveCells = moveTable.querySelectorAll('.moves-table-cell.moves-move');

        result.allMoveCells = Array.from(moveCells).map((cell, index) => ({
            index: index,
            text: cell.textContent.trim(),
            isEmpty: cell.textContent.trim().length === 0,
            classes: cell.className
        }));

        // Filter actual moves (non-empty)
        const actualMoves = Array.from(moveCells).filter(cell =>
            cell.textContent.trim().length > 0
        );

        result.actualMoves = Array.from(actualMoves).map((cell, index) => ({
            index: index,
            text: cell.textContent.trim(),
            classes: cell.className,
            isWhiteMove: index % 2 === 0,
            nextTurn: index % 2 === 0 ? 'black' : 'white'
        }));

        if (actualMoves.length > 0) {
            const lastMoveIndex = actualMoves.length - 1;
            const lastMove = actualMoves[lastMoveIndex];

            result.lastMove = {
                index: lastMoveIndex,
                text: lastMove.textContent.trim(),
                classes: lastMove.className,
                isWhiteMove: lastMoveIndex % 2 === 0,
                nextTurn: lastMoveIndex % 2 === 0 ? 'black' : 'white'
            };
        }

        return result;
        """

        try:
            result = self.driver.execute_script(js_script)

            print("\n[MOVE TABLE]")
            if result['moveTable'] and result['moveTable']['found']:
                print(f"  ✓ Found .moves-table")
                print(f"  Classes: {result['moveTable']['classes']}")
            else:
                print("  ✗ .moves-table not found")
                print("  ⚠ Make sure game window is large enough for move list to be visible")
                print("="*60 + "\n")
                return

            print(f"\n[ALL MOVE CELLS]")
            print(f"  Total cells found: {len(result['allMoveCells'])}")
            if len(result['allMoveCells']) > 0:
                for cell in result['allMoveCells'][:10]:
                    empty_marker = " (EMPTY)" if cell['isEmpty'] else ""
                    print(f"    [{cell['index']}] '{cell['text']}'{empty_marker}")
                if len(result['allMoveCells']) > 10:
                    print(f"    ... and {len(result['allMoveCells']) - 10} more")

            print(f"\n[ACTUAL MOVES] (non-empty cells)")
            print(f"  Total actual moves: {len(result['actualMoves'])}")
            if len(result['actualMoves']) > 0:
                show_count = min(8, len(result['actualMoves']))
                for move in result['actualMoves'][:show_count]:
                    color = "WHITE" if move['isWhiteMove'] else "BLACK"
                    print(f"    [{move['index']}] {move['text']:8s} ({color}) → Next: {move['nextTurn']}")

                if len(result['actualMoves']) > show_count:
                    remaining = len(result['actualMoves']) - show_count
                    print(f"    ... and {remaining} more moves")

            print("\n[LAST MOVE ANALYSIS]")
            if result['lastMove']:
                lm = result['lastMove']
                color = "WHITE" if lm['isWhiteMove'] else "BLACK"
                print(f"  Last move: '{lm['text']}' (index {lm['index']})")
                print(f"  Color: {color}")
                print(f"  Logic: {color} just moved → {lm['nextTurn'].upper()}'s turn")
            else:
                print("  No moves yet → White's turn (starting position)")

            print(f"\n[FINAL RESULT]")
            print(f"  ✓ Detected turn: {detected_turn}")

            print("="*60 + "\n")

        except Exception as e:
            print(f"[DEBUG] ✗ Error: {e}")
            import traceback
            traceback.print_exc()

    def _old_debug_turn_detection_dom(self):
        """
        OLD debug function - kept for reference.
        Inspects DOM elements for turn indicators (CSS classes, etc).
        """
        js_script = """
        const result = {
            board: null,
            playerBoxes: [],
            clocks: [],
            anyActiveElements: []
        };

        // Check board element and its classes
        const board = document.querySelector('.TheBoard') ||
                     document.querySelector('[class*="board"]');
        if (board) {
            result.board = {
                found: true,
                classes: board.className,
                hasWhite: board.className.includes('white'),
                hasBlack: board.className.includes('black')
            };
        }

        // Check player boxes for active/turn indicators
        const topBox = document.querySelector('.playerbox-top');
        const bottomBox = document.querySelector('.playerbox-bottom');

        if (topBox) {
            result.playerBoxes.push({
                position: 'top',
                classes: topBox.className,
                hasActive: topBox.className.includes('active'),
                hasTurn: topBox.className.includes('turn'),
                dataPlayer: topBox.querySelector('[data-player]')?.getAttribute('data-player')
            });
        }

        if (bottomBox) {
            result.playerBoxes.push({
                position: 'bottom',
                classes: bottomBox.className,
                hasActive: bottomBox.className.includes('active'),
                hasTurn: bottomBox.className.includes('turn'),
                dataPlayer: bottomBox.querySelector('[data-player]')?.getAttribute('data-player')
            });
        }

        // Check clock elements
        const clocks = document.querySelectorAll('[class*="clock"]');
        clocks.forEach(clock => {
            const rect = clock.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) {
                result.clocks.push({
                    classes: clock.className,
                    text: clock.textContent.trim(),
                    hasRunning: clock.className.includes('running'),
                    hasActive: clock.className.includes('active'),
                    parent: clock.parentElement?.className || 'none'
                });
            }
        });

        // Find any elements with 'active' or 'turn' in their class
        const activeElements = document.querySelectorAll('[class*="active"], [class*="turn"]');
        activeElements.forEach(el => {
            if (el.className.includes('player') ||
                el.className.includes('board') ||
                el.className.includes('clock')) {
                result.anyActiveElements.push({
                    tagName: el.tagName,
                    classes: el.className,
                    text: el.textContent.substring(0, 30)
                });
            }
        });

        return result;
        """

        try:
            result = self.driver.execute_script(js_script)

            print("\n[BOARD ELEMENT]")
            if result['board']:
                print(f"  ✓ Found")
                print(f"  Classes: {result['board']['classes']}")
                print(f"  Has 'white': {result['board']['hasWhite']}")
                print(f"  Has 'black': {result['board']['hasBlack']}")
            else:
                print("  ✗ Not found")

            print("\n[PLAYER BOXES]")
            for box in result['playerBoxes']:
                print(f"  {box['position'].upper()} box:")
                print(f"    Classes: {box['classes']}")
                print(f"    Has 'active': {box['hasActive']}")
                print(f"    Has 'turn': {box['hasTurn']}")
                print(f"    data-player: {box['dataPlayer']}")

            print("\n[CLOCKS]")
            print(f"  Total found: {len(result['clocks'])}")
            for i, clock in enumerate(result['clocks']):
                print(f"  Clock {i+1}:")
                print(f"    Classes: {clock['classes']}")
                print(f"    Time: {clock['text']}")
                print(f"    Has 'running': {clock['hasRunning']}")
                print(f"    Has 'active': {clock['hasActive']}")
                print(f"    Parent: {clock['parent']}")

            print("\n[ELEMENTS WITH 'active' OR 'turn']")
            print(f"  Total found: {len(result['anyActiveElements'])}")
            for el in result['anyActiveElements']:
                print(f"    - {el['tagName']}: {el['classes'][:80]}")

            print("="*60 + "\n")

        except Exception as e:
            print(f"[DEBUG] ✗ Error: {e}")
            import traceback
            traceback.print_exc()

    def is_board_flipped(self):
        """
        Detect if the board is flipped (rank 1 at top).

        Returns:
            bool: True if board is flipped, False otherwise
        """
        orientation = self.get_board_orientation()
        return orientation['is_flipped']

    # Debug functions removed to reduce output noise
    # Use browser DevTools console for detailed inspection if needed


    def get_square_coordinates(self, square, is_flipped=None, board_size=None):
        """
        Get the pixel coordinates of a square on the chess.com board.
        Automatically adjusts for board flip and board size.

        Args:
            square: Square in UCI format (e.g., 'e2', 'd4', 'j8')
            is_flipped: Optional pre-computed board flip state (True/False).
                       If None, will detect automatically.
            board_size: Optional pre-computed board size dict {'files': int, 'ranks': int}.
                       If None, will detect automatically.

        Returns:
            dict: {'x': x_coord, 'y': y_coord} or None if not found
        """
        file_letter = square[0]
        rank_number = int(square[1:])  # Support multi-digit ranks (e.g., '10', '14')

        # Convert file letter to number (a=1, b=2, ..., j=10, etc.)
        file_num = ord(file_letter) - ord('a') + 1

        # Detect board orientation (only if not provided)
        if is_flipped is None:
            is_flipped = self.is_board_flipped()

        # Detect board size (only if not provided)
        if board_size is None:
            board_size = self.detect_board_size()

        num_files = board_size.get('files', 8)
        num_ranks = board_size.get('ranks', 8)

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
        const numFiles = {num_files};
        const numRanks = {num_ranks};
        const squareSize = rect.width / numFiles;
        const isFlipped = {str(is_flipped).lower()};

        let fileIndex, rankIndex;

        if (isFlipped) {{
            // BLACK ON BOTTOM (flipped board)
            // For any board size NxM:
            // Visual: rightmost file at left, rank 1 at bottom
            // Pixel coords (0,0) at top-left
            fileIndex = numFiles - {file_num};  // rightmost=0, ..., leftmost=numFiles-1
            rankIndex = {rank_number} - 1;      // rank 1=0, rank 2=1, ..., rank N=N-1
            console.log('[Coords] FLIPPED ({square}): fileIndex=' + fileIndex + ', rankIndex=' + rankIndex);
        }} else {{
            // WHITE ON BOTTOM (normal board)
            // For any board size NxM:
            // Visual: leftmost file at left, highest rank at top
            // Pixel coords (0,0) at top-left
            fileIndex = {file_num} - 1;         // a=0, b=1, c=2, ...
            rankIndex = numRanks - {rank_number};  // highest rank=0, ..., rank 1=numRanks-1
            console.log('[Coords] NORMAL ({square}): fileIndex=' + fileIndex + ', rankIndex=' + rankIndex);
        }}

        const x = rect.left + (fileIndex * squareSize) + (squareSize / 2);
        const y = rect.top + (rankIndex * squareSize) + (squareSize / 2);

        return {{
            x: x,
            y: y,
            method: 'calculated',
            flipped: isFlipped,
            boardSize: {{ files: numFiles, ranks: numRanks }},
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
            uci_move: Move in UCI format (e.g., 'e2e4', 'd7d5', 'g14n7')

        Returns:
            bool: True if move was successful, False otherwise
        """
        try:
            # Parse UCI move properly (handles multi-digit ranks)
            parsed = UCIHandler.parse_uci_move(uci_move)
            if not parsed or parsed.get('type') != 'normal':
                print(f"[ChessCom] ✗ Invalid move format: {uci_move}")
                return False

            from_square = parsed['from']
            to_square = parsed['to']

            print(f"[ChessCom] Move: {from_square} → {to_square}")

            # Detect board orientation and size once (cache for this move)
            is_flipped = self.is_board_flipped()
            board_size = self.detect_board_size()

            # Get coordinates for both squares (pass cached flip state and board size)
            from_coords = self.get_square_coordinates(from_square, is_flipped, board_size)
            to_coords = self.get_square_coordinates(to_square, is_flipped, board_size)

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
            uci_move: Move in UCI format (e.g., 'e2e4', 'd7d5', 'g14n7')

        Returns:
            bool: True if move was successful, False otherwise
        """
        try:
            # Check whose turn it is
            turn = self.get_turn()

            # Parse UCI move properly (handles multi-digit ranks)
            parsed = UCIHandler.parse_uci_move(uci_move)
            if not parsed or parsed.get('type') != 'normal':
                print(f"[ChessCom] ✗ Invalid move format: {uci_move}")
                return False

            from_square = parsed['from']
            to_square = parsed['to']

            print(f"[ChessCom] Move: {from_square} -> {to_square}")

            # Detect board parameters once (cache for this move)
            is_flipped = self.is_board_flipped()
            board_size = self.detect_board_size()

            # Get coordinates for both squares
            from_coords = self.get_square_coordinates(from_square, is_flipped, board_size)
            to_coords = self.get_square_coordinates(to_square, is_flipped, board_size)

            if not from_coords or not to_coords:
                print(f"[ChessCom] Could not find board squares")
                return False


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

            # Wait for move to process
            time.sleep(0.6)

            # Validate: check if turn changed
            new_turn = self.get_turn()

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

    def handle_promotion(self, promotion_piece):
        """
        Handle pawn promotion dialog after a promotion move.

        Args:
            promotion_piece: UCI promotion character ('q', 'r', 'b', 'n', 'k', 'u', 'w', 'f', 'a', 'c')
                            UCI: a=Archbishop, c=Chancellor (chess.com: H, E)

        Returns:
            bool: True if promotion was handled successfully, False otherwise
        """
        try:
            print(f"[ChessCom] Handling promotion to: {promotion_piece.upper()}")

            # Wait for promotion dialog to appear
            time.sleep(0.3)

            js_script = """
            // Map UCI promotion characters to piece types
            // NOTE: UCI uses 'a'/'c' for Archbishop/Chancellor, but chess.com uses 'H'/'E'
            const promotionPiece = arguments[0].toLowerCase();
            const pieceMap = {
                'q': 'Q',  // Queen
                'r': 'R',  // Rook
                'b': 'B',  // Bishop
                'n': 'N',  // Knight
                'k': 'K',  // King (for variants)
                'u': 'U',  // Unicorn (for variants)
                'w': 'W',  // Wazir (for variants)
                'f': 'F',  // Ferz (for variants)
                'a': 'H',  // Archbishop (UCI: A → chess.com: H)
                'c': 'E'   // Chancellor (UCI: C → chess.com: E)
            };

            const targetPiece = pieceMap[promotionPiece];
            if (!targetPiece) {
                return { error: 'Unknown promotion piece: ' + promotionPiece };
            }

            // First, find the promotion dialog container
            const dialogSelectors = [
                '[class*="promotion"]',
                '.promotion-area',
                '[class*="piece-choice"]',
                '[class*="upgrade"]'
            ];

            // Find the LARGEST visible promotion dialog (not hidden/minimized ones)
            let promotionDialog = null;
            let largestArea = 0;

            for (const selector of dialogSelectors) {
                const elements = document.querySelectorAll(selector);
                for (const elem of elements) {
                    const rect = elem.getBoundingClientRect();
                    const area = rect.width * rect.height;

                    // Must be visible and larger than what we've found
                    if (area > largestArea && rect.width > 50 && rect.height > 50) {
                        promotionDialog = elem;
                        largestArea = area;
                    }
                }
            }

            if (!promotionDialog) {
                return { error: 'Promotion dialog container not found (no large visible dialogs)' };
            }

            // LOG DIALOG DETAILS
            const dialogRect = promotionDialog.getBoundingClientRect();
            console.log('[Promotion] Dialog found:', {
                class: promotionDialog.className,
                rect: { x: dialogRect.left, y: dialogRect.top, w: dialogRect.width, h: dialogRect.height },
                innerHTML: promotionDialog.innerHTML.substring(0, 300)
            });

            // Find pieces WITHIN the promotion dialog - try multiple selectors
            let promotionPieces = promotionDialog.querySelectorAll('[data-piece]');

            // If no pieces found, try alternative selectors
            if (promotionPieces.length === 0) {
                promotionPieces = promotionDialog.querySelectorAll('[class*="piece"]');
            }
            if (promotionPieces.length === 0) {
                promotionPieces = promotionDialog.querySelectorAll('img[src*="piece"]');
            }
            if (promotionPieces.length === 0) {
                promotionPieces = promotionDialog.querySelectorAll('div[role="button"]');
            }

            if (promotionPieces.length === 0) {
                return { error: 'No promotion pieces found in dialog' };
            }

            // Debug: Log all available pieces with their actual positions
            const availablePieces = Array.from(promotionPieces).map((p, idx) => {
                const pRect = p.getBoundingClientRect();
                return {
                    index: idx,
                    dataPiece: p.getAttribute('data-piece'),
                    rect: { x: Math.round(pRect.left), y: Math.round(pRect.top), w: Math.round(pRect.width), h: Math.round(pRect.height) }
                };
            });
            console.log('[Promotion] Available pieces:', availablePieces);
            console.log('[Promotion] Looking for:', targetPiece);

            // Find the matching piece
            for (const piece of promotionPieces) {
                const dataPiece = piece.getAttribute('data-piece');

                // ONLY match by data-piece attribute (exact match)
                // This prevents false positives like 'H' matching "bisHop"
                if (dataPiece === targetPiece) {
                    // Found the target piece! Use its ACTUAL bounding rect
                    const pieceRect = piece.getBoundingClientRect();

                    console.log('[Promotion] Found target piece:', {
                        dataPiece,
                        rect: { x: Math.round(pieceRect.left), y: Math.round(pieceRect.top), w: Math.round(pieceRect.width), h: Math.round(pieceRect.height) }
                    });

                    // Use the piece's actual position (trust getBoundingClientRect now that we have the right dialog!)
                    const x = Math.round(pieceRect.left + pieceRect.width / 2);
                    const y = Math.round(pieceRect.top + pieceRect.height / 2);

                    console.log('[Promotion] Will click at piece center:', { x, y });

                    return {
                        found: true,
                        piece: targetPiece,
                        clickMethod: 'cdp',
                        x: x,
                        y: y
                    };
                }
            }

            // Not found
            return {
                found: false,
                searched: targetPiece,
                available: availablePieces.map(p => p.dataPiece).join(', ')
            };

            // If exact match not found, return what we found for debugging
            return {
                found: false,
                searched: targetPiece,
                available: availablePieces.join(', ')
            };
            """

            result = self.driver.execute_script(js_script, promotion_piece)

            if result.get('found'):
                # Use CDP to click on the promotion piece (creates trusted events)
                x = result['x']
                y = result['y']

                print(f"[ChessCom] Clicking promotion piece at ({x}, {y})")
                if 'debug' in result:
                    print(f"[ChessCom] Piece size: {result['debug']['pieceWidth']}x{result['debug']['pieceHeight']}, Container: {result['debug']['containerWidth']}x{result['debug']['containerHeight']}")

                # DEBUG: Check what element is at these coordinates
                elem_at_coords = self.driver.execute_script(f"""
                    const elem = document.elementFromPoint({x}, {y});
                    if (!elem) return {{ error: 'No element at coordinates' }};

                    const rect = elem.getBoundingClientRect();
                    return {{
                        tag: elem.tagName,
                        class: elem.className,
                        dataPiece: elem.getAttribute('data-piece'),
                        rect: {{ x: rect.left, y: rect.top, w: rect.width, h: rect.height }},
                        pointerEvents: window.getComputedStyle(elem).pointerEvents
                    }};
                """)
                print(f"[ChessCom] Element at ({x}, {y}): {elem_at_coords}")

                try:
                    # Click using CDP (creates trusted mouse events)
                    self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                        'type': 'mouseMoved',
                        'x': x,
                        'y': y
                    })
                    time.sleep(0.05)

                    self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                        'type': 'mousePressed',
                        'x': x,
                        'y': y,
                        'button': 'left',
                        'clickCount': 1
                    })
                    time.sleep(0.05)

                    self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                        'type': 'mouseReleased',
                        'x': x,
                        'y': y,
                        'button': 'left',
                        'clickCount': 1
                    })

                    # Wait and verify the promotion dialog closed
                    time.sleep(0.5)

                    # Check if promotion dialog is still visible
                    dialog_check = self.driver.execute_script("""
                        const dialogs = document.querySelectorAll('[class*="promotion"]');
                        for (const dialog of dialogs) {
                            const rect = dialog.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {
                                return { stillVisible: true };
                            }
                        }
                        return { stillVisible: false };
                    """)

                    if dialog_check.get('stillVisible'):
                        print(f"[ChessCom] ⚠ Promotion dialog still visible after click - promotion may have failed")
                        return False
                    else:
                        print(f"[ChessCom] ✓ Promoted to {result['piece']} (CDP, dialog closed)")
                        return True

                except Exception as cdp_error:
                    print(f"[ChessCom] ✗ CDP click failed: {cdp_error}")
                    return False
            else:
                error_msg = f"[ChessCom] ✗ Promotion piece not found"
                if 'available' in result:
                    error_msg += f" (searched: {result.get('searched')}, available: {result['available']})"
                print(error_msg)
                return False

        except Exception as e:
            print(f"[ChessCom] Error handling promotion: {e}")
            import traceback
            traceback.print_exc()
            return False

    def make_move(self, uci_move):
        """
        Make a move on the chess.com board using UCI notation.

        Handles both regular moves and drop moves (Crazyhouse/variants):
        - Regular moves: e2e4, d7d5, a7a8q (with promotion)
        - Drop moves: N@g3, P@e5, Q@d8, A@e1, C@d1 (place piece from pocket)
          NOTE: UCI uses A/C for Archbishop/Chancellor, chess.com uses H/E

        This is the main entry point that tries multiple methods:
        1. Drop moves: make_drop_move (for piece placements)
        2. CDP Input.dispatchMouseEvent (exact Puppeteer equivalent)
        3. JavaScript DOM event dispatch (backup)
        4. ActionChains (requires focus - last resort)

        Args:
            uci_move: Move in UCI format (e.g., 'e2e4', 'd7d5', 'N@g3', 'A@e1')

        Returns:
            bool: True if move was successful, False otherwise
        """
        # Check if this is a drop move (contains '@')
        if '@' in uci_move:
            # Parse drop move: P@e5, N@g3, etc.
            parts = uci_move.upper().split('@')
            if len(parts) == 2:
                piece_type = parts[0]
                to_square = parts[1].lower()
                return self.make_drop_move(piece_type, to_square)
            else:
                print(f"[ChessCom] ✗ Invalid drop move format: {uci_move}")
                return False

        # Parse the move to detect promotion (works with any board size)
        parsed_move = UCIHandler.parse_uci_move(uci_move)

        promotion_piece = None
        base_move = uci_move

        if parsed_move and parsed_move.get('type') == 'normal' and parsed_move.get('promotion'):
            promotion_piece = parsed_move['promotion']
            # Reconstruct base move without promotion
            base_move = parsed_move['from'] + parsed_move['to']
            print(f"[ChessCom] Promotion move detected: {base_move} → {promotion_piece.upper()}")

        # Regular move - try CDP first (works in background)
        success = self.make_move_cdp(base_move)
        if success:
            # Handle promotion if needed
            if promotion_piece:
                return self.handle_promotion(promotion_piece)
            return True

        # Fallback to JS events
        print("[ChessCom] Trying JS fallback...")
        success = self.make_move_js(base_move)
        if success:
            # Handle promotion if needed
            if promotion_piece:
                return self.handle_promotion(promotion_piece)
            return True

        # Last resort: ActionChains (requires focus)
        print("[ChessCom] Trying ActionChains fallback...")
        success = self.make_move_actionchains(base_move)
        if success and promotion_piece:
            return self.handle_promotion(promotion_piece)
        return success

    def make_move_actionchains(self, uci_move):
        """
        Make a move using Selenium ActionChains (requires window focus).
        This is the fallback method when JavaScript dispatch doesn't work.

        Args:
            uci_move: Move in UCI format (e.g., 'e2e4', 'd7d5', 'g14n7')

        Returns:
            bool: True if move was successful, False otherwise
        """
        try:
            # Focus the browser window first (required for ActionChains!)
            self.focus_browser()

            # Check whose turn it is
            turn = self.get_turn()

            # Parse UCI move properly (handles multi-digit ranks)
            parsed = UCIHandler.parse_uci_move(uci_move)
            if not parsed or parsed.get('type') != 'normal':
                print(f"[ChessCom] ✗ Invalid move format: {uci_move}")
                return False

            from_square = parsed['from']
            to_square = parsed['to']

            # Detect board parameters once (cache for this move)
            is_flipped = self.is_board_flipped()
            board_size = self.detect_board_size()

            # Get coordinates for both squares
            from_coords = self.get_square_coordinates(from_square, is_flipped, board_size)
            to_coords = self.get_square_coordinates(to_square, is_flipped, board_size)

            if not from_coords or not to_coords:
                print(f"[ChessCom] Could not find board squares")
                return False


            # Use Selenium ActionChains for physical drag-and-drop
            # This actually moves the mouse and triggers real browser events

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
                # Try direct drag and drop between elements
                actions = ActionChains(self.driver)
                actions.drag_and_drop(from_square_element, to_square_element)
                try:
                    actions.perform()
                except Exception as e:
                    # Fallback: manual click and hold sequence
                    actions = ActionChains(self.driver)
                    actions.move_to_element(from_square_element)
                    actions.pause(0.1)
                    actions.click_and_hold(from_square_element)
                    actions.pause(0.3)
                    actions.move_to_element(to_square_element)
                    actions.pause(0.3)
                    actions.release(to_square_element)
                    actions.perform()
            else:
                print(f"[ChessCom] ✗ Could not find square elements")
                return False

            # Wait for move to register
            time.sleep(0.5)

            # Validate: check if turn changed
            new_turn = self.get_turn()

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

    def get_pocket_piece_coordinates(self, piece_type, player_color=None):
        """
        Get the coordinates of a piece in the pocket (captured pieces in hand).

        Args:
            piece_type: Single letter piece type (Q, R, N, B, P, U, W, F, A, C)
                       UCI: A=Archbishop, C=Chancellor (chess.com: H, E)
            player_color: 'white' or 'black', or None to auto-detect

        Returns:
            dict: {'x': x_coord, 'y': y_coord} or None if not found
        """
        # Auto-detect player color if not provided
        if not player_color:
            player_color = self.get_player_color()

        # Convert UCI notation to chess.com notation
        # UCI: A=Archbishop, C=Chancellor → chess.com: H, E
        uci_to_chesscom = {
            'A': 'H',  # Archbishop
            'C': 'E'   # Chancellor
        }

        # Use chess.com notation if piece is A or C, otherwise use as-is
        chesscom_piece = uci_to_chesscom.get(piece_type.upper(), piece_type.upper())

        if piece_type.upper() != chesscom_piece:
            print(f"[ChessCom] Converting UCI {piece_type.upper()} → chess.com {chesscom_piece}")

        # Map piece type to full name for Chess.com's class naming
        # NOTE: UCI uses A/C for Archbishop/Chancellor, but chess.com uses H/E
        piece_map = {
            'Q': 'queen',
            'R': 'rook',
            'N': 'knight',
            'B': 'bishop',
            'P': 'pawn',
            'U': 'unicorn',
            'W': 'wazir',
            'F': 'ferz',
            'H': 'archbishop',  # chess.com notation
            'E': 'chancellor'   # chess.com notation
        }

        piece_name = piece_map.get(chesscom_piece)
        if not piece_name:
            print(f"[ChessCom] ✗ Unknown piece type: {piece_type}")
            return None

        # Chess.com uses data-piece attribute (e.g., data-piece="P")
        # and positions pieces to the LEFT of the board for pockets
        js_script = f"""
        const pieceType = '{chesscom_piece}';  // Use chess.com notation (H/E, not A/C)
        const playerColor = '{player_color}';

        // Step 1: Find the board position
        const board = document.querySelector('.TheBoard-squares') ||
                     document.querySelector('[class*="Board-squares"]') ||
                     document.querySelector('.board');

        if (!board) {{
            return {{ error: 'Board not found' }};
        }}

        const boardRect = board.getBoundingClientRect();

        // DEBUG: Find ALL pocket pieces to see what's available
        const allPocketElements = document.querySelectorAll('[class*="pocket"] [data-piece]');
        const debugPocketPieces = [];
        for (const elem of allPocketElements) {{
            const dataPiece = elem.getAttribute('data-piece');
            const rect = elem.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) {{
                debugPocketPieces.push({{
                    dataPiece: dataPiece,
                    className: elem.className,
                    rect: {{ x: Math.round(rect.left), y: Math.round(rect.top), w: Math.round(rect.width), h: Math.round(rect.height) }}
                }});
            }}
        }}
        console.log('[PocketDrop] All pocket pieces found:', debugPocketPieces);
        console.log('[PocketDrop] Searching for piece type:', pieceType);

        // Step 2: Find all pieces with matching data-piece attribute
        const allPieces = document.querySelectorAll(`[data-piece="${{pieceType}}"]`);

        // Step 3: Filter to pieces LEFT of the board (pocket area)
        // Pocket pieces are positioned to the left of the board
        const pocketPieces = [];

        for (const piece of allPieces) {{
            const rect = piece.getBoundingClientRect();

            // Check if piece is to the LEFT of the board
            // (right edge of piece is before or near left edge of board)
            const isLeftOfBoard = rect.right < boardRect.left + 50;

            // Check if piece is near the vertical range of the board
            const isNearBoard = rect.bottom > boardRect.top - 50 &&
                               rect.top < boardRect.bottom + 50;

            // Must have non-zero size (filters out hidden elements)
            if (isLeftOfBoard && isNearBoard && rect.width > 0 && rect.height > 0) {{
                pocketPieces.push({{
                    element: piece,
                    rect: rect,
                    dataColor: piece.getAttribute('data-color'),
                    dataPlayer: piece.getAttribute('data-player'),
                    className: piece.className,
                    position: {{
                        top: rect.top,
                        bottom: rect.bottom,
                        left: rect.left,
                        right: rect.right,
                        centerX: rect.left + rect.width / 2,
                        centerY: rect.top + rect.height / 2
                    }}
                }});
            }}
        }}

        if (pocketPieces.length === 0) {{
            return {{ error: 'No pocket pieces found', searched: pieceType }};
        }}

        // Step 4: Select the correct pocket based on player color AND board orientation
        // - NORMAL orientation (rank 8 at top): Black pocket at TOP, White pocket at BOTTOM
        // - FLIPPED orientation (rank 1 at top): Black pocket at BOTTOM, White pocket at TOP

        const isFlipped = {str(self.is_board_flipped()).lower()};

        let selectedPiece = null;
        let selectTopPocket = false;

        if (playerColor === 'black') {{
            // Black in normal orientation → top pocket
            // Black in flipped orientation → bottom pocket
            selectTopPocket = !isFlipped;
        }} else if (playerColor === 'white') {{
            // White in normal orientation → bottom pocket
            // White in flipped orientation → top pocket
            selectTopPocket = isFlipped;
        }} else {{
            // Unknown - try bottom pocket
            selectTopPocket = false;
        }}

        if (selectTopPocket) {{
            // Select piece closest to TOP of screen
            pocketPieces.sort((a, b) => a.position.centerY - b.position.centerY);
            selectedPiece = pocketPieces[0];
        }} else {{
            // Select piece closest to BOTTOM of screen
            pocketPieces.sort((a, b) => b.position.centerY - a.position.centerY);
            selectedPiece = pocketPieces[0];
        }}

        if (selectedPiece) {{
            return {{
                x: selectedPiece.position.centerX,
                y: selectedPiece.position.centerY,
                dataColor: selectedPiece.dataColor,
                className: selectedPiece.className,
                found: true
            }};
        }}

        return {{ error: 'Could not select pocket piece' }};
        """

        try:
            result = self.driver.execute_script(js_script)

            if result and result.get('found'):
                return {'x': result['x'], 'y': result['y']}
            else:
                print(f"[ChessCom] ✗ Could not find {piece_type.upper()} in pocket: {result.get('error', 'Unknown')}")
                return None

        except Exception as e:
            print(f"[ChessCom] ✗ Error finding pocket piece: {e}")
            import traceback
            traceback.print_exc()
            return None

    def make_drop_move(self, piece_type, to_square):
        """
        Execute a drop move (place a piece from pocket onto the board).

        Args:
            piece_type: Single letter piece type (Q, R, N, B, P, U, W, F, A, C)
                       UCI: A=Archbishop, C=Chancellor (chess.com: H, E)
            to_square: Destination square in UCI format (e.g., 'g3', 'e5')

        Returns:
            bool: True if drop was successful, False otherwise
        """
        print(f"[ChessCom] Drop move: {piece_type}@{to_square}")

        try:
            # Get player color
            player_color = self.get_player_color()
            if player_color == 'unknown':
                print(f"[ChessCom] ✗ Cannot determine player color")
                return False

            # Get coordinates of pocket piece
            from_coords = self.get_pocket_piece_coordinates(piece_type, player_color)
            if not from_coords:
                print(f"[ChessCom] ✗ Could not find {piece_type} in pocket")
                return False

            # Detect board parameters once (cache for this move)
            is_flipped = self.is_board_flipped()
            board_size = self.detect_board_size()

            # Get coordinates of destination square
            to_coords = self.get_square_coordinates(to_square, is_flipped, board_size)
            if not to_coords:
                print(f"[ChessCom] ✗ Could not find destination square {to_square}")
                return False

            # Execute drop using CDP (same as regular moves)
            try:
                # Move to pocket piece position
                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mouseMoved',
                    'x': from_coords['x'],
                    'y': from_coords['y']
                })
                time.sleep(0.03)

                # Mouse down on pocket piece
                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mousePressed',
                    'x': from_coords['x'],
                    'y': from_coords['y'],
                    'button': 'left',
                    'clickCount': 1
                })
                time.sleep(0.05)

                # Drag to destination square with steps
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

                # Wait for move to process
                time.sleep(0.15)
                return True

            except Exception as cdp_error:
                print(f"[ChessCom] ✗ CDP error during drop: {cdp_error}")
                return False

        except Exception as e:
            print(f"[ChessCom] ✗ Error executing drop move: {e}")
            import traceback
            traceback.print_exc()
            return False

    def resign(self):
        """
        Resign the current game by clicking the resign button.

        Returns:
            bool: True if resign was successful, False otherwise
        """
        print("[ChessCom] Attempting to resign...")

        try:
            js_script = """
            // Find resign button - try multiple selectors
            const resignSelectors = [
                'button[aria-label*="Resign"]',
                'button:has-text("Resign")',
                '[class*="resign"]',
                'button[data-cy="resign"]',
                '[data-test-element="resign"]'
            ];

            // Try to find resign button
            let resignButton = null;

            // Method 1: Look for buttons with "resign" text
            const allButtons = document.querySelectorAll('button');
            for (const button of allButtons) {
                const text = button.textContent?.toLowerCase() || '';
                const ariaLabel = button.getAttribute('aria-label')?.toLowerCase() || '';

                if (text.includes('resign') || ariaLabel.includes('resign')) {
                    const rect = button.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        resignButton = button;
                        break;
                    }
                }
            }

            // Method 2: Look in game menu/options
            if (!resignButton) {
                // Try to find and open game menu first
                const menuButtons = document.querySelectorAll('button[aria-label*="Menu"], button[aria-label*="menu"], [class*="menu-button"]');
                for (const menuBtn of menuButtons) {
                    const rect = menuBtn.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        menuBtn.click();
                        break;
                    }
                }

                // Wait a bit for menu to open
                return { needsMenu: true };
            }

            if (!resignButton) {
                return { error: 'Resign button not found' };
            }

            // Get button coordinates for CDP click
            const rect = resignButton.getBoundingClientRect();
            return {
                found: true,
                x: rect.left + rect.width / 2,
                y: rect.top + rect.height / 2
            };
            """

            result = self.driver.execute_script(js_script)

            if result.get('needsMenu'):
                # Menu was opened, wait and try again
                time.sleep(0.5)
                result = self.driver.execute_script(js_script)

            if result.get('found'):
                x = result['x']
                y = result['y']

                print(f"[ChessCom] Clicking resign button at ({x}, {y})")

                # Click using CDP
                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mouseMoved',
                    'x': x,
                    'y': y
                })
                time.sleep(0.05)

                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mousePressed',
                    'x': x,
                    'y': y,
                    'button': 'left',
                    'clickCount': 1
                })
                time.sleep(0.05)

                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mouseReleased',
                    'x': x,
                    'y': y,
                    'button': 'left',
                    'clickCount': 1
                })

                # Wait for confirmation dialog
                time.sleep(0.5)

                # Look for confirmation button
                confirm_script = """
                const confirmButtons = document.querySelectorAll('button');
                for (const button of confirmButtons) {
                    const text = button.textContent?.toLowerCase() || '';
                    if (text.includes('resign') || text.includes('confirm') || text.includes('yes')) {
                        const rect = button.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            return {
                                found: true,
                                x: rect.left + rect.width / 2,
                                y: rect.top + rect.height / 2
                            };
                        }
                    }
                }
                return { found: false };
                """

                confirm_result = self.driver.execute_script(confirm_script)

                if confirm_result.get('found'):
                    # Click confirmation button
                    x = confirm_result['x']
                    y = confirm_result['y']

                    print(f"[ChessCom] Confirming resignation at ({x}, {y})")

                    self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                        'type': 'mouseMoved',
                        'x': x,
                        'y': y
                    })
                    time.sleep(0.05)

                    self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                        'type': 'mousePressed',
                        'x': x,
                        'y': y,
                        'button': 'left',
                        'clickCount': 1
                    })
                    time.sleep(0.05)

                    self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                        'type': 'mouseReleased',
                        'x': x,
                        'y': y,
                        'button': 'left',
                        'clickCount': 1
                    })

                print("[ChessCom] ✓ Resignation successful")
                return True
            else:
                print(f"[ChessCom] ✗ {result.get('error', 'Could not find resign button')}")
                print("[ChessCom] Hint: Make sure you're in an active game")
                return False

        except Exception as e:
            print(f"[ChessCom] ✗ Error resigning: {e}")
            import traceback
            traceback.print_exc()
            return False

    def rematch(self):
        """
        Request a rematch after a game ends by clicking the Rematch button.

        Returns:
            bool: True if rematch was successful, False otherwise
        """
        print("[ChessCom] Attempting to click Rematch button...")

        try:
            js_script = """
            // Find rematch button - appears after game ends
            const rematchSelectors = [
                'button[aria-label*="Rematch"]',
                'button[aria-label*="rematch"]',
                '[class*="rematch"]',
                'button[data-cy="rematch"]'
            ];

            let rematchButton = null;

            // Look for buttons with "rematch" text (not "play again")
            const allButtons = document.querySelectorAll('button, a');
            for (const button of allButtons) {
                const text = button.textContent?.toLowerCase() || '';
                const ariaLabel = button.getAttribute('aria-label')?.toLowerCase() || '';

                if (text.includes('rematch') || ariaLabel.includes('rematch')) {
                    const rect = button.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        rematchButton = button;
                        break;
                    }
                }
            }

            if (!rematchButton) {
                return { error: 'Rematch button not found - game may not be over yet' };
            }

            // Get button coordinates for CDP click
            const rect = rematchButton.getBoundingClientRect();
            return {
                found: true,
                x: rect.left + rect.width / 2,
                y: rect.top + rect.height / 2,
                text: rematchButton.textContent
            };
            """

            result = self.driver.execute_script(js_script)

            if result.get('found'):
                x = result['x']
                y = result['y']

                print(f"[ChessCom] Clicking Rematch button at ({x}, {y})")

                # Click using CDP
                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mouseMoved',
                    'x': x,
                    'y': y
                })
                time.sleep(0.05)

                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mousePressed',
                    'x': x,
                    'y': y,
                    'button': 'left',
                    'clickCount': 1
                })
                time.sleep(0.05)

                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mouseReleased',
                    'x': x,
                    'y': y,
                    'button': 'left',
                    'clickCount': 1
                })

                time.sleep(0.5)
                print("[ChessCom] ✓ Rematch button clicked")
                return True
            else:
                print(f"[ChessCom] ✗ {result.get('error', 'Could not find Rematch button')}")
                print("[ChessCom] Hint: Make sure the game has ended")
                return False

        except Exception as e:
            print(f"[ChessCom] ✗ Error clicking Rematch button: {e}")
            import traceback
            traceback.print_exc()
            return False

    def play_again(self):
        """
        Request to play again after a game ends by clicking the Play Again button.

        Returns:
            bool: True if play again was successful, False otherwise
        """
        print("[ChessCom] Attempting to click Play Again button...")

        try:
            js_script = """
            // Find play again button - appears after game ends
            const playAgainSelectors = [
                'button[aria-label*="Play"]',
                'button[aria-label*="play"]',
                '[class*="play-again"]',
                'button[data-cy="play-again"]'
            ];

            let playAgainButton = null;

            // Look for buttons with "play again" or "play" text (not "rematch")
            // Note: Button may be shortened to just "Play"
            // Important: Ignore buttons in the left sidebar
            const allButtons = document.querySelectorAll('button, a');
            for (const button of allButtons) {
                const text = button.textContent?.toLowerCase() || '';
                const ariaLabel = button.getAttribute('aria-label')?.toLowerCase() || '';

                // Check for "play again" or standalone "play" (but not "rematch")
                const hasPlayAgain = text.includes('play again') || ariaLabel.includes('play again');
                const hasPlay = (text.trim() === 'play' || text.includes('play')) && !text.includes('rematch');

                if (hasPlayAgain || hasPlay) {
                    const rect = button.getBoundingClientRect();

                    // Ignore buttons in the left sidebar (typically x < 250px)
                    // We want the Play button in the main game area
                    if (rect.width > 0 && rect.height > 0 && rect.left > 250) {
                        playAgainButton = button;
                        break;
                    }
                }
            }

            if (!playAgainButton) {
                return { error: 'Play Again button not found - game may not be over yet' };
            }

            // Get button coordinates for CDP click
            const rect = playAgainButton.getBoundingClientRect();
            return {
                found: true,
                x: rect.left + rect.width / 2,
                y: rect.top + rect.height / 2,
                text: playAgainButton.textContent
            };
            """

            result = self.driver.execute_script(js_script)

            if result.get('found'):
                x = result['x']
                y = result['y']

                print(f"[ChessCom] Clicking Play Again button at ({x}, {y})")

                # Click using CDP
                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mouseMoved',
                    'x': x,
                    'y': y
                })
                time.sleep(0.05)

                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mousePressed',
                    'x': x,
                    'y': y,
                    'button': 'left',
                    'clickCount': 1
                })
                time.sleep(0.05)

                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mouseReleased',
                    'x': x,
                    'y': y,
                    'button': 'left',
                    'clickCount': 1
                })

                time.sleep(0.5)
                print("[ChessCom] ✓ Play Again button clicked")
                return True
            else:
                print(f"[ChessCom] ✗ {result.get('error', 'Could not find Play Again button')}")
                print("[ChessCom] Hint: Make sure the game has ended")
                return False

        except Exception as e:
            print(f"[ChessCom] ✗ Error clicking Play Again button: {e}")
            import traceback
            traceback.print_exc()
            return False

    def exit_to_lobby(self):
        """
        Exit to the lobby after a game by clicking the Exit button.

        Returns:
            bool: True if exit was successful, False otherwise
        """
        print("[ChessCom] Attempting to click Exit button...")

        try:
            js_script = """
            // Find exit button - appears after game ends
            const exitSelectors = [
                'button[aria-label*="Exit"]',
                'button[aria-label*="exit"]',
                '[class*="exit"]',
                'button[data-cy="exit"]'
            ];

            let exitButton = null;

            // Look for buttons with "exit" text
            const allButtons = document.querySelectorAll('button, a');
            for (const button of allButtons) {
                const text = button.textContent?.toLowerCase() || '';
                const ariaLabel = button.getAttribute('aria-label')?.toLowerCase() || '';

                if (text.includes('exit') || ariaLabel.includes('exit')) {
                    const rect = button.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        exitButton = button;
                        break;
                    }
                }
            }

            if (!exitButton) {
                return { error: 'Exit button not found - game may not be over yet' };
            }

            // Get button coordinates for CDP click
            const rect = exitButton.getBoundingClientRect();
            return {
                found: true,
                x: rect.left + rect.width / 2,
                y: rect.top + rect.height / 2,
                text: exitButton.textContent
            };
            """

            result = self.driver.execute_script(js_script)

            if result.get('found'):
                x = result['x']
                y = result['y']

                print(f"[ChessCom] Clicking Exit button at ({x}, {y})")

                # Click using CDP
                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mouseMoved',
                    'x': x,
                    'y': y
                })
                time.sleep(0.05)

                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mousePressed',
                    'x': x,
                    'y': y,
                    'button': 'left',
                    'clickCount': 1
                })
                time.sleep(0.05)

                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mouseReleased',
                    'x': x,
                    'y': y,
                    'button': 'left',
                    'clickCount': 1
                })

                time.sleep(0.5)
                print("[ChessCom] ✓ Exit button clicked, returning to lobby")
                return True
            else:
                print(f"[ChessCom] ✗ {result.get('error', 'Could not find Exit button')}")
                print("[ChessCom] Hint: Make sure the game has ended or you're on the post-game screen")
                return False

        except Exception as e:
            print(f"[ChessCom] ✗ Error clicking Exit button: {e}")
            import traceback
            traceback.print_exc()
            return False

    def detect_game_started(self):
        """
        Detect if a new game has started by checking for:
        - Chat message with "Game #X started"
        - Blue notification pop-ups
        - Username in player boxes

        Returns:
            dict: {
                'started': bool,
                'game_number': str or None,
                'method': str (how it was detected)
            }
        """
        try:
            js_script = """
            const result = {
                started: false,
                game_number: null,
                method: null
            };

            // Method 1: Check for "Game #X started" in chat
            const chatMessages = document.querySelectorAll('[class*="chat"], [class*="message"]');
            for (const msg of chatMessages) {
                const text = msg.textContent || '';
                const match = text.match(/Game #(\\d+) started/i);
                if (match) {
                    result.started = true;
                    result.game_number = match[1];
                    result.method = 'chat_message';
                    return result;
                }
            }

            // Method 2: Check for blue notification pop-ups (game starting notifications)
            const notifications = document.querySelectorAll('[class*="notification"], [class*="alert"], [class*="toast"]');
            for (const notif of notifications) {
                const text = (notif.textContent || '').toLowerCase();
                if (text.includes('game has begun') ||
                    text.includes('game is starting') ||
                    text.includes('your game')) {
                    const rect = notif.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        result.started = true;
                        result.method = 'notification_popup';
                        return result;
                    }
                }
            }

            // Method 3: Check if username is in player boxes and game is active
            const playerBoxes = document.querySelectorAll('[class*="player"], [class*="user"]');
            let usernameFound = false;

            for (const box of playerBoxes) {
                const text = (box.textContent || '').trim();
                if (text.length > 0 && text.length < 50) {
                    // Check if this looks like an active game (timer present, etc.)
                    const parent = box.closest('[class*="player-component"], [class*="player-panel"]');
                    if (parent) {
                        const hasTimer = parent.querySelector('[class*="clock"], [class*="timer"]');
                        if (hasTimer) {
                            usernameFound = true;
                            break;
                        }
                    }
                }
            }

            if (usernameFound) {
                result.started = true;
                result.method = 'player_boxes';
            }

            return result;
            """

            result = self.driver.execute_script(js_script)
            return result

        except Exception as e:
            print(f"[ChessCom] Error detecting game start: {e}")
            return {'started': False, 'game_number': None, 'method': None}

    def setup_move_observer(self):
        """
        Set up a MutationObserver to watch for board/move changes.
        This detects when the opponent makes a move (truly event-driven).

        The observer sets a JS global flag, which Python polls via execute_script.
        (console.log-based approach doesn't work when attaching to existing browser)
        """
        js_script = """
        // Clean up any existing observer
        if (window.__moveObserver) {
            window.__moveObserver.disconnect();
            delete window.__moveObserver;
        }

        // Initialize the flag
        window.__boardChanged = false;

        // Debounce timer to batch rapid changes
        let debounceTimer = null;

        // Create the observer
        const observer = new MutationObserver(() => {
            // Debounce: wait 50ms for all related mutations to complete
            // Prevents duplicate triggers while maintaining responsiveness
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(() => {
                // Set global flag - Python polls this via execute_script
                window.__boardChanged = true;
            }, 50);
        });

        // Watch only the moves table - this fires exactly once per move played.
        // NOTE: Variants server uses .moves-table, NOT .move-list
        // Observing the board element (even childList only) fires on every piece
        // animation (elements added/removed during movement), so we avoid it.
        const moveTable = document.querySelector('.moves-table');
        const movesContainer = document.querySelector('[class*="moves"]');

        let observerActive = false;

        if (moveTable) {
            observer.observe(moveTable, {
                childList: true,
                subtree: true
            });
            observerActive = true;
        }

        if (movesContainer && movesContainer !== moveTable) {
            observer.observe(movesContainer, {
                childList: true,
                subtree: true
            });
            observerActive = true;
        }

        if (observerActive) {
            window.__moveObserver = observer;
            return true;
        } else {
            return false;
        }
        """

        try:
            result = self.driver.execute_script(js_script)
            return result
        except Exception as e:
            print(f"[ChessCom] Error setting up move observer: {e}")
            return False

    def reset_game_over_observer(self):
        """Reset the game over observer flags so the next game is detected."""
        try:
            self.driver.execute_script(
                "window.__gameOver = false; window.__gameOverLogged = false;"
            )
        except:
            pass

    def setup_game_over_observer(self):
        """
        Set up a MutationObserver to watch for game over dialog appearing.
        This is truly event-driven using console log notifications.

        The observer logs to console when a dialog appears.
        Python listens to browser console logs (no polling of DOM!).
        """
        js_script = """
        // Clean up any existing observer
        if (window.__gameOverObserver) {
            window.__gameOverObserver.disconnect();
            delete window.__gameOverObserver;
        }

        // Initialize flags (both exposed on window so Python can reset them)
        window.__gameOver = false;
        window.__gameOverLogged = false;

        // Create the observer
        const observer = new MutationObserver((mutations) => {
            // Only check if we haven't already logged
            if (window.__gameOverLogged) return;

            // Check if any game over dialog has appeared.
            // Use a broad set of class selectors since variants.chess.com may
            // use class names unlike the standard chess.com modal/dialog names.
            const GAME_OVER_RE = /black won|white won|you won|you lost|you drew|draw|checkmate|stalemate|time.*out|resign/i;

            const selectors =
                '[class*="modal"], [class*="dialog"], [class*="popup"], ' +
                '[class*="game-over"], [class*="result"], [class*="challenge"], ' +
                '[class*="win"], [class*="victory"], [class*="defeat"], [class*="end"]';

            for (const dialog of document.querySelectorAll(selectors)) {
                const rect = dialog.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    if (GAME_OVER_RE.test(dialog.textContent || '')) {
                        window.__gameOver = true;
                        window.__gameOverLogged = true;
                        return;
                    }
                }
            }

            // Fallback: scan visible, large-enough divs whose inline style
            // indicates overlay/modal positioning (position:fixed or z-index).
            for (const el of document.querySelectorAll(
                    '[style*="position: fixed"], [style*="position:fixed"], [style*="z-index"]')) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 150 && rect.height > 80) {
                    if (GAME_OVER_RE.test(el.textContent || '')) {
                        window.__gameOver = true;
                        window.__gameOverLogged = true;
                        return;
                    }
                }
            }
        });

        // Watch the entire document body for changes
        // This catches any new dialogs being added
        observer.observe(document.body, {
            childList: true,
            subtree: true,
            attributes: true,
            attributeFilter: ['class', 'style']  // Watch for dialogs becoming visible
        });

        window.__gameOverObserver = observer;
        return true;
        """

        try:
            self.driver.execute_script(js_script)
            return True
        except Exception as e:
            print(f"[ChessCom] Error setting up game over observer: {e}")
            return False

    def detect_game_over(self):
        """
        Detect if the game has ended by looking for the result dialog.

        Returns:
            dict: {
                'game_over': bool,
                'result': str or None (e.g., "Black Won", "White Won", "Draw"),
                'dialog_found': bool
            }
        """
        try:
            js_script = """
            const result = {
                game_over: false,
                result: null,
                dialog_found: false,
                dialog_coords: null
            };

            const GAME_OVER_RE = /black won|white won|you won|you lost|you drew|draw|checkmate|stalemate|time.*out|resign/i;

            function extractResult(text) {
                if (text.match(/black won/i))  return 'Black Won';
                if (text.match(/white won/i))  return 'White Won';
                if (text.match(/you won/i))    return 'You Won';
                if (text.match(/you lost/i))   return 'You Lost';
                if (text.match(/you drew/i))   return 'Draw';
                if (text.match(/stalemate/i))  return 'Stalemate';
                if (text.match(/draw/i))       return 'Draw';
                if (text.match(/resign/i))     return 'Resigned';
                return 'Game Over';
            }

            function checkElement(el) {
                const rect = el.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) return false;
                const text = el.textContent || '';
                if (!GAME_OVER_RE.test(text)) return false;
                result.game_over = true;
                result.dialog_found = true;
                result.result = extractResult(text);
                result.dialog_coords = {
                    left: rect.left, top: rect.top,
                    right: rect.right, bottom: rect.bottom,
                    width: rect.width, height: rect.height
                };
                return true;
            }

            // Primary sweep: broad set of class-name patterns
            const selectors =
                '[class*="modal"], [class*="dialog"], [class*="popup"], ' +
                '[class*="game-over"], [class*="result"], [class*="challenge"], ' +
                '[class*="win"], [class*="victory"], [class*="defeat"], [class*="end"]';
            for (const el of document.querySelectorAll(selectors)) {
                if (checkElement(el)) return result;
            }

            // Fallback: inline-styled overlay/modal elements
            for (const el of document.querySelectorAll(
                    '[style*="position: fixed"], [style*="position:fixed"], [style*="z-index"]')) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 150 && rect.height > 80 && checkElement(el)) return result;
            }

            return result;
            """

            result = self.driver.execute_script(js_script)
            return result

        except Exception as e:
            print(f"[ChessCom] Error detecting game over: {e}")
            return {'game_over': False, 'result': None, 'dialog_found': False}

    def dismiss_game_over_dialog(self):
        """
        Dismiss the game over dialog by clicking outside it or on the X button.

        Returns:
            bool: True if dismissal was successful, False otherwise
        """
        print("[ChessCom] Attempting to dismiss game over dialog...")

        try:
            js_script = """
            // Find the X button in the dialog
            const closeButtons = document.querySelectorAll(
                '[class*="close"], [class*="dismiss"], [aria-label*="close"], ' +
                '[aria-label*="Close"], button[class*="icon-"]'
            );

            for (const btn of closeButtons) {
                const rect = btn.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    // Check if this button is in the top-right area of a dialog
                    const parent = btn.closest('[class*="modal"], [class*="dialog"]');
                    if (parent) {
                        const parentRect = parent.getBoundingClientRect();
                        // X button should be in upper right
                        if (rect.right > parentRect.right - 50 && rect.top < parentRect.top + 50) {
                            return {
                                found: true,
                                method: 'close_button',
                                x: rect.left + rect.width / 2,
                                y: rect.top + rect.height / 2
                            };
                        }
                    }
                }
            }

            // If no X button found, click outside the dialog (backdrop)
            const dialogs = document.querySelectorAll('[class*="modal"], [class*="dialog"]');
            for (const dialog of dialogs) {
                const rect = dialog.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    // Click to the left of the dialog (on the backdrop)
                    return {
                        found: true,
                        method: 'backdrop',
                        x: Math.max(50, rect.left - 50),
                        y: rect.top + rect.height / 2
                    };
                }
            }

            return { found: false };
            """

            result = self.driver.execute_script(js_script)

            if result.get('found'):
                x = result['x']
                y = result['y']
                method = result['method']

                print(f"[ChessCom] Clicking to dismiss dialog ({method}) at ({x}, {y})")

                # Click using CDP
                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mouseMoved',
                    'x': x,
                    'y': y
                })
                time.sleep(0.05)

                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mousePressed',
                    'x': x,
                    'y': y,
                    'button': 'left',
                    'clickCount': 1
                })
                time.sleep(0.05)

                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mouseReleased',
                    'x': x,
                    'y': y,
                    'button': 'left',
                    'clickCount': 1
                })

                time.sleep(0.3)
                print("[ChessCom] ✓ Dialog dismissed")
                return True
            else:
                print("[ChessCom] ✗ Could not find dialog to dismiss")
                return False

        except Exception as e:
            print(f"[ChessCom] ✗ Error dismissing dialog: {e}")
            import traceback
            traceback.print_exc()
            return False

    def get_game_state(self):
        """
        Get the current game state (whether in an active game or not).

        Returns:
            dict: {
                'in_game': bool,
                'username': str or None,
                'color': str or None,
                'turn': str or None
            }
        """
        try:
            # Check if we can get player color (indicates active game)
            # Use verbose=False to avoid spamming console during automatic monitoring
            color = self.get_player_color(verbose=False)
            turn = self.get_turn()
            username = self.get_username_from_page(verbose=False)

            # We're in a game if data-player is 0 or 2, which means color is 'white' or 'black'
            # If color is 'unknown', it means data-player was None, so we're NOT in a game
            # We only need to check color since data-player is the reliable indicator
            in_game = (color in ['white', 'black'])

            return {
                'in_game': in_game,
                'username': username,
                'color': color,
                'turn': turn
            }

        except Exception as e:
            print(f"[ChessCom] Error getting game state: {e}")
            return {
                'in_game': False,
                'username': None,
                'color': None,
                'turn': None
            }

