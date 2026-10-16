"""Regression coverage for pdf artifact normalizer."""

from modules.ingestion.pdf_artifact_normalizer import normalize_pdf_artifacts


def test_04_pdf_numbered_headings_remain_distinct():
    pages = [{"text": f"Experiment {number}\nThe measurement was recorded for this trial."} for number in (1, 2)]
    result = normalize_pdf_artifacts(pages)
    assert all(f"Experiment {number}" in page["text"] for number, page in enumerate(result, 1))
