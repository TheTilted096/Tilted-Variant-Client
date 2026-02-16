"""UCI protocol handler for chess variant engines."""
import re


class UCIHandler:
    """Handles UCI protocol parsing and validation."""

    @staticmethod
    def validate_uci_move(move):
        """
        Validate a UCI move format (regular or drop).
        Supports various board sizes: 4x4, 6x6, 8x8, 10x8, 14x14, etc.

        Args:
            move: String move in UCI format

        Returns:
            bool: True if valid UCI format, False otherwise
        """
        # Regular UCI move pattern: e2e4, a7a8q, j1j8 (with promotion)
        # For all board sizes: source square + destination square + optional promotion
        # Files: a-n (supports up to 14 files)
        # Ranks: 1-14 (supports up to 14 ranks)
        # Promotion pieces: q, r, b, n, k, u, w, f, a, c (variants: Unicorn, Wazir, Ferz, Archbishop, Chancellor)
        regular_pattern = r'^[a-n][1-9][0-4]?[a-n][1-9][0-4]?[qrbnkuwfac]?$'

        # Drop move pattern for Crazyhouse/variants: P@e5, N@g3, Q@d8, etc.
        # Format: <PIECE>@<square> where PIECE is Q, R, N, B, P, U, W, F, A, C
        # Pattern is case-insensitive for squares
        drop_pattern = r'^[QRNBPUWFACqrnbpuwfac]@[a-nA-N][1-9][0-4]?$'

        move_lower = move.lower()

        return bool(re.match(regular_pattern, move_lower)) or bool(re.match(drop_pattern, move))

    @staticmethod
    def parse_uci_move(move):
        """
        Parse a UCI move string (regular or drop move).
        Supports various board sizes with multi-digit ranks (e.g., 'e10e12').

        Args:
            move: String move in UCI format (e.g., 'e2e4', 'j10j8', or 'N@g3')

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
            # Regular move: e2e4, a7a8q, j10j12, etc.
            move_lower = move.lower()

            # Parse using regex to handle multi-digit ranks
            # Pattern: <file><rank><file><rank><promotion?>
            pattern = r'^([a-n])([1-9][0-4]?)([a-n])([1-9][0-4]?)([qrbnkuwfac]?)$'
            match = re.match(pattern, move_lower)

            if not match:
                return None

            from_file, from_rank, to_file, to_rank, promotion = match.groups()

            return {
                'type': 'normal',
                'from': from_file + from_rank,
                'to': to_file + to_rank,
                'promotion': promotion if promotion else None
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
                'P': 'Pawn',
                'U': 'Unicorn',
                'W': 'Wazir',
                'F': 'Ferz',
                'A': 'Archbishop',
                'C': 'Chancellor'
            }.get(move_dict['piece'], move_dict['piece'])
            return f"Drop {piece_name} @ {move_dict['to']}"
        else:
            # Regular move
            base = f"{move_dict['from']} -> {move_dict['to']}"
            if move_dict.get('promotion'):
                base += f" (promotes to {move_dict['promotion']})"
            return base
