"""UCI engine manager for Tilted Variant Client.

Handles discovery, activation, process lifecycle, and move searching
for external UCI-compatible chess engine executables.
"""
import os
import subprocess
import threading
import time


# Maps chess.com URL slugs (returned by get_variant_name()) to the UCI
# variant name the engine expects in "setoption name UCI_Variant value ...".
# Slugs not present here are passed through unchanged.
_VARIANT_MAP = {
    # chess.com slug       : UCI variant name
    'chaturanga'           : 'shatranj',
    'grandchess'           : 'grand',
    'threecheck'           : '3check',
    'chess960'             : 'fischerandom',
    # The entries below are already consistent between chess.com and
    # Fairy-Stockfish, but are listed explicitly for documentation:
    'capablanca'           : 'capablanca',
    'gothic'               : 'gothic',
    'courier'              : 'courier',
    'amazon'               : 'amazon',
    'crazyhouse'           : 'crazyhouse',
    'horde'                : 'horde',
    'kingofthehill'        : 'kingofthehill',
    'racingkings'          : 'racingkings',
    'giveaway'             : 'giveaway',
    'antichess'            : 'antichess',
}


class EngineManager:
    """Manages a UCI chess engine subprocess.

    Lifecycle per game:
      1. User calls activate(name)       — selects which executable to use.
      2. Client calls start(variant, color) — spawns process + UCI handshake.
      3. Client calls request_move()     — each time it is the engine's turn.
      4. Client calls stop()             — immediately after game-over dialog
                                           is dismissed, before any loop steps.

    Search modes
    ------------
    nodes (default)
        go nodes <count>
        Ignores real time; consistent depth regardless of clock.
        Configure with set_search_nodes(n).

    time
        go wtime <ms> btime <ms> winc <ms> binc <ms>
        Mirrors a real chess clock. Both sides start with time_base ms;
        after each engine move the clock is decremented by actual think
        time then incremented by time_increment.
        Configure with set_search_time(base_ms, increment_ms).

    All text sent to and received from the engine is appended to engine.log
    in the project root, so you can monitor communication live with:
        tail -f engine.log          (Linux / Git Bash)
        Get-Content engine.log -Wait  (PowerShell)
    """

    def __init__(self, engines_dir):
        self.engines_dir = engines_dir
        self.active_engine_name = None   # executable filename chosen by user
        self.process = None              # running subprocess (or None)
        self._searching = False          # True while a go command is in flight

        # ── Search configuration ──────────────────────────────────────────────
        self.search_mode   = 'nodes'     # 'nodes' | 'time'
        self.nodes_count   = 1_000_000   # used when search_mode == 'nodes'
        self.time_base     = 60_000      # starting clock per side (ms)
        self.time_increment = 1_000      # increment per move (ms)

        # ── Per-game clock state (reset on start()) ───────────────────────────
        self.engine_color  = 'white'     # side the engine plays this game
        self.engine_time   = self.time_base  # only the engine's clock is tracked

        # ── Logging ───────────────────────────────────────────────────────────
        self._log_file = None
        self._log_path = os.path.join(
            os.path.dirname(self.engines_dir), 'engine.log'
        )

    # ── Engine discovery ──────────────────────────────────────────────────────

    def list_engines(self):
        """Return a sorted list of executable filenames in engines_dir."""
        if not os.path.isdir(self.engines_dir):
            return []
        names = []
        for name in sorted(os.listdir(self.engines_dir)):
            if name == '.gitkeep':
                continue
            path = os.path.join(self.engines_dir, name)
            if os.path.isfile(path) and os.access(path, os.X_OK):
                names.append(name)
        return names

    # ── Activation / deactivation ─────────────────────────────────────────────

    def activate(self, name):
        """Set *name* as the active engine.  Does not spawn a process.

        Returns (True, message) on success, (False, message) on failure.
        """
        available = self.list_engines()
        if name not in available:
            return False, (
                f"Engine '{name}' not found in engines/ folder.  "
                f"Available: {available or '(none)'}"
            )
        self.active_engine_name = name
        return True, f"Engine '{name}' activated (will start on next game)."

    def deactivate(self):
        """Deactivate the engine and stop any running process."""
        self.stop()
        name = self.active_engine_name
        self.active_engine_name = None
        return name

    @property
    def is_configured(self):
        """True when an engine has been selected via activate()."""
        return self.active_engine_name is not None

    # ── Search-mode configuration ─────────────────────────────────────────────

    def set_search_nodes(self, nodes):
        """Switch to node-limited search.

        Args:
            nodes: Number of nodes to search per move.
        """
        self.search_mode = 'nodes'
        self.nodes_count = nodes

    def set_search_time(self, base_ms, increment_ms):
        """Switch to time-control search.

        Both sides are given *base_ms* milliseconds at the start of each game.
        After every engine move the consumed time is subtracted and
        *increment_ms* is added back, mirroring a real chess clock.

        Args:
            base_ms:       Starting time per side in milliseconds.
            increment_ms:  Per-move increment in milliseconds.
        """
        self.search_mode    = 'time'
        self.time_base      = base_ms
        self.time_increment = increment_ms
        # Reset the engine's running clock so the new base takes effect
        # immediately, even if changed mid-game.
        self.engine_time    = base_ms

    def search_config_str(self):
        """Human-readable description of the current search configuration."""
        if self.search_mode == 'nodes':
            return f"nodes {self.nodes_count:,}"
        base_s = self.time_base / 1000
        inc_s  = self.time_increment / 1000
        return f"time  base={base_s:g}s  inc={inc_s:g}s"

    # ── Process lifecycle ─────────────────────────────────────────────────────

    def start(self, variant, engine_color='white'):
        """Spawn the engine executable and perform the UCI handshake.

        The chess.com variant slug is translated to its UCI equivalent via
        _VARIANT_MAP before being sent.  Unrecognised slugs are passed through
        unchanged.

        Sequence:
          uci → (wait for uciok)
          setoption name UCI_Variant value <uci_variant>
          ucinewgame
          isready → (wait for readyok)

        The internal clocks for both sides are reset to time_base.

        Args:
            variant:      chess.com URL slug (e.g. 'chaturanga', 'crazyhouse').
            engine_color: 'white' or 'black' — which side the engine plays.

        Returns True on success, False on any failure.
        """
        if not self.active_engine_name:
            return False

        path = os.path.join(self.engines_dir, self.active_engine_name)
        if not os.path.isfile(path) or not os.access(path, os.X_OK):
            print(f"[Engine] Executable not found or not executable: {path}")
            return False

        if self.process is not None:
            self.stop()

        # Translate chess.com slug → UCI variant name.
        uci_variant = _VARIANT_MAP.get(variant, variant) or 'chess'

        # Store engine color and reset the engine's internal clock.
        self.engine_color = engine_color
        self.engine_time  = self.time_base

        # Open (or truncate) the log file for this session.
        try:
            self._log_file = open(self._log_path, 'w', buffering=1)
            self._log(f"# engine:  {self.active_engine_name}")
            self._log(f"# variant: {variant}  →  {uci_variant}")
            self._log(f"# color:   {engine_color}")
            self._log(f"# search:  {self.search_config_str()}")
            self._log(f"# started: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            self._log("")
        except Exception as exc:
            print(f"[Engine] Warning: could not open log file: {exc}")
            self._log_file = None

        try:
            self.process = subprocess.Popen(
                [path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            print(f"[Engine] Failed to launch '{self.active_engine_name}': {exc}")
            self.process = None
            self._close_log()
            return False

        # UCI identification exchange.
        if not self._send_and_wait("uci\n", "uciok", timeout=10.0):
            print("[Engine] Timed out waiting for 'uciok'.")
            self.stop()
            return False

        # Configure variant and announce new game, then sync.
        self._send(f"setoption name UCI_Variant value {uci_variant}\n")
        self._send("ucinewgame\n")

        if not self._send_and_wait("isready\n", "readyok", timeout=10.0):
            print("[Engine] Timed out waiting for 'readyok'.")
            self.stop()
            return False

        return True

    def stop(self):
        """Terminate the engine process immediately.

        Safe to call even when no process is running.
        """
        proc = self.process
        self.process = None
        self._searching = False

        if proc is None:
            self._close_log()
            return

        try:
            proc.stdin.write("quit\n")
            proc.stdin.flush()
            self._log("> quit")
        except Exception:
            pass

        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

        self._close_log()

    # ── Move searching ────────────────────────────────────────────────────────

    def request_move(self, move_list, callback):
        """Ask the engine for the best move in a background thread.

        In *nodes* mode sends:  go nodes <count>
        In *time* mode sends:   go wtime <ms> btime <ms> winc <ms> binc <ms>

        After the search completes in time mode the engine's clock is updated:
            new_time = old_time - time_used + increment

        Calls callback(uci_move_string) when the engine returns 'bestmove'.
        If a search is already in progress the call is silently ignored.

        Args:
            move_list: Space-separated UCI moves played so far (may be "").
            callback:  Callable(str) invoked with the engine's chosen move.
        """
        if self.process is None:
            return
        if self._searching:
            self._log("# request_move ignored: search already in progress")
            return

        self._searching = True
        t_relay_start = time.monotonic()
        thread = threading.Thread(
            target=self._search_worker,
            args=(move_list, callback, t_relay_start),
            daemon=True,
        )
        thread.start()

    def _search_worker(self, move_list, callback, t_relay_start=None):
        """Background thread: send position + go, parse bestmove."""
        best = None
        think_ms = 0
        relay_ms = 0
        try:
            stripped = move_list.strip()
            if stripped:
                position_cmd = f"position startpos moves {stripped}\n"
            else:
                position_cmd = "position startpos\n"

            self._send(position_cmd)

            # Build the go command according to the current search mode.
            if self.search_mode == 'time':
                # The opponent's time is a fixed placeholder (time_base); only
                # the engine's own clock is tracked and decremented each move.
                if self.engine_color == 'white':
                    go_cmd = (
                        f"go wtime {self.engine_time} btime {self.time_base}"
                        f" winc {self.time_increment} binc {self.time_increment}\n"
                    )
                else:
                    go_cmd = (
                        f"go wtime {self.time_base} btime {self.engine_time}"
                        f" winc {self.time_increment} binc {self.time_increment}\n"
                    )
            else:
                go_cmd = f"go nodes {self.nodes_count}\n"

            # relay_ms: time from request_move() call to the moment we send
            # the go command — covers thread scheduling + position command I/O.
            t0 = time.monotonic()
            relay_ms = int((t0 - t_relay_start) * 1000) if t_relay_start is not None else 0
            self._send(go_cmd)
            best = self._read_until_bestmove(timeout=120.0)
            think_ms = int((time.monotonic() - t0) * 1000)

            if best:
                # Decrement only the engine's own clock.
                if self.search_mode == 'time':
                    self.engine_time = max(
                        0, self.engine_time - think_ms + self.time_increment
                    )
                    self._log(
                        f"# clock: {self.engine_color}={self.engine_time}ms"
                        f" (used {think_ms}ms)"
                    )
        finally:
            # Clear _searching BEFORE the callback.  The callback runs
            # _verify_move_registered() which sleeps 1-5 s.  If the opponent
            # responds during that window the monitor thread must be able to
            # start a new search immediately — otherwise the request is
            # silently dropped and the client stalls until timeout.
            self._searching = False

        if best:
            callback(best, think_ms, relay_ms)

    # ── Low-level helpers ─────────────────────────────────────────────────────

    def _send(self, cmd):
        """Write *cmd* to the engine's stdin and log it."""
        proc = self.process
        if proc is not None:
            try:
                proc.stdin.write(cmd)
                proc.stdin.flush()
                self._log(f"> {cmd.rstrip()}")
            except Exception:
                pass

    def _send_and_wait(self, cmd, expected_token, timeout=10.0):
        """Send *cmd* then read stdout lines until one contains *expected_token*.

        Returns True if the token is found within *timeout* seconds.
        """
        self._send(cmd)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process is None:
                return False
            try:
                line = self.process.stdout.readline()
            except Exception:
                return False
            if not line:
                time.sleep(0.01)
                continue
            self._log(f"< {line.rstrip()}")
            if expected_token in line:
                return True
        return False

    def _read_until_bestmove(self, timeout=120.0):
        """Read engine stdout until a 'bestmove' line; return the move string.

        Returns None on timeout, process death, or '(none)' move.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process is None:
                return None
            try:
                line = self.process.stdout.readline()
            except Exception:
                return None
            if not line:
                time.sleep(0.01)
                continue
            self._log(f"< {line.rstrip()}")
            line = line.strip()
            if line.startswith("bestmove"):
                parts = line.split()
                if len(parts) >= 2 and parts[1] != "(none)":
                    return parts[1]
                return None
        print("[Engine] Timed out waiting for 'bestmove'.")
        return None

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log(self, text):
        """Append *text* to the log file (no-op if log is not open)."""
        if self._log_file is not None:
            try:
                self._log_file.write(text + "\n")
            except Exception:
                pass

    def _close_log(self):
        """Flush and close the log file handle."""
        lf = self._log_file
        self._log_file = None
        if lf is not None:
            try:
                lf.write(f"\n# stopped: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                lf.flush()
                lf.close()
            except Exception:
                pass
