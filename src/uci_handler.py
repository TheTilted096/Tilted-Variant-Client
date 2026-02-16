"""UCI protocol handler for chess variant engines."""
import re


class UCIHandler:
    """Handles UCI protocol parsing and validation."""

    @staticmethod
    def validate_uci_move(move):
        """
        Validate a UCI move format (regular or drop).

        Args:
            move: String move in UCI format

        Returns:
            bool: True if valid UCI format, False otherwise
        """
        # Regular UCI move pattern: e2e4, a7a8q (with promotion)
        # For standard variants: source square (2 chars) + destination square (2 chars) + optional promotion
        # Promotion pieces: q, r, b, n, k (king for variants like Racing Kings)
        regular_pattern = r'^[a-h][1-8][a-h][1-8][qrbnk]?$'

        # Drop move pattern for Crazyhouse/variants: P@e5, N@g3, Q@d8, etc.
        # Format: <PIECE>@<square> where PIECE is Q, R, N, B, or P
        # Pattern is case-insensitive for squares
        drop_pattern = r'^[QRNBPqrnbp]@[a-hA-H][1-8]$'

        move_lower = move.lower()

        return bool(re.match(regular_pattern, move_lower)) or bool(re.match(drop_pattern, move))

    @staticmethod
    def parse_uci_move(move):
        """
        Parse a UCI move string (regular or drop move).

        Args:
            move: String move in UCI format (e.g., 'e2e4' or 'N@g3')

        Returns:
            dict: Parsed move with keys:
                - Regular move: {'type': 'normal', 'from': 'e2', 'to': 'e4', 'promotion': 'q' or None}
                - Drop move: {'type': 'drop', 'piece': 'N', 'to': 'g3'}
            None: If move is invalid
        """
        move = move.strip()

        if not UCIHandler.validate_uci_move(move):
            return None

        # Check if it's a drop move (contains '@')
        if '@' in move:
            # Drop move: P@e5, N@g3, etc.
            parts = move.upper().split('@')
            if len(parts) != 2:
                return None

            piece = parts[0]  # Q, R, N, B, or P
            square = parts[1].lower()  # e5, g3, etc.

            return {
                'type': 'drop',
                'piece': piece,
                'to': square
            }
        else:
            # Regular move: e2e4, a7a8q, etc.
            move_lower = move.lower()
            return {
                'type': 'normal',
                'from': move_lower[:2],
                'to': move_lower[2:4],
                'promotion': move_lower[4] if len(move_lower) == 5 else None
            }

        return None

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

        move_type = move_dict.get('type', 'normal')

        if move_type == 'drop':
            # Drop move: N@g3, P@e5, etc.
            piece_name = {
                'Q': 'Queen',
                'R': 'Rook',
                'N': 'Knight',
                'B': 'Bishop',
                'P': 'Pawn'
            }.get(move_dict['piece'], move_dict['piece'])
            return f"Drop {piece_name} @ {move_dict['to']}"
        else:
            # Regular move
            base = f"{move_dict['from']} -> {move_dict['to']}"
            if move_dict.get('promotion'):
                base += f" (promotes to {move_dict['promotion']})"
            return base
