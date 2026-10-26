// The last book the listener played, so the next launch opens on it and
// Continue listening picks up where they stopped. A convenience only: when
// storage is unavailable the library simply opens on its newest book.
const LAST_PLAYED_BOOK_KEY = "orpheus.library.last-played-book.v1";

export function readLastPlayedBookId(): number | null {
  try {
    const value = Number.parseInt(window.localStorage.getItem(LAST_PLAYED_BOOK_KEY) ?? "", 10);
    return Number.isInteger(value) && value > 0 ? value : null;
  } catch {
    return null;
  }
}

export function rememberLastPlayedBookId(bookId: number | null): void {
  try {
    if (bookId === null) window.localStorage.removeItem(LAST_PLAYED_BOOK_KEY);
    else window.localStorage.setItem(LAST_PLAYED_BOOK_KEY, String(bookId));
  } catch {
    // Private windows and blocked storage just lose the preference.
  }
}
