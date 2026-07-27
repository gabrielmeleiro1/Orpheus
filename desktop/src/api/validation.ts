/** Shared request-parameter guard for the book-scoped endpoints. */
export function normalizeBookId(bookId: number): number {
  if (!Number.isInteger(bookId) || bookId <= 0) {
    throw new Error("bookId must be a positive integer");
  }
  return bookId;
}
