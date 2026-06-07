# Kokoro Single-Book Benchmark

This folder benchmarks elapsed time for one book on this Mac. It compares:

- the old array readback path at the first chunk size by default
- the old array readback path at every chunk size with `--include-array-baselines`
- the new Kokoro file-result path at each requested chunk size
- optional cleaner/TTS chunk overlap for every requested chunk size
- repeated runs with aggregate mean, median, min, max, and stdev

The runner writes one metrics folder per case plus:

```text
output/kokoro_single_book/kokoro_single_book_summary.json
```

Run from the repo root:

```bash
python benchmarks/kokoro_single_book/run_kokoro_single_book.py "test_books/A Modest Proposal (AmazonClassics Edition) -- Swift, Jonathan -- AmazonClassics Edition, 2019 -- Amazon Classics -- 44ef80c76e8a9c098ca90e31070dda81 -- Anna’s Archive.epub" --chunk-sizes 500 700 900 1100 1300 --include-array-baselines --include-overlap --repetitions 3
```
