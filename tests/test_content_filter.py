from modules.ingestion.content_filter import (
    ContentClassification,
    SkippedSection,
    classify_chapter_content,
    filter_audiobook_chapters,
)
from modules.ingestion.models import Chapter


def test_classifies_index_with_alphabetized_page_references_as_skip():
    content = "\n".join(
        [
            "Anderson, Mary 12, 44, 109",
            "Barnes, Thomas 15, 61",
            "Carter, Evelyn 18, 72, 119",
            "Dawson, River 31, 88",
            "Ellis Island 41, 42",
            "Franklin, Anna 53, 100",
        ]
    )

    classification = classify_chapter_content("Index", content)

    assert classification == ContentClassification(
        should_skip=True,
        reason="index",
        confidence=0.95,
    )


def test_classifies_references_with_citation_density_as_skip():
    content = "\n".join(
        [
            "Adams, J. (2018). The long memory of cities. Oxford University Press.",
            "Baker, L. and Chen, M. (2020). Archive fever revisited. Journal of History, 14(2), 33-49.",
            "Davis, P. (2016). Reading by lantern light. New York: North River Press.",
            "Evans, R. (2021). Listening rooms and public speech. https://example.org/listening",
            "Foster, N. (2019). Oral traditions in print. Cambridge University Press.",
        ]
    )

    classification = classify_chapter_content("Bibliography", content)

    assert classification.should_skip is True
    assert classification.reason == "references"
    assert classification.confidence >= 0.9


def test_classifies_real_extracted_bibliography_with_split_citation_lines_as_skip():
    content = "\n".join(
        [
            "194                             Bibliography",
            ". Rhetoric and Poetics. Translated by W. Rhys Roberts and In-",
            "gram Bywater. New York: The Modern Library, 1954.",
            "Asensio y Toledo, Jose Maria, and Marcelino Menendez y Pelayo, /n-",
            "troduccion. Madrid: Imprenta Central, 1905.",
            "Bataillon, Marcel. Erasmo y Espana. Mexico City: Fondo de Cultura, 1966.",
            "Castro, Americo. El pensamiento de Cervantes. Barcelona: Noguer, 1972.",
            "Cervantes, Miguel de. Don Quixote. Translated by Walter Starkie. New York:",
            "New American Library, 1964.",
            "Gilman, Stephen. The Novel According to Cervantes. Berkeley: University of",
            "California Press, 1989.",
            "Riley, E. C. Cervantes's Theory of the Novel. Oxford: Clarendon Press, 1962.",
        ]
    )

    classification = classify_chapter_content("Bibliography", content)

    assert classification.should_skip is True
    assert classification.reason == "references"


def test_classifies_two_column_subject_index_as_skip():
    content = "\n".join(
        [
            "Subject Index",
            "Numbers in italics refer to figures.",
            "A                                          bacteria 31, 260",
            "adaptation 205                             balance 250",
            "alarm pheromones 87, 88                    beekeeping 16, 23, 275",
            "antennae 44, 45, 89                        brood 120, 122, 125",
            "Apis mellifera 11, 14, 20                  caste 33, 66",
            "architecture 184, 187                      colony 7, 9, 31, 80",
            "aristotle 5                                communication 91, 102, 109",
            "waggle dance 98, 99, 101                   wax 151, 152",
        ]
    )

    classification = classify_chapter_content("Subject Index", content)

    assert classification.should_skip is True
    assert classification.reason == "index"


def test_classifies_ebook_index_with_locator_lines_after_intro_note_as_skip():
    content = "\n".join(
        [
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            "Index",
            "The page numbers in this index refer to the printed version of this book.",
            "adaptability, 22, 48, 193",
            "aggression, 41, 52-54, 301",
            "ambition, 3, 14, 77, 108",
            "anxiety, 31, 201",
            "character, 82, 83, 212-216",
            "envy, 97, 101, 145",
            "status, 5, 18, 59, 203",
        ]
    )

    classification = classify_chapter_content("Index", content)

    assert classification.should_skip is True
    assert classification.reason == "index"


def test_classifies_table_of_contents_with_title_page_lines_as_skip():
    content = "\n".join(
        [
            "Foreword ................................ ix",
            "Chapter 1 The Arrival .................... 1",
            "Chapter 2 The Winter Road ............... 23",
            "Chapter 3 A Letter from Home ............ 47",
            "Epilogue ................................ 211",
        ]
    )

    classification = classify_chapter_content("Contents", content)

    assert classification.should_skip is True
    assert classification.reason == "table_of_contents"
    assert classification.confidence >= 0.9


