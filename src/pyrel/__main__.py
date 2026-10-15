"""Expose the same lsp actions through python -m."""

from .commands import main

raise SystemExit(main())
