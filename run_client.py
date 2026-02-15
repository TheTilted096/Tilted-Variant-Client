#!/usr/bin/env python3
"""Launcher script for the Tilted Variants Client."""
import sys
import os

# Add src directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from variants_client import main

if __name__ == "__main__":
    main()