def test_classifies_list_of_maps_as_skip():
    content = "\n".join(
        [
            "Europe in 1740",
            "The Silesian Campaign",
            "The Seven Years War",
            "Prussia after 1763",
        ]
    )

    classification = classify_chapter_content("List of Maps", content)

    assert classification.should_skip is True
    assert classification.reason == "list_of_maps"
    assert classification.confidence >= 0.9


def test_does_not_classify_map_or_figure_prose_titles_as_map_lists():
    for title in ("Map of the Heart", "Figure of Speech"):
        classification = classify_chapter_content(
            title,
            "This short chapter title is figurative\nand continues as prose without punctuation",
        )

        assert classification.should_skip is False


def test_classifies_copyright_and_legal_page_as_skip():
    content = (
        "Copyright (c) 2024 Example Press. All rights reserved.\n"
        "No part of this publication may be reproduced or transmitted in any form.\n"
        "ISBN 978-1-23456-789-0\n"
        "Published by Example Press, New York."
    )

    classification = classify_chapter_content("Copyright", content)

    assert classification.should_skip is True
    assert classification.reason == "copyright"
    assert classification.confidence >= 0.9


def test_classifies_very_low_prose_density_as_skip():
    content = "\n".join(
        [
            "1",
            "2",
            "3",
            "Figures",
            "Table 1",
            "12",
            "19",
            "A",
            "B",
            "C",
            "24",
            "31",
        ]
    )

    classification = classify_chapter_content("Tables", content)

    assert classification.should_skip is True
    assert classification.reason == "low_prose_density"
    assert classification.confidence >= 0.85


def test_keeps_normal_prose_with_numbers_and_chapter_title():
    content = (
        "Chapter 12\n\n"
        "At 3 in the afternoon, Mara counted 27 windows along the station wall. "
        "The numbers mattered to her only because they kept her mind away from "
        "the letter in her coat pocket. When the train finally arrived, nobody "
        "on the platform moved for a long moment."
    )

    classification = classify_chapter_content("Chapter 12", content)

    assert classification.should_skip is False
    assert classification.reason is None
    assert classification.confidence <= 0.5


def test_filter_preserves_order_renumbers_dataclass_chapters_and_reports_skips():
    chapters = [
        Chapter(number=1, title="Contents", content="Chapter 1 ........ 1\nChapter 2 ........ 9\nChapter 3 ........ 22"),
        Chapter(number=2, title="Chapter 1", content="The room was quiet. " * 20),
        Chapter(number=3, title="Index", content="Apples 1, 3\nBakers 7, 9\nCandles 12, 19\nDoors 22, 25"),
        Chapter(number=4, title="Chapter 2", content="The road bent toward the sea. " * 20),
    ]

    retained, skipped = filter_audiobook_chapters(chapters)

    assert retained == [
        Chapter(number=1, title="Chapter 1", content="The room was quiet. " * 20),
        Chapter(number=2, title="Chapter 2", content="The road bent toward the sea. " * 20),
    ]
    assert skipped == [
        SkippedSection(
            original_number=1,
            title="Contents",
            reason="table_of_contents",
            confidence=0.95,
            chars=len(chapters[0].content),
        ),
        SkippedSection(
            original_number=3,
            title="Index",
            reason="index",
            confidence=0.95,
            chars=len(chapters[2].content),
        ),
    ]
    assert chapters[1].number == 2
    assert retained[0] is not chapters[1]


class PlainChapterLike:
    def __init__(self, number: int, title: str, content: str) -> None:
        self.number = number
        self.title = title
        self.content = content


def test_filter_falls_back_to_shallow_copy_for_non_dataclass_chapter_like_objects():
    first = PlainChapterLike(10, "Chapter One", "A complete paragraph of prose. " * 20)
    second = PlainChapterLike(11, "Copyright", "Copyright 2024. All rights reserved. ISBN 1234567890.")
    third = PlainChapterLike(12, "Chapter Two", "Another complete paragraph of prose. " * 20)

    retained, skipped = filter_audiobook_chapters([first, second, third])

    assert [chapter.number for chapter in retained] == [1, 2]
    assert [chapter.title for chapter in retained] == ["Chapter One", "Chapter Two"]
    assert retained[0] is not first
    assert first.number == 10
    assert skipped[0].original_number == 11
    assert skipped[0].reason == "copyright"


