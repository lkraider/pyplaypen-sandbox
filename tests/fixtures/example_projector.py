"""Stand-in type_projector: a custom class no one but this test knows about."""


class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y


def project(value):
    if isinstance(value, Point):
        return {"x": value.x, "y": value.y}
    raise ValueError(f"no projection for {type(value).__name__}")


def broken_project(value):
    raise RuntimeError("always fails")


class Bomb:
    pass


def infinite_project(value):
    """Never converges to plain data: always hands back another unsupported
    value. The projection depth cap must stop this, not the interpreter's
    recursion limit or a hang."""
    return Bomb()
