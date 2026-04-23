"""
my-itau CLI

  my-itau doctor                        verify config + connectivity
  my-itau moves                         CC transactions (default card, current month)
  my-itau cards                         list credit cards
  my-itau accounts                      list bank accounts
  my-itau account-moves <hash>          bank account transactions
  my-itau set-card                      change default card
  my-itau config                        save credentials
  my-itau request get <path>            raw authenticated GET (escape hatch)
  my-itau serve                         start REST + MCP server
  my-itau mcp                           start stdio MCP server
"""

import json
import sys
from datetime import datetime
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .client import ItauAuthError, ItauClient, ItauSessionExpired

app = typer.Typer(
    name="my-itau",
    help="Itaú Uruguay banking — CLI, REST API, and MCP server.",
    invoke_without_command=True,
    no_args_is_help=False,
)

console = Console()
err = Console(stderr=True)
# spinner always goes to stderr so --json output stays clean
_status = Console(stderr=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json_out(data: object, exit_code: int = 0) -> None:
    """Print JSON to stdout and exit."""
    print(json.dumps(data, ensure_ascii=False, default=str))
    raise typer.Exit(exit_code)


def _json_error(message: str, exit_code: int = 1) -> None:
    _json_out({"error": message}, exit_code)


@app.callback()
def _root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    from .config import credentials
    doc, _ = credentials()
    if not doc:
        _onboarding()
    else:
        console.print(ctx.get_help())


def _onboarding() -> None:
    from .config import save, keyring_available

    console.print("\n[bold cyan]Welcome to my-itau![/bold cyan]")
    console.print("No credentials configured yet.\n")

    doc = typer.prompt("  Document number")
    pwd = typer.prompt("  Password", hide_input=True)

    in_keyring = save(doc, pwd)
    if in_keyring:
        console.print("\n[green]✓[/green] Credentials saved to [bold]system keyring[/bold]")
        console.print("[dim]  (document number in ~/.my-itau/config.json, password in keyring)[/dim]")
    else:
        console.print("\n[green]✓[/green] Credentials saved to [bold]~/.my-itau/config.json[/bold] (chmod 600)")
        console.print("[dim]  (keyring unavailable — consider installing a Secret Service on Linux)[/dim]")
    console.print("\nNext:\n")
    console.print("  [bold]my-itau doctor[/bold]     — verify setup")
    console.print("  [bold]my-itau moves[/bold]      — current month transactions")
    console.print("  [bold]my-itau cards[/bold]      — list credit cards\n")


def _require_credentials() -> None:
    from .config import credentials
    doc, pwd = credentials()
    if not doc or not pwd:
        console.print("\n[yellow]No credentials configured.[/yellow]\n")
        _onboarding()
        raise typer.Exit(0)


def _require_api_key() -> None:
    from .config import any_api_keys_configured, add_api_key, generate_api_key
    if any_api_keys_configured():
        return
    console.print("\n[dim]An API key protects the server from unauthorized access.[/dim]")
    choice = typer.prompt("  Set an API key? [Y/n]", default="Y")
    if choice.strip().lower() in ("n", "no"):
        console.print("[yellow]No API key set — server will be open.[/yellow]\n")
        return
    key = generate_api_key()
    in_keyring = add_api_key("default", key)
    console.print(f"\n[green]✓[/green] API key ([bold]default[/bold]) created:")
    console.print(f"  [bold cyan]{key}[/bold cyan]")
    console.print(f"  {'Stored in system keyring' if in_keyring else 'Stored in ~/.my-itau/config.json'}")
    console.print("  [dim]Save this — it won't be shown again.[/dim]\n")


def _pick_account(client: ItauClient) -> str:
    accounts = client.accounts
    if not accounts:
        err.print("[red]No bank accounts found.[/red]")
        raise typer.Exit(1)

    console.print("\n[bold]Select account:[/bold]\n")
    for i, a in enumerate(accounts, 1):
        atype    = escape(a.get("type") or "")
        holder   = escape(a.get("holder") or "")
        currency = escape(a.get("currency") or "")
        balance  = escape(str(a.get("balance") or ""))
        console.print(f"  [cyan]{i}[/cyan]  {atype}  [bold]{holder}[/bold]  {currency}  [dim]balance: {balance}[/dim]")

    console.print()
    choice = typer.prompt(f"Account (1-{len(accounts)})", default="1")
    try:
        idx = int(choice) - 1
        if not 0 <= idx < len(accounts):
            raise ValueError
    except ValueError:
        err.print("[red]Invalid selection.[/red]")
        raise typer.Exit(1)

    return accounts[idx]["hash"]


def _pick_card(client: ItauClient) -> str:
    from .config import save_default_card

    cards = client.credit_cards
    if not cards:
        err.print("[red]No credit cards found.[/red]")
        raise typer.Exit(1)

    console.print("\n[bold]Select default card:[/bold]\n")
    for i, c in enumerate(cards, 1):
        brand  = escape(c.get("brand") or "")
        number = escape(c.get("masked_number") or "")
        holder = escape(c.get("holder") or "")
        console.print(f"  [cyan]{i}[/cyan]  {brand} {number}  [dim]{holder}[/dim]")

    console.print()
    choice = typer.prompt(f"Card (1-{len(cards)})", default="1")
    try:
        idx = int(choice) - 1
        if not 0 <= idx < len(cards):
            raise ValueError
    except ValueError:
        err.print("[red]Invalid selection.[/red]")
        raise typer.Exit(1)

    selected = cards[idx]
    save_default_card(selected["hash"])
    brand  = escape(selected.get("brand") or "")
    number = escape(selected.get("masked_number") or "")
    console.print(f"\n[green]✓[/green] Default card → [bold]{brand} {number}[/bold]")
    console.print("[dim]Change anytime: my-itau set-card[/dim]\n")
    return selected["hash"]


def _get_client(json_mode: bool = False) -> ItauClient:
    _require_credentials()
    from .config import credentials
    doc, pwd = credentials()
    with _status.status("Logging in to Itaú..."):
        client = ItauClient()
        try:
            client.login(doc, pwd)
        except ItauAuthError as e:
            if json_mode:
                _json_error(str(e))
            err.print(f"[red]Auth error:[/red] {escape(str(e))}")
            err.print("Run [bold]my-itau config[/bold] to update credentials.")
            raise typer.Exit(1)
    return client


def _resolve_card_hash(client: ItauClient, card_opt: Optional[str]) -> str:
    """Return card hash from --card flag, stored default, or interactive pick."""
    if card_opt:
        return card_opt
    from .config import default_card
    h = default_card()
    if not h:
        h = _pick_card(client)
    return h


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

@app.command()
def doctor(
    json_mode: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Verify config, credentials, and connectivity."""
    import httpx
    from .config import load, CONFIG_FILE

    from .config import keyring_available, list_api_keys, any_api_keys_configured
    cfg = load()
    has_doc = bool(cfg.get("document_number"))
    has_pwd = bool(cfg.get("password"))
    has_card = bool(cfg.get("default_card"))
    kr_ok = keyring_available()
    api_keys = list_api_keys()
    active_keys = [k for k in api_keys if not k["expired"]]

    reachable = False
    http_status = None
    try:
        r = httpx.get("https://www.itaulink.com.uy/trx/", timeout=5, follow_redirects=False)
        http_status = r.status_code
        reachable = r.status_code < 500
    except Exception as e:
        http_status = str(e)

    ready = has_doc and has_pwd and reachable

    result = {
        "ready": ready,
        "config_file": str(CONFIG_FILE),
        "keyring": "available" if kr_ok else "unavailable (file fallback)",
        "credentials": {
            "document_number": "set" if has_doc else "missing",
            "password": "set" if has_pwd else "missing",
        },
        "api_keys": {
            "total": len(api_keys),
            "active": len(active_keys),
            "aliases": [k["alias"] for k in active_keys],
        },
        "default_card": cfg.get("default_card") or "not set",
        "connectivity": {
            "itaulink.com.uy": "ok" if reachable else "unreachable",
            "http_status": http_status,
        },
    }

    if json_mode:
        _json_out(result, exit_code=0 if ready else 1)

    # Human output
    tick = "[green]✓[/green]"
    cross = "[red]✗[/red]"

    apikey_label = (
        f"[green]{len(active_keys)} active[/green]"
        + (f" [dim]({', '.join(k['alias'] for k in active_keys)})[/dim]" if active_keys else "")
        if active_keys else "[yellow]none — run: my-itau api-key add <alias>[/yellow]"
    )

    console.print("\n[bold]my-itau doctor[/bold]\n")
    console.print(f"  {tick if has_doc else cross} document_number {'set' if has_doc else '[red]missing[/red]'}")
    console.print(f"  {tick if has_pwd else cross} password {'set' if has_pwd else '[red]missing[/red]'}")
    console.print(f"  {tick if has_card else '[yellow]·[/yellow]'} default_card {escape(cfg.get('default_card') or '[yellow]not set — run: my-itau set-card[/yellow]')}")
    console.print(f"  {tick if kr_ok else '[yellow]·[/yellow]'} keyring {'[green]system keyring[/green]' if kr_ok else '[yellow]unavailable — file fallback[/yellow]'}")
    console.print(f"  {tick if active_keys else '[yellow]·[/yellow]'} api_keys {apikey_label}")
    console.print(f"  {tick if reachable else cross} itaulink.com.uy {'reachable' if reachable else '[red]unreachable[/red]'} [dim](HTTP {http_status})[/dim]")

    if ready:
        console.print("\n[green]Ready.[/green]\n")
    else:
        console.print("\n[red]Not ready.[/red] Fix issues above.\n")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

@app.command()
def config(
    document_number: str = typer.Option(..., prompt="Document number"),
    password: str = typer.Option(..., prompt="Password", hide_input=True),
) -> None:
    """Save credentials (password to system keyring when available)."""
    from .config import save
    in_keyring = save(document_number, password)
    if in_keyring:
        console.print("[green]✓[/green] Password saved to [bold]system keyring[/bold]")
        console.print("[dim]  document number in ~/.my-itau/config.json[/dim]")
    else:
        console.print("[green]✓[/green] Saved to [bold]~/.my-itau/config.json[/bold] (chmod 600)")


# ---------------------------------------------------------------------------
# api-key
# ---------------------------------------------------------------------------

_apikey_app = typer.Typer(help="Manage API keys for the REST/MCP server.")
app.add_typer(_apikey_app, name="api-key")


@_apikey_app.command(name="add")
def apikey_add(
    alias:      str           = typer.Argument(..., help="Short name for this key (e.g. home, ci, mobile)"),
    expires:    Optional[str] = typer.Option(None, "--expires", "-e", help="Expiry date YYYY-MM-DD (optional)"),
    key:        Optional[str] = typer.Option(None, "--key", "-k", help="Provide your own key (auto-generated if omitted)"),
) -> None:
    """Add a new API key."""
    from .config import add_api_key, generate_api_key, list_api_keys

    existing = [k["alias"] for k in list_api_keys()]
    if alias in existing:
        if not typer.confirm(f"Key '{alias}' already exists. Replace it?"):
            raise typer.Exit(0)

    expires_at: Optional[str] = None
    if expires:
        try:
            dt = datetime.strptime(expires, "%Y-%m-%d")
            expires_at = dt.strftime("%Y-%m-%dT00:00:00")
        except ValueError:
            err.print("[red]Invalid date format. Use YYYY-MM-DD.[/red]")
            raise typer.Exit(1)

    raw_key = key or generate_api_key()
    in_keyring = add_api_key(alias, raw_key, expires_at)

    console.print(f"\n[green]✓[/green] API key [bold]{alias}[/bold] added")
    console.print(f"  Key: [bold cyan]{raw_key}[/bold cyan]")
    if expires_at:
        console.print(f"  Expires: [yellow]{expires}[/yellow]")
    console.print(f"  {'Stored in system keyring' if in_keyring else 'Stored in ~/.my-itau/config.json'}")
    console.print("  [dim]Save this — it won't be shown again.[/dim]\n")


@_apikey_app.command(name="list")
def apikey_list(
    json_mode: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """List all API keys and their status."""
    from .config import list_api_keys

    keys = list_api_keys()

    if json_mode:
        _json_out(keys)

    if not keys:
        console.print("[dim]No API keys configured. Run: my-itau api-key add <alias>[/dim]")
        return

    table = Table(title="API Keys", show_lines=False)
    table.add_column("Alias", style="cyan")
    table.add_column("Created", style="dim")
    table.add_column("Expires")
    table.add_column("Status")
    table.add_column("Storage", style="dim")

    for k in keys:
        if k["expired"]:
            status_str = "[red]expired[/red]"
        else:
            status_str = "[green]active[/green]"
        expires_str = escape(k["expires_at"] or "never")
        created_str = escape((k["created_at"] or "")[:10])
        storage_str = "keyring" if k["in_keyring"] else "file"
        table.add_row(escape(k["alias"]), created_str, expires_str, status_str, storage_str)

    console.print(table)


@_apikey_app.command(name="remove")
def apikey_remove(
    alias: str = typer.Argument(..., help="Alias of the key to remove"),
    yes:   bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Remove an API key by alias."""
    from .config import remove_api_key

    if not yes:
        typer.confirm(f"Remove API key '{alias}'?", abort=True)

    removed = remove_api_key(alias)
    if removed:
        console.print(f"[green]✓[/green] Removed key [bold]{alias}[/bold]")
    else:
        err.print(f"[red]No key with alias '{alias}' found.[/red]")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# cards
# ---------------------------------------------------------------------------

@app.command()
def cards(
    json_mode: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """List credit cards."""
    client = _get_client(json_mode)

    if json_mode:
        _json_out([{k: v for k, v in c.items() if k != "raw"} for c in client.credit_cards])

    table = Table(title="Credit Cards")
    table.add_column("#", style="dim", width=2)
    table.add_column("Brand", style="cyan")
    table.add_column("Number")
    table.add_column("Holder")
    table.add_column("Expiry")
    table.add_column("Currency")
    table.add_column("Limit", justify="right")
    table.add_column("Status")
    table.add_column("Hash", style="dim")

    from .config import default_card
    default = default_card()

    for i, c in enumerate(client.credit_cards, 1):
        h = c.get("hash") or ""
        marker = " [green]●[/green]" if h == default else ""
        table.add_row(
            str(i) + marker,
            escape(c.get("brand") or ""),
            escape(c.get("masked_number") or ""),
            escape(c.get("holder") or ""),
            escape(c.get("expiry") or ""),
            escape(c.get("currency") or ""),
            escape(str(c.get("limit") or "")),
            escape(c.get("status") or ""),
            escape(h[:16]) + "…",
        )

    console.print(table)
    if default:
        console.print("[dim]● = default card  |  change: my-itau set-card[/dim]")


# ---------------------------------------------------------------------------
# set-card
# ---------------------------------------------------------------------------

@app.command(name="set-card")
def set_card() -> None:
    """Change the default credit card used for moves."""
    from .config import default_card
    client = _get_client()
    current = default_card()
    if current:
        obj = next((c for c in client.credit_cards if c["hash"] == current), None)
        if obj:
            brand  = escape(obj.get("brand") or "")
            number = escape(obj.get("masked_number") or "")
            console.print(f"\nCurrent default: [bold]{brand} {number}[/bold]")
    _pick_card(client)


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------

@app.command()
def accounts(
    json_mode: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """List bank accounts."""
    client = _get_client(json_mode)

    if json_mode:
        _json_out(client.accounts)

    table = Table(title="Bank Accounts")
    table.add_column("Type", style="cyan")
    table.add_column("Holder")
    table.add_column("Currency")
    table.add_column("Balance", justify="right", style="bold")
    table.add_column("Hash", style="dim")

    for a in client.accounts:
        table.add_row(
            escape(a.get("type") or ""),
            escape(a.get("holder") or ""),
            escape(a.get("currency") or ""),
            escape(str(a.get("balance") or "")),
            escape((a.get("hash") or "")[:16]) + "…",
        )

    console.print(table)


# ---------------------------------------------------------------------------
# moves
# ---------------------------------------------------------------------------

@app.command()
def moves(
    month: Optional[int] = typer.Option(None, "--month", "-m", help="Month 1-12 (default: current)"),
    year:  Optional[int] = typer.Option(None, "--year",  "-y", help="Year e.g. 2026 (default: current)"),
    card:  Optional[str] = typer.Option(None, "--card",  "-c", help="Card hash override"),
    all_cards: bool      = typer.Option(False, "--all",  "-a", help="All cards"),
    json_mode: bool      = typer.Option(False, "--json",       help="Machine-readable output"),
) -> None:
    """Credit card transactions."""
    client = _get_client(json_mode)

    if not client.credit_cards:
        if json_mode:
            _json_error("no credit cards found", 1)
        err.print("[red]No credit cards found.[/red]")
        raise typer.Exit(1)

    if all_cards:
        targets = client.credit_cards
    else:
        hash_ = _resolve_card_hash(client, card)
        targets = [next((c for c in client.credit_cards if c["hash"] == hash_), client.credit_cards[0])]

    all_results = []

    for card_obj in targets:
        hash_ = card_obj["hash"]
        label = f"{card_obj.get('brand', '')} {card_obj.get('masked_number', hash_)}"

        with _status.status(f"Fetching {label}..."):
            try:
                move_list = client.get_credit_card_moves(hash_, month, year)
            except ItauSessionExpired:
                if json_mode:
                    _json_error("session expired", 1)
                err.print("[red]Session expired.[/red]")
                raise typer.Exit(1)
            except Exception as e:
                if json_mode:
                    _json_error(str(e), 1)
                err.print(f"[red]Error:[/red] {escape(str(e))}")
                raise typer.Exit(1)

        if json_mode:
            all_results.append({"card": {k: v for k, v in card_obj.items() if k != "raw"}, "moves": move_list})
            continue

        from .normalizers import cc_transaction, is_payment
        move_list = [cc_transaction(m) for m in move_list if not is_payment(m)]

        period = f"{month}/{year}" if month else "current month"
        table = Table(title=f"{escape(label)} — {period}", show_lines=False)
        table.add_column("Date", style="cyan", no_wrap=True)
        table.add_column("Merchant")
        table.add_column("Detail", style="dim")
        table.add_column("Inst.", justify="center", style="dim")
        table.add_column("Amount", justify="right", style="bold")
        table.add_column("Cur", style="dim")

        for m in move_list:
            ta   = m.get("transactionAmount") or {}
            inst = (m.get("cardTransaction") or {}).get("installments") or {}
            inst_str = f"{inst['current']}/{inst['total']}" if inst else ""
            table.add_row(
                escape(m.get("bookingDate") or ""),
                escape(m.get("creditorName") or ""),
                escape(m.get("remittanceInformationUnstructured") or ""),
                escape(inst_str),
                escape(ta.get("amount") or ""),
                escape(ta.get("currency") or ""),
            )

        console.print(table)

        totals: dict[str, float] = {}
        for m in move_list:
            ta = m.get("transactionAmount") or {}
            cur = ta.get("currency") or "?"
            totals[cur] = round(totals.get(cur, 0.0) + abs(float(ta.get("amount") or 0)), 2)

        for cur, total in totals.items():
            console.print(f"  Total {escape(cur)}: [bold]{total:,.2f}[/bold]")
        console.print(f"  [dim]{len(move_list)} transactions[/dim]\n")

    if json_mode:
        _json_out(all_results if all_cards else all_results[0]["moves"])


# ---------------------------------------------------------------------------
# account-moves
# ---------------------------------------------------------------------------

@app.command(name="account-moves")
def account_moves(
    account_hash: Optional[str] = typer.Argument(None, help="Account hash (omit to pick interactively)"),
    month:  Optional[int]       = typer.Option(None, "--month", "-m", help="Month 1-12"),
    year:   Optional[int]       = typer.Option(None, "--year",  "-y", help="Year e.g. 2026"),
    json_mode: bool             = typer.Option(False, "--json",       help="Machine-readable output"),
) -> None:
    """Bank account transactions."""
    client = _get_client(json_mode)

    if not account_hash:
        if json_mode:
            _json_error("account_hash required in --json mode", 1)
        account_hash = _pick_account(client)

    with _status.status("Fetching account moves..."):
        try:
            move_list = client.get_account_moves(account_hash, month, year)
        except ItauSessionExpired:
            if json_mode:
                _json_error("session expired", 1)
            err.print("[red]Session expired.[/red]")
            raise typer.Exit(1)
        except Exception as e:
            if json_mode:
                _json_error(str(e), 1)
            err.print(f"[red]Error:[/red] {escape(str(e))}")
            raise typer.Exit(1)

    from .normalizers import account_transaction, currency_code

    # Resolve account currency from the account list (moves don't carry moneda)
    account_obj = next((a for a in client.accounts if a.get("hash") == account_hash), None)
    raw_currency = account_obj.get("currency") or "" if account_obj else ""
    acct_currency = currency_code(raw_currency)
    acct_label = escape(account_obj.get("holder") or account_hash[:12]) if account_obj else escape(account_hash[:12])

    normalized = [account_transaction(m, raw_currency) for m in move_list]

    if json_mode:
        _json_out({"transactions": {"booked": normalized}})

    period = f"{month}/{year}" if month else "current month"
    table = Table(title=f"{acct_label} — {period}", show_lines=False)
    table.add_column("Date", style="cyan", no_wrap=True)
    table.add_column("Description")
    table.add_column("Amount", justify="right", style="bold")
    table.add_column("Cur", style="dim")
    table.add_column("Balance", justify="right", style="dim")

    for m in normalized:
        ta      = m.get("transactionAmount") or {}
        bal     = (m.get("balanceAfterTransaction") or {}).get("balanceAmount") or {}
        cur     = ta.get("currency") or acct_currency
        table.add_row(
            escape(m.get("bookingDate") or ""),
            escape(m.get("remittanceInformationUnstructured") or ""),
            escape(ta.get("amount") or ""),
            escape(cur),
            escape(bal.get("amount") or ""),
        )

    console.print(table)
    console.print(f"[dim]{len(normalized)} transactions[/dim]")


# ---------------------------------------------------------------------------
# request  (read-only escape hatch)
# ---------------------------------------------------------------------------

_request_app = typer.Typer(help="Raw authenticated requests to itaulink.com.uy (escape hatch).")
app.add_typer(_request_app, name="request")


@_request_app.command(name="get")
def request_get(
    path: str           = typer.Argument(..., help="Path e.g. /trx/"),
    json_mode: bool     = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Authenticated GET to itaulink.com.uy. Read-only."""
    client = _get_client(json_mode)

    with _status.status(f"GET {path}..."):
        try:
            r = client._ajax_post(path, b"{}") if path != "/trx/" else client._http.get(path)
            # For most paths an ajax POST is needed; for /trx/ a plain GET
        except Exception as e:
            if json_mode:
                _json_error(str(e))
            err.print(f"[red]Request failed:[/red] {escape(str(e))}")
            raise typer.Exit(1)

    if json_mode:
        try:
            _json_out({"status": r.status_code, "body": r.json()})
        except Exception:
            _json_out({"status": r.status_code, "body": r.text})

    console.print(f"[dim]HTTP {r.status_code}[/dim]\n")
    try:
        console.print_json(r.text)
    except Exception:
        console.print(r.text)


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------

@app.command()
def reset(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete ~/.my-itau/ and all stored credentials."""
    from .config import CONFIG_DIR

    if not CONFIG_DIR.exists():
        console.print("[dim]Nothing to reset — ~/.my-itau/ does not exist.[/dim]")
        return

    if not yes:
        typer.confirm("Delete ~/.my-itau/ and all stored credentials?", abort=True)

    from .config import purge_keyring
    purge_keyring()

    import shutil
    shutil.rmtree(CONFIG_DIR)
    console.print("[green]✓[/green] Removed [bold]~/.my-itau/[/bold] and cleared keyring entries")
    console.print("[dim]Run my-itau config to set up again.[/dim]")


# ---------------------------------------------------------------------------
# serve / mcp
# ---------------------------------------------------------------------------

@app.command()
def serve(
    host:   str  = typer.Option("0.0.0.0", "--host",   help="Bind host"),
    port:   int  = typer.Option(8787,      "--port", "-p", help="Bind port"),
    reload: bool = typer.Option(False,     "--reload",  help="Auto-reload on code changes"),
) -> None:
    """Start REST + MCP server."""
    _require_credentials()
    _require_api_key()
    import uvicorn

    console.print("[bold]my-itau server[/bold]")
    console.print(f"  REST → http://{host}:{port}/")
    console.print(f"  MCP  → http://{host}:{port}/mcp/sse")
    console.print(f"  Docs → http://{host}:{port}/docs\n")

    uvicorn.run("my_itau.server:app", host=host, port=port, reload=reload, log_level="info")


@app.command()
def mcp() -> None:
    """
    Start MCP server (stdio, for Claude Desktop).

      { "mcpServers": { "my-itau": { "command": "my-itau", "args": ["mcp"] } } }
    """
    _require_credentials()
    _require_api_key()
    from .server import mcp as _mcp
    _mcp.run()


def main():
    app()


if __name__ == "__main__":
    main()
