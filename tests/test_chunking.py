from text_utils import chunk_text


def test_short_text_has_one_chunk():
    assert chunk_text("short warning", 100, 10) == ["short warning"]


def test_long_text_overlaps():
    chunks = chunk_text("x" * 250, 100, 20)
    assert len(chunks) == 3
    assert all(len(chunk) <= 100 for chunk in chunks)


def test_invalid_overlap_is_rejected():
    try:
        chunk_text("text", 100, 100)
    except ValueError:
        return
    raise AssertionError("Expected ValueError")
