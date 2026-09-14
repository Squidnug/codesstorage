import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("code-bot")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "codes.db"))
try:
    BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0"))
except ValueError:
    BOT_OWNER_ID = 0

MAX_CODES_PER_REQUEST = 50
MAX_HISTORY_ENTRIES = 25


class CodeDatabase:
    """Small SQLite store. Every query is scoped to a Discord guild ID."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    allowed_role_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    claimed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    code TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'available'
                        CHECK (status IN ('available', 'claimed')),
                    added_by INTEGER NOT NULL,
                    added_at TEXT NOT NULL,
                    claim_id INTEGER,
                    UNIQUE (guild_id, code),
                    FOREIGN KEY (claim_id) REFERENCES claims(id)
                );

                CREATE INDEX IF NOT EXISTS idx_codes_available
                    ON codes(guild_id, status, id);
                CREATE INDEX IF NOT EXISTS idx_claims_guild
                    ON claims(guild_id, id DESC);
                """
            )

    def set_role(self, guild_id: int, role_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO guild_settings(guild_id, allowed_role_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    allowed_role_id = excluded.allowed_role_id,
                    updated_at = excluded.updated_at
                """,
                (guild_id, role_id, now),
            )

    def get_role_id(self, guild_id: int) -> int | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT allowed_role_id FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        return int(row["allowed_role_id"]) if row else None

    def add_codes(self, guild_id: int, user_id: int, codes: list[str]) -> tuple[int, int]:
        now = datetime.now(timezone.utc).isoformat()
        added = 0
        with self._write_lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for code in codes:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO codes
                        (guild_id, code, status, added_by, added_at)
                    VALUES (?, ?, 'available', ?, ?)
                    """,
                    (guild_id, code, user_id, now),
                )
                added += cursor.rowcount
            connection.commit()
        return added, len(codes) - added

    def claim_codes(
        self, guild_id: int, user_id: int, username: str, quantity: int
    ) -> tuple[list[str], int]:
        """Atomically reserve codes so two requests can never receive the same code."""
        with self._write_lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT id, code FROM codes
                WHERE guild_id = ? AND status = 'available'
                ORDER BY id
                LIMIT ?
                """,
                (guild_id, quantity),
            ).fetchall()

            if len(rows) < quantity:
                available = len(rows)
                connection.rollback()
                return [], available

            now = datetime.now(timezone.utc).isoformat()
            claim_cursor = connection.execute(
                """
                INSERT INTO claims(guild_id, user_id, username, quantity, claimed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (guild_id, user_id, username, quantity, now),
            )
            claim_id = claim_cursor.lastrowid
            code_ids = [int(row["id"]) for row in rows]
            placeholders = ",".join("?" for _ in code_ids)
            cursor = connection.execute(
                f"""
                UPDATE codes SET status = 'claimed', claim_id = ?
                WHERE guild_id = ? AND status = 'available'
                  AND id IN ({placeholders})
                """,
                (claim_id, guild_id, *code_ids),
            )
            if cursor.rowcount != quantity:
                connection.rollback()
                raise RuntimeError("The claim changed during processing; no codes were issued.")
            connection.commit()
            return [str(row["code"]) for row in rows], quantity

    def counts(self, guild_id: int) -> tuple[int, int, int]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'available' THEN 1 ELSE 0 END) AS available,
                    SUM(CASE WHEN status = 'claimed' THEN 1 ELSE 0 END) AS claimed
                FROM codes WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
        return int(row["total"] or 0), int(row["available"] or 0), int(row["claimed"] or 0)

    def history(self, guild_id: int, limit: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT id, user_id, username, quantity, claimed_at
                FROM claims WHERE guild_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (guild_id, limit),
            ).fetchall()


database = CodeDatabase(DATABASE_PATH)


class CodeBot(commands.Bot):
    async def setup_hook(self) -> None:
        await self.tree.sync()
        log.info("Slash commands synchronized.")


bot = CodeBot(command_prefix="!", intents=discord.Intents.default())


def is_owner(user_id: int) -> bool:
    return BOT_OWNER_ID != 0 and user_id == BOT_OWNER_ID


async def guild_only(interaction: discord.Interaction) -> bool:
    if interaction.guild is None or interaction.guild_id is None:
        await interaction.response.send_message(
            "This command can only be used inside a Discord server.", ephemeral=True
        )
        return False
    return True


async def require_access(interaction: discord.Interaction) -> bool:
    if not await guild_only(interaction):
        return False
    if is_owner(interaction.user.id):
        return True

    role_id = database.get_role_id(interaction.guild_id)
    if role_id is None:
        await interaction.response.send_message(
            "This server has not been configured. An administrator must run `/setup`.",
            ephemeral=True,
        )
        return False

    if isinstance(interaction.user, discord.Member):
        if any(role.id == role_id for role in interaction.user.roles):
            return True

    await interaction.response.send_message(
        "You do not have the role required to use this bot.", ephemeral=True
    )
    return False