def test_keeps_narrative_prose_with_parenthetical_citation_year():
    content = (
        "The historian argued that memory is selective (Miller, 2019), especially "
        "when witnesses recall events from decades earlier. She repeated the point "
        "in the next paragraph to keep the chapter readable."
    )
    classification = classify_chapter_content("Chapter 7", content)
    assert classification.should_skip is False
    assert classification.reason is None


def test_keeps_narrative_prose_with_doi_and_isbn_mentions_in_body():
    content = (
        "The archivist noted that the DOI https://example.org/paper/9 was printed on "
        "a handbill, not in the novel itself. ISBN numbers belong on copyright pages, "
        "she said, and this chapter is ordinary narrative prose about the discovery."
    )
    classification = classify_chapter_content("Discovery", content)
    assert classification.should_skip is False


def test_keeps_chapter_title_with_colon_and_substantive_body():
    content = (
        "The committee convened before dawn. Their debate about self-governing towns "
        "lasted through breakfast and into the promise-making season without ever "
        "resembling a bibliography or table of contents."
    )
    classification = classify_chapter_content("Chapter 3: The Argument", content)
    assert classification.should_skip is False
    assert classification.reason is None


def test_keeps_intentional_hyphenated_compounds_in_prose():
    content = (
        "The twenty-first-century harbor still used old stone quays. Mara described "
        "a well-known self-governing village where promise-making rituals continued "
        "each spring without interruption."
    )
    classification = classify_chapter_content("Chapter 8", content)
    assert classification.should_skip is False


def test_keeps_long_midbook_nonfiction_chapter_about_publishing():
    # A substantive chapter that merely discusses publishing mentions many
    # bibliographic terms (publisher, ISBN, permissions) but is real narrative
    # prose and must not be deleted just because it crosses the term threshold.
    paragraph = (
        "The modern publisher occupies a peculiar position in the literary economy. "
        "When an author signs a contract, the publisher acquires certain rights and, "
        "in exchange, assumes responsibility for copyright registration, the assignment "
        "of an ISBN, and the negotiation of permissions for any quoted material. Every "
        "reprint requires that these permissions be cleared again, and the publisher's "
        "legal department tracks each grant so that no part of the finished book "
        "infringes another author's work. This chapter follows one editor through a "
        "single acquisition to show how the sausage of publishing is actually made. "
    )
    content = (paragraph * 12).strip()
    assert len(content.split()) > 450

    classification = classify_chapter_content("The Business of Publishing", content)

    assert classification.should_skip is False
    assert classification.reason is None


def test_keeps_short_real_copyright_page_as_skip():
    content = (
        "Copyright © 2021 by Harbor House Press.\n"
        "All rights reserved. No part of this publication may be reproduced or "
        "transmitted in any form without written permission.\n"
        "ISBN 978-0-00-000000-0\n"
        "Published by Harbor House Press, New York.\n"
        "First edition."
    )
    assert len(content.split()) < 400

    classification = classify_chapter_content("Copyright", content)

    assert classification.should_skip is True
    assert classification.reason == "copyright"


def test_keeps_short_narrative_that_mentions_publishing_terms():
    content = (
        "The publisher's office kept a copy of the contract beside the manuscript. "
        "She checked the ISBN, reviewed permissions, and discussed copyright with the editor, "
        "then returned to the story of how the first edition reached the harbor town."
    )

    classification = classify_chapter_content("The Editor", content)

    assert classification.should_skip is False
    assert classification.reason is None


def test_low_prose_density_rule_can_be_disabled_via_flag():
    content = "\n".join(
        ["1", "2", "3", "Figures", "Table 1", "12", "19", "A", "B", "C", "24", "31"]
    )

    assert classify_chapter_content("Tables", content).reason == "low_prose_density"
    assert (
        classify_chapter_content(
            "Tables", content, enable_low_prose_density=False
        ).should_skip
        is False
    )


def test_filter_respects_disabled_low_prose_density_flag():
    verse = Chapter(
        number=1,
        title="Poem",
        content="\n".join(
            [
                "pale moon rising",
                "cold river flows",
                "the wind is low",
                "night falls deep",
                "stars are bright",
                "silent are hills",
                "soft rain falls",
                "grey clouds go",
                "birds fly home",
                "day fades slow",
            ]
        ),
    )

    dropped, _ = filter_audiobook_chapters([verse])
    assert dropped == []

    kept, skipped = filter_audiobook_chapters([verse], enable_low_prose_density=False)
    assert [chapter.title for chapter in kept] == ["Poem"]
    assert skipped == []
