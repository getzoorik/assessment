"""Typed reads of JSON answers: each field as its API documents it, and UnexpectedResponse for any other shape."""

import json
from typing import Dict, List, Mapping, Optional, TypeVar, Union

from zoorik.errors import UnexpectedResponse

Value = TypeVar("Value")


def parse_json(text: str, source: str) -> object:
    """Parse a whole answer from source; an empty or malformed answer is unexpected."""
    try:
        value: object = json.loads(text)
    except ValueError:
        raise UnexpectedResponse(f"unexpected answer: {source} gave no JSON") from None
    return value


def records_of(value: object, path: str) -> List["Record"]:
    """Read an answer that is a list of objects."""
    return [Record(item, f"{path}[{index}]") for index, item in enumerate(_list(value, path))]


def texts_of(value: object, path: str) -> List[str]:
    """Read an answer that is a list of texts."""
    items = _list(value, path)
    if not all(isinstance(item, str) for item in items):
        raise UnexpectedResponse(f"unexpected answer: {path} is not a list of texts")
    return [str(item) for item in items]


def _list(value: object, path: str) -> List[object]:
    """Give value as a list, or raise UnexpectedResponse naming path."""
    if not isinstance(value, list):
        raise UnexpectedResponse(f"unexpected answer: {path} is not a list")
    return list(value)


class Record:
    """One JSON object of an answer, read field by field.

    A required field that is missing or null, or any field of another type, raises UnexpectedResponse naming its path.
    An optional field, one the API documents as left out or sent as null when it doesn't apply, reads as None.
    """

    def __init__(self, value: object, path: str) -> None:
        """Read value, which must be an object; path names it in a failure, such as az aks list[2]."""
        if not isinstance(value, dict):
            raise UnexpectedResponse(f"unexpected answer: {path} is not an object")
        self._fields: Mapping[str, object] = value
        self.path = path

    def text(self, key: str) -> str:
        """Read a required text."""
        return self._present(key, self.optional_text(key))

    def optional_text(self, key: str) -> Optional[str]:
        """Read an optional text."""
        value = self._fields.get(key)
        if value is None or isinstance(value, str):
            return value
        raise self._wrong(key, "a text")

    def integer(self, key: str) -> int:
        """Read a required whole number."""
        return self._present(key, self.optional_integer(key))

    def optional_integer(self, key: str) -> Optional[int]:
        """Read an optional whole number."""
        value = self._fields.get(key)
        if value is None or (isinstance(value, int) and not isinstance(value, bool)):
            return value
        raise self._wrong(key, "a whole number")

    def optional_whole_or_text(self, key: str) -> Union[int, str, None]:
        """Read an optional field that holds a whole number or a text, as a Kubernetes int-or-string field does."""
        value = self._fields.get(key)
        if value is None or isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool)):
            return value
        raise self._wrong(key, "a whole number or a text")

    def number(self, key: str) -> float:
        """Read a required number."""
        return self._present(key, self.optional_number(key))

    def optional_number(self, key: str) -> Optional[float]:
        """Read an optional number."""
        value = self._fields.get(key)
        if value is None:
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        raise self._wrong(key, "a number")

    def flag(self, key: str) -> bool:
        """Read a required true or false."""
        return self._present(key, self.optional_flag(key))

    def optional_flag(self, key: str) -> Optional[bool]:
        """Read an optional true or false."""
        value = self._fields.get(key)
        if value is None or isinstance(value, bool):
            return value
        raise self._wrong(key, "true or false")

    def record(self, key: str) -> "Record":
        """Read a required object."""
        return self._present(key, self.optional_record(key))

    def optional_record(self, key: str) -> Optional["Record"]:
        """Read an optional object."""
        value = self._fields.get(key)
        return None if value is None else Record(value, f"{self.path}.{key}")

    def records(self, key: str) -> List["Record"]:
        """Read a required list of objects."""
        return self._present(key, self.optional_records(key))

    def optional_records(self, key: str) -> Optional[List["Record"]]:
        """Read an optional list of objects."""
        value = self._fields.get(key)
        return None if value is None else records_of(value, f"{self.path}.{key}")

    def texts(self, key: str) -> List[str]:
        """Read a required list of texts."""
        return self._present(key, self.optional_texts(key))

    def optional_texts(self, key: str) -> Optional[List[str]]:
        """Read an optional list of texts."""
        value = self._fields.get(key)
        return None if value is None else texts_of(value, f"{self.path}.{key}")

    def keyed_records(self, key: str) -> List["Record"]:
        """Read a required object whose values are objects under ids, as the AWS Price List's terms are."""
        return self._present(key, self.optional_keyed_records(key))

    def optional_keyed_records(self, key: str) -> Optional[List["Record"]]:
        """Read an optional object whose values are objects under ids, as a list of those objects."""
        value = self._fields.get(key)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise self._wrong(key, "an object")
        return [Record(item, f"{self.path}.{key}.{name}") for name, item in value.items()]

    def text_map(self, key: str) -> Dict[str, str]:
        """Read a required object whose values are all texts."""
        return self._present(key, self.optional_text_map(key))

    def optional_text_map(self, key: str) -> Optional[Dict[str, str]]:
        """Read an optional object whose values are all texts, such as labels."""
        value = self._fields.get(key)
        if value is None:
            return None
        if isinstance(value, dict) and all(isinstance(item, str) for item in value.values()):
            return {str(name): str(item) for name, item in value.items()}
        raise self._wrong(key, "an object of texts")

    def _present(self, key: str, value: Optional[Value]) -> Value:
        """Give a required field's value; a missing or null one is unexpected."""
        if value is None:
            raise UnexpectedResponse(f"unexpected answer: {self.path}.{key} is missing")
        return value

    def _wrong(self, key: str, kind: str) -> UnexpectedResponse:
        """Make the failure for a field of the wrong type."""
        return UnexpectedResponse(f"unexpected answer: {self.path}.{key} is not {kind}")