@bot.tree.command(name="setup", description="Choose the role allowed to use the code bot.")
@app_commands.describe(role="The role that can use the bot")
async def setup(interaction: discord.Interaction, role: discord.Role) -> None:
    if not await guild_only(interaction):
        return
    member = interaction.user
    administrator = isinstance(member, discord.Member) and member.guild_permissions.administrator
    if not is_owner(member.id) and not administrator:
        await interaction.response.send_message(
            "Only the bot owner or a server administrator can run `/setup`.", ephemeral=True
        )
        return
    database.set_role(interaction.guild_id, role.id)
    await interaction.response.send_message(
        f"Setup complete. Members with {role.mention} can now use the bot.", ephemeral=True
    )


@bot.tree.command(name="addcodes", description="Add one or more one-time codes.")
@app_commands.describe(codes="Codes separated by spaces, commas, or new lines")
async def addcodes(interaction: discord.Interaction, codes: str) -> None:
    if not await require_access(interaction):
        return

    normalized = [code.strip() for code in codes.replace(",", " ").split() if code.strip()]
    if not normalized:
        await interaction.response.send_message("No valid codes were provided.", ephemeral=True)
        return

    added, duplicates = database.add_codes(interaction.guild_id, interaction.user.id, normalized)
    await interaction.response.send_message(
        f"Added **{added}** code(s). Skipped **{duplicates}** duplicate code(s).",
        ephemeral=False,
    )


@bot.tree.command(name="getcodes", description="Claim unused one-time codes.")
@app_commands.describe(amount=f"Number of codes to claim (1-{MAX_CODES_PER_REQUEST})")
async def getcodes(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 50]) -> None:
    if not await require_access(interaction):
        return

    claimed, available = database.claim_codes(
        interaction.guild_id,
        interaction.user.id,
        str(interaction.user),
        amount,
    )
    if not claimed:
        await interaction.response.send_message(
            f"You requested **{amount}**, but only **{available}** code(s) are available. "
            "No codes were claimed.",
            ephemeral=False,
        )
        return

    formatted = "\n".join(f"`{code}`" for code in claimed)
    await interaction.response.send_message(
        f"You claimed **{amount}** code(s):\n\n{formatted}\n\n"
        "These codes are now permanently marked as claimed.",
        ephemeral=False,
    )


@bot.tree.command(name="available", description="Show the number of unused codes remaining.")
async def available(interaction: discord.Interaction) -> None:
    if not await require_access(interaction):
        return
    _, available_count, _ = database.counts(interaction.guild_id)
    await interaction.response.send_message(
        f"**{available_count}** unused code(s) are available.", ephemeral=False
    )


@bot.tree.command(name="stats", description="Show total, available, and claimed code counts.")
async def stats(interaction: discord.Interaction) -> None:
    if not await require_access(interaction):
        return
    total, available_count, claimed = database.counts(interaction.guild_id)
    embed = discord.Embed(title="Code Statistics", color=discord.Color.blurple())
    embed.add_field(name="Total", value=str(total), inline=True)
    embed.add_field(name="Available", value=str(available_count), inline=True)
    embed.add_field(name="Claimed", value=str(claimed), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=False)


@bot.tree.command(name="history", description="Show recent code claims in this server.")
@app_commands.describe(limit=f"Number of claims to show (1-{MAX_HISTORY_ENTRIES})")
async def history(
    interaction: discord.Interaction,
    limit: app_commands.Range[int, 1, 25] = 10,
) -> None:
    if not await require_access(interaction):
        return
    rows = database.history(interaction.guild_id, limit)
    if not rows:
        await interaction.response.send_message("No codes have been claimed yet.", ephemeral=False)
        return

    lines = []
    for row in rows:
        timestamp = datetime.fromisoformat(row["claimed_at"])
        unix_time = int(timestamp.timestamp())
        lines.append(
            f"**#{row['id']}** • <@{row['user_id']}> claimed "
            f"**{row['quantity']}** code(s) • <t:{unix_time}:f>"
        )
    embed = discord.Embed(
        title="Recent Code Claims",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Exact claimed codes remain private.")
    await interaction.response.send_message(embed=embed, ephemeral=False)


@bot.event
async def on_ready() -> None:
    if bot.user:
        log.info("Logged in as %s (%s)", bot.user, bot.user.id)


def main() -> None:
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing. Add it to .env or Railway Variables.")
    if BOT_OWNER_ID == 0:
        raise RuntimeError("BOT_OWNER_ID is missing or invalid.")
    bot.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
