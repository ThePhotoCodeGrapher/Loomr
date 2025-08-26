import os
import sys
import shutil
from pathlib import Path
from typing import Optional, List

import typer
from rich import print
from rich.prompt import Confirm
import yaml

APP = typer.Typer(help="Messaging Service CLI: init project, manage plugins, and run services.")

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
CONFIG_PATH = PROJECT_ROOT / "messaging_service" / "config" / "config.yaml"
ENV_PATH = PROJECT_ROOT / "messaging_service" / ".env"
ENV_EXAMPLE_PATH = PROJECT_ROOT / "messaging_service" / ".env.example"
PLUGINS_DIR = PROJECT_ROOT / "messaging_service" / "plugins"


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _save_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)


def _ensure_file(path: Path, content: str = "") -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _plugins_list(cfg: dict) -> List[str]:
    plugins = cfg.get("plugins", {}).get("enabled", [])
    return list(plugins) if isinstance(plugins, list) else []


@APP.command()
def init(
    minimal: bool = typer.Option(False, "--minimal", help="Minimal setup: only admin_tools and analytics."),
    with_examples: bool = typer.Option(True, "--with-examples/--no-examples", help="Keep example plugins enabled"),
    force_env: bool = typer.Option(False, "--force-env", help="Overwrite .env with example if present"),
):
    """Initialize local environment and config for first run."""
    # .env
    if ENV_PATH.exists() and not force_env:
        print("[yellow].env already exists; keeping it. Use --force-env to overwrite.[/yellow]")
    else:
        if ENV_EXAMPLE_PATH.exists():
            shutil.copyfile(ENV_EXAMPLE_PATH, ENV_PATH)
            print(f"[green]Created {ENV_PATH} from .env.example[/green]")
        else:
            _ensure_file(ENV_PATH, "TELEGRAM_BOT_TOKEN=\nADMIN_REGISTER_SECRET=\n")
            print(f"[green]Created blank {ENV_PATH}[/green]")

    # Config
    cfg = _load_yaml(CONFIG_PATH)
    if not cfg:
        print(f"[red]Config not found at {CONFIG_PATH}. Please ensure the project is checked out correctly.[/red]")
        raise typer.Exit(code=1)

    enabled = _plugins_list(cfg)
    default_examples = ["echo", "questionnaire", "start_router", "location_demo", "menu", "product_catalog", "support"]
    base = {"admin_tools", "analytics"}

    if minimal:
        new_enabled = list(base)
    else:
        new_enabled = list(set(enabled) | base)
        if not with_examples:
            new_enabled = [p for p in new_enabled if p not in default_examples]

    cfg.setdefault("plugins", {})["enabled"] = sorted(new_enabled)
    _save_yaml(CONFIG_PATH, cfg)
    print(f"[green]Updated plugins.enabled -> {cfg['plugins']['enabled']}[/green]")

    # Ensure common store files exist (git-ignored)
    stores = [
        PROJECT_ROOT / "messaging_service" / "config" / "users.json",
        PROJECT_ROOT / "messaging_service" / "config" / "groups.json",
        PROJECT_ROOT / "messaging_service" / "config" / "support_tickets.json",
        PROJECT_ROOT / "messaging_service" / "config" / "invites.json",
    ]
    for s in stores:
        if not s.exists():
            if s.name.endswith(".json"):
                _ensure_file(s, "{\n  \"groups\": {}\n}\n" if s.name == "groups.json" else "{\n  \"users\": {},\n  \"admins\": []\n}\n")
                print(f"[green]Created {s}[/green]")

    print("[bold green]Init complete. Next:[/bold green] make install && make run-bot (and/or make run-api)")


@APP.command("enable-plugin")
def enable_plugin(name: str = typer.Argument(..., help="Plugin name (module under plugins/ without .py)")):
    cfg = _load_yaml(CONFIG_PATH)
    enabled = set(_plugins_list(cfg))
    enabled.add(name)
    cfg.setdefault("plugins", {})["enabled"] = sorted(enabled)
    _save_yaml(CONFIG_PATH, cfg)
    print(f"[green]Enabled plugin '{name}'. Now enabled: {cfg['plugins']['enabled']}[/green]")


@APP.command("disable-plugin")
def disable_plugin(name: str = typer.Argument(...)):
    cfg = _load_yaml(CONFIG_PATH)
    enabled = [p for p in _plugins_list(cfg) if p != name]
    cfg.setdefault("plugins", {})["enabled"] = enabled
    _save_yaml(CONFIG_PATH, cfg)
    print(f"[green]Disabled plugin '{name}'. Now enabled: {enabled}[/green]")


PLUGIN_TEMPLATE = '''"""Example plugin: {name}
Add your logic in handle(). Return True if the message was handled.
"""
from typing import Any

from messaging_service.core.message import Message  # type: ignore
from messaging_service.core.service import MessagingService  # type: ignore


class {class_name}:
    name = "{name}"

    async def handle(self, message: Message, service: MessagingService) -> bool:
        text = (message.content or "").strip()
        if text.lower().startswith("/{name}"):
            await service.send_message(chat_id=str(message.chat.id), text="Hello from {name}!")
            return True
        return False
'''


@APP.command("create-plugin")
def create_plugin(name: str = typer.Argument(..., help="snake_case plugin name")):
    mod_name = name.strip()
    if not mod_name.isidentifier():
        print("[red]Invalid plugin name. Use snake_case letters/underscores.[/red]")
        raise typer.Exit(code=1)
    target = PLUGINS_DIR / f"{mod_name}.py"
    if target.exists() and not Confirm.ask(f"{target} exists. Overwrite?", default=False):
        raise typer.Exit(code=0)
    class_name = "".join(part.capitalize() for part in mod_name.split("_"))
    content = PLUGIN_TEMPLATE.format(name=mod_name, class_name=class_name)
    _ensure_file(target, content)
    print(f"[green]Created plugin at {target}[/green]
Add to config.yaml under plugins.enabled: \n  - {mod_name}")


@APP.command("run-bot")
def run_bot():
    os.execvp(sys.executable, [sys.executable, str(PROJECT_ROOT / "messaging_service" / "main.py")])


@APP.command("run-api")
def run_api(host: str = "127.0.0.1", port: int = 8090, reload: bool = False):
    try:
        import uvicorn  # type: ignore
    except Exception:
        print("[red]uvicorn not installed. Run: make install[/red]")
        raise typer.Exit(code=1)
    kwargs = {"host": host, "port": port, "reload": reload}
    uvicorn.run("messaging_service.api_server:app", **kwargs)


def main():
    APP()


if __name__ == "__main__":
    main()
