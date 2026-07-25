"""Stand-in extension module: any package the caller owns can look like this."""


def build_globals(ctx):
    def greet(name):
        return f"hello {name} from {ctx['request_id'][:8]}"

    def echo_extra():
        return ctx.get("extra", {})

    return {"greet": greet, "echo_extra": echo_extra}


def not_a_dict_provider(ctx):
    return "nope"
