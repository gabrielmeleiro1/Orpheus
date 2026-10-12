"""Pure limit constants shared across app, web, and pipeline layers.

These live in ``modules/contracts`` so there is exactly one definition each;
``pipeline`` and ``app`` re-export from here for backward compatibility while
``web`` imports them directly instead of reaching into lower layers.
"""

# Maximum accepted book title length (characters) for workspace/directory naming.
MAX_BOOK_TITLE_LENGTH = 200

# Maximum accepted author and tag text lengths (characters) for library metadata.
MAX_BOOK_AUTHOR_LENGTH = 200
MAX_BOOK_TAGS_LENGTH = 500

# Maximum accepted uploaded source size (bytes).
MAX_UPLOAD_BYTES = 500 * 1024 * 1024
