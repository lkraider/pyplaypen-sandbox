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
