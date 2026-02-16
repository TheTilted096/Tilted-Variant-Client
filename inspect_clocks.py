#!/usr/bin/env python3
"""Inspect clock elements to find color indicators."""

import sys
sys.path.insert(0, '/home/user/Tilted-Variant-Client/src')

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

def inspect_clocks():
    """Inspect clock structure and color indicators."""

    print("Connecting to Chrome...")
    chrome_options = Options()
    chrome_options.add_experimental_option("debuggerAddress", "127.0.0.1:9222")

    try:
        driver = webdriver.Chrome(options=chrome_options)
        print("✓ Connected to Chrome\n")
    except Exception as e:
        print(f"✗ Could not connect: {e}")
        return

    print("Inspecting playerboxes...")

    # Simple script to find playerboxes first
    try:
        playerboxes_exist = driver.execute_script("""
            const top = document.querySelector('.playerbox-top');
            const bottom = document.querySelector('.playerbox-bottom');
            return {
                hasTop: top !== null,
                hasBottom: bottom !== null
            };
        """)
        print(f"  Top playerbox found: {playerboxes_exist['hasTop']}")
        print(f"  Bottom playerbox found: {playerboxes_exist['hasBottom']}")

        if not playerboxes_exist['hasTop'] or not playerboxes_exist['hasBottom']:
            print("\n⚠ Warning: Not all playerboxes found. Make sure you're in a game!")
            return
    except Exception as e:
        print(f"Error checking playerboxes: {e}")
        return

    print("\nGetting playerbox details...\n")

    # Get top box info
    try:
        print("  Querying TOP playerbox...")
        top_info = driver.execute_script("""
            const box = document.querySelector('.playerbox-top');
            if (!box) return { error: 'Not found' };

            const userTag = box.querySelector('.playerbox-user-tag');
            const username = userTag ? userTag.textContent.trim() : 'Unknown';

            return {
                username: username,
                boxClasses: box.className,
                html: box.outerHTML.substring(0, 500)
            };
        """)
        print(f"    ✓ Got top box info")
    except Exception as e:
        print(f"    ✗ Error: {e}")
        top_info = {'error': str(e)}

    # Get bottom box info
    try:
        print("  Querying BOTTOM playerbox...")
        bottom_info = driver.execute_script("""
            const box = document.querySelector('.playerbox-bottom');
            if (!box) return { error: 'Not found' };

            const userTag = box.querySelector('.playerbox-user-tag');
            const username = userTag ? userTag.textContent.trim() : 'Unknown';

            return {
                username: username,
                boxClasses: box.className,
                html: box.outerHTML.substring(0, 500)
            };
        """)
        print(f"    ✓ Got bottom box info\n")
    except Exception as e:
        print(f"    ✗ Error: {e}\n")
        bottom_info = {'error': str(e)}

    # Print results
    print("="*70)
    print("PLAYERBOX INSPECTION RESULTS")
    print("="*70)

    print("\nTOP PLAYERBOX:")
    if 'error' in top_info:
        print(f"  Error: {top_info['error']}")
    else:
        print(f"  Username: {top_info.get('username', 'N/A')}")
        print(f"  Classes: {top_info.get('boxClasses', 'N/A')}")
        print(f"  HTML preview: {top_info.get('html', 'N/A')[:200]}...")

    print("\nBOTTOM PLAYERBOX:")
    if 'error' in bottom_info:
        print(f"  Error: {bottom_info['error']}")
    else:
        print(f"  Username: {bottom_info.get('username', 'N/A')}")
        print(f"  Classes: {bottom_info.get('boxClasses', 'N/A')}")
        print(f"  HTML preview: {bottom_info.get('html', 'N/A')[:200]}...")

    print("\n" + "="*70)

if __name__ == "__main__":
    inspect_clocks()
