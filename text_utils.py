"""Small text-processing helpers shared by the embedding job and tests."""


def chunk_text(text: str, chunk_size: int = 800, overlap: int = 100) -> list[str]:
    """Split text into overlapping character windows."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be between 0 and chunk_size - 1")

    cleaned = " ".join(str(text or "").split()).strip()
    if not cleaned:
        return []
    if len(cleaned) <= chunk_size:
        return [cleaned]

    step = chunk_size - overlap
    chunks: list[str] = []
    for start in range(0, len(cleaned), step):
        chunk = cleaned[start : start + chunk_size].strip()
        if chunk:
            chunks.append(chunk)
        if start + chunk_size >= len(cleaned):
            break
    return chunks
