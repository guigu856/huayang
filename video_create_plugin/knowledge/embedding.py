from __future__ import annotations

import hashlib
import math
import re
import unicodedata

from .models import EMBEDDING_DIMENSION, EMBEDDING_VERSION

_TEXT_PART = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff0-9a-z]+")


class ChineseCharNgramEmbedding:
    """固定版本的中文字符 n-gram feature hashing 向量。"""

    version = EMBEDDING_VERSION
    dimension = EMBEDDING_DIMENSION

    def embed(self, text: str) -> list[float]:
        parts = _TEXT_PART.findall(unicodedata.normalize("NFKC", text).lower())
        grams = [
            part[index : index + width]
            for part in parts
            for width in (1, 2, 3)
            for index in range(len(part) - width + 1)
        ]
        if not grams:
            raise ValueError("文本不包含可索引字符")

        vector = [0.0] * self.dimension
        for gram in grams:
            digest = hashlib.blake2b(
                gram.encode("utf-8"),
                digest_size=8,
                person=b"vc-zh-ngram-v1",
            ).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign

        magnitude = math.sqrt(sum(value * value for value in vector))
        return [value / magnitude for value in vector]
