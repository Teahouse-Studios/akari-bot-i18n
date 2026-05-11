from typing import Any

def flatten_dict(nested_dict: dict[str, Any], parent_key: str = "", sep: str = ".") -> dict[str, Any]:
    """将嵌套字典扁平化。"""
    flat_dict = {}
    for k, v in nested_dict.items():
        flat_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            flat_dict.update(flatten_dict(v, flat_key, sep))
        else:
            flat_dict[flat_key] = v
    return flat_dict