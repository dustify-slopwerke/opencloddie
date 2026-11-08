import unittest

from tests.harness import ROOT  # noqa: F401

from opencloddie.models import (
    DEFAULT_MODELS,
    context_tokens_for,
    find_model,
    model_ids,
    normalize_model,
    normalize_models,
)


class ModelsTest(unittest.TestCase):
    def test_defaults(self):
        ids = model_ids(normalize_models(None))
        self.assertIn("deepseek-v4.1-flash", ids)
        self.assertIn("qwen3.8-flash", ids)
        self.assertEqual(normalize_models(None), [dict(m) for m in DEFAULT_MODELS])

    def test_accepts_strings_and_objects(self):
        models = normalize_models(
            ["alpha", {"id": "beta", "label": "Beta", "context_tokens": "1234"}, {"bad": True}]
        )
        self.assertEqual([m["id"] for m in models], ["alpha", "beta"])
        self.assertEqual(models[1]["context_tokens"], 1234)

    def test_normalize_model(self):
        models = normalize_models(["a", "b"])
        self.assertEqual(normalize_model(models, "b", "a"), "b")
        self.assertEqual(normalize_model(models, "missing", "a"), "a")
        self.assertEqual(normalize_model(models, None, "missing"), "a")

    def test_context(self):
        models = normalize_models(
            [{"id": "a", "context_tokens": 1000}, {"id": "b", "context_tokens": None}]
        )
        self.assertEqual(context_tokens_for(models, "a"), 1000)
        self.assertIsNone(context_tokens_for(models, "b"))
        self.assertIsNone(context_tokens_for(models, "zzz"))

    def test_find(self):
        models = normalize_models(["a"])
        self.assertEqual(find_model(models, "a")["id"], "a")
        self.assertIsNone(find_model(models, "b"))


if __name__ == "__main__":
    unittest.main()
