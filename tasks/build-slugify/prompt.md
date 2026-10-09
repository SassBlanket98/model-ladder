`textutil.py` has a `slugify(title)` function used to build URL slugs. Users report three problems:

1. Titles with accented letters lose those letters ("Café au lait" becomes "caf-au-lait").
2. Runs of punctuation or spaces produce repeated hyphens ("a  --  b" becomes "a------b").
3. Slugs can start or end with a hyphen ("  Hello! " becomes "-hello-").

Fix `slugify` so that accented letters are reduced to their plain ASCII letter, any run of
characters that are not ASCII letters or digits becomes a single hyphen, and the result has no
leading or trailing hyphen. An input with no letters or digits returns an empty string.
Keep the function signature. Use the standard library only. `test_textutil.py` must still pass.
