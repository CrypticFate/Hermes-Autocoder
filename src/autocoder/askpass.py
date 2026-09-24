#!/usr/bin/env python3
import os
import sys

if __name__ == "__main__":
    print("x-access-token" if "username" in sys.argv[1].lower() else os.environ["AUTOCODER_GIT_TOKEN"])
