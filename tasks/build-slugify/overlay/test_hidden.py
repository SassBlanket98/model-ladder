"""Acceptance tests. Copied into the work dir after the model finishes; the model never sees them."""

import unittest

from textutil import slugify


class SlugifyAcceptance(unittest.TestCase):
    def test_existing_behaviour_kept(self) -> None:
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_accents_reduced_to_ascii(self) -> None:
        self.assertEqual(slugify("Café au lait"), "cafe-au-lait")
        self.assertEqual(slugify("Ångström über"), "angstrom-uber")

    def test_runs_collapse_to_one_hyphen(self) -> None:
        self.assertEqual(slugify("a  --  b"), "a-b")
        self.assertEqual(slugify("rock & roll!!!"), "rock-roll")

    def test_no_leading_or_trailing_hyphen(self) -> None:
        self.assertEqual(slugify("  Hello! "), "hello")

    def test_digits_kept(self) -> None:
        self.assertEqual(slugify("Top 10 of 2024"), "top-10-of-2024")

    def test_nothing_usable_gives_empty_string(self) -> None:
        self.assertEqual(slugify("!!! ???"), "")
        self.assertEqual(slugify(""), "")

    def test_non_latin_letters_are_separators(self) -> None:
        self.assertEqual(slugify("tea 茶 time"), "tea-time")


if __name__ == "__main__":
    unittest.main()
