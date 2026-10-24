"""Small schema validator for the deliberately simple strict tool schemas."""
import math


def validate_arguments(name, args):
    from .tools import TOOLS
    definition = next((t for t in TOOLS if t["name"] == name), None)
    if definition is None:
        raise ValueError("Неизвестный инструмент")
    def check(value, rule):
        types = rule.get("type")
        types = types if isinstance(types, list) else [types]
        actual = "null" if value is None else "boolean" if type(value) is bool else "integer" if type(value) is int else "number" if type(value) is float else "string" if isinstance(value, str) else "array" if isinstance(value, list) else "object" if isinstance(value, dict) else None
        if actual not in types and not (actual == "integer" and "number" in types):
            raise ValueError("Неверный тип аргумента инструмента")
        if value is None:
            return
        if "enum" in rule and value not in rule["enum"]:
            raise ValueError("Недопустимое значение аргумента")
        if actual in {"integer", "number"}:
            if not math.isfinite(value) or value < rule.get("minimum", -1e12) or value > rule.get("maximum", 1e12) or value <= rule.get("exclusiveMinimum", -1e12):
                raise ValueError("Число вне допустимого диапазона")
        if actual == "object":
            if set(value) != set(rule["properties"]):
                raise ValueError("Неполный или неизвестный набор аргументов инструмента")
            for k, v in value.items():
                check(v, rule["properties"][k])
        if actual == "array":
            if not rule.get("minItems", 0) <= len(value) <= rule.get("maxItems", 1000):
                raise ValueError("Слишком много элементов")
            for v in value:
                check(v, rule["items"])
        if actual == "string" and len(value) > 200000:
            raise ValueError("Аргумент инструмента слишком длинный")
    check(args, definition["parameters"])
