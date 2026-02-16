#!/usr/bin/env python3
"""Test script for player color detection using data-player attribute."""

import sys
sys.path.insert(0, '/home/user/Tilted-Variant-Client/src')

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.edge.options import Options as EdgeOptions
from chesscom_interface import ChessComInterface

def test_detection():
    """Test player color detection on current game."""

    print("Attempting to connect to browser...")
    print("(Trying Edge first, then Chrome)")

    driver = None

    # Try Edge first (since user is using Edge)
    try:
        edge_options = EdgeOptions()
        edge_options.add_experimental_option("debuggerAddress", "127.0.0.1:9222")
        driver = webdriver.Edge(options=edge_options)
        print("✓ Connected to Edge")
    except Exception as e:
        print(f"  Could not connect to Edge: {e}")

    # Fallback to Chrome
    if not driver:
        try:
            chrome_options = Options()
            chrome_options.add_experimental_option("debuggerAddress", "127.0.0.1:9222")
            driver = webdriver.Chrome(options=chrome_options)
            print("✓ Connected to Chrome")
        except Exception as e:
            print(f"✗ Could not connect to Chrome: {e}")
            print("\nMake sure the client is running with remote debugging enabled")
            return

    # Create interface
    interface = ChessComInterface(driver)

    print("\n" + "="*60)
    print("PLAYER COLOR DETECTION TEST")
    print("="*60)

    # Test username detection
    print("\n1. Username Detection")
    print("-" * 60)
    username = interface.get_username_from_page()

    if username:
        print(f"✓ Username: {username}")
    else:
        print("✗ Could not detect username")
        return

    # Test color detection (this will use data-player attribute)
    print("\n2. Color Detection (data-player method)")
    print("-" * 60)
    color = interface.get_player_color()

    if color in ['white', 'black']:
        print(f"✓ Your color: {color.upper()}")
    else:
        print(f"✗ Could not determine color: {color}")

    # Test board orientation
    print("\n3. Board Orientation")
    print("-" * 60)
    is_flipped = interface.is_board_flipped()
    print(f"✓ Board flipped: {is_flipped}")

    print("\n" + "="*60)
    print("🎉 TEST COMPLETE!")
    print("="*60)

if __name__ == "__main__":
    test_detection()
