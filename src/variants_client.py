"""Main variants client for chess.com."""
import sys
import time
import threading
from browser_launcher import BrowserLauncher
from chesscom_interface import ChessComInterface
from uci_handler import UCIHandler


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
        # Background monitoring thread
        self.monitor_thread = None
        self.monitor_thread_running = False
        self.monitor_interval = 0.05  # Check console logs every 50ms (very lightweight)
        # Automated variant-loop state
        self.loop_running = False
        self.loop_thread = None

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

            # Also periodically check for game start
            self.check_for_game_start()

        except Exception as e:
            # Only print non-session errors (session errors are expected on shutdown)
            if 'invalid session' not in str(e).lower():
                print(f"[Debug] Error in process_console_events: {e}")

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
                move = self.chesscom_interface.get_last_move(verbose=False)
                if move:
                    self.move_list += move + " "
                    _bg_print(f"[+] {move}")
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

                # Wait for play/rematch buttons to render after dialog closes.
                time.sleep(2.0)

                # Reset game state tracking
                self.last_game_number = game_number
                self.was_in_game = False
                self.current_game_number = None

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

                _bg_print("=" * 60)
                _bg_print("[Game State] 🎮 GAME STARTED!")
                _bg_print(f"[Game State] Game #{new_number}")
                self.current_game_number = new_number
                self.last_game_number = None  # Consumed; clear so future games work
                if start_info.get('method'):
                    _bg_print(f"[Game State] Detected via: {start_info['method']}")
                if game_state['color']:
                    _bg_print(f"[Game State] You are playing as: {game_state['color']}")
                _bg_print("=" * 60)
                self.was_in_game = True
                self.move_list = ""

                # Reset the game over observer and dedup tracker for the new game
                self.game_over_handled_for = None
                self.chesscom_interface.reset_game_over_observer()

                # Re-initialise the move observer now that the game page is loaded.
                # The initial setup (at startup) runs before any game is open, so
                # .moves-table doesn't exist yet and the observer is a no-op.
                if self.chesscom_interface.setup_move_observer():
                    _bg_print("[Game State] ✓ Move observer re-initialised")
                else:
                    _bg_print("[Game State] ⚠ Could not re-initialise move observer")

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
        print("  - 'status' - Check current game state (in game or not)")
        print("  - 'getmove' - Detect the last move played on the board (UCI format)")
        print("  - 'movelist' - Print the move history for the current game")
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
                print(f"[+] {user_input}")

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
        """Background thread: challenge → wait → play → rematch → repeat.

        'loop stop' behaviour:
          - In lobby / challenge phase: pending challenges are cancelled
            immediately and the loop exits.
          - In game: the current game is allowed to finish naturally; once it
            ends the loop exits without attempting a rematch.
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

            # ── GAME + REMATCH INNER LOOP ─────────────────────────────────────
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
                # current_game_number. Waiting for both conditions ensures
                # rematch() is never called while the dialog is still open.
                while True:
                    if (self.game_over_handled_for == game_num
                            and self.current_game_number is None):
                        break
                    time.sleep(0.5)

                # Extra pause so the Rematch button is fully rendered.
                time.sleep(1.0)

                # Game finished — honour a pending stop request now.
                if not self.loop_running:
                    _bg_print("[Loop] Game over — loop stopped (no rematch).")
                    break

                # ── Rematch window (15 seconds) ───────────────────────────────
                _bg_print("[Loop] Game over — requesting rematch "
                          "(15-second window)...")
                rematch_sent = self.chesscom_interface.rematch()

                if not rematch_sent:
                    # Button not found — opponent likely already left.
                    _bg_print("[Loop] Rematch button not found — "
                              "going to lobby...")
                    self.chesscom_interface.exit_to_lobby()
                    time.sleep(2.0)
                    break

                deadline = time.time() + 15
                rematch_accepted = False

                while self.loop_running:
                    cur = self.current_game_number
                    if cur is not None and cur != game_num:
                        rematch_accepted = True
                        break
                    if time.time() > deadline:
                        break
                    time.sleep(0.5)

                if rematch_accepted:
                    _bg_print(f"[Loop] Rematch accepted — "
                              f"Game #{self.current_game_number}")
                    continue  # Stay in inner loop for the new game

                # Rematch not accepted (timeout or stop requested).
                if not self.loop_running:
                    # Stop was requested during the rematch window.
                    _bg_print("[Loop] Stop requested — cancelling rematch...")
                    self.chesscom_interface.rematch()  # second click cancels
                    time.sleep(0.5)
                    break

                # Normal timeout: go directly to lobby for next challenge.
                # Opponent may have already left; don't attempt a cancel click.
                _bg_print("[Loop] Rematch not accepted — going to lobby...")
                self.chesscom_interface.exit_to_lobby()
                time.sleep(2.0)
                break  # Back to outer challenge loop

        _bg_print("[Loop] Stopped.")
        self.loop_running = False

    def cleanup(self):
        """Clean up resources."""
        print("[Client] Cleaning up...")

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
