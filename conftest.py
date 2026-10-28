# Ensures the project root is on sys.path, so tests can `import database`,
# `import mcp_server` and `import app` regardless of how pytest is invoked.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
