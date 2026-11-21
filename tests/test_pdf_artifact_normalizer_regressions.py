"""Regression coverage for pdf artifact normalizer."""

import pytest

from modules.ingestion.pdf_artifact_normalizer import normalize_pdf_artifacts


def test_04_pdf_numbered_headings_remain_distinct():
    pages = [{"text": f"Experiment {number}\nThe measurement was recorded for this trial."} for number in (1, 2)]
    result = normalize_pdf_artifacts(pages)
    assert all(f"Experiment {number}" in page["text"] for number, page in enumerate(result, 1))


def test_notes_subheading_followed_by_prose_keeps_the_rest_of_the_page():
    def recipe_page(n):
        return "\n".join([
            f"Recipe {2 * n - 1} Lemon Curd for batch {n}",
            f"Whisk {n + 2} eggs with the sugar until pale.",
            f"Stir the batch {n} curd over a low heat.",
            f"Pour batch {n} into warm jars.",
            "Notes",
            f"Use unwaxed lemons for batch {n}.",
            f"Recipe {2 * n} Plum Tart for batch {n}",
            f"Halve the plums for batch {n} and arrange them.",
            f"Bake batch {n} until the pastry is golden.",
            f"Serve batch {n} warm with cream.",
            f"Leftovers from batch {n} keep for two days.",
        ])

    result = normalize_pdf_artifacts([{"text": recipe_page(n)} for n in (1, 2, 3)])
    for n, cleaned in enumerate(result, 1):
        assert f"Use unwaxed lemons for batch {n}." in cleaned["text"]
        assert f"Halve the plums for batch {n} and arrange them." in cleaned["text"]


def test_terminal_notes_and_references_with_entries_are_removed():
    pages = [
        {"text": "The essay closes here.\nNotes\n1. Smith, On Rivers, p. 4.\n2. Ibid., p. 9."},
        {"text": "The study ends.\nReferences\nJones, A. (2001). Tides. Ocean Press.\nLee, B. Maps."},
    ]
    result = normalize_pdf_artifacts(pages)
    assert [page["text"] for page in result] == ["The essay closes here.", "The study ends."]


def test_wrapped_sentence_starting_with_a_figure_reference_is_kept():
    page = (
        "The census data were collected over ten years. As shown in\n"
        "Figure 2, the population grew by forty percent over the decade,\n"
        "which surprised the planners who had expected a decline."
    )
    result = normalize_pdf_artifacts([{"text": page}])
    assert "Figure 2, the population grew by forty percent over the decade," in result[0]["text"]


def test_standalone_figure_captions_are_removed():
    page = (
        "The census data were collected over ten years.\n"
        "Figure 2. Population by decade.\n"
        "Fig. 3: Regional distribution\n"
        "Plate 4 — The harbour at dawn\n"
        "The planners had expected a decline."
    )
    result = normalize_pdf_artifacts([{"text": page}])
    assert result[0]["text"] == (
        "The census data were collected over ten years.\nThe planners had expected a decline."
    )


_KITCHEN_FILLER = (
    "The kitchen was quiet that morning and the oven ticked as it warmed.\n"
    "She measured everything twice before she began, as her mother had.\n"
)


@pytest.mark.parametrize(
    ("page", "kept"),
    [
        pytest.param(
            "Recipe 1 Lemon Cake\n" + _KITCHEN_FILLER + "Notes\nUse unwaxed lemons for the zest.\n"
            "Recipe 2 Plum Tart\n4 ripe plums\n200 g flour\n100 g butter\n1 egg\n2 tbsp sugar\n"
            "Rub the butter into the flour until it looks like sand.",
            ["Use unwaxed lemons for the zest.", "Recipe 2 Plum Tart", "4 ripe plums", "1 egg",
             "Rub the butter into the flour until it looks like sand."],
            id="ingredient-quantities",
        ),
        pytest.param(
            "The war ended and the town rebuilt.\nNotes\nThe archive was moved in 1946.\n"
            "In 1948 the council voted to rebuild the old bridge, which had stood since 1802,\n"
            "and by 1951 the work was finished. The mayor, who had served since 1939,\n"
            "opened it in a short ceremony. Few people came.",
            ["The archive was moved in 1946.", "In 1948 the council voted", "Few people came."],
            id="prose-with-years",
        ),
    ],
)
def test_notes_box_over_quantities_or_dated_prose_keeps_the_rest_of_the_page(page, kept):
    result = normalize_pdf_artifacts([{"text": page}, {"text": "The next page carries on with the story."}])
    for text in kept:
        assert text in result[0]["text"]


def test_figure_reference_with_a_parenthetical_panel_is_prose():
    page = (
        "The results were clear.\n"
        "Figure 2 (left) shows the growth rate over the decade, and\n"
        "the right panel shows the decline in the same period."
    )
    result = normalize_pdf_artifacts([{"text": page}, {"text": "Another page of prose here that ends properly."}])
    assert "Figure 2 (left) shows the growth rate over the decade, and" in result[0]["text"]


def test_captions_after_a_heading_or_with_a_panel_label_are_removed():
    page = (
        "2.3 Growth of the Cities\n"
        "Figure 2.1 Map of the cities in 1900\n"
        "The cities grew quickly after the railway arrived, and many people moved.\n"
        "Figure 2.2 (a). The harbour in 1910\n"
        "The harbour grew with them."
    )
    result = normalize_pdf_artifacts([{"text": page}, {"text": "Another page of prose here that ends properly."}])
    assert result[0]["text"] == (
        "2.3 Growth of the Cities\n"
        "The cities grew quickly after the railway arrived, and many people moved.\n"
        "The harbour grew with them."
    )
