import json
import unittest
from typing import Optional, Union

from xun.toolcall import Function, ToolCallContext


def _sample_tool(
    choices: list[str],
    crop: Optional[tuple[float, float, float, float]] = None,
    value: Union[str, list[str], None] = None,
    count: int = 1,
) -> str:
    """A sample tool exercising sequence argument coercion."""
    return f"{choices} {crop} {value} {count}"


class ToolCallSequenceArgsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.function = Function.from_function(_sample_tool)
        self.context = ToolCallContext._dummy()

    def call(self, args: dict) -> str:
        return self.function.call(json.dumps(args), self.context)

    def test_list_accepts_all_argument_formats(self) -> None:
        for args in (["A", "B"], '["A", "B"]', "A, B"):
            self.assertEqual(self.call({"choices": args}), "['A', 'B'] None None 1")
        self.assertEqual(self.call({"choices": "A"}), "['A'] None None 1")

    def test_tuple_accepts_array_or_stringified_array(self) -> None:
        # JSON-decoded arrays arrive as lists; strict tuple would reject them without coercion.
        for crop in ([0, 0.5, 1, 0.5], "[0.0, 0.5, 1.0, 0.5]"):
            self.assertEqual(self.call({"choices": ["A"], "crop": crop}), "['A'] (0.0, 0.5, 1.0, 0.5) None 1")

    def test_tuple_wrong_length_still_fails(self) -> None:
        with self.assertRaises(ValueError):
            self.call({"choices": ["A"], "crop": [0.0]})

    def test_str_or_list_union_keeps_plain_string_or_list(self) -> None:
        self.assertEqual(self.call({"choices": ["A"], "value": "Enter"}), "['A'] None Enter 1")
        self.assertEqual(self.call({"choices": ["A"], "value": ["x", "y"]}), "['A'] None ['x', 'y'] 1")

    def test_non_sequence_strictness_unchanged(self) -> None:
        # String-to-number coercion must stay rejected: only sequence args are tolerant.
        with self.assertRaises(ValueError):
            self.call({"choices": ["A"], "count": "3"})

    def test_schema_unaffected_by_coercion(self) -> None:
        schema = self.function.tool_schema["function"]["parameters"]["properties"]
        self.assertEqual(schema["choices"]["type"], "array")
        self.assertEqual(schema["count"]["type"], "integer")


if __name__ == "__main__":
    unittest.main()
