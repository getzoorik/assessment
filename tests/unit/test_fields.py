"""Typed reads of JSON answers."""

import unittest
from typing import Callable, List, Tuple

from zoorik.cloud.fields import Record, parse_json, records_of, texts_of
from zoorik.errors import UnexpectedResponse

ANSWER = {
    "name": "cluster-one",
    "count": 3,
    "price": 0.5,
    "private": True,
    "zones": ["1", "2"],
    "labels": {"team": "one"},
    "profile": {"mode": "System"},
    "pools": [{"name": "system"}, {"name": "user"}],
    "terms": {"term-one": {"unit": "Hrs"}},
    "nothing": None,
}


class ParseTest(unittest.TestCase):
    """parse_json, records_of and texts_of."""

    def test_json_is_parsed(self) -> None:
        """A whole JSON answer is parsed."""
        self.assertEqual(parse_json('{"a": [1, 2]}', "az"), {"a": [1, 2]})

    def test_an_empty_or_malformed_answer_is_unexpected(self) -> None:
        """An answer that is not JSON names its source."""
        for text in ("", "not json", '{"a": '):
            with self.assertRaisesRegex(UnexpectedResponse, r"^unexpected answer: az gave no JSON$"):
                parse_json(text, "az")

    def test_a_list_of_objects_is_read(self) -> None:
        """Each object of a list is a record named by its place."""
        records = records_of([{"name": "a"}, {"name": "b"}], "az aks list")
        self.assertEqual([record.text("name") for record in records], ["a", "b"])
        self.assertEqual(records[1].path, "az aks list[1]")

    def test_a_list_of_another_shape_is_unexpected(self) -> None:
        """A value that is not a list, or holds something other than objects or texts, is named."""
        with self.assertRaisesRegex(UnexpectedResponse, r"az aks list is not a list$"):
            records_of({"name": "a"}, "az aks list")
        with self.assertRaisesRegex(UnexpectedResponse, r"az aks list\[1\] is not an object$"):
            records_of([{"name": "a"}, "b"], "az aks list")
        with self.assertRaisesRegex(UnexpectedResponse, r"regions is not a list of texts$"):
            texts_of(["eu-west-1", 2], "regions")
        self.assertEqual(texts_of(["eu-west-1"], "regions"), ["eu-west-1"])


class RecordTest(unittest.TestCase):
    """Record."""

    def setUp(self) -> None:
        """Read the made-up answer."""
        self.record = Record(ANSWER, "answer")

    def test_required_fields_are_read_as_their_type(self) -> None:
        """Each reader gives its field as the API documents it."""
        record = self.record
        self.assertEqual(record.text("name"), "cluster-one")
        self.assertEqual(record.integer("count"), 3)
        self.assertEqual(record.number("price"), 0.5)
        self.assertEqual(record.number("count"), 3.0)
        self.assertIs(record.flag("private"), True)
        self.assertEqual(record.texts("zones"), ["1", "2"])
        self.assertEqual(record.text_map("labels"), {"team": "one"})
        self.assertEqual(record.record("profile").text("mode"), "System")
        self.assertEqual([pool.text("name") for pool in record.records("pools")], ["system", "user"])
        self.assertEqual([term.text("unit") for term in record.keyed_records("terms")], ["Hrs"])

    def test_a_missing_or_null_required_field_is_unexpected(self) -> None:
        """A required field that is left out or null is named by its path."""
        for key in ("absent", "nothing"):
            with self.assertRaisesRegex(UnexpectedResponse, rf"^unexpected answer: answer\.{key} is missing$"):
                self.record.text(key)

    def test_an_optional_field_that_is_missing_or_null_is_none(self) -> None:
        """An optional field reads as None whether it is left out or sent as null."""
        record = self.record
        for key in ("absent", "nothing"):
            self.assertIsNone(record.optional_text(key))
            self.assertIsNone(record.optional_integer(key))
            self.assertIsNone(record.optional_number(key))
            self.assertIsNone(record.optional_flag(key))
            self.assertIsNone(record.optional_record(key))
            self.assertIsNone(record.optional_records(key))
            self.assertIsNone(record.optional_texts(key))
            self.assertIsNone(record.optional_keyed_records(key))
            self.assertIsNone(record.optional_text_map(key))

    def test_a_field_of_another_type_is_unexpected(self) -> None:
        """A field of the wrong type is named with the type it should have; true is not a number."""
        record = self.record
        cases: List[Tuple[Callable[[], object], str]] = [
            (lambda: record.text("count"), r"answer\.count is not a text"),
            (lambda: record.integer("private"), r"answer\.private is not a whole number"),
            (lambda: record.integer("price"), r"answer\.price is not a whole number"),
            (lambda: record.number("private"), r"answer\.private is not a number"),
            (lambda: record.flag("name"), r"answer\.name is not true or false"),
            (lambda: record.record("name"), r"answer\.name is not an object"),
            (lambda: record.records("profile"), r"answer\.profile is not a list"),
            (lambda: record.keyed_records("zones"), r"answer\.zones is not an object"),
            (lambda: record.text_map("terms"), r"answer\.terms is not an object of texts"),
        ]
        for read, message in cases:
            with self.assertRaisesRegex(UnexpectedResponse, message):
                read()

    def test_a_record_must_be_an_object(self) -> None:
        """A value that is not an object can't be read as a record."""
        with self.assertRaisesRegex(UnexpectedResponse, r"^unexpected answer: az account show is not an object$"):
            Record(["sub-one"], "az account show")
