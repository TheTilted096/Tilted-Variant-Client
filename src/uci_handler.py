"""UCI protocol handler for chess variant engines."""
import re


class UCIHandler:
    """Handles UCI protocol parsing and validation."""

    @staticmethod
    def validate_uci_move(move):
        """
        Validate a UCI move format.

        Args:
            move: String move in UCI format

        Returns:
            bool: True if valid UCI format, False otherwise
        """
        # Basic UCI move pattern: e2e4, a7a8q (with promotion)
        # For standard variants: source square (2 chars) + destination square (2 chars) + optional promotion
        pattern = r'^[a-h][1-8][a-h][1-8][qrbn]?$'
        return bool(re.match(pattern, move.lower()))

    @staticmethod
    def parse_uci_move(move):
        """
        Parse a UCI move string.

        Args:
            move: String move in UCI format (e.g., 'e2e4')

        Returns:
            dict: Parsed move with 'from', 'to', and optional 'promotion' keys
            None: If move is invalid
        """
        move = move.lower().strip()

        if not UCIHandler.validate_uci_move(move):
            return None

        parsed = {
            'from': move[:2],
            'to': move[2:4],
            'promotion': move[4] if len(move) == 5 else None
        }

        return parsed

    @staticmethod
    def format_move_display(move_dict):
        """
        Format a parsed move for display.

        Args:
            move_dict: Dictionary with move information

        Returns:
            str: Formatted move string
        """
        if not move_dict:
            return "Invalid move"

        base = f"{move_dict['from']} -> {move_dict['to']}"
        if move_dict.get('promotion'):
            base += f" (promotes to {move_dict['promotion']})"

        return base
