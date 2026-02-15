"""Main variants client for chess.com."""
import sys
import time
from browser_launcher import BrowserLauncher
from chesscom_interface import ChessComInterface
from uci_handler import UCIHandler


class VariantsClient:
    """Main client for playing chess variants on chess.com."""

    def __init__(self):
        """Initialize the variants client."""
        self.browser_launcher = None
        self.chesscom_interface = None
        self.driver = None
        self.running = False

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
            print("  4. Return here and enter moves in UCI format")
            print("=" * 60)
            print()

            input("[Client] Press Enter when you're in a game and ready to play...")

            self.running = True
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

    def run_terminal_interface(self):
        """Run the terminal interface for move input."""
        print("=" * 60)
        print("Terminal Move Interface")
        print("=" * 60)
        print("Commands:")
        print("  - Enter UCI move (e.g., 'e2e4', 'd2d3')")
        print("  - 'debug' - Inspect board structure (troubleshooting)")
        print("  - 'quit' - Exit the client")
        print("=" * 60)
        print()

        while self.running:
            try:
                # Get user input
                user_input = input("Enter move (UCI): ").strip().lower()

                if not user_input:
                    continue

                # Handle commands
                if user_input == 'quit':
                    print("[Client] Exiting...")
                    break

                if user_input == 'debug':
                    print("[Client] Inspecting board structure...")
                    self.chesscom_interface.debug_board()
                    print()
                    continue

                # Try to parse as UCI move
                parsed_move = UCIHandler.parse_uci_move(user_input)

                if not parsed_move:
                    print(f"[Error] Invalid UCI move format: {user_input}")
                    print("[Help] Expected format: e2e4 (source square + destination square)")
                    print()
                    continue

                # Display the move
                move_display = UCIHandler.format_move_display(parsed_move)
                print(f"[Move] {move_display}")

                # Make the move on chess.com
                success = self.chesscom_interface.make_move(user_input)

                if success:
                    print("[Success] Move executed!")
                else:
                    print("[Error] Failed to execute move on chess.com")
                    print("[Help] Try running 'debug' to inspect the board structure")
                    print("[Help] Make sure you're in an active game on the board")

                print()

            except KeyboardInterrupt:
                print("\n[Client] Shutting down...")
                break
            except Exception as e:
                print(f"[Error] {e}")
                print()

    def cleanup(self):
        """Clean up resources."""
        print("[Client] Cleaning up...")

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
