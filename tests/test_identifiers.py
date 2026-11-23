import pytest

from polyextract.schemas.identifiers import extract_dois, make_paper_id, normalize_doi, normalize_title


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("10.5555/ABC.123", "10.5555/abc.123"),
        ("https://doi.org/10.5555/abc", "10.5555/abc"),
        ("http://dx.doi.org/10.5555/abc", "10.5555/abc"),
        ("doi: 10.5555/abc.", "10.5555/abc"),
        ("10.5555/0032-3861(93)90001-X", "10.5555/0032-3861(93)90001-x"),
        ("https://pubs.example.org/doi/10.5555/abc,", "10.5555/abc"),
        ("no doi here", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_doi(raw, expected):
    assert normalize_doi(raw) == expected


def test_extract_dois_unique():
    assert extract_dois("see 10.5555/a and doi.org/10.5555/A and 10.5555/b") == ["10.5555/a", "10.5555/b"]


def test_normalize_title_strips_markup_and_accents():
    assert normalize_title("Effect of M<sub>w</sub> on T<sub>g</sub>: Polystyrène!") == "effect of mw on tg polystyrene"


def test_paper_id_stable():
    assert make_paper_id("10.5555/x", "t", 2000) == "doi:10.5555/x"
    a = make_paper_id(None, "TEST FIXTURE Title", 2000)
    assert a == make_paper_id(None, "test fixture  title!", 2000)
    assert a != make_paper_id(None, "TEST FIXTURE Title", 2001)
