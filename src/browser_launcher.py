"""Browser launcher module for Edge with remote debugging."""
import re
import subprocess
import time
import os
import sys
from selenium import webdriver
from selenium.webdriver.edge.service import Service
from selenium.webdriver.edge.options import Options
from selenium.common.exceptions import WebDriverException

_WEBDRIVER_NOISE_RE = re.compile(
    r'\s*\n\s*from unknown error:.*'
    r'|\s*\n\s*\(Session info:.*'
    r'|\s*Stacktrace:\s*\n.*',
    re.DOTALL,
)


def _short_err(exc):
    """Return a concise one-liner from a (possibly verbose) exception."""
    msg = _WEBDRIVER_NOISE_RE.sub('', str(exc)).strip()
    if msg.startswith('Message: '):
        msg = msg[len('Message: '):]
    return msg


class BrowserLauncher:
    """Handles launching and connecting to Edge browser with debugging enabled."""

    def __init__(self, debugging_port=9223):
        """
        Initialize the browser launcher.

        Args:
            debugging_port: Port for remote debugging (default: 9223)
        """
        self.debugging_port = debugging_port
        self.driver = None
        self.edge_process = None

    def find_edge_executable(self):
        """Find the Edge executable path."""
        if sys.platform == "win32":
            # Windows paths
            possible_paths = [
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            ]
        elif sys.platform == "darwin":
            # macOS path
            possible_paths = [
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
            ]
        else:
            # Linux paths
            possible_paths = [
                "/usr/bin/microsoft-edge",
                "/usr/bin/microsoft-edge-stable",
                "/usr/bin/microsoft-edge-beta",
                "/usr/bin/microsoft-edge-dev",
            ]

        for path in possible_paths:
            if os.path.exists(path):
                return path

        return None

    def launch_edge_process(self):
        """Launch Edge as a subprocess with debugging enabled."""
        edge_path = self.find_edge_executable()

        if not edge_path:
            raise RuntimeError(
                "Could not find Edge executable. Please ensure Microsoft Edge is installed.\n"
                "If Edge is installed in a non-standard location, please update the paths in browser_launcher.py"
            )

        print(f"[Browser] Found Edge at: {edge_path}")
        print(f"[Browser] Launching Edge with debugging on port {self.debugging_port}...")

        # Build Edge command with debugging flags
        edge_args = [
            edge_path,
            f"--remote-debugging-port={self.debugging_port}",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            "--start-maximized",
            # ── Background / occlusion / throttling flags ─────────────────────
            # Layer 1 – renderer process: prevent the renderer from being
            # deprioritised when the window loses focus or is hidden.
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-background-media-suspend",
            # Layer 2 – Chrome feature flags that cause the "works briefly then
            # spikes" pattern observed when the window is minimised/covered:
            #   IntensiveWakeUpThrottling – throttles JS timers to ≤1/minute
            #     after 5 s of the page being hidden.  This is the primary
            #     cause of the delayed spike: the grace period hides the
            #     problem at first, then latency jumps to ~5 s once it kicks in.
            #   CalculateNativeWinOcclusion – Windows-specific: uses the Win32
            #     API to track whether the browser window is occluded/minimised
            #     and triggers a separate throttling path that --disable-
            #     backgrounding-occluded-windows does not cover.
            "--disable-features=IntensiveWakeUpThrottling,CalculateNativeWinOcclusion",
            # Open the variants lobby in a full browser window (not app/PWA
            # mode) so that installed extensions such as Cold Turkey Blocker
            # are active and the instance is recognised as a normal Edge tab.
            "https://www.chess.com/variants",
        ]

        try:
            # Launch Edge as a subprocess
            if sys.platform == "win32":
                # Windows: use CREATE_NEW_PROCESS_GROUP to allow Edge to run independently
                self.edge_process = subprocess.Popen(
                    edge_args,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                # Unix-like: just spawn the process
                self.edge_process = subprocess.Popen(edge_args)

            print("[Browser] Edge process started, waiting for it to be ready...")
            time.sleep(3)  # Wait for Edge to fully start
            print("[Browser] Edge should now be running with debugging enabled!")
            return True

        except Exception as e:
            print(f"[Browser] Failed to launch Edge process: {_short_err(e)}")
            raise

    def connect_to_edge(self):
        """Connect Selenium to the already-running Edge instance."""
        print(f"[Browser] Connecting to Edge on debugging port {self.debugging_port}...")

        edge_options = Options()
        edge_options.add_experimental_option("debuggerAddress", f"127.0.0.1:{self.debugging_port}")

        try:
            # Connect to existing Edge instance
            self.driver = webdriver.Edge(options=edge_options)
            # execute_async_script needs an explicit timeout; 5 s is ample
            # for any in-page async work (inter-click gap is ≤ 200 ms).
            self.driver.set_script_timeout(5)
            # Cap the HTTP timeout for all WebDriver commands.  Without
            # this, a dead browser leaves urllib3 retrying TCP connections
            # for ~60 s before surfacing a MaxRetryError.  10 s is more
            # than enough for any legitimate command (JS snippets complete
            # in <100 ms) and lets session-death detection fire quickly.
            try:
                self.driver.command_executor.set_timeout(10)
            except Exception:
                pass
            print("[Browser] Successfully connected to Edge!")

            # ── CDP anti-throttling (survives page navigations) ───────────────
            # Layer 3 – focus emulation: Chrome's focus-loss throttling is
            # suppressed entirely; the tab always behaves as if it has focus.
            try:
                self.driver.execute_cdp_cmd(
                    'Emulation.setFocusEmulationEnabled', {'enabled': True}
                )
            except Exception:
                pass

            # Layer 4 – Page Visibility API override: inject a script that
            # runs on every new document so chess.com (and any other page code
            # that pauses on visibilityState === 'hidden') always sees the
            # page as visible, even when the window is minimised or covered.
            try:
                self.driver.execute_cdp_cmd(
                    'Page.addScriptToEvaluateOnNewDocument',
                    {
                        'source': (
                            'Object.defineProperty(document,"visibilityState",'
                            '{get:()=>"visible",configurable:true});'
                            'Object.defineProperty(document,"hidden",'
                            '{get:()=>false,configurable:true});'
                        )
                    },
                )
            except Exception:
                pass

            # Close any tabs that Edge restored from a previous session so
            # we start with a single chess.com/variants tab.
            self._close_extra_tabs()

            return self.driver

        except Exception as e:
            print(f"[Browser] Failed to connect to Edge: {_short_err(e)}")
            print("[Browser] Make sure Edge is running with debugging enabled.")
            raise

    def _close_extra_tabs(self):
        """Close duplicate tabs, keeping only one.

        Edge may restore tabs from a previous session alongside the one
        opened by our launch command, resulting in two (or more) chess.com
        tabs.  Close all but the last window handle (which is the tab our
        launch command opened).
        """
        try:
            handles = self.driver.window_handles
            if len(handles) <= 1:
                return
            # The tab opened by our launch command is typically the last
            # handle.  Keep it; close everything else.
            keep = handles[-1]
            for h in handles:
                if h != keep:
                    self.driver.switch_to.window(h)
                    self.driver.close()
            self.driver.switch_to.window(keep)
            print(f"[Browser] Closed {len(handles) - 1} restored tab(s)")
        except Exception:
            # Non-fatal — if tab cleanup fails we can still function.
            pass

    def launch_edge(self):
        """Launch Edge and connect to it."""
        # First, launch Edge as a subprocess
        self.launch_edge_process()

        # Then, connect Selenium to it
        return self.connect_to_edge()

    def is_session_alive(self):
        """Return True if the WebDriver session is still responsive."""
        if not self.driver:
            return False
        try:
            self.driver.title  # lightweight round-trip
            return True
        except WebDriverException:
            return False

    def reconnect(self):
        """Relaunch Edge and reconnect Selenium after a crash.

        Returns the new WebDriver instance, or raises on failure.
        """
        print("[Browser] Session dead — attempting to relaunch Edge...")

        # Dispose of the stale driver handle (ignore errors; it's dead)
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

        # Kill any lingering Edge process from the previous session
        if self.edge_process:
            try:
                self.edge_process.kill()
            except Exception:
                pass
            self.edge_process = None

        # Relaunch and reconnect
        self.launch_edge_process()
        return self.connect_to_edge()

    def navigate_to_chesscom_variants(self):
        """Navigate to chess.com variants page."""
        if not self.driver:
            raise RuntimeError("Browser not launched. Call launch_edge() first.")

        print("[Browser] Navigating to chess.com variants...")
        self.driver.get("https://www.chess.com/variants")
        time.sleep(2)
        print("[Browser] Loaded chess.com variants page")

    def get_driver(self):
        """Get the WebDriver instance."""
        return self.driver

    def close(self):
        """Close the browser and terminate the Edge process."""
        if self.driver:
            print("[Browser] Closing Selenium connection...")
            try:
                self.driver.quit()
            except Exception as e:
                print(f"[Browser] Error closing driver: {_short_err(e)}")
            self.driver = None

        # Terminate the Edge process
        if self.edge_process:
            print("[Browser] Terminating Edge process...")
            try:
                if sys.platform == "win32":
                    # Primary: find the process listening on our debugging port
                    # and kill its entire process tree.  The stored PID
                    # (self.edge_process.pid) may belong to a short-lived
                    # launcher that already exited and handed off to an existing
                    # Edge instance, making a direct /PID kill unreliable.
                    port = self.debugging_port
                    ps_cmd = (
                        f'$p = (Get-NetTCPConnection -LocalPort {port} '
                        f'-State Listen -ErrorAction SilentlyContinue'
                        f').OwningProcess | Select-Object -First 1; '
                        f'if ($p) {{ taskkill /F /T /PID $p | Out-Null }}'
                    )
                    subprocess.run(
                        ['PowerShell', '-NoProfile', '-Command', ps_cmd],
                        capture_output=True, timeout=10,
                    )
                    # Fallback: also attempt by stored PID in case the
                    # debugging port is not yet bound (e.g. killed very early).
                    subprocess.run(
                        ['taskkill', '/F', '/T', '/PID',
                         str(self.edge_process.pid)],
                        capture_output=True,
                    )
                else:
                    # Unix-like: send SIGTERM
                    self.edge_process.terminate()
                    # Wait up to 2 seconds for graceful shutdown
                    try:
                        self.edge_process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        # Force kill if it doesn't terminate
                        self.edge_process.kill()
                print("[Browser] Edge process terminated")
            except Exception as e:
                print(f"[Browser] Error terminating Edge process: {_short_err(e)}")
