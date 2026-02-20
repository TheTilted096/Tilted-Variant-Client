"""Main variants client for chess.com."""
import os
import sys
import time
import threading
from collections import deque
from browser_launcher import BrowserLauncher
from chesscom_interface import ChessComInterface
from uci_handler import UCIHandler
from engine_manager import EngineManager

# Absolute path to the engines/ directory (one level above src/).
_ENGINES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'engines'
)


def _bg_print(msg=''):
    """Print from a background thread without disrupting the readline input prompt.

    Clears the current terminal line (which may contain a half-typed command),
    prints the message, then reprints '> ' plus whatever the user had typed so
    that the prompt is cleanly restored below the new output.
    """
    try:
        import readline
        buf = readline.get_line_buffer()
        sys.stdout.write(f'\r\033[K{msg}\n> {buf}')
    except (ImportError, AttributeError):
        # readline unavailable (Windows without pyreadline, or non-tty)
        sys.stdout.write(f'\n{msg}\n')
    sys.stdout.flush()

class VariantsClient:
    """Main client for playing chess variants on chess.com."""

    def __init__(self):
        """Initialize the variants client."""
        self.browser_launcher = None
        self.chesscom_interface = None
        self.driver = None
        self.running = False
        # Game state tracking
        self.was_in_game = False
        self.current_game_number = None
        self.last_game_number = None  # Game number that just finished; prevents re-announcing it
        self.game_over_handled_for = None  # Game number for which game-over was already fully handled
        self.last_state_check = 0
        # Move list storage: space-separated UCI moves for the current game
        self.move_list = ""
        # Fallback game-over polling (monotonic timestamp of last check)
        self._last_game_over_poll = 0.0
        # Background monitoring thread
        self.monitor_thread = None
        self.monitor_thread_running = False
        self.monitor_interval = 0.05  # Check console logs every 50ms (very lightweight)
        # Automated variant-loop state
        self.loop_running = False
        self.loop_thread = None
        # Engine integration
        self.engine_manager = EngineManager(_ENGINES_DIR)
        # Monotonically-incrementing counter: bumped every time a new game
        # starts.  The engine callback captures this value so it can detect
        # whether the game it was searching for is still the current one.
        self._game_generation = 0
        # Timing: milliseconds spent detecting the last opponent move.
        self._detect_ms = 0
        # Promo banner recovery: set True when a membership banner is detected
        # so the loop can restart the challenge phase immediately.
        self._loop_restart_required = False
        # Timestamp of the last promo-banner check (monotonic seconds).
        self._last_promo_check = 0.0
        # Guard against false game-over signals during the new-game setup
        # window.  Set to False as soon as a new game number is confirmed,
        # and back to True only after setup_game_over_observer() completes.
        # handle_game_over() is a no-op while this is False, preventing stale
        # DOM / cached __gameOverResult from the previous game from being
        # misread as the new game having immediately ended.
        self._new_game_settled = True
        # Monotonic timestamp of when the current game's setup completed and
        # game-over detection was re-enabled.  Used to enforce a minimum game
        # age before accepting any game-over signal (see handle_game_over).
        self._game_start_time = 0.0
        # Timing history for the 'ping' graph: deque of
        # (detect_ms, engine_ms, exec_ms, overhead_ms) tuples, newest last.
        self._timing_history = deque(maxlen=15)
        self._ping_thread = None

    def start(self):
        """Start the variants client."""
        print("=" * 60)
        print("Tilted Variants Client")
        print("Chess.com Variants Terminal Interface")
        print("=" * 60)
        print()

        try:
            # Launch browser
            print("[Client] Initializing browser...")
            self.browser_launcher = BrowserLauncher(debugging_port=9223)
            self.driver = self.browser_launcher.launch_edge()

            # Initialize chess.com interface
            self.chesscom_interface = ChessComInterface(self.driver)

            print()
            print("=" * 60)
            print("[Client] Setup complete!")
            print("[Client] Steps:")
            print("  1. Log in to Chess.com in the browser window")
            print("  2. Navigate to a variant game (Chaturanga, etc.)")
            print("  3. Start a game with a friend")
            print("  4. Use the terminal to enter moves and commands")
            print("=" * 60)
            print()

            self.running = True
            # Set up MutationObservers for event-driven detection
            print("[Client] Setting up event-driven monitoring...")

            observers_ok = True
            if self.chesscom_interface.setup_game_over_observer():
                print("[Client] ✓ Game over observer initialized")
            else:
                print("[Client] ⚠ Could not set up game over observer")
                observers_ok = False

            if self.chesscom_interface.setup_move_observer():
                print("[Client] ✓ Move observer initialized")
            else:
                print("[Client] ⚠ Could not set up move observer")
                observers_ok = False

            if not observers_ok:
                print("[Client] ⚠ Falling back to polling mode")

            # Start background monitoring thread
            print("[Client] Starting background monitor...")
            self.start_background_monitor()
            print("[Client] ✓ Background monitor thread started")
            self.run_terminal_interface()

        except KeyboardInterrupt:
            print("\n[Client] Shutting down...")
        except Exception as e:
            print(f"\n[Client] Error: {e}")
            print("\n[Client] Troubleshooting:")
            print("  - Make sure Microsoft Edge is installed")
            print("  - Close any existing Edge windows and try again")
            print("  - Check that port 9223 is not in use")
            import traceback
            traceback.print_exc()
        finally:
            self.cleanup()

    def background_monitor_loop(self):
        """
        Background thread that listens to browser console logs.
        Truly event-driven - no DOM polling!
        """
        print("[Monitor] Event listener started", flush=True)
        while self.monitor_thread_running and self.running:
            try:
                self.process_console_events()
                time.sleep(self.monitor_interval)
            except Exception as e:
                print(f"[Monitor] Error in background loop: {e}")
                import traceback
                traceback.print_exc()

    def process_console_events(self):
        """
        Poll JS global flags set by MutationObservers in the browser.
        Uses execute_script instead of get_log (which requires logging prefs at launch).
        """
        try:
            driver = self.chesscom_interface.driver

            # Poll board change flag and reset atomically
            board_changed = driver.execute_script(
                "var v = window.__boardChanged; window.__boardChanged = false; return v;"
            )
            if board_changed:
                self.handle_board_changed()

            # Poll game over flag and reset atomically
            game_over = driver.execute_script(
                "var v = window.__gameOver; window.__gameOver = false; return v;"
            )
            if game_over:
                self.handle_game_over()

            # Fallback: the MutationObserver can miss the game-over dialog
            # (animation timing, observer disconnected after navigation, class
            # names not matching selectors, etc.).  Every ~2 s, when we believe
            # a game is in progress and game-over hasn't been handled yet, run a
            # direct DOM scan as a safety net.
            #
            # IMPORTANT: we do NOT reuse detect_game_over() here because its
            # selectors are intentionally broad (they include [class*="result"],
            # [class*="end"], [class*="win"], etc.) — fine for *confirming* an
            # observer signal but far too broad for standalone *detection*.
            # Chat messages, rating badges, and sidebar elements from the
            # previous game would cause false positives.  Instead, we use a
            # tighter check that only matches actual modal dialog overlays.
            if (not game_over
                    and self.was_in_game
                    and self.current_game_number is not None
                    and self.game_over_handled_for != self.current_game_number):
                now = time.monotonic()
                if now - self._last_game_over_poll >= 2.0:
                    self._last_game_over_poll = now
                    dialog_visible = self._is_game_over_dialog_visible()
                    if dialog_visible:
                        _bg_print("[Game State] Game over detected via "
                                  "fallback scan (observer missed it)")
                        self.handle_game_over()

            # Also periodically check for game start
            self.check_for_game_start()

            # Periodically check for a full-screen membership promo banner
            # (only when not actively in a game to avoid false positives).
            if not self.was_in_game:
                now = time.monotonic()
                if now - self._last_promo_check >= 3.0:
                    self._last_promo_check = now
                    self._check_for_promo_banner()

        except Exception as e:
            # Only print non-session errors (session errors are expected on shutdown)
            if 'invalid session' not in str(e).lower():
                print(f"[Debug] Error in process_console_events: {e}")

    def _is_game_over_dialog_visible(self):
        """Targeted check for the game-over modal dialog.

        Unlike detect_game_over() — which uses very broad CSS selectors suited
        to *confirming* an observer event — this method only matches genuine
        modal overlays (large, visible dialogs with game-over text).  This
        prevents false positives from chat messages, rating badges, and other
        page elements that contain stale result text from previous games.

        Returns True if a game-over dialog overlay is currently visible.
        """
        try:
            return self.chesscom_interface.driver.execute_script("""
                const RE = /you won|you lost|you drew|black won|white won|draw by agreement|checkmate|stalemate|time.?out|flagged|resign|abandon/i;

                // 1. Check explicit modal/dialog/popup elements that are large
                //    enough to be an overlay (not a small chat badge or tooltip).
                const selectors = '[class*="modal"], [class*="dialog"], [class*="popup"], [class*="game-over"]';
                for (const el of document.querySelectorAll(selectors)) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 250 || rect.height < 200) continue;
                    const style = getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    if (RE.test(el.textContent || '')) return true;
                }

                // 2. Check inline-styled fixed-position overlays (some sites
                //    render modals with inline styles rather than class names).
                for (const el of document.querySelectorAll(
                        '[style*="position: fixed"], [style*="position:fixed"]')) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 250 || rect.height < 200) continue;
                    if (RE.test(el.textContent || '')) return true;
                }

                return false;
            """) or False
        except Exception:
            return False

    def handle_board_changed(self):
        """Handle board change event from MutationObserver.

        When it becomes our turn, the opponent just moved — harvest their UCI
        move and append it to the running move list.
        """
        try:
            game_state = self.chesscom_interface.get_game_state()
            if not game_state['in_game']:
                return
            color = game_state['color']
            turn  = game_state['turn']
            if not color or not turn:
                return
            # It's our turn → the opponent just moved; collect their move.
            if color == turn:
                t_detect_start = time.monotonic()
                move = self.chesscom_interface.get_last_move(verbose=False)
                self._detect_ms = int((time.monotonic() - t_detect_start) * 1000)
                if move:
                    self.move_list += move + " "
                # If the engine is running, ask it for the reply.
                if self.engine_manager.process is not None:
                    self._trigger_engine_move()
        except Exception:
            pass

    def handle_game_over(self):
        """Handle game over event from MutationObserver."""
        try:
            game_number = self.current_game_number

            # Require a confirmed game number from chat before doing anything.
            # Without one there is no verified game in progress, so any detection
            # is a false positive (e.g. browsing the lobby or variant-selection UI).
            if game_number is None:
                return

            # Deduplicate: ignore if we've already handled game-over for this game.
            if game_number == self.game_over_handled_for:
                return

            # Block during the new-game setup window.
            # check_for_game_start() sets this False as soon as a new game
            # number is confirmed and True only after setup_game_over_observer()
            # completes (clearing window.__gameOverResult and reinstalling the
            # observer).  Without this guard, stale DOM / cached results from
            # the previous game can trigger a false game-over for the new one.
            if not self._new_game_settled:
                return

            # Enforce a minimum game age before accepting any game-over signal.
            #
            # The MutationObserver from the previous game can fire on DOM
            # mutations that carry stale result text and write a console log.
            # That log is buffered in the monitor queue and may be read in the
            # next iteration AFTER _new_game_settled is already True, bypassing
            # the flag above.  Stale events always arrive within a few hundred
            # milliseconds of game start; real games virtually never end that
            # quickly.  Any legitimate near-instant game-over that falls inside
            # this window is still caught by the fallback DOM scan, which uses
            # tighter selectors (large modal dialogs only) and fires 2 s after
            # game detection — well after this window expires.
            _MIN_GAME_AGE = 3.0  # seconds
            if time.monotonic() - self._game_start_time < _MIN_GAME_AGE:
                return

            # Run full detection to get details
            game_over_info = self.chesscom_interface.detect_game_over()

            if game_over_info['game_over']:
                # Record before clearing current_game_number so the guard above holds.
                self.game_over_handled_for = game_number

                _bg_print("=" * 60)
                _bg_print("[Game State] 🏁 GAME OVER!")
                _bg_print(f"[Game State] Game #{game_number}")
                if game_over_info['result']:
                    _bg_print(f"[Game State] Result: {game_over_info['result']}")
                _bg_print("=" * 60)

                # Auto-dismiss the dialog. Always attempt Escape regardless of
                # whether detect_game_over() found the dialog via its selectors;
                # the observer may have triggered on text that the DOM query
                # missed, and sending Escape is harmless when no dialog is open.
                _bg_print("[Game State] Auto-dismissing game over dialog...")
                success = self.chesscom_interface.dismiss_game_over_dialog()
                if success:
                    _bg_print("[Game State] ✓ Dialog dismissed")
                else:
                    _bg_print("[Game State] ✗ Failed to dismiss dialog")

                # Terminate the engine immediately — this must happen before any
                # further loop steps (rematch requests, lobby navigation, etc.).
                # A fresh instance will be started when the next game begins.
                if self.engine_manager.process is not None:
                    _bg_print("[Engine] Terminating engine process...")
                    self.engine_manager.stop()
                    _bg_print("[Engine] ✓ Engine process terminated")

                # Wait for play/rematch buttons to render after dialog closes.
                time.sleep(2.0)

                # Reset game state tracking.
                # Guard: check_for_game_start() may have detected a new game
                # during the sleep above (e.g. opponent accepted a rematch very
                # quickly, before we even had a chance to click the button).
                # In that case current_game_number was already updated to the
                # new game number and was_in_game set back to True — do NOT
                # overwrite that state or the new game will be invisible to the
                # inner loop and challenge logic.
                self.last_game_number = game_number
                if self.current_game_number == game_number:
                    # Normal path: no new game detected during the sleep.
                    self.current_game_number = None
                    self.was_in_game = False
                    # Force the promo-banner check to fire on the very next
                    # monitor cycle rather than waiting up to 3 more seconds.
                    self._last_promo_check = 0.0
                # else: a new game is already running — leave state intact.

        except Exception as e:
            pass

    def start_background_monitor(self):
        """Start the background monitoring thread."""
        if self.monitor_thread is None or not self.monitor_thread.is_alive():
            self.monitor_thread_running = True
            self.monitor_thread = threading.Thread(
                target=self.background_monitor_loop,
                daemon=True  # Daemon thread will exit when main program exits
            )
            self.monitor_thread.start()

    def stop_background_monitor(self):
        """Stop the background monitoring thread."""
        if self.monitor_thread and self.monitor_thread.is_alive():
            print("[Monitor] ⏸ Stopping background monitoring thread...")
            self.monitor_thread_running = False
            self.monitor_thread.join(timeout=2.0)  # Wait up to 2 seconds
            self.monitor_thread = None

    def check_for_game_start(self):
        """
        Check if a new game has started.
        Called periodically by the console event listener.
        """
        try:
            game_state = self.chesscom_interface.get_game_state()

            # Detect transition from not in game to in game
            if not self.was_in_game and game_state['in_game']:
                start_info = self.chesscom_interface.detect_game_started()
                new_number = start_info.get('game_number')

                # Require a confirmed game number - without one we cannot verify
                # a real game is in progress (could be lobby/variant-selection UI).
                if not new_number:
                    return

                # If the detected game number matches the one that just ended,
                # the page hasn't updated yet - skip until a new number appears.
                if new_number == self.last_game_number:
                    return

                # Raise the gate BEFORE assigning the new game number.
                # handle_game_over() is a no-op while False, so stale DOM /
                # cached __gameOverResult from the previous game cannot be
                # misread as an immediate loss for the new game during the
                # window between game-number assignment and observer reset.
                self._new_game_settled = False

                _bg_print("=" * 60)
                _bg_print("[Game State] 🎮 GAME STARTED!")
                _bg_print(f"[Game State] Game #{new_number}")
                self.current_game_number = new_number
                self.last_game_number = None  # Consumed; clear so future games work
                self._game_generation += 1    # Invalidate any in-flight engine search
                if start_info.get('method'):
                    _bg_print(f"[Game State] Detected via: {start_info['method']}")
                if game_state['color']:
                    _bg_print(f"[Game State] You are playing as: {game_state['color']}")
                _bg_print("=" * 60)
                self.was_in_game = True
                self.move_list = ""
                # Push the fallback game-over poll into the future so it
                # cannot fire during the page transition (stale elements from
                # the previous game may still be in the DOM for a few seconds).
                self._last_game_over_poll = time.monotonic()
                # Discard any stale board-orientation/size cache from the
                # previous game so the first move of the new game re-detects
                # them correctly.
                self.chesscom_interface.invalidate_board_params_cache()

                # If an engine is configured, start a fresh process for this game.
                if self.engine_manager.is_configured:
                    variant = self.chesscom_interface.get_variant_name() or 'chess'
                    _bg_print(f"[Engine] Starting '{self.engine_manager.active_engine_name}' "
                              f"for variant: {variant}")
                    ok = self.engine_manager.start(
                        variant, game_state.get('color', 'white')
                    )
                    if ok:
                        _bg_print("[Engine] ✓ Engine ready")
                        # Re-fetch state: the UCI handshake can take several
                        # hundred milliseconds, during which the opponent may
                        # have played.  Using the stale game_state from above
                        # would send "position startpos" (white to move) even
                        # when the engine is black and a move was missed.
                        fresh_state = self.chesscom_interface.get_game_state()
                        if fresh_state.get('color') == fresh_state.get('turn'):
                            # Harvest any move played while the engine was
                            # starting so it isn't missing from the move list.
                            missed = self.chesscom_interface.get_last_move(verbose=False)
                            if missed:
                                self.move_list += missed + " "
                                _bg_print(f"[+] {missed} (caught up)")
                            self._trigger_engine_move()
                    else:
                        _bg_print("[Engine] ✗ Failed to start engine — playing manually")

                # Re-install the game-over observer for the new game.
                # reset_game_over_observer() only clears flags; the observer
                # itself may have been destroyed by an SPA page transition, so
                # we re-run the full setup (which disconnects any stale observer
                # first) to guarantee it is live for this game.
                self.game_over_handled_for = None
                self.chesscom_interface.setup_game_over_observer()

                # Re-initialise the move observer now that the game page is loaded.
                # The initial setup (at startup) runs before any game is open, so
                # .moves-table doesn't exist yet and the observer is a no-op.
                if self.chesscom_interface.setup_move_observer():
                    _bg_print("[Game State] ✓ Move observer re-initialised")
                else:
                    _bg_print("[Game State] ⚠ Could not re-initialise move observer")

                # Setup is complete: the game-over observer is live and its
                # stale-result cache has been cleared.  Re-enable game-over
                # detection for this game.
                self._new_game_settled = True
                self._game_start_time = time.monotonic()

        except Exception as e:
            # Silently handle errors
            pass

    def run_terminal_interface(self):
        """Run the terminal interface for commands and move input."""
        print("=" * 60)
        print("Terminal Interface")
        print("=" * 60)
        print("Available Commands:")
        print("  - Enter UCI move (e.g., 'e2e4', 'd2d3')")
        print("  - Enter drop move (e.g., 'N@g3', 'P@e5') for Crazyhouse/variants")
        print("  - Enter promotion move (e.g., 'h2h1r', 'a7a8q')")
        print("  - 'resign' - Resign the current game")
        print("  - 'rematch' - Click the Rematch button after game ends")
        print("  - 'play-again' - Click the Play Again button after game ends")
        print("  - 'lobby' - Click the Exit button to return to lobby")
        print("  - 'c <variant>' / 'challenge <variant>' - Create a challenge (e.g. c chaturanga, c gothic, c koth)")
        print("  - 'loop start <v1> [v2 ...]' - Auto-loop: challenge all listed variants, play, rematch, repeat")
        print("  - 'loop stop'                - Stop the running loop after the current operation")
        print("  - 'engines list'             - List available engine executables in engines/")
        print("  - 'engine activate <name>'   - Activate an engine (plays moves in place of you)")
        print("  - 'engine deactivate'        - Deactivate the engine (return to manual play)")
        print("  - 'engine status'            - Show which engine is active")
        print("  - 'status' - Check current game state (in game or not)")
        print("  - 'getmove' - Detect the last move played on the board (UCI format)")
        print("  - 'movelist' - Print the move history for the current game")
        print("  - 'ping' - Open a live graph of move-cycle overhead for the last 25 moves")
        print("  - 'quit' - Exit the client")
        print()
        print("=" * 60)
        print()

        while self.running:
            try:
                # Get user input (preserve case for drop moves like N@g3)
                user_input = input("> ").strip()

                # Only lowercase for commands, not for moves (need uppercase for drops)
                command = user_input.lower()

                if not user_input:
                    continue

                # Handle commands (use lowercase version)
                if command == 'quit':
                    print("[Client] Exiting...")
                    break
                elif command == 'resign':
                    print("[Client] Resigning game...")
                    success = self.chesscom_interface.resign()
                    if success:
                        print("[Success] Game resigned!")
                    else:
                        print("[Error] Failed to resign")
                    print()
                    continue
                elif command == 'rematch':
                    print("[Client] Clicking Rematch button...")
                    success = self.chesscom_interface.rematch()
                    if success:
                        print("[Success] Rematch button clicked!")
                    else:
                        print("[Error] Failed to click Rematch button")
                    print()
                    continue
                elif command == 'play-again' or command == 'playagain':
                    print("[Client] Clicking Play Again button...")
                    success = self.chesscom_interface.play_again()
                    if success:
                        print("[Success] Play Again button clicked!")
                    else:
                        print("[Error] Failed to click Play Again button")
                    print()
                    continue
                elif command == 'lobby':
                    print("[Client] Clicking Exit button to return to lobby...")
                    success = self.chesscom_interface.exit_to_lobby()
                    if success:
                        print("[Success] Exit button clicked, returning to lobby!")
                    else:
                        print("[Error] Failed to click Exit button")
                    print()
                    continue
                elif command == 'cancel':
                    print("[Client] Cancelling pending challenge(s)...")
                    success = self.chesscom_interface.cancel_challenge()
                    if success:
                        print("[Success] Challenge(s) cancelled!")
                    else:
                        print("[Error] Failed to cancel — no pending challenges?")
                    print()
                    continue
                elif command.startswith('loop'):
                    parts = command.split()
                    if len(parts) >= 2 and parts[1] == 'stop':
                        self._stop_loop()
                    elif len(parts) >= 3 and parts[1] == 'start':
                        self._start_loop(parts[2:])
                    else:
                        print("[Error] Usage:  loop start <variant1> [variant2 ...]")
                        print("[Error]         loop stop")
                    print()
                    continue
                elif command.startswith('challenge') or command.startswith('c '):
                    parts = user_input.split(None, 1)
                    if len(parts) < 2:
                        print("[Error] Usage: challenge <variant>  (e.g. c chaturanga, c gothic, c koth)")
                        print()
                        continue
                    variant = parts[1].strip()
                    print(f"[Client] Creating challenge for variant: {variant}")
                    success = self.chesscom_interface.create_challenge(variant)
                    if success:
                        print(f"[Success] Challenge flow initiated for {variant}!")
                    else:
                        print(f"[Error] Failed to create challenge for {variant}")
                    print()
                    continue
                elif command == 'engines list':
                    engines = self.engine_manager.list_engines()
                    if engines:
                        print(f"[Engines] Found {len(engines)} engine(s) in engines/:")
                        for name in engines:
                            marker = " *" if name == self.engine_manager.active_engine_name else ""
                            print(f"  {name}{marker}")
                    else:
                        print("[Engines] No executables found in engines/ folder.")
                        print("[Engines] Drop a UCI-compatible engine binary there and try again.")
                    print()
                    continue
                elif command.startswith('engine activate'):
                    parts = user_input.split(None, 2)
                    if len(parts) < 3:
                        print("[Error] Usage: engine activate <name>")
                        print()
                        continue
                    name = parts[2].strip()
                    ok, msg = self.engine_manager.activate(name)
                    if ok:
                        print(f"[Engine] ✓ {msg}")
                    else:
                        print(f"[Engine] ✗ {msg}")
                    print()
                    continue
                elif command == 'engine deactivate':
                    name = self.engine_manager.deactivate()
                    if name:
                        print(f"[Engine] ✓ Engine '{name}' deactivated — returning to manual play.")
                    else:
                        print("[Engine] No engine was active.")
                    print()
                    continue
                elif command == 'engine status':
                    if self.engine_manager.is_configured:
                        em = self.engine_manager
                        running = em.process is not None
                        state = "running" if running else "idle (starts on next game)"
                        print(f"[Engine] Active: {em.active_engine_name} ({state})")
                        print(f"[Engine] Search: {em.search_config_str()}")
                        if running and em.search_mode == 'time':
                            print(f"[Engine] Clock:  {em.engine_color}={em.engine_time}ms")
                    else:
                        print("[Engine] No engine active — use 'engine activate <name>'.")
                    print()
                    continue
                elif command.startswith('config mode'):
                    parts = command.split()
                    # config mode nodes [N]
                    if len(parts) >= 3 and parts[2] == 'nodes':
                        nodes = int(parts[3]) if len(parts) >= 4 else 1_000_000
                        self.engine_manager.set_search_nodes(nodes)
                        print(f"[Engine] Search mode → nodes {nodes:,}")
                    # config mode time <base_ms> <inc_ms>
                    elif len(parts) >= 5 and parts[2] == 'time':
                        base = int(parts[3])
                        inc  = int(parts[4])
                        self.engine_manager.set_search_time(base, inc)
                        print(f"[Engine] Search mode → time  base={base}ms  inc={inc}ms")
                    else:
                        print("[Error] Usage: config mode nodes [N]")
                        print("[Error]        config mode time <base_ms> <inc_ms>")
                    print()
                    continue
                elif command == 'status':
                    print("[Client] Checking game state...")
                    # First check player color with verbose output for debugging
                    print("[Client] Checking player color (verbose)...")
                    color = self.chesscom_interface.get_player_color(verbose=True)

                    # Now get full game state
                    game_state = self.chesscom_interface.get_game_state()
                    print("\n" + "=" * 60)
                    print("[Game State] Status Report")
                    print("=" * 60)
                    if game_state['in_game']:
                        print("[Game State] ✓ Currently IN GAME")
                        if game_state['username']:
                            print(f"[Game State] Username: {game_state['username']}")
                        if game_state['color']:
                            print(f"[Game State] Playing as: {game_state['color']}")
                        if game_state['turn']:
                            print(f"[Game State] Current turn: {game_state['turn']}")
                        if self.current_game_number:
                            print(f"[Game State] Game #: {self.current_game_number}")
                    else:
                        print("[Game State] ✗ NOT in game")
                        print("[Game State] Waiting in lobby or between games")
                    print("=" * 60 + "\n")
                    continue
                elif command == 'getmove':
                    print("[Client] Detecting last move on board...")
                    move = self.chesscom_interface.get_last_move()
                    if move:
                        print(f"[getmove] Last move: {move}")
                    else:
                        print("[getmove] Could not detect last move")
                    print()
                    continue
                elif command == 'movelist':
                    print(f"[movelist] {self.move_list}")
                    print()
                    continue
                elif command == 'ping':
                    self._open_ping_window()
                    print()
                    continue

                # Try to parse as UCI move (preserve original case for drop moves)
                parsed_move = UCIHandler.parse_uci_move(user_input)

                if not parsed_move:
                    print(f"[Error] Invalid UCI move format: {user_input}")
                    print("[Help] Expected formats:")
                    print("[Help]   - Regular move: e2e4 (source + destination)")
                    print("[Help]   - Drop move: N@g3 (piece type + @ + square)")
                    print()
                    continue

                # Display the move
                move_display = UCIHandler.format_move_display(parsed_move)
                print(f"[Move] {move_display}")

                # Store the move before executing it
                self.move_list += user_input + " "

                # Make the move on chess.com
                success = self.chesscom_interface.make_move(user_input)

                if success:
                    print("[Success] Move executed!")
                else:
                    print("[Error] Failed to execute move on chess.com")
                    print("[Help] Make sure you're in an active game on the board")
                    print("[Help] Check the browser window for any dialogs or alerts")

                print()

            except KeyboardInterrupt:
                print("\n[Client] Shutting down...")
                break
            except Exception as e:
                print(f"[Error] {e}")
                print()

    # ── Promo banner recovery ────────────────────────────────────────────────

    def _check_for_promo_banner(self):
        """Detect a chess.com membership promotion page or overlay.

        chess.com sometimes navigates the user to a full membership/premium
        page (e.g. chess.com/membership) rather than showing the variants
        lobby after a game.  It may also display an overlay banner on top of
        the variants page.  Either form is detected here.

        When detected, we navigate directly back to the variants lobby and,
        if an automated loop is running, signal it to restart the challenge
        phase immediately.
        """
        try:
            driver = self.chesscom_interface.driver

            # ── Primary: URL-based detection ─────────────────────────────────
            # A full-page navigation to the membership/premium URL is the most
            # common form and is trivially reliable to detect.
            current_url = driver.current_url
            BAD_URL_FRAGMENTS = ('/membership', '/premium', '/upgrade',
                                 '/subscription', '/pricing')
            is_promo = any(frag in current_url for frag in BAD_URL_FRAGMENTS)

            # ── Secondary: DOM overlay scan ───────────────────────────────────
            # Catches banners that appear on top of the variants page without
            # a URL change (less common but possible).
            if not is_promo:
                is_promo = driver.execute_script("""
                    const PROMO_RE = /upgrade|membership|chess\\.com premium|get premium|try premium|subscribe/i;
                    // Class-name selectors commonly used by chess.com promo overlays.
                    const selectors = [
                        '[class*="upgrade"]',
                        '[class*="membership"]',
                        '[class*="premium-banner"]',
                        '[class*="upsell"]',
                        '[class*="subscription"]',
                    ];
                    for (const sel of selectors) {
                        for (const el of document.querySelectorAll(sel)) {
                            const rect = el.getBoundingClientRect();
                            // Must be large enough to be a real full-screen overlay.
                            if (rect.width < 450 || rect.height < 350) continue;
                            const style = getComputedStyle(el);
                            if (style.display === 'none' ||
                                style.visibility === 'hidden' ||
                                parseFloat(style.opacity) < 0.1) continue;
                            if (PROMO_RE.test(el.textContent || '')) return true;
                        }
                    }
                    // Also scan large fixed-position overlays with promo text.
                    for (const el of document.querySelectorAll(
                            '[style*="position: fixed"], [style*="position:fixed"]')) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width < 450 || rect.height < 350) continue;
                        if (PROMO_RE.test(el.textContent || '')) return true;
                    }
                    return false;
                """) or False

            if is_promo:
                _bg_print(f"[Client] ⚠ Membership promotion page detected "
                          f"({current_url}) — navigating back to variants lobby...")
                driver.get("https://www.chess.com/variants")
                time.sleep(2.5)
                # Re-install observers for the freshly loaded lobby page.
                self.chesscom_interface.setup_game_over_observer()
                self.chesscom_interface.setup_move_observer()
                _bg_print("[Client] ✓ Returned to variants lobby")

                if self.loop_running:
                    self._loop_restart_required = True
                    _bg_print("[Loop] Signalling loop to restart challenge phase...")

        except Exception:
            pass

    # ── Engine integration ───────────────────────────────────────────────────

    def _trigger_engine_move(self):
        """Ask the active engine for a move and execute it on the board.

        Runs the search in a background thread (via EngineManager.request_move)
        so the monitor thread is never blocked.
        """
        # Capture the current generation.  If game over fires and a new game
        # starts before the search finishes, the generation will have changed
        # and the stale callback will discard its result instead of making a
        # move on the wrong (or already-finished) game.
        generation = self._game_generation
        # Freeze detection time at the point of triggering.
        detect_ms = self._detect_ms

        def on_best_move(uci_move, think_ms=0, relay_ms=0):
            if not uci_move:
                return
            if self._game_generation != generation:
                _bg_print(f"[Engine] Discarding stale move {uci_move} "
                          f"(game ended during search)")
                return
            self.move_list += uci_move + " "
            _bg_print(f"[Engine] Playing: {uci_move}")
            t_exec_start = time.monotonic()
            success = self.chesscom_interface.make_move(uci_move)
            exec_ms = int((time.monotonic() - t_exec_start) * 1000)
            if not success:
                _bg_print(f"[Engine] ✗ Failed to execute move: {uci_move}")
            total_ms = detect_ms + think_ms + exec_ms
            overhead_ms = total_ms - think_ms
            _bg_print(
                f"[Timing] detect={detect_ms}ms | "
                f"engine={think_ms}ms | exec={exec_ms}ms | "
                f"total={total_ms}ms (overhead={overhead_ms}ms)"
            )
            self._timing_history.append(
                (detect_ms, think_ms, exec_ms, overhead_ms)
            )

        self.engine_manager.request_move(self.move_list, on_best_move)

    # ── Variant loop ────────────────────────────────────────────────────────

    def _start_loop(self, variants):
        """Start the automated variant loop in a background thread."""
        if self.loop_running:
            print("[Loop] Already running — use 'loop stop' first.")
            return
        if not variants:
            print("[Error] No variants specified.")
            return
        self.loop_running = True
        self.loop_thread = threading.Thread(
            target=self._loop_body, args=(list(variants),), daemon=True
        )
        self.loop_thread.start()
        print(f"[Loop] Started for variants: {', '.join(variants)}")

    def _stop_loop(self):
        """Signal the loop to stop after its current operation."""
        if not self.loop_running:
            print("[Loop] Not running.")
            return
        self.loop_running = False
        print("[Loop] Stop signal sent — will halt after the current step.")

    def _loop_body(self, variants):
        """Background thread: challenge → wait → play → lobby → repeat.

        'loop stop' behaviour:
          - In lobby / challenge phase: pending challenges are cancelled
            immediately and the loop exits.
          - In game: the current game is allowed to finish naturally; once it
            ends the loop exits and returns to the lobby.
        """
        _bg_print(f"[Loop] Starting loop for: {', '.join(variants)}")

        while self.loop_running:
            # ── CHALLENGE PHASE ───────────────────────────────────────────────
            _bg_print(f"[Loop] Issuing {len(variants)} challenge(s): "
                      f"{', '.join(variants)}")
            # Snapshot the current game number; any change signals acceptance.
            prev_game_num = self.current_game_number

            game_accepted = False
            for variant in variants:
                if not self.loop_running:
                    break
                # Abort immediately if a game started while issuing challenges.
                if self.current_game_number != prev_game_num:
                    _bg_print("[Loop] Game detected mid-challenge — "
                              "aborting remaining challenges.")
                    game_accepted = True
                    break
                _bg_print(f"[Loop] → Challenging: {variant}")
                ok = self.chesscom_interface.create_challenge(
                    variant,
                    abort_check=lambda: self.current_game_number != prev_game_num,
                )
                # If abort_check fired inside create_challenge, treat it the
                # same as detecting the game at the top of the loop.
                if self.current_game_number != prev_game_num:
                    _bg_print("[Loop] Game detected during challenge creation — "
                              "aborting remaining challenges.")
                    game_accepted = True
                    break
                if not ok:
                    _bg_print(f"[Loop]   Warning: challenge creation failed for {variant}")
                time.sleep(0.5)

            if not self.loop_running:
                # Stopped while issuing — cancel anything that went through.
                _bg_print("[Loop] Stop requested — cancelling pending challenges...")
                self.chesscom_interface.cancel_challenge()
                break

            if not game_accepted:
                # ── WAIT FOR ACCEPTANCE (10-minute timeout) ───────────────────
                _bg_print("[Loop] Waiting for a challenge to be accepted "
                          "(10-minute timeout)...")
                deadline = time.time() + 600

                while True:
                    if not self.loop_running:
                        # Stopped while waiting in lobby — cancel challenges.
                        _bg_print("[Loop] Stop requested — cancelling pending challenges...")
                        self.chesscom_interface.cancel_challenge()
                        break
                    if self._loop_restart_required:
                        # Promo banner navigated us away; cancel stale challenges
                        # and restart the challenge phase immediately.
                        _bg_print("[Loop] Promo banner recovery — "
                                  "restarting challenge phase...")
                        self._loop_restart_required = False
                        self.chesscom_interface.cancel_challenge()
                        break
                    cur = self.current_game_number
                    if cur is not None and cur != prev_game_num:
                        game_accepted = True
                        break
                    if time.time() > deadline:
                        _bg_print("[Loop] 10-minute timeout — "
                                  "cancelling challenges and re-issuing...")
                        self.chesscom_interface.cancel_challenge()
                        time.sleep(1.0)
                        break
                    time.sleep(0.5)

                if not game_accepted or not self.loop_running:
                    continue  # outer while will exit if loop_running=False

            # ── GAME INNER LOOP ───────────────────────────────────────────────
            # Once a game is in progress the inner loop runs to completion
            # regardless of loop_running — the game is never abandoned
            # mid-play. The loop_running flag is only checked *between* games.
            while True:
                game_num = self.current_game_number
                if game_num is None:
                    _bg_print("[Loop] Lost game reference — "
                              "returning to challenge phase.")
                    break

                # Brief wait for the game page to settle, then identify variant.
                time.sleep(1.5)
                label = self.chesscom_interface.get_ingame_variant_label()
                _bg_print(f"[Loop] ▶ Game #{game_num} | "
                          f"Variant: {label or 'unknown'}")
                _bg_print("[Loop]   Play manually or enter UCI moves "
                          "in the terminal.")

                # ── Wait for game-over (always, even if stop was requested) ───
                # handle_game_over() sets game_over_handled_for first, then
                # dismisses the dialog, waits 2 s, and only then clears
                # current_game_number.
                while True:
                    if self._loop_restart_required:
                        # Promo banner appeared (possibly blocking game-over UI).
                        break
                    if self.game_over_handled_for == game_num:
                        if self.current_game_number is None:
                            # Game over fully processed.
                            break
                    time.sleep(0.5)

                # Promo banner recovery.
                if self._loop_restart_required:
                    self._loop_restart_required = False
                    _bg_print("[Loop] Promo banner recovery — "
                              "restarting challenge phase...")
                    break  # Break inner game loop; outer loop will re-challenge

                # Game finished — honour a pending stop request now.
                if not self.loop_running:
                    _bg_print("[Loop] Game over — loop stopped.")
                    break

                # Go directly to lobby for next round of challenges.
                _bg_print("[Loop] Game over — going to lobby...")
                exit_ok = self.chesscom_interface.exit_to_lobby()
                if not exit_ok:
                    # Exit button missing: likely on the promo page.
                    self._check_for_promo_banner()
                if self._loop_restart_required:
                    self._loop_restart_required = False
                    _bg_print("[Loop] Promo banner recovery — "
                              "restarting challenge phase...")
                    break
                time.sleep(2.0)
                break  # Back to outer challenge loop

        _bg_print("[Loop] Stopped.")
        self.loop_running = False

    # ── Ping / overhead graph ────────────────────────────────────────────────

    def _open_ping_window(self):
        """Open the live overhead graph in a background thread.

        If the window is already open, print a notice instead of opening a
        second one.  The terminal remains fully interactive while the graph
        window is displayed.
        """
        if self._ping_thread is not None and self._ping_thread.is_alive():
            print("[ping] Graph window is already open.")
            return
        self._ping_thread = threading.Thread(
            target=self._ping_graph_thread, daemon=True
        )
        self._ping_thread.start()
        print("[ping] Opening overhead graph window...")

    def _ping_graph_thread(self):
        """Background thread: render a live-updating overhead bar chart.

        Each bar represents one move cycle, stacked as:
          - bottom (blue)  = detect time  (opponent-move detection)
          - top    (red)   = exec time    (DOM move injection)
          bar height = overhead = detect + exec  (i.e. non-engine time)

        A dashed yellow line marks the rolling mean overhead.
        The chart refreshes every 500 ms automatically.

        Tkinter and the Figure are created directly in this thread to avoid
        the pyplot thread-safety warning that appears when plt.subplots() /
        plt.show() are called from a non-main thread.  root.after() schedules
        redraws on the Tk event loop that owns the window, which is the
        correct way to animate from a background thread.
        """
        try:
            import tkinter as tk
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure
        except ImportError:
            _bg_print(
                "[ping] matplotlib is required for the graph — "
                "run: pip install matplotlib"
            )
            return

        BG       = '#1e1e2e'
        C_DETECT = '#4c8eda'   # blue  — detect segment
        C_EXEC   = '#e07060'   # coral — exec segment
        C_MEAN   = '#f0e080'   # yellow dashed — mean overhead line
        C_TEXT   = '#d0d0d0'
        C_GRID   = '#2e2e4e'
        C_SPINE  = '#44445e'

        root = tk.Tk()
        root.title('Tilted — Move Overhead')
        root.configure(bg=BG)

        fig = Figure(figsize=(11, 4), facecolor=BG)
        ax  = fig.add_subplot(111)

        canvas = FigureCanvasTkAgg(fig, master=root)
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # Mutable cell so update() can store its own after-ID for cancellation.
        after_id = [None]

        def _style_ax():
            ax.set_facecolor(BG)
            for sp in ax.spines.values():
                sp.set_edgecolor(C_SPINE)
            ax.tick_params(colors=C_TEXT)
            ax.xaxis.label.set_color(C_TEXT)
            ax.yaxis.label.set_color(C_TEXT)

        def on_close():
            # Cancel any pending redraw callback before destroying the window.
            # This releases Tk's reference to the update closure (and through
            # it, canvas → PhotoImage), so those objects are finalized here
            # in the background thread rather than later by the main thread's
            # GC, which would raise "main thread is not in main loop".
            if after_id[0] is not None:
                try:
                    root.after_cancel(after_id[0])
                except Exception:
                    pass
            root.destroy()

        root.protocol("WM_DELETE_WINDOW", on_close)

        def update():
            try:
                ax.cla()
                _style_ax()

                history = list(self._timing_history)
                n = len(history)

                if n == 0:
                    ax.text(
                        0.5, 0.5,
                        'No moves yet — waiting for engine moves...',
                        ha='center', va='center', transform=ax.transAxes,
                        color=C_TEXT, fontsize=12,
                    )
                    ax.set_title(
                        'Move Cycle Overhead', color=C_TEXT, fontsize=13
                    )
                else:
                    xs       = list(range(n))
                    detect   = [h[0] for h in history]
                    exec_t   = [h[2] for h in history]
                    overhead = [h[3] for h in history]
                    mean_oh  = sum(overhead) / n

                    ax.bar(xs, detect, color=C_DETECT, label='detect', zorder=2)
                    ax.bar(xs, exec_t, bottom=detect, color=C_EXEC,
                           label='exec', zorder=2)
                    ax.axhline(
                        mean_oh, color=C_MEAN, linestyle='--', linewidth=1.3,
                        label=f'mean {mean_oh:.0f} ms', zorder=3,
                    )
                    ax.set_xlabel(
                        'Move  (oldest → newest)', color=C_TEXT, fontsize=9
                    )
                    ax.set_ylabel('Time (ms)', color=C_TEXT, fontsize=9)
                    ax.set_title(
                        f'Move Cycle Overhead — last {n}  |  '
                        f'last={overhead[-1]} ms    avg={mean_oh:.0f} ms    '
                        f'max={max(overhead)} ms',
                        color=C_TEXT, fontsize=10,
                    )
                    ax.set_xlim(-0.6, max(n, 1) - 0.4)
                    ax.set_ylim(0, max(max(overhead) * 1.30, 200))
                    ax.set_xticks(xs)
                    ax.set_xticklabels(
                        [str(i + 1) for i in range(n)],
                        color=C_TEXT, fontsize=7,
                    )
                    ax.legend(
                        loc='upper left',
                        facecolor='#2a2a3e', edgecolor=C_SPINE,
                        labelcolor=C_TEXT, fontsize=8,
                    )
                    ax.grid(axis='y', color=C_GRID, linewidth=0.7, zorder=0)

                fig.tight_layout()
                canvas.draw()
                after_id[0] = root.after(500, update)
            except tk.TclError:
                pass  # Window was closed — stop scheduling further redraws

        update()
        root.mainloop()

    def cleanup(self):
        """Clean up resources."""
        print("[Client] Cleaning up...")

        # Stop engine process before anything else.
        if self.engine_manager.process is not None:
            print("[Engine] Stopping engine process...")
            self.engine_manager.stop()

        # Stop background thread FIRST before closing browser session
        self.stop_background_monitor()

        if self.browser_launcher:
            # Close browser automatically on exit
            self.browser_launcher.close()

        print("[Client] Goodbye!")


def main():
    """Main entry point."""
    client = VariantsClient()
    client.start()


if __name__ == "__main__":
    main()
