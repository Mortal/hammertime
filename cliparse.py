"""Helper for building a CLI from type-annotated functions.

make_cliparser() returns a (subcommand, main) pair. Functions decorated
with @subcommand gain an argparse subcommand whose arguments are derived
from the function's parameter annotations:

- Annotated[T, Arg(...)] / Annotated[T, Pos(...)] add option/positional metadata
- Literal[...] becomes a choices argument
- Optional[...] with a None default becomes an optional argument
- bool (defaulting to False) becomes a store_true flag
"""

import argparse
import functools
import inspect
import textwrap
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass
from inspect import Parameter
from typing import Annotated, Any, Literal


@dataclass(frozen=True)
class Pos:
    """Annotation metadata: pass the parameter as a positional argument."""

    metavar: str
    help: str | None = None


@dataclass(frozen=True)
class Arg:
    """Annotation metadata: override the option string (e.g. "-x"), help, metavar."""

    arg: str | None = None
    help: str | None = None
    metavar: str | None = None


def make_cliparser(
    help: str, usage: str, fun_prefix: str
) -> tuple[Callable[[Callable[..., None]], None], Callable[[], None]]:
    """Return (subcommand_decorator, main) for a CLI with the given usage string.

    Decorated functions must be named fun_prefix + <subcommand name>; the
    prefix is stripped to derive the subcommand name.
    """
    parser = argparse.ArgumentParser(usage=usage, add_help=False)
    parser.add_argument("--help", "-h", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("help")

    def main() -> None:
        args = parser.parse_args()
        if args.help or args.command == "help" or args.command is None:
            print(help.strip())
            raise SystemExit
        args.subcommand_impl(args)

    def subcommand(main_func: Callable[..., None]) -> None:
        """
        Decorator applied to htime_FOO functions to configure subcommands,
        using the function's parameter annotations to define CLI arguments.
        """
        assert main_func.__name__.startswith(fun_prefix)
        subparser = subparsers.add_parser(
            main_func.__name__.removeprefix(fun_prefix),
            description=textwrap.dedent(main_func.__doc__ or "").strip(),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        type_hints = typing.get_type_hints(main_func, include_extras=True)
        signature = inspect.signature(main_func)

        @functools.wraps(main_func)
        def wrapped(namespace: argparse.Namespace) -> None:
            # Adapt the argparse Namespace back into positional/keyword
            # arguments matching main_func's signature.
            args = []
            kwargs = {}
            for k, parm in signature.parameters.items():
                match parm.kind:
                    case Parameter.POSITIONAL_ONLY | Parameter.POSITIONAL_OR_KEYWORD:
                        args.append(getattr(namespace, k))
                    case Parameter.VAR_POSITIONAL:
                        args.extend(getattr(namespace, k))
                    case Parameter.KEYWORD_ONLY:
                        kwargs[k] = getattr(namespace, k)
                    case Parameter.VAR_KEYWORD:
                        raise Exception("Unimplemented: **kwargs")
                    case _:
                        raise Exception("Unknown parameter type")
            main_func(*args, **kwargs)

        subparser.set_defaults(subcommand_impl=wrapped)
        for arg_name, parameter in signature.parameters.items():
            the_type = type_hints[arg_name]
            # Process the_type, which can be e.g.
            # Annotated[Union[Literal["a", "b"], None], Arg(...)]
            # ...to parameters we can pass to subparser.add_argument().

            arg_info: Arg | Pos | None = None
            if typing.get_origin(the_type) is Annotated:
                the_type, arg_info = typing.get_args(the_type)
                assert isinstance(arg_info, Arg | Pos)

            required = True
            default: Any = None
            if parameter.default is not inspect.Parameter.empty:
                required = False
                default = parameter.default
                if default is None:
                    assert typing.get_origin(the_type) in (
                        typing.Union,
                        types.UnionType,
                    ), typing.get_origin(the_type)
                    non_none = [
                        t for t in typing.get_args(the_type) if t is not type(None)
                    ]
                    assert len(non_none) == 1, non_none
                    (the_type,) = non_none

            choices: list[Any] | tuple[Any, ...] | None = None
            if typing.get_origin(the_type) is Literal:
                choices = typing.get_args(the_type)
                the_type = type(choices[0])
                if not all(type(c) is the_type for c in choices):
                    raise Exception("For Literal, all values must be the same type")

            if parameter.kind == Parameter.VAR_POSITIONAL:
                assert the_type is not bool
                assert choices is None
                subparser.add_argument(
                    dest=arg_name,
                    metavar=None if arg_info is None else arg_info.metavar,
                    type=the_type,
                    nargs="*",
                    choices=choices,
                    help=None if arg_info is None else arg_info.help,
                )
                continue

            if isinstance(arg_info, Pos):
                assert the_type is not bool
                subparser.add_argument(
                    dest=arg_name,
                    metavar=arg_info.metavar,
                    type=the_type,
                    nargs=None if required else "?",
                    choices=choices,
                    help=arg_info.help,
                )
                continue

            help_text = arg_info.help if arg_info else None
            long = f"--{arg_name.replace('_', '-')}"
            arg = [long, arg_info.arg] if arg_info and arg_info.arg else [long]
            assert all(a.startswith("-") for a in arg)
            if the_type is bool:
                assert not required
                assert default is False
                assert choices is None
                assert arg_info is None or arg_info.metavar is None
                subparser.add_argument(
                    *arg,
                    action="store_true",
                    dest=arg_name,
                    required=required,
                    help=help_text,
                )
                continue
            subparser.add_argument(
                *arg,
                type=the_type,
                dest=arg_name,
                metavar=None if arg_info is None else arg_info.metavar,
                required=required,
                default=default,
                choices=choices,
                help=help_text,
            )

    return subcommand, main
