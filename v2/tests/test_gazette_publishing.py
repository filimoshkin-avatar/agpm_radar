"""What an issue's address is, and where its title comes from.

Both questions used to be answered by hand: the address was a filename somebody
typed into three lists, and the title was typed into the tool beside the file it
had to match. Since 2026-09-05 the address carries the digest of the bytes it
serves - the route is `immutable` for a year, so a revision at the old address
would never reach a returning reader, and the activator refuses it outright -
and the title is read out of the document.
"""

from __future__ import annotations

import sqlite3

import pytest
from apps.api.public_data import PublicDataRepository
from tools.build_gazette_candidate import html_title


def _repository() -> PublicDataRepository:
    return PublicDataRepository(sqlite3.connect(":memory:"))


def test_the_title_is_read_the_way_the_validator_reads_it() -> None:
    """`_GazetteHtmlParser` resolves entities, so this has to as well.

    A `<title>` holding `&mdash;` used to make the tool fail with "title differs
    from candidate title", naming a value the operator had never typed.
    """
    source = "<title>Новости &mdash; 1 &laquo;AgPM&raquo;</title>".encode()
    assert html_title(source) == "Новости — 1 «AgPM»"
    # Attributes on the tag, and whitespace the parser collapses.
    attributed = '<title lang="ru">  Два   слова\n</title>'.encode()
    assert html_title(attributed) == "Два слова"
    with pytest.raises(ValueError, match="no <title>"):
        html_title(b"<html><body>no title here</body></html>")
    with pytest.raises(ValueError, match="title is empty"):
        html_title(b"<title>   </title>")


def test_a_second_html_file_does_not_take_the_whole_endpoint_down() -> None:
    """Nothing upstream enforces one HTML file per issue.

    Not the validator, not the candidate contract, and `gazettes` has no column
    for an entrypoint. A published issue with a print version beside it used to
    answer 503 for `/api/gazettes` - taking every readable issue down with it.
    """
    repository = _repository()
    period = "2026-09"
    # The historical name wins when it is there.
    assert (
        repository._gazette_url(
            period, ["gazettes/2026-09/index.html", "gazettes/2026-09/print.html"]
        )
        == "/gazettes/2026-09/index.html"
    )
    # Otherwise the first, and the query orders them, so it is deterministic.
    assert (
        repository._gazette_url(
            period, ["gazettes/2026-09/index-abc123.html", "gazettes/2026-09/print.html"]
        )
        == "/gazettes/2026-09/index-abc123.html"
    )
    # No HTML at all: the directory address, which is what it was before.
    assert repository._gazette_url(period, []) == "/gazettes/2026-09/"


def test_every_stored_path_shape_resolves_to_the_same_route() -> None:
    """Two shapes live in the database and both have to come out addressable.

    The Stage 11 seed wrote `gazettes/<period>/index.html`; the fixtures write a
    bare `index.html`. `_gazette_asset` in apps/api/application.py looks a route
    up by all three forms.
    """
    repository = _repository()
    for stored in ("gazettes/2026-08/index.html", "2026-08/index.html", "index.html"):
        assert repository._gazette_url("2026-08", [stored]) == "/gazettes/2026-08/index.html"
    # A path that climbs out of its period is not turned into a route.
    assert repository._gazette_url("2026-08", ["other/2026-08/index.html"]) == "/gazettes/2026-08/"
