"""``dmz completion bash|zsh|fish``: tab completion for the command tree."""
from __future__ import annotations

COMMANDS = ["auth", "whoami", "doctor", "init", "validate", "plan", "apply", "verify", "destroy", "drift",
            "stack", "stacks", "keys", "check", "hold", "release", "engage", "states", "decisions", "reviews",
            "watch", "mcp", "completion"]
SUBCOMMANDS = {
    "auth": ["set", "status", "clear"],
    "keys": ["mint"],
    "reviews": ["claim", "resolve", "release", "hold", "escalate"],
    "mcp": ["tools", "resources", "read", "call", "enforce", "record", "ping", "config", "bridge"],
    "init": ["chatbot", "agent", "sdk-app", "desk", "logic", "mcp"],
    "completion": ["bash", "zsh", "fish"],
}


def register(sub, formatter) -> None:
    p = sub.add_parser("completion", help="print a tab-completion script for your shell", formatter_class=formatter,
                       epilog="examples:\n  eval \"$(dmz completion bash)\"\n  dmz completion zsh > ~/.zfunc/_dmz\n"
                              "  dmz completion fish > ~/.config/fish/completions/dmz.fish")
    p.add_argument("shell", choices=["bash", "zsh", "fish"])
    p.set_defaults(func=completion)


def completion(args, ctx_factory) -> int:
    print({"bash": bash, "zsh": zsh, "fish": fish}[args.shell]())
    return 0


def bash() -> str:
    cases = "\n".join(f'    {k}) COMPREPLY=( $(compgen -W "{" ".join(v)}" -- "$cur") ); return;;'
                      for k, v in SUBCOMMANDS.items())
    return f"""_dmz() {{
  local cur prev cmd
  COMPREPLY=()
  cur="${{COMP_WORDS[COMP_CWORD]}}"
  cmd="${{COMP_WORDS[1]}}"
  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=( $(compgen -W "{" ".join(COMMANDS)}" -- "$cur") ); return
  fi
  if [ "$COMP_CWORD" -eq 2 ]; then case "$cmd" in
{cases}
  esac; fi
  case "$cur" in -*) COMPREPLY=( $(compgen -W "--json --help --env-file --profile --base-url --quiet --color" -- "$cur") );; esac
}}
complete -F _dmz dmz"""


def zsh() -> str:
    subs = "\n".join(f"      {k}) _values 'action' {' '.join(v)};;" for k, v in SUBCOMMANDS.items())
    return f"""#compdef dmz
_dmz() {{
  local -a commands
  commands=({" ".join(COMMANDS)})
  if (( CURRENT == 2 )); then
    _describe 'command' commands; return
  fi
  if (( CURRENT == 3 )); then
    case "$words[2]" in
{subs}
    esac
  fi
  _files
}}
_dmz "$@\""""


def fish() -> str:
    lines = [f"complete -c dmz -n '__fish_use_subcommand' -a '{c}'" for c in COMMANDS]
    for k, v in SUBCOMMANDS.items():
        lines.append(f"complete -c dmz -n '__fish_seen_subcommand_from {k}' -a '{' '.join(v)}'")
    lines.append("complete -c dmz -l json -d 'machine-readable output'")
    lines.append("complete -c dmz -l env-file -r -d 'read the key and ids from this env file'")
    lines.append("complete -c dmz -l profile -r -d 'saved profile'")
    return "\n".join(lines)
