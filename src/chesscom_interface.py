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
        # Cached board parameters (is_flipped, board_size) shared between
        # get_last_move() and make_move_cdp().  Board orientation and size
        # are stable for the entire game; recomputing them on every move
        # wastes 2-3 execute_script round-trips (~80-120 ms).  The cache is
        # invalidated externally when a new game starts.
        self._board_params_cache = None   # (is_flipped: bool, board_size: dict)
        self._board_params_time  = 0.0    # monotonic timestamp of last refresh

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

    # ── Board-parameter cache ─────────────────────────────────────────────────

    def _get_cached_board_params(self, max_age=60.0):
        """Return (is_flipped, board_size, board_rect), refreshing at most every max_age seconds.

        Board orientation, dimensions, and on-screen position are constant
        within a game; recomputing them on every move wastes execute_script
        round-trips (~80-120 ms each, and much worse when the tab is occluded
        and Chrome throttles JS execution).  The cache is invalidated
        externally when a new game starts.

        board_rect is {'left': float, 'top': float, 'width': float} and is
        extracted at no extra cost from the debug info already returned by
        get_board_orientation().  Callers can use _coords_for_square_py()
        to compute pixel coordinates in pure Python with zero execute_script
        calls.  board_rect may be None if the board element was not found.
        """
        now = time.monotonic()
        if self._board_params_cache and (now - self._board_params_time < max_age):
            return self._board_params_cache
        # get_board_orientation() already calls getBoundingClientRect() and
        # stores the result in debug.boardInfo — extract it for free rather
        # than running a separate script just to get the board rect.
        orientation = self.get_board_orientation()
        is_flipped  = orientation['is_flipped']
        board_info  = orientation.get('debug', {}).get('boardInfo') or {}
        board_rect  = (
            {'left': board_info['left'], 'top': board_info['top'],
             'width': board_info['width']}
            if board_info.get('width')
            else None
        )
        board_size  = self.detect_board_size()
        self._board_params_cache = (is_flipped, board_size, board_rect)
        self._board_params_time  = now
        return self._board_params_cache

    def invalidate_board_params_cache(self):
        """Discard cached board parameters (call when a new game starts)."""
        self._board_params_cache = None
        self._board_params_time  = 0.0

    def _coords_for_square_py(self, square, is_flipped, board_size, board_rect):
        """Return {'x': float, 'y': float} for square using pure Python arithmetic.

        This is the execute_script-free fast path for coordinate lookup.
        Requires board_rect from the cache (populated by _get_cached_board_params).
        The math mirrors what get_two_square_coordinates() does in JS.
        """
        file_letter  = square[0].lower()
        rank_number  = int(square[1:])
        file_num     = ord(file_letter) - ord('a') + 1
        num_files    = board_size.get('files', 8)
        num_ranks    = board_size.get('ranks', 8)
        sq_size      = board_rect['width'] / num_files
        if is_flipped:
            fi = num_files - file_num
            ri = rank_number - 1
        else:
            fi = file_num - 1
            ri = num_ranks - rank_number
        return {
            'x': board_rect['left'] + fi * sq_size + sq_size / 2,
            'y': board_rect['top']  + ri * sq_size + sq_size / 2,
        }

    def inject_background_fix(self):
        """Override Page Visibility API so chess.com stays active when the
        browser window is not in focus or is behind another full-screen app.

        chess.com pauses interactions when it sees document.hidden == true
        or document.hasFocus() == false.  Overriding these getters keeps the
        game running while the user has other windows open.
        """
        try:
            self.driver.execute_script("""
                if (window.__bgFixInjected) return;
                window.__bgFixInjected = true;
                try {
                    Object.defineProperty(document, 'hidden',
                        {get: () => false, configurable: true});
                    Object.defineProperty(document, 'visibilityState',
                        {get: () => 'visible', configurable: true});
                    document.hasFocus = () => true;
                    // Suppress blur events that tell the page it lost focus.
                    window.addEventListener('blur', e => e.stopImmediatePropagation(), true);
                } catch(e) {}
            """)
        except Exception:
            pass

    # ── Dual-square coordinate lookup ────────────────────────────────────────

    def get_two_square_coordinates(self, from_square, to_square, is_flipped, board_size):
        """Return pixel centres for two squares in a single execute_script call.

        Replacing two separate get_square_coordinates() calls (2 round-trips)
        with this method halves the number of execute_script calls needed for
        a move, saving ~40 ms per move.

        Returns:
            (from_coords, to_coords) where each is a dict with 'x' and 'y',
            or (None, None) on failure.
        """
        num_files = board_size.get('files', 8)
        num_ranks = board_size.get('ranks', 8)

        def _parse(sq):
            """Split algebraic square into (file_num, rank_number)."""
            if not sq or len(sq) < 2:
                return None, None
            file_letter = sq[0].lower()
            rank_str    = sq[1:]
            try:
                file_num     = ord(file_letter) - ord('a') + 1
                rank_number  = int(rank_str)
                return file_num, rank_number
            except ValueError:
                return None, None

        ff, fr = _parse(from_square)
        tf, tr = _parse(to_square)
        if ff is None or tf is None:
            return None, None

        js = f"""
        const board = document.querySelector('.TheBoard-squares') ||
                     document.querySelector('[class*="Board-squares"]') ||
                     document.querySelector('.board') ||
                     document.querySelector('[class*="board"]');
        if (!board) return null;
        const rect      = board.getBoundingClientRect();
        const numFiles  = {num_files};
        const numRanks  = {num_ranks};
        const sqSize    = rect.width / numFiles;
        const isFlipped = {str(is_flipped).lower()};

        const coords = function(fileNum, rankNumber) {{
            let fi, ri;
            if (isFlipped) {{
                fi = numFiles - fileNum;
                ri = rankNumber - 1;
            }} else {{
                fi = fileNum - 1;
                ri = numRanks - rankNumber;
            }}
            return {{
                x: rect.left + fi * sqSize + sqSize / 2,
                y: rect.top  + ri * sqSize + sqSize / 2
            }};
        }};
        return [coords({ff}, {fr}), coords({tf}, {tr})];
        """
        try:
            result = self.driver.execute_script(js)
            if result and len(result) == 2:
                return result[0], result[1]
        except Exception:
            pass
        return None, None

    # ─────────────────────────────────────────────────────────────────────────

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

            # Board geometry is stable for an entire game.  The cache is
            # filled with one execute_script on the first move (TTL 60 s,
            # explicit invalidation between games) and reused at zero cost
            # for every subsequent move — critical when the tab is occluded
            # and Chrome throttles JS execution to several seconds per call.
            is_flipped, board_size, board_rect = self._get_cached_board_params()

            # Compute square centres in pure Python from the cached board
            # rect.  This replaces the per-move execute_script in
            # get_two_square_coordinates() with simple arithmetic, so the
            # hot path after the first move contains zero execute_script
            # calls and is completely immune to Chrome's occlusion throttling.
            if board_rect:
                from_coords = self._coords_for_square_py(
                    from_square, is_flipped, board_size, board_rect)
                to_coords   = self._coords_for_square_py(
                    to_square,   is_flipped, board_size, board_rect)
            else:
                # Fallback: board element not found in cache; use the JS path.
                from_coords, to_coords = self.get_two_square_coordinates(
                    from_square, to_square, is_flipped, board_size
                )

            if not from_coords or not to_coords:
                print(f"[ChessCom] ✗ Could not find board squares")
                return False

            # Use CDP Input.dispatchMouseEvent for a click-click move.
            #
            # Why click-click instead of drag:
            #   • Drag suppresses chess.com's CSS piece animation; the site
            #     tracks the cursor position directly, so any synthetic drag
            #     (whether 1 step or 100) looks choppy because we control
            #     the visual, not the CSS engine.
            #   • Click-click (select piece → click destination) lets chess.com
            #     handle the animation itself via CSS transitions at native
            #     60 fps — exactly the same smooth motion as a human clicking.
            #   • It also eliminates the execute_script call that caused
            #     background-tab JS throttling (5 s overhead when minimised).
            #     All five events below go through the CDP input pipeline,
            #     which is not subject to background-tab throttling.
            try:
                x_from, y_from = from_coords['x'], from_coords['y']
                x_to,   y_to   = to_coords['x'],   to_coords['y']

                # Move cursor to the source square.
                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mouseMoved',
                    'x': x_from, 'y': y_from
                })

                # Click source square → piece becomes selected.
                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mousePressed',
                    'x': x_from, 'y': y_from,
                    'button': 'left', 'clickCount': 1
                })
                time.sleep(0.015)
                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mouseReleased',
                    'x': x_from, 'y': y_from,
                    'button': 'left', 'clickCount': 1
                })
                time.sleep(0.05)   # let piece-selection state register

                # Click destination square → piece placed; chess.com fires
                # its CSS transition animation from here.
                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mousePressed',
                    'x': x_to, 'y': y_to,
                    'button': 'left', 'clickCount': 1
                })
                time.sleep(0.015)
                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mouseReleased',
                    'x': x_to, 'y': y_to,
                    'button': 'left', 'clickCount': 1
                })

            except Exception as cdp_error:
                print(f"[ChessCom] ✗ CDP error: {cdp_error}")
                return False

            # Wait for move to process
            time.sleep(0.05)
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
                'a': 'H',  // Archbishop (UCI: a → chess.com: H)
                'c': 'E',  // Chancellor (UCI: c → chess.com: E)
                'd': 'Δ'   // Dragon Bishop (UCI: d → chess.com: Δ)
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
        try:
            # Get player color
            player_color = self.get_player_color(verbose=False)
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
                time.sleep(0.01)

                # Mouse down on pocket piece
                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mousePressed',
                    'x': from_coords['x'],
                    'y': from_coords['y'],
                    'button': 'left',
                    'clickCount': 1
                })
                time.sleep(0.02)

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

                # Mouse up at destination
                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mouseReleased',
                    'x': to_coords['x'],
                    'y': to_coords['y'],
                    'button': 'left',
                    'clickCount': 1
                })

                # Wait for move to process
                time.sleep(0.05)
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
                print("[ChessCom] ✓ Exit button clicked")

                # --- Click the Lobby tab ---
                lobby_result = self.driver.execute_script("""
                    function findTabByLabel(label) {
                        const re = new RegExp('^' + label + '$', 'i');
                        const walker = document.createTreeWalker(
                            document.body, NodeFilter.SHOW_TEXT, null
                        );
                        let node;
                        while ((node = walker.nextNode())) {
                            if (re.test(node.nodeValue.trim())) {
                                let el = node.parentElement;
                                while (el && el !== document.body) {
                                    const rect = el.getBoundingClientRect();
                                    if (rect.width > 0 && rect.height > 0) {
                                        return {
                                            found: true,
                                            x: rect.left + rect.width / 2,
                                            y: rect.top + rect.height / 2
                                        };
                                    }
                                    el = el.parentElement;
                                }
                            }
                        }
                        return { found: false };
                    }
                    return findTabByLabel('Lobby');
                """)

                if not lobby_result.get('found'):
                    print("[ChessCom] ✗ Lobby tab not found after exit")
                    return False

                lx, ly = lobby_result['x'], lobby_result['y']
                print(f"[ChessCom] Clicking Lobby tab at ({lx}, {ly})")

                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mouseMoved', 'x': lx, 'y': ly
                })
                time.sleep(0.05)
                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mousePressed', 'x': lx, 'y': ly,
                    'button': 'left', 'clickCount': 1
                })
                time.sleep(0.05)
                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mouseReleased', 'x': lx, 'y': ly,
                    'button': 'left', 'clickCount': 1
                })
                time.sleep(0.3)
                print("[ChessCom] ✓ Lobby tab clicked, returning to lobby")
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

    # Maps CLI shorthand → text to type into the variant search box.
    _VARIANT_SEARCH = {
        # Abbreviated command → full search term typed into the search box
        'koth':        'King of the Hill',
        'chaturanga':  'Chaturanga',
        'gothic':      'Gothic Chess',
        'duck':        'Duck Chess',
        'xxl':         'XXL Chess',
        'paradigm':    'Paradigm Chess30',
        'crazyhouse':  'Crazyhouse',
        '3check':      '3-Check',
        'atomic':      'Atomic',
        'horde':       'Horde',
        'racing':      'Racing Kings',
    }

    def create_challenge(self, variant_name, abort_check=None):
        """
        Begin the challenge-creation flow for the given variant.

        Steps (per spec):
          1. Click the 'Lobby' tab in the top-right navigation bar.
          2. Click the 'Play' (or 'Home') tab — the leftmost option in that bar.
          3. Press Escape (clears any stale dialog).
          4. Click the search box; press Backspace 20 times to clear old text.
          5. Type the variant name into the search box.

        Args:
            variant_name (str): CLI variant arg (e.g. 'koth', 'Chaturanga').
                                 'koth' is expanded to 'King of the Hill'; all
                                 others are used verbatim as the search term.
            abort_check (callable | None): optional zero-argument callable that
                returns True when the caller wants to abort mid-flow (e.g. a
                game was detected).  Checked between each major step; if it
                fires the function returns False immediately without typing.

        Returns:
            bool: True if all steps completed, False otherwise.
        """
        search_term = self._VARIANT_SEARCH.get(variant_name.lower(), variant_name)
        print(f"[ChessCom] Creating challenge for variant: {variant_name} "
              f"(search term: '{search_term}')")

        try:
            # --- Step 1: Click the Lobby tab ---
            # Use a TreeWalker to locate the raw text node "Lobby", then walk up
            # to find the first ancestor with a visible bounding box to click.
            # This is robust against SVG icon content polluting textContent.
            lobby_result = self.driver.execute_script("""
                function findTabByLabel(label) {
                    const re = new RegExp('^' + label + '$', 'i');
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null
                    );
                    let node;
                    while ((node = walker.nextNode())) {
                        if (re.test(node.nodeValue.trim())) {
                            let el = node.parentElement;
                            while (el && el !== document.body) {
                                const rect = el.getBoundingClientRect();
                                if (rect.width > 0 && rect.height > 0) {
                                    return {
                                        found: true,
                                        x: rect.left + rect.width / 2,
                                        y: rect.top + rect.height / 2
                                    };
                                }
                                el = el.parentElement;
                            }
                        }
                    }
                    return { found: false };
                }
                return findTabByLabel('Lobby');
            """)

            if not lobby_result.get('found'):
                print("[ChessCom] ✗ Lobby tab not found")
                return False

            x, y = lobby_result['x'], lobby_result['y']
            print(f"[ChessCom] Clicking Lobby tab at ({x:.0f}, {y:.0f})")
            self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                'type': 'mouseMoved', 'x': x, 'y': y
            })
            time.sleep(0.05)
            self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                'type': 'mousePressed', 'x': x, 'y': y,
                'button': 'left', 'clickCount': 1
            })
            time.sleep(0.05)
            self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                'type': 'mouseReleased', 'x': x, 'y': y,
                'button': 'left', 'clickCount': 1
            })
            time.sleep(0.2)

            # --- Step 2: Click the Play / Home tab (leftmost in the bar) ---
            play_result = self.driver.execute_script("""
                // The leftmost variants-panel tab is labelled either "Home" or
                // "Play" depending on server state. Require x > half the viewport
                // to exclude the left sidebar's own "Play" link.
                const midX = window.innerWidth / 2;
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_TEXT, null
                );
                let node;
                while ((node = walker.nextNode())) {
                    if (/^(home|play)$/i.test(node.nodeValue.trim())) {
                        let el = node.parentElement;
                        while (el && el !== document.body) {
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0 &&
                                    rect.left + rect.width / 2 > midX) {
                                return {
                                    found: true,
                                    x: rect.left + rect.width / 2,
                                    y: rect.top + rect.height / 2
                                };
                            }
                            el = el.parentElement;
                        }
                    }
                }
                return { found: false };
            """)

            if not play_result.get('found'):
                print("[ChessCom] ✗ Play/Home tab not found")
                return False

            x, y = play_result['x'], play_result['y']
            print(f"[ChessCom] Clicking Play/Home tab at ({x:.0f}, {y:.0f})")
            self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                'type': 'mouseMoved', 'x': x, 'y': y
            })
            time.sleep(0.05)
            self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                'type': 'mousePressed', 'x': x, 'y': y,
                'button': 'left', 'clickCount': 1
            })
            time.sleep(0.05)
            self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                'type': 'mouseReleased', 'x': x, 'y': y,
                'button': 'left', 'clickCount': 1
            })
            time.sleep(0.2)

            if abort_check and abort_check():
                print("[ChessCom] Abort: game detected after Lobby click — stopping challenge")
                return False

            # --- Step 3: Escape key (clears any stale dialog before search) ---
            self.driver.execute_cdp_cmd('Input.dispatchKeyEvent', {
                'type': 'keyDown',
                'key': 'Escape',
                'code': 'Escape',
                'windowsVirtualKeyCode': 27,
                'nativeVirtualKeyCode': 27
            })
            self.driver.execute_cdp_cmd('Input.dispatchKeyEvent', {
                'type': 'keyUp',
                'key': 'Escape',
                'code': 'Escape',
                'windowsVirtualKeyCode': 27,
                'nativeVirtualKeyCode': 27
            })
            time.sleep(0.15)

            if abort_check and abort_check():
                print("[ChessCom] Abort: game detected after Play/Home click — stopping challenge")
                return False

            # --- Step 4: Type the variant name into the search box ---
            # Find the first visible <input> in the right half of the viewport.
            # Also call inp.select() so any existing text is pre-selected and
            # a single Delete key will clear it without needing many Backspaces.
            search_result = self.driver.execute_script("""
                const midX = window.innerWidth / 2;
                for (const inp of document.querySelectorAll('input')) {
                    const rect = inp.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0 &&
                            rect.left + rect.width / 2 > midX) {
                        inp.focus();
                        // Clear via native setter so React sees the change
                        const setter = Object.getOwnPropertyDescriptor(
                            HTMLInputElement.prototype, 'value').set;
                        setter.call(inp, '');
                        inp.dispatchEvent(new Event('input', { bubbles: true }));
                        return {
                            found: true,
                            x: rect.left + rect.width / 2,
                            y: rect.top + rect.height / 2
                        };
                    }
                }
                return { found: false };
            """)

            if not search_result.get('found'):
                print("[ChessCom] ✗ Variant search box not found")
                return False

            # The JS already focused and cleared the input via the native setter.
            # Give React one tick to reconcile before typing.
            time.sleep(0.05)

            # Last chance to abort before any keypresses are sent — typing into
            # the wrong focused element (e.g. game chat) would be visible to the
            # opponent and a clear tell of automation.
            if abort_check and abort_check():
                print("[ChessCom] Abort: game detected before typing — stopping challenge")
                return False

            print(f"[ChessCom] Typing search term: '{search_term}'")
            for ch in search_term:
                self.driver.execute_cdp_cmd('Input.dispatchKeyEvent', {
                    'type': 'char', 'text': ch
                })
                time.sleep(0.03)

            if abort_check and abort_check():
                print("[ChessCom] Abort: game detected before Enter — stopping challenge")
                return False

            self.driver.execute_cdp_cmd('Input.dispatchKeyEvent', {
                'type': 'keyDown',
                'key': 'Enter',
                'code': 'Enter',
                'windowsVirtualKeyCode': 13,
                'nativeVirtualKeyCode': 13
            })
            self.driver.execute_cdp_cmd('Input.dispatchKeyEvent', {
                'type': 'keyUp',
                'key': 'Enter',
                'code': 'Enter',
                'windowsVirtualKeyCode': 13,
                'nativeVirtualKeyCode': 13
            })
            time.sleep(0.5)

            # --- Step 4c: Click the variant card that matches variant_name ---
            # Strategy: find the element whose *own* direct text exactly equals
            # the variant name (i.e. the big card title), then click it directly.
            # Clicking the title element lands on the right-hand text side of the
            # card, avoiding the icon on the left that can mis-navigate.
            CARD_TITLE_MAP = {
                'koth':      'king of the hill',
                'duck':      'duck chess',
                'xxl':       'xxl chess',
                'gothic':    'gothic chess',
                'paradigm':  'paradigm chess30',
            }
            target_lower = CARD_TITLE_MAP.get(
                variant_name.strip().lower(),
                variant_name.strip().lower()
            )
            card = self.driver.execute_script(f"""
                const target = {repr(target_lower)};

                for (const el of document.querySelectorAll('*')) {{
                    // Read only the direct text nodes of this element (not descendants)
                    // so we match the card title, not the full card text block.
                    let ownText = '';
                    for (const node of el.childNodes) {{
                        if (node.nodeType === Node.TEXT_NODE)
                            ownText += node.textContent;
                    }}
                    if (ownText.trim().toLowerCase() !== target) continue;

                    // Click the title element itself — it sits on the right side of
                    // the card, away from the icon on the left.
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;

                    return {{x: r.left + r.width / 2, y: r.top + r.height / 2,
                             text: ownText.trim()}};
                }}
                return null;
            """)

            if card:
                cx, cy = card['x'], card['y']
                print(f"[ChessCom] Clicking variant card '{card['text']}' at ({cx:.0f}, {cy:.0f})")
                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mousePressed', 'x': cx, 'y': cy,
                    'button': 'left', 'clickCount': 1
                })
                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mouseReleased', 'x': cx, 'y': cy,
                    'button': 'left', 'clickCount': 1
                })
            else:
                print(f"[ChessCom] Warning: no matching card found for '{variant_name}'; "
                      f"the search results may not have loaded yet or the name differs")

            time.sleep(0.2)

            # --- Step 5: Click the Play / Play! button ---
            # The button appears either inline in the panel ("Play!") or inside a
            # modal dialog ("Play") depending on the variant.  Find whichever one
            # is currently visible and click it.
            play_btn = self.driver.execute_script("""
                const leftGuard = window.innerWidth / 4;
                for (const el of document.querySelectorAll('button, a, [role="button"]')) {
                    let ownText = '';
                    for (const node of el.childNodes) {
                        if (node.nodeType === Node.TEXT_NODE)
                            ownText += node.textContent;
                    }
                    const t = ownText.trim();
                    if (t !== 'Play' && t !== 'Play!') continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    // Exclude left-sidebar links; 25% is generous enough to catch
                    // centered modals while still clearing the narrow sidebar
                    if (rect.left + rect.width / 2 <= leftGuard) continue;
                    return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2,
                            text: t};
                }
                return null;
            """)

            if not play_btn:
                print("[ChessCom] ✗ Play button not found")
                return False

            px, py = play_btn['x'], play_btn['y']
            print(f"[ChessCom] Clicking '{play_btn['text']}' button at ({px:.0f}, {py:.0f})")
            self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                'type': 'mousePressed', 'x': px, 'y': py,
                'button': 'left', 'clickCount': 1
            })
            self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                'type': 'mouseReleased', 'x': px, 'y': py,
                'button': 'left', 'clickCount': 1
            })
            time.sleep(0.2)

            # --- Step 6: Click Lobby tab (same as Step 1) ---
            lobby_result2 = self.driver.execute_script("""
                function findTabByLabel(label) {
                    const re = new RegExp('^' + label + '$', 'i');
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null
                    );
                    let node;
                    while ((node = walker.nextNode())) {
                        if (re.test(node.nodeValue.trim())) {
                            let el = node.parentElement;
                            while (el && el !== document.body) {
                                const rect = el.getBoundingClientRect();
                                if (rect.width > 0 && rect.height > 0) {
                                    return {
                                        found: true,
                                        x: rect.left + rect.width / 2,
                                        y: rect.top + rect.height / 2
                                    };
                                }
                                el = el.parentElement;
                            }
                        }
                    }
                    return { found: false };
                }
                return findTabByLabel('Lobby');
            """)

            if not lobby_result2.get('found'):
                print("[ChessCom] ✗ Lobby tab not found after placing challenge")
                return False

            lx, ly = lobby_result2['x'], lobby_result2['y']
            print(f"[ChessCom] Clicking Lobby tab at ({lx:.0f}, {ly:.0f})")
            self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                'type': 'mouseMoved', 'x': lx, 'y': ly
            })
            time.sleep(0.05)
            self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                'type': 'mousePressed', 'x': lx, 'y': ly,
                'button': 'left', 'clickCount': 1
            })
            time.sleep(0.05)
            self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                'type': 'mouseReleased', 'x': lx, 'y': ly,
                'button': 'left', 'clickCount': 1
            })
            time.sleep(0.3)

            print(f"[ChessCom] ✓ Challenge placed for {variant_name}")
            return True

        except Exception as e:
            print(f"[ChessCom] ✗ Error in create_challenge: {e}")
            import traceback
            traceback.print_exc()
            return False

    def cancel_challenge(self):
        """
        Cancel pending challenge(s) from the lobby by clicking the
        'Cancel' or 'Cancel All' button.

        Returns:
            bool: True if the button was found and clicked, False otherwise.
        """
        print("[ChessCom] Attempting to click Cancel / Cancel All button...")

        try:
            result = self.driver.execute_script("""
                function findBtn(label) {
                    const lower = label.toLowerCase();
                    for (const el of document.querySelectorAll('button, a, [role="button"]')) {
                        if (el.textContent.trim().toLowerCase() !== lower) continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) continue;
                        return {found: true,
                                x: rect.left + rect.width / 2,
                                y: rect.top + rect.height / 2,
                                text: label};
                    }
                    return null;
                }
                // Prefer "Cancel All" so individual per-row buttons are not hit
                // when a bulk button is present.
                return findBtn('Cancel All') || findBtn('Cancel') || {found: false};
            """)

            if not result.get('found'):
                print("[ChessCom] ✗ Cancel button not found — no pending challenges?")
                return False

            x, y, label = result['x'], result['y'], result['text']
            print(f"[ChessCom] Clicking '{label}' button at ({x:.0f}, {y:.0f})")
            self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                'type': 'mouseMoved', 'x': x, 'y': y
            })
            time.sleep(0.05)
            self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                'type': 'mousePressed', 'x': x, 'y': y,
                'button': 'left', 'clickCount': 1
            })
            time.sleep(0.05)
            self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                'type': 'mouseReleased', 'x': x, 'y': y,
                'button': 'left', 'clickCount': 1
            })
            time.sleep(0.3)
            print(f"[ChessCom] ✓ '{label}' button clicked")
            return True

        except Exception as e:
            print(f"[ChessCom] ✗ Error in cancel_challenge: {e}")
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
                "window.__gameOver = false; window.__gameOverLogged = false; window.__gameOverResult = null;"
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
        window.__gameOverResult = null;

        function extractResultText(text) {
            if (text.match(/black won/i))           return 'Black Won';
            if (text.match(/white won/i))           return 'White Won';
            if (text.match(/you won/i))             return 'You Won';
            if (text.match(/you lost/i))            return 'You Lost';
            if (text.match(/you drew/i))            return 'Draw';
            if (text.match(/draw by agreement/i))   return 'Draw (By Agreement)';
            if (text.match(/stalemate/i))           return 'Stalemate';
            if (text.match(/checkmate/i))           return 'Checkmate';
            if (text.match(/draw/i))                return 'Draw';
            if (text.match(/time.*out|flagged/i))   return 'Timeout';
            if (text.match(/resign/i))              return 'Resigned';
            if (text.match(/abandon/i))             return 'Abandoned';
            return 'Game Over';
        }

        // Create the observer
        const observer = new MutationObserver((mutations) => {
            // Only check if we haven't already logged
            if (window.__gameOverLogged) return;

            // Check if any game over dialog has appeared.
            // Use a broad set of class selectors since variants.chess.com may
            // use class names unlike the standard chess.com modal/dialog names.
            const GAME_OVER_RE = /black won|white won|you won|you lost|you drew|draw by agreement|draw|checkmate|stalemate|time.*out|flagged|resign|abandon/i;

            const selectors =
                '[class*="modal"], [class*="dialog"], [class*="popup"], ' +
                '[class*="game-over"], [class*="result"], [class*="challenge"], ' +
                '[class*="win"], [class*="victory"], [class*="defeat"], [class*="end"]';

            for (const dialog of document.querySelectorAll(selectors)) {
                const rect = dialog.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    const text = dialog.textContent || '';
                    if (GAME_OVER_RE.test(text)) {
                        window.__gameOver = true;
                        window.__gameOverLogged = true;
                        window.__gameOverResult = extractResultText(text);
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
                    const text = el.textContent || '';
                    if (GAME_OVER_RE.test(text)) {
                        window.__gameOver = true;
                        window.__gameOverLogged = true;
                        window.__gameOverResult = extractResultText(text);
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

            // If the MutationObserver already detected and cached the result, use it
            if (window.__gameOverResult) {
                result.game_over = true;
                result.dialog_found = true;
                result.result = window.__gameOverResult;
                // Still find dialog coords for dismissal
            }

            const GAME_OVER_RE = /black won|white won|you won|you lost|you drew|draw by agreement|draw|checkmate|stalemate|time.*out|flagged|resign|abandon/i;

            function extractResult(text) {
                if (text.match(/black won/i))           return 'Black Won';
                if (text.match(/white won/i))           return 'White Won';
                if (text.match(/you won/i))             return 'You Won';
                if (text.match(/you lost/i))            return 'You Lost';
                if (text.match(/you drew/i))            return 'Draw';
                if (text.match(/draw by agreement/i))   return 'Draw (By Agreement)';
                if (text.match(/stalemate/i))           return 'Stalemate';
                if (text.match(/checkmate/i))           return 'Checkmate';
                if (text.match(/draw/i))                return 'Draw';
                if (text.match(/time.*out|flagged/i))   return 'Timeout';
                if (text.match(/resign/i))              return 'Resigned';
                if (text.match(/abandon/i))             return 'Abandoned';
                return 'Game Over';
            }

            function checkElement(el) {
                const rect = el.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) return false;
                const text = el.textContent || '';
                if (!GAME_OVER_RE.test(text)) return false;
                result.game_over = true;
                result.dialog_found = true;
                // Prefer the observer-cached result; fall back to re-extracting
                if (!result.result) result.result = extractResult(text);
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
        Dismiss the game over dialog.

        Attempts dismissal in this order:
          1. Escape key press (works for most modal overlays)
          2. CDP mouse click on a close/X button found via JS
          3. Backdrop click (left of the dialog)

        Returns:
            bool: True if a dismissal action was taken, False if nothing was found
        """
        print("[ChessCom] Attempting to dismiss game over dialog...")

        try:
            # --- Strategy 1: Escape key ---
            self.driver.execute_cdp_cmd('Input.dispatchKeyEvent', {
                'type': 'keyDown',
                'key': 'Escape',
                'code': 'Escape',
                'windowsVirtualKeyCode': 27,
                'nativeVirtualKeyCode': 27
            })
            self.driver.execute_cdp_cmd('Input.dispatchKeyEvent', {
                'type': 'keyUp',
                'key': 'Escape',
                'code': 'Escape',
                'windowsVirtualKeyCode': 27,
                'nativeVirtualKeyCode': 27
            })
            time.sleep(0.3)

            # Check if the dialog is gone after Escape
            still_open = self.driver.execute_script("""
                const GAME_OVER_RE = /black won|white won|you won|you lost|you drew|draw|checkmate|stalemate|time.*out|flagged|resign|abandon/i;
                const selectors =
                    '[class*="modal"], [class*="dialog"], [class*="popup"], ' +
                    '[class*="game-over"], [class*="result"]';
                for (const el of document.querySelectorAll(selectors)) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0 && GAME_OVER_RE.test(el.textContent || '')) {
                        return true;
                    }
                }
                return false;
            """)

            if not still_open:
                print("[ChessCom] ✓ Dialog dismissed via Escape key")
                return True

            # --- Strategies 2 & 3: Find the close/X button, then CDP-click it ---
            js_script = """
            // Candidates: explicit close/dismiss buttons and aria-labelled buttons
            const closeSelectors = [
                '[aria-label="Close"]',
                '[aria-label="close"]',
                '[aria-label*="Close"]',
                '[aria-label*="close"]',
                '[class*="close"]',
                '[class*="dismiss"]',
                'button[class*="icon-"]'
            ];

            for (const sel of closeSelectors) {
                for (const btn of document.querySelectorAll(sel)) {
                    const rect = btn.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) continue;

                    // Must be inside a modal/dialog container
                    const parent = btn.closest(
                        '[class*="modal"], [class*="dialog"], [class*="popup"], [class*="game-over"]'
                    );
                    if (!parent) continue;

                    const parentRect = parent.getBoundingClientRect();
                    // X button is typically in upper-right corner of the dialog
                    if (rect.right > parentRect.right - 60 && rect.top < parentRect.top + 60) {
                        return {
                            found: true,
                            method: 'close_button',
                            x: rect.left + rect.width / 2,
                            y: rect.top + rect.height / 2
                        };
                    }
                }
            }

            // If no X button found, fall back to backdrop (left of dialog)
            const dialogs = document.querySelectorAll(
                '[class*="modal"], [class*="dialog"], [class*="popup"], [class*="game-over"]'
            );
            for (const dialog of dialogs) {
                const rect = dialog.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    return {
                        found: true,
                        method: 'backdrop',
                        x: Math.max(10, rect.left - 60),
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

                # Strategy 2: CDP mouse click at the coordinates
                print(f"[ChessCom] Clicking to dismiss dialog ({method}) at ({x:.0f}, {y:.0f})")

                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mouseMoved', 'x': x, 'y': y
                })
                time.sleep(0.05)
                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mousePressed', 'x': x, 'y': y,
                    'button': 'left', 'clickCount': 1
                })
                time.sleep(0.05)
                self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                    'type': 'mouseReleased', 'x': x, 'y': y,
                    'button': 'left', 'clickCount': 1
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

    def get_variant_name(self):
        """Return a lowercase variant slug from the current page URL/title, or ''."""
        _KNOWN = (
            'chaturanga', 'capablanca', 'gothic', 'paradigm', 'courier',
            'amazon', 'grandchess', 'crazyhouse', 'chess960', 'horde',
            'kingofthehill', 'threecheck', 'racingkings', 'giveaway', 'antichess',
        )
        try:
            url = self.driver.execute_script("return window.location.href") or ''
            for name in _KNOWN:
                if name in url.lower():
                    return name
            title = self.driver.execute_script("return document.title") or ''
            for name in _KNOWN:
                if name in title.lower():
                    return name
        except Exception:
            pass
        return ''

    def get_ingame_variant_label(self):
        """Read the human-readable variant name shown in the top-right of the game page.

        Scans the right half of the viewport for an element whose sole direct
        text matches one of the known chess.com variant display names (e.g.
        'Gothic Chess', 'Chaturanga').  Falls back to URL/title slug detection
        via get_variant_name() when no DOM match is found.

        Returns:
            str: Display name such as 'Gothic Chess', or the slug from
                 get_variant_name(), or '' if detection fails entirely.
        """
        # Human-readable names as they appear on chess.com
        DISPLAY_NAMES = [
            'Chaturanga', 'Capablanca Chess', 'Gothic Chess', 'Paradigm Chess30',
            'Courier Chess', 'Amazon Chess', 'Grand Chess', 'Crazyhouse',
            'Chess960', 'Fischerandom', 'Horde', 'King of the Hill',
            '3-Check', 'Three-Check', 'Racing Kings', 'Duck Chess',
            'XXL Chess', 'Atomic Chess', 'Atomic', 'Bughouse', 'Antichess', 'Giveaway',
        ]
        try:
            result = self.driver.execute_script("""
                const names = arguments[0];
                const midX  = window.innerWidth / 2;
                const lower = names.map(n => n.toLowerCase());

                // First pass: right half of viewport only (true "top-right" label)
                for (const el of document.querySelectorAll('*')) {
                    // Match elements whose OWN direct text (not descendants) equals a name
                    let ownText = '';
                    for (const node of el.childNodes) {
                        if (node.nodeType === Node.TEXT_NODE)
                            ownText += node.textContent;
                    }
                    ownText = ownText.trim();
                    if (!ownText) continue;

                    const idx = lower.indexOf(ownText.toLowerCase());
                    if (idx === -1) continue;

                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    if (rect.left + rect.width / 2 > midX) {
                        return names[idx];   // Return the canonical display name
                    }
                }

                // Second pass: anywhere on page (handles centred/narrow layouts)
                for (const el of document.querySelectorAll('*')) {
                    let ownText = '';
                    for (const node of el.childNodes) {
                        if (node.nodeType === Node.TEXT_NODE)
                            ownText += node.textContent;
                    }
                    ownText = ownText.trim();
                    if (!ownText) continue;

                    const idx = lower.indexOf(ownText.toLowerCase());
                    if (idx === -1) continue;

                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0)
                        return names[idx];
                }
                return null;
            """, DISPLAY_NAMES)

            if result:
                return result
        except Exception:
            pass

        # Fallback: URL / title slug
        return self.get_variant_name()

    @staticmethod
    def _parse_title_coords(text, num_files, num_ranks, variant=''):
        """
        Parse a move string that uses chess.com's internal 14×14 coordinate
        system and convert it to standard UCI notation.

        Chess.com centers every variant board on a conceptual 14×14 grid.
        A square that is (file_idx, rank) in 1-based algebraic notation
        appears as (file_idx + offset_f, rank + offset_r) in the 14×14
        frame, where:

            offset_f = (14 - num_files) // 2
            offset_r = (14 - num_ranks) // 2

        Examples
        --------
        8×8 board (offset 3, 3):  g8 → j11,  h6 → k9
        4×4 board (offset 5, 5):  b2 → g7

        The move text format is:
            [PieceLetter] from_file from_rank [-|x] to_file to_rank [=PromoLetter]

        e.g.  "Nj11-k9"    knight from j11 to k9
              "j2-j4"      pawn push
              "j7xk8=Q"    pawn capture-promotion

        Returns
        -------
        str | None  UCI move string ("g8h6", "a7a8q", …) or None if the text
                    cannot be parsed or the converted squares are out of range.
        """
        import re

        # ── Normalise chess.com special Unicode piece symbols to ASCII ────────
        # Δ (Greek capital Delta) is used for the Dragon Bishop; replace it with
        # 'D' so the regex below can treat it as any other piece-letter prefix.
        text = text.replace('Δ', 'D').replace('δ', 'd')

        offset_f = (14 - num_files) // 2
        offset_r = (14 - num_ranks) // 2

        def _conv(f_ch, r_str):
            """Convert a 14×14 file letter + rank string to an actual square
            string, e.g. ('k', '9') → 'h6' on an 8×8 board.
            Returns None when the square falls outside the board."""
            fi = ord(f_ch.lower()) - ord('a') - offset_f   # 0-based actual file
            r  = int(r_str) - offset_r                      # 1-based actual rank
            if not (0 <= fi < num_files and 1 <= r <= num_ranks):
                return None
            return chr(ord('a') + fi) + str(r)

        # ── Piece drop: @[irrelevant][piece]-file+rank  →  UCI "Q@e4" notation ─
        # Example: "@rQ-h8+" → drop a Queen to h8 (14×14) → Q@e5 on an 8×8 board.
        # The single character immediately after '@' carries no useful information.
        drop_m = re.match(r'^@.([A-Z])[x\-]([a-n])(\d{1,2})', text, re.IGNORECASE)
        if drop_m:
            piece_letter = drop_m.group(1).upper()
            to_sq = _conv(drop_m.group(2), drop_m.group(3))
            if to_sq is None:
                return None
            return piece_letter + '@' + to_sq

        # ── Standard / capture move ───────────────────────────────────────────
        # Format: [piece] from_sq [-|x] [captured_piece] to_sq [=promo]
        # The captured-piece letter (e.g. the "N" in "k8xNj9") is optional and
        # must be skipped – it is a capital letter immediately after the "x".
        m = re.match(
            r'^[A-Za-z]?([a-n])(\d{1,2})[x\-][A-Za-z]?([a-n])(\d{1,2})(?:=([A-Za-z]))?',
            text
        )
        if not m:
            return None
        from_f_ch, from_r_str, to_f_ch, to_r_str, promo = m.groups()

        from_sq = _conv(from_f_ch, from_r_str)
        to_sq   = _conv(to_f_ch,   to_r_str)
        if from_sq is None or to_sq is None:
            return None

        uci = from_sq + to_sq
        if promo:
            # chess.com promotion letter → internal UCI letter.
            # E = Chancellor → c   H = Archbishop → a
            # F = Ferz; in Chaturanga this promotes to a queen-strength piece.
            # D = Dragon Bishop → d   (all others pass through unchanged)
            _cc_promo = {'e': 'c', 'h': 'a'}
            if variant == 'chaturanga':
                _cc_promo['f'] = 'q'
            p = promo.lower()
            uci += _cc_promo.get(p, p)
        return uci

    def get_last_move(self, verbose=True):
        """
        Detect the last move played on the board by combining two sources of
        information gathered in a single script call:

          1. Move-list text  – the last entry in .moves-table-cell.moves-move,
             stripped of check/mate markers (+/#).  Used as the ground-truth
             signal for castling (O-O / O-O-O).

          2. Board highlights – Chess.com's last-move overlay elements.  Used
             to recover the exact source and destination squares for every move
             type.

        Move types handled:
          - 1 highlighted square  → piece-drop  e.g. "Q@f3", "A@e4"
          - 2 highlighted squares → normal move e.g. "e2e4", "g1f3"
                                    or promotion e.g. "a7a8q", "a7a8c"
          - O-O / O-O-O in move list → castling, king→rook UCI encoding
                                        e.g. "e1h1", "e1a1"

        Returns:
            str: UCI move string, or None if the move could not be determined.
        """
        # Use shared cache so the board-param round-trips already paid by the
        # monitor cycle (or by make_move_cdp) are not repeated here.
        is_flipped, board_size, _board_rect = self._get_cached_board_params()
        variant_name = self.get_variant_name()
        num_files = board_size.get('files', 8)
        num_ranks = board_size.get('ranks', 8)

        js_script = f"""
        return (function() {{
            // ── chess.com piece letter → UCI character ──────────────────────
            // Used for class-encoded pieces ("piece wn").
            // chess.com: E=Chancellor, H=Archbishop; UCI: c=Chancellor, a=Archbishop
            const chesscomToUci = {{
                'p': 'p', 'n': 'n', 'b': 'b', 'r': 'r',
                'q': 'q', 'k': 'k',
                'e': 'c',   // Chancellor  (chess.com E → UCI c)
                'h': 'a',   // Archbishop  (chess.com H → UCI a)
                'u': 'u', 'w': 'w', 'f': 'f', 'd': 'd'
            }};

            // data-piece values from the TheBoard architecture (uppercase).
            // Δ is chess.com's symbol for the Dragon Bishop.
            const dataPieceToUci = {{
                'R':'r','N':'n','B':'b','Q':'q','K':'k','P':'p',
                'E':'c', 'H':'a', 'Δ':'d', 'D':'d',
                'U':'u', 'W':'w', 'F':'f'
            }};

            // data-color: chess.com's internal player-color codes.
            const dataColorToStr = {{ '5': 'white', '6': 'black' }};

            const numFiles  = {num_files};
            const numRanks  = {num_ranks};
            const isFlipped = {str(is_flipped).lower()};

            // ── Read last move text from move list ──────────────────────────
            // Reuses the same selector as get_turn().
            let lastMoveText  = null;
            let lastMoveTitle = null;   // title attribute – contains raw 14×14 coords
            let actualMovesLength = 0;  // total moves played (used to determine castling color)
            const moveTable = document.querySelector('.moves-table');
            if (moveTable) {{
                const moveCells = moveTable.querySelectorAll(
                    '.moves-table-cell.moves-move'
                );
                const actualMoves = Array.from(moveCells).filter(
                    c => c.textContent.trim().length > 0
                );
                actualMovesLength = actualMoves.length;
                if (actualMoves.length > 0) {{
                    const lastCell = actualMoves[actualMoves.length - 1];
                    const raw = lastCell.textContent.trim();
                    // Strip check (+) and mate (#) markers – irrelevant to what moved
                    lastMoveText  = raw.replace(/[+#]/g, '');
                    // The title attribute lives on an inner child (e.g. the
                    // .moves-pointer element), not on the outer cell element.
                    lastMoveTitle = lastCell.getAttribute('title')
                                 || lastCell.querySelector('[title]')?.getAttribute('title');
                }}
            }}

            // ── Locate the board ────────────────────────────────────────────
            const board = document.querySelector('.TheBoard-squares') ||
                          document.querySelector('[class*="Board-squares"]') ||
                          document.querySelector('.board') ||
                          document.querySelector('[class*="board"]');
            if (!board) return {{ error: 'Board not found' }};

            // Use the board dimensions from detect_board_size() (coordinate-label
            // detection).  A previous DOM child-count heuristic was unreliable:
            // on 10x8 boards the board element's direct children are file-columns
            // (10 items), not rank-rows, so it returned files/ranks swapped.
            const numFilesActual = numFiles;
            const numRanksActual = numRanks;

            const boardRect  = board.getBoundingClientRect();
            const squareW    = boardRect.width  / numFiles;
            const squareH    = boardRect.height / numRanks;

            // ── Pixel → algebraic square ────────────────────────────────────
            function pixelToSquare(cx, cy) {{
                if (cx < boardRect.left - 2 || cx > boardRect.right  + 2) return null;
                if (cy < boardRect.top  - 2 || cy > boardRect.bottom + 2) return null;

                const fi = Math.min(Math.floor((cx - boardRect.left) / squareW), numFiles - 1);
                const ri = Math.min(Math.floor((cy - boardRect.top)  / squareH), numRanks - 1);

                let file, rank;
                if (isFlipped) {{
                    file = String.fromCharCode('a'.charCodeAt(0) + (numFiles - 1 - fi));
                    rank = ri + 1;
                }} else {{
                    file = String.fromCharCode('a'.charCodeAt(0) + fi);
                    rank = numRanks - ri;
                }}
                return file + rank;
            }}

            // ── Extract UCI piece type from element's className ──────────────
            function pieceTypeFromClass(cls) {{
                if (typeof cls !== 'string') return null;
                // Pattern: "piece w<letter>" or "piece b<letter>"
                const m = cls.match(/piece\\s+[wb]([a-z])/);
                if (!m) return null;
                return chesscomToUci[m[1]] || m[1];
            }}

            // ── Extract color ('white'/'black') from className ──────────────
            function pieceColorFromClass(cls) {{
                if (typeof cls !== 'string') return null;
                const m = cls.match(/piece\\s+([wb])/);
                if (!m) return null;
                return m[1] === 'w' ? 'white' : 'black';
            }}

            // ── Element-level helpers: prefer data-* attrs (TheBoard arch) ──
            // Falls back to class-based detection for the traditional .board.
            function pieceTypeFromEl(el) {{
                const dp = el.getAttribute('data-piece');
                if (dp) return dataPieceToUci[dp] || null;
                return pieceTypeFromClass(el.className);
            }}
            function pieceColorFromEl(el) {{
                const dc = el.getAttribute('data-color');
                if (dc) return dataColorToStr[dc] || null;
                return pieceColorFromClass(el.className);
            }}

            // ── Collect highlighted squares ─────────────────────────────────
            const highlightEls = document.querySelectorAll('[class*="highlight"]');
            const seenSquares  = new Set();
            const highlighted  = [];
            const rawHighlightCount = highlightEls.length;

            // Helper: extract an algebraic square name (e.g. "f3") from a
            // title/aria-label string, e.g. "Square f3", "f3 highlight", "f3".
            function sqFromTitle(attr) {{
                if (!attr) return null;
                const m = attr.match(/\b([a-n]\d{{1,2}})\b/i);
                return m ? m[1].toLowerCase() : null;
            }}

            // Primary: title attribute first (most direct + works for variants),
            // then pixel-based getBoundingClientRect as fallback.
            for (const el of highlightEls) {{
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) continue;
                const cx = (rect.left + rect.right)  / 2;
                const cy = (rect.top  + rect.bottom) / 2;

                // Try title/aria-label on the element and its nearest ancestor
                let sq = sqFromTitle(el.getAttribute('title'))
                      || sqFromTitle(el.getAttribute('aria-label'))
                      || sqFromTitle(el.parentElement?.getAttribute('title'))
                      || sqFromTitle(el.parentElement?.getAttribute('aria-label'))
                      || sqFromTitle(el.closest('[title]')?.getAttribute('title'));
                if (!sq) sq = pixelToSquare(cx, cy);

                if (!sq || seenSquares.has(sq)) continue;
                seenSquares.add(sq);
                highlighted.push({{ square: sq,
                    left: rect.left, right: rect.right,
                    top:  rect.top,  bottom: rect.bottom }});
            }}

            // Fallback 1: extract square from CSS class (e.g. 'square-35' → 'c5').
            // Used when the pixel approach finds nothing – e.g. elements are
            // inside a shadow DOM or the board rect belongs to a different
            // container than where the highlights are positioned.
            if (highlighted.length === 0) {{
                for (const el of highlightEls) {{
                    const cls = typeof el.className === 'string' ? el.className : '';
                    const m = cls.match(/(?:^| )square-(\d+)(?= |$)/);
                    if (!m) continue;
                    const code = m[1];
                    // chess.com square code: last char = rank (1-8),
                    // leading chars = file index (1=a, 2=b, …, 10=j, …)
                    const rankNum   = parseInt(code.slice(-1), 10);
                    const fileDigit = parseInt(code.slice(0, -1), 10);
                    if (isNaN(rankNum) || isNaN(fileDigit) || fileDigit < 1) continue;
                    const sq = String.fromCharCode('a'.charCodeAt(0) + fileDigit - 1) + rankNum;
                    if (seenSquares.has(sq)) continue;
                    seenSquares.add(sq);
                    highlighted.push({{ square: sq }});
                }}
            }}

            // Fallback 2: scan every square's computed background colour.
            // Chess.com variants don't add a CSS class or inline style to
            // highlighted squares; instead the highlight is applied via CSS
            // rules that change the element's rendered background.
            // Strategy:
            //   • collect getComputedStyle().backgroundColor for all squares
            //   • the two most frequent colours are the normal light/dark tile colours
            //   • any square with a different colour is a highlight candidate
            //   • piece presence is determined by a direct rect-overlap test
            //     against each piece element's getBoundingClientRect() – this
            //     avoids the globally-built pieceMap which has wrong keys when
            //     board-size detection is off (e.g. 8×8 detected for a 10×10 board)
            if (highlighted.length === 0 && board) {{
                // Snapshot piece visual centres before entering the square loop
                const pieceCenters = [];
                for (const el of document.querySelectorAll('[class*="piece"]')) {{
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    const pt = pieceTypeFromEl(el);
                    const pc = pieceColorFromEl(el);
                    if (!pt || !pc) continue;
                    pieceCenters.push({{
                        cx: (r.left + r.right) / 2,
                        cy: (r.top  + r.bottom) / 2,
                        left: r.left, right: r.right, top: r.top, bottom: r.bottom,
                        pieceType: pt, pieceColor: pc
                    }});
                }}

                const bgCount = {{}};
                const allSqBgs = [];
                for (const rankRow of board.children) {{
                    for (const sqEl of rankRow.children) {{
                        const bg = window.getComputedStyle(sqEl).backgroundColor;
                        bgCount[bg] = (bgCount[bg] || 0) + 1;
                        allSqBgs.push({{ sqEl, bg }});
                    }}
                }}
                // Two most common bg colours = normal square colours
                const normalBgs = new Set(
                    Object.entries(bgCount)
                          .sort((a, b) => b[1] - a[1])
                          .slice(0, 2)
                          .map(([c]) => c)
                );
                for (const {{ sqEl, bg }} of allSqBgs) {{
                    if (normalBgs.has(bg)) continue;
                    const sqRect = sqEl.getBoundingClientRect();
                    if (sqRect.width === 0 || sqRect.height === 0) continue;
                    const cx = (sqRect.left + sqRect.right) / 2;
                    const cy = (sqRect.top  + sqRect.bottom) / 2;
                    const sq = pixelToSquare(cx, cy);
                    if (!sq || seenSquares.has(sq)) continue;
                    seenSquares.add(sq);
                    // Overlap test: find a piece whose visual centre is inside this square
                    let piece = null, color = null;
                    for (const pc of pieceCenters) {{
                        if (pc.cx >= sqRect.left && pc.cx <= sqRect.right &&
                            pc.cy >= sqRect.top  && pc.cy <= sqRect.bottom) {{
                            piece = pc.pieceType;
                            color = pc.pieceColor;
                            break;
                        }}
                    }}
                    highlighted.push({{ square: sq, piece, color }});
                }}
            }}

            // ── Collect board pieces ────────────────────────────────────────
            // Map: square → {{ pieceType, pieceColor }}
            //
            // Strategy: walk the board DOM (rank-rows → square elements) so
            // that we can read each square element's title/aria-label for the
            // square name, and look for piece children inside it.  This avoids
            // calling pixelToSquare on piece elements whose bounding rect can be
            // slightly off due to CSS transitions / animations.
            //
            // If pieces are NOT children of their square elements (some board
            // implementations float all pieces at the board root), the fallback
            // global scan uses the overlap approach instead.
            const pieceMap = {{}};
            let piecesFoundViaStructure = 0;

            for (const rankRow of board.children) {{
                for (const sqEl of rankRow.children) {{
                    const sqRect = sqEl.getBoundingClientRect();
                    if (sqRect.width === 0 || sqRect.height === 0) continue;

                    // Square name: title attr > aria-label > pixelToSquare on centre
                    let sqName = sqFromTitle(sqEl.getAttribute('title'))
                              || sqFromTitle(sqEl.getAttribute('aria-label'));
                    if (!sqName) {{
                        const cx = (sqRect.left + sqRect.right) / 2;
                        const cy = (sqRect.top  + sqRect.bottom) / 2;
                        sqName = pixelToSquare(cx, cy);
                    }}
                    if (!sqName) continue;

                    // Find a piece that is a descendant of this square element
                    for (const child of sqEl.querySelectorAll('[class*="piece"]')) {{
                        const pt = pieceTypeFromEl(child);
                        const pc = pieceColorFromEl(child);
                        if (pt && pc) {{
                            pieceMap[sqName] = {{ pieceType: pt, pieceColor: pc }};
                            piecesFoundViaStructure++;
                            break;
                        }}
                    }}
                }}
            }}

            // Fallback: if no pieces were found as square children (pieces live
            // at the board root), scan globally and use a spatial overlap test
            // against the highlighted square rects to assign them.
            const pieceEls = document.querySelectorAll('[class*="piece"]');
            if (piecesFoundViaStructure === 0) {{
                for (const el of pieceEls) {{
                    const pieceType  = pieceTypeFromEl(el);
                    const pieceColor = pieceColorFromEl(el);
                    if (!pieceType || !pieceColor) continue;

                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    const cx = (rect.left + rect.right) / 2;
                    const cy = (rect.top  + rect.bottom) / 2;
                    const sq = pixelToSquare(cx, cy);
                    if (!sq) continue;
                    pieceMap[sq] = {{ pieceType, pieceColor }};
                }}
            }}

            // ── Annotate each highlighted square with its piece ─────────────
            const annotated = highlighted.map(h => {{
                const p = pieceMap[h.square];
                return {{
                    square: h.square,
                    piece:  p ? p.pieceType  : null,
                    color:  p ? p.pieceColor : null
                }};
            }});

            // ── Piece-map diagnostic (runs when highlights exist but all lack piece) ──
            const pieceMapDiag = (annotated.length > 0 && annotated.every(h => !h.piece))
                ? (() => {{
                    // TheBoard-pieces is a separate layer; its children are the
                    // actual piece elements.  Capture their classes so we can
                    // update pieceTypeFromClass to match.
                    const piecesLayer = document.querySelector('.TheBoard-pieces');
                    const layerChildren = piecesLayer
                        ? Array.from(piecesLayer.children).slice(0, 5).map(c => ({{
                            cls:       c.className,
                            title:     c.getAttribute('title'),
                            ariaLabel: c.getAttribute('aria-label'),
                            dataAttrs: Object.fromEntries(
                                Array.from(c.attributes)
                                     .filter(a => a.name.startsWith('data-'))
                                     .map(a => [a.name, a.value])
                            ),
                            style:     (c.getAttribute('style') || '').slice(0, 80)
                          }}))
                        : null;
                    return {{
                        pieceMapKeys:          Object.keys(pieceMap).slice(0, 10),
                        piecesFoundViaStruct:  piecesFoundViaStructure,
                        totalPieceEls:         pieceEls.length,
                        samplePieceClass:      pieceEls[0] ? pieceEls[0].className : null,
                        sampleSqTitle:         board.children[0]?.children[0]?.getAttribute('title'),
                        sampleSqAriaLabel:     board.children[0]?.children[0]?.getAttribute('aria-label'),
                        highlightedSquares:    annotated.map(h => h.square),
                        piecesLayerChildren:   layerChildren
                    }};
                  }})()
                : null;

            // ── Diagnostic: DOM inspection when no highlights found ─────────────
            let diag = null;
            if (rawHighlightCount === 0 && board && board.parentElement) {{

                // 1. Class-frequency map inside the board wrapper.
                //    Elements that appear only 1-2 times are candidates for
                //    highlight squares (from + to square).
                const classCounts = {{}};
                for (const el of board.parentElement.querySelectorAll('*')) {{
                    for (const cls of el.classList) {{
                        classCounts[cls] = (classCounts[cls] || 0) + 1;
                    }}
                }}
                const rareClasses = Object.entries(classCounts)
                    .filter(([, n]) => n <= 4)
                    .sort((a, b) => a[1] - b[1])
                    .slice(0, 20);

                // 2. CSS custom properties set via inline style on the board
                //    and every ancestor up to <body>.
                const cssVarChain = [];
                let el = board;
                while (el && el.tagName !== 'BODY') {{
                    const st = el.getAttribute('style') || '';
                    if (st.includes('--')) {{
                        cssVarChain.push({{
                            cls:   (el.className || '').slice(0, 60),
                            style: st.slice(0, 300)
                        }});
                    }}
                    el = el.parentElement;
                }}

                // 3. Canvas / SVG elements anywhere in the board wrapper.
                const canvases = Array.from(
                    board.parentElement.querySelectorAll('canvas, svg')
                ).map(c => ({{
                    tag: c.tagName,
                    id:  c.id,
                    cls: (c.className && c.className.baseVal !== undefined
                          ? c.className.baseVal : c.className || '').slice(0, 60),
                    w:   c.getAttribute('width'),
                    h:   c.getAttribute('height'),
                    childCount: c.children.length
                }}));

                // 4. Computed background of individual squares (catches highlight
                //    colours applied via CSS rules that don't change className).
                //    Also samples the ::before pseudo-element background.
                const squareBgs = [];
                let ri = 0;
                for (const rankRow of board.children) {{
                    let ci = 0;
                    for (const sq of rankRow.children) {{
                        const cs   = window.getComputedStyle(sq);
                        const csBefore = window.getComputedStyle(sq, '::before');
                        const bg   = cs.backgroundColor;
                        const bgB  = csBefore.backgroundColor;
                        const transparent = ['rgba(0, 0, 0, 0)', 'transparent', ''];
                        if (!transparent.includes(bg) || !transparent.includes(bgB)) {{
                            squareBgs.push({{ ri, ci, bg, bgBefore: bgB }});
                        }}
                        ci++;
                    }}
                    ri++;
                }}

                // 5. Last non-empty move-list cell outer-HTML.
                const moveCells2 = document.querySelectorAll('.moves-table-cell.moves-move');
                const lastNonEmpty = Array.from(moveCells2)
                    .filter(c => c.textContent.trim().length > 0)
                    .pop();
                const lastCellHtml = lastNonEmpty ? lastNonEmpty.outerHTML : null;

                diag = {{
                    rareClasses,
                    cssVarChain,
                    canvases,
                    squareBgs:   squareBgs.slice(0, 10),
                    lastCellHtml
                }};
            }}

            return {{
                highlights:        annotated,
                numHighlights:     annotated.length,
                pieceMap:          pieceMap,
                lastMoveText:      lastMoveText,
                lastMoveTitle:     lastMoveTitle,
                numFilesActual:    numFilesActual,
                numRanksActual:    numRanksActual,
                rawHighlightCount: rawHighlightCount,
                actualMovesLength: actualMovesLength,
                pieceMapDiag:      pieceMapDiag,
                diag:              diag
            }};
        }})();
        """

        try:
            data = self.driver.execute_script(js_script)
        except Exception as e:
            if verbose:
                print(f"[getmove] Script error: {e}")
            return None

        if not data:
            if verbose:
                print("[getmove] No data returned from board")
            return None

        if data.get('error'):
            if verbose:
                print(f"[getmove] {data['error']}")
            return None

        highlights       = data.get('highlights', [])
        n                = len(highlights)
        last_move_text   = (data.get('lastMoveText')  or '').strip()
        last_move_title  = (data.get('lastMoveTitle') or '').strip()
        num_files_actual = data.get('numFilesActual', num_files)
        num_ranks_actual = data.get('numRanksActual', num_ranks)

        # ── Title-based coordinate parsing (primary for variant boards) ───────
        # Chess.com stores moves in the move cell's `title` attribute using its
        # internal 14×14 coordinate frame.  Convert by subtracting the centering
        # offset derived from the actual board dimensions.
        for candidate in (last_move_title, last_move_text):
            if not candidate:
                continue
            uci = self._parse_title_coords(candidate, num_files_actual, num_ranks_actual,
                                           variant=variant_name)
            if uci:
                return uci

        # ── Castling: detected reliably from the move list ───────────────────
        # NOTE: these are capital letter O characters, not zeroes.
        # After stripping +/# the only remaining text for castling is 'O-O'
        # (kingside) or 'O-O-O' (queenside).
        is_kingside  = (last_move_text == 'O-O')
        is_queenside = (last_move_text == 'O-O-O')

        if is_kingside or is_queenside:
            # Determine which color just castled.
            # White plays on odd-numbered moves (1st, 3rd, …), so if the total
            # move count is odd the last move was White's; even → Black's.
            move_count   = data.get('actualMovesLength', 0)
            white_castle = (move_count % 2 == 1)

            # ── 8×8 standard chess ───────────────────────────────────────────
            if num_files_actual == 8 and num_ranks_actual == 8:
                if white_castle:
                    return 'e1g1' if is_kingside else 'e1c1'
                else:
                    return 'e8g8' if is_kingside else 'e8c8'

            # ── Gothic Chess / Capablanca (10×8) ─────────────────────────────
            if num_files_actual == 10 and num_ranks_actual == 8:
                if white_castle:
                    return 'f1i1' if is_kingside else 'f1c1'
                else:
                    return 'f8i8' if is_kingside else 'f8c8'

            # ── Other board sizes: derive squares from highlights ─────────────
            # Empty squares = pieces that moved away (king src + rook src).
            with_piece    = [h for h in highlights if h['piece']]
            without_piece = [h for h in highlights if not h['piece']]

            if len(without_piece) < 2:
                return None

            # Rook source: outermost empty square on the castling side.
            if is_kingside:
                rook_src = max(without_piece, key=lambda h: ord(h['square'][0]))
            else:
                rook_src = min(without_piece, key=lambda h: ord(h['square'][0]))

            king_src_candidates = [
                h for h in without_piece if h['square'] != rook_src['square']
            ]
            if not king_src_candidates:
                return None

            king_src = king_src_candidates[0]
            return f"{king_src['square']}{rook_src['square']}"

        # ── 0 highlights ────────────────────────────────────────────────────
        if n == 0:
            return None

        # ── 1 highlight → drop move ─────────────────────────────────────────
        if n == 1:
            h = highlights[0]
            if not h['piece']:
                return None
            uci_piece = h['piece'].upper()
            return f"{uci_piece}@{h['square']}"

        # ── 2 highlights → normal move / promotion ──────────────────────────
        if n == 2:
            with_piece    = [h for h in highlights if h['piece']]
            without_piece = [h for h in highlights if not h['piece']]

            if len(with_piece) == 0:
                return None

            if len(with_piece) == 2:
                dest = with_piece[0]
                src  = with_piece[1]
            else:
                dest = with_piece[0]
                src  = without_piece[0]

            from_sq      = src['square']
            to_sq        = dest['square']
            ending_piece = dest['piece']

            # Promotion heuristic: destination on a back rank, source on the
            # penultimate rank, and the landing piece is not a pawn.
            try:
                dest_rank = int(to_sq[1:])
                src_rank  = int(from_sq[1:])
            except ValueError:
                dest_rank = src_rank = 0

            on_back_rank   = (dest_rank == 1 or dest_rank == num_ranks)
            from_promo_row = (src_rank  == 2 or src_rank  == num_ranks - 1)
            is_promotion   = on_back_rank and from_promo_row and ending_piece != 'p'

            if is_promotion:
                return f"{from_sq}{to_sq}{ending_piece}"
            return f"{from_sq}{to_sq}"

        return None

