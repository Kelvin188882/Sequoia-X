"""股票代码格式转换工具。"""


def to_xueqiu_code(code: str) -> str:
    """将纯数字代码转为雪球格式：6开头→SH，4/8开头→BJ，其余→SZ。"""
    if code.startswith("6"):
        return f"SH{code}"
    elif code.startswith(("4", "8")):
        return f"BJ{code}"
    return f"SZ{code}"
